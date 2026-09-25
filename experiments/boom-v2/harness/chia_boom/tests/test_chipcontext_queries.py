from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from chia_boom.chipcontext.query import (
    ChipContextQueryService,
    EvidenceHandle,
    QueryError,
    QueryScope,
    TrustedStoreRegistry,
)
from chia_boom.chipcontext.schema import CandidateRef, SchemaError, content_hash
from chia_boom.chipcontext.service import ChipContextService
from chia_boom.chipcontext.store import EvidenceStore


FIXTURES = Path(__file__).parents[1] / "chipcontext" / "fixtures"


class QueryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(
        self,
        fixture_name: str = "success",
        *,
        store_id: str = "primary",
        request_access: str = "public",
        destination: str | None = None,
    ):
        suffix = destination or store_id
        fixture = self.root / f"input-{suffix}"
        shutil.copytree(FIXTURES / fixture_name, fixture)
        if request_access != "public":
            request = json.loads((fixture / "request.json").read_text())
            request["access"] = request_access
            (fixture / "request.json").write_text(
                json.dumps(request, indent=2, sort_keys=True) + "\n"
            )
        store = EvidenceStore(self.root / f"store-{suffix}", {"input": fixture})
        prepared = ChipContextService(store).prepare(fixture / "request.json")
        candidate = CandidateRef(**{
            key: value
            for key, value in prepared.snapshot["candidate_ref"].items()
            if key != "ref_id"
        })
        scope = QueryScope(
            EvidenceHandle(store_id, prepared.snapshot["content_hash"]),
            expected_candidate=candidate,
            expected_attempt_id=prepared.evaluation_manifest["attempt_id"],
            working_source_sha256=candidate.source_sha256,
        )
        return fixture, store, prepared, candidate, scope

    @staticmethod
    def service(store_id: str, store: EvidenceStore, *levels: str):
        return ChipContextQueryService(
            {store_id: store}, {store_id: set(levels or ("public",))}
        )

    def prepare_raw(
        self,
        *,
        drop_legacy_metric: bool = False,
        raw_check_only: bool = False,
        request_access: str = "public",
        destination: str = "raw",
        raw_wns: float = -4.0,
        extra_utilization: str = "",
        timing_path_content: str | None = None,
        include_timing_paths: bool = True,
    ):
        fixture = self.root / f"input-{destination}"
        shutil.copytree(FIXTURES / "success", fixture)
        (fixture / "raw-summary.rpt").write_text(
            "WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints\n"
            "------- ------- --------------------- -------------------\n"
            f"{raw_wns:.3f} -12.000 2 128\n"
        )
        (fixture / "raw-utilization.rpt").write_text(
            "| Slice LUTs* | 102 | 0 |\n"
            "| Slice Registers | 80 | 0 |\n"
            + extra_utilization
        )
        if include_timing_paths:
            if timing_path_content is None:
                shutil.copy(
                    FIXTURES / "extraction" / "post_synth_timing_paths.rpt",
                    fixture / "raw-paths.rpt",
                )
            else:
                (fixture / "raw-paths.rpt").write_text(timing_path_content)
        request = json.loads((fixture / "request.json").read_text())
        request["access"] = request_access
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
        ])
        if include_timing_paths:
            request["artifacts"].append({
                "name": "post_synth_timing_paths",
                "kind": "post_synth_timing_paths",
                "path": "raw-paths.rpt",
            })
        result = json.loads((fixture / "result.json").read_text())
        evaluation = result["search_evaluation"]
        if drop_legacy_metric:
            evaluation["post_synth"].pop("tns_ns")
        if raw_check_only:
            raw = {
                "cycles": 100,
                "seed": 41,
                "scenario": "raw-only-pass",
                "directed_phases": ["idle", "dispatch"],
                "returncode": 0,
                "interface_ok": True,
                "passed": True,
            }
            evaluation.pop("differential", None)
            evaluation["stages"] = [
                row for row in evaluation["stages"]
                if row["stage"] != "correctness"
            ]
            (fixture / "raw-differential.json").write_text(json.dumps(raw))
            (fixture / "raw-differential.stdout").write_text("PASS cycles=100\n")
            request["artifacts"].extend([
                {
                    "name": "differential_result",
                    "kind": "differential_result",
                    "path": "raw-differential.json",
                },
                {
                    "name": "differential_stdout",
                    "kind": "differential_stdout",
                    "path": "raw-differential.stdout",
                },
            ])
        (fixture / "result.json").write_text(json.dumps(result, indent=2))
        (fixture / "request.json").write_text(json.dumps(request, indent=2))
        store = EvidenceStore(self.root / f"store-{destination}", {"input": fixture})
        prepared = ChipContextService(store).prepare(fixture / "request.json")
        candidate = CandidateRef(**{
            key: value
            for key, value in prepared.snapshot["candidate_ref"].items()
            if key != "ref_id"
        })
        scope = QueryScope(
            EvidenceHandle(destination, prepared.snapshot["content_hash"]),
            expected_candidate=candidate,
            expected_attempt_id=prepared.evaluation_manifest["attempt_id"],
            working_source_sha256=candidate.source_sha256,
        )
        return fixture, store, prepared, candidate, scope

    def prepare_failure_raw(self, *, destination: str = "raw-failure"):
        fixture = self.root / f"input-{destination}"
        shutil.copytree(FIXTURES / "failure", fixture)
        shutil.copy(
            FIXTURES / "extraction" / "differential-result.json",
            fixture / "raw-differential.json",
        )
        shutil.copy(
            FIXTURES / "extraction" / "differential.stdout",
            fixture / "raw-differential.stdout",
        )
        request = json.loads((fixture / "request.json").read_text())
        request["artifacts"].extend([
            {
                "name": "differential_result",
                "kind": "differential_result",
                "path": "raw-differential.json",
            },
            {
                "name": "differential_stdout",
                "kind": "differential_stdout",
                "path": "raw-differential.stdout",
            },
        ])
        (fixture / "request.json").write_text(json.dumps(request, indent=2))
        store = EvidenceStore(self.root / f"store-{destination}", {"input": fixture})
        prepared = ChipContextService(store).prepare(fixture / "request.json")
        candidate = CandidateRef(**{
            key: value for key, value in prepared.snapshot["candidate_ref"].items()
            if key != "ref_id"
        })
        scope = QueryScope(
            EvidenceHandle(destination, prepared.snapshot["content_hash"]),
            expected_candidate=candidate,
            expected_attempt_id=prepared.evaluation_manifest["attempt_id"],
            working_source_sha256=candidate.source_sha256,
        )
        return fixture, store, prepared, candidate, scope

    @staticmethod
    def rebind_extraction(
        store: EvidenceStore,
        prepared,
        scope: QueryScope,
        replacement_ref: str,
    ) -> QueryScope:
        old_ref = prepared.snapshot["extraction_refs"][0]

        def replace(value):
            if value == old_ref:
                return replacement_ref
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            return value

        manifest_payload = {
            key: value for key, value in prepared.evaluation_manifest.items()
            if key not in {"schema_version", "content_hash"}
        }
        manifest_payload["extraction_refs"] = [replacement_ref]
        manifest = store.write_record(
            "manifests", "chipcontext.evaluation-manifest.v1", manifest_payload
        )
        snapshot_payload = replace({
            key: value for key, value in prepared.snapshot.items()
            if key not in {"schema_version", "content_hash"}
        })
        snapshot_payload["evaluation_manifest_ref"] = manifest["content_hash"]
        snapshot = store.write_record(
            "snapshots", "chipcontext.evidence-snapshot.v1", snapshot_payload
        )
        return QueryScope(
            EvidenceHandle(scope.handle.store_id, snapshot["content_hash"]),
            expected_candidate=scope.expected_candidate,
            expected_attempt_id=scope.expected_attempt_id,
            working_source_sha256=scope.working_source_sha256,
        )

    @staticmethod
    def make_one_artifact_controlled(
        store: EvidenceStore,
        prepared,
        scope: QueryScope,
    ) -> QueryScope:
        row = next(
            value for value in prepared.evaluation_manifest["artifacts"]
            if value["kind"] == "vivado_timing_report"
        )
        old_ref = row["ref_id"]
        replacement = store.register_artifact(
            Path(row["location"]),
            root_id=row["root_id"],
            kind=row["kind"],
            owner_ref=row["owner_ref"],
            attempt_id=row["attempt_id"],
            access="controlled",
            media_type=row["media_type"],
        )

        def replace(value):
            if value == old_ref:
                return replacement.ref_id
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            return value

        manifest_payload = replace({
            key: value for key, value in prepared.evaluation_manifest.items()
            if key not in {"schema_version", "content_hash"}
        })
        manifest_payload["artifacts"] = [
            replacement.to_dict()
            if value["ref_id"] == replacement.ref_id else value
            for value in manifest_payload["artifacts"]
        ]
        manifest = store.write_record(
            "manifests", "chipcontext.evaluation-manifest.v1", manifest_payload
        )
        snapshot_payload = replace({
            key: value for key, value in prepared.snapshot.items()
            if key not in {"schema_version", "content_hash"}
        })
        snapshot_payload["evaluation_manifest_ref"] = manifest["content_hash"]
        snapshot = store.write_record(
            "snapshots", "chipcontext.evidence-snapshot.v1", snapshot_payload
        )
        return QueryScope(
            EvidenceHandle(scope.handle.store_id, snapshot["content_hash"]),
            expected_candidate=scope.expected_candidate,
            expected_attempt_id=scope.expected_attempt_id,
            working_source_sha256=scope.working_source_sha256,
        )


