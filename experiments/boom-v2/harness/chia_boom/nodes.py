from __future__ import annotations

import fcntl
import json
import os
import signal
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import psutil

from chia.base.ChiaFunction import ChiaFunction

from .artifacts import (
    CandidateArtifact,
    EvaluationArtifact,
    StageResult,
    dump_json,
    sha256_file,
)
from .core import classify_failure, update_validity
from .environment import chipyard_environment_command, vivado_environment_command
from .frozen import baseline_rtl, tool_file
from .chipcontext.extractors.vivado import parse_ppa_bytes


def _tail(stdout: str, stderr: str, limit: int = 32_000) -> str:
    text = (stderr + "\n" + stdout).strip()
    return text[-limit:]


def _differential_failure_message(output: Path, stdout: str, stderr: str, result: Any) -> str:
    parts = []
    driver = _tail(stdout, stderr)
    if driver:
        parts.append("DRIVER\n" + driver)
    for label, relative in (
        ("VERILATOR_BUILD_STDOUT", "run/build.stdout"),
        ("VERILATOR_BUILD_STDERR", "run/build.stderr"),
        ("SIMULATOR_STDOUT", "run/run.stdout"),
        ("SIMULATOR_STDERR", "run/run.stderr"),
    ):
        path = output / relative
        if path.exists():
            content = path.read_text(errors="replace").strip()
            if content:
                parts.append(label + "\n" + content[-32_000:])
    parts.append("RESULT\n" + json.dumps(result, sort_keys=True))
    return "\n\n".join(parts)[-96_000:]


def _run(
    command: str,
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout: int,
    env: dict[str, str] | None = None,
    workspace_slot: Path | None = None,
) -> tuple[int, str, str, float]:
    started = time.monotonic()
    proc = subprocess.Popen(
        ["bash", "-lc", command],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **(env or {})},
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            stdout, stderr = _terminate_process_group(
                proc, workspace_slot=workspace_slot, graceful=True
            )
        except BaseException:
            if workspace_slot is not None:
                _quarantine_workspace(
                    workspace_slot, "process group cleanup failed after timeout"
                )
            raise
        stderr += f"\ncommand timeout after {timeout}s"
        returncode = -9
    except BaseException:
        try:
            _terminate_process_group(
                proc, workspace_slot=workspace_slot, graceful=False
            )
        except BaseException as cleanup_error:
            if workspace_slot is not None:
                _quarantine_workspace(
                    workspace_slot, "process group cleanup failed after exception"
                )
            raise RuntimeError("process_group_cleanup_failed") from cleanup_error
        raise
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(stdout)
    stderr_path.write_text(stderr)
    return returncode, stdout, stderr, time.monotonic() - started


def _terminate_process_group(
    proc: subprocess.Popen[str],
    *,
    workspace_slot: Path | None,
    graceful: bool,
) -> tuple[str, str]:
    if graceful:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        else:
            _require_process_group_gone(proc.pid, workspace_slot)
            return stdout, stderr
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        stdout, stderr = proc.communicate(timeout=10)
    except BaseException as exc:
        if workspace_slot is not None:
            _quarantine_workspace(
                workspace_slot, f"cannot reap process group: {type(exc).__name__}"
            )
        raise RuntimeError("process_group_cleanup_failed") from exc
    _require_process_group_gone(proc.pid, workspace_slot)
    return stdout, stderr


def _require_process_group_gone(
    process_group: int, workspace_slot: Path | None
) -> None:
    deadline = time.monotonic() + 2
    while True:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return
        live_members = []
        for process in psutil.process_iter(["pid", "status"]):
            try:
                if (
                    os.getpgid(process.pid) == process_group
                    and process.info.get("status") != psutil.STATUS_ZOMBIE
                ):
                    live_members.append(process.pid)
            except (ProcessLookupError, PermissionError, psutil.Error):
                continue
        # A reaped group leader can briefly leave an orphaned zombie visible
        # to killpg(0) on some Linux init implementations. Zombies cannot run
        # or retain EDA resources, so they are not a cleanup failure.
        if not live_members:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if workspace_slot is not None:
        _quarantine_workspace(workspace_slot, "process group survived cleanup")
    raise RuntimeError("process_group_cleanup_failed")


