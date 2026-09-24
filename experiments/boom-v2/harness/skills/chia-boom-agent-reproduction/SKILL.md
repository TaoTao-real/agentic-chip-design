---
name: chia-boom-agent-reproduction
description: Reproduce, resume, audit, or finalize CHIA-native Agent optimization of BOOM Chisel modules with Ray, differential verification, Vivado PPA, Verilator regression, and controlled Design Episode access.
---

# CHIA BOOM Agent Reproduction

Use the public harness in this package while preserving its causal and evidence
boundaries.

## Before running

1. Read the frozen experiment config; do not reconstruct commits or paths from memory.
2. Read [references/environment.md](references/environment.md) when installing or qualifying a host.
3. Read [references/protocol.md](references/protocol.md) before blind discovery or a memory ablation.
4. Run `CONFIG=... scripts/verify_environment.sh prequal` before Q0 and
   `CONFIG=... scripts/verify_environment.sh postqual` afterward. Use the
   `api` phase only when the user has supplied `DEEPSEEK_API_KEY`. Treat source,
   tool, qualification, credential or frozen-input drift as a new experiment
   version.
5. Before spending model tokens, run the public `chia-boom smoke` frozen
   baseline-vs-baseline gate and require `passed=true`, `interface_ok=true` and
   `model_calls=0`.
5. Inject the model key through `DEEPSEEK_API_KEY`; never save its value in config, prompts, commands, logs or artifacts.

## Choose the operation

- `qualify`: prove baseline repeatability and decide whether one or two physical slots are safe.
- `memory-mode none`: blind discovery with no historical knowledge.
- `memory-mode generic`: blind discovery with cross-target process and validation lessons only.
- `memory-mode target`: explicitly mounted current-target memory; label the result engineering reuse.
- `finalize`: rebuild the saved best candidate, use new differential seeds, run post-route PPA and MegaBOOM `rsort.riscv`.
- `resume`: continue the saved lineage without changing prompt, model, config or budget.

## Preserve the gates

For each candidate enforce one mutable file, elaborate the frozen BOOM config,
run directed plus one-million-cycle differential verification, then run fixed
post-synthesis timing and area. Promote only a correct candidate within the
105% LUT bound that improves its parent.

Keep candidate validity, promotability, final validity and valid improvement as
different states. Retry infrastructure failures in place without consuming a
new model call. Candidate failures retain their raw stage, error, source, diff
and parent as feedback. Formal autonomous campaigns do not permit manual repair.

Require an exact top-level port name/direction/width signature before cycle
differential testing. Bind reusable qualification and finalization evidence to
the current config, tool versions, source, golden RTL, verification tools and
candidate source hash. Never accept a retryable infrastructure result as a
completed finalization cache entry.

Report provider usage, model calls, EDA count, active tool time, wall time,
correctness, post-synthesis and post-route PPA, area and processor regression.
State that results are FPGA out-of-context module evidence, not ASIC PPA,
whole-core Fmax, IPC or board-level proof.
