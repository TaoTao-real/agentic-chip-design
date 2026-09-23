from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chia_boom.artifacts import CandidateArtifact, EvaluationArtifact, dump_json
from chia_boom.campaign import packaged_process_memory, validate_process_memory_bundle
from chia_boom.finalize import (
    _cached_finalization,
    build_finalization_identity,
    finalize_candidate,
)
from chia_boom.frozen import (
    baseline_rtl,
    qualification_request,
    qualified_artifact_hashes,
    seal_frozen_run,
    verify_frozen_run,
    verify_qualification,
)
from chia_boom.nodes import _run
from chia_boom.scripts.prepare_inputs import git_blob
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


if __name__ == "__main__":
    unittest.main()
