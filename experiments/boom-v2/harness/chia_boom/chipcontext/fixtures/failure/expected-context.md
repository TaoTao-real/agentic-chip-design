# ChipContext v1

- Experiment: `synthetic-failure-campaign`
- Candidate: `neutral-failure-candidate-01`
- Working state: `evaluated_current`
- Bundle kind: `failure`
- Completeness: `partial`

## Selected evidence

- **candidate_identity**: synthetic-failure-campaign/neutral-failure-candidate-01 source=1f323dd9235f contract=41f3ee01095d
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

- `candidate_record` → `1693716180da06152b124297b41eac4ab4d14f3df2f6867f100835bdb62216b6`
- `evaluation_record` → `d8ca9830be63994e5423e71d8798bc3dbd386782235bfbb365848d4c7159b49b`
- `sealed_manifest` → `554a4457221c69346d2cb9260ce6bca414b31d5c5db3b1fb808169643d25c6fc`
- `baseline_measurement` → `0fe530efde94d04ef3c150cb3ab935f6cd7e5f0e3abcc404ef63086e1e32c43d`
- `working_source` → `59e62609becececc29cf32ba8b3a7082b5fb6bebf5c997762bdb2b7acdc0d380`
- `qualification_record` → `5970d03720481ad9b99f32898bc682d4c0bb59f487ea1eb26c804720fdf7729b`
- `differential_stderr` → `12045edf3fa886241e42f92550b613b3ce083efdfecbada14d9dcd498dd57118`
