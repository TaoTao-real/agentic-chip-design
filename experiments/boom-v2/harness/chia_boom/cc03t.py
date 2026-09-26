from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from .artifacts import dump_json, load_json, sha256_file, sha256_text
from .core import canonical_hash
from .interactive import (
    finalize_interactive_issueq,
    resume_interactive_issueq,
    run_interactive_issueq,
)
from .optimization_trace import (
    BASELINE_CANDIDATE_ID,
    SEARCH_POLICY_REVISION,
    Transition,
    atomic_dump,
    compare_ppa,
    content_hash,
    build_trace,
    physical_tool_contract_hash,
    signed_record,
)
from .validation_timeline import finalization_timeline


EXPERIMENT_SCHEMA = "chia-boom.cc03t-experiment.v1"
PREPARED_SCHEMA = "chia-boom.cc03t-prepared.v1"
DECISION_SCHEMA = "chia-boom.cc03t-decision-experiment.v1"
COMPARISON_SCHEMA = "chia-boom.cc03t-comparison.v1"


class CC03TError(RuntimeError):
    pass


def _strict_keys(value: dict[str, Any], allowed: set[str], where: str) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise CC03TError(f"{where} contains unknown fields: {', '.join(extra)}")


def validate_manifest(value: dict[str, Any]) -> None:
    _strict_keys(
        value,
        {
            "schema_version", "experiment_id", "target", "model", "memory_mode",
            "search_policy", "fixtures", "decision_pairs", "end_to_end",
        },
        "experiment manifest",
    )
    if value.get("schema_version") != EXPERIMENT_SCHEMA:
        raise CC03TError("unsupported CC-03T experiment manifest")
    if value.get("target") != "T0-issue-queue":
        raise CC03TError("CC-03T target must be T0-issue-queue")
    if value.get("model") != "deepseek-v4-pro":
        raise CC03TError("CC-03T model must be deepseek-v4-pro")
    if value.get("memory_mode") != "none":
        raise CC03TError("CC-03T memory_mode must be none")
    if value.get("search_policy") != SEARCH_POLICY_REVISION:
        raise CC03TError("unsupported CC-03T SearchPolicy")
    fixtures = value.get("fixtures")
    if not isinstance(fixtures, dict) or set(fixtures) != {"D0", "Dfail", "Dperf"}:
        raise CC03TError("fixtures must define D0, Dfail, and Dperf")
    for name, fixture in fixtures.items():
        if not isinstance(fixture, dict):
            raise CC03TError(f"fixture {name} must be an object")
        allowed = {"kind"} if name == "D0" else {
            "kind", "campaign", "candidate_index", "source_sha256",
            "critical_delay_ns", "slice_luts",
        }
        _strict_keys(fixture, allowed, f"fixture {name}")
        if name == "D0" and fixture.get("kind") != "baseline":
            raise CC03TError("D0 must be a baseline fixture")
        if name != "D0" and fixture.get("kind") != "sealed_candidate":
            raise CC03TError(f"{name} must be a sealed_candidate fixture")
    expected_pairs = [
        ("D0", 41, ["E0", "E1T"]),
        ("Dfail", 42, ["E1T", "E0"]),
        ("Dperf", 43, ["E0", "E1T"]),
        ("Dperf", 44, ["E1T", "E0"]),
        ("Dperf", 45, ["E0", "E1T"]),
    ]
    actual_pairs = [
        (row.get("scenario"), row.get("seed"), row.get("order"))
        for row in value.get("decision_pairs", [])
    ]
    if actual_pairs != expected_pairs:
        raise CC03TError("decision pair schedule differs from the frozen Issue #25 order")
    end = value.get("end_to_end") or {}
    if end != {"seed": 46, "order": ["E1T", "E0"]}:
        raise CC03TError("end-to-end schedule must be seed 46, E1T then E0")


def prepare_experiment(
    config_path: Path,
    manifest_path: Path,
    output: Path,
) -> dict[str, Any]:
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("CC-03T output directory must be new or empty")
    manifest = load_json(manifest_path)
    validate_manifest(manifest)
    config = load_json(config_path)
    if config.get("model", {}).get("id") != "deepseek-v4-pro":
        raise CC03TError("runtime config model differs from frozen CC-03T model")
    if config.get("model", {}).get("api_key_env") != "DEEPSEEK_API_KEY":
        raise CC03TError("official key must be injected only through DEEPSEEK_API_KEY")
    if "api.deepseek.com" not in str(config.get("model", {}).get("api_base", "")):
        raise CC03TError("CC-03T requires the official DeepSeek API endpoint")
    if set(config.get("targets", {})) != {"T0-issue-queue"}:
        raise CC03TError("runtime config must contain only T0-issue-queue")
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config_path, output / "CONFIG.json")
    shutil.copyfile(manifest_path, output / "EXPERIMENT.json")
    prepared = signed_record({
        "schema_version": PREPARED_SCHEMA,
        "experiment_id": manifest["experiment_id"],
        "config_sha256": sha256_file(output / "CONFIG.json"),
        "manifest_sha256": sha256_file(output / "EXPERIMENT.json"),
        "policy_revision": SEARCH_POLICY_REVISION,
        "model_calls": 0,
        "eda_calls": 0,
        "status": "prepared",
    })
    atomic_dump(output / "PREPARED.json", prepared)
    return prepared


