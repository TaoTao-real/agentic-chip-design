from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from chia.base.ChiaFunction import get

from .artifacts import (
    CandidateArtifact,
    EvaluationArtifact,
    dump_json,
    load_json,
    sha256_text,
)
from .campaign import _evaluate_with_infra_retries
from .chipcontext.runtime import (
    FEEDBACK_ARMS,
    RuntimeContext,
    build_structured_feedback,
    prepare_runtime_context,
    query_runtime_context,
)
from .core import apply_exact_edits, canonical_hash, lineage_fields, make_diff, update_validity
from .deepseek import DeepSeekOfficialToolLLM
from .finalize import BoomFinalizationNode, finalize_candidate
from .frozen import (
    baseline_input,
    baseline_rtl,
    freeze_run_inputs,
    immutable_contract,
    load_frozen_run_config,
    qualification_request,
    qualified_artifact_hashes,
    runtime_tool_versions,
    seal_frozen_run,
    verify_frozen_run,
    verify_qualification,
)
from .knowledge import MEMORY_MODES, MEMORY_TOOL_SPECS, KnowledgeStore
from .nodes import BoomCandidateEvaluationNode
from .optimization_trace import (
    BASELINE_CANDIDATE_ID,
    CandidatePool,
    ParentSelection,
    TRACE_TAIL_MAX_BYTES,
    Transition,
    branch_history,
    build_trace_tail,
    build_visibility_manifest,
    canonical_json,
    compare_ppa,
    content_hash,
    physical_tool_contract_hash,
    select_parent,
    signed_record,
)
from .validation_timeline import Timeline, timeline_from_evaluation


INTERACTIVE_SYSTEM = """You are an autonomous hardware optimization agent.
Work only through the supplied tools. Optimize the specified synthesizable
BOOM module for lower post-synthesis critical delay while preserving exact
cycle-visible behavior, interfaces, dimensions, and configuration. Slice LUTs
must remain within 105% of baseline. Discover bottlenecks yourself from source,
generated RTL, raw timing evidence, and measured candidate results. No human
diagnosis or historical solution is available. Never ask for a suggested
optimization. Inspect, form hypotheses, edit, evaluate, repair, and finish with
the best measured candidate. Budget inspection so that you evaluate a concrete
edit instead of exhausting the turn limit on raw reads. Do not claim improvement
without tool evidence."""

OBSERVATION_TOOLS = {
    "search_knowledge", "retrieve_knowledge", "read_source", "search_source",
    "read_timing", "read_generated_rtl", "read_candidate_artifact",
    "query_candidate_status", "list_candidate_artifacts",
    "query_candidate_failure", "compare_candidate_metrics",
    "query_candidate_timing_paths",
}
CC03T_ARMS = ("E0", "E1T")
TRACE_TAIL_PREFIX = "CC03T_TRACE_TAIL\n"
INSPECTION_WARNING_TURNS = 12
INSPECTION_HARD_LIMIT_TURNS = 16


def _advance_inspection_budget(
    current: int, *, observed: bool, progressed: bool,
) -> tuple[int, str | None]:
    if progressed:
        return 0, None
    if not observed:
        return current, None
    value = current + 1
    if value == INSPECTION_WARNING_TURNS:
        return value, (
            "Inspection budget warning: form a concrete hypothesis now. Raw-read "
            "tools will be disabled after four more inspection-only turns until "
            "you apply an edit or evaluate the edited source."
        )
    if value == INSPECTION_HARD_LIMIT_TURNS:
        return value, (
            "Inspection-only budget is exhausted. The next action must apply a "
            "concrete edit or evaluate the current edited source."
        )
    return value, None


def _available_tool_specs(
    tool_specs: list[dict[str, Any]], inspection_only_turns: int,
) -> list[dict[str, Any]]:
    if inspection_only_turns < INSPECTION_HARD_LIMIT_TURNS:
        return tool_specs
    return [
        spec for spec in tool_specs
        if (spec.get("function") or {}).get("name") not in OBSERVATION_TOOLS
    ]


def _prepare_interactive_snapshot(
    config: dict[str, Any], output: Path
) -> dict[str, Any]:
    qualification_path = (
        Path(config["remote"]["install_root"])
        / "qualification/QUALIFICATION.json"
    )
    qualification = load_json(qualification_path)
    tool_versions = runtime_tool_versions(config)
    verify_qualification(
        config, qualification, tool_versions=tool_versions
    )
    source_reference = {
        "qualification_fingerprint": qualification_request(config)["fingerprint"],
        "qualified_artifact_hashes": qualified_artifact_hashes(config),
        "tool_versions": tool_versions,
    }
    source_reference["fingerprint"] = canonical_hash(source_reference)
    frozen_config = freeze_run_inputs(config, output / "frozen")
    qualification_root = output / "frozen/qualification"
    qualification_root.mkdir(parents=True)
    dump_json(qualification_root / "QUALIFICATION.json", qualification)
    dump_json(qualification_root / "TOOL_VERSIONS.json", tool_versions)
    frozen_manifest = seal_frozen_run(output / "frozen", frozen_config)
    manifest = {
        "schema_version": "interactive-frozen-run-v1",
        "source_contract": immutable_contract(config),
        "source_reference": source_reference,
        "config": frozen_config,
        "frozen_run_fingerprint": frozen_manifest["fingerprint"],
    }
    dump_json(output / "INTERACTIVE_MANIFEST.json", manifest)
    dump_json(output / "INTERACTIVE_REFERENCE.json", {
        **source_reference,
        "frozen_run_fingerprint": frozen_manifest["fingerprint"],
    })
    return frozen_config


def _load_interactive_snapshot(
    requested_config: dict[str, Any], output: Path
) -> dict[str, Any]:
    config = load_frozen_run_config(
        requested_config,
        root=output / "frozen",
        run_manifest=output / "INTERACTIVE_MANIFEST.json",
        schema_version="interactive-frozen-run-v1",
    )
    qualification_root = output / "frozen/qualification"
    qualification = load_json(qualification_root / "QUALIFICATION.json")
    recorded_versions = load_json(
        qualification_root / "TOOL_VERSIONS.json"
    )
    current_versions = runtime_tool_versions(config)
    if current_versions != recorded_versions:
        raise RuntimeError(
            "interactive compiler, simulator, or EDA versions changed"
        )
    verify_qualification(
        config, qualification, tool_versions=current_versions
    )
    return config


def _interactive_system(memory_mode: str) -> str:
    if memory_mode == "none":
        return INTERACTIVE_SYSTEM
    if memory_mode == "generic":
        return INTERACTIVE_SYSTEM.replace(
            "No human\ndiagnosis or historical solution is available. Never ask for a suggested\noptimization.",
            "No target-specific diagnosis or historical solution is available. Search the\ncross-target process-memory index once before detailed analysis, then retrieve\nonly sections you judge useful and validate every hypothesis with hardware tools.",
        )
    if memory_mode == "target":
        return INTERACTIVE_SYSTEM.replace(
            "No human\ndiagnosis or historical solution is available. Never ask for a suggested\noptimization.",
            "Structured historical design memory can be searched with knowledge tools.\n"
            "Search its index once before detailed analysis and retrieve the most relevant\n"
            "target-specific episode; choose only the sections you judge useful. Then validate\n"
            "every remembered hypothesis with correctness and EDA tools.",
        )
    raise ValueError(f"unknown memory mode: {memory_mode}")


def _interactive_system_with_feedback(memory_mode: str, feedback_arm: str) -> str:
    value = _interactive_system(memory_mode)
    if feedback_arm == "E1":
        return value + (
            "\nAfter each evaluation, ChipContext supplies deterministic structured facts "
            "from the same raw artifacts available to the control arm. Use its query tools "
            "to inspect missing, conflicting, or source-linked evidence before the next edit."
        )
    if feedback_arm not in {"E0", "E1T"}:
        raise ValueError(f"unknown feedback arm: {feedback_arm}")
    return value