def _quarantine_workspace(slot_root: Path, reason: str) -> None:
    slot_root.mkdir(parents=True, exist_ok=True)
    (slot_root / "QUARANTINED").write_text(reason + "\n")


@dataclass(frozen=True)
class WorkspaceLease:
    workspace: Path
    slot_root: Path

    def quarantine(self, reason: str) -> None:
        _quarantine_workspace(self.slot_root, reason)


def parse_vivado_ppa(timing: Path, utilization: Path, period_ns: float) -> dict[str, Any]:
    # One parser defines both the evaluator's score and ChipContext's raw facts.
    # Reading once per file also ensures hashes describe the parsed bytes.
    return parse_ppa_bytes(timing.read_bytes(), utilization.read_bytes(), period_ns)


@contextmanager
def acquire_workspace(remote: dict[str, Any]) -> Iterator[WorkspaceLease]:
    slots = Path(remote["workspace_slots"])
    deadline = time.monotonic() + int(remote.get("workspace_wait_seconds", 7200))
    while time.monotonic() < deadline:
        for workspace in sorted(slots.glob("slot-*/chipyard")):
            if (workspace.parent / "QUARANTINED").exists():
                continue
            lock_path = workspace.parent / "candidate.lock"
            handle = lock_path.open("a+")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            if (workspace.parent / "QUARANTINED").exists():
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
                continue
            lease = WorkspaceLease(
                workspace=workspace, slot_root=workspace.parent
            )
            try:
                yield lease
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
            return
        time.sleep(5)
    raise TimeoutError("no isolated BOOM workspace became available")


def _target_boom_relative(mutable_file: str) -> str:
    prefix = "generators/boom/"
    if not mutable_file.startswith(prefix):
        raise ValueError("mutable file must be inside generators/boom")
    return mutable_file[len(prefix):]


