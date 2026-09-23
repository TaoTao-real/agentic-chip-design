from __future__ import annotations

import json
import shutil
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from chia.base.ChiaFunction import get

from .artifacts import (
    CandidateArtifact,
    EvaluationArtifact,
    dump_json,
    load_json,
    sha256_file,
    sha256_text,
)
from .core import (
    SYSTEM_PROMPT,
    apply_exact_edits,
    audit_prompt_for_answer_leak,
    build_iteration_digest_rows,
    build_schedule,
    build_user_prompt,
    canonical_hash,
    choose_parent,
    duplicate_candidate_id,
    evaluation_feedback_rows,
    lineage_fields,
    search_evaluation_data,
    summarize,
    update_validity,
    validate_config,
    validate_generic_episode_bundle,
    validate_proposal,
)
from .deepseek import DeepSeekOfficialLLM, parse_result
from .environment import load_config
from .frozen import (
    TOOL_FILES,
    baseline_rtl,
    regression_binary,
    tool_file,
    verify_frozen_run,
)
from .nodes import BoomCandidateEvaluationNode


_STATE_LOCK = threading.Lock()


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def validate_process_memory_bundle(value: Any) -> None:
    validate_generic_episode_bundle(value)
    if isinstance(value, list):
        count = len(value)
    elif isinstance(value, dict) and isinstance(value.get("episodes"), list):
        count = len(value["episodes"])
    else:
        count = 1
    if count == 0:
        raise RuntimeError("formal arm D requires non-empty process memory")


def packaged_process_memory() -> list[dict[str, Any]]:
    """Load the non-empty public cross-target bundle used by arm D."""
    episode_root = Path(__file__).resolve().parent / "design_episodes"
    episodes = []
    for path in sorted(episode_root.glob("*.json")):
        episode = load_json(path)
        if episode.get("knowledge_class") == "cross-target-process-memory":
            episodes.append(episode)
    validate_process_memory_bundle(episodes)
    return episodes


def init_campaign(
    config_path: Path,
    campaign: Path,
    *,
    preflight: bool,
    force: bool = False,
) -> dict[str, Any]:
    config = load_config(config_path)
    validate_config(config, formal=not preflight)
    if campaign.exists():
        if not force:
            raise FileExistsError(
                f"campaign already exists; use resume or a new path: {campaign}"
            )
        shutil.rmtree(campaign)
    campaign.mkdir(parents=True)
    schedule = build_schedule(config, preflight=preflight)
    frozen_source = Path(config["remote"]["frozen_inputs_root"])
    for target in config["targets"]:
        for filename in ("baseline-source.scala", "baseline-timing.txt", "baseline-ppa.json"):
            _copy_file(
                frozen_source / target / filename,
                campaign / "frozen/targets" / target / filename,
            )
        rtl_source = baseline_rtl(config, target)
        rtl_target = campaign / "frozen/targets" / target / "baseline-rtl"
        if not rtl_source.is_dir():
            raise RuntimeError(f"qualified baseline RTL is missing: {rtl_source}")
        shutil.copytree(rtl_source, rtl_target)
    for name in TOOL_FILES:
        _copy_file(tool_file(config, name), campaign / "frozen/tools" / name)
    _copy_file(regression_binary(config), campaign / "frozen/tests/rsort.riscv")
    required_patch = config.get("remote", {}).get("required_patch")
    if required_patch:
        frozen_patch = campaign / "frozen/base/required.patch"
        _copy_file(Path(required_patch), frozen_patch)
        config["remote"]["required_patch"] = str(frozen_patch.resolve())
    replay_source = Path(config["remote"]["replay_root"])
    frozen_replay = campaign / "frozen/q1-replay"
    if replay_source.is_dir():
        shutil.copytree(replay_source, frozen_replay)
    else:
        frozen_replay.mkdir(parents=True)
    config["remote"]["replay_root"] = str(frozen_replay.resolve())
    episodes_source = frozen_source / "design-episodes.json"
    if episodes_source.exists():
        episodes = load_json(episodes_source)
        validate_process_memory_bundle(episodes)
        _copy_file(episodes_source, campaign / "frozen/design-episodes.json")
    else:
        dump_json(
            campaign / "frozen/design-episodes.json", packaged_process_memory()
        )
    config["frozen_run_root"] = str((campaign / "frozen").resolve())
    manifest = {
        "version": config["version"],
        "kind": "preflight" if preflight else "formal",
        "config": config,
        "config_sha256": canonical_hash(config),
        "status": "initialized",
        "manual_candidate_edits_allowed": False,
        "schedule": [item.__dict__ | {"id": item.id} for item in schedule],
        "created_epoch": time.time(),
    }
    dump_json(campaign / "manifest.json", manifest)
    for item in schedule:
        run_dir = campaign / "runs" / item.id
        run_dir.mkdir(parents=True)
        dump_json(
            run_dir / "run.json",
            {
                "id": item.id,
                **item.__dict__,
                "status": "pending",
                "model_calls": 0,
                "candidates": [],
            },
        )
    return manifest


