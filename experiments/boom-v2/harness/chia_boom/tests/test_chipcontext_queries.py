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
        _, store, _, _, scope = self.prepare(request_access="controlled")
        public_service = self.service("primary", store, "public")
        for operation in (
            lambda: public_service.candidate_status(scope),
            lambda: public_service.candidate_artifacts(scope),
        ):
            with self.assertRaises(QueryError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "permission_denied")

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