class BoomElaborationNode:
    @ChiaFunction(resources={"chipyard": 1}, max_retries=0)
    def run(
        self,
        *,
        candidate: CandidateArtifact,
        target: dict[str, Any],
        config: dict[str, Any],
        workspace: str,
        output_dir: str,
    ) -> StageResult:
        started = time.monotonic()
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        stdout_path = output / "elaboration.stdout"
        stderr_path = output / "elaboration.stderr"
        chipyard = Path(workspace)
        boom = chipyard / "generators/boom"
        remote = config["remote"]
        base = config["base"]
        try:
            commands = [
                f"git -C {shlex_quote(chipyard)} rev-parse HEAD",
                f"git -C {shlex_quote(boom)} reset --hard {shlex_quote(base['boom_commit'])}",
                f"git -C {shlex_quote(boom)} clean -fd",
            ]
            required_patch = remote.get("required_patch")
            if required_patch:
                commands.append(
                    f"git -C {shlex_quote(boom)} apply -p3 "
                    f"{shlex_quote(Path(required_patch))}"
                )
            setup = subprocess.run(
                ["bash", "-lc", " && ".join(commands)],
                text=True,
                capture_output=True,
                timeout=600,
            )
            stdout_path.write_text(setup.stdout)
            stderr_path.write_text(setup.stderr)
            if setup.returncode:
                failure, retryable = classify_failure(
                    _tail(setup.stdout, setup.stderr), returncode=setup.returncode
                )
                return StageResult(
                    stage="materialize", success=False,
                    status="infra_blocked" if retryable else "candidate_invalid",
                    failure_class=failure, retryable=retryable,
                    message=_tail(setup.stdout, setup.stderr),
                    stdout_path=str(stdout_path), stderr_path=str(stderr_path),
                    active_seconds=time.monotonic() - started,
                )
            chipyard_head = setup.stdout.splitlines()[0].strip() if setup.stdout else ""
            if chipyard_head != base["chipyard_commit"]:
                return StageResult(
                    stage="materialize", success=False, status="infra_blocked",
                    failure_class="environment_drift", retryable=False,
                    message=f"Chipyard commit drift: {chipyard_head}",
                    active_seconds=time.monotonic() - started,
                )
            mutable = boom / _target_boom_relative(target["mutable_file"])
            tmp = mutable.with_suffix(mutable.suffix + ".candidate")
            tmp.write_text(candidate.source)
            tmp.replace(mutable)
            diff_path = output / "applied.diff"
            source_path = output / "candidate-source.scala"
            source_path.write_text(candidate.source)
            diff_check = subprocess.run(
                ["git", "-C", str(boom), "diff", "--check"],
                text=True, capture_output=True, timeout=60,
            )
            diff = subprocess.run(
                ["git", "-C", str(boom), "diff"],
                text=True, capture_output=True, timeout=60,
            )
            diff_path.write_text(diff.stdout)
            if diff_check.returncode:
                return StageResult(
                    stage="materialize", success=False, status="candidate_invalid",
                    failure_class="candidate_diff_invalid", retryable=False,
                    message=_tail(diff_check.stdout, diff_check.stderr),
                    stdout_path=str(stdout_path), stderr_path=str(stderr_path),
                    payload={"candidate_source_path": str(source_path), "candidate_diff_path": str(diff_path)},
                    active_seconds=time.monotonic() - started,
                )
            boom_config = base["config"]
            generated = chipyard / (
                "sims/verilator/generated-src/chipyard.harness.TestHarness."
                + boom_config
            )
            command = (
                chipyard_environment_command(config, chipyard) + "; set -u; "
                f"rm -f {shlex_quote(chipyard / '.classpath_cache/chipyard.jar')}; "
                f"rm -rf {shlex_quote(generated)}; "
                f"cd {shlex_quote(chipyard / 'sims/verilator')}; "
                f"MAKEFLAGS=-j{int(config['physical']['cpus_per_job'])} make verilog "
                f"CONFIG={shlex_quote(boom_config)}"
            )
            rc, stdout, stderr, elapsed = _run(
                command, cwd=chipyard, stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=int(config["physical"]["candidate_timeout_seconds"]),
                workspace_slot=chipyard.parent,
            )
            if rc:
                failure, retryable = classify_failure(_tail(stdout, stderr), returncode=rc)
                return StageResult(
                    stage="elaboration", success=False,
                    status="infra_blocked" if retryable else "candidate_invalid",
                    failure_class=failure, retryable=retryable,
                    message=_tail(stdout, stderr),
                    stdout_path=str(stdout_path), stderr_path=str(stderr_path),
                    payload={"candidate_source_path": str(source_path), "candidate_diff_path": str(diff_path)},
                    active_seconds=elapsed,
                )
            collateral = generated / "gen-collateral"
            rtl_dir = output / "rtl"
            if rtl_dir.exists():
                shutil.rmtree(rtl_dir)
            rtl_dir.mkdir()
            for pattern in target["rtl_files"]:
                matches = list(collateral.glob(pattern))
                if not matches:
                    return StageResult(
                        stage="elaboration", success=False, status="candidate_invalid",
                        failure_class="target_rtl_missing", retryable=False,
                        message=f"no generated RTL matched {pattern}",
                        active_seconds=elapsed,
                    )
                for source in matches:
                    shutil.copy2(source, rtl_dir / source.name)
            hashes = {path.name: sha256_file(path) for path in sorted(rtl_dir.glob("*.sv"))}
            return StageResult(
                stage="elaboration", success=True, status="complete",
                stdout_path=str(stdout_path), stderr_path=str(stderr_path),
                payload={
                    "workspace": str(chipyard), "rtl_dir": str(rtl_dir),
                    "candidate_source_path": str(source_path),
                    "candidate_diff_path": str(diff_path), "rtl_hashes": hashes,
                },
                active_seconds=elapsed,
            )
        except Exception as exc:
            failure, retryable = classify_failure(str(exc))
            return StageResult(
                stage="elaboration", success=False,
                status="infra_blocked" if retryable else "candidate_invalid",
                failure_class=failure, retryable=retryable,
                message=f"{type(exc).__name__}: {exc}",
                stdout_path=str(stdout_path), stderr_path=str(stderr_path),
                active_seconds=time.monotonic() - started,
            )