def write_evidence_hashes(root: Path, output: Path) -> None:
    values = {
        str(path.relative_to(root)): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path != output
    }
    dump_json(output, values)


def _load_pairs(state: dict[str, Any]) -> list[tuple[CandidateArtifact, EvaluationArtifact]]:
    pairs = []
    for row in state["candidates"]:
        evaluation_data = search_evaluation_data(row)
        if row.get("candidate") and evaluation_data:
            pairs.append(
                (
                    CandidateArtifact.from_dict(row["candidate"]),
                    EvaluationArtifact.from_dict(evaluation_data),
                )
            )
    return pairs


def _save_state(path: Path, state: dict[str, Any]) -> None:
    with _STATE_LOCK:
        dump_json(path, state)


def _provider_token_count(metadata: dict[str, Any]) -> int | None:
    usage = metadata.get("usage")
    return usage.get("total_tokens") if isinstance(usage, dict) else None


def _evaluate_with_infra_retries(
    *,
    node: BoomCandidateEvaluationNode,
    candidate: CandidateArtifact,
    target: dict[str, Any],
    config: dict[str, Any],
    baseline: dict[str, Any],
    candidate_dir: Path,
    start_attempt: int = 1,
    prior_attempts: list[dict[str, Any]] | None = None,
) -> tuple[EvaluationArtifact, int, list[dict[str, Any]]]:
    attempt_limit = int(config["search"]["infrastructure_attempt_limit"])
    last = EvaluationArtifact(candidate_id=candidate.id)
    attempts_used = start_attempt - 1
    records = list(prior_attempts or [])
    for attempt in range(start_attempt, attempt_limit + 1):
        attempts_used = attempt
        output = candidate_dir / f"evaluation-attempt-{attempt:02d}"
        ref = node.evaluate.chia_remote(
            node,
            candidate=candidate,
            target=target,
            config=config,
            baseline=baseline,
            output_dir=str(output),
            _chia_display_name=f"evaluate:{candidate.id}:attempt-{attempt}",
        )
        last = get(ref)
        dump_json(candidate_dir / "evaluation.json", last)
        records.append({
            "attempt": attempt,
            "output_dir": str(output),
            "active_seconds": last.active_seconds,
            "status": last.status,
            "retryable": last.retryable,
            "failure_class": last.failure_class,
            "evaluation": last.to_dict(),
        })
        if last.status != "infra_blocked" or not last.retryable:
            return last, attempts_used, records
    return last, attempts_used, records


