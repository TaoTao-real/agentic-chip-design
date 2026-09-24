# ChipContext v1

- Experiment: `synthetic-failure-campaign`
- Candidate: `neutral-failure-candidate-01`
- Working state: `evaluated_current`
- Bundle kind: `failure`
- Completeness: `partial`

## Selected evidence

- **candidate_identity**: synthetic-failure-campaign/neutral-failure-candidate-01 source=1f323dd9235f contract=36ab4dae92ca
- **working_state**: evaluated_current
- **validation_status**: elaboration=pass, lint=not_run, interface_signature=pass, differential_correctness=fail, post_synth=not_run, post_route=not_run, processor_regression=not_run, formal_equivalence=not_run
- **question_bundle**: failure scope=current completeness=partial

## Missing evidence

- `measurements.correctness`: `not_supported` — v1 only normalizes post-synth and post-route PPA
- `checks.lint`: `not_run` — legacy evidence does not establish execution
- `checks.post_synth`: `not_run` — legacy evidence does not establish execution
- `checks.post_route`: `not_run` — legacy evidence does not establish execution
- `checks.processor_regression`: `not_run` — legacy evidence does not establish execution
- `checks.formal_equivalence`: `not_run` — legacy evidence does not establish execution

## Drilldown

- `candidate_record` → `602993e65cb40d3d8a05c5f6b46233a18b9d346559dcbeaa13dfd4f90aaefd11`
- `evaluation_record` → `7895770b419ffb1a7e0c16c0f5d66067ce337aca72795daac1bb01aae51c2fd0`
- `sealed_manifest` → `05e6cc33e1c368a1b44e989d160bec617df1d0144253004d83e48af748d5803f`
- `baseline_measurement` → `69fa57bd853325e017116ac1262660eaf17238ecf0f12e094a6d4c4a5005bbe2`
- `working_source` → `73190977d2127e45485a1e698735e41afb2d4990b3437a42a2abe997d8cda1a4`
- `qualification_record` → `0611c8722675bf3d5fb262475579a292810e5881ad3a39425c05a47dc376f066`
- `differential_stderr` → `ca836449fa682d03ba297386bbb412d2c37e3e054c207e5e19edf2bcbe7547c2`