class QueryFoundationVerticalSliceTests(QueryTestCase):
    def test_status_list_and_scoped_read_form_a_complete_chain(self) -> None:
        _, store, prepared, _, scope = self.prepare()
        service = self.service("primary", store, "public")

        status = service.candidate_status(scope)
        self.assertEqual(status["schema_version"], "chipcontext.query-answer.v1")
        self.assertEqual(status["applicability"]["status"], "current")
        self.assertEqual(status["result"]["completion_status"], "complete")
        self.assertEqual(status["result"]["final_chip_acceptance"], "not_assessed")
        checks = {row["check_id"]: row for row in status["result"]["checks"]}
        self.assertEqual(checks["post_synth"]["outcome"], "pass")
        self.assertFalse(checks["post_route"]["executed"])
        self.assertIsNone(checks["post_route"]["outcome"])

        listing = service.candidate_artifacts(
            scope, kinds=["vivado_timing_report"]
        )
        artifact = listing["result"]["artifacts"][0]
        self.assertEqual(artifact["stage"], "unknown")
        self.assertNotIn("location", artifact)
        self.assertNotIn("root_id", artifact)
        self.assertNotIn("access", artifact)

        page = service.read_artifact(
            scope, artifact["content_ref"], start_line=1, line_count=3
        )
        self.assertIn("Vivado-style summary", page["result"]["content"])
        self.assertEqual(
            page["source_refs"][0]["ref"], artifact["content_ref"]
        )
        self.assertEqual(
            status["resolved_scope"]["snapshot_ref"],
            prepared.snapshot["content_hash"],
        )

    def test_public_example_answer_hashes_are_frozen(self) -> None:
        _, store, _, _, scope = self.prepare(store_id="example")
        service = self.service("example", store, "public")
        status = service.get_candidate_status(scope)
        artifacts = service.list_candidate_artifacts(scope, limit=100)
        selected = artifacts["result"]["artifacts"][0]
        source = service.read_artifact(
            scope, selected["content_ref"], start_line=1, line_count=4
        )
        expected = json.loads(
            (FIXTURES / "query-foundation" / "expected.json").read_text()
        )
        self.assertEqual(status["content_hash"], expected["status"])
        self.assertEqual(artifacts["content_hash"], expected["artifacts"])
        self.assertEqual(source["content_hash"], expected["source"])

    def test_query_answers_are_stable_and_do_not_write_the_store(self) -> None:
        _, store, _, _, scope = self.prepare()
        service = self.service("primary", store, "public")
        before = sorted(path.relative_to(store.root) for path in store.root.rglob("*"))
        first = service.candidate_status(scope)
        second = service.candidate_status(scope)
        after = sorted(path.relative_to(store.root) for path in store.root.rglob("*"))
        self.assertEqual(first, second)
        self.assertEqual(before, after)
        unsigned = {key: value for key, value in first.items() if key != "content_hash"}
        self.assertEqual(content_hash(unsigned), first["content_hash"])

    def test_applicability_distinguishes_selected_current_and_historical(self) -> None:
        _, store, prepared, candidate, current = self.prepare()
        service = self.service("primary", store, "public")
        selected = QueryScope(
            current.handle,
            expected_candidate=candidate,
            expected_attempt_id=prepared.evaluation_manifest["attempt_id"],
        )
        historical = QueryScope(
            current.handle,
            expected_candidate=candidate,
            expected_attempt_id=prepared.evaluation_manifest["attempt_id"],
            working_source_sha256="f" * 64,
        )
        self.assertEqual(
            service.candidate_status(selected)["applicability"]["status"],
            "selected_evaluation",
        )
        self.assertEqual(
            service.candidate_status(current)["applicability"]["status"], "current"
        )
        self.assertEqual(
            service.candidate_status(historical)["applicability"]["status"],
            "historical",
        )

    def test_evaluation_record_stage_comes_from_manifest(self) -> None:
        _, store, _, _, scope = self.prepare()
        service = self.service("primary", store, "public")
        answer = service.candidate_artifacts(
            scope, kinds=["evaluation_record"]
        )
        item = answer["result"]["artifacts"][0]
        self.assertEqual(item["stage"], "post_synth")
        self.assertEqual(item["stage_basis"], "evaluation_manifest")