def _messages_for_model(
    messages: list[dict[str, Any]], *, keep_recent_tool_results: int = 4,
) -> list[dict[str, Any]]:
    """Keep the full audit transcript on disk while bounding repeated prompt bytes.

    Old tool results stay addressable through the same read/search/query tools.
    Their content is replaced only in the next model request by an identity
    marker; assistant tool calls and their corresponding tool responses remain
    structurally paired for API compatibility.
    """

    tool_indexes = [
        index for index, message in enumerate(messages)
        if message.get("role") == "tool"
    ]
    retained = set(tool_indexes[-keep_recent_tool_results:])
    tool_names: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            call_id = str(call.get("id", ""))
            tool_names[call_id] = str((call.get("function") or {}).get("name", "unknown"))

    compacted: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        copied = dict(message)
        if message.get("role") == "tool" and index not in retained:
            original = str(message.get("content", ""))
            call_id = str(message.get("tool_call_id", ""))
            copied["content"] = json.dumps({
                "status": "archived_tool_result",
                "tool": tool_names.get(call_id, "unknown"),
                "content_sha256": sha256_text(original),
                "content_chars": len(original),
                "instruction": "Reissue the tool query if these bytes are needed again.",
            }, sort_keys=True)
        compacted.append(copied)
    return compacted


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_source",
            "description": "Read a numbered line range from the current editable Chisel source.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["start_line", "end_line"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_source",
            "description": "Search the current Chisel source for a literal string and return nearby numbered lines.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "context_lines": {"type": "integer", "minimum": 0, "maximum": 12},
                },
                "required": ["query", "context_lines"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_timing",
            "description": "Read raw baseline Vivado timing lines matching a literal query. Empty query returns the report head.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 160},
                },
                "required": ["query", "max_lines"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_generated_rtl",
            "description": "Read or search frozen baseline generated RTL for the target or its slot submodule.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "query": {"type": "string"},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 160},
                },
                "required": ["file", "query", "max_lines"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_exact_edits",
            "description": "Apply exact old-to-new replacements to the current Chisel source. Each old string must occur once.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array", "minItems": 1, "maxItems": 16,
                        "items": {
                            "type": "object",
                            "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
                            "required": ["old", "new"], "additionalProperties": False,
                        },
                    }
                },
                "required": ["edits"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_candidate",
            "description": "Run Chisel elaboration, 1,000,000-cycle differential verification, and Vivado post-synthesis PPA on current source.",
            "strict": True,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revert_source",
            "description": "Restore the baseline source or the best measured valid source.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string", "enum": ["baseline", "best"]}},
                "required": ["target"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "End the session and submit the best measured valid candidate. Explain the measured basis briefly.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"], "additionalProperties": False,
            },
        },
    },
]


RAW_EVIDENCE_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_candidate_artifact",
            "description": (
                "Read a bounded line range from a raw artifact belonging to an "
                "evaluated candidate. Use refs returned by evaluate_candidate."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "artifact_ref": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "line_count": {"type": "integer", "minimum": 1, "maximum": 400},
                    "limit_bytes": {"type": "integer", "minimum": 1, "maximum": 65536},
                },
                "required": [
                    "candidate_id", "artifact_ref", "start_line", "line_count",
                    "limit_bytes",
                ],
                "additionalProperties": False,
            },
        },
    },
]


STRUCTURED_EVIDENCE_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_candidate_status",
            "description": "Query grounded check, binding, applicability, missing, and conflict status.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"candidate_id": {"type": "string"}},
                "required": ["candidate_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_candidate_artifacts",
            "description": "List authorized raw artifact refs for one evaluated candidate.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "stage": {"type": "string"},
                    "kinds": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["candidate_id", "stage", "kinds", "limit"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_candidate_failure",
            "description": "Query a recorded check and any grounded mismatch observation separately.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "check": {"type": "string"},
                },
                "required": ["candidate_id", "check"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_candidate_metrics",
            "description": "Compare explicit current metrics with the frozen bound baseline.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "stage": {"type": "string", "enum": ["post_synth", "post_route"]},
                    "metric_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["candidate_id", "stage", "metric_ids"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_candidate_timing_paths",
            "description": "Query collected timing paths and report coverage without claiming global coverage.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "extraction_ref": {"type": "string"},
                    "stage": {"type": "string", "enum": ["post_synth", "post_route"]},
                    "path_group": {"type": "string"},
                    "source": {"type": "string"},
                    "destination": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": [
                    "candidate_id", "extraction_ref", "stage", "path_group",
                    "source", "destination", "limit",
                ],
                "additionalProperties": False,
            },
        },
    },
]


def _interactive_tool_specs(memory_enabled: bool, feedback_arm: str) -> list[dict[str, Any]]:
    if feedback_arm not in set(FEEDBACK_ARMS) | {"E1T"}:
        raise ValueError(f"unknown feedback arm: {feedback_arm}")
    return (
        (MEMORY_TOOL_SPECS if memory_enabled else [])
        + TOOL_SPECS
        + RAW_EVIDENCE_TOOL_SPECS
        + (STRUCTURED_EVIDENCE_TOOL_SPECS if feedback_arm == "E1" else [])
    )


def _numbered(lines: list[str], start: int, end: int) -> str:
    start = max(1, start)
    end = min(len(lines), max(start, end))
    return "\n".join(f"{i:5d}: {lines[i-1]}" for i in range(start, end + 1))


def _literal_context(text: str, query: str, max_lines: int, context: int = 2) -> str:
    lines = text.splitlines()
    if not query:
        return _numbered(lines, 1, min(len(lines), max_lines))
    matches = [i for i, line in enumerate(lines) if query.lower() in line.lower()]
    selected: list[int] = []
    for index in matches:
        selected.extend(range(max(0, index - context), min(len(lines), index + context + 1)))
    selected = sorted(set(selected))[:max_lines]
    if not selected:
        return f"No literal matches for {query!r}."
    return "\n".join(f"{i+1:5d}: {lines[i]}" for i in selected)


def _compact_evaluation(value: dict[str, Any]) -> dict[str, Any]:
    raw = value.get("raw_error") or ""
    return {
        "candidate_id": value.get("candidate_id"),
        "status": value.get("status"),
        "stage": value.get("stage"),
        "failure_class": value.get("failure_class"),
        "build_ok": value.get("build_ok"),
        "interface_ok": value.get("interface_ok"),
        "correctness_ok": value.get("correctness_ok"),
        "candidate_valid": value.get("candidate_valid"),
        "promotable": value.get("promotable"),
        "post_synth": value.get("post_synth"),
        "raw_error_tail": raw[-8000:],
    }


def _meets_auto_stop(
    baseline_delay_ns: float,
    post_synth: dict[str, Any] | None,
    threshold_percent: float | None,
) -> bool:
    if threshold_percent is None or not post_synth:
        return False
    candidate_delay = post_synth.get("critical_delay_ns")
    if not isinstance(candidate_delay, (int, float)) or baseline_delay_ns <= 0:
        return False
    improvement = (baseline_delay_ns - float(candidate_delay)) / baseline_delay_ns * 100.0
    return improvement >= threshold_percent


