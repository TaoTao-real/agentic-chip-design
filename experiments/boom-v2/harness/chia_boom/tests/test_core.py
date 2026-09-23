from __future__ import annotations

import subprocess
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
from pathlib import Path

from chia_boom.artifacts import CandidateArtifact, EvaluationArtifact
from chia_boom.core import (
    SYSTEM_PROMPT,
    apply_exact_edits,
    audit_prompt_for_answer_leak,
    build_schedule,
    build_iteration_digest,
    build_iteration_digest_rows,
    build_user_prompt,
    choose_parent,
    classify_failure,
    duplicate_candidate_id,
    evaluation_feedback,
    evaluation_feedback_rows,
    lineage_fields,
    update_validity,
    validate_config,
    validate_generic_episode_bundle,
)
from chia_boom.deepseek import DeepSeekOfficialLLM
from chia_boom.environment import load_config
from chia_boom.nodes import BoomCandidateEvaluationNode
from chia_boom.nodes import _differential_failure_message
from chia_boom.finalize import _verilator_run_passed


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/issueq-blind.example.json"


def experiment_config() -> dict:
    with mock.patch.dict(
        "os.environ",
        {
            "CONDA_SETUP": "/opt/conda/setup.sh",
            "VIVADO_SETTINGS": "/opt/vivado/settings64.sh",
            "AGENTIC_CHIP_LAB_ROOT": "/srv/agentic-chip-lab",
            "AGENTIC_CHIP_HARNESS_ROOT": "/srv/agentic-chip-harness",
            "CHIPYARD_WORKSPACE_ROOT": "/srv/chipyard-slots",
            "CHIPYARD_ROOT": "/srv/chipyard",
        },
    ):
        return load_config(CONFIG)


def candidate(index: int, diff: str = "+changed") -> CandidateArtifact:
    return CandidateArtifact(
        campaign_id="test", target="T1", arm="C", seed=42, index=index,
        parent_id=None, source="source", diff=diff,
        diagnosis=[{
            "evidence": "e", "source_region": "s", "hypothesis": "h",
            "predicted_effect": "p", "risk": "r",
        }], selected_hypothesis=0, visible_feedback="",
        request_sha256="a", response_sha256="b", usage={"total_tokens": 3},
    )


