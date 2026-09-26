from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chia_boom.information_audit import (
    ANALYSIS_POLICY,
    AuditError,
    audit_campaign,
    audit_campaign_pair,
)


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def compact(messages: list[dict], keep: int = 4) -> list[dict]:
    indexes = [i for i, row in enumerate(messages) if row.get("role") == "tool"]
    retained = set(indexes[-keep:])
    names = {}
    for row in messages:
        if row.get("role") == "assistant":
            for call in row.get("tool_calls") or []:
                names[call["id"]] = call["function"]["name"]
    result = []
    for index, row in enumerate(messages):
        copied = dict(row)
        if row.get("role") == "tool" and index not in retained:
            raw = row["content"]
            copied["content"] = json.dumps({
                "status": "archived_tool_result",
                "tool": names[row["tool_call_id"]],
                "content_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "content_chars": len(raw),
                "instruction": "Reissue the tool query if these bytes are needed again.",
            }, sort_keys=True)
        result.append(copied)
    return result


BASE_TOOLS = [
    "read_source", "search_source", "read_timing", "read_generated_rtl",
    "read_candidate_artifact", "apply_exact_edits", "evaluate_candidate",
    "revert_source", "finish",
]
STRUCTURED_TOOLS = [
    "query_candidate_status", "list_candidate_artifacts",
    "query_candidate_failure", "compare_candidate_metrics",
    "query_candidate_timing_paths",
]


def specs(arm: str) -> list[dict]:
    names = BASE_TOOLS + (STRUCTURED_TOOLS if arm == "E1" else [])
    return [
        {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
        for name in names
    ]


def call(name: str, call_id: str, arguments: dict | None = None) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {})},
    }


def evaluation(arm: str, index: int, valid: bool = True) -> dict:
    candidate_id = f"NeutralQueue-{arm}-seed41-candidate-{index:02d}"
    ppa = None if not valid else {
        "clock_period_ns": 5.0,
        "critical_delay_ns": 9.0 - index * 0.25,
        "slice_luts": 1000 + index,
        "slice_registers": 500,
        "wns_ns": -4.0 + index * 0.25,
        "tns_ns": -100.0 + index,
        "failing_endpoints": 20 - index,
        "total_endpoints": 100,
    }
    artifacts = [
        {
            "kind": "candidate_diff", "content_ref": f"{index:064x}",
            "stage": "elaboration", "media_type": "text/plain", "size_bytes": 20,
        },
        {
            "kind": "post_synth_timing_paths", "content_ref": f"{index + 20:064x}",
            "stage": "post_synth", "media_type": "text/plain", "size_bytes": 40,
        },
    ] if valid else [{
        "kind": "elaboration_stderr", "content_ref": f"{index + 40:064x}",
        "stage": "elaboration", "media_type": "text/plain", "size_bytes": 30,
    }]
    value = {
        "status": "complete" if valid else "candidate_invalid",
        "stage": "synthesis" if valid else "elaboration",
        "failure_class": None if valid else "elaboration_error",
        "build_ok": valid,
        "interface_ok": valid,
        "correctness_ok": valid,
        "candidate_valid": valid,
        "promotable": valid,
        "post_synth": ppa,
        "candidate_id": candidate_id,
        "raw_error_tail": "" if valid else "neutral elaboration failure",
        "evaluations_remaining": 4 - index,
        "baseline_post_synth": {"critical_delay_ns": 10.0, "slice_luts": 1000},
        "best_post_synth": ppa if valid else None,
        "raw_evidence": {
            "candidate_id": candidate_id,
            "attempt_id": f"attempt-{index:02d}",
            "snapshot_ref": f"{index + 80:064x}",
            "artifacts": artifacts,
            "read_tool": "read_candidate_artifact",
        },
    }
    if arm == "E1":
        comparisons = [] if not valid else [
            {"metric_id": "critical_delay_ns", "status": "comparable", "delta": -1.0},
            {"metric_id": "slice_luts", "status": "comparable", "delta": index},
        ]
        value["structured_feedback"] = {
            "schema_version": "chipcontext.runtime-feedback.v1",
            "candidate": {"candidate_id": candidate_id},
            "applicability": {"status": "current"},
            "status": {
                "completion_status": value["status"],
                "checks": [{"check_id": "differential", "executed": valid, "outcome": "pass" if valid else "inconclusive"}],
            },
            "failure": None if valid else {"failure_class": "elaboration_error"},
            "metrics": {"comparisons": comparisons},
            "timing_paths": {
                "paths": [] if not valid else [{"rank": 1, "source": "neutral/a", "destination": "neutral/z"}],
            },
            "raw_artifacts": artifacts,
            "missing": [],
            "conflicts": [],
            "source_refs": [],
            "content_hash": f"{index + 120:064x}",
        }
    return value


