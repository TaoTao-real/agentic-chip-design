# CC-03T Minimal OptimizationTrace + Candidate DAG

状态：工程实现、离线重建、冻结工具链资格验证和 D0 配对实验已完成。D0 门禁失败，按预注册停止规则未运行 Dfail、Dperf 和 end-to-end。

Refs #20 #21 #23 #24 #25

## 1. 研究问题与唯一变量

本实验只改变一个因素：E1T 在与 E0 相同的源码、原始工具、权限、父候选策略和验证预算上，额外获得不超过 6 KiB 的确定性 TraceTail。TraceTail 只含 parent/current/best、上一条 transition、selected-parent 祖先链最近三条终态结果、剩余评估预算和已有 raw 引用。

旧 E1 的结构化时序反馈、Design Episode、Issue #21 审计结果、sibling patch、历史最佳解法、generated RTL 语义和策略建议均未进入实验。VisibilityManifest 证明两组的 common context、tool schema 和 raw permissions 哈希相同；E1T 每次请求额外得到 573 bytes TraceTail。

## 2. 已实现合同

- `OptimizationTrace v1`：RunTrace、DecisionPoint、Action、Candidate、Evaluation、Transition 和 CostSpan。
- `Candidate DAG v1`：稳定的 `candidate-00` baseline、CandidatePool 和 ParentSelection。
- `baseline-3-best-2-v1`：前三个评估槽从 baseline 分叉，后两个从当时 current best 分叉；没有优于 baseline 的候选时明确回退 baseline。
- `TraceTail v1`：规范 JSON、内容哈希、6 KiB fail-closed 门禁，只沿 selected-parent ancestry，不暴露 sibling。
- `VisibilityManifest v1`：每次请求记录共同上下文、工具 schema、raw 权限和 treatment hash。
- `ValidationTimeline v1`：记录模型、编辑、elaboration、interface/differential、Vivado、Trace/feedback 和 finalization 阶段；无法测量的值保持 `null + reason`。

`trace-build` 只读取 sealed campaign，并拒绝把结果写回输入 campaign。它不调用模型、EDA 或模拟器；输入在构建前后重新哈希。

## 3. 离线 Trace 校准

在封存的 seed41 E1 campaign 上从三个空目录重建 Trace，三个 `TRACE_MANIFEST.json` 文件逐字节相同：

- manifest 文件 SHA-256：`29929de7ad127e89a1743c1786013e35af5bfe359c5d3535f372563d2dad298a`
- manifest content hash：`3360b367333869fb9f459b4e1a5005a6a13c6b34a28217ef1cc762e5ee288f51`
- RunTrace content hash：`4d9ac6096e9ded8c331f25b52a7bd15afae1fff3f0801a2f49a04d43eccfe232`
- 重建内容：4 条 transition、46 条真实工具 action
- 新调用：0 model、0 EDA、0 simulator

这证明 Trace 重建是确定的；它不证明 TraceTail 会提高 Agent 的 QoR。

## 4. 冻结工具链资格

资格文件 SHA-256 为 `e8384ebdf86b94e795477bd35f7c5806aba23fe8e7a0cc5c8bb84d2f7401b000`，资格指纹为 `b7eaa979ef59707729ad70564d61a0095e13e5f0d92051dafeaf25b039de55c1`。

| 检查 | 结果 |
|---|---:|
| post-synth delay，三次 | 31.920 / 31.920 / 31.920 ns |
| post-synth LUT，三次 | 49,214 / 49,214 / 49,214 |
| post-route delay，三次 | 28.962 / 28.962 / 28.962 ns |
| post-route LUT，三次 | 50,160 / 50,160 / 50,160 |
| Q1 replay | 3/3 checks passed |
| 双槽完整进程树峰值 | 23,067,615,232 bytes，低于 24 GiB 门槛 |
| qualified physical slots | 2 |

正式实验仍按预注册采用串行物理任务。官方 DeepSeek API 最小预检返回 `deepseek-v4-pro` 和 HTTP 200，共 41 tokens；凭据只通过 Ray worker 环境注入。

## 5. D0 配对结果

| 指标 | E0-control | E1T |
|---|---:|---:|
| model calls | 24 | 23 |
| provider tokens | 273,180 | 434,057 |
| tool calls | 41 | 31 |
| edit calls | 1 | 2 |
| evaluate calls | 0 | 1 |
| candidate evaluations | 0 | 1 |
| valid candidates | 0 | 0 |
| synthesis calls | 0 | 0 |
| model API active | 164.537 s | 335.255 s |
| candidate validation active | 未执行 | 170.763 s |
| total wall | 165.724 s | 506.615 s |
| TraceTail bytes | 0 | 13,179（23 × 573） |
| Trace/feedback active | 0 | 0.273 ms |

