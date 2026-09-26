from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


AUDIT_REVISION = "chipcontext-information-sufficiency-v1"
ANALYSIS_POLICY = {
    "usage_class": "analysis_only",
    "eligible_for_agent_context": False,
    "eligible_for_knowledge_store": False,
}
STATE_CHANGING_TOOLS = {
    "apply_exact_edits", "evaluate_candidate", "revert_source", "finish",
}
AVAILABILITY_ORDER = ("pushed", "structured_pull", "raw_pull", "unavailable")
EXPOSURE_ORDER = (
    "inline_current_request", "visible_recent_tool_result",
    "archived_hash_only", "not_exposed",
)

EVIDENCE_FAMILIES: dict[str, dict[str, Any]] = {
    "E1": {
        "name": "design_and_source_change",
        "components": ["working_source", "candidate_diff", "parent_diff"],
    },
    "E2": {
        "name": "generated_rtl_and_structural_implementation",
        "components": ["baseline_generated_rtl", "current_candidate_generated_rtl"],
    },
    "E3": {
        "name": "correctness_and_failure",
        "components": ["build", "interface", "differential", "failure"],
    },
    "E4": {
        "name": "scalar_ppa",
        "components": [
            "critical_delay_ns", "slice_luts", "slice_registers", "wns_ns",
            "tns_ns", "failing_endpoints", "total_endpoints",
        ],
    },
    "E5": {
        "name": "timing_topology_and_shape",
        "components": [
            "source", "destination", "path_group", "logic_levels",
            "data_path_delay_ns", "coverage",
        ],
    },
    "E6": {
        "name": "candidate_transition",
        "components": ["baseline", "parent", "current", "best"],
    },
    "E7": {
        "name": "critical_path_movement",
        "components": ["signature_change", "endpoint_movement", "path_group_movement"],
    },
    "E8": {
        "name": "current_branch_search_history",
        "components": ["candidate_lineage", "prior_outcomes", "best_history"],
    },
    "E9": {
        "name": "cost_and_validation_state",
        "components": ["remaining_budget", "validation_stage", "query_cost", "eda_cost"],
    },
    "E10": {
        "name": "provenance_and_uncertainty",
        "components": ["source_refs", "hashes", "missing", "conflicts"],
    },
}

TOOL_FAMILIES: dict[str, set[str]] = {
    "read_source": {"E1"},
    "search_source": {"E1"},
    "apply_exact_edits": {"E1"},
    "read_generated_rtl": {"E2"},
    "read_timing": {"E4", "E5"},
    "query_candidate_status": {"E3", "E9", "E10"},
    "list_candidate_artifacts": {"E10"},
    "query_candidate_failure": {"E3", "E10"},
    "compare_candidate_metrics": {"E4", "E6", "E10"},
    "query_candidate_timing_paths": {"E5", "E7", "E10"},
    "revert_source": {"E6", "E8", "E9"},
    "finish": {"E6", "E8", "E9"},
}

ARTIFACT_FAMILIES: dict[str, set[str]] = {
    "candidate_diff": {"E1"},
    "elaboration_stdout": {"E3", "E10"},
    "elaboration_stderr": {"E3", "E10"},
    "differential_stdout": {"E3"},
    "differential_stderr": {"E3"},
    "differential_result": {"E3", "E10"},
    "post_synth_timing_summary": {"E4", "E5", "E7"},
    "post_synth_timing_paths": {"E5", "E7"},
    "post_synth_utilization": {"E4"},
    "vivado_stdout": {"E4", "E5", "E10"},
    "vivado_stderr": {"E4", "E5", "E10"},
    "vivado_diagnostic": {"E4", "E5", "E10"},
}


