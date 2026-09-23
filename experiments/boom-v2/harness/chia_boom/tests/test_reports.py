from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from chia_boom.campaign import campaign_report, paired_loop_analysis, preflight_analysis


class ReportTests(unittest.TestCase):
    @staticmethod
    def row(target: str, seed: int, arm: str, delay: float, efficiency: float) -> dict:
        return {
            "target": target, "seed": seed, "arm": arm,
            "best_post_route_delay_ns": delay,
            "valid_candidates": 5, "candidate_count": 5,
            "valid_improvement": True,
            "active_time_efficiency_ns_per_hour": 1.0,
            "token_efficiency_ns_per_million": efficiency,
        }

    def test_pair_gate(self) -> None:
        rows = []
        for target in ("T1", "T2"):
            for seed in (42, 43, 44):
                common = {
                    "valid_candidates": 5, "candidate_count": 5,
                    "valid_improvement": True,
                    "active_time_efficiency_ns_per_hour": 1.0,
                }
                rows.append({
                    **common, "target": target, "seed": seed, "arm": "B",
                    "best_post_route_delay_ns": 10.0,
                    "token_efficiency_ns_per_million": 1.0,
                })
                rows.append({
                    **common, "target": target, "seed": seed, "arm": "C",
                    "best_post_route_delay_ns": 9.0,
                    "token_efficiency_ns_per_million": 2.0,
                })
        result = paired_loop_analysis(rows)
        self.assertEqual(result["wins"], 6)
        self.assertTrue(result["primary_pair_gates_pass"])
        self.assertEqual(len(result["bootstrap_median_95_percent"]), 2)

    def test_single_target_blind_reproduction_pair_count(self) -> None:
        rows = []
        for seed in (42, 43, 44):
            rows.append(self.row("T0", seed, "B", 10.0, 1.0))
            rows.append(self.row("T0", seed, "C", 9.0, 2.0))
        result = paired_loop_analysis(rows)
        self.assertEqual(result["expected_pairs"], 3)
        self.assertEqual(result["required_wins"], 2)
        self.assertTrue(result["primary_pair_gates_pass"])

    def test_campaign_report_loads_each_target_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            campaign = Path(raw)
            manifest = {
                "kind": "formal",
                "schedule": [{"id": "T0-B-seed42"}],
            }
            (campaign / "manifest.json").write_text(json.dumps(manifest))
            baseline_dir = campaign / "frozen/targets/T0"
            baseline_dir.mkdir(parents=True)
            (baseline_dir / "baseline-ppa.json").write_text(json.dumps({
                "post_route_critical_delay_ns": 28.962,
            }))
            run_dir = campaign / "runs/T0-B-seed42"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(json.dumps({
                "target": "T0", "arm": "B", "seed": 42,
                "status": "search_complete", "model_calls": 0,
                "candidates": [],
            }))
            report = campaign_report(campaign)
            self.assertEqual(
                report["rows"][0]["baseline_post_route_delay_ns"], 28.962
            )

    def test_preflight_requires_real_feedback_and_linked_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            campaign = Path(raw)
            manifest = {
                "kind": "preflight",
                "manual_candidate_edits_allowed": False,
                "config": {"targets": {"T0": {}}},
            }
            (campaign / "CHIA_PROFILE.json").write_text("[]")

            def candidate(arm: str, index: int, feedback: str = "") -> dict:
                return {
                    "id": f"T0-{arm}-seed41-candidate-{index:02d}",
                    "index": index,
                    "diff": f"diff-{arm}-{index}",
                    "source": f"source-{arm}-{index}",
                    "visible_feedback": feedback,
                    "request_sha256": "request",
                    "response_sha256": "response",
                }

            valid_eval = {
                "candidate_valid": True,
                "correctness_ok": True,
                "failure_class": None,
                "raw_error": "",
            }
            b1 = candidate("B", 1)
            c1 = candidate("C", 1)
            c1_feedback = json.dumps([{
                "candidate_id": c1["id"],
                "candidate_diff": c1["diff"],
                "agent_diagnosis": [],
                "evaluation": valid_eval,
            }])
            c2 = candidate("C", 2, c1_feedback)
            states = {
                "B": {"id": "T0-B-seed41", "status": "search_complete", "candidates": [
                    {"index": 1, "candidate": b1, "evaluation": valid_eval}
                ]},
                "C": {"id": "T0-C-seed41", "status": "search_complete", "candidates": [
                    {"index": 1, "candidate": c1, "evaluation": valid_eval},
                    {"index": 2, "candidate": c2, "evaluation": valid_eval},
                ]},
            }
            for arm, state in states.items():
                run_dir = campaign / "runs" / state["id"]
                run_dir.mkdir(parents=True)
                (run_dir / "run.json").write_text(json.dumps(state))
                for row in state["candidates"]:
                    item = run_dir / f"candidate-{row['index']:02d}"
                    (item / "model").mkdir(parents=True)
                    (item / "model/request.json").write_text("{}")
                    (item / "model/provider-metadata.json").write_text("{}")
                    (item / "candidate.json").write_text("{}")
                    (item / "evaluation.json").write_text("{}")
                    vivado = item / "evaluation-attempt-01/vivado"
                    vivado.mkdir(parents=True)
                    (vivado / "post_synth_timing_summary.rpt").write_text("timing")
            result = preflight_analysis(campaign, manifest)
            self.assertTrue(result["passed"])
            self.assertTrue(result["targets"]["T0"]["gates"][
                "c_feedback_contains_full_prior_diff_and_evaluation"
            ])


if __name__ == "__main__":
    unittest.main()
