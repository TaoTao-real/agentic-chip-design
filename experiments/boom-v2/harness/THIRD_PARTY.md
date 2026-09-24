# Third-party dependencies

This directory does not vendor third-party source code.

| Component | Version / revision | Source | License | Role |
|---|---|---|---|---|
| CHIA (`chialoops`) | 1.0.1 | <https://github.com/ucb-bar/chia> / PyPI `chialoops` | BSD-3-Clause | `ChiaFunction`, Ray scheduling, Chipyard build and Verilator nodes |
| Chipyard | `4ab72313087580a44d647b52923389a06ec0712f` | <https://github.com/ucb-bar/chipyard> | See upstream | SoC build and simulation environment |
| BOOM | `3229345a4f6388562f81ce1955deefe1a4f9acbd` | <https://github.com/riscv-boom/riscv-boom> | See upstream | Chisel hardware under experiment |
| Ray | 2.54.0, pulled by CHIA 1.0.1 | <https://github.com/ray-project/ray> | Apache-2.0 | Durable resource scheduling |

Vivado is a separately installed licensed tool. No Vivado code, device database,
license material, PDK, or generated EDA artifact is included here.