class BoomDifferentialNode:
    @ChiaFunction(resources={"boom_sim": 1}, max_retries=0)
    def run(
        self,
        *,
        target: dict[str, Any],
        config: dict[str, Any],
        rtl_dir: str,
        seed: int,
        output_dir: str,
        cycles: int,
        workspace_slot: str,
    ) -> StageResult:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        stdout_path, stderr_path = output / "driver.stdout", output / "driver.stderr"
        golden_rtl = baseline_rtl(config, target["id"])
        command = (
            f"python3 {shlex_quote(tool_file(config, 'differential_test.py'))} "
            f"--baseline {shlex_quote(golden_rtl)} --candidate {shlex_quote(Path(rtl_dir))} "
            f"--top {shlex_quote(target['rtl_top'])} --out {shlex_quote(output / 'run')} "
            f"--cycles {int(cycles)} --seed {int(seed)} --scenario {shlex_quote(target['id'])} "
            f"--jobs {int(config['physical']['cpus_per_job'])}"
        )
        rc, stdout, stderr, elapsed = _run(
            command, cwd=output, stdout_path=stdout_path, stderr_path=stderr_path,
            timeout=int(config["physical"]["candidate_timeout_seconds"]),
            workspace_slot=Path(workspace_slot),
        )
        result_path = output / "run/result.json"
        result = json.loads(result_path.read_text()) if result_path.exists() else None
        if rc or not result or not result.get("passed"):
            message = _differential_failure_message(output, stdout, stderr, result)
            failure, retryable = classify_failure(message, returncode=rc)
            if not retryable:
                failure = (
                    result.get("failure_class")
                    if isinstance(result, dict) and result.get("failure_class")
                    else "candidate_correctness_failure"
                )
            return StageResult(
                stage="correctness", success=False,
                status="infra_blocked" if retryable else "candidate_invalid",
                failure_class=failure, retryable=retryable,
                message=message,
                stdout_path=str(stdout_path), stderr_path=str(stderr_path),
                payload={"differential": result}, active_seconds=elapsed,
            )
        return StageResult(
            stage="correctness", success=True, status="complete",
            stdout_path=str(stdout_path), stderr_path=str(stderr_path),
            payload={"differential": result}, active_seconds=elapsed,
        )


class BoomVivadoNode:
    @ChiaFunction(resources={"boom_vivado": 1}, max_retries=0)
    def run(
        self,
        *,
        target: dict[str, Any],
        config: dict[str, Any],
        rtl_dir: str,
        output_dir: str,
        route: bool,
        workspace_slot: str,
    ) -> StageResult:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        stdout_path, stderr_path = output / "vivado.stdout", output / "vivado.stderr"
        script = "vivado_ooc_route.tcl" if route else "vivado_ooc_synth_only.tcl"
        command = (
            vivado_environment_command(config) + "; "
            f"vivado -mode batch -notrace -source "
            f"{shlex_quote(tool_file(config, script))}"
        )
        env = {
            "RTL_DIR": str(Path(rtl_dir)), "OUT_DIR": str(output),
            "TOP": target["rtl_top"], "PART": config["physical"]["part"],
            "CLOCK_PERIOD_NS": str(config["physical"]["clock_period_ns"]),
            "VIVADO_THREADS": str(config["physical"]["cpus_per_job"]),
        }
        timeout = int(
            config["physical"]["route_timeout_seconds"]
            if route else config["physical"]["candidate_timeout_seconds"]
        )
        rc, stdout, stderr, elapsed = _run(
            command, cwd=output, stdout_path=stdout_path, stderr_path=stderr_path,
            timeout=timeout, env=env, workspace_slot=Path(workspace_slot),
        )
        stage = "route" if route else "synthesis"
        prefix = "post_route" if route else "post_synth"
        timing = output / f"{prefix}_timing_summary.rpt"
        utilization = output / f"{prefix}_utilization.rpt"
        if rc or not timing.exists() or not utilization.exists():
            failure, retryable = classify_failure(_tail(stdout, stderr), returncode=rc)
            if not retryable:
                failure = "candidate_vivado_failure"
            return StageResult(
                stage=stage, success=False,
                status="infra_blocked" if retryable else "candidate_invalid",
                failure_class=failure, retryable=retryable,
                message=_tail(stdout, stderr), stdout_path=str(stdout_path),
                stderr_path=str(stderr_path), active_seconds=elapsed,
            )
        try:
            ppa = parse_vivado_ppa(
                timing, utilization, float(config["physical"]["clock_period_ns"])
            )
        except Exception as exc:
            return StageResult(
                stage=stage, success=False, status="candidate_invalid",
                failure_class="vivado_report_parse_failure", retryable=False,
                message=f"{type(exc).__name__}: {exc}",
                stdout_path=str(stdout_path), stderr_path=str(stderr_path),
                active_seconds=elapsed,
            )
        return StageResult(
            stage=stage, success=True, status="complete",
            stdout_path=str(stdout_path), stderr_path=str(stderr_path),
            payload={prefix: ppa}, active_seconds=elapsed,
        )


