# CC-00／CC-01 实施与校准记录

## 范围与版本

- 工作包：Issue #7 的 CC-00／CC-01。
- 实施基线：`main@257b26079f11f6a03c23d5136d6910e78c6a5bab`。
- schema：`chipcontext.*.v1`；parser：`legacy-evaluation-v3`；
  recipe：`failure_summary_v1`、`comparable_delta_v1`；policy：
  `cc01-static-required-v1`。
- 实现：确定性身份/状态/来源契约、内容寻址文件存储、legacy adapter、
  两个最小配方、ContextPacket、Markdown 渲染和有界原文查询。
- 未实现：Agent/runtime 接入、`feedback_mode`、DesignIndex、CC-02～CC-06、
  付费模型调用和优化效果实验。
- 与计划的偏差：初始 32/200 GiB 资源门槛不适合这台 32 GB/约 100 GiB
  空闲磁盘服务器；经用户确认，为单槽、无模型 smoke 建立新配置版本，将门槛
  调为 30/80 GiB。该门槛不用于双槽搜索资格。

## 公开安全样例

两个 fixture 均为 synthetic，中性命名且不包含 BOOM 历史解法、候选补丁或
真实 EDA 日志。每个 fixture 从三个空输出目录重建，内容哈希和 Markdown
完全一致。

| 样例 | Manifest | Snapshot | Bundle | ContextPacket | Packet canonical bytes |
|---|---|---|---|---|---:|
| success | `6966aef3a768ccf2477d4b3430ac171b1563019fbff89c987e248c139d5c5d36` | `78bc94bd8b67bf373a8895e19df6d9d2176b5c997b823c73458a4118cccb8962` | `bd8e5bc5408351b1020ccc06f3a6930fced9c424866da542f50898b8d1a4c669` | `da3e5d266953c3fcda08f577d0a58b29102c04e59f5eaa047e9d24aad3261782` | 3207 |
| failure | `5ae982348494ef3d3e912dfd07f4fa415c398ce4a22d787cd3b3872af535d3fb` | `672d23adcb56d619e36df8d52850d0883f6aa78e7a8e4c0f27ea87cedf51b3c1` | `fa67b7ee0ac4933cb1abe738f867745004c4818e6b273d648fab96e1a86416f5` | `8d3144a54eeab390eff892db9e6929ab0dfaa703f9a0d199b251e4f2e60414af` | 3415 |

失败样例的 `raw_error_ref` 为
`12045edf3fa886241e42f92550b613b3ce083efdfecbada14d9dcd498dd57118`。
按第 2～5 行查询返回 byte span 42～115、原始 artifact hash 和未截断状态，
内容包含固定 cycle、seed、input、expected 和 actual，可由摘要回到原始反例。

## 私有真实证据校准

在受控服务器上只读适配一个已有成功 attempt 和一个已有差分失败 attempt。
真实源码、补丁、生成 RTL、完整报告、主机身份和路径没有进入公开仓库；公开
机器可读摘要见
[`chipcontext-cc01-controlled-calibration.json`](chipcontext-cc01-controlled-calibration.json)。

| 样例 | 一致字段 | missing / conflict | prepare | query | 读取字节 |
|---|---:|---:|---:|---:|---:|
| success | 15 / 15 | 5 / 0 | 90.000 ms | 70.000 ms | 922 |
| failure | 8 / 8 | 9 / 0 | 90.000 ms | 60.000 ms | 328 |

成功 attempt 的身份、attempt、工作源码、原始/规范 stage、完成状态、artifact
hash 和 8 个 post-synth 指标均与既有记录一致。失败 attempt 的身份、阶段、
失败类别和原始 stdout 引用一致。历史记录没有证明 lint、formal、route 等检查
已执行，失败记录也没有结构化 expected/actual；adapter 保持 `not_run`、
`not_collected` 或 `not_supported`，没有补造结果。两例的候选源码/attempt、
baseline 和 qualification 绑定均为 `verified`，最终 packet 的规范字节数分别与
`budget.required` 完全一致。

第一次真实校准暴露了一个反例：历史差分错误写在 stdout，而初版配方只优先
识别 stderr。实现随后改为按登记的失败产物类型选择 stdout/stderr，并新增回归
测试；最终 failure 查询命中原始 stdout 的 SHA-256。

## 验证结果

服务器 Python 3.12.3 环境实际执行：

- ChipContext 离线故障矩阵：50 项通过；
- 完整 harness：102 项通过；
- `compileall`、两个 CLI help 和所有 shell 脚本语法检查通过；
- 无 DS API 调用、无 Ray 启动、无 Vivado 运行、模型费用为 0。

低资源配置下 public `doctor --require-qualification` 全部通过，沿用的新版
qualification 合同和工具版本保持 current。第一次 smoke 暴露了一个入口反例：
相对 `--output` 在子进程切换 cwd 后被再次解释，工具执行成功但结果写入错误目录；
该失败证据被保留。入口随后在启动前把 config/output 规范化为绝对路径并增加
回归测试。review 修复后又在新目录执行一次 10,000 周期
baseline-vs-baseline smoke，通过全部八个定向阶段和随机差分，耗时 17.611 秒，
模型调用为 0；差分结果哈希与此前一致。私有日志和路径仍不公开；
doctor、失败 smoke 和通过 smoke 只在机器可读摘要中保留哈希与必要统计。

## 结论与下一批建议

CC-01 支持的结论是：同一批 legacy 输入能确定性地产生可追溯证据；版本错配、
未评估源码、不可比指标、未知/缺失结果和越权查询会显式失败或降级。它尚未
证明结构化上下文能提高 Agent 的成功率、QoR、速度或 token 效率。

PR review 的五类反例已纳入合同：发布后的记录不会被后续 list/dict 修改；评估
必须绑定真实源码和 attempt；baseline/qualification 必须绑定冻结清单；基础
设施失败不会伪装成功能失败，回归结果按实际 producer 字段归一化；packet 预算
覆盖最终带 hash 的记录，而不是预算字段加入前的中间 payload。第二轮复审的
两个残留分支也已纳入：基线 stage 缺失时保持未知且禁止 delta；同一检查的
payload、stage 和 summary 必须一致，冲突时记录来源并输出 `inconclusive`。

CC-02 的最小增量建议为：复用现有 manifest/snapshot/store，增加原始
Vivado/Verilator 支持字段提取、当前候选完整产物查询和路径采集范围；先补齐
真实历史记录中未表达的 interface/check provenance，再冻结 E0/E0.5/E1 输入，
不要在这一批引入 runtime 或模型搜索。
