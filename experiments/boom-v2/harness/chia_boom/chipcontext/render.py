from __future__ import annotations

import json
from typing import Any, Mapping


def render_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _cell(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, sort_keys=True, ensure_ascii=False)
    else:
        rendered = str(value)
    return rendered.replace("|", "\\|").replace("\n", "<br>")


def _json_section(title: str, value: Any) -> list[str]:
    return [
        f"## {title}",
        "",
        "```json",
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False),
        "```",
        "",
    ]


def render_markdown(
    answer: Mapping[str, Any],
    cost: Mapping[str, Any],
) -> str:
    """Render a complete QueryAnswer without interpreting or hiding facts."""
    query = answer.get("query", {})
    scope = answer.get("resolved_scope", {})
    applicability = answer.get("applicability", {})
    lines = [
        f"# ChipContext query: {_cell(query.get('name'))}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| answer hash | `{_cell(answer.get('content_hash'))}` |",
        f"| query revision | `{_cell(query.get('revision'))}` |",
        f"| store | `{_cell(scope.get('store_id'))}` |",
        f"| candidate | `{_cell(scope.get('candidate_ref', {}).get('candidate_id'))}` |",
        f"| attempt | `{_cell(scope.get('attempt_id'))}` |",
        f"| applicability | `{_cell(applicability.get('status'))}` |",
        f"| stage | `{_cell(query.get('parameters', {}).get('stage'))}` |",
        "",
    ]
    result = answer.get("result")
    if query.get("name") == "read_artifact" and isinstance(result, dict):
        structured = dict(result)
        raw = structured.pop("content", "")
        lines.extend(_json_section("Result", structured))
        lines.extend(["## Raw content", ""])
        raw_lines = str(raw).splitlines(keepends=True)
        if raw_lines:
            lines.extend(f"    {line.rstrip(chr(10)).rstrip(chr(13))}" for line in raw_lines)
        else:
            lines.append("    ")
        lines.append("")
    else:
        lines.extend(_json_section("Result", result))

    for key, title in (
        ("query", "Normalized query"),
        ("resolved_scope", "Resolved scope"),
        ("applicability", "Applicability"),
        ("conditions", "Conditions"),
        ("coverage", "Coverage"),
        ("missing", "Missing evidence"),
        ("conflicts", "Conflicts"),
        ("source_refs", "Source references"),
        ("pagination", "Pagination"),
    ):
        lines.extend(_json_section(title, answer.get(key)))
    lines.extend(_json_section("Query cost", cost))
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_json", "render_markdown"]
