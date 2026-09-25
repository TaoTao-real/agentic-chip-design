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

## CC-02b PR-B2: evidence-grounded domain queries

PR-B2 extends the same explicit `QueryScope` with three read-only domain
queries.  These operations use `chipcontext-query-domain-v2`; the B1 status,
artifact and source-read answers retain `chipcontext-query-foundation-v2`, so
the already reviewed answer hashes do not change.

- `failure(scope, check, extraction_ref?)` keeps the recorded check verdict
  separate from an extracted mismatch observation.  It reports configured and
  completed cycles separately, retains missing expected/actual values and
  returns the exact registered source span.  More than one differential
  extraction requires an explicit reference.
- `compare_metrics(scope, reference, stage, metric_ids)` accepts only the
  manifest-bound baseline or another explicit `QueryScope`.  Each metric is
  independently `comparable`, `missing`, `conflict`, or `not_comparable`;
  deltas are always `current - reference`.  Stage, device, clock, tool,
  reference fingerprint, definition and unit must all be present and equal.
  `all_comparable` and `any_comparable` are reported separately.
  Requested conflicts retain the original conflict record, source references,
  evidence side, store and snapshot; unrelated metric conflicts are omitted.
- `timing_paths(scope, extraction_ref, stage, ...)` provides exact field
  filters only.  It preserves producer ranks and distinguishes report rank 1,
  the minimum slack among the filtered collected rows and the unsupported
  claim of a global worst path.  Producer command, requested top-k,
  reported/parsed counts and parse status remain visible.  A minimum drawn
  from only parsed definite matches is `partial` when an unparseable or
  possible match could change it.  Missing reports, unparseable reports,
  possible matches and confirmed no-match results remain distinct.

The comparison qualification is implemented once in `recipes.py`.  The
legacy prepare path calls the same pure recipe and retains its fixed public
JSON, Markdown and content hashes.  Domain queries expand and authorize their
raw dependencies before returning facts; they neither rescan arbitrary paths
nor launch a parser, simulator or EDA tool outside the sealed extraction.

### Python examples

```python
failure = service.failure(
    scope,
    check="differential_correctness",
    extraction_ref=differential_extraction_ref,
)

metrics = service.compare_metrics(
    scope,
    reference="bound_baseline",
    stage="post_synth",
    metric_ids=["critical_delay_ns", "slice_luts"],
)

paths = service.timing_paths(
    scope,
    extraction_ref=vivado_extraction_ref,
    stage="post_synth",
    path_group="clock",       # exact match
    limit=10,
)
```

The snapshot reference form of `reference` is another complete `QueryScope`;
there is no parent/latest/best shorthand.  A historical scope is still
queryable, but its applicability remains `historical` and does not claim that
the current working edit was measured.

## CC-02b PR-B3/B4: query CLI, output contract and calibration

The public entry point is now:

```text
chia-chipcontext query \
  --registry <trusted-stores.json> \
  --request <query.json> \
  --format json|markdown \
  --max-output-bytes <n> \
  [--allow-controlled]
```

Minimal trusted registry and request shapes are:

```json
{
  "schema_version": "chipcontext.store-registry.v1",
  "stores": [
    {
      "store_id": "primary",
      "root": "../evidence-store",
      "allowed_access": ["public"]
    }
  ]
}
```

```json
{
  "schema_version": "chipcontext.query-request.v1",
  "operation": "candidate_status",
  "scope": {
    "handle": {
      "store_id": "primary",
      "snapshot_ref": "<64-lowercase-hex>"
    },
    "expected_candidate": null,
    "expected_attempt_id": "evaluation-attempt-01",
    "working_source_sha256": null
  },
  "parameters": {}
}
```

The trusted registry is `chipcontext.store-registry.v1`. Every entry contains a
unique logical store ID, a root relative to the registry file and the access
levels the operator permits. The request is `chipcontext.query-request.v1` and
contains only an operation, explicit QueryScope and that operation's parameter
allowlist. Unknown fields at every parsed level are rejected. Requests cannot
provide paths, permissions, regular expressions, shell fragments or implicit
`latest`/`best` selection.