def execute_run(campaign: Path, run_id: str) -> dict[str, Any]:
    manifest = load_json(campaign / "manifest.json")
    verify_frozen_run(campaign / "frozen", manifest.get("frozen_run_fingerprint"))
    config = manifest["config"]
    run_path = campaign / "runs" / run_id
    state_path = run_path / "run.json"
    state = load_json(state_path)
    target = config["targets"][state["target"]]
    baseline_source = (campaign / "frozen/targets" / state["target"] / "baseline-source.scala").read_text()
    timing = (campaign / "frozen/targets" / state["target"] / "baseline-timing.txt").read_text()
    baseline = load_json(campaign / "frozen/targets" / state["target"] / "baseline-ppa.json")
    baseline["maximum_lut_ratio"] = config["physical"]["maximum_lut_ratio"]
    episodes_path = campaign / "frozen/design-episodes.json"
    episodes = episodes_path.read_text() if episodes_path.exists() else ""
    knowledge_episode_ids: list[str] = []
    knowledge_sha256: str | None = None
    if state["arm"] == "D" and not episodes.strip():
        raise RuntimeError("arm D requires a non-empty frozen process-memory bundle")
    if state["arm"] == "D":
        knowledge_value = json.loads(episodes)
        validate_process_memory_bundle(knowledge_value)
        knowledge_rows = (
            knowledge_value
            if isinstance(knowledge_value, list)
            else knowledge_value.get("episodes", [knowledge_value])
        )
        knowledge_episode_ids = [
            str(item.get("episode_id")) for item in knowledge_rows
        ]
        knowledge_sha256 = sha256_text(episodes)
    llm = DeepSeekOfficialLLM(
        system_message=SYSTEM_PROMPT,
        model=config["model"]["id"],
        base_url=config["model"]["api_base"],
        temperature=float(config["model"]["temperature"]),
        max_tokens=int(config["model"]["max_output_tokens"]),
        timeout_seconds=int(config["model"]["timeout_seconds"]),
        attempts=int(config["model"]["attempt_limit"]),
        api_key_env=config["model"]["api_key_env"],
    )
    evaluator = BoomCandidateEvaluationNode()
    state["status"] = "running"
    state.setdefault("started_epoch", time.time())
    _save_state(state_path, state)

    # Resume an already generated candidate after infrastructure failure without
    # issuing or counting a new model request.
    if state["candidates"]:
        pending = state["candidates"][-1]
        evaluation_data = search_evaluation_data(pending)
        if evaluation_data and evaluation_data.get("status") == "infra_blocked" and evaluation_data.get("retryable"):
            candidate = CandidateArtifact.from_dict(pending["candidate"])
            next_attempt = int(pending.get("infrastructure_attempts", 0)) + 1
            if next_attempt <= config["search"]["infrastructure_attempt_limit"]:
                evaluation, attempts, attempt_records = _evaluate_with_infra_retries(
                    node=evaluator, candidate=candidate, target=target,
                    config=config, baseline=baseline,
                    candidate_dir=run_path / f"candidate-{candidate.index:02d}",
                    start_attempt=next_attempt,
                    prior_attempts=pending.get("evaluation_attempts", []),
                )
                pending["search_evaluation"] = evaluation.to_dict()
                pending.pop("evaluation", None)
                pending["evaluation_attempts"] = attempt_records
                pending["search_active_seconds_total"] = sum(
                    float(item.get("active_seconds", 0.0)) for item in attempt_records
                )
                pending["infrastructure_attempts"] = attempts
                _save_state(state_path, state)
            if search_evaluation_data(pending)["status"] == "infra_blocked":
                state["status"] = "infra_blocked"
                _save_state(state_path, state)
                return state

    limit = int(config["search"]["candidate_limit"])
    while state["model_calls"] < limit:
        pairs = _load_pairs(state)
        parent_row = choose_parent(pairs, baseline) if state["arm"] in ("C", "D") else None
        parent = parent_row[0] if parent_row else None
        parent_evaluation = parent_row[1] if parent_row else None
        source = parent.source if parent else baseline_source
        feedback = evaluation_feedback_rows(state["candidates"])
        index = state["model_calls"] + 1
        user_prompt = build_user_prompt(
            arm=state["arm"], target=target, source=source,
            candidate_index=index, timing=timing, feedback=feedback,
            episodes=episodes,
            iteration_digest=build_iteration_digest_rows(state["candidates"]),
        )
        leaks = audit_prompt_for_answer_leak(
            user_prompt,
            config.get("information_policy", {}).get("forbidden_prompt_terms", []),
        )
        if leaks:
            raise RuntimeError(f"prompt answer leakage detected: {leaks}")
        candidate_dir = run_path / f"candidate-{index:02d}"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        model_dirs = sorted(candidate_dir.glob("model*"))
        model_dir = candidate_dir / (
            "model" if not model_dirs else f"model-infra-retry-{len(model_dirs):02d}"
        )
        model_dir.mkdir(parents=True, exist_ok=False)
        request = {
            "system": SYSTEM_PROMPT,
            "user": user_prompt,
            "model": config["model"]["id"],
            "temperature": config["model"]["temperature"],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }
        dump_json(model_dir / "request.json", request)
        ref = llm.prompt.chia_remote(
            llm, user_prompt,
            _chia_display_name=f"deepseek:{run_id}:candidate-{index:02d}",
        )
        proposal = None
        try:
            result = get(ref)
        except Exception as exc:
            failure = {
                "index": index,
                "model_dir": str(model_dir),
                "error": f"{type(exc).__name__}: {exc}",
                "epoch": time.time(),
            }
            state.setdefault("model_infrastructure_failures", []).append(failure)
            state["status"] = "infra_blocked"
            _save_state(state_path, state)
            return state
        metadata = json.loads(result.stream_result) if result.stream_result else {}
        dump_json(model_dir / "provider-metadata.json", metadata)
        # HTTP/network exhaustion has no candidate response and does not consume
        # the fixed candidate/model-output budget.  Resume retries the same
        # candidate index.  A malformed HTTP-200 response does consume it.
        if metadata.get("response") is None:
            failure = {
                "index": index,
                "model_dir": str(model_dir),
                "error": result.stderr or "provider returned no response",
                "attempts": metadata.get("attempts", []),
                "epoch": time.time(),
            }
            state.setdefault("model_infrastructure_failures", []).append(failure)
            state["status"] = "infra_blocked"
            _save_state(state_path, state)
            return state
        state["model_calls"] += 1
        try:
            proposal, metadata = parse_result(result)
            validate_proposal(proposal)
            dump_json(model_dir / "proposal.json", proposal)
            candidate_source = apply_exact_edits(source, proposal["edits"])
            (model_dir / "candidate-source.scala").write_text(candidate_source)
            lineage = lineage_fields(
                parent_id=parent.id if parent else None,
                parent_source=source,
                baseline_source=baseline_source,
                candidate_source=candidate_source,
                mutable_file=target["mutable_file"],
            )
            diff = lineage["diff"]
            (model_dir / "candidate.patch").write_text(diff)
            candidate = CandidateArtifact(
                campaign_id=campaign.name, target=state["target"], arm=state["arm"],
                seed=int(state["seed"]), index=index,
                parent_id=lineage["parent_id"],
                source=candidate_source, diff=diff,
                baseline_diff=lineage["baseline_diff"],
                parent_source_sha256=lineage["parent_source_sha256"],
                diagnosis=proposal["diagnosis"],
                selected_hypothesis=proposal["selected_hypothesis"],
                visible_feedback=feedback if state["arm"] in ("C", "D") else "",
                request_sha256=canonical_hash(request),
                response_sha256=sha256_text(result.result),
                usage=metadata.get("usage"), attempts=metadata.get("attempts", []),
                model_elapsed_seconds=float(metadata.get("elapsed_seconds", 0.0)),
                provider_model=metadata.get("provider_model"),
                visible_knowledge_sha256=knowledge_sha256,
                visible_knowledge_episode_ids=knowledge_episode_ids,
            )
            dump_json(candidate_dir / "candidate.json", candidate)
            duplicate_of = duplicate_candidate_id(candidate_source, pairs)
            if duplicate_of:
                evaluation = EvaluationArtifact(
                    candidate_id=candidate.id,
                    status="candidate_invalid",
                    stage="materialize",
                    failure_class="candidate_duplicate",
                    retryable=False,
                    raw_error=f"candidate_source_repeats:{duplicate_of}",
                )
                infra_attempts = 0
                attempt_records = []
            else:
                evaluation, infra_attempts, attempt_records = _evaluate_with_infra_retries(
                    node=evaluator, candidate=candidate, target=target,
                    config=config, baseline=baseline, candidate_dir=candidate_dir,
                )
            update_validity(evaluation, baseline, parent_evaluation)
            dump_json(candidate_dir / "evaluation.json", evaluation)
            row = {
                "index": index,
                "candidate": candidate.to_dict(),
                # Keep the exact model edit set alongside the materialized
                # candidate.  CandidateArtifact deliberately stores the
                # resulting source/diff, while the proposal is needed by the
                # next feedback turn to show precisely what must be repaired
                # or avoided.
                "model_proposal": proposal,
                "search_evaluation": evaluation.to_dict(),
                "evaluation_attempts": attempt_records if not duplicate_of else [],
                "search_active_seconds_total": (
                    sum(float(item.get("active_seconds", 0.0)) for item in attempt_records)
                    if not duplicate_of else 0.0
                ),
                "infrastructure_attempts": infra_attempts,
                "provider_tokens": _provider_token_count(metadata),
            }
        except Exception as exc:
            row = {
                "index": index,
                "candidate": None,
                "model_proposal": proposal,
                "visible_feedback": feedback if state["arm"] in ("C", "D") else "",
                "search_evaluation": {
                    "candidate_id": f"{run_id}-candidate-{index:02d}",
                    "status": "candidate_invalid",
                    "stage": "model",
                    "failure_class": "model_response_invalid",
                    "retryable": False,
                    "raw_error": f"{type(exc).__name__}: {exc}",
                },
                "provider_tokens": _provider_token_count(metadata),
            }
        state["candidates"].append(row)
        _save_state(state_path, state)
        if search_evaluation_data(row).get("status") == "infra_blocked":
            state["status"] = "infra_blocked"
            _save_state(state_path, state)
            return state

    state["status"] = "search_complete"
    state["completed_epoch"] = time.time()
    _save_state(state_path, state)
    return state