class CoreTests(unittest.TestCase):
    def test_config_and_schedule(self) -> None:
        config = experiment_config()
        validate_config(config, formal=True)
        schedule = build_schedule(config)
        self.assertEqual(len(schedule), 12)
        self.assertEqual({item.seed for item in schedule}, {42, 43, 44})

    def test_information_policy(self) -> None:
        target = experiment_config()["targets"]["T0-issue-queue"]
        a = build_user_prompt(arm="A", target=target, source="x", candidate_index=1)
        d = build_user_prompt(
            arm="D", target=target, source="x", candidate_index=2,
            timing="raw", feedback="prior", episodes="generic",
        )
        self.assertNotIn("FROZEN BASELINE TIMING", a)
        self.assertIn("PRIOR CANDIDATE FEEDBACK", d)
        self.assertIn("RETRIEVED DESIGN EPISODES", d)
        self.assertIn("Do not repeat an earlier edit set", d)

    def test_formal_memory_bundle_rejects_target_history(self) -> None:
        validate_generic_episode_bundle({
            "knowledge_class": "cross-target-process-memory",
            "access_policy": {"contains_target_specific_solution": False},
        })
        with self.assertRaisesRegex(ValueError, "not cross-target"):
            validate_generic_episode_bundle({
                "knowledge_class": "target-specific-solution",
                "access_policy": {"contains_target_specific_solution": True},
            })

    def test_edit_protocol_is_generic_and_contains_no_answer(self) -> None:
        self.assertIn("Each old string must occur exactly once", SYSTEM_PROMPT)
        self.assertIn("new is its complete replacement", SYSTEM_PROMPT)
        self.assertEqual(
            audit_prompt_for_answer_leak(SYSTEM_PROMPT, ["known-answer-token"]), []
        )

    def test_exact_edits_are_applied_without_interpretation(self) -> None:
        self.assertEqual(
            apply_exact_edits("alpha beta gamma", [{"old": "beta", "new": "delta"}]),
            "alpha delta gamma",
        )
        with self.assertRaisesRegex(ValueError, "occurs 2 times"):
            apply_exact_edits("same same", [{"old": "same", "new": "different"}])

    def test_duplicate_candidate_is_identified_before_eda(self) -> None:
        prior = candidate(1)
        prior.source = "same source"
        self.assertEqual(
            duplicate_candidate_id(
                "same source", [(prior, EvaluationArtifact(candidate_id=prior.id))]
            ),
            prior.id,
        )
        self.assertIsNone(duplicate_candidate_id("new source", [(prior, EvaluationArtifact(candidate_id=prior.id))]))

    def test_answer_leak_audit_ignores_raw_artifacts_but_checks_episodes(self) -> None:
        raw_only = """SOURCE
known-answer-token
END SOURCE
FROZEN BASELINE TIMING
known-answer-token
END TIMING
PRIOR CANDIDATE FEEDBACK
known-answer-token
END FEEDBACK
RETRIEVED DESIGN EPISODES
Preserve reset behavior.
END EPISODES"""
        self.assertEqual(
            audit_prompt_for_answer_leak(raw_only, ["known-answer-token"]), []
        )
        leaked_episode = raw_only.replace(
            "Preserve reset behavior.", "Use known-answer-token."
        )
        self.assertIn(
            "known-answer-token",
            audit_prompt_for_answer_leak(leaked_episode, ["known-answer-token"]),
        )

    def test_validity_states_are_separate(self) -> None:
        baseline = {
            "critical_delay_ns": 10.0, "post_route_critical_delay_ns": 11.0,
            "slice_luts": 100, "maximum_lut_ratio": 1.05,
        }
        evaluation = EvaluationArtifact(
            candidate_id="c", status="complete", build_ok=True, lint_ok=True,
            interface_ok=True, correctness_ok=True,
            post_synth={"critical_delay_ns": 10.5, "slice_luts": 101},
        )
        update_validity(evaluation, baseline)
        self.assertTrue(evaluation.candidate_valid)
        self.assertFalse(evaluation.promotable)
        self.assertFalse(evaluation.final_valid)
        evaluation.post_route = {"critical_delay_ns": 9.0, "slice_luts": 101}
        evaluation.final_valid = True
        update_validity(evaluation, baseline)
        self.assertTrue(evaluation.valid_improvement)

    def test_parent_requires_promotable(self) -> None:
        first = EvaluationArtifact(candidate_id="a", candidate_valid=True, promotable=False,
                                   post_synth={"critical_delay_ns": 7, "slice_luts": 90})
        second = EvaluationArtifact(candidate_id="b", candidate_valid=True, promotable=True,
                                    post_synth={"critical_delay_ns": 8, "slice_luts": 95})
        selected = choose_parent([(candidate(1), first), (candidate(2), second)], {})
        self.assertEqual(selected[0].index, 2)

    def test_feedback_preserves_diff_and_raw_error(self) -> None:
        evaluation = EvaluationArtifact(
            candidate_id="c", status="candidate_invalid", stage="elaboration",
            failure_class="candidate_elaboration_recursion",
            raw_error="java.lang.StackOverflowError at priorityMux",
        )
        text = evaluation_feedback([(candidate(1, "+recursive change"), evaluation)])
        self.assertIn("+recursive change", text)
        self.assertIn("StackOverflowError", text)
        digest = build_iteration_digest([(candidate(1, "+recursive change"), evaluation)])
        self.assertIn("candidate_elaboration_recursion", digest)
        self.assertIn("StackOverflowError", digest)
        self.assertIn("forbidden_repeat_diff_sha256", digest)

    def test_model_materialization_failure_enters_next_feedback(self) -> None:
        row = {
            "index": 2,
            "candidate": None,
            "model_proposal": {
                "diagnosis": [{"hypothesis": "generated"}],
                "edits": [{"old": "missing", "new": "replacement"}],
            },
            "evaluation": {
                "candidate_id": "candidate-02",
                "status": "candidate_invalid",
                "stage": "model",
                "failure_class": "model_response_invalid",
                "raw_error": "edit 1 old text occurs 0 times; expected exactly once",
            },
        }
        feedback = evaluation_feedback_rows([row])
        digest = build_iteration_digest_rows([row])
        self.assertIn("missing", feedback)
        self.assertIn("occurs 0 times", feedback)
        self.assertIn("occurs 0 times", digest)
        self.assertIn("forbidden_repeat_diff_or_proposal_sha256", digest)
        self.assertIn("exact_failed_diff_or_proposal", digest)
        self.assertIn("replacement", digest)

    def test_materialized_failure_digest_repeats_exact_diff_at_prompt_end(self) -> None:
        row = {
            "index": 1,
            "candidate": {
                "id": "candidate-01",
                "diff": "@@ -1 +1 @@\n-old\n+new\n",
            },
            "model_proposal": {
                "diagnosis": [{"hypothesis": "generated"}],
                "edits": [{"old": "old", "new": "new"}],
            },
            "evaluation": {
                "candidate_id": "candidate-01",
                "status": "candidate_invalid",
                "stage": "correctness",
                "failure_class": "candidate_correctness_failure",
                "raw_error": "MISMATCH cycle=27 port=ready",
            },
        }
        feedback = evaluation_feedback_rows([row])
        digest = build_iteration_digest_rows([row])
        self.assertIn('"old": "old"', feedback)
        self.assertIn("@@ -1 +1 @@", digest)
        self.assertIn("MISMATCH cycle=27", digest)

    def test_failure_taxonomy(self) -> None:
        self.assertEqual(classify_failure("Connection reset by peer"), ("infrastructure", True))
        self.assertEqual(classify_failure("java.lang.StackOverflowError")[0], "candidate_elaboration_recursion")
        self.assertEqual(classify_failure("candidate_source_unchanged")[0], "candidate_noop")

    def test_noop_evaluation_is_specific(self) -> None:
        config = experiment_config()
        with tempfile.TemporaryDirectory() as directory:
            result = BoomCandidateEvaluationNode.evaluate._chia_original(
                BoomCandidateEvaluationNode(),
                candidate=candidate(1, diff=""),
                target=config["targets"]["T0-issue-queue"], config=config,
                baseline={"critical_delay_ns": 1, "slice_luts": 1},
                output_dir=directory,
            )
        self.assertEqual(result.failure_class, "candidate_noop")
        self.assertEqual(result.raw_error, "candidate_source_unchanged")

    def test_differential_failure_preserves_simulator_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "run").mkdir()
            (output / "run/run.stdout").write_text(
                "MISMATCH cycle=27 port=io_dis_uops_3_ready\n"
            )
            (output / "run/run.stderr").write_text("Assertion failed\n")
            message = _differential_failure_message(
                output, "", "", {"returncode": -6, "passed": False}
            )
        self.assertIn("cycle=27", message)
        self.assertIn("io_dis_uops_3_ready", message)
        self.assertIn("Assertion failed", message)

    def test_deepseek_is_chia_native(self) -> None:
        options = DeepSeekOfficialLLM.prompt._chia_options
        self.assertEqual(options["resources"], {"deepseek_creds": 0.01})
        self.assertEqual(options["max_retries"], 0)

    def test_verilator_regression_uses_official_chia_success_contract(self) -> None:
        normal_rsort = SimpleNamespace(success=True, returncode=0, log="$finish")
        failed = SimpleNamespace(success=False, returncode=1, log="assertion failed")
        self.assertTrue(_verilator_run_passed(normal_rsort))
        self.assertFalse(_verilator_run_passed(failed))

    def test_lineage_separates_actual_parent_from_baseline(self) -> None:
        first = lineage_fields(
            parent_id=None,
            parent_source="base\n",
            baseline_source="base\n",
            candidate_source="first\n",
            mutable_file="Issue.scala",
        )
        second = lineage_fields(
            parent_id="candidate-01",
            parent_source="first\n",
            baseline_source="base\n",
            candidate_source="second\n",
            mutable_file="Issue.scala",
        )
        reverted = lineage_fields(
            parent_id=None,
            parent_source="base\n",
            baseline_source="base\n",
            candidate_source="third\n",
            mutable_file="Issue.scala",
        )
        self.assertIn("-first", second["diff"])
        self.assertIn("+second", second["diff"])
        self.assertIn("-base", second["baseline_diff"])
        self.assertEqual(reverted["parent_id"], None)
        self.assertEqual(reverted["diff"], reverted["baseline_diff"])
        self.assertNotEqual(first["parent_source_sha256"], second["parent_source_sha256"])
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "Issue.scala").write_text("first\n")
            patch = root / "candidate.patch"
            patch.write_text(second["diff"])
            subprocess.run(
                ["git", "apply", str(patch)], cwd=root,
                text=True, capture_output=True, check=True,
            )
            self.assertEqual((root / "Issue.scala").read_text(), "second\n")


if __name__ == "__main__":
    unittest.main()
