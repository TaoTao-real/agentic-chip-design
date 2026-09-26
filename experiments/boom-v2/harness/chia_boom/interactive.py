from __future__ import annotations

import json
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
from .core import apply_exact_edits, canonical_hash, lineage_fields, make_diff
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


INTERACTIVE_SYSTEM = """You are an autonomous hardware optimization agent.
Work only through the supplied tools. Optimize the specified synthesizable
BOOM module for lower post-synthesis critical delay while preserving exact
cycle-visible behavior, interfaces, dimensions, and configuration. Slice LUTs
must remain within 105% of baseline. Discover bottlenecks yourself from source,
generated RTL, raw timing evidence, and measured candidate results. No human
diagnosis or historical solution is available. Never ask for a suggested
optimization. Inspect, form hypotheses, edit, evaluate, repair, and finish with
the best measured candidate. Do not claim improvement without tool evidence."""


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
    if feedback_arm != "E0":
        raise ValueError(f"unknown feedback arm: {feedback_arm}")
    return value


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
    if feedback_arm not in FEEDBACK_ARMS:
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


def run_interactive_issueq(
    config: dict[str, Any], output: Path, *, seed: int = 41,
    max_turns: int = 24, max_evaluations: int = 5,
    memory_mode: str = "none", feedback_arm: str = "E0",
    auto_stop_improvement_percent: float | None = None,
    _resume: bool = False,
) -> dict[str, Any]:
    if memory_mode not in MEMORY_MODES:
        raise ValueError(f"unknown memory mode: {memory_mode}")
    if feedback_arm not in FEEDBACK_ARMS:
        raise ValueError(f"unknown feedback arm: {feedback_arm}")
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
    saved = load_json(session_path) if _resume else None
    if saved is not None:
        frozen_fields = {
            "seed": seed,
            "max_turns": max_turns,
            "max_evaluations": max_evaluations,
            "memory_mode": memory_mode,
            "feedback_arm": feedback_arm,
            "auto_stop_improvement_percent": auto_stop_improvement_percent,
        }
        for name, expected in frozen_fields.items():
            if saved.get(name) != expected:
                raise RuntimeError(f"interactive resume changed frozen field: {name}")
        if saved.get("status") == "finished":
            raise RuntimeError("interactive campaign is already finished")
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
        start_turn = int(saved["turn"]) + 1
    else:
        current_source = baseline_source
        current_parent_source = baseline_source
        current_parent_id: str | None = None
        best_source: str | None = None
        best_ppa: dict[str, Any] | None = None
        best_candidate: dict[str, Any] | None = None
        evaluated_hashes: set[str] = set()
        evaluations = 0
        finished = False
        finish_summary = ""
        session_usage: list[dict[str, Any]] = []
        start_turn = 1
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
        for key, value in (saved.get("chipcontext_contexts", {}) if saved else {}).items()
    }
    chipcontext_costs: list[dict[str, Any]] = (
        list(saved.get("chipcontext_costs", [])) if saved else []
    )
    dump_json(output / "KNOWLEDGE_MANIFEST.json", knowledge.manifest())
    if saved is None:
        messages = [{
            "role": "user",
            "content": (
                "Optimize target " + target_id + " in file " + target["mutable_file"] + ".\n"
                "Baseline post-synthesis critical delay is " + str(baseline["critical_delay_ns"]) +
                " ns and Slice LUTs are " + str(baseline["slice_luts"]) + ".\n"
                f"You have at most {max_evaluations} EDA evaluations. "
                "Start by inspecting raw evidence with tools."
            ),
        }]
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
        turn_dir.mkdir(parents=True)
        dump_json(turn_dir / "messages-before.json", messages)
        result = get(llm.chat_turn.chia_remote(
            llm, messages, tool_specs,
            _chia_display_name=f"deepseek-interactive:{target_id}:turn-{turn:02d}",
        ))
        metadata = json.loads(result.stream_result) if result.stream_result else {}
        dump_json(turn_dir / "provider-metadata.json", metadata)
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
            continue
        for call in tool_calls:
            call_id = call.get("id")
            fn = (call.get("function") or {}).get("name")
            try:
                args = json.loads((call.get("function") or {}).get("arguments") or "{}")
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
                    current_source = apply_exact_edits(current_source, args["edits"])
                    diff = make_diff(baseline_source, current_source, target["mutable_file"])
                    content = {
                        "status": "applied", "source_sha256": sha256_text(current_source),
                        "diff_sha256": sha256_text(diff), "changed_lines": len(diff.splitlines()),
                    }
                elif fn == "revert_source":
                    if args["target"] == "best":
                        if best_source is None:
                            raise ValueError("no measured valid best candidate exists")
                        current_source = best_source
                        current_parent_source = best_source
                        current_parent_id = best_candidate["id"]
                    else:
                        current_source = baseline_source
                        current_parent_source = baseline_source
                        current_parent_id = None
                    content = {"status": "reverted", "target": args["target"], "source_sha256": sha256_text(current_source)}
                elif fn == "evaluate_candidate":
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
                        request_sha256=sha256_text(json.dumps(messages, sort_keys=True)),
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
                        if best_ppa is None or (ppa["critical_delay_ns"], ppa.get("slice_luts", 10**18)) < (best_ppa["critical_delay_ns"], best_ppa.get("slice_luts", 10**18)):
                            best_source, best_ppa, best_candidate = current_source, ppa, candidate.to_dict()
                    current_parent_source = current_source
                    current_parent_id = candidate.id
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
            (turn_dir / f"tool-{call_id}.txt").write_text(response_text)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": response_text})
            if finished:
                break
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
            "chipcontext_contexts": {
                key: value.to_dict() for key, value in runtime_contexts.items()
            },
            "chipcontext_costs": chipcontext_costs,
            "auto_stop_improvement_percent": auto_stop_improvement_percent,
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
    }
    dump_json(output / "RESULT.json", result)
    return result


def resume_interactive_issueq(
    config: dict[str, Any], output: Path,
) -> dict[str, Any]:
    """Continue a saved interactive session without changing its experiment contract."""

    saved = load_json(output / "SESSION.json")
    required = {
        "seed", "max_turns", "max_evaluations", "memory_mode", "feedback_arm",
        "current_source", "current_parent_source", "evaluated_source_sha256s",
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