class AuditError(RuntimeError):
    """Fail-closed error for incomplete or inconsistent audit evidence."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_value(value: Any) -> str:
    return _sha_bytes(_canonical(value))


def _sealed_record(schema_version: str, value: dict[str, Any]) -> dict[str, Any]:
    payload = {"schema_version": schema_version, **ANALYSIS_POLICY, **value}
    return payload | {"content_hash": _sha_value(payload)}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


@dataclass
class _Reader:
    root: Path

    def __post_init__(self) -> None:
        self.root = self.root.resolve(strict=True)
        self.sources: dict[str, dict[str, Any]] = {}

    def _path(self, relative: str | Path) -> Path:
        relative = Path(relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise AuditError("audit input path escaped the campaign")
        path = self.root / relative
        if path.is_symlink():
            raise AuditError("audit refuses symlinked evidence")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise AuditError("required audit evidence is missing") from exc
        if not _is_relative_to(resolved, self.root):
            raise AuditError("audit input resolved outside the campaign")
        return resolved

    def bytes(self, relative: str | Path) -> bytes:
        path = self._path(relative)
        value = path.read_bytes()
        key = path.relative_to(self.root).as_posix()
        observed = {
            "path": key,
            "sha256": _sha_bytes(value),
            "size_bytes": len(value),
        }
        recorded = self.sources.get(key)
        if recorded is not None and recorded != observed:
            raise AuditError("audit input changed while it was being read")
        self.sources[key] = observed
        return value

    def text(self, relative: str | Path) -> str:
        try:
            return self.bytes(relative).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AuditError("audit evidence is not valid UTF-8") from exc

    def json(self, relative: str | Path) -> Any:
        try:
            return json.loads(self.text(relative))
        except json.JSONDecodeError as exc:
            raise AuditError(f"invalid JSON evidence: {Path(relative).name}") from exc

    def ref(self, relative: str | Path, pointer: str = "") -> dict[str, Any]:
        key = Path(relative).as_posix()
        if key not in self.sources:
            self.bytes(relative)
        return {**self.sources[key], "json_pointer": pointer}

    def verify_unchanged(self) -> None:
        for key, recorded in list(self.sources.items()):
            current = self._path(key).read_bytes()
            if (
                len(current) != recorded["size_bytes"]
                or _sha_bytes(current) != recorded["sha256"]
            ):
                raise AuditError("audit input changed while it was being read")


def _tool_names(specs: list[dict[str, Any]]) -> set[str]:
    return {
        str((item.get("function") or {}).get("name"))
        for item in specs
        if isinstance(item, dict) and (item.get("function") or {}).get("name")
    }


def _tool_call_name(call: dict[str, Any]) -> str:
    return str((call.get("function") or {}).get("name", ""))


def _tool_call_args(call: dict[str, Any]) -> dict[str, Any]:
    raw = (call.get("function") or {}).get("arguments") or "{}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuditError("saved assistant tool arguments are invalid JSON") from exc
    if not isinstance(value, dict):
        raise AuditError("saved assistant tool arguments are not an object")
    return value


def _source(
    reader: _Reader, relative: str | Path, pointer: str = "",
) -> dict[str, Any]:
    return reader.ref(relative, pointer)


def _artifact_kind(args: dict[str, Any], inventory: dict[str, str]) -> str | None:
    ref = args.get("artifact_ref")
    return inventory.get(str(ref)) if ref is not None else None


def _families_for_event(
    name: str, content: Any, args: dict[str, Any], inventory: dict[str, str],
) -> set[str]:
    families = set(TOOL_FAMILIES.get(name, set()))
    if name == "read_candidate_artifact":
        families.update(ARTIFACT_FAMILIES.get(_artifact_kind(args, inventory) or "", set()))
    if name == "evaluate_candidate" and isinstance(content, dict):
        families.update({"E3", "E6", "E8", "E9", "E10"})
        if content.get("post_synth"):
            families.add("E4")
        structured = content.get("structured_feedback") or {}
        if (structured.get("timing_paths") or {}).get("paths"):
            families.add("E5")
    return families


def _event_channel(name: str) -> str:
    if name == "evaluate_candidate" or name in {
        "apply_exact_edits", "revert_source", "finish",
    }:
        return "pushed"
    if name.startswith("query_candidate_") or name in {
        "list_candidate_artifacts", "compare_candidate_metrics",
    }:
        return "structured_pull"
    return "raw_pull"


def _message_tool_state(
    messages: list[dict[str, Any]], event: dict[str, Any],
) -> str:
    for message in messages:
        if message.get("role") != "tool" or message.get("tool_call_id") != event["call_id"]:
            continue
        content = str(message.get("content", ""))
        if content == event["raw_content"]:
            return "inline_current_request" if event["name"] == "evaluate_candidate" else "visible_recent_tool_result"
        try:
            archived = json.loads(content)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(archived, dict)
            and archived.get("status") == "archived_tool_result"
            and archived.get("content_sha256") == event["content_sha256"]
        ):
            return "archived_hash_only"
    return "not_exposed"


def _best_status(values: Iterable[str], order: tuple[str, ...]) -> str:
    found = set(values)
    return next((item for item in order if item in found), order[-1])


def _decision_class(last_evaluation: dict[str, Any] | None, actions: list[str]) -> str:
    if any(item in {"finish", "revert_source"} for item in actions):
        return "finish_or_stop"
    if last_evaluation is None:
        return "initial_design"
    if not last_evaluation.get("candidate_valid"):
        return "repair_after_failure"
    if last_evaluation.get("became_best"):
        return "optimize_after_valid_result"
    return "continue_after_non_best"


def _structured_metric_ids(content: dict[str, Any]) -> set[str]:
    structured = content.get("structured_feedback") or {}
    comparisons = (structured.get("metrics") or {}).get("comparisons", [])
    return {str(row.get("metric_id")) for row in comparisons if row.get("metric_id")}


def _information_gaps(
    family: str, last_evaluation: dict[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "lossy_fields": [],
        "derived_relation_fields": [],
        "unavailable_fields": [],
        "compression_evidence": None,
        "derived_relation_evidence": None,
    }
    if not last_evaluation:
        return result
    content = last_evaluation.get("content") or {}
    structured = content.get("structured_feedback")
    if not isinstance(structured, dict):
        return result
    base = dict(last_evaluation["source_ref"])
    if family == "E4":
        raw = set((content.get("post_synth") or {}).keys())
        represented = _structured_metric_ids(content)
        material = {
            "critical_delay_ns", "slice_luts", "slice_registers", "wns_ns",
            "tns_ns", "failing_endpoints", "total_endpoints",
        }
        result["lossy_fields"] = sorted((raw & material) - represented)
        if result["lossy_fields"]:
            result["compression_evidence"] = {
                "raw_or_wrapper_source": base | {"json_pointer": "/post_synth"},
                "structured_source": base | {
                    "json_pointer": "/structured_feedback/metrics"
                },
            }
        return result
    if family == "E6":
        current = content.get("post_synth")
        best = content.get("best_post_synth")
        parent = content.get("parent_post_synth")
        operand_sources = []
        if isinstance(current, dict) and isinstance(best, dict):
            result["derived_relation_fields"].append("best_relative_delta")
            operand_sources.extend([
                base | {"json_pointer": "/post_synth"},
                base | {"json_pointer": "/best_post_synth"},
            ])
        else:
            result["unavailable_fields"].append("best_relative_delta")
        if isinstance(current, dict) and isinstance(parent, dict):
            result["derived_relation_fields"].append("parent_relative_delta")
            operand_sources.extend([
                base | {"json_pointer": "/post_synth"},
                base | {"json_pointer": "/parent_post_synth"},
            ])
        else:
            result["unavailable_fields"].append("parent_relative_delta")
        if operand_sources:
            result["derived_relation_evidence"] = {
                "operand_sources": operand_sources,
                "structured_source": base | {
                    "json_pointer": "/structured_feedback/metrics"
                },
            }
        return result
    if family == "E7":
        current_paths = (structured.get("timing_paths") or {}).get("paths")
        reference_paths = content.get("parent_timing_paths")
        fields = [
            "critical_path_signature_change", "endpoint_or_path_group_movement",
        ]
        if isinstance(current_paths, list) and current_paths and isinstance(
            reference_paths, list
        ) and reference_paths:
            result["derived_relation_fields"] = fields
            result["derived_relation_evidence"] = {
                "operand_sources": [
                    base | {"json_pointer": "/structured_feedback/timing_paths/paths"},
                    base | {"json_pointer": "/parent_timing_paths"},
                ],
                "structured_source": base | {
                    "json_pointer": "/structured_feedback/timing_paths"
                },
            }
        else:
            result["unavailable_fields"] = fields
        return result
    if family == "E8":
        branch_history = content.get("branch_history")
        if branch_history is not None:
            result["lossy_fields"] = ["current_branch_history"]
            result["compression_evidence"] = {
                "raw_or_wrapper_source": base | {"json_pointer": "/branch_history"},
                "structured_source": base | {"json_pointer": "/structured_feedback"},
            }
        else:
            result["unavailable_fields"] = ["current_branch_history"]
        return result
    if family == "E9" and structured.get("cost") is None:
        if content.get("agent_visible_cost") is not None:
            result["lossy_fields"] = ["evaluation_and_query_cost"]
            result["compression_evidence"] = {
                "raw_or_wrapper_source": base | {
                    "json_pointer": "/agent_visible_cost"
                },
                "structured_source": base | {
                    "json_pointer": "/structured_feedback/status"
                },
            }
        else:
            result["unavailable_fields"] = ["evaluation_and_query_cost"]
    return result


def _available_channels(
    family: str,
    tool_names: set[str],
    prior_events: list[dict[str, Any]],
    last_evaluation: dict[str, Any] | None,
    messages: list[dict[str, Any]],
) -> set[str]:
    channels = {
        event["channel"]
        for event in prior_events
        if family in event["families"]
        and _message_tool_state(messages, event) in {
            "inline_current_request", "visible_recent_tool_result",
        }
    }
    for tool, families in TOOL_FAMILIES.items():
        if tool in STATE_CHANGING_TOOLS:
            continue
        if family in families and tool in tool_names:
            channels.add("structured_pull" if tool.startswith("query_") or tool in {
                "list_candidate_artifacts", "compare_candidate_metrics",
            } else "raw_pull")
    if "read_candidate_artifact" in tool_names and family in {"E1", "E3", "E4", "E5", "E7", "E10"}:
        if last_evaluation and last_evaluation.get("artifact_families", {}).get(family):
            channels.add("raw_pull")
    if family == "E2" and last_evaluation is not None:
        # The current runtime artifact inventory does not register generated RTL.
        channels.discard("raw_pull")
        if "read_generated_rtl" in tool_names:
            channels.add("raw_pull")  # baseline RTL only; qualified below.
    return channels


def _coverage_for_decision(
    *,
    decision_id: str,
    turn: int,
    messages: list[dict[str, Any]],
    tool_specs: list[dict[str, Any]],
    prior_events: list[dict[str, Any]],
    last_evaluation: dict[str, Any] | None,
    working_dirty: bool,
    reader: _Reader,
    request_path: Path,
) -> list[dict[str, Any]]:
    names = _tool_names(tool_specs)
    rows = []
    request_ref = _source(reader, request_path, "/request/messages")
    for family, definition in EVIDENCE_FAMILIES.items():
        family_events = [event for event in prior_events if family in event["families"]]
        channels = _available_channels(
            family, names, prior_events, last_evaluation, messages,
        )
        component_notes: list[dict[str, Any]] = []
        if family == "E2":
            component_notes = [
                {
                    "component": "baseline_generated_rtl",
                    "availability": "raw_pull" if "read_generated_rtl" in names else "unavailable",
                },
                {
                    "component": "current_candidate_generated_rtl",
                    "availability": "unavailable" if last_evaluation else "not_applicable",
                    "reason": "candidate generated RTL is not registered in the runtime artifact inventory",
                },
            ]
            if last_evaluation:
                channels = {item for item in channels if item != "raw_pull"}
                if "read_generated_rtl" in names:
                    channels.add("raw_pull")
        exposures = [_message_tool_state(messages, event) for event in family_events]
        visible = [value for value in exposures if value != "not_exposed"]
        consumed = any(
            event.get("seen_before_or_at") is not None
            and int(event["seen_before_or_at"]) <= turn
            for event in family_events
        )
        if family == "E4" and last_evaluation is None and any(
            "Baseline post-synthesis critical delay" in str(message.get("content", ""))
            for message in messages
        ):
            channels.add("pushed")
            visible.append("inline_current_request")
            consumed = True
        primary = _best_status(channels or {"unavailable"}, AVAILABILITY_ORDER)
        exposure = _best_status(visible or exposures or {"not_exposed"}, EXPOSURE_ORDER)
        consumption = (
            "actually_seen" if consumed
            else "available_not_seen" if primary != "unavailable"
            else "not_available"
        )
        gaps = _information_gaps(family, last_evaluation)
        lossy = gaps["lossy_fields"]
        derived = gaps["derived_relation_fields"]
        unavailable_fields = gaps["unavailable_fields"]
        stale = working_dirty and family in {"E3", "E4", "E5", "E6", "E7", "E9"}
        if family == "E2" and last_evaluation:
            diagnostic = "unavailable"
        elif stale:
            diagnostic = "stale_or_historical"
        elif lossy:
            diagnostic = "lossy_structured_representation"
        elif derived:
            diagnostic = "derived_relation_not_materialized"
        elif unavailable_fields:
            diagnostic = "unavailable"
        elif primary == "unavailable":
            diagnostic = "unavailable"
        elif consumption == "available_not_seen":
            diagnostic = "available_but_not_consumed"
        elif primary == "raw_pull" and "pushed" not in channels and "structured_pull" not in channels:
            diagnostic = "raw_only"
        else:
            diagnostic = "sufficient_and_seen"
        sources = [request_ref]
        sources.extend(event["source_ref"] for event in family_events)
        unique_sources = {(_sha_value(item), json.dumps(item, sort_keys=True)): item for item in sources}
        rows.append({
            "decision_id": decision_id,
            "family_id": family,
            "family": definition["name"],
            "components": definition["components"],
            "component_notes": component_notes,
            "availability": primary,
            "availability_channels": [item for item in AVAILABILITY_ORDER if item in channels],
            "exposure": exposure,
            "consumption": consumption,
            "diagnostic": diagnostic,
            "lossy_fields": lossy,
            "derived_relation_fields": derived,
            "unavailable_fields": unavailable_fields,
            "compression_evidence": gaps["compression_evidence"],
            "derived_relation_evidence": gaps["derived_relation_evidence"],
            "working_state": "not_evaluated" if working_dirty else "evaluated_or_initial",
            "source_refs": [unique_sources[key] for key in sorted(unique_sources)],
        })
    return rows


def _parse_campaign(campaign: Path) -> dict[str, Any]:
    reader = _Reader(campaign)
    session = reader.json("SESSION.json")
    result = reader.json("RESULT.json")
    if not isinstance(session, dict) or not isinstance(result, dict):
        raise AuditError("campaign session or result is not an object")
    if session.get("memory_mode") != "none" or result.get("memory_mode") != "none":
        raise AuditError("information audit accepts only memory_mode=none campaigns")
    arm = str(session.get("feedback_arm"))
    if arm not in {"E0", "E1"} or result.get("feedback_arm") != arm:
        raise AuditError("campaign feedback arm is inconsistent")
    seed = int(session.get("seed"))

    events: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    artifact_inventory: dict[str, str] = {}
    last_evaluation: dict[str, Any] | None = None
    working_dirty = False
    evaluation_index = 0
    pending_design_decision: dict[str, Any] | None = None
    turn_dirs = sorted((reader.root / "turns").glob("turn-*"))
    if not turn_dirs:
        raise AuditError("campaign has no saved model turns")

    requests: dict[int, list[dict[str, Any]]] = {}
    turn_payloads: list[dict[str, Any]] = []
    for turn_dir in turn_dirs:
        turn = int(turn_dir.name.split("-")[-1])
        relative = turn_dir.relative_to(reader.root)
        model_path = relative / "model-messages-before.json"
        specs_path = relative / "tool-specs.json"
        provider_path = relative / "provider-metadata.json"
        assistant_path = relative / "assistant-message.json"
        model_messages = reader.json(model_path)
        tool_specs = reader.json(specs_path)
        provider = reader.json(provider_path)
        assistant = reader.json(assistant_path)
        if not isinstance(model_messages, list) or not isinstance(tool_specs, list):
            raise AuditError("saved request messages or tool specs have invalid type")
        request = provider.get("request") if isinstance(provider, dict) else None
        if not isinstance(request, dict):
            raise AuditError("provider request is missing")
        provider_messages = request.get("messages")
        if not isinstance(provider_messages, list) or len(provider_messages) < 1:
            raise AuditError("provider messages are missing")
        if provider_messages[1:] != model_messages:
            raise AuditError("provider messages differ from model-messages-before")
        if request.get("tools") != tool_specs:
            raise AuditError("provider tools differ from tool-specs")
        choices = (provider.get("response") or {}).get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise AuditError("provider response message is missing")
        provider_message = choices[0].get("message")
        if provider_message != assistant:
            raise AuditError("saved assistant message differs from provider response")
        requests[turn] = model_messages
        calls = assistant.get("tool_calls") or []
        if not isinstance(calls, list):
            raise AuditError("assistant tool_calls are invalid")
        turn_events = []
        for index, call in enumerate(calls):
            name = _tool_call_name(call)
            call_id = str(call.get("id", ""))
            if not name or not call_id:
                raise AuditError("tool call lacks a stable identity")
            if name not in _tool_names(tool_specs):
                raise AuditError("assistant called a tool absent from the provider request")
            args = _tool_call_args(call)
            tool_path = relative / f"tool-{call_id}.txt"
            raw_content = reader.text(tool_path)
            try:
                content = json.loads(raw_content)
            except json.JSONDecodeError:
                content = raw_content
            event = {
                "event_id": f"{arm}-turn-{turn:02d}-tool-{index + 1:02d}",
                "turn": turn,
                "call_id": call_id,
                "name": name,
                "arguments": args,
                "raw_content": raw_content,
                "content_sha256": _sha_bytes(raw_content.encode()),
                "content": content,
                "channel": _event_channel(name),
                "source_ref": _source(reader, tool_path),
            }
            event["families"] = sorted(
                _families_for_event(name, content, args, artifact_inventory)
            )
            if name == "evaluate_candidate" and isinstance(content, dict):
                raw = content.get("raw_evidence") or {}
                for row in raw.get("artifacts", []) if isinstance(raw, dict) else []:
                    if row.get("content_ref") and row.get("kind"):
                        artifact_inventory[str(row["content_ref"])] = str(row["kind"])
                event["families"] = sorted(
                    _families_for_event(name, content, args, artifact_inventory)
                )
            turn_events.append(event)
        turn_payloads.append({
            "turn": turn,
            "messages": model_messages,
            "tool_specs": tool_specs,
            "provider_path": provider_path,
            "assistant_path": assistant_path,
            "events": turn_events,
        })

    # Consumption is based on bytes that reached a real provider request, not
    # on a tool call existing in the transcript.
    all_events = [event for payload in turn_payloads for event in payload["events"]]
    session_tools = {
        str(message.get("tool_call_id")): str(message.get("content", ""))
        for message in session.get("messages", [])
        if isinstance(message, dict) and message.get("role") == "tool"
    }
    for event in all_events:
        if session_tools.get(event["call_id"]) != event["raw_content"]:
            raise AuditError("tool result differs from the final session transcript")
        seen = []
        for turn, messages in requests.items():
            if turn <= event["turn"]:
                continue
            if _message_tool_state(messages, event) in {
                "inline_current_request", "visible_recent_tool_result",
            }:
                seen.append(turn)
        event["seen_turns"] = seen
        event["seen_before_or_at"] = min(seen) if seen else None
        later_turns = [turn for turn in requests if turn > event["turn"]]
        if later_turns:
            next_messages = requests[min(later_turns)]
            if _message_tool_state(next_messages, event) not in {
                "inline_current_request", "visible_recent_tool_result", "archived_hash_only",
            }:
                raise AuditError("tool result differs from the next model request")

    for payload in turn_payloads:
        turn = payload["turn"]
        actions = [
            event["name"] for event in payload["events"]
            if event["name"] in STATE_CHANGING_TOOLS
        ]
        if actions:
            decision_id = f"{arm}-decision-{len(decisions) + 1:02d}"
            decision = {
                "decision_id": decision_id,
                "arm": arm,
                "seed": seed,
                "turn": turn,
                "decision_class": _decision_class(last_evaluation, actions),
                "actions": actions,
                "evaluation_ordinal_before": evaluation_index,
                "working_state": "not_evaluated" if working_dirty else "evaluated_or_initial",
                "last_evaluated_candidate_id": (
                    last_evaluation.get("candidate_id") if last_evaluation else None
                ),
                "last_evaluation_valid": (
                    last_evaluation.get("candidate_valid") if last_evaluation else None
                ),
                "last_evaluation_became_best": (
                    last_evaluation.get("became_best") if last_evaluation else None
                ),
                "model_request_ref": _source(
                    reader, payload["provider_path"], "/request"
                ),
                "tool_specs_ref": _source(
                    reader,
                    Path("turns") / f"turn-{turn:02d}" / "tool-specs.json",
                ),
            }
            coverage.extend(_coverage_for_decision(
                decision_id=decision_id,
                turn=turn,
                messages=payload["messages"],
                tool_specs=payload["tool_specs"],
                prior_events=events,
                last_evaluation=last_evaluation,
                working_dirty=working_dirty,
                reader=reader,
                request_path=payload["provider_path"],
            ))
            decisions.append(decision)
        for event in payload["events"]:
            events.append(event)
            content = event["content"]
            if event["name"] == "apply_exact_edits" and isinstance(content, dict) and content.get("status") == "applied":
                working_dirty = True
                if decisions and decisions[-1]["turn"] == turn:
                    pending_design_decision = decisions[-1]
            elif event["name"] == "revert_source" and isinstance(content, dict) and content.get("status") == "reverted":
                working_dirty = False
            elif event["name"] == "evaluate_candidate" and isinstance(content, dict):
                evaluation_index += 1
                current = content.get("post_synth")
                best = content.get("best_post_synth")
                became_best = bool(current and best and current == best and content.get("candidate_valid"))
                artifact_families: dict[str, list[str]] = defaultdict(list)
                raw = content.get("raw_evidence") or {}
                for row in raw.get("artifacts", []) if isinstance(raw, dict) else []:
                    kind = str(row.get("kind", ""))
                    for family in ARTIFACT_FAMILIES.get(kind, set()):
                        artifact_families[family].append(kind)
                last_evaluation = {
                    "candidate_id": content.get("candidate_id"),
                    "candidate_valid": bool(content.get("candidate_valid")),
                    "promotable": bool(content.get("promotable")),
                    "became_best": became_best,
                    "content": content,
                    "artifact_families": dict(artifact_families),
                    "turn": turn,
                    "evaluation_ordinal": evaluation_index,
                    "source_ref": event["source_ref"],
                }
                working_dirty = False
                if decisions and decisions[-1]["turn"] == turn:
                    decisions[-1]["outcome_candidate_id"] = content.get("candidate_id")
                    decisions[-1]["outcome_candidate_valid"] = content.get("candidate_valid")
                    decisions[-1]["outcome_became_best"] = became_best
                    decisions[-1]["outcome_evaluation_ordinal"] = evaluation_index
                if pending_design_decision is not None:
                    pending_design_decision["outcome_candidate_id"] = content.get("candidate_id")
                    pending_design_decision["outcome_candidate_valid"] = content.get("candidate_valid")
                    pending_design_decision["outcome_became_best"] = became_best
                    pending_design_decision["outcome_evaluation_ordinal"] = evaluation_index
                    pending_design_decision = None

    event_public = []
    for event in events:
        event_public.append({
            key: event[key]
            for key in (
                "event_id", "turn", "call_id", "name", "arguments",
                "content_sha256", "channel", "families", "seen_turns", "source_ref",
            )
        })
    counts = Counter(event["name"] for event in events)
    raw_kinds = Counter()
    raw_refs: dict[str, set[str]] = defaultdict(set)
    for event in events:
        if event["name"] == "read_candidate_artifact":
            kind = _artifact_kind(event["arguments"], artifact_inventory) or "unknown"
            raw_kinds[kind] += 1
            if event["arguments"].get("artifact_ref"):
                raw_refs[kind].add(str(event["arguments"]["artifact_ref"]))
    campaign_id = reader.root.name
    analysis = {
        "campaign_id": campaign_id,
        "arm": arm,
        "seed": seed,
        "decisions": decisions,
        "coverage": coverage,
        "events": event_public,
        "tool_counts": dict(sorted(counts.items())),
        "raw_artifact_read_kinds": dict(sorted(raw_kinds.items())),
        "raw_artifact_unique_refs": {
            kind: len(refs) for kind, refs in sorted(raw_refs.items())
        },
        "raw_artifact_repeat_reads": {
            kind: raw_kinds[kind] - len(refs)
            for kind, refs in sorted(raw_refs.items())
        },
        "input_sources": [reader.sources[key] for key in sorted(reader.sources)],
        "result_summary": {
            "status": result.get("status"),
            "evaluations": result.get("evaluations"),
            "best_candidate_id": (result.get("best_candidate") or {}).get("id"),
            "best_post_synth": result.get("best_post_synth"),
        },
    }
    reader.verify_unchanged()
    return analysis


def _design_decision_for_candidate(
    analysis: dict[str, Any], candidate_id: str,
) -> dict[str, Any]:
    matched = [
        decision for decision in analysis["decisions"]
        if decision.get("outcome_candidate_id") == candidate_id
    ]
    for decision in matched:
        if "apply_exact_edits" in decision.get("actions", []):
            return decision
    if matched:
        return matched[0]
    raise AuditError("RESULT best candidate has no measured decision lineage")


def _final_best_anchor(analysis: dict[str, Any]) -> dict[str, Any]:
    candidate_id = analysis["result_summary"].get("best_candidate_id")
    if not candidate_id:
        raise AuditError("pair audit campaign has no RESULT best candidate")
    decision = _design_decision_for_candidate(analysis, str(candidate_id))
    if not decision.get("outcome_candidate_valid"):
        raise AuditError("RESULT best candidate is not a valid measured outcome")
    return decision


def _ordinal_anchor(
    analysis: dict[str, Any], evaluation_ordinal: int,
) -> tuple[dict[str, Any], str]:
    exact = [
        decision for decision in analysis["decisions"]
        if decision.get("outcome_evaluation_ordinal") == evaluation_ordinal
    ]
    for decision in exact:
        if "apply_exact_edits" in decision.get("actions", []):
            return decision, "exact_evaluation_ordinal"
    if exact:
        return exact[0], "exact_evaluation_ordinal"
    finish = [
        decision for decision in analysis["decisions"]
        if decision.get("decision_class") == "finish_or_stop"
    ]
    if finish and int(analysis["result_summary"].get("evaluations") or 0) < evaluation_ordinal:
        return finish[-1], "campaign_stopped_before_evaluation_ordinal"
    raise AuditError("campaign lacks the requested evaluation-ordinal anchor")


def _coverage_map(analysis: dict[str, Any], decision_id: str) -> dict[str, dict[str, Any]]:
    return {
        row["family_id"]: row
        for row in analysis["coverage"]
        if row["decision_id"] == decision_id
    }


def _pair_comparison(e0: dict[str, Any], e1: dict[str, Any]) -> dict[str, Any]:
    if e0["arm"] != "E0" or e1["arm"] != "E1" or e0["seed"] != e1["seed"]:
        raise AuditError("pair audit requires matched E0/E1 campaigns and seed")
    e0_best = _final_best_anchor(e0)
    e1_best = _final_best_anchor(e1)
    e0_best_ordinal = int(e0_best["outcome_evaluation_ordinal"])
    e1_aligned, e1_alignment_status = _ordinal_anchor(e1, e0_best_ordinal)
    maps = {
        "e0_before_final_best": _coverage_map(e0, e0_best["decision_id"]),
        "e1_at_e0_final_best_ordinal": _coverage_map(
            e1, e1_aligned["decision_id"]
        ),
        "e1_before_final_best": _coverage_map(e1, e1_best["decision_id"]),
    }
    q1 = [
        family for family, row in maps["e0_before_final_best"].items()
        if row["consumption"] == "actually_seen"
    ]
    q2 = [
        {
            "family_id": family,
            "e0_consumption": maps["e0_before_final_best"][family]["consumption"],
            "e1_availability": maps["e1_at_e0_final_best_ordinal"][family]["availability"],
            "e1_exposure": maps["e1_at_e0_final_best_ordinal"][family]["exposure"],
            "e1_consumption": maps["e1_at_e0_final_best_ordinal"][family]["consumption"],
            "e1_diagnostic": maps["e1_at_e0_final_best_ordinal"][family]["diagnostic"],
        }
        for family in EVIDENCE_FAMILIES
    ]
    q3_components = {
        "current_candidate_generated_rtl": maps["e1_at_e0_final_best_ordinal"]["E2"]["component_notes"],
        "candidate_diff": maps["e1_at_e0_final_best_ordinal"]["E1"],
        "parent_best_ppa": maps["e1_at_e0_final_best_ordinal"]["E6"],
        "wns_tns_failing_endpoints": maps["e1_at_e0_final_best_ordinal"]["E4"],
        "timing_topology": maps["e1_at_e0_final_best_ordinal"]["E5"],
        "critical_path_movement": maps["e1_at_e0_final_best_ordinal"]["E7"],
        "branch_history": maps["e1_at_e0_final_best_ordinal"]["E8"],
    }
    e0_reads = int(e0["tool_counts"].get("read_candidate_artifact", 0))
    e1_reads = int(e1["tool_counts"].get("read_candidate_artifact", 0))
    q4 = {
        "e0_raw_candidate_reads": e0_reads,
        "e1_raw_candidate_reads": e1_reads,
        "difference": e1_reads - e0_reads,
        "e0_read_kinds": e0["raw_artifact_read_kinds"],
        "e1_read_kinds": e1["raw_artifact_read_kinds"],
        "e0_unique_refs": e0["raw_artifact_unique_refs"],
        "e1_unique_refs": e1["raw_artifact_unique_refs"],
        "e0_repeat_reads": e0["raw_artifact_repeat_reads"],
        "e1_repeat_reads": e1["raw_artifact_repeat_reads"],
        "e0_baseline_timing_reads": e0["tool_counts"].get("read_timing", 0),
        "e1_baseline_timing_reads": e1["tool_counts"].get("read_timing", 0),
        "interpretation": (
            "Read-count reduction is decomposed by evidence kind; it is not treated "
            "as proof that observation quality improved."
        ),
        "family_effects": [
            {
                "family_id": "E3",
                "change": "differential_result raw reads removed",
                "e0_reads": e0["raw_artifact_read_kinds"].get("differential_result", 0),
                "e1_reads": e1["raw_artifact_read_kinds"].get("differential_result", 0),
                "e1_family_consumption": maps["e1_at_e0_final_best_ordinal"]["E3"]["consumption"],
                "assessment": "raw detail reduced; correctness family remained pushed",
            },
            {
                "family_id": "E5",
                "change": "post_synth_timing_paths raw reads changed",
                "e0_reads": e0["raw_artifact_read_kinds"].get("post_synth_timing_paths", 0),
                "e1_reads": e1["raw_artifact_read_kinds"].get("post_synth_timing_paths", 0),
                "e1_family_consumption": maps["e1_at_e0_final_best_ordinal"]["E5"]["consumption"],
                "assessment": "timing detail consumption increased",
            },
        ],
    }
    q5_lossy = [
        {
            "family_id": family,
            "lossy_fields": row["lossy_fields"],
            "compression_evidence": row["compression_evidence"],
        }
        for family, row in maps["e1_at_e0_final_best_ordinal"].items()
        if row["lossy_fields"]
    ]
    q5_derived = [
        {
            "family_id": family,
            "derived_relation_fields": row["derived_relation_fields"],
            "derived_relation_evidence": row["derived_relation_evidence"],
        }
        for family, row in maps["e1_at_e0_final_best_ordinal"].items()
        if row["derived_relation_fields"]
    ]
    q5_unavailable = [
        {
            "family_id": family,
            "unavailable_fields": row["unavailable_fields"],
        }
        for family, row in maps["e1_at_e0_final_best_ordinal"].items()
        if row["unavailable_fields"]
    ]
    return _sealed_record("chipcontext.information-pair.v1", {
        "seed": e0["seed"],
        "alignment": {
            "rule": "decision_class_and_evaluation_ordinal_descriptive_only",
            "same_candidate_claim": False,
            "e0_final_best_candidate_id": e0_best.get("outcome_candidate_id"),
            "e0_final_best_evaluation_ordinal": e0_best_ordinal,
            "e1_final_best_candidate_id": e1_best.get("outcome_candidate_id"),
            "e1_final_best_evaluation_ordinal": e1_best.get(
                "outcome_evaluation_ordinal"
            ),
            "e1_ordinal_alignment_status": e1_alignment_status,
            "anchors": {
                "e0_before_final_best": e0_best["decision_id"],
                "e1_at_e0_final_best_ordinal": e1_aligned["decision_id"],
                "e1_before_final_best": e1_best["decision_id"],
            },
        },
        "questions": {
            "Q1_e0_consumed_families_before_final_best": q1,
            "Q2_corresponding_e1_state": q2,
            "Q3_requested_components": q3_components,
            "Q4_raw_read_reduction": q4,
            "Q5_information_gaps": {
                "confirmed_structured_compression_loss": q5_lossy,
                "derived_relation_not_materialized": q5_derived,
                "unavailable": q5_unavailable,
            },
        },
        "causal_claim": "none",
        "limitation": (
            "The arms follow different candidate trajectories and provider sampling is not "
            "deterministic; the audit reports association and information flow only."
        ),
    })


def _markdown(single: list[dict[str, Any]], pair: dict[str, Any]) -> str:
    lines = [
        "# ChipContext Information Sufficiency Audit",
        "",
        "This report is deterministic, analysis-only, and does not claim that an evidence gap caused a QoR outcome.",
        "",
        "- usage_class: `analysis_only`",
        "- eligible_for_agent_context: `false`",
        "- eligible_for_knowledge_store: `false`",
        "",
    ]
    for item in single:
        lines.extend([
            f"## {item['arm']} seed {item['seed']}",
            "",
            f"- Campaign hash: `{item['campaign_hash']}`",
            f"- Decision points: {len(item['decisions'])}",
            f"- Raw candidate reads: {item['tool_counts'].get('read_candidate_artifact', 0)}",
            f"- Baseline timing reads: {item['tool_counts'].get('read_timing', 0)}",
            "",
            "| Decision | Class | Action |",
            "|---|---|---|",
        ])
        for decision in item["decisions"]:
            lines.append(
                f"| {decision['decision_id']} | {decision['decision_class']} | "
                f"{', '.join(decision['actions'])} |"
            )
        lines.append("")
    if (
        pair.get("schema_version") == "chipcontext.information-pair.v1"
        and isinstance(pair.get("questions"), dict)
    ):
        questions = pair["questions"]
        lines.extend([
            "## Pair findings",
            "",
            "### Q1 — E0 evidence consumed before its final best candidate",
            "",
            ", ".join(questions["Q1_e0_consumed_families_before_final_best"]) or "None",
            "",
            "### Q2 — Corresponding E1 evidence state",
            "",
            "| Family | Availability | Exposure | Consumption | Diagnostic |",
            "|---|---|---|---|---|",
        ])
        for row in questions["Q2_corresponding_e1_state"]:
            lines.append(
                f"| {row['family_id']} | {row['e1_availability']} | {row['e1_exposure']} | "
                f"{row['e1_consumption']} | {row['e1_diagnostic']} |"
            )
        q4 = questions["Q4_raw_read_reduction"]
        lines.extend([
            "",
            "### Q4 — Raw-read change",
            "",
            f"E0 used {q4['e0_raw_candidate_reads']} candidate-artifact reads and E1 used "
            f"{q4['e1_raw_candidate_reads']}. E0/E1 baseline timing reads were "
            f"{q4['e0_baseline_timing_reads']}/{q4['e1_baseline_timing_reads']}. Counts alone "
            "do not establish information quality.",
            "",
            "### Q5 — Evidence-qualified information gaps",
            "",
        ])
        q5 = questions["Q5_information_gaps"]
        lines.append("Confirmed structured compression loss:")
        for row in q5["confirmed_structured_compression_loss"]:
            lines.append(f"- {row['family_id']}: {', '.join(row['lossy_fields'])}")
        if not q5["confirmed_structured_compression_loss"]:
            lines.append("- None")
        lines.append("")
        lines.append("Derived relations not materialized:")
        for row in q5["derived_relation_not_materialized"]:
            lines.append(
                f"- {row['family_id']}: {', '.join(row['derived_relation_fields'])}"
            )
        if not q5["derived_relation_not_materialized"]:
            lines.append("- None")
        lines.append("")
        lines.append("Unavailable fields:")
        for row in q5["unavailable"]:
            lines.append(
                f"- {row['family_id']}: {', '.join(row['unavailable_fields'])}"
            )
        if not q5["unavailable"]:
            lines.append("- None")
        lines.extend([
            "",
            "## Interpretation boundary",
            "",
            pair["limitation"],
            "",
        ])
    return "\n".join(lines)


def _public_campaign(analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "campaign_id": analysis["campaign_id"],
        "arm": analysis["arm"],
        "seed": analysis["seed"],
        "campaign_hash": _sha_value({
            "sources": analysis["input_sources"],
            "arm": analysis["arm"],
            "seed": analysis["seed"],
        }),
        "decisions": analysis["decisions"],
        "coverage": analysis["coverage"],
        "events": analysis["events"],
        "tool_counts": analysis["tool_counts"],
        "raw_artifact_read_kinds": analysis["raw_artifact_read_kinds"],
        "raw_artifact_unique_refs": analysis["raw_artifact_unique_refs"],
        "raw_artifact_repeat_reads": analysis["raw_artifact_repeat_reads"],
        "result_summary": analysis["result_summary"],
        "input_sources": analysis["input_sources"],
    }


def _write_outputs(
    *, output: Path, analyses: list[dict[str, Any]], pair: dict[str, Any] | None,
) -> dict[str, Any]:
    resolved_output = output.resolve(strict=False)
    for analysis in analyses:
        # The source paths themselves are not retained in analysis; their roots
        # are checked by the command wrapper before parsing.
        if output.exists():
            raise AuditError("audit output directory already exists")
    output.mkdir(parents=True)
    public = [_public_campaign(item) for item in analyses]
    decisions = _sealed_record("chipcontext.audit-decision-points.v1", {
        "campaigns": [
            {"campaign_hash": item["campaign_hash"], "arm": item["arm"], "seed": item["seed"]}
            for item in public
        ],
        "decision_points": [row for item in public for row in item["decisions"]],
    })
    coverage = _sealed_record("chipcontext.evidence-coverage.v1", {
        "evidence_families": EVIDENCE_FAMILIES,
        "rows": [row for item in public for row in item["coverage"]],
    })
    pair_record = pair or _sealed_record("chipcontext.information-pair.v1", {
        "status": "not_applicable",
        "causal_claim": "none",
    })
    manifest = _sealed_record("chipcontext.information-audit-manifest.v1", {
        "audit_revision": AUDIT_REVISION,
        "campaigns": [
            {
                "campaign_id": item["campaign_id"],
                "campaign_hash": item["campaign_hash"],
                "arm": item["arm"],
                "seed": item["seed"],
                "input_source_count": len(item["input_sources"]),
            }
            for item in public
        ],
        "record_hashes": {
            "decision_points": decisions["content_hash"],
            "evidence_coverage": coverage["content_hash"],
            "pair_comparison": pair_record["content_hash"],
        },
        "execution_budget": {"model_calls": 0, "eda_calls": 0, "simulator_calls": 0},
    })
    _write_json(output / "AUDIT_MANIFEST.json", manifest)
    _write_json(output / "DECISION_POINTS.json", decisions)
    _write_json(output / "EVIDENCE_COVERAGE.json", coverage)
    _write_json(output / "PAIR_COMPARISON.json", pair_record)
    (output / "INFORMATION_SUFFICIENCY.md").write_text(_markdown(public, pair_record))
    return manifest


def audit_campaign(campaign: Path, output: Path) -> dict[str, Any]:
    campaign = campaign.resolve(strict=True)
    output_resolved = output.resolve(strict=False)
    if _is_relative_to(output_resolved, campaign):
        raise AuditError("audit output must be outside the source campaign")
    analysis = _parse_campaign(campaign)
    return _write_outputs(output=output, analyses=[analysis], pair=None)


def audit_campaign_pair(e0: Path, e1: Path, output: Path) -> dict[str, Any]:
    e0 = e0.resolve(strict=True)
    e1 = e1.resolve(strict=True)
    output_resolved = output.resolve(strict=False)
    if e0 == e1:
        raise AuditError("pair audit requires two different campaigns")
    if _is_relative_to(output_resolved, e0) or _is_relative_to(output_resolved, e1):
        raise AuditError("audit output must be outside both source campaigns")
    left = _parse_campaign(e0)
    right = _parse_campaign(e1)
    if left["arm"] == "E1" and right["arm"] == "E0":
        left, right = right, left
    pair = _pair_comparison(left, right)
    return _write_outputs(output=output, analyses=[left, right], pair=pair)
