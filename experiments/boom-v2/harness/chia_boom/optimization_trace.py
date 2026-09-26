from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .artifacts import load_json, sha256_file, sha256_text


TRACE_SCHEMA = "chia-boom.optimization-trace.v1"
DAG_SCHEMA = "chia-boom.candidate-dag.v1"
PARENT_SELECTION_SCHEMA = "chia-boom.parent-selection.v1"
TRACE_TAIL_SCHEMA = "chia-boom.trace-tail.v1"
VISIBILITY_SCHEMA = "chia-boom.visibility-manifest.v1"
SEARCH_POLICY_REVISION = "baseline-3-best-2-v1"
BASELINE_CANDIDATE_ID = "candidate-00"
TRACE_TAIL_MAX_BYTES = 6 * 1024


class TraceError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def physical_tool_contract_hash(config: dict[str, Any]) -> str:
    """Identify the physical comparison contract without scheduler-only settings."""

    physical = config.get("physical") or {}
    base = config.get("base") or {}
    return content_hash({
        "schema_version": "chia-boom.ppa-contract.v1",
        "tool": physical.get("tool"),
        "part": physical.get("part"),
        "clock_period_ns": physical.get("clock_period_ns"),
        "maximum_lut_ratio": physical.get("maximum_lut_ratio"),
        "chipyard_commit": base.get("chipyard_commit"),
        "boom_commit": base.get("boom_commit"),
        "boom_config": base.get("config"),
    })


def signed_record(value: dict[str, Any]) -> dict[str, Any]:
    unsigned = {key: item for key, item in value.items() if key != "content_hash"}
    return unsigned | {"content_hash": content_hash(unsigned)}