def _trace_ppa(
    raw: dict[str, Any] | None, *, tool_contract_hash: str,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    delay = raw.get("critical_delay_ns")
    luts = raw.get("slice_luts")
    if not isinstance(delay, (int, float)) or not isinstance(luts, (int, float)):
        return None
    return {
        "stage": "post_synth",
        "critical_delay_ns": float(delay),
        "slice_luts": int(luts),
        "tool_contract_hash": raw.get("tool_contract_hash") or tool_contract_hash,
    }


def _trace_transition_from_state(
    *,
    index: int,
    candidate_id: str,
    parent_id: str,
    best_before_id: str,
    evaluation: dict[str, Any],
    parent_ppa: dict[str, Any] | None,
    best_before_ppa: dict[str, Any] | None,
    raw_evidence_refs: list[str],
    tool_contract_hash: str,
    run_id: str,
    branch_id: str,
    source_before_sha256: str,
    source_after_sha256: str,
    patch_ref: str,
) -> Transition:
    current_ppa = (
        _trace_ppa(
            evaluation.get("post_synth"),
            tool_contract_hash=tool_contract_hash,
        )
        if evaluation.get("candidate_valid") else None
    )
    current_score = (
        float(current_ppa["critical_delay_ns"]), float(current_ppa["slice_luts"])
    ) if current_ppa else (float("inf"), float("inf"))
    best_score = (
        float(best_before_ppa["critical_delay_ns"]), float(best_before_ppa["slice_luts"])
    ) if best_before_ppa else (float("inf"), float("inf"))
    correctness = evaluation.get("correctness_ok")
    return Transition(
        transition_id=f"transition-{index:02d}",
        decision_point_id=f"decision-{index:02d}",
        from_candidate_id=parent_id,
        to_candidate_id=candidate_id,
        evaluation_id=f"evaluation-{candidate_id}",
        best_before_candidate_id=best_before_id,
        parent_delta=compare_ppa(current_ppa, parent_ppa),
        best_delta=compare_ppa(current_ppa, best_before_ppa),
        correctness={
            "status": "available" if isinstance(correctness, bool) else "unavailable",
            "passed": correctness if isinstance(correctness, bool) else None,
        },
        became_new_best=current_score < best_score,
        raw_evidence_refs=tuple(raw_evidence_refs),
        provenance={"evaluation_index": index},
        run_id=run_id,
        branch_id=branch_id,
        selected_parent_id=parent_id,
        source_before_sha256=source_before_sha256,
        source_after_sha256=source_after_sha256,
        patch_ref=patch_ref,
        cost_ref=f"validation_timeline:{candidate_id}",
    )


def _cc03t_tail(
    state: dict[str, Any], *, max_evaluations: int,
) -> dict[str, Any]:
    from .optimization_trace import Candidate, Evaluation

    candidate_rows = {
        key: Candidate(**value) for key, value in state.get("trace_candidates", {}).items()
    }
    evaluation_rows = {
        key: Evaluation(
            evaluation_id=value["evaluation_id"],
            candidate_id=value["candidate_id"],
            status=value["status"],
            stage=value["stage"],
            correctness_ok=value.get("correctness_ok"),
            candidate_valid=bool(value.get("candidate_valid")),
            promotable=bool(value.get("promotable")),
            ppa=value.get("ppa"),
            raw_refs=tuple(value.get("raw_refs", [])),
            provenance=dict(value.get("provenance", {})),
        ) for key, value in state.get("trace_evaluations", {}).items()
    }
    transition_rows = {
        key: Transition(
            transition_id=value["transition_id"],
            decision_point_id=value["decision_point_id"],
            from_candidate_id=value["from_candidate_id"],
            to_candidate_id=value["to_candidate_id"],
            evaluation_id=value["evaluation_id"],
            best_before_candidate_id=value["best_before_candidate_id"],
            parent_delta=dict(value["parent_delta"]),
            best_delta=dict(value["best_delta"]),
            correctness=dict(value["correctness"]),
            became_new_best=bool(value["became_new_best"]),
            raw_evidence_refs=tuple(value.get("raw_evidence_refs", [])),
            provenance=dict(value.get("provenance", {})),
        ) for key, value in state.get("trace_transitions", {}).items()
    }
    last = transition_rows.get(state.get("last_transition_id"))
    history = branch_history(
        str(state["selected_parent_id"]), candidate_rows, evaluation_rows,
        transition_rows, limit=3,
    ) if str(state["selected_parent_id"]) != BASELINE_CANDIDATE_ID else []
    return build_trace_tail(
        current_candidate_id=str(state["selected_parent_id"]),
        selected_parent_id=str(state["selected_parent_id"]),
        current_best_candidate_id=str(state["current_best_candidate_id"]),
        last_transition=last,
        history=history,
        remaining_evaluation_budget=max(0, max_evaluations - int(state["evaluations"])),
        raw_evidence_refs=list(last.raw_evidence_refs) if last else [],
        source_classes=state.get("trace_source_classes", []),
        max_bytes=TRACE_TAIL_MAX_BYTES,
    )


def _append_trace_tail_message(
    messages: list[dict[str, Any]], tail: dict[str, Any],
) -> None:
    messages.append({
        "role": "user",
        "content": TRACE_TAIL_PREFIX + canonical_json(tail),
    })


def _prepare_trace_tail(
    state: dict[str, Any], *, max_evaluations: int, label: str,
) -> dict[str, Any]:
    started = time.monotonic_ns()
    tail = _cc03t_tail(state, max_evaluations=max_evaluations)
    trace_elapsed = time.monotonic_ns() - started
    prepare_started = time.monotonic_ns()
    encoded = canonical_json(tail).encode("utf-8")
    prepare_elapsed = time.monotonic_ns() - prepare_started
    ready_started = time.monotonic_ns()
    if not encoded or tail.get("content_hash") != content_hash({
        key: value for key, value in tail.items()
        if key not in {"content_hash", "serialized_bytes"}
    }):
        raise RuntimeError("TraceTail failed publication integrity check")
    ready_elapsed = time.monotonic_ns() - ready_started
    for stage, active in (
        ("trace_build", trace_elapsed),
        ("feedback_prepare", prepare_elapsed),
        ("feedback_ready", ready_elapsed),
    ):
        state["validation_timeline"].append({
            "span_id": f"{label}-{stage}",
            "stage": stage,
            "wall_time_ns": active,
            "active_time_ns": active,
            "queue_time_ns": None,
            "parent_span_id": None,
            "candidate_id": state.get("selected_parent_id"),
            "leaf": True,
            "unavailable_reason": None,
            "provenance": {"trace_tail_hash": tail["content_hash"]},
        })
    return tail


def _common_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row for row in messages
        if not (
            row.get("role") == "user"
            and str(row.get("content", "")).startswith(TRACE_TAIL_PREFIX)
        )
    ]


