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

    def test_finalization_does_not_rewrite_search_cost_or_history(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            campaign = Path(raw)
            (campaign / "manifest.json").write_text(json.dumps({
                "kind": "formal", "schedule": [{"id": "T0-C-seed42"}],
            }))
            baseline_dir = campaign / "frozen/targets/T0"
            baseline_dir.mkdir(parents=True)
            (baseline_dir / "baseline-ppa.json").write_text(json.dumps({
                "critical_delay_ns": 10.0,
                "post_route_critical_delay_ns": 11.0,
            }))
            run_dir = campaign / "runs/T0-C-seed42"
            run_dir.mkdir(parents=True)
            state = {
                "target": "T0", "arm": "C", "seed": 42,
                "status": "search_complete", "model_calls": 2,
                "started_epoch": 100.0, "completed_epoch": 130.0,
                "model_infrastructure_failures": [{
                    "index": 2, "provider_tokens": 25,
                    "model_active_seconds": 2.0,
                }],
                "candidates": [{
                    "index": 1,
                    "provider_tokens": 50,
                    "model_active_seconds": 4.0,
                    "candidate": None,
                    "search_evaluation": {
                        "candidate_id": "invalid-1",
                        "status": "candidate_invalid",
                        "stage": "model",
                        "failure_class": "model_response_invalid",
                        "retryable": False,
                    },
                }, {
                    "index": 2,
                    "provider_tokens": 100,
                    "model_active_seconds": 3.0,
                    "search_active_seconds_total": 7.0,
                    "candidate": {
                        "id": "c1", "model_elapsed_seconds": 3.0,
                    },
                    "search_evaluation": {
                        "candidate_id": "c1", "candidate_valid": True,
                        "promotable": True, "active_seconds": 7.0,
                        "post_synth": {"critical_delay_ns": 9.0, "slice_luts": 10},
                    },
                }],
            }
            (run_dir / "run.json").write_text(json.dumps(state))
            before = campaign_report(campaign)["rows"][0]
            state["status"] = "complete"
            state["finalization_completed_epoch"] = 150.0
            state["first_final_acceptance_epoch"] = 140.0
            state["last_finalization_query_epoch"] = 160.0
            state["finalization"] = {
                "candidate_id": "c1",
                "evaluation": {
                    "candidate_id": "c1", "final_valid": True,
                    "valid_improvement": True, "active_seconds": 5.0,
                    "post_route": {"critical_delay_ns": 8.0, "slice_luts": 10},
                },
                "metadata": {"attempts": [{"attempt": 1, "active_seconds": 5.0}]},
            }
            (run_dir / "run.json").write_text(json.dumps(state))
            after = campaign_report(campaign)["rows"][0]
            for key in (
                "provider_tokens", "search_model_active_seconds",
                "search_eda_active_seconds", "search_active_seconds",
                "first_improvement_tokens", "first_improvement_active_seconds",
            ):
                self.assertEqual(before[key], after[key])
            self.assertEqual(after["finalization_active_seconds"], 5.0)
            self.assertEqual(after["provider_tokens"], 175)
            self.assertEqual(after["search_model_active_seconds"], 9.0)
            self.assertEqual(after["total_active_seconds"], 21.0)
            self.assertEqual(after["first_final_acceptance_wall_seconds"], 40.0)

    def test_failed_model_request_cost_is_not_lost_or_reported_as_zero(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            campaign = Path(raw)
            (campaign / "manifest.json").write_text(json.dumps({
                "kind": "formal", "schedule": [{"id": "T0-C-seed42"}],
            }))
            baseline_dir = campaign / "frozen/targets/T0"
            baseline_dir.mkdir(parents=True)
            (baseline_dir / "baseline-ppa.json").write_text(json.dumps({
                "post_route_critical_delay_ns": 11.0,
            }))
            run_dir = campaign / "runs/T0-C-seed42"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(json.dumps({
                "target": "T0", "arm": "C", "seed": 42,
                "status": "search_complete", "model_calls": 1,
                "started_epoch": 100.0, "completed_epoch": 130.0,
                "model_infrastructure_failures": [{
                    "index": 1, "provider_tokens": None,
                    "model_active_seconds": 2.5,
                }],
                "candidates": [{
                    "index": 1, "provider_tokens": 100,
                    "model_active_seconds": 3.0,
                    "candidate": {"id": "c1", "model_elapsed_seconds": 3.0},
                    "search_evaluation": {
                        "candidate_id": "c1", "candidate_valid": True,
                        "promotable": True, "active_seconds": 0.0,
                        "post_synth": {"critical_delay_ns": 9.0, "slice_luts": 10},
                    },
                }],
            }))
            row = campaign_report(campaign)["rows"][0]
            self.assertIsNone(row["provider_tokens"])
            self.assertFalse(row["provider_usage_complete"])
            self.assertIsNone(row["first_improvement_tokens"])
            self.assertEqual(row["first_improvement_active_seconds"], 5.5)
            self.assertEqual(row["search_model_active_seconds"], 5.5)


if __name__ == "__main__":
    unittest.main()
