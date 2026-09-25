from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chia_boom.chipcontext import cli
from chia_boom.chipcontext.query import (
    ChipContextQueryService,
    EvidenceHandle,
    QueryError,
    QueryScope,
)
from chia_boom.chipcontext.query_cli import (
    QueryExecution,
    execute_query,
    serialize_execution,
)
from chia_boom.chipcontext.render import render_json, render_markdown
from chia_boom.chipcontext.schema import CandidateRef
from chia_boom.chipcontext.service import ChipContextService
from chia_boom.chipcontext.store import EvidenceMeter, EvidenceStore
from chia_boom.tests.test_chipcontext_queries import FIXTURES, QueryTestCase


class QueryCliTestCase(QueryTestCase):
    def scope_value(self, scope: QueryScope, *, working: str | None = None):
        candidate = scope.expected_candidate
        return {
            "handle": scope.handle.to_dict(),
            "expected_candidate": (
                candidate.to_dict() if isinstance(candidate, CandidateRef) else candidate
            ),
            "expected_attempt_id": scope.expected_attempt_id,
            "working_source_sha256": (
                scope.working_source_sha256 if working is None else working
            ),
        }

    def write_cli_inputs(
        self,
        *,
        stores: dict[str, EvidenceStore],
        scope: QueryScope,
        operation: str,
        parameters: dict,
        allowed_access: dict[str, list[str]] | None = None,
        reference_scope: QueryScope | None = None,
    ) -> tuple[Path, Path]:
        config = self.root / f"cli-{operation}-{len(list(self.root.iterdir()))}"
        config.mkdir()
        registry = {
            "schema_version": "chipcontext.store-registry.v1",
            "stores": [
                {
                    "store_id": store_id,
                    "root": os.path.relpath(store.root, config.resolve()),
                    "allowed_access": (
                        allowed_access or {}
                    ).get(store_id, ["public"]),
                }
                for store_id, store in sorted(stores.items())
            ],
        }
        request_parameters = dict(parameters)
        if reference_scope is not None:
            request_parameters["reference"] = self.scope_value(reference_scope)
        request = {
            "schema_version": "chipcontext.query-request.v1",
            "operation": operation,
            "scope": self.scope_value(scope),
            "parameters": request_parameters,
        }
        registry_path = config / "registry.json"
        request_path = config / "request.json"
        registry_path.write_text(json.dumps(registry, indent=2))
        request_path.write_text(json.dumps(request, indent=2))
        return registry_path, request_path

    @staticmethod
    def run_cli(*arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", ["chia-chipcontext", *arguments]):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with unittest.TestCase().assertRaises(SystemExit) as caught:
                    cli.main()
        return caught.exception.code, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def run_cli_process(*arguments: str) -> subprocess.CompletedProcess[str]:
        harness = Path(__file__).parents[2]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(harness)
        return subprocess.run(
            [sys.executable, "-m", "chia_boom.chipcontext.cli", *arguments],
            cwd=harness,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_formats(
        self, registry: Path, request: Path, *, allow_controlled: bool = False
    ) -> tuple[dict, str]:
        common = ["query", "--registry", str(registry), "--request", str(request)]
        if allow_controlled:
            common.append("--allow-controlled")
        json_code, json_output, json_error = self.run_cli(*common, "--format", "json")
        markdown_code, markdown, markdown_error = self.run_cli(
            *common, "--format", "markdown"
        )
        self.assertEqual((json_code, markdown_code), (0, 0))
        self.assertEqual(json_error + markdown_error, "")
        response = json.loads(json_output)
        self.assertEqual(response["schema_version"], "chipcontext.query-response.v1")
        answer_hash = response["answer"]["content_hash"]
        self.assertIn(answer_hash, markdown)
        self.assertEqual(response["cost"]["return_bytes"], len(json_output.encode()))
        self.assertIn("chipcontext.query-cost.v1", markdown)
        return response, markdown


class QueryCliOperationsTests(QueryCliTestCase):
    def test_public_json_and_markdown_render_fixtures_are_frozen(self) -> None:
        _, store, _, _, scope = self.prepare(
            store_id="success", destination="render"
        )
        answer = self.service("success", store, "public").candidate_status(scope)
        cost = {
            "schema_version": "chipcontext.query-cost.v1",
            "status": "success",
            "wall_time_ns": 0,
            "measurement_boundary": "through_first_complete_render",
            "phase_time_ns": {
                "evidence_query_ns": 0,
                "first_render_ns": 0,
                "request_validation_ns": 0,
                "store_resolution_ns": 0,
            },
            "configuration_read_count": 0,
            "configuration_read_bytes": 0,
            "store_metadata_read_count": 0,
            "store_metadata_read_bytes": 0,
            "record_read_count": 0,
            "record_read_bytes": 0,
            "artifact_metadata_read_count": 0,
            "artifact_metadata_read_bytes": 0,
            "artifact_read_count": 0,
            "artifact_scan_bytes": 0,
            "hash_bytes": 0,
            "parse_bytes": 0,
            "return_bytes": 0,
            "cache_status": "not_configured",
            "physical_io_bytes": None,
            "peak_memory_bytes": None,
        }
        json_text = render_json({
            "schema_version": "chipcontext.query-response.v1",
            "answer": answer,
            "cost": cost,
        })
        markdown = render_markdown(answer, cost)
        fixture = FIXTURES / "query-cli"
        expected = json.loads((fixture / "expected.json").read_text())
        self.assertEqual(answer["content_hash"], expected["answer"])
        self.assertEqual(json_text, (fixture / "expected-status.json").read_text())
        self.assertEqual(markdown, (fixture / "expected-status.md").read_text())
        self.assertEqual(
            hashlib.sha256(json_text.encode()).hexdigest(), expected["json_sha256"]
        )
        self.assertEqual(
            hashlib.sha256(markdown.encode()).hexdigest(),
            expected["markdown_sha256"],
        )

    def test_all_six_operations_render_the_same_answer(self) -> None:
        _, status_store, _, _, status_scope = self.prepare(destination="cli-status")
        status_ref = next(iter(status_store.root.joinpath("artifacts").glob("*.json"))).stem

        _, failure_store, failure_prepared, _, failure_scope = self.prepare_failure_raw(
            destination="cli-failure"
        )
        differential_ref = next(
            ref for ref in failure_prepared.snapshot["extraction_refs"]
            if failure_store.read_record("extractions", ref)["extractor"]["name"]
            == "verilator_differential"
        )

        _, domain_store, domain_prepared, _, domain_scope = self.prepare_raw(
            destination="cli-domain"
        )
        vivado_ref = next(
            ref for ref in domain_prepared.snapshot["extraction_refs"]
            if domain_store.read_record("extractions", ref)["extractor"]["name"]
            == "vivado"
        )
        cases = [
            ("candidate_status", status_store, status_scope, {}),
            ("candidate_artifacts", status_store, status_scope, {"limit": 3}),
            (
                "failure",
                failure_store,
                failure_scope,
                {
                    "check": "differential_correctness",
                    "extraction_ref": differential_ref,
                },
            ),
            (
                "compare_metrics",
                domain_store,
                domain_scope,
                {
                    "reference": "bound_baseline",
                    "stage": "post_synth",
                    "metric_ids": ["critical_delay_ns", "slice_luts"],
                },
            ),
            (
                "timing_paths",
                domain_store,
                domain_scope,
                {"extraction_ref": vivado_ref, "stage": "post_synth", "limit": 2},
            ),
            (
                "read_artifact",
                status_store,
                status_scope,
                {"artifact_ref": status_ref, "limit_bytes": 64},
            ),
        ]
        for operation, store, scope, parameters in cases:
            with self.subTest(operation=operation):
                registry, request = self.write_cli_inputs(
                    stores={scope.handle.store_id: store},
                    scope=scope,
                    operation=operation,
                    parameters=parameters,
                )
                response, markdown = self.assert_formats(registry, request)
                self.assertEqual(response["answer"]["query"]["name"], operation)
                self.assertIn(operation, markdown)

    def test_markdown_preserves_conflict_partial_and_historical_facts(self) -> None:
        _, conflict_store, _, candidate, conflict_scope = self.prepare_raw(
            destination="cli-conflict", raw_wns=-5.0
        )
        conflict_scope = QueryScope(
            conflict_scope.handle,
            expected_candidate=candidate,
            expected_attempt_id=conflict_scope.expected_attempt_id,
            working_source_sha256="f" * 64,
        )
        registry, request = self.write_cli_inputs(
            stores={"cli-conflict": conflict_store},
            scope=conflict_scope,
            operation="compare_metrics",
            parameters={
                "reference": "bound_baseline",
                "stage": "post_synth",
                "metric_ids": ["critical_delay_ns", "slice_luts"],
            },
        )
        _, markdown = self.assert_formats(registry, request)
        self.assertIn('"status": "historical"', markdown)
        self.assertIn('"status": "conflict"', markdown)
        self.assertIn('"evidence_side": "current"', markdown)

        content = (
            FIXTURES / "extraction" / "post_synth_timing_paths.rpt"
        ).read_text().replace("-1.250ns", "N/A", 1)
        _, path_store, prepared, _, path_scope = self.prepare_raw(
            destination="cli-partial", timing_path_content=content
        )
        extraction_ref = next(
            ref for ref in prepared.snapshot["extraction_refs"]
            if path_store.read_record("extractions", ref)["extractor"]["name"]
            == "vivado"
        )
        registry, request = self.write_cli_inputs(
            stores={"cli-partial": path_store},
            scope=path_scope,
            operation="timing_paths",
            parameters={"extraction_ref": extraction_ref, "stage": "post_synth"},
        )
        _, markdown = self.assert_formats(registry, request)
        self.assertIn('"availability": "partial"', markdown)
        self.assertIn('"confirmed_for_collected_matches": false', markdown)


class QueryCliSecurityAndBudgetTests(QueryCliTestCase):
    def test_legacy_read_error_contract_remains_on_stdout(self) -> None:
        _, store, _, _, _ = self.prepare(destination="legacy-cli-error")
        code, output, error = self.run_cli(
            "read-artifact",
            "--store",
            str(store.root),
            "--artifact",
            "0" * 64,
        )
        self.assertEqual((code, error), (2, ""))
        self.assertEqual(json.loads(output)["error"], "KeyError")

    def test_controlled_access_requires_the_cli_flag(self) -> None:
        _, store, _, _, scope = self.prepare(
            destination="cli-controlled", request_access="controlled"
        )
        registry, request = self.write_cli_inputs(
            stores={"primary": store},
            scope=scope,
            operation="candidate_status",
            parameters={},
            allowed_access={"primary": ["public", "controlled"]},
        )
        code, output, error = self.run_cli(
            "query", "--registry", str(registry), "--request", str(request)
        )
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(json.loads(error)["error"], "permission_denied")
        response, _ = self.assert_formats(registry, request, allow_controlled=True)
        self.assertEqual(response["answer"]["query"]["name"], "candidate_status")

    def test_registry_and_request_reject_unknown_or_unsafe_fields(self) -> None:
        _, store, _, _, scope = self.prepare(destination="cli-invalid")
        registry, request = self.write_cli_inputs(
            stores={"primary": store},
            scope=scope,
            operation="candidate_status",
            parameters={},
        )
        value = json.loads(request.read_text())
        value["permission"] = "controlled"
        request.write_text(json.dumps(value))
        code, output, error = self.run_cli(
            "query", "--registry", str(registry), "--request", str(request)
        )
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(json.loads(error)["error"], "invalid_request")
        self.assertNotIn(str(self.root), error)

        value = json.loads(registry.read_text())
        value["stores"].append(dict(value["stores"][0]))
        registry.write_text(json.dumps(value))
        request_value = json.loads(request.read_text())
        request_value.pop("permission")
        request.write_text(json.dumps(request_value))
        code, _, error = self.run_cli(
            "query", "--registry", str(registry), "--request", str(request)
        )
        self.assertEqual(code, 2)
        self.assertIn("duplicate", json.loads(error)["message"])

    def test_nested_query_contract_rejects_unknown_and_wrongly_typed_fields(self) -> None:
        _, store, _, _, scope = self.prepare(destination="cli-nested-invalid")
        registry, request = self.write_cli_inputs(
            stores={"primary": store},
            scope=scope,
            operation="candidate_status",
            parameters={},
        )
        value = json.loads(request.read_text())
        value["scope"]["expected_candidate"]["parent_ref"] = {
            "candidate_id": "parent",
            "source_sha256": "a" * 64,
            "latest": True,
        }
        request.write_text(json.dumps(value))
        code, output, error = self.run_cli(
            "query", "--registry", str(registry), "--request", str(request)
        )
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(json.loads(error)["error"], "invalid_request")

        value["scope"]["expected_candidate"].pop("parent_ref")
        value["scope"]["expected_attempt_id"] = 1
        request.write_text(json.dumps(value))
        code, output, error = self.run_cli(
            "query", "--registry", str(registry), "--request", str(request)
        )
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(json.loads(error)["error"], "invalid_request")

        value["operation"] = "compare_metrics"
        value["parameters"] = {
            "reference": "bound_baseline",
            "stage": ["post_synth"],
            "metric_ids": ["critical_delay_ns"],
        }
        value["scope"]["expected_attempt_id"] = scope.expected_attempt_id
        request.write_text(json.dumps(value))
        code, output, error = self.run_cli(
            "query", "--registry", str(registry), "--request", str(request)
        )
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(json.loads(error)["error"], "invalid_request")

        value["operation"] = "candidate_status"
        value["parameters"] = {}
        request.write_text(json.dumps(value))
        registry_value = json.loads(registry.read_text())
        registry_value["stores"][0]["root"] = 123
        registry.write_text(json.dumps(registry_value))
        code, output, error = self.run_cli(
            "query", "--registry", str(registry), "--request", str(request)
        )
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(json.loads(error)["error"], "invalid_request")

    def test_unknown_store_symlink_and_tamper_fail_closed_without_paths(self) -> None:
        _, store, prepared, _, scope = self.prepare(destination="cli-security")
        registry, request = self.write_cli_inputs(
            stores={"primary": store},
            scope=scope,
            operation="candidate_status",
            parameters={},
        )
        value = json.loads(request.read_text())
        value["scope"]["handle"]["store_id"] = "missing"
        request.write_text(json.dumps(value))
        code, output, error = self.run_cli(
            "query", "--registry", str(registry), "--request", str(request)
        )
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(json.loads(error)["error"], "unknown_store")

        value["scope"]["handle"]["store_id"] = "primary"
        request.write_text(json.dumps(value))
        linked = registry.parent / "linked-store"
        linked.symlink_to(store.root, target_is_directory=True)
        registry_value = json.loads(registry.read_text())
        registry_value["stores"][0]["root"] = "linked-store"
        registry.write_text(json.dumps(registry_value))
        code, _, error = self.run_cli(
            "query", "--registry", str(registry), "--request", str(request)
        )
        self.assertEqual(code, 2)
        self.assertNotIn(str(store.root), error)

        linked.unlink()
        registry_value["stores"][0]["root"] = os.path.relpath(
            store.root, registry.parent.resolve()
        )
        registry.write_text(json.dumps(registry_value))
        snapshot_path = (
            store.root / "records" / "snapshots"
            / f"{prepared.snapshot['content_hash']}.json"
        )
        original = snapshot_path.read_text()
        snapshot_path.write_text(
            original.replace(
                '"chipcontext.evidence-snapshot.v1"',
                '"chipcontext.evidence-snapshot.tampered"',
                1,
            )
        )
        code, _, error = self.run_cli(
            "query", "--registry", str(registry), "--request", str(request)
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(error)["error"], "integrity_error")
        self.assertNotIn(str(snapshot_path), error)

    def test_corrupt_store_metadata_is_path_free_and_audited(self) -> None:
        _, store, prepared, _, scope = self.prepare(destination="cli-corrupt-store")
        registry, request = self.write_cli_inputs(
            stores={"primary": store},
            scope=scope,
            operation="candidate_status",
            parameters={},
        )

        roots_path = store.root / "ROOTS.json"
        original_roots = roots_path.read_bytes()
        snapshot_path = (
            store.root / "records" / "snapshots"
            / f"{prepared.snapshot['content_hash']}.json"
        )
        original_snapshot = snapshot_path.read_bytes()
        artifact_path = next(store.root.joinpath("artifacts").glob("*.json"))
        original_artifact = artifact_path.read_bytes()
        mutations = [
            ("roots-array", roots_path, b"[]"),
            ("roots-type", roots_path, b'{"input":123}'),
            ("roots-encoding", roots_path, b"\xff\xfe"),
            ("record-encoding", snapshot_path, b"\xff\xfe"),
            ("artifact-encoding", artifact_path, b"\xff\xfe"),
        ]
        for name, target, damaged in mutations:
            with self.subTest(name=name):
                roots_path.write_bytes(original_roots)
                snapshot_path.write_bytes(original_snapshot)
                artifact_path.write_bytes(original_artifact)
                target.write_bytes(damaged)
                audit_path = registry.parent / f"audit-{name}.json"
                completed = self.run_cli_process(
                    "query",
                    "--registry", str(registry),
                    "--request", str(request),
                    "--audit-output", str(audit_path),
                )
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stdout, "")
                error = json.loads(completed.stderr)
                self.assertEqual(error["error"], "integrity_error")
                self.assertNotIn("Traceback", completed.stderr)
                self.assertNotIn(str(self.root), completed.stderr)
                audit = json.loads(audit_path.read_text())
                self.assertEqual(audit["status"], "rejected")
                self.assertEqual(audit["error_code"], "integrity_error")
                self.assertIsNone(audit["answer_ref"])
                self.assertGreater(audit["cost"]["wall_time_ns"], 0)
                self.assertGreater(
                    sum(audit["cost"]["phase_time_ns"].values()), 0
                )
        roots_path.write_bytes(original_roots)
        snapshot_path.write_bytes(original_snapshot)
        artifact_path.write_bytes(original_artifact)

    def test_success_and_budget_rejection_have_complete_attempt_audits(self) -> None:
        fixture = self.root / "input-audit-large"
        shutil.copytree(FIXTURES / "success", fixture)
        large = fixture / "large.txt"
        large.write_text("x" * (1024 * 1024))
        request_value = json.loads((fixture / "request.json").read_text())
        request_value["artifacts"].append({
            "name": "large_log",
            "kind": "diagnostic_log",
            "path": "large.txt",
        })
        (fixture / "request.json").write_text(json.dumps(request_value))
        store = EvidenceStore(self.root / "store-audit-large", {"input": fixture})
        prepared = ChipContextService(store).prepare(fixture / "request.json")
        candidate = CandidateRef(**{
            key: value for key, value in prepared.snapshot["candidate_ref"].items()
            if key != "ref_id"
        })
        scope = QueryScope(
            EvidenceHandle("large", prepared.snapshot["content_hash"]),
            expected_candidate=candidate,
            expected_attempt_id=prepared.evaluation_manifest["attempt_id"],
        )
        large_ref = next(
            row["ref_id"] for row in prepared.evaluation_manifest["artifacts"]
            if row["kind"] == "diagnostic_log"
        )
        registry, request = self.write_cli_inputs(
            stores={"large": store},
            scope=scope,
            operation="read_artifact",
            parameters={"artifact_ref": large_ref, "limit_bytes": 8},
        )

        success_audit_path = registry.parent / "audit-success.json"
        completed = self.run_cli_process(
            "query",
            "--registry", str(registry),
            "--request", str(request),
            "--max-output-bytes", str(64 * 1024),
            "--audit-output", str(success_audit_path),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        response = json.loads(completed.stdout)
        success_audit = json.loads(success_audit_path.read_text())
        self.assertEqual(success_audit["status"], "success")
        self.assertEqual(success_audit["answer_ref"], response["answer"]["content_hash"])
        self.assertEqual(
            success_audit["cost"]["artifact_scan_bytes"],
            response["cost"]["artifact_scan_bytes"],
        )
        self.assertEqual(
            success_audit["cost"]["hash_bytes"], response["cost"]["hash_bytes"]
        )
        self.assertEqual(
            success_audit["output"]["returned_bytes"],
            len(completed.stdout.encode("utf-8")),
        )
        self.assertGreaterEqual(
            success_audit["cost"]["artifact_scan_bytes"], 1024 * 1024
        )
        self.assertGreater(
            success_audit["cost"]["phase_time_ns"]["response_serialization_ns"], 0
        )

        rejected_audit_path = registry.parent / "audit-budget.json"
        rejected = self.run_cli_process(
            "query",
            "--registry", str(registry),
            "--request", str(request),
            "--max-output-bytes", "1024",
            "--audit-output", str(rejected_audit_path),
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(rejected.stdout, "")
        self.assertEqual(json.loads(rejected.stderr)["error"], "budget_exceeded")
        rejected_audit = json.loads(rejected_audit_path.read_text())
        self.assertEqual(rejected_audit["status"], "rejected")
        self.assertEqual(rejected_audit["error_code"], "budget_exceeded")
        self.assertIsNotNone(rejected_audit["answer_ref"])
        self.assertGreater(
            rejected_audit["output"]["attempted_output_bytes"], 1024
        )
        self.assertEqual(rejected_audit["output"]["returned_bytes"], 0)
        self.assertGreaterEqual(
            rejected_audit["cost"]["artifact_scan_bytes"], 1024 * 1024
        )
        self.assertGreater(rejected_audit["cost"]["hash_bytes"], 0)
        self.assertGreater(
            rejected_audit["cost"]["phase_time_ns"]["response_serialization_ns"], 0
        )

    def test_exact_output_budget_and_dynamic_cost_hash_separation(self) -> None:
        _, store, _, _, scope = self.prepare(destination="cli-budget")
        registry, request = self.write_cli_inputs(
            stores={"primary": store},
            scope=scope,
            operation="candidate_status",
            parameters={},
        )
        execution = execute_query(registry, request)
        with mock.patch(
            "chia_boom.chipcontext.query_cli.time.monotonic_ns", return_value=10**12
        ):
            output, cost = serialize_execution(
                QueryExecution(execution.answer, execution.meter, 0),
                output_format="json",
                max_output_bytes=64 * 1024,
            )
            exact = len(output.encode())
            exact_output, _ = serialize_execution(
                QueryExecution(execution.answer, execution.meter, 0),
                output_format="json",
                max_output_bytes=exact,
            )
            self.assertEqual(len(exact_output.encode()), exact)
            with self.assertRaises(QueryError) as caught:
                serialize_execution(
                    QueryExecution(execution.answer, execution.meter, 0),
                    output_format="json",
                    max_output_bytes=exact - 1,
                )
            markdown, markdown_cost = serialize_execution(
                QueryExecution(execution.answer, execution.meter, 0),
                output_format="markdown",
                max_output_bytes=64 * 1024,
            )
        self.assertEqual(caught.exception.code, "budget_exceeded")
        self.assertEqual(cost["return_bytes"], exact)
        self.assertEqual(markdown_cost["return_bytes"], len(markdown.encode()))
        self.assertEqual(
            json.loads(output)["answer"]["content_hash"],
            execution.answer["content_hash"],
        )

    def test_read_only_store_and_full_scan_accounting(self) -> None:
        fixture = self.root / "input-large"
        shutil.copytree(FIXTURES / "success", fixture)
        large = fixture / "large.txt"
        large.write_text("x" * (1024 * 1024))
        request_value = json.loads((fixture / "request.json").read_text())
        request_value["artifacts"].append({
            "name": "large_log",
            "kind": "diagnostic_log",
            "path": "large.txt",
        })
        (fixture / "request.json").write_text(json.dumps(request_value))
        store = EvidenceStore(self.root / "store-large", {"input": fixture})
        prepared = ChipContextService(store).prepare(fixture / "request.json")
        candidate = CandidateRef(**{
            key: value for key, value in prepared.snapshot["candidate_ref"].items()
            if key != "ref_id"
        })
        scope = QueryScope(
            EvidenceHandle("large", prepared.snapshot["content_hash"]),
            expected_candidate=candidate,
            expected_attempt_id=prepared.evaluation_manifest["attempt_id"],
        )
        large_ref = next(
            row["ref_id"] for row in prepared.evaluation_manifest["artifacts"]
            if row["kind"] == "diagnostic_log"
        )
        registry, request = self.write_cli_inputs(
            stores={"large": store},
            scope=scope,
            operation="read_artifact",
            parameters={"artifact_ref": large_ref, "limit_bytes": 8},
        )
        execution = execute_query(registry, request)
        self.assertGreaterEqual(execution.meter.artifact_scan_bytes, 1024 * 1024)
        self.assertGreater(execution.meter.hash_bytes, 8)
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            EvidenceStore(store.root, read_only=True).publish_alias("bad.json", "{}")

    def test_query_execution_does_not_start_subprocesses(self) -> None:
        _, store, _, _, scope = self.prepare(destination="cli-no-process")
        registry, request = self.write_cli_inputs(
            stores={"primary": store},
            scope=scope,
            operation="candidate_status",
            parameters={},
        )
        with mock.patch("subprocess.Popen", side_effect=AssertionError("Popen called")):
            with mock.patch("subprocess.run", side_effect=AssertionError("run called")):
                execution = execute_query(registry, request)
        self.assertEqual(execution.answer["query"]["name"], "candidate_status")

    def test_clean_query_import_does_not_load_runtime_or_model_modules(self) -> None:
        harness = Path(__file__).parents[2]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(harness)
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "import chia_boom.chipcontext.query_cli; "
                    "import chia_boom.chipcontext.cli; "
                    "assert 'ray' not in sys.modules; "
                    "assert 'chia' not in sys.modules; "
                    "assert 'requests' not in sys.modules"
                ),
            ],
            cwd=harness,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class QueryLineRangeContinuationTests(QueryCliTestCase):
    def prepare_text(self, text: str, destination: str):
        fixture = self.root / f"input-{destination}"
        shutil.copytree(FIXTURES / "success", fixture)
        (fixture / "post_synth_timing_summary.rpt").write_text(text)
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
        )
        ref = next(
            row["ref_id"] for row in prepared.evaluation_manifest["artifacts"]
            if row["kind"] == "vivado_timing_report"
        )
        return store, scope, ref

    def test_line_range_pages_round_trip_without_crossing_the_range(self) -> None:
        text = "outside\n甲乙丙丁\n😀🚀🧠\nafter\n"
        store, scope, ref = self.prepare_text(text, "line-range")
        service = self.service("line-range", store, "public")
        parts = []
        cursor = None
        while True:
            answer = service.read_artifact(
                scope,
                ref,
                start_line=2,
                line_count=2,
                cursor=cursor,
                limit_bytes=5,
            )
            parts.append(answer["result"]["content"])
            cursor = answer["pagination"]["next_cursor"]
            if cursor is None:
                break
        self.assertEqual("".join(parts), "甲乙丙丁\n😀🚀🧠\n")

        registry, request = self.write_cli_inputs(
            stores={"line-range": store},
            scope=scope,
            operation="read_artifact",
            parameters={
                "artifact_ref": ref,
                "start_line": 2,
                "line_count": 2,
                "limit_bytes": 5,
            },
        )
        cli_parts = []
        cursor = None
        while True:
            request_value = json.loads(request.read_text())
            if cursor is None:
                request_value["parameters"].pop("cursor", None)
            else:
                request_value["parameters"]["cursor"] = cursor
            request.write_text(json.dumps(request_value))
            code, output, error = self.run_cli(
                "query", "--registry", str(registry), "--request", str(request)
            )
            self.assertEqual((code, error), (0, ""))
            response = json.loads(output)
            cli_parts.append(response["answer"]["result"]["content"])
            cursor = response["answer"]["pagination"]["next_cursor"]
            if cursor is None:
                break
        self.assertEqual("".join(cli_parts), "甲乙丙丁\n😀🚀🧠\n")

        first = service.read_artifact(
            scope, ref, start_line=2, line_count=2, limit_bytes=5
        )
        with self.assertRaisesRegex(Exception, "does not belong"):
            service.read_artifact(
                scope,
                ref,
                start_line=2,
                line_count=3,
                cursor=first["pagination"]["next_cursor"],
                limit_bytes=5,
            )


if __name__ == "__main__":
    unittest.main()
