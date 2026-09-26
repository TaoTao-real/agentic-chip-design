from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from chia_boom.cc03t import CC03TError, decision_gate, validate_manifest
from chia_boom.interactive import _interactive_system_with_feedback, _interactive_tool_specs
from chia_boom.optimization_trace import (
    BASELINE_CANDIDATE_ID,
    Candidate,
    Evaluation,
    TraceError,
    Transition,
    ancestry,
    branch_history,
    build_trace,
    build_trace_tail,
    build_visibility_manifest,
    compare_ppa,
    content_hash,
    select_parent,
    visibility_pair_isolated,
)
from chia_boom.validation_timeline import Timeline, summarize_spans


def ppa(delay: float, luts: int, contract: str = "tools") -> dict:
    return {
        "stage": "post_synth",
        "critical_delay_ns": delay,
        "slice_luts": luts,
        "tool_contract_hash": contract,
    }


def candidate(cid: str, parent: str, evaluation: str) -> Candidate:
    return Candidate(
        candidate_id=cid,
        parent_candidate_id=parent,
        source_sha256=cid * 4,
        patch_sha256=cid * 4,
        evaluation_ref=evaluation,
    )


def evaluation(eid: str, cid: str, delay: float | None, *, valid: bool = True) -> Evaluation:
    return Evaluation(
        evaluation_id=eid,
        candidate_id=cid,
        status="complete" if valid else "candidate_invalid",
        stage="synthesis" if valid else "elaboration",
        correctness_ok=True if valid else None,
        candidate_valid=valid,
        promotable=valid,
        ppa=ppa(delay, 100) if delay is not None else None,
    )


def transition(tid: str, parent: str, current: str, best: str) -> Transition:
    return Transition(
        transition_id=tid,
        decision_point_id="d-" + tid,
        from_candidate_id=parent,
        to_candidate_id=current,
        evaluation_id="e-" + current,
        best_before_candidate_id=best,
        parent_delta={"status": "comparable", "delay_delta_ns": -1.0, "lut_delta": 0},
        best_delta={"status": "comparable", "delay_delta_ns": -1.0, "lut_delta": 0},
        correctness={"status": "available", "passed": True},
        became_new_best=True,
        raw_evidence_refs=("raw-" + current,),
    )