The six operations are `candidate_status`, `candidate_artifacts`, `failure`,
`compare_metrics`, `timing_paths` and `read_artifact`. Stores are opened
read-only. Query execution cannot create a store, alias, event or persistent
cache. Public access is the CLI default; `--allow-controlled` only enables the
intersection with the registry's trusted policy.

A successful JSON response is `chipcontext.query-response.v1`, containing the
unchanged deterministic `chipcontext.query-answer.v1` and a separate dynamic
`chipcontext.query-cost.v1`. The cost reports wall time, configuration/store/
record/artifact metadata reads, complete artifact scan bytes, hash bytes, JSON
parse bytes and returned bytes. `physical_io_bytes` and `peak_memory_bytes`
remain null because this implementation does not measure them, and
`cache_status` is `not_configured`. Markdown renders the same QueryAnswer and
retains the full result, applicability, conditions, coverage, missing,
conflicts, source references and pagination. Raw text is indented so tool output
cannot alter the Markdown structure.

The final encoded output has a 16 KiB default and 64 KiB hard maximum. The
response includes its own `return_bytes`; serialization iterates until that
value equals the final UTF-8 length. Overflow fails with `budget_exceeded`; no
identity, conflict, coverage or source facts are removed. Domain states such as
`partial`, `inconclusive` and `not_comparable` are successful query results and
return status 0. Invalid input, permission denial, unknown content, integrity
failure and budget overflow return a path-free `chipcontext.error.v1` and
status 2.

Scoped source reads use `chipcontext-query-foundation-v3`. The first page and
every continuation repeat the same `start_line`, `line_count` and byte limit.
The cursor binds those values plus scope, artifact and query revision. It cannot
cross the selected line range or split a UTF-8 code point, and every page
reauthorizes the complete evidence dependency closure and rechecks the artifact
hash. B1 status/artifact hashes, B2 domain hashes and CC-01 legacy fixture hashes
remain unchanged; only the scoped source-read revision changed.

### Public cost evidence

`examples/chipcontext_query_suite.py` constructs public-safe stores and requests
for all six operations. `examples/chipcontext_query_benchmark.py` measures each
operation ten times in process and five times as an independent CLI process,
using inclusive quartiles and a monotonic clock. The committed report covers the
small fixture and a deterministic 1 MiB diagnostic report. It sets no timing
threshold and labels scan/hash/parse counters as logical work rather than
physical disk I/O:

[`chipcontext-cc02b-query-cost.json`](chipcontext-cc02b-query-cost.json).

### Controlled calibration

The qualified server read only previously sealed evidence and created new
content-addressed query records. It made zero model, EDA and simulator calls.
The failure chain preserved a grounded first mismatch and drilled down to its
registered line span while keeping expected/actual absent. The performance
chain produced comparable `critical_delay_ns` and `slice_luts` deltas with both
sides' sources. The timing chain retained producer rank 1, all 20 collected
paths, top-k coverage and the original report span. JSON and Markdown were
generated through the public CLI from the same answers. All 205 harness tests
passed, low-resource qualification doctor passed, and the existing 10,000-cycle
smoke matched frozen hash `6821977a…` without rerunning it.

The public-safe hashes, field counts, missing/conflict counts, logical reads and
query timings are in
[`chipcontext-cc02b-controlled-calibration.json`](chipcontext-cc02b-controlled-calibration.json).
No source, RTL, full log, host path, address or credential is included.
The operator-facing interface reference is
[`chipcontext-cc02b.md`](chipcontext-cc02b.md).

## Current boundary

CC-02b does not support arbitrary report paths, regular-expression queries,
Agent integration, Ray, model SDKs, persistent query caches or EDA execution.
The frozen question set and wider system cost protocol belong to CC-02c. No
optimization-effect claim is made from these query calibration results.