class QueryExtractionSourceTests(QueryTestCase):
    def test_raw_extraction_can_add_a_missing_legacy_metric(self) -> None:
        _, store, prepared, _, scope = self.prepare_raw(drop_legacy_metric=True)
        service = self.service("raw", store, "public")
        measurement = next(
            row for row in prepared.snapshot["measurements"]
            if row["metric_id"] == "tns_ns"
        )
        self.assertIn(measurement["source_ref"], prepared.snapshot["extraction_refs"])
        status = service.candidate_status(scope)
        self.assertTrue(any(
            row["type"] == "extraction"
            and row["ref"] == measurement["source_ref"]
            for row in status["source_refs"]
        ))
        listing = service.candidate_artifacts(scope, kinds=["vivado_timing_report"])
        service.read_artifact(scope, listing["result"]["artifacts"][0]["content_ref"])

    def test_raw_extraction_can_add_a_missing_legacy_check(self) -> None:
        _, store, prepared, _, scope = self.prepare_raw(
            raw_check_only=True, destination="raw-check"
        )
        check = next(
            row for row in prepared.snapshot["checks"]
            if row["check_id"] == "differential_correctness"
        )
        self.assertTrue(check["executed"])
        self.assertEqual(check["outcome"], "pass")
        self.assertIn(check["source_ref"], prepared.snapshot["extraction_refs"])
        answer = self.service("raw-check", store, "public").candidate_status(scope)
        returned = next(
            row for row in answer["result"]["checks"]
            if row["check_id"] == "differential_correctness"
        )
        self.assertEqual(returned["source_ref"], check["source_ref"])

    def test_matching_raw_and_legacy_sources_are_queryable(self) -> None:
        _, store, prepared, _, scope = self.prepare_raw(destination="raw-match")
        self.assertEqual(
            len(prepared.snapshot["measurement_sources"]["critical_delay_ns"]), 2
        )
        answer = self.service("raw-match", store, "public").candidate_status(scope)
        self.assertEqual(answer["result"]["completion_status"], "complete")

    def test_missing_extraction_record_fails_closed(self) -> None:
        _, store, prepared, _, scope = self.prepare_raw(destination="raw-missing")
        missing_ref = "f" * 64
        rebound = self.rebind_extraction(store, prepared, scope, missing_ref)
        with self.assertRaises(QueryError) as caught:
            self.service("raw-missing", store, "public").candidate_status(rebound)
        self.assertEqual(caught.exception.code, "integrity_error")

    def test_foreign_extraction_owner_fails_closed(self) -> None:
        _, store, prepared, _, scope = self.prepare_raw(destination="raw-owner")
        old = store.read_record("extractions", prepared.snapshot["extraction_refs"][0])
        payload = {
            key: value for key, value in old.items()
            if key not in {"schema_version", "content_hash"}
        }
        payload["owner_ref"] = "f" * 64
        foreign = store.write_record("extractions", "chipcontext.extraction.v1", payload)
        rebound = self.rebind_extraction(
            store, prepared, scope, foreign["content_hash"]
        )
        with self.assertRaises(QueryError) as caught:
            self.service("raw-owner", store, "public").candidate_status(rebound)
        self.assertEqual(caught.exception.code, "integrity_error")

    def test_wrong_extraction_attempt_fails_closed(self) -> None:
        _, store, prepared, _, scope = self.prepare_raw(destination="raw-attempt")
        old = store.read_record("extractions", prepared.snapshot["extraction_refs"][0])
        payload = {
            key: value for key, value in old.items()
            if key not in {"schema_version", "content_hash"}
        }
        payload["attempt_id"] = "another-attempt"
        wrong = store.write_record("extractions", "chipcontext.extraction.v1", payload)
        rebound = self.rebind_extraction(
            store, prepared, scope, wrong["content_hash"]
        )
        with self.assertRaises(QueryError) as caught:
            self.service("raw-attempt", store, "public").candidate_status(rebound)
        self.assertEqual(caught.exception.code, "integrity_error")

    def test_controlled_extraction_inputs_are_reauthorized(self) -> None:
        _, store, _, _, scope = self.prepare_raw(
            destination="raw-controlled", request_access="controlled"
        )
        with self.assertRaises(QueryError) as caught:
            self.service("raw-controlled", store, "public").candidate_status(scope)
        self.assertEqual(caught.exception.code, "permission_denied")
        answer = self.service(
            "raw-controlled", store, "public", "controlled"
        ).candidate_status(scope)
        self.assertEqual(answer["result"]["completion_status"], "complete")