def _fixture_candidate_dir(campaign: Path, index: int) -> Path:
    path = campaign / "evaluations" / f"candidate-{index:02d}"
    if not path.is_dir():
        raise CC03TError(f"sealed fixture candidate-{index:02d} is missing")
    return path


def _normalize_ppa(
    raw: dict[str, Any] | None, *, tool_contract_hash: str,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    delay, luts = raw.get("critical_delay_ns"), raw.get("slice_luts")
    if not isinstance(delay, (int, float)) or not isinstance(luts, (int, float)):
        return None
    return {
        "stage": "post_synth",
        "critical_delay_ns": float(delay),
        "slice_luts": int(luts),
        "tool_contract_hash": raw.get("tool_contract_hash") or tool_contract_hash,
    }


def _fixture_history(campaign: Path, selected_index: int) -> tuple[list[dict[str, Any]], str]:
    campaign_result = load_json(campaign / "RESULT.json")
    fixture_manifest = load_json(campaign / "INTERACTIVE_MANIFEST.json")
    tool_contract_hash = physical_tool_contract_hash(fixture_manifest.get("config") or {})
    fixture_config = fixture_manifest.get("config") or {}
    target_id = next(iter(fixture_config.get("targets") or {"unknown": {}}))
    baseline_source_path = (
        (fixture_config.get("targets") or {}).get(target_id, {}).get("baseline_source")
    )
    baseline_source_sha = "unavailable"
    if baseline_source_path and Path(str(baseline_source_path)).is_file():
        baseline_source_sha = sha256_file(Path(str(baseline_source_path)))
    source_hashes = {BASELINE_CANDIDATE_ID: baseline_source_sha}
    baseline_ppa = _normalize_ppa(
        campaign_result.get("baseline_post_synth"),
        tool_contract_hash=tool_contract_hash,
    )
    best_id = BASELINE_CANDIDATE_ID
    best_ppa = baseline_ppa
    evaluations: dict[str, dict[str, Any]] = {
        BASELINE_CANDIDATE_ID: {
            "evaluation_id": "evaluation-candidate-00",
            "candidate_id": BASELINE_CANDIDATE_ID,
            "status": "complete",
            "stage": "post_synth",
            "correctness_ok": True,
            "candidate_valid": True,
            "promotable": False,
            "ppa": baseline_ppa,
            "raw_refs": [],
            "provenance": {"kind": "frozen_baseline"},
        }
    }
    history: list[dict[str, Any]] = []
    for index in range(1, selected_index + 1):
        directory = _fixture_candidate_dir(campaign, index)
        candidate_raw = load_json(directory / "candidate.json")
        result_raw = load_json(directory / "result.json")
        evaluation_raw = result_raw.get("search_evaluation") or result_raw.get("evaluation")
        if not isinstance(evaluation_raw, dict):
            raise CC03TError("sealed fixture evaluation is missing")
        candidate_id = str(candidate_raw["id"])
        parent_id = str(candidate_raw.get("parent_id") or BASELINE_CANDIDATE_ID)
        source = str(candidate_raw["source"])
        expected_sha = str(candidate_raw.get("source_sha256") or sha256_text(source))
        if sha256_text(source) != expected_sha:
            raise CC03TError("sealed fixture source hash mismatch")
        evaluation_id = f"evaluation-{candidate_id}"
        ppa = _normalize_ppa(
            evaluation_raw.get("post_synth"),
            tool_contract_hash=tool_contract_hash,
        )
        eval_row = {
            "evaluation_id": evaluation_id,
            "candidate_id": candidate_id,
            "status": str(evaluation_raw.get("status", "unknown")),
            "stage": str(evaluation_raw.get("stage", "unknown")),
            "correctness_ok": evaluation_raw.get("correctness_ok")
                if isinstance(evaluation_raw.get("correctness_ok"), bool) else None,
            "candidate_valid": bool(evaluation_raw.get("candidate_valid")),
            "promotable": bool(evaluation_raw.get("promotable")),
            "ppa": ppa,
            "raw_refs": [],
            "provenance": {
                "candidate_sha256": sha256_file(directory / "candidate.json"),
                "result_sha256": sha256_file(directory / "result.json"),
            },
        }
        parent_eval = evaluations.get(parent_id)
        transition = Transition(
            transition_id=f"fixture-transition-{index:02d}",
            decision_point_id=f"fixture-decision-{index:02d}",
            from_candidate_id=parent_id,
            to_candidate_id=candidate_id,
            evaluation_id=evaluation_id,
            best_before_candidate_id=best_id,
            parent_delta=compare_ppa(ppa if eval_row["candidate_valid"] else None, parent_eval.get("ppa") if parent_eval else None),
            best_delta=compare_ppa(ppa if eval_row["candidate_valid"] else None, best_ppa),
            correctness={
                "status": "available" if eval_row["correctness_ok"] is not None else "unavailable",
                "passed": eval_row["correctness_ok"],
            },
            became_new_best=bool(
                eval_row["candidate_valid"] and ppa and best_ppa
                and (ppa["critical_delay_ns"], ppa["slice_luts"])
                < (best_ppa["critical_delay_ns"], best_ppa["slice_luts"])
            ),
            raw_evidence_refs=(),
            provenance=eval_row["provenance"],
            run_id=campaign.name,
            branch_id=str(campaign_result.get("feedback_arm", "unknown")),
            selected_parent_id=parent_id,
            source_before_sha256=source_hashes.get(parent_id, "unavailable"),
            source_after_sha256=expected_sha,
            patch_ref=sha256_text(str(candidate_raw.get("diff", ""))),
            cost_ref=f"sealed:{candidate_id}",
        )
        candidate_row = {
            "candidate_id": candidate_id,
            "parent_candidate_id": parent_id,
            "source_sha256": expected_sha,
            "patch_sha256": sha256_text(str(candidate_raw.get("diff", ""))),
            "evaluation_ref": evaluation_id,
            "generated_rtl_ref": None,
            "generated_rtl_sha256": None,
            "provenance": eval_row["provenance"],
        }
        history.append({
            "candidate": candidate_row,
            "evaluation": eval_row,
            "transition": transition.to_dict(),
            "source": source,
        })
        source_hashes[candidate_id] = expected_sha
        evaluations[candidate_id] = eval_row
        if transition.became_new_best:
            best_id, best_ppa = candidate_id, ppa
    return history, best_id


def materialize_fixture(manifest: dict[str, Any], scenario: str) -> dict[str, Any]:
    raw = manifest["fixtures"][scenario]
    if scenario == "D0":
        return {
            "name": "D0", "history": [],
            "selected_parent_id": BASELINE_CANDIDATE_ID,
            "current_best_candidate_id": BASELINE_CANDIDATE_ID,
            "fixture_override": False,
            "frozen_decision_parent": False,
            "runtime_contexts": {},
        }
    campaign = Path(str(raw["campaign"])).resolve(strict=True)
    selected_index = int(raw["candidate_index"])
    history, best_id = _fixture_history(campaign, selected_index)
    selected = history[-1]
    if selected["candidate"]["source_sha256"] != raw["source_sha256"]:
        raise CC03TError(f"{scenario} source hash differs from the frozen manifest")
    if scenario == "Dperf":
        ppa = selected["evaluation"].get("ppa") or {}
        if (
            ppa.get("critical_delay_ns") != float(raw["critical_delay_ns"])
            or ppa.get("slice_luts") != int(raw["slice_luts"])
        ):
            raise CC03TError("Dperf PPA differs from the frozen manifest")
    session = load_json(campaign / "SESSION.json")
    allowed_ids = {row["candidate"]["candidate_id"] for row in history}
    runtime_contexts = {
        key: value for key, value in session.get("chipcontext_contexts", {}).items()
        if key in allowed_ids
    }
    return {
        "name": scenario,
        "history": history,
        "selected_parent_id": selected["candidate"]["candidate_id"],
        "selected_parent_source": selected["source"],
        "current_best_candidate_id": best_id,
        "last_transition_id": selected["transition"]["transition_id"],
        "fixture_override": scenario == "Dfail",
        "frozen_decision_parent": scenario == "Dperf",
        "runtime_contexts": runtime_contexts,
        "fixture_campaign_fingerprint": load_json(campaign / "INTERACTIVE_MANIFEST.json").get("frozen_run_fingerprint"),
    }


def _provider_usage(run_dir: Path) -> tuple[int | None, int, int | None, int | None]:
    values: list[int] = []
    active_values: list[int] = []
    wall_values: list[int] = []
    paths = sorted((run_dir / "turns").glob("turn-*/provider-metadata*.json"))
    for path in paths:
        metadata = load_json(path)
        usage = metadata.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
            values.append(int(usage["total_tokens"]))
        elapsed = metadata.get("elapsed_seconds")
        if isinstance(elapsed, (int, float)):
            active_values.append(int(float(elapsed) * 1_000_000_000))
        wall = metadata.get("client_wall_time_ns")
        if isinstance(wall, int):
            wall_values.append(wall)
    return (
        (sum(values) if len(values) == len(paths) else None),
        len(paths),
        (sum(active_values) if len(active_values) == len(paths) else None),
        (sum(wall_values) if len(wall_values) == len(paths) else None),
    )


def _tool_counts(run_dir: Path) -> dict[str, int]:
    calls = 0
    raw_reads = 0
    paths = sorted((run_dir / "turns").glob("turn-*/assistant-message.json"))
    for path in paths:
        message = load_json(path)
        for call in message.get("tool_calls", []) or []:
            calls += 1
            if (call.get("function") or {}).get("name") == "read_candidate_artifact":
                raw_reads += 1
    return {
        "tool_calls": calls,
        "tool_turns": len(paths),
        "raw_candidate_reads": raw_reads,
    }


def _qor_curves(
    run_dir: Path, result: dict[str, Any], timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    baseline = result.get("baseline_post_synth") or {}
    best_delay = float(baseline.get("critical_delay_ns", float("inf")))
    best_luts = float(baseline.get("slice_luts", float("inf")))
    cumulative_validation = 0
    cumulative_vivado = 0
    syntheses = 0
    new_bests = 0
    by_evaluation: list[dict[str, Any]] = []
    by_synthesis: list[dict[str, Any]] = []
    first_improvement_ns: int | None = None
    for path in sorted((run_dir / "evaluations").glob("candidate-*/result.json")):
        row = load_json(path)
        evaluation = row.get("search_evaluation") or row.get("evaluation") or {}
        candidate_id = str((row.get("candidate") or {}).get("id") or evaluation.get("candidate_id"))
        spans = [item for item in timeline if item.get("candidate_id") == candidate_id]
        validation_delta = sum(
            int(item["active_time_ns"]) for item in spans
            if item.get("stage") in {
                "materialize", "chisel_elaboration", "interface_check", "differential",
                "vivado_post_synth", "infrastructure_retry",
            } and isinstance(item.get("active_time_ns"), int)
        )
        vivado_delta = sum(
            int(item["active_time_ns"]) for item in spans
            if item.get("stage") == "vivado_post_synth"
            and isinstance(item.get("active_time_ns"), int)
        )
        cumulative_validation += validation_delta
        cumulative_vivado += vivado_delta
        launched_synthesis = any(item.get("stage") == "vivado_post_synth" for item in spans)
        if launched_synthesis:
            syntheses += 1
        ppa_value = evaluation.get("post_synth") or {}
        delay = ppa_value.get("critical_delay_ns")
        luts = ppa_value.get("slice_luts")
        became_best = bool(
            evaluation.get("candidate_valid")
            and isinstance(delay, (int, float))
            and isinstance(luts, (int, float))
            and (float(delay), float(luts)) < (best_delay, best_luts)
        )
        if became_best:
            best_delay, best_luts = float(delay), float(luts)
            new_bests += 1
            if first_improvement_ns is None:
                first_improvement_ns = cumulative_validation
        point = {
            "evaluation": len(by_evaluation) + 1,
            "candidate_id": candidate_id,
            "best_delay_ns": None if best_delay == float("inf") else best_delay,
            "best_luts": None if best_luts == float("inf") else best_luts,
            "synthesis_calls": syntheses,
            "validation_active_ns": cumulative_validation,
            "vivado_active_ns": cumulative_vivado,
            "became_new_best": became_best,
        }
        by_evaluation.append(point)
        if launched_synthesis:
            by_synthesis.append(dict(point))
    baseline_delay = baseline.get("critical_delay_ns")
    gain = (
        float(baseline_delay) - best_delay
        if isinstance(baseline_delay, (int, float)) and best_delay != float("inf")
        else None
    )
    return {
        "by_evaluation": by_evaluation,
        "by_synthesis": by_synthesis,
        "synthesis_calls": syntheses,
        "new_best_count": new_bests,
        "new_best_per_synthesis": new_bests / syntheses if syntheses else None,
        "delay_gain_ns": gain,
        "delay_gain_per_synthesis_ns": gain / syntheses if gain is not None and syntheses else None,
        "delay_gain_per_validation_active_minute_ns": (
            gain / (cumulative_validation / 60_000_000_000)
            if gain is not None and cumulative_validation else None
        ),
        "first_verified_improvement_active_ns": first_improvement_ns,
    }


def summarize_run(run_dir: Path) -> dict[str, Any]:
    result = load_json(run_dir / "RESULT.json")
    state = result.get("cc03t_state") or {}
    transitions = list((state.get("trace_transitions") or {}).values())
    transition = transitions[-1] if transitions else None
    evaluation = None
    if transition:
        evaluation = (state.get("trace_evaluations") or {}).get(transition["evaluation_id"])
    counts = _tool_counts(run_dir)
    timeline = state.get("validation_timeline", [])
    visibility_rows = state.get("visibility_manifests", [])
    first_visibility = visibility_rows[0] if visibility_rows else {}
    model_ns = sum(
        int(row["active_time_ns"]) for row in timeline
        if row.get("stage") == "model_api" and isinstance(row.get("active_time_ns"), int)
    )
    validation_ns = sum(
        int(row["active_time_ns"]) for row in timeline
        if row.get("stage") in {
            "materialize", "chisel_elaboration", "interface_check", "differential",
            "vivado_post_synth", "infrastructure_retry",
        } and isinstance(row.get("active_time_ns"), int)
    )
    vivado_ns = sum(
        int(row["active_time_ns"]) for row in timeline
        if row.get("stage") == "vivado_post_synth"
        and isinstance(row.get("active_time_ns"), int)
    )
    trace_feedback_ns = sum(
        int(row["active_time_ns"]) for row in timeline
        if row.get("stage") in {"trace_build", "feedback_prepare", "feedback_ready"}
        and isinstance(row.get("active_time_ns"), int)
    )
    total_wall_ns = sum(
        int(row["wall_time_ns"]) for row in timeline
        if row.get("leaf", True) and isinstance(row.get("wall_time_ns"), int)
    )
    timeline_model_wall_ns = sum(
        int(row["wall_time_ns"]) for row in timeline
        if row.get("stage") == "model_api"
        and isinstance(row.get("wall_time_ns"), int)
    )
    curves = _qor_curves(run_dir, result, timeline)
    provider_tokens, model_calls, provider_active_ns, provider_wall_ns = _provider_usage(run_dir)
    if provider_active_ns is not None:
        model_ns = provider_active_ns
    if provider_wall_ns is not None:
        total_wall_ns = total_wall_ns - timeline_model_wall_ns + provider_wall_ns
    selections = state.get("parent_selections", [])
    selected_parent = (
        selections[0].get("selected_parent_id") if selections else None
    )
    return {
        "run_id": run_dir.name,
        "arm": result.get("feedback_arm"),
        "status": result.get("status"),
        "evaluations": result.get("evaluations"),
        "candidate_valid": bool(evaluation and evaluation.get("candidate_valid")),
        "promotable": bool(evaluation and evaluation.get("promotable")),
        "became_new_best": bool(transition and transition.get("became_new_best")),
        "stage": evaluation.get("stage") if evaluation else None,
        "ppa": evaluation.get("ppa") if evaluation else None,
        "parent_delta": transition.get("parent_delta") if transition else None,
        "best_delta": transition.get("best_delta") if transition else None,
        "provider_tokens": provider_tokens,
        "model_calls": model_calls,
        **counts,
        "model_api_active_ns": model_ns or None,
        "model_api_wall_ns": provider_wall_ns,
        "candidate_validation_active_ns": validation_ns or None,
        "vivado_active_ns": vivado_ns or None,
        "trace_feedback_active_ns": trace_feedback_ns,
        "total_active_ns": (model_ns + validation_ns + trace_feedback_ns) or None,
        "total_wall_ns": total_wall_ns or None,
        "trace_tail_bytes": sum(
            int(row.get("treatment", {}).get("trace_tail_bytes", 0))
            for row in state.get("visibility_manifests", [])
        ),
        "initial_visibility": {
            key: first_visibility.get(key)
            for key in (
                "common_context_hash", "tool_schema_hash", "raw_permissions_hash",
                "treatment_hash",
            )
        },
        "qor_curves": curves,
        "selected_parent_id": selected_parent,
        "transition_parent_id": transition.get("from_candidate_id") if transition else None,
        "parent_identity_ok": bool(
            transition and selected_parent == transition.get("from_candidate_id")
        ),
    }


def _ordered_result(row: dict[str, Any]) -> tuple[int, int, float, float]:
    ppa = row.get("ppa") or {}
    return (
        0 if row.get("candidate_valid") else 1,
        0 if row.get("became_new_best") else 1,
        float(ppa.get("critical_delay_ns", float("inf"))),
        float(ppa.get("slice_luts", float("inf"))),
    )


def decision_gate(pair_results: list[dict[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    dperf_wins = {"E0": 0, "E1T": 0, "tie": 0}
    valid_counts = {"E0": 0, "E1T": 0}
    best_counts = {"E0": 0, "E1T": 0}
    for pair in pair_results:
        arms = {row["arm"]: row for row in pair["arms"]}
        if set(arms) != {"E0", "E1T"}:
            failures.append(f"{pair['pair_id']}: incomplete arm pair")
            continue
        e0, e1t = arms["E0"], arms["E1T"]
        for arm_name, arm_row in arms.items():
            if arm_row.get("evaluations") != 1:
                failures.append(f"{pair['pair_id']}: {arm_name} violated single-candidate protocol")
            if not arm_row.get("parent_identity_ok"):
                failures.append(f"{pair['pair_id']}: {arm_name} parent identity mismatch")
        for key in ("common_context_hash", "tool_schema_hash", "raw_permissions_hash"):
            if e0.get("initial_visibility", {}).get(key) != e1t.get("initial_visibility", {}).get(key):
                failures.append(f"{pair['pair_id']}: visibility mismatch in {key}")
        scenario = pair["scenario"]
        if scenario == "D0" and e0["candidate_valid"] and not e1t["candidate_valid"]:
            failures.append("D0: E0 valid while E1T invalid")
        if scenario == "D0" and _ordered_result(e0) == _ordered_result(e1t):
            comparable_costs = (
                "model_calls", "provider_tokens", "tool_turns", "total_wall_ns"
            )
            if all(
                isinstance(e0.get(key), (int, float))
                and isinstance(e1t.get(key), (int, float))
                and e1t[key] > e0[key]
                for key in comparable_costs
            ):
                failures.append("D0: equal outcome while E1T was worse in all four costs")
        if scenario == "Dfail" and e0["candidate_valid"] and not e1t["candidate_valid"]:
            failures.append("Dfail: E0 repaired while E1T failed")
        overhead = e1t.get("trace_feedback_active_ns")
        if isinstance(overhead, (int, float)):
            if (
                isinstance(e1t.get("model_api_active_ns"), (int, float))
                and overhead >= e1t["model_api_active_ns"]
            ) or (
                isinstance(e1t.get("candidate_validation_active_ns"), (int, float))
                and overhead >= e1t["candidate_validation_active_ns"]
            ):
                failures.append(f"{pair['pair_id']}: TraceTail overhead exceeded a primary stage")
        if scenario == "Dperf":
            valid_counts["E0"] += int(e0["candidate_valid"])
            valid_counts["E1T"] += int(e1t["candidate_valid"])
            best_counts["E0"] += int(e0["became_new_best"])
            best_counts["E1T"] += int(e1t["became_new_best"])
            left, right = _ordered_result(e0), _ordered_result(e1t)
            if left < right:
                dperf_wins["E0"] += 1
            elif right < left:
                dperf_wins["E1T"] += 1
            else:
                dperf_wins["tie"] += 1
    if dperf_wins["E0"] >= 2:
        failures.append("Dperf: E1T lost at least two paired decisions")
    if valid_counts["E1T"] < valid_counts["E0"]:
        failures.append("E1T produced fewer valid candidates than E0")
    if best_counts["E1T"] < best_counts["E0"]:
        failures.append("E1T produced fewer new-best candidates than E0")
    return {
        "passed": not failures,
        "failures": failures,
        "dperf_wins": dperf_wins,
        "valid_counts": valid_counts,
        "new_best_counts": best_counts,
    }


def _run_arm(
    config: dict[str, Any], root: Path, *, arm: str, seed: int,
    scenario: str, fixture: dict[str, Any], max_turns: int,
    max_evaluations: int, stop_after_first: bool,
) -> dict[str, Any]:
    run_dir = root / "runs" / f"{scenario.lower()}-seed{seed}-{arm.lower()}"
    if (run_dir / "RESULT.json").is_file():
        summary = summarize_run(run_dir)
    elif (run_dir / "SESSION.json").is_file():
        resume_interactive_issueq(config, run_dir)
        summary = summarize_run(run_dir)
    else:
        cc03t = {
            "schema_version": "chia-boom.cc03t-run.v1",
            "experiment_id": root.name,
            "scenario": scenario,
            "policy_revision": SEARCH_POLICY_REVISION,
            "fixture": fixture,
            "stop_after_first_evaluation": stop_after_first,
        }
        run_interactive_issueq(
            config, run_dir, seed=seed, max_turns=max_turns,
            max_evaluations=max_evaluations, memory_mode="none",
            feedback_arm=arm, cc03t=cc03t,
        )
        summary = summarize_run(run_dir)
    trace_output = root / "trace-views" / run_dir.name
    if not (trace_output / "TRACE_MANIFEST.json").is_file():
        build_trace(run_dir, trace_output)
    summary["trace_manifest_sha256"] = sha256_file(
        trace_output / "TRACE_MANIFEST.json"
    )
    return summary


def _write_root_artifacts(root: Path, pairs: list[dict[str, Any]], gate: dict[str, Any]) -> None:
    decision = signed_record({
        "schema_version": DECISION_SCHEMA,
        "pairs": pairs,
        "gate": gate,
    })
    atomic_dump(root / "DECISION_EXPERIMENT.json", decision)
    comparison = signed_record({
        "schema_version": COMPARISON_SCHEMA,
        "decision_gate": gate,
        "end_to_end": (
            load_json(root / "END_TO_END_COMPARISON.json")
            if (root / "END_TO_END_COMPARISON.json").is_file() else None
        ),
    })
    atomic_dump(root / "E0_E1T_COMPARISON.json", comparison)
    runs = sorted((root / "runs").glob("*/RESULT.json"))
    trace_manifest = signed_record({
        "schema_version": "chia-boom.trace-manifest-set.v1",
        "runs": [
            {
                "run_id": path.parent.name,
                "result_sha256": sha256_file(path),
                "trace_manifest_sha256": sha256_file(
                    root / "trace-views" / path.parent.name / "TRACE_MANIFEST.json"
                ),
            }
            for path in runs
        ],
        "model_calls_for_trace": 0,
        "eda_calls_for_trace": 0,
    })
    atomic_dump(root / "TRACE_MANIFEST.json", trace_manifest)
    atomic_dump(root / "CANDIDATE_DAG.json", {
        "schema_version": "chia-boom.candidate-dag-set.v1",
        "runs": [
            {
                "run_id": path.parent.name,
                "candidate_dag_sha256": sha256_file(path.parent / "CANDIDATE_DAG.json"),
            }
            for path in runs if (path.parent / "CANDIDATE_DAG.json").is_file()
        ],
    })
    atomic_dump(root / "VISIBILITY_MANIFEST.json", {
        "schema_version": "chia-boom.visibility-manifest-set.v1",
        "runs": [
            {
                "run_id": path.parent.name,
                "visibility_sha256": sha256_file(path.parent / "VISIBILITY_MANIFEST.json"),
            }
            for path in runs if (path.parent / "VISIBILITY_MANIFEST.json").is_file()
        ],
    })
    atomic_dump(root / "VALIDATION_TIMELINE.json", {
        "schema_version": "chia-boom.validation-timeline-set.v1",
        "runs": [
            {
                "run_id": path.parent.name,
                "timeline_sha256": sha256_file(path.parent / "VALIDATION_TIMELINE.json"),
            }
            for path in runs if (path.parent / "VALIDATION_TIMELINE.json").is_file()
        ],
    })


def run_experiment(root: Path) -> dict[str, Any]:
    prepared = load_json(root / "PREPARED.json")
    if prepared.get("schema_version") != PREPARED_SCHEMA:
        raise CC03TError("experiment has not been prepared")
    if sha256_file(root / "CONFIG.json") != prepared["config_sha256"]:
        raise CC03TError("prepared config changed")
    if sha256_file(root / "EXPERIMENT.json") != prepared["manifest_sha256"]:
        raise CC03TError("prepared manifest changed")
    config = load_json(root / "CONFIG.json")
    manifest = load_json(root / "EXPERIMENT.json")
    validate_manifest(manifest)
    pairs: list[dict[str, Any]] = []
    for row in manifest["decision_pairs"]:
        scenario, seed = str(row["scenario"]), int(row["seed"])
        fixture = materialize_fixture(manifest, scenario)
        arms = []
        for arm in row["order"]:
            arms.append(_run_arm(
                config, root, arm=str(arm), seed=seed, scenario=scenario,
                fixture=fixture, max_turns=24, max_evaluations=1,
                stop_after_first=True,
            ))
        pairs.append({
            "pair_id": f"{scenario}-seed{seed}",
            "scenario": scenario, "seed": seed, "order": row["order"],
            "arms": arms,
        })
        provisional = decision_gate(pairs)
        stop_dperf = scenario == "Dperf" and provisional["dperf_wins"]["E0"] >= 2
        if provisional["failures"] and (scenario in {"D0", "Dfail"} or stop_dperf):
            _write_root_artifacts(root, pairs, provisional)
            return {"status": "decision_gate_failed", "gate": provisional}
    gate = decision_gate(pairs)
    _write_root_artifacts(root, pairs, gate)
    if not gate["passed"]:
        return {"status": "decision_gate_failed", "gate": gate}

    # Experiment B is conditional and begins from the baseline fixture.
    end_rows = []
    fixture = materialize_fixture(manifest, "D0")
    for arm in manifest["end_to_end"]["order"]:
        summary = _run_arm(
            config, root, arm=str(arm), seed=46, scenario="END",
            fixture=fixture, max_turns=48, max_evaluations=5,
            stop_after_first=False,
        )
        run_dir = root / "runs" / f"end-seed46-{str(arm).lower()}"
        final = None
        result = load_json(run_dir / "RESULT.json")
        if result.get("best_candidate"):
            if (run_dir / "FINAL_RESULT.json").is_file():
                final = load_json(run_dir / "FINAL_RESULT.json")
            else:
                final = finalize_interactive_issueq(config, run_dir)
        final_active_ns = None
        if final:
            active_seconds = (final.get("evaluation") or {}).get("active_seconds")
            if isinstance(active_seconds, (int, float)):
                final_active_ns = int(float(active_seconds) * 1_000_000_000)
            timeline_path = run_dir / "VALIDATION_TIMELINE.json"
            timeline_record = load_json(timeline_path)
            candidate_id = str(final.get("candidate_id", "unknown"))
            final_timeline = finalization_timeline(
                run_dir.name, candidate_id, final.get("evaluation") or {}
            )
            timeline_record["spans"] = list(timeline_record.get("spans", [])) + [
                span.to_dict() for span in final_timeline.spans
            ]
            timeline_record.pop("content_hash", None)
            atomic_dump(timeline_path, signed_record(timeline_record))
        delivery_ns = (
            int(summary["total_active_ns"]) + int(final_active_ns)
            if isinstance(summary.get("total_active_ns"), int)
            and isinstance(final_active_ns, int) else None
        )
        end_rows.append(summary | {
            "finalization": final,
            "finalization_active_ns": final_active_ns,
            "final_valid_delivery_active_ns": delivery_ns,
        })
    end_comparison = compare_end_to_end(end_rows)
    atomic_dump(root / "END_TO_END_COMPARISON.json", end_comparison)
    _write_root_artifacts(root, pairs, gate)
    return {"status": "complete", "gate": gate, "end_to_end": end_comparison}


def compare_end_to_end(rows: list[dict[str, Any]]) -> dict[str, Any]:
    arms = {row["arm"]: row for row in rows}
    label = "inconclusive"
    if set(arms) == {"E0", "E1T"}:
        e0_final = arms["E0"].get("finalization") or {}
        e1_final = arms["E1T"].get("finalization") or {}
        if e0_final.get("final_valid") and e1_final.get("final_valid"):
            e0_delay = ((e0_final.get("evaluation") or {}).get("post_route") or {}).get("critical_delay_ns")
            e1_delay = ((e1_final.get("evaluation") or {}).get("post_route") or {}).get("critical_delay_ns")
            if isinstance(e0_delay, (int, float)) and isinstance(e1_delay, (int, float)):
                cost_keys = (
                    "provider_tokens", "model_calls", "raw_candidate_reads",
                    "candidate_validation_active_ns",
                )
                comparable_costs = [
                    key for key in cost_keys
                    if isinstance(arms["E0"].get(key), (int, float))
                    and isinstance(arms["E1T"].get(key), (int, float))
                ]
                all_costs_worse = bool(comparable_costs) and all(
                    arms["E1T"][key] > arms["E0"][key] for key in comparable_costs
                )
                efficiency_keys = (
                    "provider_tokens", "candidate_validation_active_ns",
                )
                synthesis_e0 = arms["E0"].get("qor_curves", {}).get("synthesis_calls")
                synthesis_e1t = arms["E1T"].get("qor_curves", {}).get("synthesis_calls")
                any_efficiency_gain = any(
                    isinstance(arms["E0"].get(key), (int, float))
                    and isinstance(arms["E1T"].get(key), (int, float))
                    and arms["E1T"][key] < arms["E0"][key]
                    for key in efficiency_keys
                ) or (
                    isinstance(synthesis_e0, int) and isinstance(synthesis_e1t, int)
                    and synthesis_e1t < synthesis_e0
                )
                if e1_delay < e0_delay and not all_costs_worse:
                    label = "positive_qor_smoke"
                elif e1_delay <= e0_delay and any_efficiency_gain:
                    label = "positive_efficiency_smoke"
                elif e1_delay > e0_delay:
                    label = "negative_smoke"
    return signed_record({
        "schema_version": "chia-boom.cc03t-end-to-end-comparison.v1",
        "seed": 46,
        "arms": rows,
        "conclusion": label,
        "statistical_claim": "single-pair smoke; no significance or generality claim",
    })


def resume_experiment(root: Path) -> dict[str, Any]:
    return run_experiment(root)


def report_experiment(root: Path) -> dict[str, Any]:
    if not (root / "DECISION_EXPERIMENT.json").is_file():
        raise CC03TError("decision experiment has no materialized result")
    decision = load_json(root / "DECISION_EXPERIMENT.json")
    result = {
        "schema_version": "chia-boom.cc03t-report.v1",
        "decision": decision,
        "end_to_end": (
            load_json(root / "END_TO_END_COMPARISON.json")
            if (root / "END_TO_END_COMPARISON.json").is_file() else None
        ),
        "artifacts": {
            name: sha256_file(root / name)
            for name in (
                "TRACE_MANIFEST.json", "CANDIDATE_DAG.json",
                "VISIBILITY_MANIFEST.json", "DECISION_EXPERIMENT.json",
                "VALIDATION_TIMELINE.json", "E0_E1T_COMPARISON.json",
            ) if (root / name).is_file()
        },
    }
    result["report_hash"] = content_hash(result)
    atomic_dump(root / "REPORT.json", result)
    return result
