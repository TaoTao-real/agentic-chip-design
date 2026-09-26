# ChipContext CC-03 runtime feedback prototype

Issue #17 connects the existing BOOM interactive optimization loop to the
deterministic ChipContext evidence and query layer. This exploratory runtime
bridge does not change the BOOM correctness, PPA, promotion, or finalization
gates.

## Runtime chain

After every completed candidate evaluation the harness:

1. saves the existing candidate and evaluation result;
2. binds a unique candidate, source hash and evaluation attempt;
3. prepares a content-addressed snapshot from the frozen baseline,
   qualification, evaluation and existing raw tool artifacts;
4. lists the current candidate's bounded raw artifacts for both E0 and E1;
5. for E1, queries and pushes a compact deterministic summary;
6. records prepare and query work separately from model and EDA work.

The campaign retains the complete tool transcript. Before each subsequent model
call, older tool payloads are represented by their tool name, content hash and
length while the four most recent tool results remain inline. The Agent can
reissue any bounded query. This prevents repeated raw RTL bytes from dominating
provider input without deleting the audit evidence, and is identical in E0 and
E1.

The same arm-independent action budget warns after 12 consecutive
inspection-only turns and disables further read/query calls after 16 until the
Agent edits or evaluates. It constrains tool use only; it contains no target
bottleneck or transformation hint. This rule was added after the first retained
pilot exhausted 24 turns on raw reads, performed zero evaluations, and consumed
723,549 provider-reported tokens.

The E1 push contains candidate/attempt identity, current or historical
applicability, check outcomes, a grounded failure when one exists, comparable
post-synthesis delay and LUT deltas, collected timing-path scope, explicit
missing/conflict entries, and content references for bounded raw reads. Dynamic
query timing is excluded from the feedback content hash and Agent prompt, but
retained in the campaign audit.

## E0/E1 fairness

Both arms use the same model, memory mode, target, frozen source, raw report
inventory, bounded `read_candidate_artifact` tool, evaluation budget and final
gates. E1 adds deterministic organization of those bytes and five structured
query tools. It does not receive an extra report, target solution, human
bottleneck diagnosis, parser, EDA run, or judge model.

```text
E0 extra runtime tools:
  read_candidate_artifact

E1 extra runtime tools:
  read_candidate_artifact
  query_candidate_status
  list_candidate_artifacts
  query_candidate_failure
  compare_candidate_metrics
  query_candidate_timing_paths
```

## Controlled execution

After normal environment qualification, run matched campaigns in separate
output directories. A run must use the frozen private configuration; the
portable example does not contain valid server paths or credentials.

```bash
python -m chia_boom.cli interactive \
  --config <frozen-private-config> \
  --output <campaign-root>/e0-seed41 \
  --seed 41 --max-turns 48 --max-evaluations 5 \
  --memory-mode none --feedback-arm E0

python -m chia_boom.cli interactive \
  --config <frozen-private-config> \
  --output <campaign-root>/e1-seed41 \
  --seed 41 --max-turns 48 --max-evaluations 5 \
  --memory-mode none --feedback-arm E1
```

If a server-side process stops after a saved turn, continue it without changing
the experiment contract:

```bash
python -m chia_boom.cli interactive-resume \
  --config <same-private-config> \
  --output <same-campaign-output>
```

The resume command fails closed if the seed, arm, search limits, memory mode,
auto-stop threshold, compiler/simulator/EDA versions, frozen source, or stored
ChipContext identity changed. It reuses existing candidate snapshots read-only
and does not repeat their prepare step.

Final candidates still use the existing independent finalization command. A
post-synthesis search improvement is not a final result until that gate passes.

## Evidence and limits

Public tests use neutral synthetic reports to establish deterministic prepare,
current/historical behavior, candidate/attempt isolation, idempotent restore,
bounded raw drilldown, a compact stable E1 feedback hash, and matched E0/E1 raw
artifact capabilities. CHIA/Ray, Verilator and Vivado execution remains a
controlled-server test.

The first data report must retain every failed run and separately report time to
first verified improvement, best final QoR, provider usage, model calls, EDA
evaluations, invalid candidates, and ChipContext overhead. One matched pair is
an end-to-end smoke, not evidence of stable benefit; the exploratory target is
three pairs per arm.

The first completed pair and its machine-readable measurements are recorded in
[`chipcontext-cc03-e0-e1-seed41.md`](chipcontext-cc03-e0-e1-seed41.md) and
[`chipcontext-cc03-e0-e1-seed41.json`](chipcontext-cc03-e0-e1-seed41.json).
