# ChipContext CC-02 implementation

Issue [#9](https://github.com/TaoTao-real/agentic-chip-design/issues/9)
extends the CC-01 evidence identity layer in three reviewed batches. This page
tracks the implemented boundary. It is not a claim that an Agent or search
runtime is using these facts.

## CC-02a: raw report extraction

CC-02a adds two standard-library extractors:

- `vivado-2024.1-v2` reads the timing summary, utilization report and collected
  timing-path report. It records each supported value with an artifact hash and
  exact source span. Timing-path coverage records the producer command form,
  requested `max_paths`, filters, stage, returned count and report order. A path
  absent from the collected top-k is unknown, rather than proved absent. A
  partially parsed rank-one path remains rank one and is marked `partial`; a
  supplied but unparseable report is `parse_failed`, distinct from an absent
  report (`not_collected`).
- `verilator-differential-v2` reads the differential result JSON and optional
  stdout. It separates interface checking, simulator execution and functional
  correctness. A process crash without a grounded mismatch is `inconclusive`,
  while an interface mismatch records the interface failure and leaves the
  differential behavior check `not_run`. The JSON `cycles` value is a
  configured upper bound; `completed_cycles` is published only when a matching
  stdout `PASS cycles=N` marker proves completion.

The machine-readable field contract is in
[`chipcontext-cc02-field-sources.json`](chipcontext-cc02-field-sources.json).
The existing adapter remains `legacy-evaluation-v3`.

Extraction uses `EvidenceStore.verified_artifact_bytes()`: the digest is
computed over the bytes passed to the parser, and a file mutation during or
after registration is rejected. Results are immutable
`chipcontext.extraction.v1` sidecars. Existing requests without the new raw
artifact roles keep the CC-01 record shape and hashes.

When both raw and legacy values exist, matching definition, unit, stage and
value merge their source references. A difference is an explicit conflict, and
that metric cannot produce a delta. Extractor-internal ambiguity carries an
explicit affected-metric set, so a rejected duplicate cannot be restored from
the legacy record. `nodes.parse_vivado_ppa()` delegates to the same byte parser
while preserving the frozen parser's fields on valid, unambiguous reports.
Contradictory duplicate Vivado rows are an intentional compatibility narrowing:
the old parser selected the first row, while the shared parser now fails closed
because the physical score is ambiguous.

## Registered raw artifact roles

For the current evaluated stage, `prepare` recognizes:

- `post_synth_timing_summary` or `post_route_timing_summary`;
- `post_synth_utilization` or `post_route_utilization`;
- optional `post_synth_timing_paths` or `post_route_timing_paths`;
- `differential_result` and optional `differential_stdout`.

The same labels may be request artifact names or `kind` values. Raw Vivado
extraction requires both summary and utilization. Access is inherited from the
registered artifact; extraction does not make controlled bytes public.

## Public-safe offline check

The `chipcontext/fixtures/extraction` fixture is synthetic and contains no BOOM
solution. Run:

```bash
cd experiments/boom-v2/harness
PYTHONPATH=. python -m unittest \
  chia_boom.tests.test_chipcontext_extractors \
  chia_boom.tests.test_chipcontext
```

The vertical test registers the synthetic report, creates an extraction
sidecar, asks for the worst collected timing path and reads its original line
span through the bounded artifact API. Other tests cover missing and truncated
fields, raw/legacy conflicts, immutable records and changed source files.

## Controlled calibration

The qualified server replay used one sealed successful Vivado attempt and one
sealed differential failure. It made zero model calls and zero new EDA calls.
The Vivado extraction returned eight PPA facts and all 20 collected paths; the
worst-path answer drilled down to the registered report span. The failure
extraction grounded its first mismatch while preserving expected and actual as
missing. The compatibility check produced an exact dictionary match between
the evaluator entry point and the new parsing core.

The public-safe hashes, counts, logical bytes and measured preparation/query
times are recorded in
[`chipcontext-cc02a-controlled-calibration.json`](chipcontext-cc02a-controlled-calibration.json).
The v2 server replay passed all 128 harness tests and the frozen low-resource
qualification doctor. It reused the prior 10,000-cycle smoke by verified hash;
no simulator, EDA, or model run was added. Revision, hashes, counts and timings
are recorded in the adjacent calibration JSON.
Environment verification and `doctor --require-qualification` passed against
the frozen single-slot qualification profile. The prior 10,000-cycle
baseline-vs-baseline smoke was hash-checked and remained passing; it was not
rerun because CC-02a has a zero-new-EDA budget.

## Current boundary

CC-02a does not add the public query CLI, arbitrary report paths, regex queries,
Agent integration, Ray, model SDKs or EDA execution. The complete scoped query
service belongs to CC-02b; the frozen question set and cost protocol belong to
CC-02c.