def atomic_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _finite_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _ppa(evaluation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not evaluation:
        return None
    raw = evaluation.get("post_synth")
    if not isinstance(raw, dict):
        return None
    delay = _finite_number(raw.get("critical_delay_ns"))
    luts = _finite_number(raw.get("slice_luts"))
    if delay is None or luts is None:
        return None
    return {
        "stage": "post_synth",
        "critical_delay_ns": delay,
        "slice_luts": int(luts) if luts.is_integer() else luts,
        "tool_contract_hash": raw.get("tool_contract_hash"),
    }


def compare_ppa(
    current: dict[str, Any] | None,
    reference: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return current-reference deltas without inventing missing operands."""

    if current is None or reference is None:
        missing = []
        if current is None:
            missing.append("current")
        if reference is None:
            missing.append("reference")
        return {"status": "unavailable", "missing": missing}
    if current.get("stage") != reference.get("stage"):
        return {
            "status": "not_comparable",
            "reason": "stage_mismatch",
            "current_stage": current.get("stage"),
            "reference_stage": reference.get("stage"),
        }
    current_contract = current.get("tool_contract_hash")
    reference_contract = reference.get("tool_contract_hash")
    if current_contract is None or reference_contract is None:
        return {"status": "not_comparable", "reason": "tool_contract_missing"}
    if current_contract != reference_contract:
        return {"status": "not_comparable", "reason": "tool_contract_mismatch"}
    return {
        "status": "comparable",
        "delay_delta_ns": round(
            float(current["critical_delay_ns"])
            - float(reference["critical_delay_ns"]),
            12,
        ),
        "lut_delta": round(
            float(current["slice_luts"]) - float(reference["slice_luts"]),
            12,
        ),
    }


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    parent_candidate_id: str | None
    source_sha256: str
    patch_sha256: str | None
    evaluation_ref: str | None
    generated_rtl_ref: str | None = None
    generated_rtl_sha256: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class Evaluation:
    evaluation_id: str
    candidate_id: str
    status: str
    stage: str
    correctness_ok: bool | None
    candidate_valid: bool
    promotable: bool
    ppa: dict[str, Any] | None
    raw_refs: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["raw_refs"] = list(self.raw_refs)
        return value


@dataclass(frozen=True)
class ParentSelection:
    decision_point_id: str
    evaluation_slot: int
    selected_parent_id: str
    selection_reason: str
    policy_revision: str
    fixture_override: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return signed_record({
            "schema_version": PARENT_SELECTION_SCHEMA,
            **dataclasses.asdict(self),
        })


@dataclass(frozen=True)
class DecisionPoint:
    decision_point_id: str
    decision_kind: str
    selected_parent_id: str
    current_best_candidate_id: str
    remaining_evaluation_budget: int
    model_request_ref: str | None
    visible_context_hash: str | None
    tool_schema_hash: str | None
    action_refs: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["action_refs"] = list(self.action_refs)
        return value


@dataclass(frozen=True)
class Action:
    action_id: str
    decision_point_id: str
    action_type: str
    tool_call_ref: str
    source_before_sha256: str | None = None
    source_after_sha256: str | None = None
    patch_ref: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class Transition:
    transition_id: str
    decision_point_id: str
    from_candidate_id: str
    to_candidate_id: str
    evaluation_id: str
    best_before_candidate_id: str
    parent_delta: dict[str, Any]
    best_delta: dict[str, Any]
    correctness: dict[str, Any]
    became_new_best: bool
    raw_evidence_refs: tuple[str, ...]
    provenance: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    branch_id: str | None = None
    selected_parent_id: str | None = None
    source_before_sha256: str | None = None
    source_after_sha256: str | None = None
    patch_ref: str | None = None
    cost_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["raw_evidence_refs"] = list(self.raw_evidence_refs)
        value["selected_parent_id"] = self.selected_parent_id or self.from_candidate_id
        value["evaluation_ref"] = self.evaluation_id
        return value


@dataclass(frozen=True)
class CostSpan:
    span_id: str
    stage: str
    wall_time_ns: int | None
    active_time_ns: int | None
    queue_time_ns: int | None
    parent_span_id: str | None = None
    candidate_id: str | None = None
    leaf: bool = True
    unavailable_reason: str | None = None
    queue_unavailable_reason: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class CandidatePool:
    baseline_candidate_id: str
    current_candidate_id: str
    current_best_candidate_id: str
    eligible_parent_ids: tuple[str, ...]
    verified_candidate_ids: tuple[str, ...]
    invalid_candidate_ids: tuple[str, ...]
    remaining_evaluation_budget: int

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        for key in (
            "eligible_parent_ids", "verified_candidate_ids", "invalid_candidate_ids",
        ):
            value[key] = list(value[key])
        return value


@dataclass(frozen=True)
class BranchHistory:
    selected_parent_id: str
    recent_outcomes: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_parent_id": self.selected_parent_id,
            "recent_outcomes": list(self.recent_outcomes),
        }


@dataclass(frozen=True)
class VisibilityManifest:
    request_id: str
    arm: str
    common_context_hash: str
    tool_schema_hash: str
    raw_permissions_hash: str
    treatment_hash: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class RunTrace:
    run_id: str
    experiment_id: str
    branch_id: str
    arm: str
    status: str
    initial_candidate_id: str
    final_candidate_id: str | None
    decision_points: tuple[dict[str, Any], ...]
    actions: tuple[dict[str, Any], ...]
    evaluations: tuple[dict[str, Any], ...]
    transitions: tuple[dict[str, Any], ...]
    cost_spans: tuple[dict[str, Any], ...]
    provenance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        for key in (
            "decision_points", "actions", "evaluations", "transitions", "cost_spans",
        ):
            value[key] = list(value[key])
        return value


def baseline_candidate(source_sha256: str, *, provenance: dict[str, Any]) -> Candidate:
    return Candidate(
        candidate_id=BASELINE_CANDIDATE_ID,
        parent_candidate_id=None,
        source_sha256=source_sha256,
        patch_sha256=None,
        evaluation_ref="evaluation-candidate-00",
        provenance=provenance,
    )


def select_parent(
    evaluation_slot: int,
    *,
    current_best_candidate_id: str,
    decision_point_id: str,
    fixture_parent_id: str | None = None,
) -> ParentSelection:
    if evaluation_slot < 1 or evaluation_slot > 5:
        raise TraceError("baseline-3-best-2-v1 only defines evaluation slots 1..5")
    if fixture_parent_id is not None:
        return ParentSelection(
            decision_point_id=decision_point_id,
            evaluation_slot=evaluation_slot,
            selected_parent_id=fixture_parent_id,
            selection_reason="repair_fixture_override",
            policy_revision=SEARCH_POLICY_REVISION,
            fixture_override=True,
        )
    if evaluation_slot <= 3:
        parent, reason = BASELINE_CANDIDATE_ID, "baseline_exploration"
    else:
        parent = current_best_candidate_id or BASELINE_CANDIDATE_ID
        reason = (
            "current_best_exploitation"
            if parent != BASELINE_CANDIDATE_ID
            else "baseline_fallback"
        )
    return ParentSelection(
        decision_point_id=decision_point_id,
        evaluation_slot=evaluation_slot,
        selected_parent_id=parent,
        selection_reason=reason,
        policy_revision=SEARCH_POLICY_REVISION,
    )


def _candidate_score(evaluation: Evaluation) -> tuple[float, float]:
    if not evaluation.candidate_valid or not evaluation.ppa:
        return float("inf"), float("inf")
    return (
        float(evaluation.ppa["critical_delay_ns"]),
        float(evaluation.ppa["slice_luts"]),
    )


def choose_current_best(
    baseline_evaluation: Evaluation,
    candidate_evaluations: Iterable[Evaluation],
) -> str:
    rows = [baseline_evaluation, *candidate_evaluations]
    return min(rows, key=_candidate_score).candidate_id


def ancestry(
    candidate_id: str,
    candidates: dict[str, Candidate],
    *,
    limit: int = 3,
) -> list[str]:
    result: list[str] = []
    current = candidate_id
    seen: set[str] = set()
    while current and current != BASELINE_CANDIDATE_ID and len(result) < limit:
        if current in seen or current not in candidates:
            raise TraceError("candidate DAG is cyclic or incomplete")
        seen.add(current)
        result.append(current)
        current = candidates[current].parent_candidate_id or BASELINE_CANDIDATE_ID
    return result


def branch_history(
    selected_parent_id: str,
    candidates: dict[str, Candidate],
    evaluations: dict[str, Evaluation],
    transitions: dict[str, Transition],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return terminal outcomes on the selected parent's lineage only."""

    history = []
    for candidate_id in ancestry(selected_parent_id, candidates, limit=limit):
        candidate = candidates[candidate_id]
        evaluation = evaluations.get(candidate.evaluation_ref or "")
        transition = next(
            (row for row in transitions.values() if row.to_candidate_id == candidate_id),
            None,
        )
        if evaluation is None or transition is None:
            continue
        history.append({
            "candidate_id": candidate_id,
            "parent_candidate_id": candidate.parent_candidate_id,
            "status": evaluation.status,
            "stage": evaluation.stage,
            "candidate_valid": evaluation.candidate_valid,
            "promotable": evaluation.promotable,
            "parent_delta": transition.parent_delta,
            "best_delta": transition.best_delta,
            "became_new_best": transition.became_new_best,
        })
    return history


def build_trace_tail(
    *,
    current_candidate_id: str,
    selected_parent_id: str,
    current_best_candidate_id: str,
    last_transition: Transition | None,
    history: list[dict[str, Any]],
    remaining_evaluation_budget: int,
    raw_evidence_refs: list[str],
    source_classes: Iterable[str] = (),
    max_bytes: int = TRACE_TAIL_MAX_BYTES,
) -> dict[str, Any]:
    forbidden = {
        "analysis_only", "knowledge_store", "sibling", "historical_solution",
        "generated_rtl_semantics", "timing_summary", "strategy_advice",
    }
    found = sorted(forbidden.intersection(set(source_classes)))
    if found:
        raise TraceError("forbidden TraceTail source class: " + ", ".join(found))
    consecutive_invalid = 0
    for row in history[:3]:
        if row.get("candidate_valid"):
            break
        consecutive_invalid += 1
    consecutive_non_improvement = 0
    for row in history[:3]:
        if row.get("became_new_best"):
            break
        consecutive_non_improvement += 1
    verified_delay_gains_ns: list[float] = []
    for row in history[:3]:
        delta = row.get("best_delta") or {}
        if (
            row.get("candidate_valid")
            and delta.get("status") == "comparable"
            and isinstance(delta.get("delay_delta_ns"), (int, float))
        ):
            verified_delay_gains_ns.append(-float(delta["delay_delta_ns"]))
    payload = {
        "schema_version": TRACE_TAIL_SCHEMA,
        "current_candidate_id": current_candidate_id,
        "selected_parent_id": selected_parent_id,
        "current_best_candidate_id": current_best_candidate_id,
        "last_transition": last_transition.to_dict() if last_transition else None,
        "selected_parent_lineage": history[:3],
        "branch_history": {
            "recent_outcomes": history[:3],
            "current_best_candidate_id": current_best_candidate_id,
            "remaining_evaluation_budget": remaining_evaluation_budget,
            "consecutive_invalid": consecutive_invalid,
            "consecutive_non_improvement": consecutive_non_improvement,
            "verified_delay_gains_ns": verified_delay_gains_ns,
        },
        "remaining_evaluation_budget": remaining_evaluation_budget,
        "raw_evidence_refs": list(raw_evidence_refs),
    }
    record = signed_record(payload)
    size = 0
    while True:
        candidate_size = len(
            canonical_json(record | {"serialized_bytes": size}).encode("utf-8")
        )
        if candidate_size == size:
            break
        size = candidate_size
    if size > max_bytes:
        raise TraceError(f"required TraceTail is {size} bytes, exceeding {max_bytes}")
    return record | {"serialized_bytes": size}


def build_visibility_manifest(
    *,
    request_id: str,
    arm: str,
    common_context: Any,
    tool_specs: Any,
    raw_permissions: Any,
    trace_tail: dict[str, Any] | None,
) -> dict[str, Any]:
    if arm not in {"E0", "E1T"}:
        raise TraceError("CC-03T visibility only supports E0 and E1T")
    common_hash = content_hash(common_context)
    tool_hash = content_hash(tool_specs)
    permission_hash = content_hash(raw_permissions)
    treatment = {
        "arm": arm,
        "trace_tail_hash": trace_tail.get("content_hash") if trace_tail else None,
        "trace_tail_bytes": trace_tail.get("serialized_bytes", 0) if trace_tail else 0,
    }
    return signed_record({
        "schema_version": VISIBILITY_SCHEMA,
        "request_id": request_id,
        "arm": arm,
        "common_context_hash": common_hash,
        "tool_schema_hash": tool_hash,
        "raw_permissions_hash": permission_hash,
        "treatment": treatment,
        "treatment_hash": content_hash(treatment),
    })


def visibility_pair_isolated(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(
        left.get(key) == right.get(key)
        for key in ("common_context_hash", "tool_schema_hash", "raw_permissions_hash")
    ) and left.get("arm") != right.get("arm")


def _source_ref(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _evaluation_from_dict(
    candidate_id: str,
    raw: dict[str, Any],
    *,
    evaluation_id: str,
    source_ref: dict[str, Any],
    tool_contract_hash: str,
) -> Evaluation:
    correctness = raw.get("correctness_ok")
    if not isinstance(correctness, bool):
        correctness = None
    raw_refs: list[str] = []
    for stage in raw.get("stages", []):
        if not isinstance(stage, dict):
            continue
        for name in ("stdout_path", "stderr_path"):
            if stage.get(name):
                raw_refs.append(str(stage[name]))
    return Evaluation(
        evaluation_id=evaluation_id,
        candidate_id=candidate_id,
        status=str(raw.get("status", "unknown")),
        stage=str(raw.get("stage", "unknown")),
        correctness_ok=correctness,
        candidate_valid=bool(raw.get("candidate_valid", False)),
        promotable=bool(raw.get("promotable", False)),
        ppa=(
            (_ppa(raw) or {}) | {"tool_contract_hash": tool_contract_hash}
            if _ppa(raw) is not None else None
        ),
        raw_refs=tuple(sorted(set(raw_refs))),
        provenance={"result": source_ref},
    )


def _baseline_evaluation(
    result: dict[str, Any], source_ref: dict[str, Any], tool_contract_hash: str,
) -> Evaluation:
    raw = result.get("baseline_post_synth") or {}
    delay = _finite_number(raw.get("critical_delay_ns"))
    luts = _finite_number(raw.get("slice_luts"))
    ppa = None
    if delay is not None and luts is not None:
        ppa = {
            "stage": "post_synth",
            "critical_delay_ns": delay,
            "slice_luts": int(luts) if luts.is_integer() else luts,
            "tool_contract_hash": tool_contract_hash,
        }
    return Evaluation(
        evaluation_id="evaluation-candidate-00",
        candidate_id=BASELINE_CANDIDATE_ID,
        status="complete" if ppa else "unavailable",
        stage="post_synth" if ppa else "unknown",
        correctness_ok=True if ppa else None,
        candidate_valid=ppa is not None,
        promotable=False,
        ppa=ppa,
        provenance={"result": source_ref},
    )


def _classify_decision(previous: Evaluation | None, first: bool) -> str:
    if first:
        return "initial_design"
    if previous is None:
        return "finish_or_stop"
    if not previous.candidate_valid:
        return "repair_after_failure"
    if previous.promotable:
        return "optimize_after_valid_result"
    return "continue_after_non_best"


def _trace_actions(
    campaign: Path,
) -> tuple[list[Action], dict[str, list[str]], dict[str, dict[str, Any]]]:
    actions: list[Action] = []
    by_decision: dict[str, list[str]] = {}
    decision_requests: dict[str, dict[str, Any]] = {}
    decision_index = 1
    action_index = 0
    name_map = {
        "apply_exact_edits": "edit",
        "evaluate_candidate": "evaluate",
        "revert_source": "revert",
        "finish": "finish",
    }
    for turn_dir in sorted((campaign / "turns").glob("turn-*")):
        assistant_path = turn_dir / "assistant-message.json"
        request_path = turn_dir / "model-messages-before.json"
        tools_path = turn_dir / "tool-specs.json"
        if not assistant_path.is_file():
            continue
        decision_id = f"decision-{decision_index:02d}"
        if decision_id not in decision_requests and request_path.is_file():
            decision_requests[decision_id] = {
                "model_request_ref": _source_ref(request_path, campaign),
                "visible_context_hash": sha256_file(request_path),
                "tool_schema_hash": sha256_file(tools_path) if tools_path.is_file() else None,
            }
        assistant = load_json(assistant_path)
        for call_offset, call in enumerate(assistant.get("tool_calls", []) or []):
            fn = str((call.get("function") or {}).get("name", "unknown"))
            action_type = name_map.get(fn, "inspect")
            action_index += 1
            call_id = str(call.get("id", ""))
            response_path = turn_dir / f"tool-{call_id}.txt"
            response: dict[str, Any] = {}
            if response_path.is_file():
                try:
                    value = json.loads(response_path.read_text())
                    if isinstance(value, dict):
                        response = value
                except (json.JSONDecodeError, UnicodeDecodeError):
                    response = {}
            action = Action(
                action_id=f"action-{action_index:03d}",
                decision_point_id=decision_id,
                action_type=action_type,
                tool_call_ref=(
                    f"{assistant_path.relative_to(campaign).as_posix()}"
                    f"#/tool_calls/{call_offset}"
                ),
                source_after_sha256=(
                    str(response["source_sha256"])
                    if isinstance(response.get("source_sha256"), str) else None
                ),
                patch_ref=(
                    str(response["diff_sha256"])
                    if isinstance(response.get("diff_sha256"), str) else None
                ),
                provenance={
                    "assistant": _source_ref(assistant_path, campaign),
                    "tool_result": (
                        _source_ref(response_path, campaign)
                        if response_path.is_file() else None
                    ),
                    "tool_name": fn,
                },
            )
            actions.append(action)
            by_decision.setdefault(decision_id, []).append(action.action_id)
            if fn == "evaluate_candidate":
                decision_index += 1
                decision_id = f"decision-{decision_index:02d}"
    return actions, by_decision, decision_requests


def _snapshot_inputs(campaign: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(campaign.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(campaign).as_posix()
        if rel.startswith(("profiler/", "trace/")):
            continue
        if path.name.endswith(".tmp"):
            continue
        result[rel] = sha256_file(path)
    return result


def build_trace(campaign: Path, output: Path) -> dict[str, Any]:
    campaign = campaign.resolve(strict=True)
    output = output.resolve()
    if output == campaign or campaign in output.parents:
        raise TraceError("trace output must be outside the sealed campaign")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("trace output directory must be new or empty")
    before = _snapshot_inputs(campaign)
    result_path = campaign / "RESULT.json"
    session_path = campaign / "SESSION.json"
    manifest_path = campaign / "INTERACTIVE_MANIFEST.json"
    for required in (result_path, session_path, manifest_path):
        if not required.is_file():
            raise TraceError(f"sealed campaign is missing {required.name}")
    result = load_json(result_path)
    session = load_json(session_path)
    manifest = load_json(manifest_path)
    result_ref = _source_ref(result_path, campaign)

    baseline_source_sha = "unavailable"
    config = manifest.get("config") or {}
    target_id = next(iter((config.get("targets") or {"unknown": {}})))
    target = (config.get("targets") or {}).get(target_id, {})
    baseline_source_path = target.get("baseline_source")
    if baseline_source_path:
        path = Path(str(baseline_source_path))
        if path.is_file() and campaign in path.resolve().parents:
            baseline_source_sha = sha256_file(path)
    if baseline_source_sha == "unavailable":
        candidate_sources = [
            str(row.get("source", ""))
            for row in (session.get("messages") or [])
            if isinstance(row, dict) and row.get("source")
        ]
        if candidate_sources:
            baseline_source_sha = sha256_text(candidate_sources[0])

    tool_contract_hash = physical_tool_contract_hash(config)
    baseline_eval = _baseline_evaluation(result, result_ref, tool_contract_hash)
    candidates: dict[str, Candidate] = {
        BASELINE_CANDIDATE_ID: baseline_candidate(
            baseline_source_sha,
            provenance={"interactive_manifest": _source_ref(manifest_path, campaign)},
        )
    }
    evaluations: dict[str, Evaluation] = {baseline_eval.evaluation_id: baseline_eval}
    transitions: dict[str, Transition] = {}
    rows: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for candidate_path in sorted((campaign / "evaluations").glob("candidate-*/candidate.json")):
        candidate_raw = load_json(candidate_path)
        row_path = candidate_path.parent / "result.json"
        if not row_path.is_file():
            raise TraceError(f"candidate result missing for {candidate_path.parent.name}")
        row = load_json(row_path)
        evaluation_raw = row.get("search_evaluation") or row.get("evaluation")
        if not isinstance(evaluation_raw, dict):
            raise TraceError("candidate result has no search evaluation")
        rows.append((candidate_raw, evaluation_raw, _source_ref(row_path, campaign)))

    # Frozen DecisionPoint campaigns carry predecessor candidates outside the
    # run's evaluation directory.  Their signed CC-03T state is required to
    # reconstruct the selected parent without inventing a linear history.
    cc03t_state = session.get("cc03t_state") or result.get("cc03t_state") or {}
    row_candidate_ids = {str(row[0]["id"]) for row in rows}
    for candidate_id, raw in (cc03t_state.get("trace_candidates") or {}).items():
        if candidate_id == BASELINE_CANDIDATE_ID:
            continue
        candidates.setdefault(candidate_id, Candidate(
            candidate_id=str(raw["candidate_id"]),
            parent_candidate_id=raw.get("parent_candidate_id"),
            source_sha256=str(raw.get("source_sha256", "unavailable")),
            patch_sha256=raw.get("patch_sha256"),
            evaluation_ref=raw.get("evaluation_ref"),
            generated_rtl_ref=raw.get("generated_rtl_ref"),
            generated_rtl_sha256=raw.get("generated_rtl_sha256"),
            provenance=dict(raw.get("provenance") or {}) | {"source": "cc03t_state"},
        ))
    for evaluation_id, raw in (cc03t_state.get("trace_evaluations") or {}).items():
        if evaluation_id == "evaluation-candidate-00":
            continue
        evaluations.setdefault(evaluation_id, Evaluation(
            evaluation_id=str(raw["evaluation_id"]),
            candidate_id=str(raw["candidate_id"]),
            status=str(raw.get("status", "unknown")),
            stage=str(raw.get("stage", "unknown")),
            correctness_ok=(
                raw.get("correctness_ok")
                if isinstance(raw.get("correctness_ok"), bool) else None
            ),
            candidate_valid=bool(raw.get("candidate_valid")),
            promotable=bool(raw.get("promotable")),
            ppa=raw.get("ppa"),
            raw_refs=tuple(raw.get("raw_refs", [])),
            provenance=dict(raw.get("provenance") or {}) | {"source": "cc03t_state"},
        ))
    for transition_id, raw in (cc03t_state.get("trace_transitions") or {}).items():
        if str(raw.get("to_candidate_id")) in row_candidate_ids:
            continue
        transitions[transition_id] = Transition(
            transition_id=str(raw["transition_id"]),
            decision_point_id=str(raw["decision_point_id"]),
            from_candidate_id=str(raw["from_candidate_id"]),
            to_candidate_id=str(raw["to_candidate_id"]),
            evaluation_id=str(raw.get("evaluation_id") or raw.get("evaluation_ref")),
            best_before_candidate_id=str(raw["best_before_candidate_id"]),
            parent_delta=dict(raw.get("parent_delta") or {}),
            best_delta=dict(raw.get("best_delta") or {}),
            correctness=dict(raw.get("correctness") or {}),
            became_new_best=bool(raw.get("became_new_best")),
            raw_evidence_refs=tuple(raw.get("raw_evidence_refs", [])),
            provenance=dict(raw.get("provenance") or {}) | {"source": "cc03t_state"},
            run_id=raw.get("run_id"),
            branch_id=raw.get("branch_id"),
            selected_parent_id=raw.get("selected_parent_id"),
            source_before_sha256=raw.get("source_before_sha256"),
            source_after_sha256=raw.get("source_after_sha256"),
            patch_ref=raw.get("patch_ref"),
            cost_ref=raw.get("cost_ref"),
        )

    actions, actions_by_decision, decision_requests = _trace_actions(campaign)
    # Normalize old baseline-null parent identities to the stable candidate-00.
    candidate_id_map = {
        str(row[0]["id"]): str(row[0]["id"])
        for row in rows
    }
    first_state_transition = next((
        raw for raw in (cc03t_state.get("trace_transitions") or {}).values()
        if str(raw.get("to_candidate_id")) in row_candidate_ids
    ), None)
    best_before_id = str(
        (first_state_transition or {}).get(
            "best_before_candidate_id", BASELINE_CANDIDATE_ID
        )
    )
    prior_evaluations: list[Evaluation] = []
    parent_selections: list[dict[str, Any]] = []
    decision_points: list[DecisionPoint] = []
    for index, (candidate_raw, evaluation_raw, row_ref) in enumerate(rows, start=1):
        candidate_id = str(candidate_raw["id"])
        parent_id = candidate_raw.get("parent_id") or BASELINE_CANDIDATE_ID
        if parent_id not in candidates and parent_id not in candidate_id_map:
            raise TraceError("candidate parent is absent from sealed campaign")
        eval_id = f"evaluation-{candidate_id}"
        candidate = Candidate(
            candidate_id=candidate_id,
            parent_candidate_id=parent_id,
            source_sha256=str(candidate_raw.get("source_sha256") or sha256_text(str(candidate_raw.get("source", "")))),
            patch_sha256=sha256_text(str(candidate_raw.get("diff", ""))),
            evaluation_ref=eval_id,
            provenance={"candidate": row_ref},
        )
        evaluation = _evaluation_from_dict(
            candidate_id, evaluation_raw,
            evaluation_id=eval_id, source_ref=row_ref,
            tool_contract_hash=tool_contract_hash,
        )
        candidates[candidate_id] = candidate
        evaluations[eval_id] = evaluation
        parent_eval = evaluations.get(candidates[parent_id].evaluation_ref or "")
        best_before_eval = evaluations[candidates[best_before_id].evaluation_ref or ""]
        current_ppa = evaluation.ppa if evaluation.candidate_valid else None
        parent_delta = compare_ppa(current_ppa, parent_eval.ppa if parent_eval else None)
        best_delta = compare_ppa(current_ppa, best_before_eval.ppa)
        current_score = _candidate_score(evaluation)
        became_new_best = current_score < _candidate_score(best_before_eval)
        decision_id = f"decision-{index:02d}"
        recorded = next((
            raw for raw in (cc03t_state.get("trace_transitions") or {}).values()
            if str(raw.get("to_candidate_id")) == candidate_id
        ), None)
        transition = Transition(
            transition_id=str((recorded or {}).get("transition_id", f"transition-{index:02d}")),
            decision_point_id=str((recorded or {}).get("decision_point_id", decision_id)),
            from_candidate_id=parent_id,
            to_candidate_id=candidate_id,
            evaluation_id=eval_id,
            best_before_candidate_id=str(
                (recorded or {}).get("best_before_candidate_id", best_before_id)
            ),
            parent_delta=dict((recorded or {}).get("parent_delta", parent_delta)),
            best_delta=dict((recorded or {}).get("best_delta", best_delta)),
            correctness=dict((recorded or {}).get("correctness") or {
                "status": "available" if evaluation.correctness_ok is not None else "unavailable",
                "passed": evaluation.correctness_ok,
            }),
            became_new_best=bool(
                (recorded or {}).get("became_new_best", became_new_best)
            ),
            raw_evidence_refs=tuple(
                (recorded or {}).get("raw_evidence_refs", evaluation.raw_refs)
            ),
            provenance={"result": row_ref},
            run_id=campaign.name,
            branch_id=str(result.get("feedback_arm", "unknown")),
            selected_parent_id=parent_id,
            source_before_sha256=candidates[parent_id].source_sha256,
            source_after_sha256=candidate.source_sha256,
            patch_ref=candidate.patch_sha256,
            cost_ref=f"validation_timeline:{candidate_id}",
        )
        transitions[transition.transition_id] = transition
        selected = select_parent(
            index,
            current_best_candidate_id=best_before_id,
            decision_point_id=decision_id,
        )
        # Historical runs may not implement policy v1. Record rather than rewrite reality.
        actual_parent = parent_id
        parent_selections.append(signed_record({
            "schema_version": PARENT_SELECTION_SCHEMA,
            "decision_point_id": decision_id,
            "evaluation_slot": index,
            "selected_parent_id": actual_parent,
            "selection_reason": (
                selected.selection_reason
                if selected.selected_parent_id == actual_parent
                else "historical_controller_selection"
            ),
            "policy_revision": (
                SEARCH_POLICY_REVISION
                if selected.selected_parent_id == actual_parent
                else "historical-interactive"
            ),
            "fixture_override": False,
            "provenance": {"candidate": row_ref},
        }))
        decision_points.append(DecisionPoint(
            decision_point_id=decision_id,
            decision_kind=_classify_decision(prior_evaluations[-1] if prior_evaluations else None, index == 1),
            selected_parent_id=actual_parent,
            current_best_candidate_id=best_before_id,
            remaining_evaluation_budget=max(0, int(session.get("max_evaluations", len(rows))) - index + 1),
            model_request_ref=(decision_requests.get(decision_id) or {}).get("model_request_ref"),
            visible_context_hash=(decision_requests.get(decision_id) or {}).get("visible_context_hash"),
            tool_schema_hash=(decision_requests.get(decision_id) or {}).get("tool_schema_hash"),
            action_refs=tuple(actions_by_decision.get(decision_id, [])),
            provenance={"candidate": row_ref},
        ))
        prior_evaluations.append(evaluation)
        if transition.became_new_best:
            best_before_id = candidate_id

    verified_ids = [
        candidate_id for candidate_id, candidate in candidates.items()
        if evaluations.get(candidate.evaluation_ref or "")
        and evaluations[candidate.evaluation_ref or ""].candidate_valid
    ]
    invalid_ids = [
        candidate_id for candidate_id, candidate in candidates.items()
        if evaluations.get(candidate.evaluation_ref or "")
        and not evaluations[candidate.evaluation_ref or ""].candidate_valid
    ]
    pool = CandidatePool(
        baseline_candidate_id=BASELINE_CANDIDATE_ID,
        current_candidate_id=(rows[-1][0]["id"] if rows else BASELINE_CANDIDATE_ID),
        current_best_candidate_id=best_before_id,
        eligible_parent_ids=tuple(sorted(set([BASELINE_CANDIDATE_ID, best_before_id]))),
        verified_candidate_ids=tuple(verified_ids),
        invalid_candidate_ids=tuple(invalid_ids),
        remaining_evaluation_budget=max(
            0, int(session.get("max_evaluations", len(rows))) - len(rows)
        ),
    )
    dag = signed_record({
        "schema_version": DAG_SCHEMA,
        "baseline_candidate_id": BASELINE_CANDIDATE_ID,
        "current_candidate_id": rows[-1][0]["id"] if rows else BASELINE_CANDIDATE_ID,
        "current_best_candidate_id": best_before_id,
        "candidates": [row.to_dict() for row in candidates.values()],
        "candidate_pool": pool.to_dict(),
        "parent_selections": parent_selections,
        "unexecuted_policy_slots": [
            {
                "evaluation_slot": slot,
                "status": "not_executed",
                "reason": (
                    "run_finished" if result.get("status") == "finished"
                    else "run_stopped"
                ),
            }
            for slot in range(len(rows) + 1, int(session.get("max_evaluations", len(rows))) + 1)
        ],
    })
    trace = signed_record({
        "schema_version": TRACE_SCHEMA,
        "run_id": campaign.name,
        "experiment_id": session.get("cc03t_experiment_id") or campaign.name,
        "branch_id": session.get("cc03t_branch_id") or str(result.get("feedback_arm", "unknown")),
        "arm": result.get("feedback_arm"),
        "frozen_run_fingerprint": manifest.get("frozen_run_fingerprint"),
        "memory_mode": result.get("memory_mode"),
        "initial_candidate_id": BASELINE_CANDIDATE_ID,
        "final_candidate_id": rows[-1][0]["id"] if rows else None,
        "status": result.get("status"),
        "decision_points": [row.to_dict() for row in decision_points],
        "actions": [row.to_dict() for row in actions],
        "evaluations": [row.to_dict() for row in evaluations.values()],
        "transitions": [row.to_dict() for row in transitions.values()],
        "cost_spans": session.get("validation_timeline", []),
        "provenance": {
            "result": result_ref,
            "session": _source_ref(session_path, campaign),
            "manifest": _source_ref(manifest_path, campaign),
        },
    })
    after = _snapshot_inputs(campaign)
    if before != after:
        raise TraceError("sealed campaign changed while building trace")

    output.mkdir(parents=True, exist_ok=True)
    atomic_dump(output / "RUN_TRACE.json", trace)
    atomic_dump(output / "CANDIDATE_DAG.json", dag)
    trace_manifest = signed_record({
        "schema_version": "chia-boom.trace-manifest.v1",
        "campaign_hash": content_hash(before),
        "run_trace_ref": trace["content_hash"],
        "candidate_dag_ref": dag["content_hash"],
        "builder_revision": TRACE_SCHEMA,
        "model_calls": 0,
        "eda_calls": 0,
        "simulator_calls": 0,
    })
    atomic_dump(output / "TRACE_MANIFEST.json", trace_manifest)
    return trace_manifest


def trace_build_cost(campaign: Path, output: Path) -> dict[str, Any]:
    started = time.monotonic_ns()
    result = build_trace(campaign, output)
    elapsed = time.monotonic_ns() - started
    return result | {"wall_time_ns": elapsed}
