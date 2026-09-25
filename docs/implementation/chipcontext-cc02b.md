# ChipContext CC-02b offline query contract

Status: implemented from `main@885e6b96b6c4d6f51f6c068a4b5132b3728afc7e`.
This interface reads sealed evidence. It does not run an Agent, Ray, a model,
EDA or a simulator.

## Trusted registry

The operator supplies `chipcontext.store-registry.v1`:

```json
{
  "schema_version": "chipcontext.store-registry.v1",
  "stores": [
    {
      "store_id": "primary",
      "root": "../evidence-store",
      "allowed_access": ["public", "controlled"]
    }
  ]
}
```

Roots are relative to the registry file. Store IDs are unique logical names.
The registry is trusted configuration; a query cannot add stores, paths or
access levels. The CLI grants only `public` unless the operator also passes
`--allow-controlled`. That flag cannot exceed `allowed_access`.

Every referenced store must already exist and is opened read-only. The query
path cannot create records, aliases, events or caches. Store, ROOTS, record and
artifact metadata symlinks fail closed.

## Request and scope

Every request is `chipcontext.query-request.v1`:

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

The parser rejects unknown fields at every supported level. A scope can bind
the full CandidateRef, attempt and trusted working-source hash. Omitted working
source means `selected_evaluation`; an equal hash means `current`; a different
hash means `historical`. Historical evidence remains visible but is not
represented as a measurement of the current edit.

`compare_metrics` accepts only `bound_baseline` or another complete explicit
QueryScope as its reference. No operation accepts `latest`, `best`, arbitrary
paths, regular expressions or shell commands.

## Operations

| Operation | Parameters | Result boundary |
|---|---|---|
| `candidate_status` | none | Manifest completion/binding and every CheckRecord; a passing check is not final chip acceptance. |
| `candidate_artifacts` | optional exact stage/kinds, limit and cursor | Logical kind, conservative stage, media type, size and content ref; no host path. |
| `failure` | check and optional explicit extraction | Recorded verdict kept separate from mismatch observation, cycles, signal, expected/actual and source span. |
| `compare_metrics` | explicit reference, stage and metric IDs | Per-metric comparable/missing/conflict/not-comparable; delta is current minus reference. |
| `timing_paths` | explicit extraction, stage, exact filters, limit/cursor | Producer rank and coverage; report-first, filtered minimum and unsupported global worst remain distinct. |
| `read_artifact` | content ref and bounded page parameters | Verified UTF-8 content, exact byte/line span, truncation and bound cursor. |

Every operation resolves snapshot → manifest → candidate/contract/attempt →
extraction/artifact and authorizes the complete evidence dependency closure
before returning facts. Missing, conflict, partial and inconclusive states are
part of successful answers, rather than transport failures.

## CLI and response

```bash
chia-chipcontext query \
  --registry trusted-stores.json \
  --request requests/status.json \
  --format json \
  --max-output-bytes 16384 \
  --audit-output audits/status-attempt.json
```

JSON returns `chipcontext.query-response.v1` with two members:

- `answer`: deterministic `chipcontext.query-answer.v1`; content hash excludes
  timing and counters.
- `cost`: dynamic `chipcontext.query-cost.v1`; includes wall time, configuration,
  store/record/artifact metadata reads, full artifact scan bytes, hash bytes,
  parse bytes and returned bytes. It also contains disjoint request-validation,
  store-resolution, evidence-query and first-render phase times. The response
  wall time ends after the first complete render, before fixed-point byte
  accounting and stdout writing. Cache status is `not_configured`; physical I/O
  and peak memory are null because they are not measured.

`--audit-output` is a trusted operator-side sink, not a request field and not an
EvidenceStore mutation. It must name a new file in an existing directory. Every
completed attempt writes `chipcontext.query-attempt.v1`, including `success` or
`rejected`, a nullable answer ref, a path-free error code, attempted/returned
bytes and all meter values accumulated before acceptance or rejection. Attempt
phase times are disjoint: `response_serialization_ns` contains render and byte
budget work and replaces the nested first-render value. In-process callers can
collect the same record from `run_query_attempt`; rejected calls raise
`QueryAttemptFailure` with the record attached. Without a trusted audit sink,
public stderr remains deliberately limited to the sanitized error.

Markdown is rendered from the same answer. It retains identity, stage,
applicability, units, result, conditions, coverage, missing, conflicts, source
references and pagination. Raw tool text is indented and cannot create Markdown
structure.

The final encoded JSON or Markdown has a 16 KiB default and 64 KiB hard limit.
`return_bytes` includes the response, cost, pagination and final newline. An
overflow returns `budget_exceeded`; the renderer never removes conflicts or
sources to fit.

Business results such as `partial`, `inconclusive` and `not_comparable` exit 0.
Invalid requests, permission denial, unknown content, integrity failure and
budget overflow emit path-free `chipcontext.error.v1` on stderr and exit 2.
Malformed UTF-8, non-object ROOTS/records, invalid ROOTS field types and corrupt
artifact metadata are integrity failures; decoder exceptions, tracebacks and
machine paths are never emitted by the query command.
The legacy `prepare` and unscoped `read-artifact` commands preserve their prior
stdout and error behavior.

## Scoped line-range continuation

Scoped reads use revision `chipcontext-query-foundation-v3`. A line-range page
always supplies the same `start_line`, `line_count` and `limit_bytes`, including
continuations. The cursor binds those values, the complete scope, artifact,
input hash and query revision. It cannot cross the selected range or a UTF-8
code-point boundary. Every page repeats authorization and full content-hash
verification.

Only scoped source-read hashes changed for this revision. B1 status/artifact,
B2 domain and CC-01 legacy fixture hashes remain fixed.

## Reproduction and calibration

Build the public-safe six-operation suite and run a query:

```bash
cd experiments/boom-v2/harness
OUT=$(mktemp -d /tmp/chipcontext-query.XXXXXX)
PYTHONPATH=. python examples/chipcontext_query_suite.py --output "$OUT"
PYTHONPATH=. python -m chia_boom.chipcontext.cli query \
  --registry "$OUT/trusted-stores.json" \
  --request "$OUT/requests/candidate_status.json" \
  --format json
```

The benchmark script runs all six operations ten times in process and five
times as independent CLI processes. The committed small and 1 MiB results are
in [`chipcontext-cc02b-query-cost.json`](chipcontext-cc02b-query-cost.json).
Logical scan/hash/parse counters are not physical disk-I/O measurements and no
performance threshold is used.

The controlled replay used sealed real evidence and verified a failure span,
two comparable PPA metrics and rank/top-k timing-path provenance. It passed 205
harness tests and the low-resource qualification doctor; the prior smoke hash
was checked without rerunning it. Public-safe evidence is in
[`chipcontext-cc02b-controlled-calibration.json`](chipcontext-cc02b-controlled-calibration.json).
Model, new EDA and new simulator calls were all zero.

## Remaining boundary

CC-02b does not provide arbitrary report search, a database, a persistent query
cache, Agent/runtime delivery or an optimization-effect experiment. CC-02c must
freeze the independent engineering question set and perform wider system cost
acceptance before CC-03 connects context selection to an Agent.
