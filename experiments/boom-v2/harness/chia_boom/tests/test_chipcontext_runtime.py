from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from chia_boom.artifacts import (
    CandidateArtifact,
    EvaluationArtifact,
    dump_json,
    sha256_file,
)
from chia_boom.chipcontext.runtime import (
    RuntimeContext,
    build_structured_feedback,
    prepare_runtime_context,
    query_runtime_context,
)
from chia_boom.chipcontext.schema import content_hash


FIXTURES = Path(__file__).parents[1] / "chipcontext" / "fixtures" / "extraction"


def candidate(index: int, source: str, arm: str = "E1") -> CandidateArtifact:
    return CandidateArtifact(
        campaign_id="runtime-smoke",
        target="NeutralQueue",
        arm=arm,
        seed=41,
        index=index,
        parent_id=None,
        source=source,
        diff=f"synthetic-diff-{index}",
        diagnosis=[{
            "evidence": "synthetic runtime smoke",
            "source_region": "Neutral.scala",
            "hypothesis": "neutral transformation",
            "predicted_effect": "exercise feedback",
            "risk": "checked by synthetic evidence",
        }],
        selected_hypothesis=0,
        visible_feedback="",
        request_sha256="a" * 64,
        response_sha256="b" * 64,
        usage={"total_tokens": 10},
    )


class RuntimeBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "campaign"
        target = self.root / "frozen/targets/NeutralQueue"
        qualification = self.root / "frozen/qualification"
        target.mkdir(parents=True)
        qualification.mkdir(parents=True)
        dump_json(target / "baseline-ppa.json", {
            "stage": "post_synth",
            "clock_period_ns": 5.0,
            "critical_delay_ns": 7.0,
            "slice_luts": 1200,
        })
        dump_json(qualification / "QUALIFICATION.json", {
            "passed": True,
            "tool_versions": {"vivado": "synthetic-2024.1"},
        })
        body = {
            "schema_version": "frozen-run-v1",
            "contract": {
                "physical": {
                    "part": "synthetic-part",
                    "clock_period_ns": 5.0,
                    "tool": "Vivado",
                }
            },
            "files": {
                "targets/NeutralQueue/baseline-ppa.json": sha256_file(
                    target / "baseline-ppa.json"
                ),
                "qualification/QUALIFICATION.json": sha256_file(
                    qualification / "QUALIFICATION.json"
                ),
            },
        }
        dump_json(
            self.root / "frozen/FROZEN_RUN_MANIFEST.json",
            body | {"fingerprint": content_hash(body)},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(self, index: int, source: str, arm: str = "E1"):
        artifact = candidate(index, source, arm)
        directory = self.root / arm / "evaluations" / f"candidate-{index:02d}"
        evidence = directory / "evaluation-attempt-01"
        (evidence / "vivado").mkdir(parents=True)
        (evidence / "differential/run").mkdir(parents=True)
        shutil.copy2(
            FIXTURES / "post_synth_timing_summary.rpt",
            evidence / "vivado/post_synth_timing_summary.rpt",
        )
        shutil.copy2(
            FIXTURES / "post_synth_utilization.rpt",
            evidence / "vivado/post_synth_utilization.rpt",
        )
        shutil.copy2(
            FIXTURES / "post_synth_timing_paths.rpt",
            evidence / "vivado/post_synth_timing_paths.rpt",
        )
        dump_json(evidence / "differential/run/result.json", {
            "cycles": 1_000_000,
            "completed_cycles": 1_000_000,
            "directed_phases": ["idle", "dispatch"],
            "passed": True,
            "returncode": 0,
            "scenario": "NeutralQueue",
            "seed": 41,
        })
        (evidence / "differential/driver.stdout").write_text(
            "PASS cycles=1000000 seed=41 scenario=NeutralQueue\n"
        )
        evaluation = EvaluationArtifact(
            candidate_id=artifact.id,
            status="complete",
            stage="synthesis",
            build_ok=True,
            interface_ok=True,
            correctness_ok=True,
            candidate_valid=True,
            promotable=True,
            post_synth={
                "clock_period_ns": 5.0,
                "critical_delay_ns": 6.25,
                "wns_ns": -1.25,
                "tns_ns": -3.75,
                "failing_endpoints": 3,
                "total_endpoints": 42,
                "slice_luts": 1234,
                "slice_registers": 567,
            },
            differential={
                "cycles": 1_000_000,
                "completed_cycles": 1_000_000,
                "directed_phases": ["idle", "dispatch"],
                "passed": True,
                "returncode": 0,
                "scenario": "NeutralQueue",
                "seed": 41,
            },
            stages=[
                {"stage": "elaboration", "status": "complete", "success": True},
                {"stage": "correctness", "status": "complete", "success": True},
                {"stage": "synthesis", "status": "complete", "success": True},
            ],
        )
        dump_json(directory / "candidate.json", artifact)
        dump_json(directory / "result.json", {
            "candidate": artifact.to_dict(),
            "search_evaluation": evaluation.to_dict(),
            "evaluation_attempts": [],
            "infrastructure_attempts": 1,
        })
        context = prepare_runtime_context(
            campaign_root=self.root,
            candidate_dir=directory,
            candidate=artifact,
            evaluation=evaluation,
            target_id="NeutralQueue",
            infrastructure_attempts=1,
        )
        return artifact, evaluation, directory, context

    def test_prepare_push_query_and_resume_are_one_evidence_chain(self) -> None:
        artifact, evaluation, directory, context = self.prepare(
            1, "class NeutralQueueV1\n"
        )
        feedback = build_structured_feedback(context, working_source=artifact.source)
        self.assertEqual(feedback["applicability"]["status"], "current")
        self.assertEqual(
            [row["status"] for row in feedback["metrics"]["comparisons"]],
            ["comparable", "comparable"],
        )
        self.assertEqual(
            feedback["timing_paths"]["report_first"]["fact"]["rank"], 1
        )
        self.assertTrue(feedback["raw_artifacts"])
        request = json.loads((directory / "chipcontext-request.json").read_text())
        self.assertTrue(request["artifacts"])
        self.assertTrue(all(
            "evaluation-attempt-01/" in row["path"]
            for row in request["artifacts"]
        ))
        self.assertEqual(feedback["cost"]["prepare"]["model_calls"], 0)
        self.assertEqual(feedback["cost"]["prepare"]["eda_calls"], 0)
        agent_payload = {key: value for key, value in feedback.items() if key != "cost"}
        self.assertLess(len(json.dumps(agent_payload, sort_keys=True).encode()), 12 * 1024)
        repeated = build_structured_feedback(context, working_source=artifact.source)
        self.assertEqual(feedback["content_hash"], repeated["content_hash"])

        restored = prepare_runtime_context(
            campaign_root=self.root,
            candidate_dir=directory,
            candidate=artifact,
            evaluation=evaluation,
            target_id="NeutralQueue",
            infrastructure_attempts=1,
        )
        self.assertEqual(restored, context)
        self.assertEqual(
            RuntimeContext.from_dict(
                json.loads((directory / "chipcontext-context.json").read_text())
            ),
            context,
        )

    def test_zero_model_two_round_smoke_keeps_identity_and_history(self) -> None:
        first, _, _, first_context = self.prepare(1, "class NeutralQueueV1\n")
        second, _, _, second_context = self.prepare(2, "class NeutralQueueV2\n")
        historical = query_runtime_context(
            first_context,
            working_source=second.source,
            operation="candidate_status",
        )["answer"]
        current = query_runtime_context(
            second_context,
            working_source=second.source,
            operation="candidate_status",
        )["answer"]
        self.assertEqual(historical["applicability"]["status"], "historical")
        self.assertEqual(current["applicability"]["status"], "current")
        self.assertNotEqual(first_context.snapshot_ref, second_context.snapshot_ref)
        self.assertNotEqual(first_context.attempt_id, second_context.attempt_id)

        listing = query_runtime_context(
            first_context,
            working_source=first.source,
            operation="candidate_artifacts",
            parameters={"kinds": ["post_synth_timing_paths"], "limit": 10},
        )["answer"]
        ref = listing["result"]["artifacts"][0]["content_ref"]
        page = query_runtime_context(
            first_context,
            working_source=first.source,
            operation="read_artifact",
            parameters={
                "artifact_ref": ref,
                "start_line": 1,
                "line_count": 8,
                "limit_bytes": 4096,
            },
        )["answer"]
        self.assertIn("report_timing", page["result"]["content"])

    def test_e0_e1_raw_evidence_capability_is_matched(self) -> None:
        e0, _, _, e0_context = self.prepare(1, "class NeutralQueueV1\n", "E0")
        e1, _, _, e1_context = self.prepare(1, "class NeutralQueueV1\n", "E1")
        inventories = []
        for artifact, context in ((e0, e0_context), (e1, e1_context)):
            answer = query_runtime_context(
                context,
                working_source=artifact.source,
                operation="candidate_artifacts",
                parameters={"limit": 100},
            )["answer"]
            inventories.append({
                (row["kind"], row["stage"], row["media_type"])
                for row in answer["result"]["artifacts"]
            })
        self.assertEqual(inventories[0], inventories[1])


if __name__ == "__main__":
    unittest.main()
