# Reproduction protocol

| Mode | Visible historical knowledge | Valid claim |
|---|---|---|
| `none` | none | blind discovery |
| `generic` | cross-target process lessons | blind discovery with process memory |
| `target` | explicitly mounted target history | post-holdout engineering reuse |

Never expose a target Episode, historical candidate, human root cause or known
transformation to `none` or `generic` runs.

Run `qualify`, `preflight`, `run`, `resume`, `finalize` and `report` through the
installed `chia-boom` entry point. Do not overwrite an existing formal result;
create a separately named experiment version.

Q0 requires three baseline physical runs with at most 1% critical-delay spread.
A private Q1 may replay fixed failure fixtures. Search correctness requires
directed phases plus one million deterministic random cycles. Finalization uses
three new seeds, post-route PPA and a full `rsort.riscv` regression. Candidate
errors spend candidate budget; infrastructure errors retry the same candidate.