def make_campaign(root: Path, arm: str) -> Path:
    campaign = root / f"synthetic-{arm.lower()}"
    messages = [{
        "role": "user",
        "content": "Optimize NeutralQueue. Baseline post-synthesis critical delay is 10.0 ns and Slice LUTs are 1000.",
    }]
    turn = 0

    def add_turn(calls: list[dict], results: list[dict | str]) -> None:
        nonlocal turn, messages
        turn += 1
        directory = campaign / "turns" / f"turn-{turn:02d}"
        model_messages = compact(messages)
        tool_specs = specs(arm)
        assistant = {"role": "assistant", "content": "", "tool_calls": calls}
        response = {"choices": [{"message": assistant}], "usage": {"total_tokens": 10}}
        provider = {
            "request": {
                "model": "synthetic-model",
                "messages": [{"role": "system", "content": "neutral synthetic agent"}, *model_messages],
                "tools": tool_specs,
            },
            "response": response,
            "usage": response["usage"],
        }
        dump(directory / "model-messages-before.json", model_messages)
        dump(directory / "tool-specs.json", tool_specs)
        dump(directory / "provider-metadata.json", provider)
        dump(directory / "assistant-message.json", assistant)
        messages.append(assistant)
        for item, result in zip(calls, results):
            raw = result if isinstance(result, str) else json.dumps(result, sort_keys=True)
            (directory / f"tool-{item['id']}.txt").write_text(raw)
            messages.append({"role": "tool", "tool_call_id": item["id"], "content": raw})

    # A consumed baseline RTL read becomes archived by later decision points.
    add_turn([call("read_generated_rtl", "rtl-1", {"file": "Neutral.sv"})], ["module Neutral; endmodule\n"])
    for index in range(1, 5):
        edit = call("apply_exact_edits", f"edit-{index}", {"edits": [{"old": f"v{index}", "new": f"w{index}"}]})
        if index == 1:
            # Exercise multiple actions sharing one pre-action decision point.
            evaluate = call("evaluate_candidate", f"eval-{index}")
            value = evaluation(arm, index, valid=(arm == "E0"))
            add_turn([edit, evaluate], [
                {"status": "applied", "source_sha256": f"{index:064x}", "diff_sha256": f"{index + 1:064x}"},
                value,
            ])
        else:
            add_turn([edit], [{"status": "applied", "source_sha256": f"{index:064x}", "diff_sha256": f"{index + 1:064x}"}])
            add_turn([call("evaluate_candidate", f"eval-{index}")], [evaluation(arm, index)])
        if index in ({1, 3, 4} if arm == "E0" else {2, 4}):
            value = evaluation(arm, index, valid=(arm != "E1" or index != 1))
            ref = value["raw_evidence"]["artifacts"][0]["content_ref"]
            add_turn([
                call("read_candidate_artifact", f"raw-{index}", {
                    "candidate_id": value["candidate_id"], "artifact_ref": ref,
                    "start_line": 1, "line_count": 8, "limit_bytes": 4096,
                })
            ], [f"synthetic candidate diff {index}\n"])
    add_turn([call("finish", "finish-1", {"summary": "synthetic finish"})], [{
        "status": "accepted",
        "best_candidate_id": f"NeutralQueue-{arm}-seed41-candidate-04",
        "best_post_synth": evaluation(arm, 4)["post_synth"],
    }])
    dump(campaign / "SESSION.json", {
        "memory_mode": "none", "feedback_arm": arm, "seed": 41,
        "messages": messages, "status": "finished", "evaluations": 4,
    })
    dump(campaign / "RESULT.json", {
        "memory_mode": "none", "feedback_arm": arm, "status": "finished",
        "evaluations": 4,
        "best_candidate": {"id": f"NeutralQueue-{arm}-seed41-candidate-04"},
        "best_post_synth": evaluation(arm, 4)["post_synth"],
    })
    return campaign


class InformationAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.e0 = make_campaign(self.root, "E0")
        self.e1 = make_campaign(self.root, "E1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pair_audit_is_deterministic_and_analysis_only(self) -> None:
        before = {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for campaign in (self.e0, self.e1)
            for path in campaign.rglob("*") if path.is_file()
        }
        hashes = []
        for index in range(3):
            output = self.root / f"audit-{index}"
            audit_campaign_pair(self.e0, self.e1, output)
            records = [
                json.loads((output / name).read_text())
                for name in (
                    "AUDIT_MANIFEST.json", "DECISION_POINTS.json",
                    "EVIDENCE_COVERAGE.json", "PAIR_COMPARISON.json",
                )
            ]
            hashes.append([row["content_hash"] for row in records])
            for record in records:
                for key, value in ANALYSIS_POLICY.items():
                    self.assertEqual(record[key], value)
        self.assertEqual(hashes[0], hashes[1])
        self.assertEqual(hashes[1], hashes[2])
        after = {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for campaign in (self.e0, self.e1)
            for path in campaign.rglob("*") if path.is_file()
        }
        self.assertEqual(before, after)

    def test_coverage_separates_loss_unavailability_and_archived_exposure(self) -> None:
        output = self.root / "audit"
        audit_campaign_pair(self.e0, self.e1, output)
        coverage = json.loads((output / "EVIDENCE_COVERAGE.json").read_text())["rows"]
        self.assertTrue(any(
            row["family_id"] == "E4"
            and row["diagnostic"] == "lossy_structured_representation"
            and "wns_ns" in row["lossy_fields"]
            for row in coverage
        ))
        self.assertTrue(any(
            row["family_id"] == "E2"
            and row["diagnostic"] == "unavailable"
            and any(note.get("component") == "current_candidate_generated_rtl" for note in row["component_notes"])
            for row in coverage
        ))
        self.assertTrue(any(row["exposure"] == "archived_hash_only" for row in coverage))

    def test_pair_answers_all_five_questions_without_causal_claim(self) -> None:
        output = self.root / "audit"
        audit_campaign_pair(self.e0, self.e1, output)
        pair = json.loads((output / "PAIR_COMPARISON.json").read_text())
        self.assertEqual(pair["causal_claim"], "none")
        self.assertEqual(len(pair["questions"]), 5)
        self.assertFalse(pair["alignment"]["same_candidate_claim"])
        self.assertEqual(
            pair["questions"]["Q4_raw_read_reduction"]["e0_raw_candidate_reads"], 3
        )
        self.assertEqual(
            pair["questions"]["Q4_raw_read_reduction"]["e1_raw_candidate_reads"], 2
        )
        losses = {
            row["family_id"]: row
            for row in pair["questions"]["Q5_structured_compression_loss"]
        }
        self.assertIn("E7", losses)
        self.assertIn("critical_path_signature_change", losses["E7"]["lossy_fields"])
        self.assertIn("raw_or_wrapper_source", losses["E7"]["compression_evidence"])
        self.assertIn("structured_source", losses["E7"]["compression_evidence"])

    def test_provider_request_tamper_fails_closed(self) -> None:
        path = self.e0 / "turns/turn-01/provider-metadata.json"
        value = json.loads(path.read_text())
        value["request"]["messages"][1]["content"] = "tampered"
        dump(path, value)
        with self.assertRaisesRegex(AuditError, "provider messages differ"):
            audit_campaign(self.e0, self.root / "audit")

    def test_output_inside_campaign_and_existing_output_are_rejected(self) -> None:
        with self.assertRaisesRegex(AuditError, "outside"):
            audit_campaign(self.e0, self.e0 / "audit")
        output = self.root / "audit"
        audit_campaign(self.e0, output)
        with self.assertRaisesRegex(AuditError, "already exists"):
            audit_campaign(self.e0, output)

    def test_sibling_arms_are_not_claimed_as_same_candidate(self) -> None:
        output = self.root / "audit"
        audit_campaign_pair(self.e0, self.e1, output)
        pair = json.loads((output / "PAIR_COMPARISON.json").read_text())
        self.assertEqual(
            pair["alignment"]["rule"],
            "decision_class_and_evaluation_ordinal_descriptive_only",
        )

    def test_available_but_unqueried_and_multi_action_decision_are_explicit(self) -> None:
        output = self.root / "audit"
        audit_campaign_pair(self.e0, self.e1, output)
        decisions = json.loads((output / "DECISION_POINTS.json").read_text())["decision_points"]
        coverage = json.loads((output / "EVIDENCE_COVERAGE.json").read_text())["rows"]
        first_e1 = next(row for row in decisions if row["decision_id"] == "E1-decision-01")
        self.assertEqual(first_e1["actions"], ["apply_exact_edits", "evaluate_candidate"])
        rows = [row for row in coverage if row["decision_id"] == "E1-decision-01"]
        self.assertTrue(any(
            row["availability"] in {"structured_pull", "raw_pull"}
            and row["consumption"] == "available_not_seen"
            for row in rows
        ))

    def test_assistant_prose_is_not_treated_as_consumed_evidence(self) -> None:
        output_a = self.root / "audit-a"
        audit_campaign(self.e1, output_a)
        path = self.e1 / "turns/turn-01/assistant-message.json"
        assistant = json.loads(path.read_text())
        assistant["content"] = "I inspected current candidate generated RTL and full branch history."
        dump(path, assistant)
        provider_path = self.e1 / "turns/turn-01/provider-metadata.json"
        provider = json.loads(provider_path.read_text())
        provider["response"]["choices"][0]["message"] = assistant
        dump(provider_path, provider)
        output_b = self.root / "audit-b"
        audit_campaign(self.e1, output_b)
        rows_a = json.loads((output_a / "EVIDENCE_COVERAGE.json").read_text())["rows"]
        rows_b = json.loads((output_b / "EVIDENCE_COVERAGE.json").read_text())["rows"]
        projection = lambda rows: [
            (row["decision_id"], row["family_id"], row["availability"], row["exposure"], row["consumption"], row["diagnostic"])
            for row in rows
        ]
        self.assertEqual(projection(rows_a), projection(rows_b))

    def test_audit_does_not_use_network_subprocess_or_api_key(self) -> None:
        old = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            with mock.patch("socket.create_connection", side_effect=AssertionError("network")), mock.patch(
                "subprocess.run", side_effect=AssertionError("subprocess")
            ), mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess")):
                audit_campaign(self.e0, self.root / "audit")
        finally:
            if old is not None:
                os.environ["DEEPSEEK_API_KEY"] = old

    def test_single_campaign_emits_fixed_output_set(self) -> None:
        output = self.root / "audit"
        audit_campaign(self.e0, output)
        self.assertEqual(
            {item.name for item in output.iterdir()},
            {
                "AUDIT_MANIFEST.json", "DECISION_POINTS.json",
                "EVIDENCE_COVERAGE.json", "PAIR_COMPARISON.json",
                "INFORMATION_SUFFICIENCY.md",
            },
        )


if __name__ == "__main__":
    unittest.main()
