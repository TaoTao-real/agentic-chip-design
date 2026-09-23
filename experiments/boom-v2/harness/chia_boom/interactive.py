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
from .core import apply_exact_edits, canonical_hash, lineage_fields, make_diff
from .deepseek import DeepSeekOfficialToolLLM
from .finalize import BoomFinalizationNode, finalize_candidate
from .frozen import qualification_request, qualified_artifact_hashes
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
    memory_mode: str = "none", auto_stop_improvement_percent: float | None = None,
) -> dict[str, Any]:
    if memory_mode not in MEMORY_MODES:
        raise ValueError(f"unknown memory mode: {memory_mode}")
    output.mkdir(parents=True, exist_ok=True)
    if (output / "SESSION.json").exists() or (output / "RESULT.json").exists():
        raise FileExistsError(f"interactive campaign already contains state: {output}")
    if len(config["targets"]) != 1:
        raise ValueError("interactive mode requires exactly one configured target")
    target_id = next(iter(config["targets"]))
    target = config["targets"][target_id]
    frozen = Path(config["remote"]["frozen_inputs_root"])
    rtl_root = Path(config["remote"]["baseline_rtl_root"]) / target_id
    baseline_source = (frozen / target_id / "baseline-source.scala").read_text()
    timing = (frozen / target_id / "baseline-timing.txt").read_text()
    baseline = load_json(frozen / target_id / "baseline-ppa.json")
    baseline["maximum_lut_ratio"] = config["physical"]["maximum_lut_ratio"]
    reference = {
        "qualification_fingerprint": qualification_request(config)["fingerprint"],
        "qualified_artifact_hashes": qualified_artifact_hashes(config),
    }
    reference["fingerprint"] = canonical_hash(reference)
    dump_json(output / "INTERACTIVE_REFERENCE.json", reference)
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
    knowledge_retrievals: list[dict[str, Any]] = []
    tool_specs = (MEMORY_TOOL_SPECS if knowledge.enabled else []) + TOOL_SPECS
    dump_json(output / "KNOWLEDGE_MANIFEST.json", knowledge.manifest())
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": (
            "Optimize target " + target_id + " in file " + target["mutable_file"] + ".\n"
            "Baseline post-synthesis critical delay is " + str(baseline["critical_delay_ns"]) +
            " ns and Slice LUTs are " + str(baseline["slice_luts"]) + ".\n"
            "You have at most five EDA evaluations. Start by inspecting raw evidence with tools."
        ),
    }]
    llm = DeepSeekOfficialToolLLM(
        system_message=_interactive_system(memory_mode),
        model=config["model"]["id"], base_url=config["model"]["api_base"],
        max_tokens=int(config["model"]["max_output_tokens"]),
        timeout_seconds=int(config["model"]["timeout_seconds"]),
        attempts=int(config["model"]["attempt_limit"]),
        api_key_env=config["model"]["api_key_env"], reasoning_effort="high",
    )
    evaluator = BoomCandidateEvaluationNode()

    for turn in range(1, max_turns + 1):
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
                    content = _literal_context(timing, str(args["query"]), int(args["max_lines"]), 3)
                elif fn == "read_generated_rtl":
                    name = str(args["file"])
                    if name not in set(target["rtl_files"]):
                        raise ValueError("RTL file is not allow-listed")
                    content = _literal_context(
                        (rtl_root / name).read_text(), str(args["query"]),
                        int(args["max_lines"]), 3,
                    )
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
                        campaign_id=output.name, target=target_id, arm="I", seed=seed,
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
        "auto_stop_improvement_percent": auto_stop_improvement_percent,
        "knowledge_retrievals": knowledge_retrievals,
        "knowledge_response_chars": sum(int(row["response_chars"]) for row in knowledge_retrievals),
    }
    dump_json(output / "RESULT.json", result)
    return result


def finalize_interactive_issueq(config: dict[str, Any], output: Path) -> dict[str, Any]:
    """Independently rebuild and finalize the measured best interactive candidate."""
    result = load_json(output / "RESULT.json")
    best = result.get("best_candidate")
    if not best:
        raise RuntimeError("interactive campaign has no measured valid candidate")
    candidate = CandidateArtifact.from_dict(best)
    saved_reference = load_json(output / "INTERACTIVE_REFERENCE.json")
    current_reference = {
        "qualification_fingerprint": qualification_request(config)["fingerprint"],
        "qualified_artifact_hashes": qualified_artifact_hashes(config),
    }
    current_reference["fingerprint"] = canonical_hash(current_reference)
    if current_reference != saved_reference:
        raise RuntimeError("interactive source, golden, test, or tool reference drift")
    candidate_dir = output / "evaluations" / f"candidate-{candidate.index:02d}"
    result_row = load_json(candidate_dir / "result.json")
    early = EvaluationArtifact.from_dict(
        result_row.get("search_evaluation") or result_row["evaluation"]
    )
    target = config["targets"][candidate.target]
    frozen = Path(config["remote"]["frozen_inputs_root"])
    baseline = load_json(frozen / candidate.target / "baseline-ppa.json")
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
