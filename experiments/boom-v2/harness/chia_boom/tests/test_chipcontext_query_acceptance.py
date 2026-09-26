from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HARNESS = Path(__file__).parents[2]
EXAMPLES = HARNESS / "examples"
if str(EXAMPLES) not in sys.path:
    sys.path.insert(0, str(EXAMPLES))

from chipcontext_query_acceptance import (  # noqa: E402
    AcceptanceError,
    load_corpus,
    rebuild_summary_from_run,
    run_acceptance,
    run_cli,
    verify_case,
)
from chipcontext_query_acceptance_fixture import build_acceptance_fixture  # noqa: E402


class ChipContextAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.suite = self.root / "suite"
        self.fixture_summary = build_acceptance_fixture(self.suite)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_suite(self, name: str = "run", *, mode: str = "both"):
        return run_acceptance(
            corpus_path=self.suite / "corpus.json",
            registry=self.suite / "trusted-stores.json",
            mode=mode,
            output=self.root / name,
            require_approved_oracle=False,
            code_version="test",
        )

    def test_six_case_api_and_cli_vertical_slice_passes_with_approved_oracle(self) -> None:
        summary = self.run_suite()
        self.assertEqual(summary["counts"], {"pass": 6, "fail": 0, "blocked": 0})
        self.assertEqual(summary["expected_rejection_cases"], 3)
        self.assertEqual(summary["expected_rejection_attempts"], 6)
        self.assertEqual(summary["unexpected_rejection_attempts"], 0)
        self.assertTrue(summary["formal_eligible"])
        self.assertEqual(summary["oracle_review_status"], "approved")
        attempts = [
            path for path in (self.root / "run" / "attempts").glob("*.json")
            if not path.name.endswith(".audit.json")
        ]
        self.assertEqual(len(attempts), 12)
        manifest = json.loads((self.root / "run" / "run-manifest.json").read_text())
        self.assertEqual(
            manifest["oracle_origin_validation"],
            {"origin_count": 17, "located_span_count": 16},
        )

    def test_summary_rebuilds_from_case_results(self) -> None:
        summary = self.run_suite(mode="api")
        self.assertEqual(rebuild_summary_from_run(self.root / "run"), summary)

    def test_formal_run_passes_with_maintainer_oracle_approval(self) -> None:
        summary = run_acceptance(
            corpus_path=self.suite / "corpus.json",
            registry=self.suite / "trusted-stores.json",
            mode="both",
            output=self.root / "formal",
            require_approved_oracle=True,
            code_version="test",
        )
        self.assertEqual(summary["counts"], {"pass": 6, "fail": 0, "blocked": 0})
        self.assertTrue(summary["formal_eligible"])
        self.assertEqual(summary["oracle_review_status"], "approved")

    def test_wrong_expected_fact_and_removed_conflict_are_detected(self) -> None:
        corpus = load_corpus(self.suite / "corpus.json")
        normal = next(case for case in corpus["cases"] if case["case_id"] == "comparison-normal")
        targets = {
            "answer": {
                "result": {"comparisons": [{"delta": 0.0}]},
                "conflicts": [],
            },
            "error": None,
            "audit": {"status": "success"},
        }
        passed, assertions = verify_case(normal, query_status="success", targets=targets)
        self.assertFalse(passed)
        self.assertTrue(any(not row["passed"] for row in assertions))

        conflict = next(
            case for case in corpus["cases"]
            if case["case_id"] == "comparison-raw-legacy-conflict"
        )
        conflict_targets = {
            "answer": {
                "result": {
                    "all_comparable": True,
                    "any_comparable": True,
                    "comparisons": [
                        {"status": "comparable", "delta": -1.0},
                        {"status": "comparable", "delta": 2},
                    ],
                },
                "conflicts": [],
            },
            "error": None,
            "audit": {"status": "success"},
        }
        passed, _ = verify_case(
            conflict, query_status="success", targets=conflict_targets
        )
        self.assertFalse(passed)

    def test_cli_exit_one_cannot_satisfy_a_rejection_oracle(self) -> None:
        corpus = load_corpus(self.suite / "corpus.json")
        rejection = next(
            case for case in corpus["cases"]
            if case["case_id"] == "permission-controlled-denied"
        )
        audit_path = self.root / "unexpected-exit.audit.json"
        audit = {
            "schema_version": "chipcontext.query-attempt.v1",
            "status": "rejected",
            "operation": "candidate_status",
            "answer_ref": None,
            "error_code": "permission_denied",
            "output": {
                "format": "json",
                "max_output_bytes": 65536,
                "attempted_output_bytes": 0,
                "returned_bytes": 0,
            },
            "cost": {"wall_time_ns": 1},
        }

        def fake_run(*_args, **_kwargs):
            audit_path.write_text(json.dumps(audit))
            return subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr=json.dumps({
                    "error": "permission_denied",
                    "message": "synthetic crash output",
                }),
            )

        with mock.patch(
            "chipcontext_query_acceptance.subprocess.run", side_effect=fake_run
        ):
            observed = run_cli(
                self.suite / "trusted-stores.json",
                self.suite / rejection["request"]["path"],
                rejection["execution"],
                "json",
                audit_path,
                30,
            )
        self.assertEqual(observed["query_status"], "runner_error")
        self.assertEqual(observed["exit_code"], 1)
        passed, _ = verify_case(
            rejection,
            query_status=observed["query_status"],
            targets={"answer": None, "error": None, "audit": observed["audit"]},
        )
        self.assertFalse(passed)

    def test_request_and_oracle_source_tampering_fail_before_execution(self) -> None:
        request = self.suite / "requests" / "acceptance" / "normal-comparison.json"
        request.write_text(request.read_text() + " ")
        with self.assertRaisesRegex(AcceptanceError, "request hash"):
            self.run_suite("request-tampered", mode="api")
        self.assertFalse((self.root / "request-tampered").exists())

        # Restore the suite, then damage a cited raw source.
        self.suite = self.root / "suite-2"
        build_acceptance_fixture(self.suite)
        source = (
            self.suite
            / "oracle-sources/chia_boom/chipcontext/fixtures/success/result.json"
        )
        source.write_text(source.read_text().replace('"slice_luts": 102', '"slice_luts": 103'))
        with self.assertRaisesRegex(AcceptanceError, "origin hash"):
            self.run_suite("origin-tampered", mode="api")
        self.assertFalse((self.root / "origin-tampered").exists())

    def test_output_directory_is_immutable(self) -> None:
        self.run_suite(mode="api")
        with self.assertRaisesRegex(AcceptanceError, "already exists"):
            self.run_suite(mode="api")

    def test_fixture_and_review_hashes_are_stable(self) -> None:
        other = self.root / "other"
        second = build_acceptance_fixture(other)
        self.assertEqual(self.fixture_summary, second)
        corpus_hash = hashlib.sha256((self.suite / "corpus.json").read_bytes()).hexdigest()
        review = json.loads((self.suite / "ORACLE_REVIEW.json").read_text())
        self.assertEqual(review["corpus_sha256"], corpus_hash)


if __name__ == "__main__":
    unittest.main()
