from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chia_boom.chipcontext.schema import (
    CheckRecord,
    Measurement,
    SchemaError,
    content_hash,
)
from chia_boom.chipcontext.service import ChipContextService
from chia_boom.chipcontext.store import MAX_LIMIT_BYTES, EvidenceStore


FIXTURES = Path(__file__).parents[1] / "chipcontext" / "fixtures"


class ChipContextTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def copy_fixture(self, name: str, destination: str = "input") -> Path:
        target = self.root / destination
        shutil.copytree(FIXTURES / name, target)
        return target

    def prepare(self, fixture: Path, output_name: str = "evidence"):
        output = self.root / output_name
        store = EvidenceStore(output, {"input": fixture})
        result = ChipContextService(store).prepare(fixture / "request.json")
        return result, store, output

    @staticmethod
    def read_json(path: Path) -> dict:
        return json.loads(path.read_text())

    @staticmethod
    def write_json(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


class FixtureVerticalSliceTests(ChipContextTestCase):
    def test_success_and_failure_fixtures_are_stable_across_fresh_runs(self) -> None:
        for fixture_name in ("success", "failure"):
            fixture = self.copy_fixture(fixture_name, fixture_name)
            summaries = []
            markdown = []
            for index in range(3):
                result, _, output = self.prepare(
                    fixture, f"{fixture_name}-evidence-{index}"
                )
                summaries.append(result.summary())
                markdown.append((output / "context.md").read_text())
            self.assertEqual(summaries[0], summaries[1])
            self.assertEqual(summaries[1], summaries[2])
            self.assertEqual(markdown[0], markdown[1])
            self.assertEqual(markdown[1], markdown[2])
            self.assertEqual(
                summaries[0], self.read_json(fixture / "expected.json")
            )
            self.assertEqual(
                markdown[0], (fixture / "expected-context.md").read_text()
            )

    def test_failure_summary_drills_back_to_original_line_span(self) -> None:
        fixture = self.copy_fixture("failure")
        result, store, _ = self.prepare(fixture)
        ref_id = result.bundle["facts"]["raw_error_ref"]
        page = store.read_artifact(ref_id, start_line=2, line_count=4)
        self.assertEqual(page["artifact_ref"]["ref_id"], (
            "ca836449fa682d03ba297386bbb412d2c37e3e054c207e5e19edf2bcbe7547c2"
        ))
        self.assertEqual(page["span"]["start_line"], 2)
        self.assertEqual(page["span"]["end_line"], 5)
        self.assertIn("cycle=17", page["content"])
        self.assertIn("expected=0x2a", page["content"])

    def test_failure_summary_accepts_stdout_as_the_raw_failure_source(self) -> None:
        fixture = self.copy_fixture("failure")
        request = self.read_json(fixture / "request.json")
        request["artifacts"][0]["name"] = "differential_stdout"
        request["artifacts"][0]["kind"] = "differential_stdout"
        self.write_json(fixture / "request.json", request)
        result, store, _ = self.prepare(fixture)
        ref_id = result.bundle["facts"]["raw_error_ref"]
        self.assertEqual(store.artifact(ref_id).kind, "differential_stdout")

    def test_success_bundle_contains_strictly_comparable_delta(self) -> None:
        fixture = self.copy_fixture("success")
        result, _, _ = self.prepare(fixture)
        self.assertEqual(result.bundle["kind"], "performance_delta")
        self.assertTrue(result.bundle["facts"]["comparable"])
        deltas = {
            row["metric_id"]: row for row in result.bundle["facts"]["deltas"]
        }
        self.assertEqual(deltas["critical_delay_ns"]["delta"], -1.0)
        self.assertEqual(deltas["slice_luts"]["delta"], 2)


class IdentityAndStateTests(ChipContextTestCase):
    def test_candidate_id_is_namespaced_by_experiment(self) -> None:
        fixture_a = self.copy_fixture("success", "input-a")
        fixture_b = self.copy_fixture("success", "input-b")
        request = self.read_json(fixture_b / "request.json")
        request["experiment_id"] = "another-campaign"
        self.write_json(fixture_b / "request.json", request)
        candidate = self.read_json(fixture_b / "candidate.json")
        candidate["campaign_id"] = "another-campaign"
        self.write_json(fixture_b / "candidate.json", candidate)
        result_a, _, _ = self.prepare(fixture_a, "out-a")
        result_b, _, _ = self.prepare(fixture_b, "out-b")
        self.assertNotEqual(
            result_a.packet["candidate_ref"]["ref_id"],
            result_b.packet["candidate_ref"]["ref_id"],
        )

    def test_same_source_under_new_contract_has_new_identity(self) -> None:
        fixture_a = self.copy_fixture("success", "input-a")
        fixture_b = self.copy_fixture("success", "input-b")
        manifest = self.read_json(fixture_b / "FROZEN_RUN_MANIFEST.json")
        manifest["contract"]["physical"]["maximum_lut_ratio"] = 1.04
        body = {key: value for key, value in manifest.items() if key != "fingerprint"}
        manifest["fingerprint"] = content_hash(body)
        self.write_json(fixture_b / "FROZEN_RUN_MANIFEST.json", manifest)
        result_a, _, _ = self.prepare(fixture_a, "out-a")
        result_b, _, _ = self.prepare(fixture_b, "out-b")
        self.assertNotEqual(
            result_a.packet["candidate_ref"]["ref_id"],
            result_b.packet["candidate_ref"]["ref_id"],
        )

    def test_edited_working_source_is_explicitly_not_evaluated(self) -> None:
        fixture = self.copy_fixture("success")
        (fixture / "working-source.scala").write_text(
            (fixture / "working-source.scala").read_text() + "// new edit\n"
        )
        result, _, _ = self.prepare(fixture)
        self.assertEqual(
            result.packet["working_state"]["state"], "evaluated_other_revision"
        )
        self.assertEqual(result.packet["current_evaluation_status"], "not_evaluated")
        self.assertEqual(
            result.packet["bundle"]["measurement_scope"],
            "last_evaluated_candidate",
        )
        self.assertFalse(result.bundle["facts"]["comparable"])
        self.assertEqual(result.bundle["facts"]["deltas"], [])
        self.assertIn("Working state: `not_evaluated`", result.markdown)

    def test_candidate_and_evaluation_ids_must_match(self) -> None:
        fixture = self.copy_fixture("success")
        result = self.read_json(fixture / "result.json")
        result["search_evaluation"]["candidate_id"] = "different-candidate"
        self.write_json(fixture / "result.json", result)
        with self.assertRaisesRegex(SchemaError, "candidate ID"):
            self.prepare(fixture)

    def test_request_and_candidate_experiment_ids_must_match(self) -> None:
        fixture = self.copy_fixture("success")
        request = self.read_json(fixture / "request.json")
        request["experiment_id"] = "wrong-campaign"
        self.write_json(fixture / "request.json", request)
        with self.assertRaisesRegex(SchemaError, "candidate campaign"):
            self.prepare(fixture)


class SemanticsAndComparabilityTests(ChipContextTestCase):
    def test_unexecuted_check_cannot_report_an_outcome(self) -> None:
        with self.assertRaisesRegex(SchemaError, "unexecuted"):
            CheckRecord("lint", False, "fail", {}, None)
        record = CheckRecord("lint", False, None, {}, None)
        self.assertIsNone(record.outcome)

    def test_unexecuted_legacy_lint_is_not_reported_as_failure(self) -> None:
        fixture = self.copy_fixture("success")
        result, _, _ = self.prepare(fixture)
        lint = next(row for row in result.snapshot["checks"] if row["check_id"] == "lint")
        self.assertFalse(lint["executed"])
        self.assertIsNone(lint["outcome"])

    def test_early_failure_does_not_imply_elaboration_was_executed(self) -> None:
        fixture = self.copy_fixture("failure")
        value = self.read_json(fixture / "result.json")
        evaluation = value["search_evaluation"]
        evaluation["build_ok"] = False
        evaluation["stage"] = "materialize"
        evaluation["stages"] = [
            {"stage": "materialize", "success": False, "status": "complete"}
        ]
        self.write_json(fixture / "result.json", value)
        result, _, _ = self.prepare(fixture)
        elaboration = next(
            row for row in result.snapshot["checks"]
            if row["check_id"] == "elaboration"
        )
        self.assertFalse(elaboration["executed"])
        self.assertIsNone(elaboration["outcome"])

    def test_conflicting_check_sources_are_explicitly_inconclusive(self) -> None:
        fixture = self.copy_fixture("success")
        value = self.read_json(fixture / "result.json")
        value["search_evaluation"]["correctness_ok"] = False
        self.write_json(fixture / "result.json", value)
        result, _, _ = self.prepare(fixture)
        correctness = next(
            row for row in result.snapshot["checks"]
            if row["check_id"] == "differential_correctness"
        )
        self.assertTrue(correctness["executed"])
        self.assertEqual(correctness["outcome"], "inconclusive")
        self.assertEqual(result.snapshot["completeness"]["conflict_count"], 1)
        self.assertEqual(result.packet["completeness"]["conflict_count"], 1)
        self.assertEqual(
            result.packet["conflicts"][0]["field"],
            "checks.differential_correctness",
        )
        self.assertIn("## Conflicting evidence", result.markdown)

    def test_unknown_stage_is_preserved_and_marked_unsupported(self) -> None:
        fixture = self.copy_fixture("success")
        value = self.read_json(fixture / "result.json")
        value["search_evaluation"]["stage"] = "future_stage"
        self.write_json(fixture / "result.json", value)
        result, _, _ = self.prepare(fixture)
        self.assertEqual(result.evaluation_manifest["raw_stage"], "future_stage")
        self.assertEqual(result.evaluation_manifest["normalized_stage"], "unknown")
        self.assertTrue(any(
            row["field"] == "evaluation.normalized_stage"
            and row["reason"] == "not_supported"
            for row in result.packet["missing"]
        ))

    def test_synthesis_maps_to_post_synth_while_retaining_raw_stage(self) -> None:
        fixture = self.copy_fixture("success")
        result, _, _ = self.prepare(fixture)
        self.assertEqual(result.evaluation_manifest["raw_stage"], "synthesis")
        self.assertEqual(result.evaluation_manifest["normalized_stage"], "post_synth")

    def test_stage_or_unit_change_prevents_delta(self) -> None:
        stage_fixture = self.copy_fixture("success", "stage")
        baseline = self.read_json(stage_fixture / "baseline-ppa.json")
        baseline["stage"] = "post_route"
        self.write_json(stage_fixture / "baseline-ppa.json", baseline)
        stage_result, _, _ = self.prepare(stage_fixture, "stage-out")
        self.assertFalse(stage_result.bundle["facts"]["comparable"])
        self.assertEqual(stage_result.bundle["facts"]["deltas"], [])

        unit_fixture = self.copy_fixture("success", "unit")
        baseline = self.read_json(unit_fixture / "baseline-ppa.json")
        baseline["critical_delay_ns"] = {
            "value": 10.0,
            "unit": "cycles",
            "definition_revision": "timing-critical-delay-v1",
        }
        self.write_json(unit_fixture / "baseline-ppa.json", baseline)
        unit_result, _, _ = self.prepare(unit_fixture, "unit-out")
        self.assertFalse(unit_result.bundle["facts"]["comparable"])
        self.assertEqual(unit_result.bundle["facts"]["deltas"], [])

    def test_missing_tool_identity_prevents_delta(self) -> None:
        fixture = self.copy_fixture("success")
        qualification = self.read_json(fixture / "QUALIFICATION.json")
        qualification.pop("tool_versions")
        self.write_json(fixture / "QUALIFICATION.json", qualification)
        result, _, _ = self.prepare(fixture)
        self.assertFalse(result.bundle["facts"]["comparable"])
        self.assertEqual(result.bundle["facts"]["deltas"], [])
        self.assertTrue(any(
            row["field"] == "evaluation.tool_fingerprint"
            for row in result.packet["missing"]
        ))

    def test_missing_metric_is_explicit_and_not_fabricated(self) -> None:
        fixture = self.copy_fixture("success")
        value = self.read_json(fixture / "result.json")
        value["search_evaluation"]["post_synth"] = {}
        self.write_json(fixture / "result.json", value)
        result, _, _ = self.prepare(fixture)
        self.assertEqual(result.snapshot["measurements"], [])
        self.assertTrue(any(
            row["field"] == "measurements.post_synth"
            and row["reason"] == "parse_failed"
            for row in result.packet["missing"]
        ))

    def test_nan_infinity_and_unknown_units_are_rejected(self) -> None:
        for index, value in enumerate((math.nan, math.inf, -math.inf)):
            fixture = self.copy_fixture("success", f"nonfinite-{index}")
            result = self.read_json(fixture / "result.json")
            result["search_evaluation"]["post_synth"]["critical_delay_ns"] = value
            self.write_json(fixture / "result.json", result)
            with self.assertRaises((SchemaError, ValueError)):
                self.prepare(fixture, f"nonfinite-out-{index}")

        fixture = self.copy_fixture("success", "bad-unit")
        baseline = self.read_json(fixture / "baseline-ppa.json")
        baseline["critical_delay_ns"] = {
            "value": 10.0,
            "unit": "fortnights",
            "definition_revision": "timing-critical-delay-v1",
        }
        self.write_json(fixture / "baseline-ppa.json", baseline)
        with self.assertRaisesRegex(SchemaError, "unknown measurement unit"):
            self.prepare(fixture, "bad-unit-out")

    def test_partial_json_is_rejected_as_parse_failure(self) -> None:
        fixture = self.copy_fixture("failure")
        (fixture / "result.json").write_text('{"search_evaluation":')
        with self.assertRaisesRegex(SchemaError, "cannot read evaluation"):
            self.prepare(fixture)

    def test_parser_revision_changes_the_evidence_cache_key(self) -> None:
        fixture = self.copy_fixture("success")
        result_a, _, _ = self.prepare(fixture, "out-a")
        with mock.patch(
            "chia_boom.chipcontext.service.PARSER_REVISION",
            "legacy-evaluation-v2-test",
        ):
            result_b, _, _ = self.prepare(fixture, "out-b")
        self.assertNotEqual(result_a.snapshot["content_hash"], result_b.snapshot["content_hash"])
        self.assertNotEqual(
            result_a.evaluation_manifest["content_hash"],
            result_b.evaluation_manifest["content_hash"],
        )


class StoreAndBudgetTests(ChipContextTestCase):
    def test_prepare_is_idempotent_in_one_store(self) -> None:
        fixture = self.copy_fixture("success")
        first, store, output = self.prepare(fixture)
        second = ChipContextService(store).prepare(fixture / "request.json")
        self.assertEqual(first.summary(), second.summary())
        self.assertGreaterEqual(len(list((output / "events").glob("*.json"))), 2)

    def test_queries_are_bounded_and_paginated(self) -> None:
        fixture = self.copy_fixture("failure")
        result, store, _ = self.prepare(fixture)
        ref_id = result.bundle["facts"]["raw_error_ref"]
        first = store.read_artifact(ref_id, cursor=0, limit_bytes=16)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["next_cursor"], 16)
        second = store.read_artifact(
            ref_id, cursor=first["next_cursor"], limit_bytes=16
        )
        self.assertEqual(second["span"]["start_byte"], 16)
        with self.assertRaisesRegex(SchemaError, "limit_bytes"):
            store.read_artifact(ref_id, limit_bytes=MAX_LIMIT_BYTES + 1)
        with self.assertRaises(PermissionError):
            store.read_artifact(ref_id, allowed_access=set())

    def test_unknown_reference_and_tampered_metadata_are_rejected(self) -> None:
        fixture = self.copy_fixture("failure")
        result, store, output = self.prepare(fixture)
        with self.assertRaises(KeyError):
            store.read_artifact("0" * 64)
        ref_id = result.bundle["facts"]["raw_error_ref"]
        ref_path = output / "artifacts" / f"{ref_id}.json"
        metadata = self.read_json(ref_path)
        metadata["kind"] = "tampered"
        self.write_json(ref_path, metadata)
        with self.assertRaisesRegex(RuntimeError, "content hash"):
            store.read_artifact(ref_id)

    def test_traversal_and_symlink_escape_are_rejected(self) -> None:
        fixture = self.copy_fixture("failure")
        outside = self.root / "outside.log"
        outside.write_text("outside\n")
        request = self.read_json(fixture / "request.json")
        request["artifacts"].append({
            "name": "escape", "path": "../outside.log", "kind": "raw"
        })
        self.write_json(fixture / "request.json", request)
        with self.assertRaisesRegex(SchemaError, "escapes"):
            self.prepare(fixture, "traversal-out")

        fixture = self.copy_fixture("failure", "symlink")
        (fixture / "escape.log").symlink_to(outside)
        request = self.read_json(fixture / "request.json")
        request["artifacts"].append({
            "name": "escape", "path": "escape.log", "kind": "raw"
        })
        self.write_json(fixture / "request.json", request)
        with self.assertRaisesRegex(SchemaError, "symlink"):
            self.prepare(fixture, "symlink-out")

    def test_controlled_artifact_requires_explicit_access(self) -> None:
        fixture = self.copy_fixture("failure")
        request = self.read_json(fixture / "request.json")
        request["access"] = "controlled"
        request["artifacts"][0]["access"] = "controlled"
        self.write_json(fixture / "request.json", request)
        result, store, _ = self.prepare(fixture)
        ref_id = result.bundle["facts"]["raw_error_ref"]
        with self.assertRaises(PermissionError):
            store.read_artifact(ref_id)
        page = store.read_artifact(ref_id, allowed_access={"controlled"})
        self.assertIn("mismatch", page["content"])

    def test_required_evidence_overflow_fails_closed(self) -> None:
        fixture = self.copy_fixture("success")
        request = self.read_json(fixture / "request.json")
        request["policy"]["max_payload_bytes"] = 64
        self.write_json(fixture / "request.json", request)
        with self.assertRaisesRegex(SchemaError, "required_evidence_overflow"):
            self.prepare(fixture)

    def test_model_credentials_are_not_needed_or_serialized(self) -> None:
        fixture = self.copy_fixture("success")
        marker = "must-not-appear-in-chipcontext-output"
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": marker}):
            _, _, output = self.prepare(fixture)
        rendered = "\n".join(
            path.read_text(errors="replace")
            for path in output.rglob("*") if path.is_file()
        )
        self.assertNotIn(marker, rendered)
        probe = subprocess.run(
            [
                os.sys.executable,
                "-c",
                (
                    "import sys; import chia_boom.chipcontext; "
                    "print('ray' in sys.modules, "
                    "'chia_boom.deepseek' in sys.modules)"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "DEEPSEEK_API_KEY": marker},
        )
        self.assertEqual(probe.stdout.strip(), "False False")


class SchemaTests(unittest.TestCase):
    def test_measurements_require_known_units_and_finite_values(self) -> None:
        common = {
            "metric_id": "critical_delay_ns",
            "definition_revision": "timing-critical-delay-v1",
            "scope": {},
            "provenance": "test",
            "source_ref": "0" * 64,
            "availability": "available",
        }
        with self.assertRaises(SchemaError):
            Measurement(value=math.nan, unit="ns", **common)
        with self.assertRaises(SchemaError):
            Measurement(value=1.0, unit="unknown", **common)


if __name__ == "__main__":
    unittest.main()
