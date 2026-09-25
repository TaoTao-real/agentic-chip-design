from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from chia_boom.chipcontext.extraction import ExtractionService
from chia_boom.chipcontext.extractors import (
    SourceDocument,
    extract_differential,
    extract_vivado,
    parse_ppa_bytes,
)
from chia_boom.chipcontext.schema import SchemaError
from chia_boom.chipcontext.service import ChipContextService
from chia_boom.chipcontext.store import EvidenceStore


FIXTURE = Path(__file__).parents[1] / "chipcontext" / "fixtures" / "extraction"
OWNER = "0" * 64


class ExtractorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input = self.root / "input"
        shutil.copytree(FIXTURE, self.input)
        self.store = EvidenceStore(self.root / "store", {"input": self.input})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register(self, name: str, kind: str | None = None):
        return self.store.register_artifact(
            self.input / name,
            root_id="input",
            kind=kind or name,
            owner_ref=OWNER,
            attempt_id="attempt-01",
        )

    def vivado_record(self):
        timing = self.register(
            "post_synth_timing_summary.rpt", "post_synth_timing_summary"
        )
        utilization = self.register(
            "post_synth_utilization.rpt", "post_synth_utilization"
        )
        paths = self.register(
            "post_synth_timing_paths.rpt", "post_synth_timing_paths"
        )
        return ExtractionService(self.store).vivado(
            timing_summary_ref=timing.ref_id,
            utilization_ref=utilization.ref_id,
            timing_paths_ref=paths.ref_id,
            period_ns=5.0,
            stage="post_synth",
        )