class QueryScopeAndIntegrityTests(QueryTestCase):
    def test_wrong_experiment_candidate_is_rejected(self) -> None:
        _, store, _, candidate, scope = self.prepare()
        wrong = CandidateRef(
            experiment_id="another-campaign",
            candidate_id=candidate.candidate_id,
            source_sha256=candidate.source_sha256,
            contract_sha256=candidate.contract_sha256,
        )
        service = self.service("primary", store, "public")
        with self.assertRaisesRegex(QueryError, "candidate") as caught:
            service.candidate_status(
                QueryScope(
                    scope.handle,
                    expected_candidate=wrong,
                    expected_attempt_id=scope.expected_attempt_id,
                )
            )
        self.assertEqual(caught.exception.code, "scope_mismatch")

    def test_same_source_under_another_contract_is_rejected(self) -> None:
        _, store, _, candidate, scope = self.prepare()
        wrong = CandidateRef(
            experiment_id=candidate.experiment_id,
            candidate_id=candidate.candidate_id,
            source_sha256=candidate.source_sha256,
            contract_sha256="a" * 64,
        )
        service = self.service("primary", store, "public")
        with self.assertRaises(QueryError) as caught:
            service.candidate_status(
                QueryScope(scope.handle, expected_candidate=wrong)
            )
        self.assertEqual(caught.exception.code, "scope_mismatch")

    def test_same_id_under_another_source_is_rejected(self) -> None:
        _, store, _, candidate, scope = self.prepare()
        wrong = CandidateRef(
            experiment_id=candidate.experiment_id,
            candidate_id=candidate.candidate_id,
            source_sha256="b" * 64,
            contract_sha256=candidate.contract_sha256,
        )
        with self.assertRaises(QueryError) as caught:
            self.service("primary", store, "public").candidate_status(
                QueryScope(scope.handle, expected_candidate=wrong)
            )
        self.assertEqual(caught.exception.code, "scope_mismatch")

    def test_wrong_attempt_is_rejected(self) -> None:
        _, store, _, candidate, scope = self.prepare()
        service = self.service("primary", store, "public")
        with self.assertRaises(QueryError) as caught:
            service.candidate_status(
                QueryScope(
                    scope.handle,
                    expected_candidate=candidate,
                    expected_attempt_id="another-attempt",
                )
            )
        self.assertEqual(caught.exception.code, "scope_mismatch")

    def test_snapshot_from_another_store_is_not_inferred_by_alias(self) -> None:
        _, first, _, _, _ = self.prepare("success", store_id="first")
        _, _, other, _, _ = self.prepare(
            "failure", store_id="second", destination="second"
        )
        service = self.service("first", first, "public")
        with self.assertRaises(QueryError) as caught:
            service.candidate_status(
                QueryScope(EvidenceHandle("first", other.snapshot["content_hash"]))
            )
        self.assertEqual(caught.exception.code, "unknown_reference")

    def test_artifact_changed_after_prepare_is_integrity_error(self) -> None:
        fixture, store, _, _, scope = self.prepare()
        (fixture / "working-source.scala").write_text("tampered\n")
        service = self.service("primary", store, "public")
        with self.assertRaises(QueryError) as caught:
            service.candidate_status(scope)
        self.assertEqual(caught.exception.code, "integrity_error")

    def test_artifact_reference_metadata_tampering_is_integrity_error(self) -> None:
        _, store, prepared, _, scope = self.prepare()
        ref_id = prepared.evaluation_manifest["artifacts"][0]["ref_id"]
        path = store.root / "artifacts" / f"{ref_id}.json"
        value = json.loads(path.read_text())
        value["size_bytes"] += 1
        path.write_text(json.dumps(value))
        with self.assertRaises(QueryError) as caught:
            self.service("primary", store, "public").candidate_status(scope)
        self.assertEqual(caught.exception.code, "integrity_error")

    def test_snapshot_record_tampering_is_integrity_error(self) -> None:
        _, store, prepared, _, scope = self.prepare()
        path = (
            store.root / "records" / "snapshots"
            / f"{prepared.snapshot['content_hash']}.json"
        )
        value = json.loads(path.read_text())
        value["parser_revision"] = "tampered"
        path.write_text(json.dumps(value))
        with self.assertRaises(QueryError) as caught:
            self.service("primary", store, "public").candidate_status(scope)
        self.assertEqual(caught.exception.code, "integrity_error")

    def test_unknown_artifact_cannot_be_read_through_scope(self) -> None:
        _, store, _, _, scope = self.prepare()
        with self.assertRaises(QueryError) as caught:
            self.service("primary", store, "public").read_artifact(
                scope, "a" * 64
            )
        self.assertEqual(caught.exception.code, "scope_mismatch")


