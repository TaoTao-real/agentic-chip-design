# CHIA-native BOOM optimization harness

This directory publishes the executable, public-safe part of the BOOM v2
experiment. It runs a DeepSeek design agent through CHIA/Ray, evaluates one
allowed BOOM Chisel file, and preserves candidate lineage and gate evidence.

It intentionally excludes credentials, server addresses, full model sessions,
raw EDA output, generated RTL, candidate patches, Q1 replay fixtures and the
known IssueQueue solution. Those artifacts remain in the controlled evidence
store and must not be copied into a blind Agent workspace.

## Relation to the target architecture

| Target component | Implementation in this harness | Status |
|---|---|---|
| Design Core | frozen BOOM Chisel source plus generated RTL | implemented legacy adapter |
| ChipContext | raw source/timing/RTL inspection tools and compact feedback | partial; no DesignIndex/EvidenceSnapshot/ContextPacket yet |
| Loop Engine | `campaign.py`, `interactive.py`, `qualification.py`, `finalize.py` | implemented through CHIA 1.0.1 and Ray |
| EvidenceStore | immutable campaign directories, hashes and JSON artifacts | implemented as files |
| KnowledgeStore | `knowledge.py` and generic Design Episodes | implemented with access modes |
| Design Agent runtime | official DeepSeek API adapter | implemented; DSH adapter is not included |

This is therefore the current **Legacy Chisel/RTL execution path**, not the
finished PyCircuit or DSH platform described in the repository architecture.

## Experiment arms

| Arm | Initial information | Iteration behavior |
|---|---|---|
| A | source and immutable constraints | five independent baseline children |
| B | A plus unannotated baseline Vivado report | five independent baseline children |
| C | B plus exact earlier diff and raw evaluation | best-parent feedback loop |
| D | C plus allowed cross-target process memory | best-parent loop with memory |

No arm receives a human bottleneck diagnosis or a suggested circuit
transformation. Target-specific memory is supported only when a private
directory is explicitly mounted; none is shipped in this repository.

## Evidence gates

Search candidates pass, in order:

1. one-file scope and exact-edit materialization;
2. Chisel elaboration;
3. directed and 1,000,000-cycle differential verification;
4. Vivado post-synthesis timing and area;
5. promotion only when the valid candidate improves the current parent.

The best valid candidate from each run is rebuilt from a clean workspace and
must pass three new differential seeds, post-route timing/area and CHIA's full
MegaBOOM Verilator `rsort.riscv` regression. Infrastructure failures are
retryable without spending another model call; candidate failures become raw
feedback for C/D.

## Commands

The entry point exposes the frozen v13 workflow:

```text
chia-boom qualify
chia-boom preflight
chia-boom run
chia-boom resume
chia-boom finalize
chia-boom report
chia-boom interactive
chia-boom interactive-finalize
```

Use [`DEPLOYMENT.md`](DEPLOYMENT.md) for the complete installation and replay
procedure. The portable config is
[`chia_boom/config/issueq-blind.example.json`](chia_boom/config/issueq-blind.example.json).
The reusable Codex workflow is published at
[`skills/chia-boom-agent-reproduction/`](skills/chia-boom-agent-reproduction/SKILL.md).

## What the published tests establish

The unit suite checks information isolation, exact edits, candidate lineage,
failure classification, validity transitions, CHIA decoration, knowledge
access and the B/C report gates. It does not run Chipyard, Verilator or Vivado.
Physical results in the parent experiment README remain reported evidence from
the controlled environment, not a CI reproduction.