class VivadoExtractorTests(ExtractorTestCase):
    def test_ppa_parser_preserves_scoring_fields(self) -> None:
        result = parse_ppa_bytes(
            (self.input / "post_synth_timing_summary.rpt").read_bytes(),
            (self.input / "post_synth_utilization.rpt").read_bytes(),
            5.0,
        )
        # Frozen from main@bb39ffd's independent nodes.py parser. This is not
        # computed through the new parsing core, so compatibility drift cannot
        # make both sides of the assertion change together.
        self.assertEqual(result, {
            "clock_period_ns": 5.0,
            "wns_ns": -1.25,
            "critical_delay_ns": 6.25,
            "tns_ns": -3.75,
            "failing_endpoints": 3,
            "total_endpoints": 42,
            "slice_luts": 1234,
            "slice_registers": 567,
            "timing_report_sha256": (
                "804df99ac66108d27307296fcfa1b7f713df7419e3545aaa55a86c043739401b"
            ),
            "utilization_report_sha256": (
                "9b338c72cca4eef99e2740ae872b3593c9f62e9129b9ac5af3115b3b847e4c90"
            ),
        })

    def test_ambiguous_duplicate_narrows_legacy_first_match_behavior(self) -> None:
        utilization = (
            self.input / "post_synth_utilization.rpt"
        ).read_bytes() + b"| Slice LUTs* | 999 | 0 |\n"
        with self.assertRaisesRegex(ValueError, "cannot parse"):
            parse_ppa_bytes(
                (self.input / "post_synth_timing_summary.rpt").read_bytes(),
                utilization,
                5.0,
            )

    def test_content_addressed_extraction_and_worst_path_drilldown(self) -> None:
        record = self.vivado_record()
        reread = self.store.read_record("extractions", record["content_hash"])
        self.assertEqual(reread, record)
        metrics = {
            item["metric_id"]: item for item in record["facts"]["metrics"]
        }
        self.assertEqual(metrics["critical_delay_ns"]["value"], 6.25)
        self.assertEqual(record["coverage"]["timing_paths"]["requested_max_paths"], 2)
        self.assertEqual(record["coverage"]["timing_paths"]["returned_path_count"], 2)

        answer = ExtractionService(self.store).worst_timing_path(
            record["content_hash"]
        )
        self.assertEqual(answer["availability"], "available")
        self.assertEqual(answer["fact"]["rank"], 1)
        self.assertEqual(answer["fact"]["slack_ns"], -1.25)
        location = answer["fact"]["source_location"]
        page = self.store.read_artifact(
            location["artifact_ref"],
            start_line=location["line_span"]["start"],
            line_count=(
                location["line_span"]["end"]
                - location["line_span"]["start"] + 1
            ),
        )
        self.assertIn("Slack (VIOLATED)", page["content"])
        self.assertIn("grant_reg[0]/D", page["content"])

    def test_hashes_are_stable_in_fresh_stores(self) -> None:
        first = self.vivado_record()["content_hash"]
        second_store = EvidenceStore(self.root / "store-2", {"input": self.input})
        refs = {}
        for name in (
            "post_synth_timing_summary.rpt",
            "post_synth_utilization.rpt",
            "post_synth_timing_paths.rpt",
        ):
            refs[name] = second_store.register_artifact(
                self.input / name,
                root_id="input",
                kind=name.removesuffix(".rpt"),
                owner_ref=OWNER,
                attempt_id="attempt-01",
            )
        second = ExtractionService(second_store).vivado(
            timing_summary_ref=refs["post_synth_timing_summary.rpt"].ref_id,
            utilization_ref=refs["post_synth_utilization.rpt"].ref_id,
            timing_paths_ref=refs["post_synth_timing_paths.rpt"].ref_id,
            period_ns=5.0,
            stage="post_synth",
        )["content_hash"]
        self.assertEqual(first, second)

    def test_missing_and_truncated_fields_are_explicit(self) -> None:
        timing = SourceDocument("1" * 64, "2" * 64, b"unknown report\n")
        utilization = SourceDocument("3" * 64, "4" * 64, b"unknown report\n")
        paths = SourceDocument(
            "5" * 64,
            "6" * 64,
            b"Slack (VIOLATED) : -1.000ns\n  Source: a/C\n",
        )
        result = extract_vivado(
            timing_summary=timing,
            utilization=utilization,
            timing_paths=paths,
            period_ns=5.0,
            stage="post_synth",
        )
        self.assertEqual(result["facts"]["metrics"], [])
        self.assertEqual(len(result["facts"]["timing_paths"]), 1)
        self.assertEqual(
            result["facts"]["timing_paths"][0]["availability"], "partial"
        )
        fields = {item["field"] for item in result["missing"]}
        self.assertIn("vivado.timing_summary", fields)
        self.assertIn("vivado.slice_luts", fields)
        self.assertIn("timing_paths[1]", fields)

    def test_disagreeing_duplicate_utilization_field_is_a_conflict(self) -> None:
        timing = SourceDocument(
            "1" * 64,
            "2" * 64,
            (self.input / "post_synth_timing_summary.rpt").read_bytes(),
        )
        utilization = SourceDocument(
            "3" * 64,
            "4" * 64,
            (
                self.input / "post_synth_utilization.rpt"
            ).read_bytes() + b"| Slice LUTs* | 999 | 0 |\n",
        )
        result = extract_vivado(
            timing_summary=timing,
            utilization=utilization,
            timing_paths=None,
            period_ns=5.0,
            stage="post_synth",
        )
        self.assertTrue(any(
            item["field"] == "vivado.slice_luts"
            for item in result["conflicts"]
        ))
        self.assertNotIn(
            "slice_luts",
            {item["metric_id"] for item in result["facts"]["metrics"]},
        )
        conflict = next(
            item for item in result["conflicts"]
            if item["field"] == "vivado.slice_luts"
        )
        self.assertEqual(conflict["affected_metrics"], ["slice_luts"])

    def test_partial_rank_one_remains_the_worst_path(self) -> None:
        path = self.input / "post_synth_timing_paths.rpt"
        path.write_bytes(path.read_bytes().replace(
            b"  Logic Levels:           7  (LUT5=1 LUT6=6)\n",
            b"",
            1,
        ))
        record = self.vivado_record()
        answer = ExtractionService(self.store).worst_timing_path(
            record["content_hash"]
        )
        self.assertEqual(answer["availability"], "partial")
        self.assertEqual(answer["fact"]["rank"], 1)
        self.assertEqual(answer["fact"]["slack_ns"], -1.25)
        self.assertIsNone(answer["fact"]["logic_levels"])
        self.assertEqual(answer["coverage"]["parse_status"], "partial")

    def test_malformed_path_report_is_not_reported_as_not_collected(self) -> None:
        (self.input / "post_synth_timing_paths.rpt").write_text(
            "Timing Report\nSource: state_reg/C\nDestination: grant_reg/D\n"
        )
        record = self.vivado_record()
        answer = ExtractionService(self.store).worst_timing_path(
            record["content_hash"]
        )
        self.assertEqual(answer["availability"], "parse_failed")
        self.assertIsNone(answer["fact"])
        self.assertEqual(answer["coverage"]["parse_status"], "parse_failed")

    def test_absent_path_report_is_not_collected(self) -> None:
        timing = self.register(
            "post_synth_timing_summary.rpt", "post_synth_timing_summary"
        )
        utilization = self.register(
            "post_synth_utilization.rpt", "post_synth_utilization"
        )
        record = ExtractionService(self.store).vivado(
            timing_summary_ref=timing.ref_id,
            utilization_ref=utilization.ref_id,
            timing_paths_ref=None,
            period_ns=5.0,
            stage="post_synth",
        )
        answer = ExtractionService(self.store).worst_timing_path(
            record["content_hash"]
        )
        self.assertEqual(answer["availability"], "not_collected")
        self.assertIsNone(answer["fact"])

    def test_registered_file_replacement_is_rejected(self) -> None:
        ref = self.register("post_synth_timing_summary.rpt")
        (self.input / "post_synth_timing_summary.rpt").write_text("replacement\n")
        with self.assertRaisesRegex(RuntimeError, "content hash changed"):
            self.store.verified_artifact_bytes(ref.ref_id)

    def test_record_tampering_is_rejected(self) -> None:
        record = self.vivado_record()
        path = (
            self.root / "store" / "records" / "extractions"
            / f"{record['content_hash']}.json"
        )
        value = json.loads(path.read_text())
        value["coverage"]["stage"] = "post_route"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError, "content hash"):
            self.store.read_record("extractions", record["content_hash"])

    def test_extraction_query_rechecks_controlled_access(self) -> None:
        refs = []
        for name, kind in (
            ("post_synth_timing_summary.rpt", "post_synth_timing_summary"),
            ("post_synth_utilization.rpt", "post_synth_utilization"),
            ("post_synth_timing_paths.rpt", "post_synth_timing_paths"),
        ):
            refs.append(self.store.register_artifact(
                self.input / name,
                root_id="input",
                kind=kind,
                owner_ref=OWNER,
                attempt_id="controlled-attempt",
                access="controlled",
            ))
        record = ExtractionService(
            self.store, allowed_access={"controlled"}
        ).vivado(
            timing_summary_ref=refs[0].ref_id,
            utilization_ref=refs[1].ref_id,
            timing_paths_ref=refs[2].ref_id,
            period_ns=5.0,
            stage="post_synth",
        )
        with self.assertRaises(PermissionError):
            ExtractionService(self.store).worst_timing_path(
                record["content_hash"]
            )


