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
        self.assertEqual(result["wns_ns"], -1.25)
        self.assertEqual(result["critical_delay_ns"], 6.25)
        self.assertEqual(result["tns_ns"], -3.75)
        self.assertEqual(result["failing_endpoints"], 3)
        self.assertEqual(result["total_endpoints"], 42)
        self.assertEqual(result["slice_luts"], 1234)
        self.assertEqual(result["slice_registers"], 567)

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
        self.assertEqual(result["facts"]["timing_paths"], [])
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

    def test_nonfinite_or_wrong_typed_json_is_rejected(self) -> None:
        value = json.loads((self.input / "differential-result.json").read_text())
        value["cycles"] = float("nan")
        document = SourceDocument(
            "1" * 64,
            "2" * 64,
            json.dumps(value).encode(),
        )
        with self.assertRaisesRegex(SchemaError, "finite"):
            extract_differential(result=document, stdout=None)


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


if __name__ == "__main__":
    unittest.main()