def run_interactive_issueq(
    config: dict[str, Any], output: Path, *, seed: int = 41,
    max_turns: int = 24, max_evaluations: int = 5,
    memory_mode: str = "none", feedback_arm: str = "E0",
    auto_stop_improvement_percent: float | None = None,
    cc03t: dict[str, Any] | None = None,
    _resume: bool = False,
) -> dict[str, Any]:
    if memory_mode not in MEMORY_MODES:
        raise ValueError(f"unknown memory mode: {memory_mode}")
    allowed_arms = set(FEEDBACK_ARMS) | ({"E1T"} if cc03t is not None else set())
    if feedback_arm not in allowed_arms:
        raise ValueError(f"unknown feedback arm: {feedback_arm}")
    if cc03t is not None:
        if feedback_arm not in CC03T_ARMS:
            raise ValueError("CC-03T only permits E0 and E1T")
        if memory_mode != "none":
            raise ValueError("CC-03T requires memory_mode=none")
        if cc03t.get("policy_revision") != "baseline-3-best-2-v1":
            raise ValueError("CC-03T requires baseline-3-best-2-v1")
    output.mkdir(parents=True, exist_ok=True)
    session_path = output / "SESSION.json"
    result_path = output / "RESULT.json"
    if not _resume and (session_path.exists() or result_path.exists()):
        raise FileExistsError(f"interactive campaign already contains state: {output}")
    if _resume and not session_path.is_file():
        raise FileNotFoundError("interactive campaign has no saved session")
    if len(config["targets"]) != 1:
        raise ValueError("interactive mode requires exactly one configured target")
    config = (
        _load_interactive_snapshot(config, output)
        if _resume else _prepare_interactive_snapshot(config, output)
    )
    frozen_fingerprint = load_json(output / "INTERACTIVE_MANIFEST.json")[
        "frozen_run_fingerprint"
    ]
    target_id = next(iter(config["targets"]))
    target = config["targets"][target_id]
    rtl_root = baseline_rtl(config, target_id)
    baseline_source = baseline_input(
        config, target_id, "baseline-source.scala"
    ).read_text()
    timing = baseline_input(config, target_id, "baseline-timing.txt").read_text()
    baseline = load_json(baseline_input(config, target_id, "baseline-ppa.json"))
    baseline["maximum_lut_ratio"] = config["physical"]["maximum_lut_ratio"]
    ppa_contract_hash = physical_tool_contract_hash(config)
    saved = load_json(session_path) if _resume else None
    if saved is not None:
        frozen_fields = {
            "seed": seed,
            "max_turns": max_turns,
            "max_evaluations": max_evaluations,
            "memory_mode": memory_mode,
            "feedback_arm": feedback_arm,
            "auto_stop_improvement_percent": auto_stop_improvement_percent,
            "cc03t": cc03t,
        }
        for name, expected in frozen_fields.items():
            if saved.get(name) != expected:
                raise RuntimeError(f"interactive resume changed frozen field: {name}")
        if saved.get("status") == "finished":
            raise RuntimeError("interactive campaign is already finished")
        cc03t = saved.get("cc03t")
        current_source = str(saved["current_source"])
        current_parent_source = str(saved["current_parent_source"])
        current_parent_id = saved.get("current_parent_id")
        best_candidate = saved.get("best_candidate")
        best_source = str(best_candidate["source"]) if best_candidate else None
        best_ppa = saved.get("best_post_synth")
        evaluated_hashes = set(saved.get("evaluated_source_sha256s", []))
        evaluations = int(saved["evaluations"])
        finished = False
        finish_summary = str(saved.get("finish_summary", ""))
        session_usage = list(saved.get("usage", []))
        messages = list(saved["messages"])
        inspection_only_turns = int(saved.get("inspection_only_turns", 0))
        start_turn = int(saved["turn"]) + 1
        cc03t_state = dict(saved.get("cc03t_state") or {})
        if cc03t is not None and saved.get("cc03t_state_hash") != content_hash(cc03t_state):
            raise RuntimeError("CC-03T trace/DAG state failed resume identity check")
    else:
        fixture = dict((cc03t or {}).get("fixture") or {})
        current_source = str(fixture.get("selected_parent_source") or baseline_source)
        current_parent_source = current_source
        current_parent_id: str | None = fixture.get("selected_parent_id")
        if cc03t is not None and current_parent_id is None:
            current_parent_id = BASELINE_CANDIDATE_ID
        best_source: str | None = None
        best_ppa: dict[str, Any] | None = None
        best_candidate: dict[str, Any] | None = None
        evaluated_hashes: set[str] = set()
        evaluations = 0
        finished = False
        finish_summary = ""
        session_usage: list[dict[str, Any]] = []
        inspection_only_turns = 0
        start_turn = 1
        cc03t_state: dict[str, Any] = {}
        if cc03t is not None:
            fixture = dict(cc03t.get("fixture") or {})
            baseline_candidate_row = {
                "candidate_id": BASELINE_CANDIDATE_ID,
                "parent_candidate_id": None,
                "source_sha256": sha256_text(baseline_source),
                "patch_sha256": None,
                "evaluation_ref": "evaluation-candidate-00",
                "generated_rtl_ref": None,
                "generated_rtl_sha256": None,
                "provenance": {"kind": "frozen_baseline"},
            }
            baseline_evaluation_row = {
                "evaluation_id": "evaluation-candidate-00",
                "candidate_id": BASELINE_CANDIDATE_ID,
                "status": "complete",
                "stage": "post_synth",
                "correctness_ok": True,
                "candidate_valid": True,
                "promotable": False,
                "ppa": _trace_ppa(
                    baseline, tool_contract_hash=ppa_contract_hash,
                ),
                "raw_refs": [],
                "provenance": {"kind": "frozen_baseline"},
            }
            trace_candidates = {BASELINE_CANDIDATE_ID: baseline_candidate_row}
            trace_evaluations = {"evaluation-candidate-00": baseline_evaluation_row}
            trace_transitions: dict[str, Any] = {}
            source_by_candidate = {BASELINE_CANDIDATE_ID: baseline_source}
            for row in fixture.get("history", []):
                candidate_row = dict(row["candidate"])
                evaluation_row = dict(row["evaluation"])
                candidate_id = str(candidate_row["candidate_id"])
                trace_candidates[candidate_id] = candidate_row
                trace_evaluations[str(evaluation_row["evaluation_id"])] = evaluation_row
                source_by_candidate[candidate_id] = str(row["source"])
                if row.get("transition"):
                    transition_row = dict(row["transition"])
                    trace_transitions[str(transition_row["transition_id"])] = transition_row
            selected_parent_id = str(
                fixture.get("selected_parent_id") or BASELINE_CANDIDATE_ID
            )
            current_best_id = str(
                fixture.get("current_best_candidate_id") or BASELINE_CANDIDATE_ID
            )
            if selected_parent_id not in source_by_candidate:
                raise ValueError("fixture selected parent source is unavailable")
            current_source = source_by_candidate[selected_parent_id]
            current_parent_source = current_source
            current_parent_id = selected_parent_id
            cc03t_state = {
                "evaluations": 0,
                "selected_parent_id": selected_parent_id,
                "current_best_candidate_id": current_best_id,
                "trace_candidates": trace_candidates,
                "trace_evaluations": trace_evaluations,
                "trace_transitions": trace_transitions,
                "last_transition_id": fixture.get("last_transition_id"),
                "source_by_candidate": source_by_candidate,
                "parent_selections": [],
                "visibility_manifests": [],
                "validation_timeline": [],
                "fixture_override": bool(fixture.get("fixture_override", False)),
                "trace_source_classes": [],
            }
            if fixture.get("frozen_decision_parent") and not fixture.get("fixture_override"):
                selection = ParentSelection(
                    decision_point_id="decision-01",
                    evaluation_slot=1,
                    selected_parent_id=selected_parent_id,
                    selection_reason="frozen_decision_parent",
                    policy_revision="baseline-3-best-2-v1",
                    fixture_override=False,
                    provenance={"fixture": str(fixture.get("name", "unknown"))},
                )
            else:
                selection = select_parent(
                    1,
                    current_best_candidate_id=current_best_id,
                    decision_point_id="decision-01",
                    fixture_parent_id=(selected_parent_id if fixture.get("fixture_override") else None),
                )
            if selection.selected_parent_id != selected_parent_id:
                raise ValueError("fixture parent conflicts with frozen SearchPolicy")
            cc03t_state["parent_selections"].append(selection.to_dict())
    knowledge_root = config.get("knowledge", {}).get("root")
    knowledge = KnowledgeStore(
        memory_mode, Path(knowledge_root) if knowledge_root else None
    )
    if memory_mode == "target" and not any(
        row.get("knowledge_class") == "target-specific-solution"
        for row in knowledge.manifest()["episodes"]
    ):
        raise ValueError(
            "target memory mode requires an explicitly mounted private "
            "target-specific Design Episode directory"
        )
    knowledge_retrievals: list[dict[str, Any]] = (
        list(saved.get("knowledge_retrievals", [])) if saved else []
    )
    tool_specs = _interactive_tool_specs(knowledge.enabled, feedback_arm)
    runtime_contexts: dict[str, RuntimeContext] = {
        key: RuntimeContext.from_dict(value)
        for key, value in (
            saved.get("chipcontext_contexts", {})
            if saved else ((cc03t or {}).get("fixture") or {}).get("runtime_contexts", {})
        ).items()
    }
    chipcontext_costs: list[dict[str, Any]] = (
        list(saved.get("chipcontext_costs", [])) if saved else []
    )
    dump_json(output / "KNOWLEDGE_MANIFEST.json", knowledge.manifest())
    if saved is None:
        fixture_note = ""
        if cc03t is not None and cc03t_state["selected_parent_id"] != BASELINE_CANDIDATE_ID:
            selected = cc03t_state["trace_candidates"][cc03t_state["selected_parent_id"]]
            selected_eval = cc03t_state["trace_evaluations"].get(
                selected.get("evaluation_ref"), {}
            )
            fixture_note = (
                "\nFrozen decision parent: " + cc03t_state["selected_parent_id"]
                + ". Its unprocessed recorded evaluation is:\n"
                + json.dumps(selected_eval, sort_keys=True)
            )
        messages = [{
            "role": "user",
            "content": (
                "Optimize target " + target_id + " in file " + target["mutable_file"] + ".\n"
                "Baseline post-synthesis critical delay is " + str(baseline["critical_delay_ns"]) +
                " ns and Slice LUTs are " + str(baseline["slice_luts"]) + ".\n"
                f"You have at most {max_evaluations} EDA evaluations. "
                "Start by inspecting raw evidence with tools."
                + fixture_note
            ),
        }]
        if cc03t is not None and feedback_arm == "E1T":
            _append_trace_tail_message(
                messages, _prepare_trace_tail(
                    cc03t_state, max_evaluations=max_evaluations, label="decision-01"
                )
            )
        # Publish a resumable checkpoint before the first provider request.  A
        # network or provider failure must not force a new candidate lineage.
        dump_json(output / "SESSION.json", {
            "status": "running", "turn": 0, "evaluations": evaluations,
            "best_candidate": best_candidate, "best_post_synth": best_ppa,
            "usage": session_usage, "messages": messages,
            "finish_summary": finish_summary, "memory_mode": memory_mode,
            "knowledge_retrievals": knowledge_retrievals,
            "feedback_arm": feedback_arm, "seed": seed,
            "max_turns": max_turns, "max_evaluations": max_evaluations,
            "current_source": current_source,
            "current_parent_source": current_parent_source,
            "current_parent_id": current_parent_id,
            "evaluated_source_sha256s": sorted(evaluated_hashes),
            "inspection_only_turns": inspection_only_turns,
            "chipcontext_contexts": {
                key: value.to_dict() for key, value in runtime_contexts.items()
            },
            "chipcontext_costs": chipcontext_costs,
            "auto_stop_improvement_percent": auto_stop_improvement_percent,
            "cc03t": cc03t, "cc03t_state": cc03t_state,
            "cc03t_state_hash": content_hash(cc03t_state) if cc03t else None,
            "validation_timeline": (
                cc03t_state.get("validation_timeline", []) if cc03t else []
            ),
        })
    llm = DeepSeekOfficialToolLLM(
        system_message=_interactive_system_with_feedback(memory_mode, feedback_arm),
        model=config["model"]["id"], base_url=config["model"]["api_base"],
        max_tokens=int(config["model"]["max_output_tokens"]),
        timeout_seconds=int(config["model"]["timeout_seconds"]),
        attempts=int(config["model"]["attempt_limit"]),
        api_key_env=config["model"]["api_key_env"], reasoning_effort="high",
    )
    evaluator = BoomCandidateEvaluationNode()

    for turn in range(start_turn, max_turns + 1):
        turn_dir = output / "turns" / f"turn-{turn:02d}"
        if turn_dir.exists():
            if not _resume or (turn_dir / "assistant-message.json").is_file():
                raise RuntimeError(
                    "cannot safely replay a turn after an assistant response was "
                    "published; retain the artifacts and classify the run as blocked"
                )
            metadata_path = turn_dir / "provider-metadata.json"
            if metadata_path.is_file():
                retry_index = 1
                while (
                    turn_dir / f"provider-metadata-failed-{retry_index:02d}.json"
                ).exists():
                    retry_index += 1
                metadata_path.replace(
                    turn_dir / f"provider-metadata-failed-{retry_index:02d}.json"
                )
        else:
            turn_dir.mkdir(parents=True)
        dump_json(turn_dir / "messages-before.json", messages)
        model_messages = _messages_for_model(messages)
        dump_json(turn_dir / "model-messages-before.json", model_messages)
        visible_tool_specs = _available_tool_specs(tool_specs, inspection_only_turns)
        dump_json(turn_dir / "tool-specs.json", visible_tool_specs)
        if cc03t is not None:
            tail = None
            if feedback_arm == "E1T":
                for message in reversed(model_messages):
                    content = str(message.get("content", ""))
                    if content.startswith(TRACE_TAIL_PREFIX):
                        tail = json.loads(content[len(TRACE_TAIL_PREFIX):])
                        break
                if tail is None:
                    raise RuntimeError("E1T request is missing its deterministic TraceTail")
            manifest_row = build_visibility_manifest(
                request_id=f"turn-{turn:02d}",
                arm=feedback_arm,
                common_context=_common_messages(model_messages),
                tool_specs=visible_tool_specs,
                raw_permissions=sorted(
                    (item.get("function") or {}).get("name", "")
                    for item in visible_tool_specs
                ),
                trace_tail=tail,
            )
            cc03t_state["visibility_manifests"].append(manifest_row)
            dump_json(turn_dir / "VISIBILITY.json", manifest_row)
        model_started_ns = time.monotonic_ns()
        result = get(llm.chat_turn.chia_remote(
            llm, model_messages, visible_tool_specs,
            _chia_display_name=f"deepseek-interactive:{target_id}:turn-{turn:02d}",
        ))
        model_wall_ns = time.monotonic_ns() - model_started_ns
        metadata = json.loads(result.stream_result) if result.stream_result else {}
        metadata["client_wall_time_ns"] = model_wall_ns
        dump_json(turn_dir / "provider-metadata.json", metadata)
        if cc03t is not None:
            provider_seconds = metadata.get("elapsed_seconds")
            provider_ns = (
                int(float(provider_seconds) * 1_000_000_000)
                if isinstance(provider_seconds, (int, float)) else None
            )
            cc03t_state["validation_timeline"].append({
                "span_id": f"turn-{turn:02d}-model-api",
                "stage": "model_api",
                "wall_time_ns": model_wall_ns,
                "active_time_ns": provider_ns,
                "queue_time_ns": None,
                "parent_span_id": None,
                "candidate_id": None,
                "leaf": True,
                "unavailable_reason": (
                    None if provider_ns is not None else "provider_elapsed_not_reported"
                ),
                "queue_unavailable_reason": "ray_queue_not_separately_measured",
                "provenance": {"turn": turn},
            })
            wait_ns = max(0, model_wall_ns - provider_ns) if provider_ns is not None else None
            cc03t_state["validation_timeline"].append({
                "span_id": f"turn-{turn:02d}-controller-wait",
                "stage": "controller_wait",
                "wall_time_ns": wait_ns,
                "active_time_ns": 0 if wait_ns is not None else None,
                "queue_time_ns": None,
                "parent_span_id": None,
                "candidate_id": None,
                "leaf": True,
                "unavailable_reason": (
                    None if wait_ns is not None else "provider_elapsed_not_reported"
                ),
                "queue_unavailable_reason": "ray_queue_not_separately_measured",
                "provenance": {"turn": turn},
            })
            cc03t_state["validation_timeline"].append({
                "span_id": f"turn-{turn:02d}-scheduler-queue",
                "stage": "scheduler_queue",
                "wall_time_ns": None,
                "active_time_ns": None,
                "queue_time_ns": None,
                "parent_span_id": None,
                "candidate_id": None,
                "leaf": True,
                "unavailable_reason": "ray_queue_not_separately_measured",
                "queue_unavailable_reason": "ray_queue_not_separately_measured",
                "provenance": {"turn": turn},
            })
        if not result.success:
            raise RuntimeError(result.stderr or "DeepSeek interactive turn failed")
        assistant = json.loads(result.result)
        dump_json(turn_dir / "assistant-message.json", assistant)
        messages.append(assistant)
        if isinstance(metadata.get("usage"), dict):
            session_usage.append(metadata["usage"])
        tool_calls = assistant.get("tool_calls") or []
        if not tool_calls:
            messages.append({
                "role": "user",
                "content": "Continue by calling an available tool. Use finish only after measured evaluation evidence.",
            })
        turn_observed = False
        turn_progressed = False
        decision_completed = False
        for call in tool_calls:
            call_id = call.get("id")
            fn = (call.get("function") or {}).get("name")
            tool_started_ns = time.monotonic_ns()
            try:
                args = json.loads((call.get("function") or {}).get("arguments") or "{}")
                if fn in OBSERVATION_TOOLS:
                    turn_observed = True
                    if inspection_only_turns >= INSPECTION_HARD_LIMIT_TURNS:
                        raise RuntimeError(
                            "inspection-only turn budget exhausted; apply a concrete edit "
                            "or evaluate the current edited source"
                        )
                target_episode_retrieved = any(
                    row.get("operation") == "retrieve"
                    and row.get("knowledge_class") == "target-specific-solution"
                    for row in knowledge_retrievals
                )
                if (
                    memory_mode == "target"
                    and fn not in {"search_knowledge", "retrieve_knowledge"}
                    and not target_episode_retrieved
                ):
                    content = {
                        "status": "knowledge_prerequisite_pending",
                        "required_action": (
                            "Retrieve the most relevant target-specific episode first; "
                            "choose the sections needed for this analysis."
                        ),
                    }
                elif fn == "search_knowledge":
                    found = knowledge.search(str(args["query"]), int(args["max_results"]))
                    content = found.payload
                    knowledge_retrievals.append(found.audit | {
                        "turn": turn, "tool_call_id": call_id,
                    })
                elif fn == "retrieve_knowledge":
                    found = knowledge.retrieve(str(args["episode_id"]), list(args["sections"]))
                    content = found.payload
                    knowledge_retrievals.append(found.audit | {
                        "turn": turn, "tool_call_id": call_id,
                    })
                elif fn == "read_source":
                    lines = current_source.splitlines()
                    content: Any = _numbered(lines, int(args["start_line"]), int(args["end_line"]))
                elif fn == "search_source":
                    content = _literal_context(
                        current_source, str(args["query"]), 240,
                        int(args["context_lines"]),
                    )
                elif fn == "read_timing":
                    verify_frozen_run(output / "frozen", frozen_fingerprint)
                    content = _literal_context(timing, str(args["query"]), int(args["max_lines"]), 3)
                elif fn == "read_generated_rtl":
                    verify_frozen_run(output / "frozen", frozen_fingerprint)
                    name = str(args["file"])
                    if name not in set(target["rtl_files"]):
                        raise ValueError("RTL file is not allow-listed")
                    content = _literal_context(
                        (rtl_root / name).read_text(), str(args["query"]),
                        int(args["max_lines"]), 3,
                    )
                elif fn in {
                    "read_candidate_artifact", "query_candidate_status",
                    "list_candidate_artifacts", "query_candidate_failure",
                    "compare_candidate_metrics", "query_candidate_timing_paths",
                }:
                    candidate_id = str(args["candidate_id"])
                    context = runtime_contexts.get(candidate_id)
                    if context is None:
                        raise ValueError("candidate has no prepared ChipContext evidence")
                    if fn == "read_candidate_artifact":
                        queried = query_runtime_context(
                            context,
                            working_source=current_source,
                            operation="read_artifact",
                            parameters={
                                "artifact_ref": str(args["artifact_ref"]),
                                "start_line": int(args["start_line"]),
                                "line_count": int(args["line_count"]),
                                "limit_bytes": int(args["limit_bytes"]),
                            },
                        )
                    elif feedback_arm != "E1":
                        raise ValueError("structured evidence queries are unavailable in E0")
                    elif fn == "query_candidate_status":
                        queried = query_runtime_context(
                            context, working_source=current_source,
                            operation="candidate_status",
                        )
                    elif fn == "list_candidate_artifacts":
                        parameters: dict[str, Any] = {
                            "limit": int(args["limit"]),
                            "kinds": list(args["kinds"]),
                        }
                        if args["stage"]:
                            parameters["stage"] = str(args["stage"])
                        queried = query_runtime_context(
                            context, working_source=current_source,
                            operation="candidate_artifacts", parameters=parameters,
                        )
                    elif fn == "query_candidate_failure":
                        queried = query_runtime_context(
                            context, working_source=current_source,
                            operation="failure",
                            parameters={"check": str(args["check"])},
                        )
                    elif fn == "compare_candidate_metrics":
                        queried = query_runtime_context(
                            context, working_source=current_source,
                            operation="compare_metrics",
                            parameters={
                                "reference": "bound_baseline",
                                "stage": str(args["stage"]),
                                "metric_ids": list(args["metric_ids"]),
                            },
                        )
                    else:
                        parameters = {
                            "extraction_ref": str(args["extraction_ref"]),
                            "stage": str(args["stage"]),
                            "limit": int(args["limit"]),
                        }
                        for name in ("path_group", "source", "destination"):
                            if args[name]:
                                parameters[name] = str(args[name])
                        queried = query_runtime_context(
                            context, working_source=current_source,
                            operation="timing_paths", parameters=parameters,
                        )
                    chipcontext_costs.append({
                        "candidate_id": candidate_id,
                        "kind": "agent_query",
                        "operation": fn,
                        "cost": queried["cost"],
                    })
                    content = queried
                elif fn == "apply_exact_edits":
                    turn_progressed = True
                    edit_started_ns = time.monotonic_ns()
                    current_source = apply_exact_edits(current_source, args["edits"])
                    diff = make_diff(baseline_source, current_source, target["mutable_file"])
                    content = {
                        "status": "applied", "source_sha256": sha256_text(current_source),
                        "diff_sha256": sha256_text(diff), "changed_lines": len(diff.splitlines()),
                    }
                    if cc03t is not None:
                        elapsed_ns = time.monotonic_ns() - edit_started_ns
                        cc03t_state["validation_timeline"].append({
                            "span_id": f"turn-{turn:02d}-{call_id}-edit",
                            "stage": "edit", "wall_time_ns": elapsed_ns,
                            "active_time_ns": elapsed_ns, "queue_time_ns": None,
                            "parent_span_id": None, "candidate_id": None,
                            "leaf": True, "unavailable_reason": None,
                            "provenance": {"turn": turn, "tool_call_id": call_id},
                        })
                elif fn == "revert_source":
                    turn_progressed = True
                    if cc03t is not None:
                        selected_parent_id = str(cc03t_state["selected_parent_id"])
                        current_source = str(
                            cc03t_state["source_by_candidate"][selected_parent_id]
                        )
                        current_parent_source = current_source
                        current_parent_id = selected_parent_id
                        content = {
                            "status": "reverted",
                            "target": "selected_parent",
                            "selected_parent_id": selected_parent_id,
                            "source_sha256": sha256_text(current_source),
                        }
                    elif args["target"] == "best":
                        if best_source is None:
                            raise ValueError("no measured valid best candidate exists")
                        current_source = best_source
                        current_parent_source = best_source
                        current_parent_id = best_candidate["id"]
                    else:
                        current_source = baseline_source
                        current_parent_source = baseline_source
                        current_parent_id = None
                    if cc03t is None:
                        content = {"status": "reverted", "target": args["target"], "source_sha256": sha256_text(current_source)}
                elif fn == "evaluate_candidate":
                    turn_progressed = True
                    verify_frozen_run(output / "frozen", frozen_fingerprint)
                    if evaluations >= max_evaluations:
                        raise ValueError("EDA evaluation budget exhausted")
                    source_hash = sha256_text(current_source)
                    if current_source == baseline_source:
                        raise ValueError("current source is unchanged from baseline")
                    if source_hash in evaluated_hashes:
                        raise ValueError("this complete source was already evaluated")
                    evaluations += 1
                    evaluated_hashes.add(source_hash)
                    lineage = lineage_fields(
                        parent_id=current_parent_id,
                        parent_source=current_parent_source,
                        baseline_source=baseline_source,
                        candidate_source=current_source,
                        mutable_file=target["mutable_file"],
                    )
                    candidate = CandidateArtifact(
                        campaign_id=output.name, target=target_id, arm=feedback_arm, seed=seed,
                        index=evaluations, parent_id=lineage["parent_id"],
                        source=current_source, diff=lineage["diff"],
                        baseline_diff=lineage["baseline_diff"],
                        parent_source_sha256=lineage["parent_source_sha256"],
                        diagnosis=[{
                            "evidence": "interactive DS tool trajectory",
                            "source_region": target["mutable_file"],
                            "hypothesis": "agent-selected transformation",
                            "predicted_effect": "lower measured critical delay",
                            "risk": "checked by differential verification",
                        }], selected_hypothesis=0,
                        visible_feedback="interactive tool transcript",
                        request_sha256=sha256_text(
                            json.dumps(model_messages, sort_keys=True)
                        ),
                        response_sha256=sha256_text(json.dumps(assistant, sort_keys=True)),
                        usage=metadata.get("usage"), attempts=metadata.get("attempts", []),
                        model_elapsed_seconds=float(metadata.get("elapsed_seconds", 0.0)),
                        provider_model=metadata.get("provider_model"),
                    )
                    candidate_dir = output / "evaluations" / f"candidate-{evaluations:02d}"
                    dump_json(candidate_dir / "candidate.json", candidate)
                    evaluation, attempts_used, attempt_records = _evaluate_with_infra_retries(
                        node=evaluator, candidate=candidate, target=target, config=config,
                        baseline=baseline, candidate_dir=candidate_dir,
                    )
                    if cc03t is not None:
                        parent_eval_ref = cc03t_state["trace_candidates"][
                            str(current_parent_id)
                        ].get("evaluation_ref")
                        parent_eval_raw = cc03t_state["trace_evaluations"].get(
                            parent_eval_ref, {}
                        )
                        parent_for_validity = EvaluationArtifact(
                            candidate_id=str(current_parent_id),
                            candidate_valid=bool(parent_eval_raw.get("candidate_valid")),
                            post_synth=(
                                {
                                    "critical_delay_ns": parent_eval_raw["ppa"]["critical_delay_ns"],
                                    "slice_luts": parent_eval_raw["ppa"]["slice_luts"],
                                }
                                if parent_eval_raw.get("ppa") else None
                            ),
                        )
                        update_validity(evaluation, baseline, parent=parent_for_validity)
                        dump_json(candidate_dir / "evaluation.json", evaluation)
                    row = {
                        "candidate": candidate.to_dict(),
                        "search_evaluation": evaluation.to_dict(),
                        "evaluation_attempts": attempt_records,
                        "search_active_seconds_total": sum(
                            float(item.get("active_seconds", 0.0))
                            for item in attempt_records
                        ),
                        "infrastructure_attempts": attempts_used,
                    }
                    dump_json(candidate_dir / "result.json", row)
                    runtime_context = prepare_runtime_context(
                        campaign_root=output,
                        candidate_dir=candidate_dir,
                        candidate=candidate,
                        evaluation=evaluation,
                        target_id=target_id,
                        infrastructure_attempts=attempts_used,
                    )
                    runtime_contexts[candidate.id] = runtime_context
                    inventory = query_runtime_context(
                        runtime_context,
                        working_source=current_source,
                        operation="candidate_artifacts",
                        parameters={"limit": 100},
                    )
                    chipcontext_costs.append({
                        "candidate_id": candidate.id,
                        "kind": "automatic_raw_inventory",
                        "operation": "candidate_artifacts",
                        "cost": inventory["cost"],
                    })
                    ppa = evaluation.post_synth or {}
                    if evaluation.candidate_valid and isinstance(ppa.get("critical_delay_ns"), (int, float)):
                        baseline_score = (
                            float(baseline["critical_delay_ns"]),
                            float(baseline["slice_luts"]),
                        )
                        candidate_score = (
                            float(ppa["critical_delay_ns"]),
                            float(ppa.get("slice_luts", 10**18)),
                        )
                        incumbent_score = (
                            (float(best_ppa["critical_delay_ns"]), float(best_ppa.get("slice_luts", 10**18)))
                            if best_ppa is not None else baseline_score
                        )
                        if candidate_score < incumbent_score:
                            best_source, best_ppa, best_candidate = current_source, ppa, candidate.to_dict()
                    content = _compact_evaluation(evaluation.to_dict())
                    content["evaluations_remaining"] = max_evaluations - evaluations
                    content["baseline_post_synth"] = {
                        "critical_delay_ns": baseline["critical_delay_ns"],
                        "slice_luts": baseline["slice_luts"],
                    }
                    content["best_post_synth"] = best_ppa
                    content["raw_evidence"] = {
                        "candidate_id": candidate.id,
                        "attempt_id": runtime_context.attempt_id,
                        "snapshot_ref": runtime_context.snapshot_ref,
                        "artifacts": inventory["answer"]["result"]["artifacts"],
                        "read_tool": "read_candidate_artifact",
                    }
                    if cc03t is not None:
                        best_before_id = str(cc03t_state["current_best_candidate_id"])
                        parent_id = str(current_parent_id)
                        parent_eval = cc03t_state["trace_evaluations"].get(
                            cc03t_state["trace_candidates"][parent_id].get("evaluation_ref"), {}
                        )
                        best_before_eval = cc03t_state["trace_evaluations"].get(
                            cc03t_state["trace_candidates"][best_before_id].get("evaluation_ref"), {}
                        )
                        raw_refs = [
                            str(item.get("artifact_ref"))
                            for item in inventory["answer"]["result"]["artifacts"]
                            if item.get("artifact_ref")
                        ]
                        transition = _trace_transition_from_state(
                            index=evaluations, candidate_id=candidate.id,
                            parent_id=parent_id, best_before_id=best_before_id,
                            evaluation=evaluation.to_dict(),
                            parent_ppa=parent_eval.get("ppa"),
                            best_before_ppa=best_before_eval.get("ppa"),
                            raw_evidence_refs=raw_refs,
                            tool_contract_hash=ppa_contract_hash,
                            run_id=output.name,
                            branch_id=feedback_arm,
                            source_before_sha256=sha256_text(current_parent_source),
                            source_after_sha256=sha256_text(current_source),
                            patch_ref=sha256_text(candidate.diff),
                        )
                        evaluation_ref = f"evaluation-{candidate.id}"
                        cc03t_state["trace_candidates"][candidate.id] = {
                            "candidate_id": candidate.id,
                            "parent_candidate_id": parent_id,
                            "source_sha256": sha256_text(current_source),
                            "patch_sha256": sha256_text(candidate.diff),
                            "evaluation_ref": evaluation_ref,
                            "generated_rtl_ref": None,
                            "generated_rtl_sha256": None,
                            "provenance": {"candidate_dir": f"evaluations/candidate-{evaluations:02d}"},
                        }
                        cc03t_state["trace_evaluations"][evaluation_ref] = {
                            "evaluation_id": evaluation_ref,
                            "candidate_id": candidate.id,
                            "status": evaluation.status,
                            "stage": evaluation.stage,
                            "correctness_ok": evaluation.correctness_ok,
                            "candidate_valid": evaluation.candidate_valid,
                            "promotable": evaluation.promotable,
                            "ppa": _trace_ppa(
                                evaluation.post_synth,
                                tool_contract_hash=ppa_contract_hash,
                            ),
                            "raw_refs": raw_refs,
                            "provenance": {"candidate_dir": f"evaluations/candidate-{evaluations:02d}"},
                        }
                        cc03t_state["trace_transitions"][transition.transition_id] = transition.to_dict()
                        cc03t_state["last_transition_id"] = transition.transition_id
                        cc03t_state["source_by_candidate"][candidate.id] = current_source
                        cc03t_state["evaluations"] = evaluations
                        if transition.became_new_best:
                            cc03t_state["current_best_candidate_id"] = candidate.id
                        eval_timeline = timeline_from_evaluation(
                            output.name, candidate.id, evaluation.to_dict()
                        )
                        cc03t_state["validation_timeline"].extend(
                            [span.to_dict() for span in eval_timeline.spans]
                        )
                        for attempt_row in attempt_records[:-1]:
                            seconds = attempt_row.get("active_seconds")
                            active_ns = (
                                int(float(seconds) * 1_000_000_000)
                                if isinstance(seconds, (int, float)) else None
                            )
                            cc03t_state["validation_timeline"].append({
                                "span_id": (
                                    f"{candidate.id}-infrastructure-attempt-"
                                    f"{int(attempt_row.get('attempt', 0)):02d}"
                                ),
                                "stage": "infrastructure_retry",
                                "wall_time_ns": active_ns,
                                "active_time_ns": active_ns,
                                "queue_time_ns": None,
                                "parent_span_id": None,
                                "candidate_id": candidate.id,
                                "leaf": True,
                                "unavailable_reason": (
                                    None if active_ns is not None else "not_measured"
                                ),
                                "queue_unavailable_reason": "ray_queue_not_separately_measured",
                                "provenance": {
                                    "attempt": attempt_row.get("attempt"),
                                    "status": attempt_row.get("status"),
                                },
                            })
                        decision_completed = True
                        if bool(cc03t.get("stop_after_first_evaluation")):
                            finished = True
                            finish_summary = "CC-03T frozen DecisionPoint completed one evaluation"
                        elif evaluations < max_evaluations:
                            next_selection = select_parent(
                                evaluations + 1,
                                current_best_candidate_id=str(
                                    cc03t_state["current_best_candidate_id"]
                                ),
                                decision_point_id=f"decision-{evaluations + 1:02d}",
                            )
                            cc03t_state["parent_selections"].append(next_selection.to_dict())
                            next_parent_id = next_selection.selected_parent_id
                            cc03t_state["selected_parent_id"] = next_parent_id
                            current_source = str(
                                cc03t_state["source_by_candidate"][next_parent_id]
                            )
                            current_parent_source = current_source
                            current_parent_id = next_parent_id
                            content["next_parent_selection"] = next_selection.to_dict()
                            content["working_source_sha256"] = sha256_text(current_source)
                        else:
                            finished = True
                            finish_summary = "CC-03T evaluation budget exhausted"
                    else:
                        current_parent_source = current_source
                        current_parent_id = candidate.id
                    if feedback_arm == "E1":
                        structured = build_structured_feedback(
                            runtime_context, working_source=current_source
                        )
                        content["structured_feedback"] = {
                            key: value for key, value in structured.items()
                            if key != "cost"
                        }
                        chipcontext_costs.append({
                            "candidate_id": candidate.id,
                            "kind": "automatic_structured_feedback",
                            "content_hash": structured["content_hash"],
                            "cost": structured["cost"],
                        })
                    if evaluation.candidate_valid and _meets_auto_stop(
                        float(baseline["critical_delay_ns"]), ppa,
                        auto_stop_improvement_percent,
                    ):
                        finished = True
                        improvement = (
                            float(baseline["critical_delay_ns"]) - float(ppa["critical_delay_ns"])
                        ) / float(baseline["critical_delay_ns"]) * 100.0
                        finish_summary = (
                            "automatic stop after a valid candidate reached "
                            f"{improvement:.2f}% post-synthesis delay improvement"
                        )
                        content["auto_stop"] = {
                            "threshold_percent": auto_stop_improvement_percent,
                            "measured_improvement_percent": improvement,
                        }
                elif fn == "finish":
                    turn_progressed = True
                    if best_candidate is None:
                        raise ValueError("no measured valid candidate exists; inspect failures and continue")
                    finished = True
                    finish_summary = str(args["summary"])
                    content = {"status": "accepted", "best_candidate_id": best_candidate["id"], "best_post_synth": best_ppa}
                else:
                    raise ValueError(f"unknown tool {fn!r}")
                response_text = json.dumps(content, sort_keys=True) if not isinstance(content, str) else content
            except Exception as exc:
                response_text = json.dumps({"status": "tool_error", "error": f"{type(exc).__name__}: {exc}"})
            if cc03t is not None and fn in OBSERVATION_TOOLS:
                tool_elapsed_ns = time.monotonic_ns() - tool_started_ns
                cc03t_state["validation_timeline"].append({
                    "span_id": f"turn-{turn:02d}-{call_id}-tool-inspection",
                    "stage": "tool_inspection",
                    "wall_time_ns": tool_elapsed_ns,
                    "active_time_ns": tool_elapsed_ns,
                    "queue_time_ns": None,
                    "parent_span_id": None,
                    "candidate_id": cc03t_state.get("selected_parent_id"),
                    "leaf": True,
                    "unavailable_reason": None,
                    "queue_unavailable_reason": "not_applicable",
                    "provenance": {"turn": turn, "tool_call_id": call_id, "tool": fn},
                })
            (turn_dir / f"tool-{call_id}.txt").write_text(response_text)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": response_text})
            if decision_completed:
                if cc03t is not None and feedback_arm == "E1T" and not finished:
                    _append_trace_tail_message(
                        messages,
                        _prepare_trace_tail(
                            cc03t_state,
                            max_evaluations=max_evaluations,
                            label=f"decision-{evaluations + 1:02d}",
                        ),
                    )
                break
            if finished:
                break
        inspection_only_turns, inspection_notice = _advance_inspection_budget(
            inspection_only_turns,
            observed=turn_observed,
            progressed=turn_progressed,
        )
        if inspection_notice:
            messages.append({"role": "user", "content": inspection_notice})
        dump_json(output / "KNOWLEDGE_RETRIEVALS.json", {
            "memory_mode": memory_mode,
            "retrievals": knowledge_retrievals,
            "total_response_chars": sum(int(row["response_chars"]) for row in knowledge_retrievals),
        })
        dump_json(output / "SESSION.json", {
            "status": "finished" if finished else "running", "turn": turn,
            "evaluations": evaluations, "best_candidate": best_candidate,
            "best_post_synth": best_ppa, "usage": session_usage,
            "messages": messages, "finish_summary": finish_summary,
            "memory_mode": memory_mode, "knowledge_retrievals": knowledge_retrievals,
            "feedback_arm": feedback_arm,
            "seed": seed,
            "max_turns": max_turns,
            "max_evaluations": max_evaluations,
            "current_source": current_source,
            "current_parent_source": current_parent_source,
            "current_parent_id": current_parent_id,
            "evaluated_source_sha256s": sorted(evaluated_hashes),
            "inspection_only_turns": inspection_only_turns,
            "chipcontext_contexts": {
                key: value.to_dict() for key, value in runtime_contexts.items()
            },
            "chipcontext_costs": chipcontext_costs,
            "auto_stop_improvement_percent": auto_stop_improvement_percent,
            "cc03t": cc03t,
            "cc03t_state": cc03t_state,
            "cc03t_state_hash": content_hash(cc03t_state) if cc03t else None,
            "validation_timeline": cc03t_state.get("validation_timeline", []) if cc03t else [],
        })
        if finished:
            break

    result = {
        "status": "finished" if finished else "turn_limit_exhausted",
        "evaluations": evaluations, "best_candidate": best_candidate,
        "best_post_synth": best_ppa, "baseline_post_synth": {
            "critical_delay_ns": baseline["critical_delay_ns"],
            "slice_luts": baseline["slice_luts"],
        }, "usage": session_usage, "finish_summary": finish_summary,
        "memory_mode": memory_mode,
        "feedback_arm": feedback_arm,
        "auto_stop_improvement_percent": auto_stop_improvement_percent,
        "knowledge_retrievals": knowledge_retrievals,
        "knowledge_response_chars": sum(int(row["response_chars"]) for row in knowledge_retrievals),
        "chipcontext_contexts": {
            key: value.to_dict() for key, value in runtime_contexts.items()
        },
        "chipcontext_costs": chipcontext_costs,
        "cc03t": cc03t,
        "cc03t_state": cc03t_state,
        "cc03t_state_hash": content_hash(cc03t_state) if cc03t else None,
        "validation_timeline": cc03t_state.get("validation_timeline", []) if cc03t else [],
    }
    dump_json(output / "RESULT.json", result)
    if cc03t is not None:
        verified_ids = []
        invalid_ids = []
        for candidate_id, candidate_row in cc03t_state["trace_candidates"].items():
            evaluation_row = cc03t_state["trace_evaluations"].get(
                candidate_row.get("evaluation_ref"), {}
            )
            (verified_ids if evaluation_row.get("candidate_valid") else invalid_ids).append(
                candidate_id
            )
        current_candidate_id = (
            list(cc03t_state["trace_candidates"])[-1]
            if cc03t_state["trace_candidates"] else BASELINE_CANDIDATE_ID
        )
        pool = CandidatePool(
            baseline_candidate_id=BASELINE_CANDIDATE_ID,
            current_candidate_id=current_candidate_id,
            current_best_candidate_id=cc03t_state["current_best_candidate_id"],
            eligible_parent_ids=tuple(sorted({
                BASELINE_CANDIDATE_ID,
                str(cc03t_state["current_best_candidate_id"]),
            })),
            verified_candidate_ids=tuple(sorted(verified_ids)),
            invalid_candidate_ids=tuple(sorted(invalid_ids)),
            remaining_evaluation_budget=max(0, max_evaluations - evaluations),
        )
        dump_json(output / "CANDIDATE_DAG.json", signed_record({
            "schema_version": "chia-boom.candidate-dag.v1",
            "baseline_candidate_id": BASELINE_CANDIDATE_ID,
            "current_candidate_id": current_candidate_id,
            "current_best_candidate_id": cc03t_state["current_best_candidate_id"],
            "candidates": list(cc03t_state["trace_candidates"].values()),
            "candidate_pool": pool.to_dict(),
            "parent_selections": cc03t_state["parent_selections"],
            "unexecuted_policy_slots": [
                {
                    "evaluation_slot": slot,
                    "status": "not_executed",
                    "reason": (
                        "agent_finished" if result["status"] == "finished"
                        else "turn_limit_exhausted"
                    ),
                }
                for slot in range(evaluations + 1, max_evaluations + 1)
            ],
        }))
        dump_json(output / "VISIBILITY_MANIFEST.json", signed_record({
            "schema_version": "chia-boom.visibility-manifest-set.v1",
            "rows": cc03t_state["visibility_manifests"],
        }))
        dump_json(output / "VALIDATION_TIMELINE.json", signed_record({
            "schema_version": "chia-boom.validation-timeline.v1",
            "run_id": output.name,
            "spans": cc03t_state["validation_timeline"],
        }))
    return result