def run_campaign(campaign: Path, *, parallel_runs: int | None = None) -> list[dict[str, Any]]:
    manifest = load_json(campaign / "manifest.json")
    verify_frozen_run(campaign / "frozen", manifest.get("frozen_run_fingerprint"))
    limit = parallel_runs or int(
        manifest.get("effective_parallel_runs", manifest["config"]["search"].get("parallel_runs", 1))
    )
    pending = []
    for item in manifest["schedule"]:
        state = load_json(campaign / "runs" / item["id"] / "run.json")
        if state["status"] not in ("search_complete", "complete"):
            pending.append(item["id"])
    results = []
    with ThreadPoolExecutor(max_workers=limit) as pool:
        futures = {pool.submit(execute_run, campaign, run_id): run_id for run_id in pending}
        for future in as_completed(futures):
            results.append(future.result())
    manifest["status"] = "search_complete" if all(
        result["status"] == "search_complete" for result in results
    ) else "attention_required"
    dump_json(campaign / "manifest.json", manifest)
    return results


def campaign_report(campaign: Path) -> dict[str, Any]:
    manifest = load_json(campaign / "manifest.json")
    rows = []
    for item in manifest["schedule"]:
        state = load_json(campaign / "runs" / item["id"] / "run.json")
        baseline = load_json(
            campaign / "frozen/targets" / state["target"] / "baseline-ppa.json"
        )
        baseline_route = baseline.get(
            "post_route_critical_delay_ns", baseline.get("critical_delay_ns")
        )
        evaluations = [
            EvaluationArtifact.from_dict(search_evaluation_data(row))
            for row in state["candidates"]
            if row.get("candidate") and search_evaluation_data(row)
        ]
        finalization_data = state.get("finalization") or {}
        final_evaluation_data = finalization_data.get("evaluation") or {}
        final = (
            [EvaluationArtifact.from_dict(final_evaluation_data)]
            if final_evaluation_data.get("final_valid") else []
        )
        best = min(
            final,
            key=lambda evaluation: evaluation.post_route["critical_delay_ns"],
            default=None,
        )
        tokens = [row.get("provider_tokens") for row in state["candidates"]]
        known_tokens = [value for value in tokens if isinstance(value, int)]
        cumulative_tokens = 0
        cumulative_active = 0.0
        first_improvement_tokens = None
        first_improvement_active_seconds = None
        for row in state["candidates"]:
            if isinstance(row.get("provider_tokens"), int):
                cumulative_tokens += row["provider_tokens"]
            candidate_data = row.get("candidate") or {}
            cumulative_active += float(candidate_data.get("model_elapsed_seconds", 0.0))
            evaluation_data = search_evaluation_data(row)
            cumulative_active += float(
                row.get(
                    "search_active_seconds_total",
                    evaluation_data.get("active_seconds", 0.0),
                )
            )
            if evaluation_data.get("promotable") and first_improvement_tokens is None:
                first_improvement_tokens = cumulative_tokens
                first_improvement_active_seconds = cumulative_active
        best_delay = best.post_route["critical_delay_ns"] if best else None
        improvement = max(0.0, baseline_route - best_delay) if best_delay is not None else 0.0
        provider_tokens = sum(known_tokens) if len(known_tokens) == len(tokens) else None
        search_eda_active_seconds = sum(
            float(row.get(
                "search_active_seconds_total",
                search_evaluation_data(row).get("active_seconds", 0.0),
            ))
            for row in state["candidates"]
        )
        search_model_active_seconds = sum(
            float((row.get("candidate") or {}).get("model_elapsed_seconds", 0.0))
            for row in state["candidates"]
        )
        search_active_seconds = (
            search_model_active_seconds + search_eda_active_seconds
        )
        finalization_attempts = (
            (finalization_data.get("metadata") or {}).get("attempts") or []
        )
        finalization_active_seconds = sum(
            float(item.get("active_seconds", 0.0))
            for item in finalization_attempts
        )
        if not finalization_attempts and final_evaluation_data:
            finalization_active_seconds = float(
                final_evaluation_data.get("active_seconds", 0.0)
            )
        active_seconds = search_active_seconds + finalization_active_seconds
        end_epoch = state.get("finalization_completed_epoch") or state.get("completed_epoch")
        wall_seconds = (
            float(end_epoch) - float(state["started_epoch"])
            if end_epoch and state.get("started_epoch") else None
        )
        final_acceptance_wall_seconds = (
            wall_seconds if best is not None else None
        )
        final_acceptance_active_seconds = (
            active_seconds if best is not None else None
        )
        rows.append(
            {
                "target": state["target"], "arm": state["arm"], "seed": state["seed"],
                "status": state["status"], "candidate_count": state["model_calls"],
                "valid_candidates": sum(evaluation.candidate_valid for evaluation in evaluations),
                "final_valid_candidates": len(final),
                "valid_improvement": bool(best and best.valid_improvement),
                "baseline_post_route_delay_ns": baseline_route,
                "best_post_route_delay_ns": best_delay,
                "delay_improvement_ns": improvement,
                "provider_tokens": provider_tokens,
                "search_model_active_seconds": search_model_active_seconds,
                "search_eda_active_seconds": search_eda_active_seconds,
                "search_active_seconds": search_active_seconds,
                "finalization_active_seconds": finalization_active_seconds,
                "total_active_seconds": active_seconds,
                "end_to_end_wall_seconds": wall_seconds,
                "first_final_acceptance_wall_seconds": final_acceptance_wall_seconds,
                "first_final_acceptance_active_seconds": final_acceptance_active_seconds,
                "token_efficiency_ns_per_million": (
                    improvement * 1_000_000 / provider_tokens
                    if provider_tokens else 0.0
                ),
                "active_time_efficiency_ns_per_hour": (
                    improvement * 3600 / search_active_seconds
                    if search_active_seconds else 0.0
                ),
                "first_improvement_tokens": first_improvement_tokens,
                "first_improvement_active_seconds": first_improvement_active_seconds,
            }
        )
    report = {
        "campaign": campaign.name,
        "rows": rows,
        "by_arm": summarize(rows),
        "loop_vs_direct": paired_loop_analysis(rows),
        "experience_library": experience_library_analysis(rows),
    }
    if manifest.get("kind") == "preflight":
        report["preflight"] = preflight_analysis(campaign, manifest)
    dump_json(campaign / "REPORT.json", report)
    return report