class DifferentialExtractorTests(ExtractorTestCase):
    def extract(self, value: dict, stdout: bytes | None) -> dict:
        return extract_differential(
            result=SourceDocument(
                "1" * 64,
                "2" * 64,
                json.dumps(value).encode(),
            ),
            stdout=(
                SourceDocument("3" * 64, "4" * 64, stdout)
                if stdout is not None else None
            ),
        )

    def test_failure_fields_and_missing_expected_actual_are_grounded(self) -> None:
        result = self.register("differential-result.json", "differential_result")
        stdout = self.register("differential.stdout", "differential_stdout")
        record = ExtractionService(self.store).differential(
            result_ref=result.ref_id,
            stdout_ref=stdout.ref_id,
        )
        facts = record["facts"]
        self.assertEqual(facts["check"]["outcome"], "fail")
        self.assertEqual(facts["first_mismatch"]["cycle"], 28)
        self.assertEqual(facts["first_mismatch"]["signal"], "io_output_ready")
        self.assertIsNone(facts["first_mismatch"]["expected"])
        self.assertEqual(
            {item["field"] for item in record["missing"]},
            {
                "differential.first_mismatch.expected",
                "differential.first_mismatch.actual",
                "differential.completed_cycles",
            },
        )

    def test_json_stdout_conflict_is_inconclusive(self) -> None:
        value = json.loads((self.input / "differential-result.json").read_text())
        value["passed"] = True
        value["returncode"] = 0
        (self.input / "differential-result.json").write_text(json.dumps(value))
        result = self.register("differential-result.json", "differential_result")
        stdout = self.register("differential.stdout", "differential_stdout")
        record = ExtractionService(self.store).differential(
            result_ref=result.ref_id,
            stdout_ref=stdout.ref_id,
        )
        self.assertEqual(record["facts"]["check"]["outcome"], "inconclusive")
        self.assertTrue(record["conflicts"])

    def test_normal_pass_has_exact_completed_coverage(self) -> None:
        value = {
            "cycles": 100,
            "seed": 41,
            "scenario": "normal-pass",
            "directed_phases": ["idle", "dispatch"],
            "returncode": 0,
            "interface_ok": True,
            "passed": True,
        }
        result = self.extract(value, b"PASS cycles=100\n")
        self.assertEqual(result["facts"]["check"]["outcome"], "pass")
        self.assertEqual(result["facts"]["completed_cycles"], 100)
        self.assertFalse(result["conflicts"])

    def test_crash_without_mismatch_is_inconclusive(self) -> None:
        value = {
            "cycles": 100,
            "seed": 41,
            "scenario": "crash",
            "directed_phases": ["idle"],
            "returncode": -11,
            "passed": False,
        }
        result = self.extract(value, b"Segmentation fault\n")
        self.assertEqual(
            result["facts"]["check"]["outcome"], "inconclusive"
        )
        self.assertEqual(
            result["facts"]["failure_class"],
            "infrastructure_or_unclassified",
        )

    def test_missing_stdout_does_not_claim_completed_cycles(self) -> None:
        value = {
            "cycles": 100,
            "seed": 41,
            "scenario": "missing-stdout",
            "directed_phases": ["idle"],
            "returncode": 0,
            "passed": True,
        }
        result = self.extract(value, None)
        self.assertEqual(result["facts"]["check"]["outcome"], "pass")
        self.assertIsNone(result["facts"]["completed_cycles"])
        self.assertTrue(any(
            item["field"] == "differential.completed_cycles"
            for item in result["missing"]
        ))

    def test_registered_stdout_without_pass_marker_is_inconclusive(self) -> None:
        value = {
            "cycles": 100,
            "seed": 41,
            "scenario": "missing-pass-marker",
            "directed_phases": ["idle"],
            "returncode": 0,
            "passed": True,
        }
        result = self.extract(value, b"simulation exited normally\n")
        self.assertEqual(
            result["facts"]["check"]["outcome"], "inconclusive"
        )
        self.assertIsNone(result["facts"]["completed_cycles"])

    def test_mismatching_pass_count_is_inconclusive(self) -> None:
        value = {
            "cycles": 100,
            "seed": 41,
            "scenario": "bad-pass-count",
            "directed_phases": ["idle"],
            "returncode": 0,
            "passed": True,
        }
        result = self.extract(value, b"PASS cycles=7\n")
        self.assertEqual(
            result["facts"]["check"]["outcome"], "inconclusive"
        )
        self.assertIsNone(result["facts"]["completed_cycles"])

    def test_interface_mismatch_preserves_not_run_behavior(self) -> None:
        value = {
            "cycles": 0,
            "seed": 41,
            "scenario": "interface-mismatch",
            "passed": False,
            "interface_ok": False,
            "failure_class": "candidate_interface_mismatch",
            "baseline_signature": {"input": 1},
            "candidate_signature": {"input": 2},
        }
        result = self.extract(value, None)
        checks = {item["check_id"]: item for item in result["facts"]["checks"]}
        self.assertEqual(checks["interface_signature"]["outcome"], "fail")
        self.assertFalse(checks["differential_correctness"]["executed"])
        self.assertIsNone(checks["differential_correctness"]["outcome"])
        self.assertIsNone(result["facts"]["return_code"])
        self.assertEqual(result["coverage"]["simulator_execution"], "not_run")

    def test_noninteger_counters_are_rejected_without_truncation(self) -> None:
        value = json.loads((self.input / "differential-result.json").read_text())
        for field in ("cycles", "seed"):
            for bad in (3.75, float("nan"), True):
                with self.subTest(field=field, bad=bad):
                    changed = dict(value)
                    changed[field] = bad
                    with self.assertRaisesRegex(
                        SchemaError, "non-negative integer"
                    ):
                        self.extract(changed, None)