class TraceTests(unittest.TestCase):
    def test_delta_direction_and_missing_contract(self) -> None:
        self.assertEqual(compare_ppa(ppa(9, 101), ppa(10, 100))["delay_delta_ns"], -1)
        self.assertEqual(compare_ppa(None, ppa(10, 100))["status"], "unavailable")
        missing = ppa(9, 100)
        missing["tool_contract_hash"] = None
        self.assertEqual(
            compare_ppa(missing, ppa(10, 100))["reason"],
            "tool_contract_missing",
        )
        other = ppa(9, 100, "other")
        self.assertEqual(compare_ppa(other, ppa(10, 100))["status"], "not_comparable")

    def test_search_policy_three_baseline_then_best(self) -> None:
        parents = [
            select_parent(i, current_best_candidate_id="best", decision_point_id=f"d{i}")
            for i in range(1, 6)
        ]
        self.assertEqual([row.selected_parent_id for row in parents[:3]], [BASELINE_CANDIDATE_ID] * 3)
        self.assertEqual([row.selected_parent_id for row in parents[3:]], ["best", "best"])
        fallback = select_parent(4, current_best_candidate_id=BASELINE_CANDIDATE_ID, decision_point_id="d")
        self.assertEqual(fallback.selection_reason, "baseline_fallback")

    def test_dfail_override_is_explicit(self) -> None:
        selected = select_parent(
            1, current_best_candidate_id=BASELINE_CANDIDATE_ID,
            decision_point_id="d", fixture_parent_id="failed",
        )
        self.assertTrue(selected.fixture_override)
        self.assertEqual(selected.selection_reason, "repair_fixture_override")

    def test_branch_history_excludes_sibling(self) -> None:
        candidates = {
            "a": candidate("a", BASELINE_CANDIDATE_ID, "e-a"),
            "b": candidate("b", BASELINE_CANDIDATE_ID, "e-b"),
            "c": candidate("c", "a", "e-c"),
        }
        evaluations = {
            "e-a": evaluation("e-a", "a", 9),
            "e-b": evaluation("e-b", "b", 8),
            "e-c": evaluation("e-c", "c", 7),
        }
        transitions = {
            "ta": transition("ta", BASELINE_CANDIDATE_ID, "a", BASELINE_CANDIDATE_ID),
            "tb": transition("tb", BASELINE_CANDIDATE_ID, "b", "a"),
            "tc": transition("tc", "a", "c", "b"),
        }
        rows = branch_history("c", candidates, evaluations, transitions)
        self.assertEqual([row["candidate_id"] for row in rows], ["c", "a"])
        self.assertNotIn("b", [row["candidate_id"] for row in rows])

    def test_ancestry_fails_closed_on_cycle(self) -> None:
        candidates = {
            "a": candidate("a", "b", "e-a"),
            "b": candidate("b", "a", "e-b"),
        }
        with self.assertRaises(TraceError):
            ancestry("a", candidates)

    def test_trace_tail_budget_and_forbidden_sources(self) -> None:
        tail = build_trace_tail(
            current_candidate_id="a", selected_parent_id="a",
            current_best_candidate_id="a", last_transition=None,
            history=[], remaining_evaluation_budget=4,
            raw_evidence_refs=["raw"], max_bytes=6144,
        )
        self.assertLessEqual(tail["serialized_bytes"], 6144)
        self.assertEqual(tail["branch_history"]["consecutive_invalid"], 0)
        with self.assertRaises(TraceError):
            build_trace_tail(
                current_candidate_id="a", selected_parent_id="a",
                current_best_candidate_id="a", last_transition=None,
                history=[], remaining_evaluation_budget=4,
                raw_evidence_refs=[], source_classes=["analysis_only"],
            )
        with self.assertRaises(TraceError):
            build_trace_tail(
                current_candidate_id="a", selected_parent_id="a",
                current_best_candidate_id="a", last_transition=None,
                history=[{"blob": "x" * 7000}], remaining_evaluation_budget=4,
                raw_evidence_refs=[], max_bytes=6144,
            )

    def test_e0_and_e1t_have_identical_tools_and_system(self) -> None:
        self.assertEqual(_interactive_tool_specs(False, "E0"), _interactive_tool_specs(False, "E1T"))
        self.assertEqual(
            _interactive_system_with_feedback("none", "E0"),
            _interactive_system_with_feedback("none", "E1T"),
        )
        left = build_visibility_manifest(
            request_id="e0", arm="E0", common_context=[1], tool_specs=[2],
            raw_permissions=[3], trace_tail=None,
        )
        right = build_visibility_manifest(
            request_id="e1", arm="E1T", common_context=[1], tool_specs=[2],
            raw_permissions=[3], trace_tail={"content_hash": "tail"},
        )
        self.assertTrue(visibility_pair_isolated(left, right))

    def test_timeline_counts_leaf_spans_once_and_preserves_unknown(self) -> None:
        timeline = Timeline("run")
        timeline.add_unknown("differential", span_id="unknown", reason="legacy_result")
        with timeline.measure("vivado_post_synth", span_id="leaf"):
            pass
        with timeline.measure("candidate_envelope", span_id="parent", leaf=False):
            pass
        summary = summarize_spans(timeline.spans)
        self.assertEqual(summary["leaf_span_count"], 2)
        self.assertEqual(summary["unknown_active_span_count"], 1)
        self.assertEqual(summary["candidate_validation_active_ns"], timeline.spans[1].active_time_ns)

    def test_manifest_is_frozen(self) -> None:
        manifest = {
            "schema_version": "chia-boom.cc03t-experiment.v1",
            "experiment_id": "cc03t",
            "target": "T0-issue-queue",
            "model": "deepseek-v4-pro",
            "memory_mode": "none",
            "search_policy": "baseline-3-best-2-v1",
            "fixtures": {
                "D0": {"kind": "baseline"},
                "Dfail": {"kind": "sealed_candidate", "campaign": "/x", "candidate_index": 1, "source_sha256": "a"},
                "Dperf": {"kind": "sealed_candidate", "campaign": "/x", "candidate_index": 2, "source_sha256": "b", "critical_delay_ns": 23.657, "slice_luts": 50715},
            },
            "decision_pairs": [
                {"scenario": "D0", "seed": 41, "order": ["E0", "E1T"]},
                {"scenario": "Dfail", "seed": 42, "order": ["E1T", "E0"]},
                {"scenario": "Dperf", "seed": 43, "order": ["E0", "E1T"]},
                {"scenario": "Dperf", "seed": 44, "order": ["E1T", "E0"]},
                {"scenario": "Dperf", "seed": 45, "order": ["E0", "E1T"]},
            ],
            "end_to_end": {"seed": 46, "order": ["E1T", "E0"]},
        }
        validate_manifest(manifest)
        manifest["target"] = "other"
        with self.assertRaises(CC03TError):
            validate_manifest(manifest)

    def test_decision_gate_stops_when_e1t_loses_two_dperf_pairs(self) -> None:
        pairs = []
        for seed in (43, 44):
            pairs.append({
                "pair_id": f"Dperf-{seed}", "scenario": "Dperf",
                "arms": [
                    {"arm": "E0", "candidate_valid": True, "became_new_best": True, "ppa": ppa(8, 100)},
                    {"arm": "E1T", "candidate_valid": True, "became_new_best": False, "ppa": ppa(9, 100)},
                ],
            })
        gate = decision_gate(pairs)
        self.assertFalse(gate["passed"])
        self.assertIn("Dperf: E1T lost at least two paired decisions", gate["failures"])

    def _write_campaign(self, root: Path) -> Path:
        campaign = root / "campaign"
        (campaign / "evaluations/candidate-01").mkdir(parents=True)
        (campaign / "evaluations/candidate-02").mkdir(parents=True)
        (campaign / "INTERACTIVE_MANIFEST.json").write_text(json.dumps({
            "frozen_run_fingerprint": "frozen",
            "config": {"targets": {"T0": {}}},
        }))
        (campaign / "SESSION.json").write_text(json.dumps({
            "max_evaluations": 5, "messages": [],
        }))
        (campaign / "RESULT.json").write_text(json.dumps({
            "feedback_arm": "E0", "memory_mode": "none", "status": "finished",
            "baseline_post_synth": {"critical_delay_ns": 10.0, "slice_luts": 100},
        }))
        rows = [
            (1, "c1", None, 9.0),
            (2, "c2", "c1", 8.0),
        ]
        for index, cid, parent, delay in rows:
            directory = campaign / f"evaluations/candidate-{index:02d}"
            candidate_raw = {
                "id": cid, "parent_id": parent, "source": f"source {cid}",
                "source_sha256": content_hash(f"source {cid}"), "diff": f"diff {cid}",
            }
            # Builder verifies source only when no explicit hash normalization is needed.
            candidate_raw["source_sha256"] = __import__("hashlib").sha256(
                candidate_raw["source"].encode()
            ).hexdigest()
            (directory / "candidate.json").write_text(json.dumps(candidate_raw))
            (directory / "result.json").write_text(json.dumps({
                "search_evaluation": {
                    "candidate_id": cid, "status": "complete", "stage": "synthesis",
                    "correctness_ok": True, "candidate_valid": True,
                    "promotable": True,
                    "post_synth": {"critical_delay_ns": delay, "slice_luts": 100},
                    "stages": [],
                }
            }))
        return campaign

    def test_trace_rebuild_is_deterministic_and_best_is_best_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaign = self._write_campaign(root)
            hashes = []
            for index in range(3):
                output = root / f"trace-{index}"
                result = build_trace(campaign, output)
                hashes.append(result["content_hash"])
            self.assertEqual(len(set(hashes)), 1)
            trace = json.loads((root / "trace-0/RUN_TRACE.json").read_text())
            second = trace["transitions"][1]
            self.assertEqual(second["best_before_candidate_id"], "c1")
            self.assertEqual(second["best_delta"]["delay_delta_ns"], -1.0)
            self.assertEqual(
                second["source_before_sha256"],
                trace["transitions"][0]["source_after_sha256"],
            )
            self.assertEqual(second["evaluation_ref"], "evaluation-c2")
            self.assertEqual(trace["provenance"]["result"]["sha256"], __import__("hashlib").sha256(
                (campaign / "RESULT.json").read_bytes()
            ).hexdigest())

    def test_trace_rebuild_includes_frozen_parent_outside_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaign = self._write_campaign(root)
            shutil.rmtree(campaign / "evaluations/candidate-02")
            candidate_path = campaign / "evaluations/candidate-01/candidate.json"
            candidate_row = json.loads(candidate_path.read_text())
            candidate_row["parent_id"] = "fixture-fail"
            candidate_path.write_text(json.dumps(candidate_row))
            session_path = campaign / "SESSION.json"
            session = json.loads(session_path.read_text())
            session["cc03t_state"] = {
                "trace_candidates": {
                    "fixture-fail": {
                        "candidate_id": "fixture-fail",
                        "parent_candidate_id": BASELINE_CANDIDATE_ID,
                        "source_sha256": "f" * 64,
                        "patch_sha256": "e" * 64,
                        "evaluation_ref": "evaluation-fixture-fail",
                        "generated_rtl_ref": None,
                        "generated_rtl_sha256": None,
                        "provenance": {"fixture": "Dfail"},
                    }
                },
                "trace_evaluations": {
                    "evaluation-fixture-fail": {
                        "evaluation_id": "evaluation-fixture-fail",
                        "candidate_id": "fixture-fail",
                        "status": "candidate_invalid",
                        "stage": "elaboration",
                        "correctness_ok": None,
                        "candidate_valid": False,
                        "promotable": False,
                        "ppa": None,
                        "raw_refs": [],
                    }
                },
                "trace_transitions": {},
            }
            session_path.write_text(json.dumps(session))
            build_trace(campaign, root / "trace")
            dag = json.loads((root / "trace/CANDIDATE_DAG.json").read_text())
            ids = {row["candidate_id"] for row in dag["candidates"]}
            self.assertIn("fixture-fail", ids)
            transition_row = json.loads((root / "trace/RUN_TRACE.json").read_text())["transitions"][0]
            self.assertEqual(transition_row["selected_parent_id"], "fixture-fail")


if __name__ == "__main__":
    unittest.main()
