# ChipContext CC-02c black-box acceptance

Issue #15 advances ChipContext from an implemented query interface to an
independently checkable engineering evidence service.  The acceptance runner is
model-free and does not invoke Vivado, Verilator, a simulator, Ray, or CHIA.

## C1 boundary

The first slice freezes six public-safe cases:

1. comparable delay and LUT measurements;
2. missing post-route evidence;
3. conflicting raw and legacy evidence;
4. controlled evidence denied to a public caller;
5. corrupt store metadata rejected without fallback;
6. output-budget rejection after evidence scanning.

The committed corpus declares 32 planned cases but implements only these six.
The remaining status, failure, timing-path, pagination, permission, integrity,
and budget cases belong to PR-C2.

Expected values were manually transcribed from the cited raw fixture hashes and
spans.  The verifier does not import a production extractor or comparison
recipe.  `ORACLE_REVIEW.json` remains `pending` until a PR maintainer compares
the expected facts with those raw sources.  A development run may execute while
review is pending, but it reports `formal_eligible=false`; `--require-approved-oracle`
fails closed.

## Reproduce the public vertical slice

From `experiments/boom-v2/harness`:

```bash
SUITE=$(mktemp -d)/suite
RUN=$(mktemp -d)/acceptance-run

PYTHONPATH=. python3 examples/chipcontext_query_acceptance_fixture.py \
  --output "$SUITE"

PYTHONPATH=. python3 examples/chipcontext_query_acceptance.py \
  --corpus "$SUITE/corpus.json" \
  --registry "$SUITE/trusted-stores.json" \
  --mode both \
  --output "$RUN"
```

The builder creates only synthetic stores.  It copies the independently cited
raw bytes into `oracle-sources`, creates an isolated controlled envelope, and
creates a separately damaged store for the integrity case.  It verifies every
generated request against the frozen request hash before publishing the suite.

The runner writes a new output directory containing:

- `run-manifest.json` with corpus, review, registry and environment identity;
- one immutable acceptance record and one trusted CLI audit per attempt;
- `events.jsonl` and one result per case;
- a machine-readable summary and a Markdown report.

API and CLI executions are checked separately against the literal oracle.
Expected query rejection is distinct from case failure.  A missing audit,
timeout, malformed output, or runner failure is recorded as blocked rather than
zero-cost evidence or a successful rejection.

## What C1 establishes

A successful development run establishes that the six-case harness can retain
normal, unknown, and rejected outcomes with raw source identity and trusted
attempt cost.  It does not establish formal CC-02c acceptance until the oracle
review is approved, does not cover the remaining 26 cases, and does not show
Agent or hardware optimization benefit.
