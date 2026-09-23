# Frozen environment reference

The executable deployment guide is
[`../../../DEPLOYMENT.md`](../../../DEPLOYMENT.md). Verify these identifiers
against the selected config and the execution host:

- Chipyard `4ab72313087580a44d647b52923389a06ec0712f`
- BOOM `3229345a4f6388562f81ce1955deefe1a4f9acbd`
- `chialoops==1.0.1`, Ray 2.54.0
- Vivado 2024.1, Verilator 5.020
- `xc7z020clg400-1`, 5 ns constraint
- `MegaBoomChiaBigCacheConfig`

Use two physical slots only after qualification shows stable baselines and a
combined peak memory below 24 GiB. Keep each physical task at no more than 12
CPUs unless a new qualification version supports another setting.