E0 在第 18 turn 修改源码，但到第 24 turn 上限仍未调用 evaluation，状态为 `turn_limit_exhausted`。E1T 在第 23 turn 调用 evaluation；候选通过 Chisel elaboration 和接口检查，但差分测试在 cycle 28 的 `io_dis_uops_1_ready` 首次不一致，因此为 `candidate_correctness_failure`，未进入 Vivado。

门禁的唯一失败原因为：`D0-seed41: E0 violated single-candidate protocol`。缺失 transition 时 parent identity 被记录为 unavailable，不再误报为 mismatch。

## 6. ValidationTimeline 结论

E1T 唯一一次候选验证中：

- Chisel elaboration：153.855 s；
- interface check：0.028 s；
- differential build/run：16.880 s；
- candidate validation 合计：170.763 s；
- Vivado：未启动；
- Trace build + feedback prepare/ready：0.273 ms。

本次实际执行的最耗时阶段是 Chisel elaboration。Trace/feedback 开销远小于模型 API 和候选验证时间，满足工程开销门禁。Ray scheduler queue 没有独立时间源，继续保存为 `null`，没有填零。

## 7. 对 Issue #25 十个问题的回答

1. **Minimal Trace 能否稳定重建 parent → current → best？** 能。离线三次重建逐字节一致；正式 E1T 的 transition 从 `candidate-00` 指向候选 01，best-before 仍为 baseline，失败候选没有成为 best。
2. **Candidate DAG 是否在真实运行中产生多个 baseline sibling？** 没有。代码和 public tests 已验证 3＋2 policy，但真实实验在 D0 gate 停止，E0 没有候选，E1T 只有一个 baseline child，不能把单元测试能力当成真实搜索证据。
3. **D0／Dfail／Dperf 的实际结果如何？** D0 如上：E0 未评估，E1T 评估后正确性失败。Dfail 和三组 Dperf 因 D0 gate 失败而未运行。
4. **E1T 是否至少持平 E0？** 当前不能判定。E1T 比 E0 少一次模型调用和 10 次工具调用，并实际提交了候选；但候选无效，token 和总时间更高。两组都没有 verified QoR。
5. **TraceTail 的额外成本是多少？** 每请求 573 bytes，共 13,179 bytes；Trace build/prepare/ready active 合计 0.273 ms。单独 CPU、hash 和物理 I/O 未测，保持 unknown。E1T 多出的 160,877 tokens 来自已经分叉的整条决策轨迹，不能全部归因于 TraceTail 字节。
6. **候选验证最耗时的阶段是什么？** 已执行阶段中是 Chisel elaboration，153.855 s，占候选验证约 90%。
7. **fixed synthesis count／EDA active time 下 E1T 是否更有效率？** 无法比较。两组 synthesis count 都为 0，共同范围为空，也没有 verified QoR 曲线。
8. **是否执行 end-to-end？** 没有。预注册要求 D0 单候选协议通过；E0 未产生 evaluation，因此 Experiment A 立即停止。
9. **下一步改什么？** 暂不扩充 TraceTail，也没有证据要求改变 3＋2 SearchPolicy。应先单独预注册一个 `submit-or-stop` 决策协议，在有限 inspection budget 内强制 Agent 提交候选或明确停止，再建立新实验版本重跑 D0；不能用本次结果静默增加 turn 后补绿。
10. **哪些是事实，哪些是假设？** 上述哈希、可见性、工具动作、token、时间、失败 cycle/signal 和停止原因是测量事实。“限制 inspection 能改善有效候选率”只是下一步假设；本实验没有证明 TraceTail 改善或损害 QoR。

## 8. 结论

本轮结论为 `inconclusive_at_d0_protocol_gate`。Minimal Trace 和可见性隔离按合同运行，开销可以忽略；但本轮没有产生任何有效候选，也没有 PPA 数据，因此不能声称 E1T 持平或超过 E0。

停止是实验结果的一部分：继续 Dperf 或 end-to-end 会违反冻结门禁并把一个没有完成基本行动协议的实验包装成优化收益实验。

脱敏机器证据位于 [`cc03t-evidence`](cc03t-evidence/)。受控服务器保留完整 source、patch、prompt、工具日志和原始评估产物。

## 9. 当前限制

- Provider sampling 不能由 run seed 完全控制；seed 是实验序列标识，不是服务端随机性控制。
- CHIA/Ray scheduler queue 尚无独立时间源。
- current-candidate generated RTL 没有进入 TraceTail；Agent 仍通过与 E0 相同的 raw tool 主动读取。
- 单个 D0 pair 不支持统计显著性、普遍优势或 QoR 因果结论。
