from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chia_boom.artifacts import (
    CandidateArtifact,
    EvaluationArtifact,
    StageResult,
    dump_json,
)
from chia_boom.campaign import packaged_process_memory, validate_process_memory_bundle
from chia_boom.deployment import doctor
from chia_boom.finalize import (
    _cached_finalization,
    BoomFinalizationNode,
    build_finalization_identity,
    finalize_candidate,
    finalize_campaign,
)
from chia_boom.frozen import (
    baseline_rtl,
    qualification_request,
    qualified_artifact_hashes,
    seal_frozen_run,
    verify_frozen_run,
    verify_qualification,
)
from chia_boom.interactive import (
    _load_interactive_snapshot,
    _prepare_interactive_snapshot,
)
from chia_boom.nodes import BoomCandidateEvaluationNode, _run, acquire_workspace
from chia_boom.qualification import qualified_parallel_runs
from chia_boom.scripts.prepare_inputs import git_blob
from chia_boom.smoke import run_baseline_smoke
from chia_boom.tools.differential_test import compare_port_signatures


class IntegrityTests(unittest.TestCase):
    def _reference(self, root: Path) -> dict:
        target = root / "inputs/T0"
        rtl = root / "rtl/T0"
        tools = root / "tools"
        tests = root / "tests"
        target.mkdir(parents=True)
        rtl.mkdir(parents=True)
        tools.mkdir(parents=True)
        tests.mkdir(parents=True)
        (target / "baseline-source.scala").write_text("source\n")
        (target / "baseline-timing.txt").write_text("timing\n")
        (target / "baseline-ppa.json").write_text("{}\n")
        (rtl / "Top.sv").write_text("module Top(input clock); endmodule\n")
        for name in (
            "differential_test.py",
            "vivado_ooc_route.tcl",
            "vivado_ooc_synth_only.tcl",
        ):
            (tools / name).write_text(name + "\n")
        (tests / "rsort.riscv").write_bytes(b"rsort")
        patch = root / "required.patch"
        patch.write_text("patch\n")
        return {
            "base": {"chipyard_commit": "c", "boom_commit": "b", "config": "Cfg"},
            "physical": {
                "tool": "Vivado",
                "part": "part",
                "clock_period_ns": 5,
                "maximum_lut_ratio": 1.05,
                "candidate_timeout_seconds": 10,
                "route_timeout_seconds": 10,
                "regression_timeout_seconds": 10,
                "cpus_per_job": 2,
            },
            "search": {"parallel_runs": 2, "infrastructure_attempt_limit": 3},
            "qualification": {"q1_replay_required": True},
            "targets": {"T0": {"id": "T0", "mutable_file": "generators/boom/T.scala"}},
            "remote": {
                "frozen_inputs_root": str(root / "inputs"),
                "baseline_rtl_root": str(root / "rtl"),
                "tools_root": str(tools),
                "rsort_binary": str(tests / "rsort.riscv"),
                "required_patch": str(patch),
            },
        }

    def test_qualification_rejects_config_tool_and_golden_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._reference(root)
            versions = {"python": "3.10", "java": "17", "verilator": "5", "vivado": "2024.1"}
            request = qualification_request(config)
            evidence = {
                "passed": True,
                "qualification_fingerprint": request["fingerprint"],
                "qualified_artifact_hashes": qualified_artifact_hashes(config),
                "tool_versions": versions,
            }
            verify_qualification(config, evidence, tool_versions=versions)
            changed = json.loads(json.dumps(config))
            changed["physical"]["cpus_per_job"] = 4
            with self.assertRaisesRegex(RuntimeError, "contract fingerprint"):
                verify_qualification(changed, evidence, tool_versions=versions)
            (root / "rtl/T0/Top.sv").write_text("module Top(input reset); endmodule\n")
            with self.assertRaisesRegex(RuntimeError, "artifacts changed"):
                verify_qualification(config, evidence, tool_versions=versions)
            (root / "rtl/T0/Top.sv").write_text(
                "module Top(input clock); endmodule\n"
            )
            with self.assertRaisesRegex(RuntimeError, "versions changed"):
                verify_qualification(
                    config, evidence,
                    tool_versions=versions | {"vivado": "2025.1"},
                )

    def test_frozen_run_detects_private_copy_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._reference(root)
            frozen = root / "frozen"
            frozen.mkdir()
            (frozen / "artifact").write_text("stable")
            manifest = seal_frozen_run(frozen, config)
            verify_frozen_run(frozen, manifest["fingerprint"])
            (frozen / "artifact").write_text("mutated")
            with self.assertRaisesRegex(RuntimeError, "drift"):
                verify_frozen_run(frozen, manifest["fingerprint"])

    def test_campaign_golden_is_private_from_later_global_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._reference(root)
            frozen = root / "campaign/frozen"
            private_rtl = frozen / "targets/T0/baseline-rtl"
            private_rtl.mkdir(parents=True)
            (private_rtl / "Top.sv").write_text("private golden\n")
            config["frozen_run_root"] = str(frozen)
            manifest = seal_frozen_run(frozen, config)
            (root / "rtl/T0/Top.sv").write_text("later qualification output\n")
            self.assertEqual(baseline_rtl(config, "T0"), private_rtl)
            self.assertEqual((private_rtl / "Top.sv").read_text(), "private golden\n")
            verify_frozen_run(frozen, manifest["fingerprint"])

    def test_interactive_snapshot_ignores_later_shared_golden_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._reference(root)
            output = root / "interactive"
            output.mkdir()
            frozen_config = _prepare_interactive_snapshot(config, output)
            shared = root / "rtl/T0/Top.sv"
            shared.write_text("later shared golden\n")
            restored = _load_interactive_snapshot(config, output)
            self.assertEqual(restored, frozen_config)
            self.assertEqual(
                (baseline_rtl(restored, "T0") / "Top.sv").read_text(),
                "module Top(input clock); endmodule\n",
            )
            verify_frozen_run(
                output / "frozen",
                json.loads((output / "INTERACTIVE_MANIFEST.json").read_text())[
                    "frozen_run_fingerprint"
                ],
            )

            candidate = CandidateArtifact(
                campaign_id="interactive", target="T0", arm="I", seed=41,
                index=1, parent_id=None, source="changed source", diff="diff",
                diagnosis=[], selected_hypothesis=0, visible_feedback="",
                request_sha256="request", response_sha256="response", usage=None,
            )
            slot = root / "slots/slot-0"
            workspace = slot / "chipyard"
            workspace.mkdir(parents=True)
            elaboration = StageResult(
                stage="elaboration", success=True, status="complete",
                payload={"rtl_dir": str(root / "candidate-rtl")},
            )
            differential = StageResult(
                stage="correctness", success=True, status="complete",
                payload={"differential": {"interface_ok": True}},
            )
            vivado = StageResult(
                stage="synthesis", success=True, status="complete",
                payload={"post_synth": {
                    "critical_delay_ns": 9.0, "slice_luts": 100,
                }},
            )
            baseline = {
                "critical_delay_ns": 10.0, "slice_luts": 100,
                "maximum_lut_ratio": 1.05,
            }
            with (
                mock.patch(
                    "chia_boom.nodes.acquire_workspace",
                    return_value=nullcontext(SimpleNamespace(
                        workspace=workspace, slot_root=slot
                    )),
                ),
                mock.patch(
                    "chia_boom.nodes.BoomElaborationNode.run",
                    return_value=elaboration,
                ),
                mock.patch(
                    "chia_boom.nodes.BoomDifferentialNode.run",
                    return_value=differential,
                ) as differential_run,
                mock.patch(
                    "chia_boom.nodes.BoomVivadoNode.run",
                    return_value=vivado,
                ),
            ):
                result = BoomCandidateEvaluationNode.evaluate.__wrapped__(
                    BoomCandidateEvaluationNode(), candidate=candidate,
                    target=restored["targets"]["T0"], config=restored,
                    baseline=baseline, output_dir=str(root / "evaluation"),
                )
            self.assertTrue(result.candidate_valid)
            evaluation_config = differential_run.call_args.kwargs["config"]
            self.assertEqual(
                baseline_rtl(evaluation_config, "T0"),
                (output / "frozen/targets/T0/baseline-rtl").resolve(),
            )

    def test_prepare_inputs_reads_commit_blob_not_dirty_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            source = repo / "T.scala"
            source.write_text("committed\n")
            subprocess.run(["git", "-C", str(repo), "add", "T.scala"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
            revision = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            source.write_text("dirty\n")
            self.assertEqual(git_blob(repo, revision, "T.scala"), b"committed\n")

    def test_two_slots_require_two_passing_concurrent_resource_probes(self) -> None:
        one_probe = [{"passed": True, "peak_tree_rss_bytes": 8 * 1024 ** 3}]
        slots, combined = qualified_parallel_runs(2, one_probe)
        self.assertEqual(slots, 1)
        self.assertEqual(combined, 8 * 1024 ** 3)
        two_probes = one_probe * 2
        slots, combined = qualified_parallel_runs(2, two_probes)
        self.assertEqual(slots, 2)
        self.assertEqual(combined, 16 * 1024 ** 3)
        slots, _ = qualified_parallel_runs(
            2,
            [
                {"passed": True, "peak_tree_rss_bytes": 13 * 1024 ** 3},
                {"passed": True, "peak_tree_rss_bytes": 13 * 1024 ** 3},
            ],
        )
        self.assertEqual(slots, 1)

    def test_baseline_smoke_freezes_inputs_and_never_records_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._reference(root)
            config["remote"]["install_root"] = str(root / "install")
            config["targets"]["T0"]["rtl_top"] = "Top"
            qualification = root / "install/qualification/QUALIFICATION.json"
            qualification.parent.mkdir(parents=True)
            qualification.write_text('{"passed": true}\n')
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            output = root / "smoke"
            secret = "must-not-appear-in-smoke-evidence"

            def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                run = output / "differential"
                run.mkdir(parents=True)
                (run / "result.json").write_text(json.dumps({
                    "passed": True,
                    "interface_ok": True,
                    "cycles": 1000,
                    "seed": 41,
                }))
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="PASS", stderr="")

            with (
                mock.patch("chia_boom.smoke.verify_qualification"),
                mock.patch("chia_boom.smoke.subprocess.run", side_effect=fake_run),
                mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}),
            ):
                result = run_baseline_smoke(
                    config,
                    config_path=config_path,
                    output=output,
                    target_id="T0",
                    cycles=1000,
                    seed=41,
                )
            self.assertTrue(result["passed"])
            self.assertEqual(result["model_calls"], 0)
            self.assertFalse(result["api_key_used"])
            self.assertNotIn(secret, (output / "SMOKE.json").read_text())
            self.assertTrue((output / "frozen/FROZEN_RUN_MANIFEST.json").is_file())

    def test_deployment_doctor_records_only_credential_presence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._reference(root)
            config.update({
                "version": "test",
                "model": {
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "api_base": "https://api.deepseek.com/v1",
                    "id": "deepseek-v4-pro",
                },
                "environment": {
                    "conda_setup": str(root / "conda.sh"),
                    "vivado_settings": str(root / "vivado.sh"),
                },
                "arms": ["A", "B", "C", "D"],
            })
            config["remote"].update({
                "workspace_slots": str(root / "slots"),
                "install_root": str(root / "install"),
            })
            secret = "doctor-must-not-serialize-this-value"
            with (
                mock.patch("chia_boom.deployment.validate_config"),
                mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}),
            ):
                result = doctor(config)
            rendered = json.dumps(result)
            self.assertNotIn(secret, rendered)
            credential = result["checks"]["model_credential"]
            self.assertIn("present=true", credential["detail"])
            self.assertFalse(credential["required"])

    def test_arm_d_packaged_memory_is_nonempty_and_generic(self) -> None:
        episodes = packaged_process_memory()
        self.assertGreater(len(episodes), 0)
        self.assertTrue(all(
            row["knowledge_class"] == "cross-target-process-memory"
            for row in episodes
        ))
        with self.assertRaisesRegex(RuntimeError, "non-empty"):
            validate_process_memory_bundle([])
        with self.assertRaisesRegex(ValueError, "not cross-target"):
            validate_process_memory_bundle({
                "knowledge_class": "target-specific-solution",
                "access_policy": {"contains_target_specific_solution": True},
            })

    def test_interface_gate_rejects_added_removed_direction_and_width(self) -> None:
        base_text = """module Top(
input logic clock,
input logic [3:0] in,
output logic out
);
endmodule
"""
        cases = (
            base_text.replace("output logic out", "output logic out,\ninput logic extra"),
            base_text.replace("input logic [3:0] in,\n", ""),
            base_text.replace("input logic [3:0] in", "output logic [3:0] in"),
            base_text.replace("[3:0]", "[4:0]"),
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            baseline = root / "baseline"
            candidate = root / "candidate"
            baseline.mkdir()
            candidate.mkdir()
            (baseline / "Top.sv").write_text(base_text)
            for text in cases:
                (candidate / "Top.sv").write_text(text)
                ok, _, _ = compare_port_signatures(baseline, candidate, "Top")
                self.assertFalse(ok)
            (candidate / "Top.sv").write_text(
                base_text.replace("input logic clock", "inout logic clock")
            )
            with self.assertRaisesRegex(RuntimeError, "unsupported"):
                compare_port_signatures(baseline, candidate, "Top")

    def test_timeout_kills_background_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rc, _, _, _ = _run(
                "sleep 30 & child=$!; echo $child > child.pid; wait",
                cwd=root,
                stdout_path=root / "stdout",
                stderr_path=root / "stderr",
                timeout=1,
            )
            self.assertEqual(rc, -9)
            pid = int((root / "child.pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_cleanup_failure_quarantines_explicit_slot_and_skips_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            slot0 = root / "slots/slot-0"
            slot1 = root / "slots/slot-1"
            (slot0 / "chipyard").mkdir(parents=True)
            (slot1 / "chipyard").mkdir(parents=True)
            evidence = root / "campaign/evidence"
            evidence.mkdir(parents=True)
            fake = mock.Mock(pid=12345, returncode=None)
            fake.communicate.side_effect = subprocess.TimeoutExpired(
                cmd="fake", timeout=1
            )
            with (
                mock.patch("chia_boom.nodes.subprocess.Popen", return_value=fake),
                mock.patch(
                    "chia_boom.nodes._terminate_process_group",
                    side_effect=RuntimeError("cleanup failed"),
                ),
                self.assertRaises(RuntimeError),
            ):
                _run(
                    "fake", cwd=evidence,
                    stdout_path=evidence / "stdout",
                    stderr_path=evidence / "stderr", timeout=1,
                    workspace_slot=slot0,
                )
            self.assertTrue((slot0 / "QUARANTINED").exists())
            with acquire_workspace({
                "workspace_slots": str(root / "slots"),
                "workspace_wait_seconds": 1,
            }) as lease:
                self.assertEqual(lease.slot_root, slot1)

    def test_regression_infrastructure_failure_survives_full_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._reference(root)
            target = config["targets"]["T0"]
            baseline = {
                "critical_delay_ns": 10.0,
                "post_route_critical_delay_ns": 11.0,
                "slice_luts": 100,
                "maximum_lut_ratio": 1.05,
            }
            candidate = CandidateArtifact(
                campaign_id="test", target="T0", arm="C", seed=42, index=1,
                parent_id=None, source="candidate source", diff="diff",
                diagnosis=[], selected_hypothesis=0, visible_feedback="",
                request_sha256="request", response_sha256="response", usage=None,
            )
            early = EvaluationArtifact(
                candidate_id=candidate.id, candidate_valid=True,
                post_synth={"critical_delay_ns": 9.0, "slice_luts": 100},
            )
            slot = root / "slots/slot-0"
            workspace = slot / "chipyard"
            workspace.mkdir(parents=True)
            elaboration = StageResult(
                stage="elaboration", success=True, status="complete",
                payload={"rtl_dir": str(root / "rtl-candidate")},
            )
            differential = StageResult(
                stage="correctness", success=True, status="complete",
                payload={"differential": {"interface_ok": True}},
            )
            route = StageResult(
                stage="route", success=True, status="complete",
                payload={"post_route": {
                    "critical_delay_ns": 10.0, "slice_luts": 100,
                }},
            )
            regression = StageResult(
                stage="regression", success=False, status="infra_blocked",
                failure_class="regression_infrastructure_failure",
                retryable=True, message="worker disappeared",
            )
            actual_node = BoomFinalizationNode()

            class Remote:
                def __init__(self) -> None:
                    self.calls = 0

                def chia_remote(self, _node: object, **kwargs: object) -> EvaluationArtifact:
                    self.calls += 1
                    kwargs.pop("_chia_display_name", None)
                    return BoomFinalizationNode.run.__wrapped__(
                        actual_node, **kwargs
                    )

            remote = Remote()
            node = SimpleNamespace(run=remote)
            patches = (
                mock.patch(
                    "chia_boom.finalize.acquire_workspace",
                    return_value=nullcontext(SimpleNamespace(
                        workspace=workspace, slot_root=slot
                    )),
                ),
                mock.patch(
                    "chia_boom.finalize.BoomElaborationNode.run",
                    return_value=elaboration,
                ),
                mock.patch(
                    "chia_boom.finalize.BoomDifferentialNode.run",
                    return_value=differential,
                ),
                mock.patch(
                    "chia_boom.finalize.BoomVivadoNode.run",
                    return_value=route,
                ),
                mock.patch(
                    "chia_boom.finalize.BoomRegressionNode.run",
                    return_value=regression,
                ),
                mock.patch(
                    "chia.base.ChiaFunction.get", side_effect=lambda value: value
                ),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                first, first_meta = finalize_candidate(
                    node=node, candidate=candidate, early=early, target=target,
                    config=config, baseline=baseline,
                    root=root / "finalization", display_name="test",
                )
                second, second_meta = finalize_candidate(
                    node=node, candidate=candidate, early=early, target=target,
                    config=config, baseline=baseline,
                    root=root / "finalization", display_name="test",
                )
            self.assertEqual(first.status, "infra_blocked")
            self.assertTrue(first.retryable)
            self.assertEqual(first.failure_class, "regression_infrastructure_failure")
            self.assertEqual(second.status, "infra_blocked")
            self.assertEqual(remote.calls, 2)
            self.assertEqual(first_meta["attempt"], 1)
            self.assertEqual(second_meta["attempt"], 2)

    def test_repeated_campaign_finalization_preserves_first_acceptance_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            campaign = Path(raw)
            candidate = CandidateArtifact(
                campaign_id="test", target="T0", arm="C", seed=42, index=1,
                parent_id=None, source="candidate source", diff="diff",
                diagnosis=[], selected_hypothesis=0, visible_feedback="",
                request_sha256="request", response_sha256="response", usage=None,
            )
            config = {
                "physical": {"maximum_lut_ratio": 1.05},
                "targets": {"T0": {"id": "T0"}},
            }
            dump_json(campaign / "manifest.json", {
                "config": config, "frozen_run_fingerprint": "frozen",
                "schedule": [{"id": "T0-C-seed42"}],
            })
            dump_json(campaign / "frozen/targets/T0/baseline-ppa.json", {
                "critical_delay_ns": 10.0,
                "post_route_critical_delay_ns": 11.0,
                "slice_luts": 100,
            })
            early = EvaluationArtifact(
                candidate_id=candidate.id, candidate_valid=True,
                post_synth={"critical_delay_ns": 9.0, "slice_luts": 100},
            )
            dump_json(campaign / "runs/T0-C-seed42/run.json", {
                "target": "T0", "status": "search_complete",
                "started_epoch": 100.0,
                "candidates": [{
                    "candidate": candidate.to_dict(),
                    "search_evaluation": early.to_dict(),
                }],
            })
            final = EvaluationArtifact(
                candidate_id=candidate.id, status="complete",
                final_valid=True, valid_improvement=True,
                post_route={"critical_delay_ns": 8.0, "slice_luts": 100},
            )
            metadata = {
                "reused": False, "completed_epoch": 140.0,
                "attempts": [{"attempt": 1, "active_seconds": 5.0}],
            }
            with (
                mock.patch("chia_boom.finalize.verify_frozen_run"),
                mock.patch(
                    "chia_boom.finalize.finalize_candidate",
                    return_value=(final, metadata),
                ),
                mock.patch("chia_boom.finalize.time.time", side_effect=(150.0, 200.0)),
            ):
                finalize_campaign(campaign)
                first = json.loads(
                    (campaign / "runs/T0-C-seed42/run.json").read_text()
                )
                finalize_campaign(campaign)
                second = json.loads(
                    (campaign / "runs/T0-C-seed42/run.json").read_text()
                )
            self.assertEqual(first["first_final_acceptance_epoch"], 140.0)
            self.assertEqual(second["first_final_acceptance_epoch"], 140.0)
            self.assertEqual(second["finalization_completed_epoch"], 140.0)
            self.assertEqual(second["last_finalization_query_epoch"], 200.0)

    def test_finalization_cache_binds_candidate_source_and_retries_infra(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            identity = {
                "fingerprint": "contract-a",
                "candidate_id": "candidate-a",
                "candidate_source_sha256": "source-a",
            }
            wrong = EvaluationArtifact(candidate_id="candidate-b", status="complete")
            wrong.hashes["candidate_source_sha256"] = "source-b"
            dump_json(root / "attempt-01/IDENTITY.json", identity)
            dump_json(root / "attempt-01/evaluation.json", wrong)
            cached, attempt, reasons = _cached_finalization(root, identity)
            self.assertIsNone(cached)
            self.assertEqual(attempt, 2)
            self.assertIn("candidate_mismatch", reasons[0]["reasons"])

            infra = EvaluationArtifact(
                candidate_id="candidate-a", status="infra_blocked", retryable=True
            )
            infra.hashes["candidate_source_sha256"] = "source-a"
            dump_json(root / "attempt-02/IDENTITY.json", identity)
            dump_json(root / "attempt-02/evaluation.json", infra)
            cached, attempt, _ = _cached_finalization(root, identity)
            self.assertIsNone(cached)
            self.assertEqual(attempt, 3)

            complete = EvaluationArtifact(candidate_id="candidate-a", status="complete")
            complete.hashes["candidate_source_sha256"] = "source-a"
            dump_json(root / "attempt-03/IDENTITY.json", identity)
            dump_json(root / "attempt-03/evaluation.json", complete)
            cached, _, _ = _cached_finalization(root, identity)
            self.assertIsNotNone(cached)
            changed_contract = identity | {"fingerprint": "contract-b"}
            cached, _, reasons = _cached_finalization(root, changed_contract)
            self.assertIsNone(cached)
            self.assertTrue(all(
                "identity_mismatch" in row["reasons"] for row in reasons
            ))

    def test_retryable_finalization_schedules_new_attempt_then_reuses_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._reference(root)
            target = config["targets"]["T0"]
            baseline = {
                "critical_delay_ns": 10.0,
                "post_route_critical_delay_ns": 11.0,
                "slice_luts": 100,
                "maximum_lut_ratio": 1.05,
            }
            candidate = CandidateArtifact(
                campaign_id="test", target="T0", arm="C", seed=42, index=1,
                parent_id=None, source="candidate source", diff="diff",
                diagnosis=[], selected_hypothesis=0, visible_feedback="",
                request_sha256="request", response_sha256="response", usage=None,
            )
            early = EvaluationArtifact(
                candidate_id=candidate.id,
                build_ok=True,
                interface_ok=True,
                correctness_ok=True,
                candidate_valid=True,
                post_synth={"critical_delay_ns": 9.0, "slice_luts": 100},
            )
            final_root = root / "finalization"
            identity = build_finalization_identity(
                candidate, target, config, baseline
            )
            infra = EvaluationArtifact(
                candidate_id=candidate.id,
                status="infra_blocked",
                retryable=True,
            )
            infra.hashes["candidate_source_sha256"] = identity[
                "candidate_source_sha256"
            ]
            dump_json(final_root / "attempt-01/IDENTITY.json", identity)
            dump_json(final_root / "attempt-01/evaluation.json", infra)

            complete = EvaluationArtifact(
                candidate_id=candidate.id, status="complete", final_valid=True
            )

            class FakeRemote:
                def __init__(self) -> None:
                    self.calls = 0

                def chia_remote(self, *_args: object, **_kwargs: object) -> EvaluationArtifact:
                    self.calls += 1
                    return complete

            remote = FakeRemote()
            node = SimpleNamespace(run=remote)
            with mock.patch("chia.base.ChiaFunction.get", side_effect=lambda value: value):
                result, metadata = finalize_candidate(
                    node=node,
                    candidate=candidate,
                    early=early,
                    target=target,
                    config=config,
                    baseline=baseline,
                    root=final_root,
                    display_name="test",
                )
                again, reused = finalize_candidate(
                    node=node,
                    candidate=candidate,
                    early=early,
                    target=target,
                    config=config,
                    baseline=baseline,
                    root=final_root,
                    display_name="test",
                )
            self.assertEqual(remote.calls, 1)
            self.assertEqual(metadata["attempt"], 2)
            self.assertTrue((final_root / "attempt-02/evaluation.json").exists())
            self.assertEqual(result.candidate_id, candidate.id)
            self.assertTrue(reused["reused"])
            self.assertEqual(again.candidate_id, candidate.id)
            self.assertIsNotNone(metadata["completed_epoch"])
            self.assertEqual(
                reused["completed_epoch"], metadata["completed_epoch"]
            )


if __name__ == "__main__":
    unittest.main()