def resume_interactive_issueq(
    config: dict[str, Any], output: Path,
) -> dict[str, Any]:
    """Continue a saved interactive session without changing its experiment contract."""

    saved = load_json(output / "SESSION.json")
    required = {
        "seed", "max_turns", "max_evaluations", "memory_mode", "feedback_arm",
        "current_source", "current_parent_source", "evaluated_source_sha256s",
        "inspection_only_turns",
    }
    missing = sorted(required - set(saved))
    if missing:
        raise RuntimeError(
            "interactive session predates resumable CC-03 state: " + ", ".join(missing)
        )
    return run_interactive_issueq(
        config,
        output,
        seed=int(saved["seed"]),
        max_turns=int(saved["max_turns"]),
        max_evaluations=int(saved["max_evaluations"]),
        memory_mode=str(saved["memory_mode"]),
        feedback_arm=str(saved["feedback_arm"]),
        auto_stop_improvement_percent=saved.get("auto_stop_improvement_percent"),
        cc03t=saved.get("cc03t"),
        _resume=True,
    )


def finalize_interactive_issueq(config: dict[str, Any], output: Path) -> dict[str, Any]:
    """Independently rebuild and finalize the measured best interactive candidate."""
    config = _load_interactive_snapshot(config, output)
    result = load_json(output / "RESULT.json")
    best = result.get("best_candidate")
    if not best:
        raise RuntimeError("interactive campaign has no measured valid candidate")
    candidate = CandidateArtifact.from_dict(best)
    candidate_dir = output / "evaluations" / f"candidate-{candidate.index:02d}"
    result_row = load_json(candidate_dir / "result.json")
    early = EvaluationArtifact.from_dict(
        result_row.get("search_evaluation") or result_row["evaluation"]
    )
    target = config["targets"][candidate.target]
    baseline = load_json(
        baseline_input(config, candidate.target, "baseline-ppa.json")
    )
    baseline["maximum_lut_ratio"] = config["physical"]["maximum_lut_ratio"]
    final_dir = output / "finalization"
    node = BoomFinalizationNode()
    final, finalization_meta = finalize_candidate(
        node=node,
        candidate=candidate,
        early=early,
        target=target,
        config=config,
        baseline=baseline,
        root=final_dir,
        display_name=f"finalize-interactive:{candidate.id}",
    )
    payload = {
        "candidate_id": candidate.id,
        "status": "complete" if final.final_valid else final.status,
        "final_valid": final.final_valid,
        "valid_improvement": final.valid_improvement,
        "evaluation": final.to_dict(),
        "metadata": finalization_meta,
    }
    dump_json(output / "FINAL_RESULT.json", payload)
    return payload