class QueryAccessAndPaginationTests(QueryTestCase):
    def test_controlled_dependency_rejects_entire_status_and_list(self) -> None:
        _, store, prepared, _, scope = self.prepare(request_access="controlled")
        public_service = self.service("primary", store, "public")
        artifact_ref = prepared.evaluation_manifest["artifacts"][0]["ref_id"]
        for operation in (
            lambda: public_service.candidate_status(scope),
            lambda: public_service.candidate_artifacts(scope),
            lambda: public_service.read_artifact(scope, artifact_ref),
        ):
            with self.assertRaises(QueryError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "permission_denied")

    def test_controlled_envelope_rejects_empty_filtered_lists(self) -> None:
        _, store, _, _, scope = self.prepare(
            request_access="controlled", destination="controlled-empty"
        )
        service = self.service("primary", store, "public")
        for arguments in (
            {},
            {"kinds": ["vivado_timing_report"]},
            {"kinds": ["absent-kind"]},
            {"stage": "post_route"},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(QueryError) as caught:
                    service.candidate_artifacts(scope, **arguments)
                self.assertEqual(caught.exception.code, "permission_denied")

    def test_mixed_access_envelope_is_not_downgraded_by_filters(self) -> None:
        _, store, prepared, _, original = self.prepare(destination="mixed")
        scope = self.make_one_artifact_controlled(store, prepared, original)
        public = self.service("primary", store, "public")
        for arguments in (
            {"kinds": ["evaluation_record"]},
            {"kinds": ["absent-kind"]},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(QueryError) as caught:
                    public.candidate_artifacts(scope, **arguments)
                self.assertEqual(caught.exception.code, "permission_denied")
        public_ref = next(
            value["ref_id"] for value in prepared.evaluation_manifest["artifacts"]
            if value["kind"] == "evaluation_record"
        )
        with self.assertRaises(QueryError) as caught:
            public.read_artifact(scope, public_ref)
        self.assertEqual(caught.exception.code, "permission_denied")
        allowed = self.service("primary", store, "public", "controlled")
        self.assertEqual(
            allowed.candidate_artifacts(
                scope, kinds=["absent-kind"]
            )["coverage"]["matching_count"],
            0,
        )

    def test_authorization_is_rechecked_after_cursor_is_issued(self) -> None:
        _, store, prepared, _, original = self.prepare(destination="tighten")
        scope = self.make_one_artifact_controlled(store, prepared, original)
        broad = self.service("primary", store, "public", "controlled")
        first = broad.candidate_artifacts(scope, limit=1)
        cursor = first["pagination"]["next_cursor"]
        self.assertIsNotNone(cursor)
        with self.assertRaises(QueryError) as caught:
            self.service("primary", store, "public").candidate_artifacts(
                scope, limit=1, cursor=cursor
            )
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_authorized_empty_results_are_returned(self) -> None:
        _, store, _, _, scope = self.prepare(destination="public-empty")
        answer = self.service("primary", store, "public").candidate_artifacts(
            scope, kinds=["absent-kind"]
        )
        self.assertEqual(answer["result"]["artifacts"], [])
        self.assertEqual(answer["coverage"]["matching_count"], 0)

    def test_controlled_access_must_come_from_trusted_policy(self) -> None:
        _, store, _, _, scope = self.prepare(request_access="controlled")
        service = self.service("primary", store, "public", "controlled")
        answer = service.candidate_status(scope)
        self.assertEqual(answer["result"]["completion_status"], "complete")

    def test_artifact_list_cursor_is_stable_and_bound_to_filters(self) -> None:
        _, store, _, _, scope = self.prepare()
        service = self.service("primary", store, "public")
        first = service.candidate_artifacts(scope, limit=2)
        self.assertEqual(len(first["result"]["artifacts"]), 2)
        cursor = first["pagination"]["next_cursor"]
        self.assertIsNotNone(cursor)
        second = service.candidate_artifacts(scope, limit=2, cursor=cursor)
        first_refs = {
            item["content_ref"] for item in first["result"]["artifacts"]
        }
        second_refs = {
            item["content_ref"] for item in second["result"]["artifacts"]
        }
        self.assertTrue(first_refs.isdisjoint(second_refs))
        with self.assertRaisesRegex(SchemaError, "does not belong"):
            service.candidate_artifacts(
                scope, kinds=["evaluation_record"], limit=2, cursor=cursor
            )

    def test_artifact_cursor_is_bound_to_scope_and_page_shape(self) -> None:
        _, store, prepared, _, scope = self.prepare()
        service = self.service("primary", store, "public")
        ref_id = next(
            row["ref_id"] for row in prepared.evaluation_manifest["artifacts"]
            if row["kind"] == "evaluation_record"
        )
        first = service.read_artifact(scope, ref_id, limit_bytes=32)
        cursor = first["pagination"]["next_cursor"]
        self.assertIsNotNone(cursor)
        second = service.read_artifact(scope, ref_id, limit_bytes=32, cursor=cursor)
        self.assertNotEqual(
            first["result"]["span"]["start_byte"],
            second["result"]["span"]["start_byte"],
        )
        with self.assertRaisesRegex(SchemaError, "does not belong"):
            service.read_artifact(scope, ref_id, limit_bytes=31, cursor=cursor)

    def test_cursor_tampering_is_rejected(self) -> None:
        _, store, _, _, scope = self.prepare()
        service = self.service("primary", store, "public")
        answer = service.candidate_artifacts(scope, limit=1)
        cursor = answer["pagination"]["next_cursor"]
        replacement = ("A" if cursor[-1] != "A" else "B")
        with self.assertRaises(SchemaError):
            service.candidate_artifacts(
                scope, limit=1, cursor=cursor[:-1] + replacement
            )

    def test_answers_do_not_expose_host_paths_or_storage_locations(self) -> None:
        _, store, _, _, scope = self.prepare()
        service = self.service("primary", store, "public")
        answer = service.candidate_artifacts(scope)
        encoded = json.dumps(answer, sort_keys=True)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn('"root_id"', encoded)
        self.assertNotIn('"location"', encoded)


class QueryUtf8ReadTests(QueryTestCase):
    def prepare_text(self, data: bytes, destination: str):
        fixture = self.root / f"input-{destination}"
        shutil.copytree(FIXTURES / "success", fixture)
        (fixture / "post_synth_timing_summary.rpt").write_bytes(data)
        store = EvidenceStore(self.root / f"store-{destination}", {"input": fixture})
        prepared = ChipContextService(store).prepare(fixture / "request.json")
        candidate = CandidateRef(**{
            key: value
            for key, value in prepared.snapshot["candidate_ref"].items()
            if key != "ref_id"
        })
        scope = QueryScope(
            EvidenceHandle(destination, prepared.snapshot["content_hash"]),
            expected_candidate=candidate,
            expected_attempt_id=prepared.evaluation_manifest["attempt_id"],
        )
        artifact_ref = next(
            row["ref_id"] for row in prepared.evaluation_manifest["artifacts"]
            if row["kind"] == "vivado_timing_report"
        )
        return store, scope, artifact_ref

    def read_all(self, data: bytes, *, limit: int, destination: str) -> str:
        store, scope, artifact_ref = self.prepare_text(data, destination)
        service = self.service(destination, store, "public")
        parts: list[str] = []
        cursor = None
        while True:
            answer = service.read_artifact(
                scope, artifact_ref, limit_bytes=limit, cursor=cursor
            )
            parts.append(answer["result"]["content"])
            cursor = answer["pagination"]["next_cursor"]
            if cursor is None:
                return "".join(parts)

    def test_ascii_pagination_round_trips(self) -> None:
        data = b"alpha\nbeta\ngamma\n"
        self.assertEqual(
            self.read_all(data, limit=4, destination="ascii"), data.decode()
        )

    def test_chinese_pagination_never_splits_a_character(self) -> None:
        text = "甲乙丙丁\n"
        self.assertEqual(
            self.read_all(text.encode(), limit=4, destination="chinese"), text
        )

    def test_four_byte_characters_round_trip(self) -> None:
        text = "😀🚀🧠\n"
        self.assertEqual(
            self.read_all(text.encode(), limit=5, destination="emoji"), text
        )

    def test_limit_shorter_than_next_character_is_rejected(self) -> None:
        store, scope, artifact_ref = self.prepare_text("甲\n".encode(), "short")
        with self.assertRaises(QueryError) as caught:
            self.service("short", store, "public").read_artifact(
                scope, artifact_ref, limit_bytes=2
            )
        self.assertEqual(caught.exception.code, "text_page_too_small")

    def test_invalid_utf8_is_explicitly_rejected(self) -> None:
        store, scope, artifact_ref = self.prepare_text(b"valid\xffinvalid\n", "invalid")
        with self.assertRaises(QueryError) as caught:
            self.service("invalid", store, "public").read_artifact(
                scope, artifact_ref, limit_bytes=64
            )
        self.assertEqual(caught.exception.code, "invalid_text_encoding")


class QueryFailureTests(QueryTestCase):
    def test_failure_keeps_verdict_and_observation_separate(self) -> None:
        _, store, prepared, _, scope = self.prepare_failure_raw()
        extraction_ref = next(
            ref for ref in prepared.snapshot["extraction_refs"]
            if store.read_record("extractions", ref)["extractor"]["name"]
            == "verilator_differential"
        )
        answer = self.service("raw-failure", store, "public").failure(
            scope,
            check="differential_correctness",
            extraction_ref=extraction_ref,
        )
        self.assertEqual(answer["query"]["revision"], "chipcontext-query-domain-v2")
        self.assertEqual(answer["result"]["recorded_check"]["outcome"], "fail")
        observation = answer["result"]["observation"]
        self.assertEqual(observation["availability"], "available")
        self.assertEqual(observation["configured_cycles"], 1_000_000)
        self.assertIsNone(observation["completed_cycles"])
        self.assertEqual(observation["first_mismatch"]["cycle"], 28)
        self.assertEqual(observation["first_mismatch"]["signal"], "io_output_ready")
        self.assertIsNone(observation["first_mismatch"]["expected"])
        self.assertTrue(any(
            value["type"] == "source_location" for value in answer["source_refs"]
        ))

    def test_failure_requires_a_check_and_marks_missing_observation(self) -> None:
        _, store, _, _, scope = self.prepare("failure", store_id="failure")
        service = self.service("failure", store, "public")
        with self.assertRaises(SchemaError):
            service.failure(scope, check="")
        answer = service.failure(scope, check="differential_correctness")
        self.assertEqual(answer["result"]["recorded_check"]["outcome"], "fail")
        self.assertEqual(
            answer["result"]["observation"]["availability"], "not_collected"
        )

    def test_failure_rejects_a_non_differential_extraction(self) -> None:
        _, store, prepared, _, scope = self.prepare_raw(destination="wrong-failure")
        with self.assertRaises(QueryError) as caught:
            self.service("wrong-failure", store, "public").failure(
                scope,
                check="differential_correctness",
                extraction_ref=prepared.snapshot["extraction_refs"][0],
            )
        self.assertEqual(caught.exception.code, "scope_mismatch")

    def test_public_domain_answer_hashes_are_frozen(self) -> None:
        expected = json.loads(
            (FIXTURES / "query-domain" / "expected.json").read_text()
        )
        _, failure_store, failure_prepared, _, failure_scope = (
            self.prepare_failure_raw(destination="hash-failure")
        )
        differential_ref = next(
            ref for ref in failure_prepared.snapshot["extraction_refs"]
            if failure_store.read_record("extractions", ref)["extractor"]["name"]
            == "verilator_differential"
        )
        failure = self.service(
            "hash-failure", failure_store, "public"
        ).failure(
            failure_scope,
            check="differential_correctness",
            extraction_ref=differential_ref,
        )
        _, path_store, path_prepared, _, path_scope = self.prepare_raw(
            destination="hash-paths"
        )
        vivado_ref = next(
            ref for ref in path_prepared.snapshot["extraction_refs"]
            if path_store.read_record("extractions", ref)["extractor"]["name"]
            == "vivado"
        )
        service = self.service("hash-paths", path_store, "public")
        metrics = service.compare_metrics(
            path_scope,
            reference="bound_baseline",
            stage="post_synth",
            metric_ids=["critical_delay_ns", "slice_luts"],
        )
        paths = service.timing_paths(
            path_scope,
            extraction_ref=vivado_ref,
            stage="post_synth",
            limit=10,
        )
        self.assertEqual(failure["content_hash"], expected["failure"])
        self.assertEqual(metrics["content_hash"], expected["metrics"])
        self.assertEqual(paths["content_hash"], expected["timing_paths"])


class QueryMetricComparisonTests(QueryTestCase):
    def test_bound_baseline_comparison_is_per_metric(self) -> None:
        _, store, _, _, scope = self.prepare(store_id="metrics")
        answer = self.service("metrics", store, "public").compare_metrics(
            scope,
            reference="bound_baseline",
            stage="post_synth",
            metric_ids=["critical_delay_ns", "slice_luts", "unknown_metric"],
        )
        values = {
            value["metric_id"]: value
            for value in answer["result"]["comparisons"]
        }
        self.assertEqual(values["critical_delay_ns"]["status"], "comparable")
        self.assertEqual(values["critical_delay_ns"]["delta"], -1.0)
        self.assertEqual(values["slice_luts"]["delta"], 2)
        self.assertEqual(values["unknown_metric"]["status"], "missing")
        self.assertTrue(answer["result"]["any_comparable"])
        self.assertFalse(answer["result"]["all_comparable"])
        self.assertEqual(
            answer["query"]["parameters"]["delta_direction"],
            "current_minus_reference",
        )

    def test_explicit_snapshot_reference_is_not_inferred(self) -> None:
        _, left_store, _, _, left_scope = self.prepare(
            store_id="left", destination="left"
        )
        _, right_store, _, _, right_scope = self.prepare(
            store_id="right", destination="right"
        )
        service = ChipContextQueryService(
            {"left": left_store, "right": right_store},
            {"left": {"public"}, "right": {"public"}},
        )
        answer = service.compare_metrics(
            right_scope,
            reference=left_scope,
            stage="post_synth",
            metric_ids=["critical_delay_ns"],
        )
        comparison = answer["result"]["comparisons"][0]
        self.assertEqual(comparison["status"], "comparable")
        self.assertEqual(comparison["delta"], 0.0)
        self.assertEqual(
            answer["query"]["parameters"]["reference"]["snapshot_ref"],
            left_scope.handle.snapshot_ref,
        )

    def test_condition_mismatch_suppresses_delta(self) -> None:
        _, store, _, _, scope = self.prepare(store_id="stage-mismatch")
        answer = self.service("stage-mismatch", store, "public").compare_metrics(
            scope,
            reference="bound_baseline",
            stage="post_route",
            metric_ids=["critical_delay_ns"],
        )
        comparison = answer["result"]["comparisons"][0]
        self.assertEqual(comparison["status"], "missing")
        self.assertIsNone(comparison["delta"])

    def test_conflicted_metric_never_gets_a_delta(self) -> None:
        _, conflict_store, conflict_prepared, _, conflict_scope = self.prepare_raw(
            destination="metric-conflict", raw_wns=-5.0
        )
        answer = self.service(
            "metric-conflict", conflict_store, "public"
        ).compare_metrics(
            conflict_scope,
            reference="bound_baseline",
            stage="post_synth",
            metric_ids=["critical_delay_ns", "slice_luts"],
        )
        values = {row["metric_id"]: row for row in answer["result"]["comparisons"]}
        self.assertEqual(values["critical_delay_ns"]["status"], "conflict")
        self.assertIsNone(values["critical_delay_ns"]["delta"])
        self.assertEqual(values["slice_luts"]["status"], "comparable")
        conflict = next(
            value for value in answer["conflicts"]
            if "critical_delay_ns" in value["affected_requested_metrics"]
        )
        self.assertEqual(conflict["evidence_side"], "current")
        self.assertEqual(conflict["store_id"], "metric-conflict")
        self.assertEqual(
            conflict["snapshot_ref"], conflict_prepared.snapshot["content_hash"]
        )
        returned = {
            value["ref"] for value in answer["source_refs"]
            if value["evidence_side"] == "current"
        }
        self.assertTrue(set(conflict["source_refs"]).issubset(returned))

    def test_conflicts_from_both_snapshots_keep_side_and_sources(self) -> None:
        _, current_store, _, _, current_scope = self.prepare_raw(
            destination="current-conflict", raw_wns=-5.0
        )
        _, reference_store, _, _, reference_scope = self.prepare_raw(
            destination="reference-conflict",
            extra_utilization="| Slice LUTs* | 999 | 0 |\n",
        )
        service = ChipContextQueryService(
            {
                "current-conflict": current_store,
                "reference-conflict": reference_store,
            },
            {
                "current-conflict": {"public"},
                "reference-conflict": {"public"},
            },
        )
        answer = service.compare_metrics(
            current_scope,
            reference=reference_scope,
            stage="post_synth",
            metric_ids=["critical_delay_ns", "slice_luts", "slice_registers"],
        )
        values = {row["metric_id"]: row for row in answer["result"]["comparisons"]}
        self.assertEqual(values["critical_delay_ns"]["status"], "conflict")
        self.assertEqual(values["slice_luts"]["status"], "conflict")
        self.assertEqual(values["slice_registers"]["status"], "comparable")
        self.assertEqual(values["slice_registers"]["delta"], 0)
        sides = {
            metric: {
                row["evidence_side"] for row in answer["conflicts"]
                if metric in row["affected_requested_metrics"]
            }
            for metric in ("critical_delay_ns", "slice_luts")
        }
        self.assertEqual(sides["critical_delay_ns"], {"current"})
        self.assertEqual(sides["slice_luts"], {"reference"})
        source_sides = {row["evidence_side"] for row in answer["source_refs"]}
        self.assertEqual(source_sides, {"current", "reference"})


class QueryTimingPathTests(QueryTestCase):
    def _prepared(self, destination: str = "paths", **kwargs):
        _, store, prepared, _, scope = self.prepare_raw(
            destination=destination, **kwargs
        )
        extraction_ref = next(
            ref for ref in prepared.snapshot["extraction_refs"]
            if store.read_record("extractions", ref)["extractor"]["name"] == "vivado"
        )
        return store, scope, extraction_ref

    def test_report_first_and_exact_filtered_minimum_are_distinct(self) -> None:
        store, scope, extraction_ref = self._prepared()
        service = self.service("paths", store, "public")
        answer = service.timing_paths(
            scope,
            extraction_ref=extraction_ref,
            stage="post_synth",
            destination="grant_reg[1]/D",
        )
        self.assertEqual(answer["result"]["report_first"]["fact"]["rank"], 1)
        self.assertEqual(answer["result"]["filtered_minimum"]["fact"]["rank"], 2)
        self.assertEqual(answer["result"]["paths"][0]["rank"], 2)
        self.assertEqual(
            answer["result"]["global_worst"]["availability"], "not_supported"
        )
        self.assertEqual(
            answer["coverage"]["report"]["requested_max_paths"], 2
        )
        self.assertTrue(
            answer["result"]["filtered_minimum"][
                "confirmed_for_collected_matches"
            ]
        )

    def test_unparseable_first_slack_makes_parsed_minimum_partial(self) -> None:
        content = (
            FIXTURES / "extraction" / "post_synth_timing_paths.rpt"
        ).read_text().replace(
            "Slack (VIOLATED) :        -1.250ns",
            "Slack (VIOLATED) :        N/A",
            1,
        )
        store, scope, extraction_ref = self._prepared(
            "paths-partial-slack", timing_path_content=content
        )
        answer = self.service(
            "paths-partial-slack", store, "public"
        ).timing_paths(
            scope, extraction_ref=extraction_ref, stage="post_synth"
        )
        self.assertEqual(
            answer["result"]["report_first"]["availability"], "parse_failed"
        )
        minimum = answer["result"]["filtered_minimum"]
        self.assertEqual(minimum["availability"], "partial")
        self.assertEqual(minimum["fact"]["rank"], 2)
        self.assertFalse(minimum["confirmed_for_collected_matches"])
        self.assertEqual(minimum["unparseable_definite_ranks"], [1])
        self.assertEqual(
            minimum["basis"], "minimum_among_parsed_definite_matches"
        )

    def test_all_unparseable_slacks_are_parse_failed(self) -> None:
        content = (
            FIXTURES / "extraction" / "post_synth_timing_paths.rpt"
        ).read_text()
        content = content.replace("-1.250ns", "N/A", 1).replace(
            "-0.750ns", "N/A", 1
        )
        store, scope, extraction_ref = self._prepared(
            "paths-all-unparseable", timing_path_content=content
        )
        minimum = self.service(
            "paths-all-unparseable", store, "public"
        ).timing_paths(
            scope, extraction_ref=extraction_ref, stage="post_synth"
        )["result"]["filtered_minimum"]
        self.assertEqual(minimum["availability"], "parse_failed")
        self.assertIsNone(minimum["fact"])
        self.assertEqual(minimum["unparseable_definite_ranks"], [1, 2])

    def test_missing_and_unparseable_reports_are_not_no_match(self) -> None:
        missing_store, missing_scope, missing_ref = self._prepared(
            "paths-not-collected", include_timing_paths=False
        )
        missing = self.service(
            "paths-not-collected", missing_store, "public"
        ).timing_paths(
            missing_scope, extraction_ref=missing_ref, stage="post_synth"
        )["result"]["filtered_minimum"]
        self.assertEqual(missing["availability"], "not_collected")

        broken_store, broken_scope, broken_ref = self._prepared(
            "paths-no-blocks",
            timing_path_content=(
                "Timing Report\nSource: state_reg/C\nDestination: grant_reg/D\n"
            ),
        )
        broken = self.service(
            "paths-no-blocks", broken_store, "public"
        ).timing_paths(
            broken_scope, extraction_ref=broken_ref, stage="post_synth"
        )["result"]["filtered_minimum"]
        self.assertEqual(broken["availability"], "parse_failed")

    def test_missing_exact_filter_field_is_possible_not_no_match(self) -> None:
        content = (
            FIXTURES / "extraction" / "post_synth_timing_paths.rpt"
        ).read_text().replace(
            "  Source:                 state_reg[3]/C\n", "", 1
        )
        store, scope, extraction_ref = self._prepared(
            "paths-unknown-filter", timing_path_content=content
        )
        answer = self.service(
            "paths-unknown-filter", store, "public"
        ).timing_paths(
            scope,
            extraction_ref=extraction_ref,
            stage="post_synth",
            source="state_reg[3]/C",
        )
        minimum = answer["result"]["filtered_minimum"]
        self.assertEqual(minimum["availability"], "inconclusive")
        self.assertEqual(minimum["possible_match_ranks"], [1])
        self.assertEqual(answer["coverage"]["matching_count"], 0)
        self.assertEqual(answer["coverage"]["possible_match_count"], 1)

    def test_unrelated_optional_field_does_not_weaken_minimum(self) -> None:
        content = (
            FIXTURES / "extraction" / "post_synth_timing_paths.rpt"
        ).read_text().replace(
            "  Logic Levels:           7  (LUT5=1 LUT6=6)\n", "", 1
        )
        store, scope, extraction_ref = self._prepared(
            "paths-optional-field", timing_path_content=content
        )
        minimum = self.service(
            "paths-optional-field", store, "public"
        ).timing_paths(
            scope,
            extraction_ref=extraction_ref,
            stage="post_synth",
            destination="grant_reg[0]/D",
        )["result"]["filtered_minimum"]
        self.assertEqual(minimum["availability"], "available")
        self.assertTrue(minimum["confirmed_for_collected_matches"])
        self.assertEqual(minimum["fact"]["availability"], "partial")

    def test_no_match_and_original_rank_pagination(self) -> None:
        store, scope, extraction_ref = self._prepared("paths-page")
        service = self.service("paths-page", store, "public")
        first = service.timing_paths(
            scope,
            extraction_ref=extraction_ref,
            stage="post_synth",
            limit=1,
        )
        second = service.timing_paths(
            scope,
            extraction_ref=extraction_ref,
            stage="post_synth",
            limit=1,
            cursor=first["pagination"]["next_cursor"],
        )
        self.assertEqual(first["result"]["paths"][0]["rank"], 1)
        self.assertEqual(second["result"]["paths"][0]["rank"], 2)
        none = service.timing_paths(
            scope,
            extraction_ref=extraction_ref,
            stage="post_synth",
            source="does-not-exist",
        )
        self.assertEqual(
            none["result"]["filtered_minimum"]["availability"], "no_match"
        )

    def test_stage_and_cursor_binding_fail_closed(self) -> None:
        store, scope, extraction_ref = self._prepared("paths-binding")
        service = self.service("paths-binding", store, "public")
        with self.assertRaises(QueryError) as caught:
            service.timing_paths(
                scope, extraction_ref=extraction_ref, stage="post_route"
            )
        self.assertEqual(caught.exception.code, "scope_mismatch")
        first = service.timing_paths(
            scope, extraction_ref=extraction_ref, stage="post_synth", limit=1
        )
        with self.assertRaises(SchemaError):
            service.timing_paths(
                scope,
                extraction_ref=extraction_ref,
                stage="post_synth",
                limit=1,
                path_group="clock",
                cursor=first["pagination"]["next_cursor"],
            )


class QueryContractValidationTests(QueryTestCase):
    def test_registry_requires_explicit_access_policy(self) -> None:
        _, store, _, _, _ = self.prepare()
        with self.assertRaises(SchemaError):
            ChipContextQueryService({"primary": store}, {})

    def test_registry_description_is_deterministic_and_path_free(self) -> None:
        _, store, _, _, _ = self.prepare()
        registry = TrustedStoreRegistry(
            {"primary": store}, {"primary": {"controlled", "public"}}
        )
        first = registry.describe()
        second = registry.describe()
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], "chipcontext.store-registry.v1")
        self.assertEqual(
            content_hash({key: value for key, value in first.items() if key != "content_hash"}),
            first["content_hash"],
        )
        self.assertNotIn(str(self.root), json.dumps(first))

    def test_string_is_not_accepted_as_an_artifact_kind_collection(self) -> None:
        _, store, _, _, scope = self.prepare()
        with self.assertRaises(SchemaError):
            self.service("primary", store, "public").candidate_artifacts(
                scope, kinds="evaluation_record"
            )

    def test_invalid_handle_and_candidate_identity_are_rejected(self) -> None:
        with self.assertRaises(SchemaError):
            EvidenceHandle("../escape", "a" * 64)
        with self.assertRaises(SchemaError):
            QueryScope(
                EvidenceHandle("primary", "a" * 64),
                expected_candidate={
                    "experiment_id": "e",
                    "candidate_id": "c",
                    "source_sha256": "b" * 64,
                    "contract_sha256": "c" * 64,
                    "ref_id": "d" * 64,
                },
            )

    def test_no_query_answer_contains_a_dynamic_cost_field(self) -> None:
        _, store, _, _, scope = self.prepare()
        answer = self.service("primary", store, "public").candidate_status(scope)
        self.assertNotIn("cost", answer)
        self.assertNotIn("wall", json.dumps(answer).lower())


if __name__ == "__main__":
    unittest.main()
