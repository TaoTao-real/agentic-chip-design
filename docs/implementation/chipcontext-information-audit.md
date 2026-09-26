# ChipContext Information Sufficiency Audit

## Commands

Audit one sealed campaign:

```bash
chia-boom audit-information \
  --campaign <sealed-campaign> \
  --output <new-output-directory>
```

Audit a matched E0/E1 pair:

```bash
chia-boom audit-information-pair \
  --e0 <sealed-e0-campaign> \
  --e1 <sealed-e1-campaign> \
  --output <new-output-directory>
```

The output directory must not already exist and must be outside every input campaign. Both commands are read-only and accept only `memory_mode=none` campaign records.

## Evidence contract

The provider request saved in `provider-metadata.json` is authoritative. The auditor refuses the campaign when it differs from `model-messages-before.json` or `tool-specs.json`, or when the saved assistant response differs from the provider response.

Decision points are created immediately before `apply_exact_edits`, `evaluate_candidate`, `revert_source`, or `finish`. Classification depends only on prior tool state:

- `initial_design`
- `repair_after_failure`
- `optimize_after_valid_result`
- `continue_after_non_best`
- `finish_or_stop`

Each decision point contains one row for evidence families E1–E10. Availability, current exposure, and historical consumption are independent fields. An exact tool result counts as consumed only after it appears in a real provider request; an assistant claim does not count.

## Outputs

- `AUDIT_MANIFEST.json`
- `DECISION_POINTS.json`
- `EVIDENCE_COVERAGE.json`
- `PAIR_COMPARISON.json`
- `INFORMATION_SUFFICIENCY.md`

The four JSON records have deterministic content hashes. Runtime timestamps and host paths are excluded. All records carry:

```json
{
  "usage_class": "analysis_only",
  "eligible_for_agent_context": false,
  "eligible_for_knowledge_store": false
}
```

KnowledgeStore fails closed if an analysis-only file is placed in an episode directory, even if it is disguised as a normal Design Episode.

## Interpretation

The pair report aligns different trajectories only by decision class and evaluation ordinal. It never claims the E0 and E1 candidates are the same design and never attributes a QoR difference causally to an observed information difference.
