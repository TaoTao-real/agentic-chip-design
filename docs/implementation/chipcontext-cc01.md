# CC-00／CC-01 实施与校准记录

## 范围与版本

- 工作包：Issue #7 的 CC-00／CC-01。
- 实施基线：`main@257b26079f11f6a03c23d5136d6910e78c6a5bab`。
- schema：`chipcontext.*.v1`；parser：`legacy-evaluation-v1`；
  recipe：`failure_summary_v1`、`comparable_delta_v1`；policy：
  `cc01-static-required-v1`。
- 实现：确定性身份/状态/来源契约、内容寻址文件存储、legacy adapter、
  两个最小配方、ContextPacket、Markdown 渲染和有界原文查询。
- 未实现：Agent/runtime 接入、`feedback_mode`、DesignIndex、CC-02～CC-06、
  付费模型调用和优化效果实验。
- 与计划的偏差：冻结工具链的 `doctor --require-qualification` 未通过当前
  资源门槛，因此按停止规则没有启动新的 baseline-vs-baseline smoke。

## 公开安全样例

两个 fixture 均为 synthetic，中性命名且不包含 BOOM 历史解法、候选补丁或
真实 EDA 日志。每个 fixture 从三个空输出目录重建，内容哈希和 Markdown
完全一致。

| 样例 | Manifest | Snapshot | Bundle | ContextPacket | Markdown bytes |
|---|---|---|---|---|---:|
| success | `59920f9599e5f4b7fd92fba25d39d6930418b5f2ef24ac86c30b5f4b5feb0204` | `a95f6b179532924e365c85ba16da79d6d06fc3e96e2a36379933291393a9daa2` | `d816ddcb1d71acb3c4ac2209bef343bfbaf72cc3fa967a6e92ac7e376b37596f` | `18d82fec14774b388e8019a3b9a4b57f287cf4d8e2805bc45853fc9b57df058b` | 1705 |
| failure | `4981466d84726f03c9e9aec879ca867914207a0b3b829ebe3214a67d05843080` | `461039dc2b9707a9f0c440defda6f0696ee7d16f759b7bd8792c7ce2327bd33b` | `0955f1c37b0fb1ca2dec76c01edc59b39384fdc44f1a7531ccaf71f4e40be8d5` | `29af6943b390015aff30c8c742d8733b8040781566e295bda1678fcaadbd2e4d` | 1867 |

失败样例的 `raw_error_ref` 为
`ca836449fa682d03ba297386bbb412d2c37e3e054c207e5e19edf2bcbe7547c2`。
按第 2～5 行查询返回 byte span 42～115、原始 artifact hash 和未截断状态，
内容包含固定 cycle、seed、input、expected 和 actual，可由摘要回到原始反例。

## 私有真实证据校准

在受控服务器上只读适配一个已有成功 attempt 和一个已有差分失败 attempt。
真实源码、补丁、生成 RTL、完整报告、主机身份和路径没有进入公开仓库；公开
机器可读摘要见
[`chipcontext-cc01-controlled-calibration.json`](chipcontext-cc01-controlled-calibration.json)。

| 样例 | 一致字段 | missing / conflict | prepare | query | 读取字节 |
|---|---:|---:|---:|---:|---:|
| success | 15 / 15 | 5 / 0 | 20.719 ms | 0.453 ms | 475 |
| failure | 8 / 8 | 9 / 0 | 22.400 ms | 0.422 ms | 316 |

成功 attempt 的身份、attempt、工作源码、原始/规范 stage、完成状态、artifact
hash 和 8 个 post-synth 指标均与既有记录一致。失败 attempt 的身份、阶段、
失败类别和原始 stdout 引用一致。历史记录没有证明 lint、formal、route 等检查
已执行，失败记录也没有结构化 expected/actual；adapter 保持 `not_run`、
`not_collected` 或 `not_supported`，没有补造结果。

第一次真实校准暴露了一个反例：历史差分错误写在 stdout，而初版配方只优先
识别 stderr。实现随后改为按登记的失败产物类型选择 stdout/stderr，并新增回归
测试；最终 failure 查询命中原始 stdout 的 SHA-256。

## 验证结果

服务器 Python 3.12.3 环境实际执行：

- ChipContext 离线故障矩阵：31 项通过；
- 完整 harness：81 项通过；
- `compileall`、两个 CLI help 和所有 shell 脚本语法检查通过；
- 无 DS API 调用、无 Ray 启动、无 EDA 运行、模型费用为 0。

环境脚本确认既有 qualification 仍为通过且两个物理槽的历史资格记录可读；
随后 public `doctor --require-qualification` 的 qualification 子检查通过，但总门禁
因当前可用内存低于固定 32 GiB 下限、可用磁盘低于固定 200 GiB 下限而失败。
因此没有用旧 smoke 冒充本次 public smoke，也没有降低门槛或修改配置继续运行。
私有 doctor 和环境日志只以哈希出现在机器可读摘要中。

## 结论与下一批建议

CC-01 支持的结论是：同一批 legacy 输入能确定性地产生可追溯证据；版本错配、
未评估源码、不可比指标、未知/缺失结果和越权查询会显式失败或降级。它尚未
证明结构化上下文能提高 Agent 的成功率、QoR、速度或 token 效率。

CC-02 的最小增量建议为：复用现有 manifest/snapshot/store，增加原始
Vivado/Verilator 支持字段提取、当前候选完整产物查询和路径采集范围；先补齐
真实历史记录中未表达的 interface/check provenance，再冻结 E0/E0.5/E1 输入，
不要在这一批引入 runtime 或模型搜索。
