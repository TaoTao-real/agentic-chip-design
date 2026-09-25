# ChipContext CC-02 implementation

Issue [#9](https://github.com/TaoTao-real/agentic-chip-design/issues/9)
extends the CC-01 evidence identity layer in three reviewed batches. This page
tracks the implemented boundary. It is not a claim that an Agent or search
runtime is using these facts.

## CC-02a: raw report extraction

CC-02a adds two standard-library extractors:

- `vivado-2024.1-v3` reads the timing summary, utilization report and collected
  timing-path report. It records each supported value with an artifact hash and
  exact source span. Timing-path coverage records the producer command form,
  requested `max_paths`, filters, stage, returned count and report order. A path
  absent from the collected top-k is unknown, rather than proved absent. A
  partially parsed rank-one path remains rank one and is marked `partial`; a
  supplied but unparseable report is `parse_failed`, distinct from an absent
  report (`not_collected`). Path-block boundaries are identified before Slack
  parsing, so an invalid first Slack value cannot renumber a later path as the
  collected worst path.
- `verilator-differential-v3` reads the differential result JSON and optional
  stdout. It separates interface checking, simulator execution and functional
  correctness. A process crash without a grounded mismatch is `inconclusive`,
  while an interface mismatch records the interface failure and leaves the
  differential behavior check `not_run`. The JSON `cycles` value is a
  configured upper bound; `completed_cycles` is published only when a matching
  stdout `PASS cycles=N` marker proves completion. Early-exit, run fields,
  PASS/mismatch markers and supported explicit failures are reconciled before
  checks are published; contradictory evidence remains `inconclusive` with a
  conflict record.

The machine-readable field contract is in
[`chipcontext-cc02-field-sources.json`](chipcontext-cc02-field-sources.json).
The legacy adapter is `legacy-evaluation-v4`; the revision changed because an
interface-failure claim that coexists with run evidence now remains an explicit
conflict instead of being normalized to `not_run`.

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
The v3 server replay passed all 134 harness tests and the frozen low-resource
qualification doctor. It reused the prior 10,000-cycle smoke by verified hash;
no simulator, EDA, or model run was added. Revision, hashes, counts and timings
are recorded in the adjacent calibration JSON.
Environment verification and `doctor --require-qualification` passed against
the frozen single-slot qualification profile. The prior 10,000-cycle
baseline-vs-baseline smoke was hash-checked and remained passing; it was not
rerun because CC-02a has a zero-new-EDA budget.

## CC-02b PR-B1: scoped query foundation

PR-B1 adds a standard-library, read-only query foundation over sealed stores:

- `EvidenceHandle` names one trusted store and one content-addressed snapshot;
- `QueryScope` can require the complete candidate identity, attempt and trusted
  working-source hash;
- `EvidenceResolver` verifies snapshot, manifest, candidate, contract, attempt,
  extraction and artifact ownership without consulting top-level aliases;
- `ChipContextQueryService` answers candidate status, lists registered
  artifacts and performs bounded source reads;
- `chipcontext.query-answer.v1` keeps deterministic facts separate from future
  runtime cost records.

Working-source applicability is `selected_evaluation` when no working hash was
provided, `current` when it equals the evaluated source, and `historical` when
it differs. Historical evidence remains queryable but is not represented as a
measurement of the current edit. Status reports preserve every `CheckRecord`
and explicitly state that passing recorded checks is not final chip acceptance.

The trusted in-process registry follows `chipcontext.store-registry.v1`. Store
paths and access levels do not come from a query. Query revision
`chipcontext-query-foundation-v2` treats both artifacts and versioned
extractions as evidence sources. An extraction source is accepted only after
its owner, attempt and input bindings are verified, then authorization expands
to every underlying artifact. Every operation authorizes the common candidate
envelope before filtering, so an empty result cannot disclose controlled
candidate metadata. Every page rechecks source hashes and permissions.
Answers expose content references, logical kinds, conservative stages, sizes
and media types; they do not expose host paths, root IDs or registered relative
locations.

Scoped source reads use strict UTF-8 pagination. Page ends move back to a code
point boundary, so concatenating pages reproduces valid input exactly. Invalid
UTF-8 returns `invalid_text_encoding`; a limit too small for the next code point
returns `text_page_too_small`. The pre-existing `EvidenceStore.read_artifact`
API is unchanged for CC-01 compatibility.

Artifact stages use `artifact-stage-map-v1`. The evaluation record uses the
manifest's normalized stage. Only explicit elaboration, differential,
post-synth, post-route and regression kind prefixes get a fixed mapping; other
artifacts remain `unknown`. The implementation does not infer a stage from a
filename.

Run the synthetic public vertical example from the harness directory:

```bash
QUERY_OUTPUT=$(mktemp -d /tmp/chipcontext-query-foundation.XXXXXX)
PYTHONPATH=. python examples/chipcontext_query_foundation.py \
  --request chia_boom/chipcontext/fixtures/success/request.json \
  --output "$QUERY_OUTPUT"
```

The script performs `snapshot handle → candidate status → artifact list →
bounded source read`. Its inputs contain no BOOM solution. The same chain and
negative identity, extraction-source, envelope authorization, lossless UTF-8,
cursor and tamper cases are executable in
`chia_boom.tests.test_chipcontext_queries`; its fixed answer hashes are stored
in `chipcontext/fixtures/query-foundation/expected.json`.

The frozen Linux Python 3.12 CI passed all 175 harness tests after the review
fixes, plus compileall, both CLI help checks and shell syntax checks. The public example's
complete JSON SHA-256 was
`04cf47fd806c4f19f978b5e03115754b13fd67fa608e14357736a5f6d17fc502` after
the v2 query-contract fixes.
The run explicitly removed `DEEPSEEK_API_KEY` and made zero model, EDA and
simulator calls.

## Current boundary

PR-B1 does not add the public `query` CLI, JSON/Markdown query renderer,
failure/metric/timing-path domain queries, arbitrary report paths, regex
queries, Agent integration, Ray, model SDKs or EDA execution. Those interfaces
remain in Issue #11 PR-B2/PR-B3. The frozen question set and system-level cost
protocol belong to CC-02c.