def preflight_analysis(campaign: Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check the G1 evidence rather than treating search completion as success."""
    manifest = manifest or load_json(campaign / "manifest.json")
    by_target: dict[str, Any] = {}
    all_targets_pass = True
    for target in sorted(manifest["config"]["targets"]):
        states = {}
        for arm in ("B", "C"):
            path = campaign / "runs" / f"{target}-{arm}-seed41" / "run.json"
            states[arm] = load_json(path) if path.exists() else None
        complete = all(
            state is not None and state.get("status") == "search_complete"
            for state in states.values()
        )
        rows = [
            row
            for state in states.values() if state is not None
            for row in state.get("candidates", [])
        ]
        changed_and_functional = any(
            bool((row.get("candidate") or {}).get("diff"))
            and bool(search_evaluation_data(row).get("correctness_ok"))
            for row in rows
        )

        c_rows = (states.get("C") or {}).get("candidates", [])
        feedback_checks = []
        repair_checks = []
        for index, current in enumerate(c_rows[1:], 1):
            current_candidate = current.get("candidate") or {}
            try:
                visible = json.loads(
                    current_candidate.get("visible_feedback")
                    or current.get("visible_feedback")
                    or "[]"
                )
            except json.JSONDecodeError:
                visible = []
            feedback_index = {item.get("candidate_id"): item for item in visible}
            prior_rows = [row for row in c_rows[:index] if row.get("candidate")]
            feedback_checks.append(bool(prior_rows) and all(
                prior["candidate"].get("id") in feedback_index
                and feedback_index[prior["candidate"].get("id")].get("candidate_diff")
                    == prior["candidate"].get("diff")
                and feedback_index[prior["candidate"].get("id")].get("evaluation")
                    == search_evaluation_data(prior)
                for prior in prior_rows
            ))
            previous = c_rows[index - 1]
            previous_eval = search_evaluation_data(previous)
            if previous.get("candidate") and not previous_eval.get("candidate_valid"):
                prior_id = previous["candidate"].get("id")
                raw = previous_eval.get("raw_error")
                feedback_eval = (feedback_index.get(prior_id) or {}).get("evaluation") or {}
                repair_checks.append(
                    bool(current_candidate)
                    and current_candidate.get("source") != previous["candidate"].get("source")
                    and feedback_eval.get("failure_class") == previous_eval.get("failure_class")
                    and feedback_eval.get("raw_error") == raw
                )

        feedback_chain = len(c_rows) >= 2 and all(feedback_checks)
        failure_repair = all(repair_checks)
        artifact_linkage = (campaign / "CHIA_PROFILE.json").exists() and all(
            _candidate_row_has_linked_artifacts(campaign, state, row)
            for state in states.values() if state is not None
            for row in state.get("candidates", [])
        )
        no_manual_edits = manifest.get("manual_candidate_edits_allowed") is False and all(
            not row.get("candidate") or (
                bool(row["candidate"].get("request_sha256"))
                and bool(row["candidate"].get("response_sha256"))
            )
            for row in rows
        )
        gates = {
            "both_runs_search_complete": complete,
            "changed_functionally_valid_candidate": changed_and_functional,
            "c_feedback_contains_full_prior_diff_and_evaluation": feedback_chain,
            "failed_candidate_evidence_drives_different_repair_when_applicable": failure_repair,
            "model_chia_and_vivado_artifacts_linked": artifact_linkage,
            "no_manual_candidate_edits": no_manual_edits,
        }
        passed = all(gates.values())
        all_targets_pass = all_targets_pass and passed
        by_target[target] = {
            "passed": passed,
            "gates": gates,
            "observed_failure_repairs": len(repair_checks),
        }
    return {"passed": all_targets_pass and bool(by_target), "targets": by_target}


def _candidate_row_has_linked_artifacts(
    campaign: Path, state: dict[str, Any], row: dict[str, Any]
) -> bool:
    candidate = row.get("candidate")
    evaluation = search_evaluation_data(row)
    if not candidate:
        # Invalid HTTP-200 model outputs are still linked by their model folder.
        index = int(row.get("index", 0))
        model_dir = campaign / "runs" / state["id"] / f"candidate-{index:02d}" / "model"
        return (model_dir / "request.json").exists() and (model_dir / "provider-metadata.json").exists()
    candidate_dir = campaign / "runs" / state["id"] / f"candidate-{int(candidate['index']):02d}"
    model_dirs = sorted(candidate_dir.glob("model*"))
    model_ok = any(
        (path / "request.json").exists()
        and (path / "provider-metadata.json").exists()
        for path in model_dirs
    ) and (candidate_dir / "candidate.json").exists()
    evaluation_ok = (candidate_dir / "evaluation.json").exists()
    if evaluation.get("candidate_valid"):
        vivado_ok = any((path / "vivado/post_synth_timing_summary.rpt").exists()
                        for path in candidate_dir.glob("evaluation-attempt-*"))
    else:
        vivado_ok = True
    return model_ok and evaluation_ok and vivado_ok


def paired_loop_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    index = {(row["target"], row["seed"], row["arm"]): row for row in rows}
    targets = sorted({row["target"] for row in rows})
    expected_pairs = len(targets) * len((42, 43, 44))
    required_wins = (2 * expected_pairs + 2) // 3
    pairs = []
    for target in targets:
        for seed in (42, 43, 44):
            b = index.get((target, seed, "B"))
            c = index.get((target, seed, "C"))
            if not b or not c or b.get("best_post_route_delay_ns") is None or c.get("best_post_route_delay_ns") is None:
                continue
            gain = 100 * (
                b["best_post_route_delay_ns"] - c["best_post_route_delay_ns"]
            ) / b["best_post_route_delay_ns"]
            pairs.append(
                {
                    "target": target, "seed": seed,
                    "b_delay_ns": b["best_post_route_delay_ns"],
                    "c_delay_ns": c["best_post_route_delay_ns"],
                    "c_gain_vs_b_percent": gain,
                }
            )
    gains = [pair["c_gain_vs_b_percent"] for pair in pairs]
    wins = sum(pair["c_delay_ns"] < pair["b_delay_ns"] for pair in pairs)
    b_rows = [row for row in rows if row["arm"] == "B"]
    c_rows = [row for row in rows if row["arm"] == "C"]
    def rate(group: list[dict[str, Any]]) -> float:
        return sum(row.get("valid_candidates", 0) for row in group) / max(
            1, sum(row.get("candidate_count", 0) for row in group)
        )
    def median_metric(group: list[dict[str, Any]], name: str) -> float:
        values = [float(row.get(name, 0.0)) for row in group]
        return statistics.median(values) if values else 0.0
    b_token_eff = median_metric(b_rows, "token_efficiency_ns_per_million")
    c_token_eff = median_metric(c_rows, "token_efficiency_ns_per_million")
    b_time_eff = median_metric(b_rows, "active_time_efficiency_ns_per_hour")
    c_time_eff = median_metric(c_rows, "active_time_efficiency_ns_per_hour")
    token_better = c_token_eff > b_token_eff
    time_better = c_time_eff > b_time_eff
    token_within = c_token_eff >= 0.8 * b_token_eff
    time_within = c_time_eff >= 0.8 * b_time_eff
    gates = {
        "paired_results_complete": len(pairs) == expected_pairs,
        "wins_at_least_two_thirds": wins >= required_wins,
        "median_gain_at_least_five_percent": bool(gains and statistics.median(gains) >= 5.0),
        "valid_improved_runs_not_lower": sum(bool(row.get("valid_improvement")) for row in c_rows) >= sum(bool(row.get("valid_improvement")) for row in b_rows),
        "valid_candidate_rate_not_lower": rate(c_rows) >= rate(b_rows),
        "efficiency_gate": (token_better and time_within) or (time_better and token_within),
        "all_compared_results_final_valid": len(pairs) == expected_pairs,
    }
    return {
        "paired_runs": pairs,
        "expected_pairs": expected_pairs,
        "required_wins": required_wins,
        "wins": wins,
        "median_gain_percent": statistics.median(gains) if gains else None,
        "bootstrap_median_95_percent": bootstrap_median_interval(gains),
        "valid_candidate_rate": {"B": rate(b_rows), "C": rate(c_rows)},
        "median_token_efficiency": {"B": b_token_eff, "C": c_token_eff},
        "median_active_time_efficiency": {"B": b_time_eff, "C": c_time_eff},
        "gates": gates,
        "primary_pair_gates_pass": all(gates.values()),
    }


def experience_library_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    c_rows = [row for row in rows if row["arm"] == "C"]
    d_rows = [row for row in rows if row["arm"] == "D"]
    c_tokens = [row["first_improvement_tokens"] for row in c_rows if row["first_improvement_tokens"] is not None]
    d_tokens = [row["first_improvement_tokens"] for row in d_rows if row["first_improvement_tokens"] is not None]
    c_times = [row["first_improvement_active_seconds"] for row in c_rows if row["first_improvement_active_seconds"] is not None]
    d_times = [row["first_improvement_active_seconds"] for row in d_rows if row["first_improvement_active_seconds"] is not None]
    token_reduction = None
    time_reduction = None
    if c_tokens and d_tokens and statistics.median(c_tokens):
        token_reduction = 100 * (statistics.median(c_tokens) - statistics.median(d_tokens)) / statistics.median(c_tokens)
    if c_times and d_times and statistics.median(c_times):
        time_reduction = 100 * (statistics.median(c_times) - statistics.median(d_times)) / statistics.median(c_times)
    paired_delays = []
    index = {(row["target"], row["seed"], row["arm"]): row for row in rows}
    for target in sorted({row["target"] for row in rows}):
        for seed in (42, 43, 44):
            c = index.get((target, seed, "C"))
            d = index.get((target, seed, "D"))
            if c and d and c["best_post_route_delay_ns"] is not None and d["best_post_route_delay_ns"] is not None:
                paired_delays.append(
                    100 * (d["best_post_route_delay_ns"] - c["best_post_route_delay_ns"])
                    / c["best_post_route_delay_ns"]
                )
    delay_regression = statistics.median(paired_delays) if paired_delays else None
    return {
        "first_improvement_token_reduction_percent": token_reduction,
        "first_improvement_time_reduction_percent": time_reduction,
        "median_final_delay_regression_vs_c_percent": delay_regression,
        "experience_gate_pass": bool(
            (token_reduction is not None and token_reduction >= 20.0)
            or (time_reduction is not None and time_reduction >= 20.0)
        ) and delay_regression is not None and delay_regression <= 2.0,
    }


def bootstrap_median_interval(values: list[float], samples: int = 10_000) -> list[float] | None:
    if not values:
        return None
    import random

    rng = random.Random(20260922)
    medians = []
    for _ in range(samples):
        medians.append(statistics.median(rng.choice(values) for _ in values))
    medians.sort()
    return [medians[int(samples * 0.025)], medians[int(samples * 0.975) - 1]]
