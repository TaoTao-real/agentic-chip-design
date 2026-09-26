# ChipContext Information Sufficiency Audit — seed41

## Purpose

This audit tests whether the first structured-feedback treatment omitted, weakened, or over-emphasized evidence needed for the next hardware-design decision. It replays the sealed seed41 E0/E1 records without calling a model, Vivado, Verilator, or any simulator.

The analysis starts from `main@6bf8aa45c2f053fcc34bb4cd58a7a17db60e930e`. Full reports remain in controlled storage. This document contains only hashes, evidence categories, counts, and non-sensitive conclusions.

## Method

The auditor reconstructs each actual model request from provider metadata and cross-checks it against the saved model messages, tool definitions, assistant action, and tool result. For every state-changing action it records:

- whether each evidence family was pushed, available through a structured query, available only through a raw read, or unavailable;
- whether the evidence remained inline, remained as a recent tool result, had been replaced by an archived hash, or was absent;
- whether the exact evidence bytes had previously reached a model request.

Assistant prose is never treated as proof that evidence was observed. E0/E1 candidates are aligned descriptively by decision type and evaluation ordinal; they are not treated as identical designs.

## Reproducibility

Three clean replays produced identical record hashes:

| Record | SHA-256 |
|---|---|
| Audit manifest | `55be095f1ec6ef133fc3137a3425cb8b554fedc72a5f610dd81bed61a6982061` |
| Decision points | `4634daa515693dabfd8b0337631b45f9f5c95cf81e42a66096f5059fe44b4237` |
| Evidence coverage | `e858b038f927300e9cc57cac4cc72ab59d2fa3b4b1bd875a98db33a81418e7dc` |
| Pair comparison | `b7d03964b35af6a79d1502e8648160f9331b8599eb63cae5fc0e8fc90384bb23` |

The audit reconstructed 21 decision points and 210 decision-by-evidence-family rows from 216 hashed input files per arm.

## Findings

### What E0 consumed before candidate-04

Before producing its final new best, E0 had consumed source/diff evidence, correctness results, scalar PPA, timing reports, current/baseline/best state, raw timing evidence capable of manual path comparison, validation state, and provenance. It had not consumed current-candidate generated RTL or an explicit branch-history summary.

### What changed in E1

E1 retained the same broad evidence families, but the representation remained state-oriented:

- current-candidate generated RTL was unavailable; the `read_generated_rtl` tool exposed only frozen baseline RTL;
- parent-relative and best-relative deltas were not computed;
- no explicit critical-path signature or endpoint/path-group movement was produced;
- no current-branch search-history summary was exposed;
- evaluation and query cost were excluded from Agent-visible structured feedback.

The structured PPA block contained delay and LUT deltas but omitted WNS, TNS, registers, endpoint counts, and failing endpoints. Those values were still present in the outer evaluation response, so this is a structured-representation loss rather than complete delivery loss in seed41.

### What the 9-to-4 raw-read reduction means

| Raw access | E0 | E1 |
|---|---:|---:|
| Candidate differential-result reads | 7 | 0 |
| Candidate timing-path reads | 2 | 4 |
| Baseline timing reads | 9 | 14 |

The five-read reduction came from eliminating seven repeated differential-result reads while candidate timing-path reads increased by two. Correctness remained automatically pushed, so the audit does not find a missing correctness evidence family. E1 also read more baseline and candidate timing detail, which means its weaker final QoR cannot be summarized as “the Agent saw too little timing information.”

The seven E0 differential reads all targeted one content reference, so six were repeats. E1's four candidate timing reads targeted two references, with two repeats.

The combination of automatic top-three timing paths and more timing reads is consistent with a possible timing-detail anchoring effect, but this retrospective audit cannot establish causality.

## Consequence for DecisionPacket work

The next feedback revision should be tested around decision transitions rather than adding more timing detail. The evidenced candidate fields are:

1. current versus parent delay/LUT and correctness transition;
2. current versus best delay/LUT and new-best/promotable state;
3. critical-path signature and endpoint/path-group movement with coverage limits;
4. bounded current-branch history and remaining evaluation budget;
5. validation cost kept separate from design facts.

This report does not authorize those schema changes by itself. Issue #20 must test them at frozen decision points before claiming that they improve search quality.

## Boundaries

- The two arms diverged before their first evaluated candidate and the provider sampling seed is not controllable.
- Evidence availability and consumption are measured exactly; the Agent's internal reasoning is not inferred.
- Associations with candidate validity or QoR are descriptive and are not causal conclusions.
- Every audit output is `analysis_only`, is ineligible for Agent context and KnowledgeStore ingestion, and must not be reused as optimization memory.