class BoomCandidateEvaluationNode:
    """One durable CHIA task owns a workspace for the complete early pipeline."""

    @ChiaFunction(
        resources={"chipyard": 1, "boom_sim": 1, "boom_vivado": 1},
        max_retries=0,
    )
    def evaluate(
        self,
        *,
        candidate: CandidateArtifact,
        target: dict[str, Any],
        config: dict[str, Any],
        baseline: dict[str, Any],
        output_dir: str,
    ) -> EvaluationArtifact:
        evaluation = EvaluationArtifact(candidate_id=candidate.id)
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        if not candidate.diff:
            evaluation.failure_class = "candidate_noop"
            evaluation.raw_error = "candidate_source_unchanged"
            evaluation.status = "candidate_invalid"
            dump_json(output / "evaluation.json", evaluation)
            return evaluation
        try:
            with acquire_workspace(config["remote"]) as lease:
                workspace = lease.workspace
                elaboration = BoomElaborationNode().run(
                    candidate=candidate, target=target, config=config,
                    workspace=str(workspace), output_dir=str(output / "elaboration"),
                )
                evaluation.append_stage(elaboration)
                evaluation.candidate_source_path = elaboration.payload.get("candidate_source_path")
                evaluation.candidate_diff_path = elaboration.payload.get("candidate_diff_path")
                evaluation.hashes.update(elaboration.payload.get("rtl_hashes", {}))
                if not elaboration.success:
                    dump_json(output / "evaluation.json", evaluation)
                    return evaluation
                evaluation.build_ok = True
                differential = BoomDifferentialNode().run(
                    target=target, config=config,
                    rtl_dir=elaboration.payload["rtl_dir"], seed=candidate.seed,
                    output_dir=str(output / "differential"), cycles=1_000_000,
                    workspace_slot=str(lease.slot_root),
                )
                evaluation.append_stage(differential)
                evaluation.differential = differential.payload.get("differential")
                if not differential.success:
                    dump_json(output / "evaluation.json", evaluation)
                    return evaluation
                evaluation.interface_ok = bool(
                    evaluation.differential.get("interface_ok")
                    if isinstance(evaluation.differential, dict) else False
                )
                evaluation.correctness_ok = True
                vivado = BoomVivadoNode().run(
                    target=target, config=config,
                    rtl_dir=elaboration.payload["rtl_dir"],
                    output_dir=str(output / "vivado"), route=False,
                    workspace_slot=str(lease.slot_root),
                )
                evaluation.append_stage(vivado)
                evaluation.post_synth = vivado.payload.get("post_synth")
                if not vivado.success:
                    dump_json(output / "evaluation.json", evaluation)
                    return evaluation
                evaluation.status = "complete"
                evaluation.stage = "synthesis"
                evaluation.retryable = False
                update_validity(evaluation, baseline)
                dump_json(output / "evaluation.json", evaluation)
                return evaluation
        except Exception as exc:
            failure, retryable = classify_failure(str(exc))
            evaluation.status = "infra_blocked" if retryable else "candidate_invalid"
            evaluation.failure_class = failure
            evaluation.retryable = retryable
            evaluation.raw_error = f"{type(exc).__name__}: {exc}"
            dump_json(output / "evaluation.json", evaluation)
            return evaluation


def shlex_quote(value: str | Path) -> str:
    import shlex

    return shlex.quote(str(value))
