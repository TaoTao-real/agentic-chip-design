# ChipContext CC-03 first matched E0/E1 result

This report records the first real end-to-end matched pair requested by
Issue #17. It is an exploratory smoke, not a claim of stable benefit. The
machine-readable evidence is in
[`chipcontext-cc03-e0-e1-seed41.json`](chipcontext-cc03-e0-e1-seed41.json).

## Frozen comparison

Both runs used the same BOOM Issue Queue target, source and qualification
fingerprints, official `deepseek-v4-pro` model, seed 41, no Design Episode
memory, 48 model-turn ceiling, five-evaluation ceiling, Vivado 2024.1 flow,
PYNQ-Z2 part, and final correctness gates. Both arms could inspect the same
current-candidate raw artifacts. E1 alone received deterministic structured
feedback and structured query tools.

The implementation under test was commit
`8384c1846ca2fd05c7e3828647fe427e33ec9b7f`. Both recorded campaigns use frozen
run fingerprint
`3826a28dbbd542da77f7871127aa4579e36b12797ea435938e8c1c4205104c00`.

## Result

| Metric | E0 raw feedback | E1 structured feedback | Observation |
| --- | ---: | ---: | --- |
| First verified improvement | 600.37 s | 721.37 s | E0 was 121.00 s earlier |
| Best post-synthesis delay | 20.147 ns | 22.446 ns | E0 was better |
| Best post-synthesis LUTs | 49,418 | 50,783 | Both passed the 105% gate |
| Final post-route delay | 21.643 ns | 23.711 ns | E0 was better |
| Final post-route LUTs | 50,210 | 50,774 | Both passed the 105% gate |
| Improvement from 28.962 ns route baseline | 25.27% | 18.13% | Both improved the baseline |
| Search wall time | 1,832.09 s | 2,001.28 s | E1 was 9.23% longer |
| Model calls / HTTP attempts | 42 / 42 | 42 / 42 | Every attempt returned HTTP 200 |
| Provider-reported tokens | 1,281,580 | 1,226,120 | E1 used 4.33% fewer |
| Valid candidates | 2 / 4 | 3 / 4 | E1 had the higher valid rate |
| Agent raw-artifact queries | 9 | 4 | E1 needed fewer raw drilldowns |
| ChipContext prepare + query time | 0.504 s | 0.672 s | Negligible against search wall time |
| Independent finalization | pass | pass | Both are `final_valid=true` |

Provider usage was present for all 84 model calls. The responses did not include
a monetary billing amount, so monetary cost is recorded as unknown rather than
zero.

## What the loop actually did

E1 completed three real feedback transitions after evaluation. Its first
candidate failed elaboration; the next request received the grounded failure
and produced a functional repair. Candidate 2 then supplied measured PPA to the
next request, and candidate 3 improved the post-synthesis delay again. Candidate
4 remained valid but did not beat candidate 3, so finalization correctly kept
candidate 3.

E0 also discovered an effective implementation from raw source and timing
evidence. Its first candidate was valid, candidates 2 and 3 failed elaboration
and correctness respectively, and candidate 4 produced the best measured QoR
of this pair. No candidate in either arm received a manual patch or a human
bottleneck hint.

Each selected candidate was rebuilt from saved source, checked with three
independent one-million-cycle differential seeds, placed and routed, and used
to run the complete MegaBOOM `rsort.riscv` regression. Both regressions returned
zero and reached the normal finish marker. Neither design meets the artificial
5 ns constraint on XC7Z020; the comparison uses the same failed constraint and
reports the measured critical delay rather than claiming 200 MHz closure.

## Cost and failure evidence

ChipContext prepared four candidate contexts in each arm. E1 added four compact
structured feedback pushes and performed four Agent raw reads; E0 performed
nine Agent raw reads. The inclusive measured ChipContext work was below one
second in both arms, so feedback preparation did not explain the roughly
169-second search-wall difference.

Four earlier E1 pilots remain in controlled storage. They consumed a confirmed
2,481,437 tokens and exposed bounded-history, inspection-budget and nested raw
artifact discovery defects. They are not part of the matched pair and were not
deleted or relabeled as successful runs.

## Decision

The first pair does **not** prove that structured feedback improves the complete
optimization outcome. E1 reduced tokens, raw drilldowns and invalid-candidate
rate, but E0 found an improvement sooner and produced better verified QoR in
less wall time. The current status is `needs_adjustment`.

The next two matched seeds should be run before making a stability claim. Their
analysis should test whether the E1 summary over-focuses the current measured
path or consumes useful reasoning context. If E1 repeats the token/validity
benefit without recovering QoR or delivery time, CC-04 should treat structured
feedback as a cost-control feature rather than an optimization-quality gain.
