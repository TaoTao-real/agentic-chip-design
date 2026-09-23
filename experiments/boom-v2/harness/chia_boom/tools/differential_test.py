#!/usr/bin/env python3
"""Cycle-exact baseline/candidate differential test with directed phases."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


PORT = re.compile(
    r"^\s*(input|output)\s+(?:wire\s+|reg\s+|logic\s+)?"
    r"(?:\[([^]]+)\]\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*,?",
    re.M,
)


def ports(path: Path, top: str) -> list[tuple[str, str, int, str]]:
    text = path.read_text(errors="replace")
    match = re.search(rf"module\s+{re.escape(top)}\s*\((.*?)\n\);", text, re.S)
    if not match:
        raise RuntimeError(f"cannot find ANSI port list for {top}")
    result = []
    for direction, width, name in PORT.findall(match.group(1)):
        if width:
            nums = [int(value) for value in re.findall(r"\d+", width)]
            bits, declaration = abs(nums[0] - nums[1]) + 1, f"[{width}]"
        else:
            bits, declaration = 1, ""
        result.append((direction, declaration, bits, name))
    if not result:
        raise RuntimeError("no ports parsed")
    return result


def rename_modules(source: Path, target: Path, suffix: str) -> None:
    texts = {path.name: path.read_text(errors="replace") for path in source.glob("*.sv")}
    names = sorted(
        {name for text in texts.values() for name in re.findall(r"(?m)^module\s+([A-Za-z_][A-Za-z0-9_]*)", text)},
        key=len,
        reverse=True,
    )
    for filename, text in texts.items():
        for name in names:
            text = re.sub(rf"\b{re.escape(name)}\b", name + suffix, text)
        (target / (Path(filename).stem + suffix + ".sv")).write_text(text)


def random_expr(bits: int) -> str:
    words = max(1, (bits + 31) // 32)
    return "$urandom" if words == 1 else "{" + ",".join(["$urandom"] * words) + "}"


def directed_lines(scenario: str, inputs: list[tuple[str, str, int, str]]) -> tuple[list[str], list[str]]:
    names = {entry[3] for entry in inputs}
    assignable = sorted(names - {"clock", "reset"})
    lines = ["if (cycle >= 8 && cycle < 128) begin"]
    lines.extend(f"  {name} = '0;" for name in assignable)
    phases: list[str]
    if scenario == "T0-issue-queue":
        phases = [
            "idle", "dispatch", "backpressure", "wakeup", "issue",
            "branch_recovery", "flush", "mixed",
        ]
        for index in range(4):
            valid = f"io_dis_uops_{index}_valid"
            fu_code = f"io_dis_uops_{index}_bits_fu_code"
            prs1_busy = f"io_dis_uops_{index}_bits_prs1_busy"
            prs2_busy = f"io_dis_uops_{index}_bits_prs2_busy"
            if valid in names:
                lines.append(f"  if (cycle >= 16 && cycle < 48) {valid} = 1;")
                lines.append(f"  if (cycle >= 112) {valid} = ((cycle + {index}) & 1);")
            if fu_code in names:
                lines.append(f"  if (cycle >= 16) {fu_code} = 10'h001;")
            if prs1_busy in names:
                lines.append(f"  if (cycle >= 32 && cycle < 64) {prs1_busy} = 1;")
            if prs2_busy in names:
                lines.append(f"  if (cycle >= 32 && cycle < 64) {prs2_busy} = 1;")
        for index in range(16):
            valid = f"io_wakeup_ports_{index}_valid"
            pdst = f"io_wakeup_ports_{index}_bits_pdst"
            if valid in names:
                lines.append(f"  if (cycle >= 48 && cycle < 64) {valid} = 1;")
            if pdst in names:
                lines.append(f"  if (cycle >= 48 && cycle < 64) {pdst} = 7'd{index + 1};")
        for index in range(4):
            fu_types = f"io_fu_types_{index}"
            if fu_types in names:
                lines.append(f"  if (cycle >= 64 && cycle < 80) {fu_types} = '1;")
                lines.append(f"  if (cycle >= 112) {fu_types} = '1;")
        for name in sorted(names):
            if "brupdate" in name and name.endswith("mispredict"):
                lines.append(f"  if (cycle >= 80 && cycle < 96) {name} = 1;")
            if name in ("io_flush_pipeline", "io_kill"):
                lines.append(f"  if (cycle >= 96 && cycle < 112) {name} = 1;")
    elif scenario == "T1-rename-freelist":
        phases = ["idle", "allocate", "deallocate", "branch_snapshot", "mispredict_recovery", "exhaustion", "simultaneous"]
        for index in range(4):
            if f"io_reqs_{index}" in names:
                lines.append(f"  if (cycle >= 16 && cycle < 32) io_reqs_{index} = ((cycle-16) >> {index}) & 1;")
                lines.append(f"  if (cycle >= 80 && cycle < 112) io_reqs_{index} = 1;")
                lines.append(f"  if (cycle >= 112) io_reqs_{index} = 1;")
            valid = f"io_dealloc_pregs_{index}_valid"
            bits = f"io_dealloc_pregs_{index}_bits"
            if valid in names:
                lines.append(f"  if (cycle >= 32 && cycle < 48) {valid} = 1;")
                lines.append(f"  if (cycle >= 112) {valid} = 1;")
            if bits in names:
                lines.append(f"  if (cycle >= 32 && cycle < 48) {bits} = 7'd{index + 1};")
                lines.append(f"  if (cycle >= 112) {bits} = 7'd{index + 33};")
            br_valid = f"io_ren_br_tags_{index}_valid"
            br_bits = f"io_ren_br_tags_{index}_bits"
            if br_valid in names:
                lines.append(f"  if (cycle >= 48 && cycle < 64) {br_valid} = 1;")
            if br_bits in names:
                lines.append(f"  if (cycle >= 48 && cycle < 64) {br_bits} = 5'd{index};")
        if "io_brupdate_b2_mispredict" in names:
            lines.append("  if (cycle >= 64 && cycle < 80) io_brupdate_b2_mispredict = 1;")
        if "io_brupdate_b2_uop_br_tag" in names:
            lines.append("  if (cycle >= 64 && cycle < 80) io_brupdate_b2_uop_br_tag = cycle - 64;")
    elif scenario == "T2-register-read":
        phases = ["idle", "normal_issue", "zero_register", "multiple_bypass_priority", "predicate_bypass", "kill", "reset"]
        for index in range(6):
            valid = f"io_iss_valids_{index}"
            if valid in names:
                lines.append(f"  if (cycle >= 16 && cycle < 80) {valid} = 1;")
            for operand in ("prs1", "prs2", "prs3"):
                name = f"io_iss_uops_{index}_{operand}"
                if name in names:
                    lines.append(f"  if (cycle >= 16 && cycle < 32) {name} = 7'd{index + 1};")
                    lines.append(f"  if (cycle >= 32 && cycle < 48) {name} = 0;")
                    lines.append(f"  if (cycle >= 48 && cycle < 80) {name} = 7'd7;")
            pred = f"io_iss_uops_{index}_ppred"
            if pred in names:
                lines.append(f"  if (cycle >= 64 && cycle < 80) {pred} = 6'd3;")
        for index in range(6):
            valid = f"io_bypass_{index}_valid"
            pdst = f"io_bypass_{index}_bits_uop_pdst"
            data = f"io_bypass_{index}_bits_data"
            if valid in names:
                lines.append(f"  if (cycle >= 48 && cycle < 80) {valid} = 1;")
            if pdst in names:
                lines.append(f"  if (cycle >= 48 && cycle < 80) {pdst} = 7'd7;")
            if data in names:
                lines.append(f"  if (cycle >= 48 && cycle < 80) {data} = 64'h{index + 1:016x};")
        if "io_kill" in names:
            lines.append("  if (cycle >= 80 && cycle < 96) io_kill = 1;")
        lines.append("  if (cycle >= 96 && cycle < 112) reset = 1;")
    else:
        raise ValueError(f"unknown scenario: {scenario}")
    lines.append("end")
    return lines, phases


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--top", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=1_000_000)
    parser.add_argument("--seed", type=int, default=20260922)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    base_files = list(args.baseline.glob("*.sv"))
    candidate_dir = args.out / "candidate-renamed"
    candidate_dir.mkdir()
    rename_modules(args.candidate, candidate_dir, "__candidate")
    top_file = next(
        (path for path in base_files if re.search(rf"(?m)^module\s+{re.escape(args.top)}\b", path.read_text(errors="replace"))),
        None,
    )
    if not top_file:
        raise RuntimeError(f"top module {args.top} not found")
    port_list = ports(top_file, args.top)
    inputs = [entry for entry in port_list if entry[0] == "input"]
    outputs = [entry for entry in port_list if entry[0] == "output"]
    lines = ["module diff_tb;", "integer cycle;", f"integer seed = {args.seed};"]
    for _, declaration, _, name in inputs:
        lines.append(f"reg {declaration} {name};")
    for _, declaration, _, name in outputs:
        lines.extend([f"wire {declaration} base_{name};", f"wire {declaration} cand_{name};"])

    def instance(module: str, prefix: str) -> str:
        connections = [
            f".{name}({name if direction == 'input' else prefix + name})"
            for direction, _, _, name in port_list
        ]
        return f"{module} {prefix}uut(" + ",".join(connections) + ");"

    lines.extend([instance(args.top, "base_"), instance(args.top + "__candidate", "cand_")])
    lines.extend(["initial begin", "seed = $urandom(seed);", "clock = 0; reset = 1;"])
    lines.extend(f"{name} = '0;" for _, _, _, name in inputs if name not in ("clock", "reset"))
    lines.append(f"for (cycle = 0; cycle < {args.cycles}; cycle = cycle + 1) begin")
    lines.append("#5 clock = 0;")
    for _, _, bits, name in inputs:
        if name not in ("clock", "reset"):
            lines.append(f"{name} = {random_expr(bits)};")
    lines.append("if (cycle >= 8) reset = 0;")
    directed, phases = directed_lines(args.scenario, inputs)
    lines.extend(directed)
    lines.extend(["#5 clock = 1;", "#1;"])
    for _, _, _, name in outputs:
        lines.append(
            f"if (base_{name} !== cand_{name}) begin "
            f"$display(\"MISMATCH cycle=%0d port={name}\", cycle); $fatal(1); end"
        )
    lines.extend(["end", f"$display(\"PASS cycles={args.cycles}\");", "$finish;", "end", "endmodule"])
    testbench = args.out / "diff_tb.sv"
    testbench.write_text("\n".join(lines) + "\n")
    sources = [str(path) for path in base_files]
    sources.extend(str(path) for path in candidate_dir.glob("*.sv"))
    sources.append(str(testbench))
    command = [
        "verilator", "--binary", "--timing", "-Wno-fatal", "-DSYNTHESIS",
        "-j", str(max(1, args.jobs)),
        "--top-module", "diff_tb", "--Mdir", str(args.out / "obj_dir"), *sources,
    ]
    build = subprocess.run(command, text=True, capture_output=True)
    (args.out / "build.stdout").write_text(build.stdout)
    (args.out / "build.stderr").write_text(build.stderr)
    if build.returncode:
        raise SystemExit(build.returncode)
    run = subprocess.run([str(args.out / "obj_dir/Vdiff_tb")], text=True, capture_output=True)
    (args.out / "run.stdout").write_text(run.stdout)
    (args.out / "run.stderr").write_text(run.stderr)
    result = {
        "cycles": args.cycles, "seed": args.seed, "scenario": args.scenario,
        "directed_phases": phases, "returncode": run.returncode,
        "passed": run.returncode == 0 and f"PASS cycles={args.cycles}" in run.stdout,
    }
    (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
