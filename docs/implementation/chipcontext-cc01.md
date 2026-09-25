# CC-00／CC-01 实施与校准记录

## 范围与版本

- 工作包：Issue #7 的 CC-00／CC-01。
- 实施基线：`main@257b26079f11f6a03c23d5136d6910e78c6a5bab`。
- schema：`chipcontext.*.v1`；parser：`legacy-evaluation-v2`；
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
| success | `8209c6c48bb069f083863ba009ba839e9d93ef299fd99ab158e163cad3b920da` | `f32bd5344523cfb8197f1995b40bca7f87dbb265c214904e6caaf63b7438f90d` | `a2edea30eeb149c7b35e304667f5cfb4f6fa8af4b6eb4a37d8e9a2f898da44f4` | `46760d444757fc2badf04011f533cac21b2b181289100ba14f8da8c485785122` | 3207 |
| failure | `12284fda72760b5dd99ca5cfd4486f57033a591c4d531122aec020998bf04e72` | `5568c3a0fca14ce7b84ef57a67842b7d0f5e622a3c6058efadea8bd43bccae3c` | `2ef8f35f9d475d1b8ff280a8b887a608301f9935cced65bfcb098578e0fc151b` | `542934ab353e44dd404781c092eb8faae81f36ddbbd66bbd885dff5e89af0e27` | 3415 |

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
| success | 15 / 15 | 5 / 0 | 136.459 ms | 0.404 ms | 922 |
| failure | 8 / 8 | 9 / 0 | 144.687 ms | 0.357 ms | 328 |

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

- ChipContext 离线故障矩阵：43 项通过；
- 完整 harness：95 项通过；
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
覆盖最终带 hash 的记录，而不是预算字段加入前的中间 payload。

CC-02 的最小增量建议为：复用现有 manifest/snapshot/store，增加原始
Vivado/Verilator 支持字段提取、当前候选完整产物查询和路径采集范围；先补齐
真实历史记录中未表达的 interface/check provenance，再冻结 E0/E0.5/E1 输入，
不要在这一批引入 runtime 或模型搜索。