class PreparationIntegrationTests(ExtractorTestCase):
    def _raw_success_fixture(self) -> Path:
        fixture = self.root / "success"
        shutil.copytree(FIXTURE.parent / "success", fixture)
        (fixture / "raw-summary.rpt").write_text(
            "WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints\n"
            "------- ------- --------------------- -------------------\n"
            "-4.000 -12.000 2 128\n"
        )
        (fixture / "raw-utilization.rpt").write_text(
            "| Slice LUTs* | 102 | 0 |\n"
            "| Slice Registers | 80 | 0 |\n"
        )
        shutil.copy(
            FIXTURE / "post_synth_timing_paths.rpt",
            fixture / "raw-paths.rpt",
        )
        request = json.loads((fixture / "request.json").read_text())
        request["artifacts"].extend([
            {
                "name": "post_synth_timing_summary",
                "kind": "post_synth_timing_summary",
                "path": "raw-summary.rpt",
            },
            {
                "name": "post_synth_utilization",
                "kind": "post_synth_utilization",
                "path": "raw-utilization.rpt",
            },
            {
                "name": "post_synth_timing_paths",
                "kind": "post_synth_timing_paths",
                "path": "raw-paths.rpt",
            },
        ])
        (fixture / "request.json").write_text(json.dumps(request, indent=2))
        return fixture

    def test_prepare_reconciles_matching_raw_and_legacy_metrics(self) -> None:
        fixture = self._raw_success_fixture()
        store = EvidenceStore(self.root / "prepared", {"input": fixture})
        result = ChipContextService(store).prepare(fixture / "request.json")
        self.assertTrue(result.bundle["facts"]["comparable"])
        self.assertEqual(len(result.snapshot["extraction_refs"]), 1)
        self.assertEqual(
            len(result.snapshot["measurement_sources"]["critical_delay_ns"]), 2
        )
        self.assertFalse(any(
            item["field"] == "measurements.critical_delay_ns"
            for item in result.packet["conflicts"]
        ))

    def test_raw_legacy_conflict_prevents_affected_delta(self) -> None:
        fixture = self._raw_success_fixture()
        summary = fixture / "raw-summary.rpt"
        summary.write_text(summary.read_text().replace("-4.000", "-5.000", 1))
        store = EvidenceStore(self.root / "conflict", {"input": fixture})
        result = ChipContextService(store).prepare(fixture / "request.json")
        deltas = {
            item["metric_id"] for item in result.bundle["facts"]["deltas"]
        }
        self.assertNotIn("wns_ns", deltas)
        self.assertNotIn("critical_delay_ns", deltas)
        self.assertTrue(any(
            item["field"] == "measurements.critical_delay_ns"
            for item in result.packet["conflicts"]
        ))

    def test_internal_duplicate_lut_conflict_prevents_only_lut_delta(self) -> None:
        fixture = self._raw_success_fixture()
        utilization = fixture / "raw-utilization.rpt"
        utilization.write_text(
            utilization.read_text() + "| Slice LUTs* | 999 | 0 |\n"
        )
        result = ChipContextService(
            EvidenceStore(self.root / "duplicate-lut", {"input": fixture})
        ).prepare(fixture / "request.json")
        deltas = {item["metric_id"] for item in result.bundle["facts"]["deltas"]}
        self.assertNotIn("slice_luts", deltas)
        self.assertIn("critical_delay_ns", deltas)
        self.assertIn("slice_registers", deltas)

    def test_duplicate_timing_summary_suppresses_timing_deltas(self) -> None:
        fixture = self._raw_success_fixture()
        summary = fixture / "raw-summary.rpt"
        summary.write_text(
            summary.read_text()
            + "WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints\n"
            + "------- ------- --------------------- -------------------\n"
            + "-5.000 -15.000 3 128\n"
        )
        result = ChipContextService(
            EvidenceStore(self.root / "duplicate-summary", {"input": fixture})
        ).prepare(fixture / "request.json")
        deltas = {item["metric_id"] for item in result.bundle["facts"]["deltas"]}
        self.assertTrue({
            "wns_ns", "tns_ns", "failing_endpoints",
            "total_endpoints", "critical_delay_ns",
        }.isdisjoint(deltas))
        self.assertIn("slice_luts", deltas)
        self.assertIn("slice_registers", deltas)

    def test_prepare_accepts_interface_failure_producer_shape(self) -> None:
        fixture = self.root / "interface-failure"
        shutil.copytree(FIXTURE.parent / "failure", fixture)
        raw = {
            "cycles": 0,
            "seed": 43,
            "scenario": "synthetic-interface-mismatch",
            "passed": False,
            "interface_ok": False,
            "failure_class": "candidate_interface_mismatch",
            "baseline_signature": {"input": "UInt<4>"},
            "candidate_signature": {"input": "UInt<5>"},
        }
        (fixture / "raw-differential.json").write_text(json.dumps(raw))
        legacy_result = json.loads((fixture / "result.json").read_text())
        evaluation = legacy_result["search_evaluation"]
        evaluation["differential"] = raw
        evaluation["interface_ok"] = False
        evaluation["failure_class"] = "candidate_interface_mismatch"
        (fixture / "result.json").write_text(json.dumps(legacy_result, indent=2))
        request = json.loads((fixture / "request.json").read_text())
        request["artifacts"].append({
            "name": "differential_result",
            "kind": "differential_result",
            "path": "raw-differential.json",
        })
        (fixture / "request.json").write_text(json.dumps(request, indent=2))
        result = ChipContextService(
            EvidenceStore(self.root / "interface-store", {"input": fixture})
        ).prepare(fixture / "request.json")
        checks = {
            item["check_id"]: item for item in result.snapshot["checks"]
        }
        self.assertEqual(checks["interface_signature"]["outcome"], "fail")
        self.assertFalse(checks["differential_correctness"]["executed"])
        self.assertIsNone(checks["differential_correctness"]["outcome"])


if __name__ == "__main__":
    unittest.main()
