# CC-03T Minimal OptimizationTrace + Candidate DAG

状态：工程实现和离线校准已完成；真实 E0/E1T 结果在受控服务器实验结束后回填。

Refs #20 #21 #23 #24 #25

## 1. 研究问题

本实验只改变一个因素：E1T 在与 E0 相同的源码、原始工具、权限、父候选策略和验证预算上，额外获得不超过 6 KiB 的确定性 TraceTail。TraceTail 只含 parent/current/best、上一条 transition、selected-parent 祖先链最近三条终态结果、剩余评估预算和已有 raw 引用。

旧 E1 的结构化时序反馈、Design Episode、Issue #21 审计结果、sibling patch、历史最佳解法、generated RTL 语义和策略建议均不进入实验。

## 2. 已实现合同

- `OptimizationTrace v1`：RunTrace、DecisionPoint、Action、Candidate、Evaluation、Transition 和 CostSpan。
- `Candidate DAG v1`：稳定的 `candidate-00` baseline、CandidatePool 和 ParentSelection。
- `baseline-3-best-2-v1`：前三个评估槽从 baseline 分叉，后两个从当时 current best 分叉；没有优于 baseline 的候选时明确回退 baseline。
- `TraceTail v1`：规范 JSON、内容哈希、6 KiB fail-closed 门禁，只沿 selected-parent ancestry，不暴露 sibling。
- `VisibilityManifest v1`：每次请求记录共同上下文、工具 schema、raw 权限和 treatment hash。
- `ValidationTimeline v1`：记录模型、编辑、elaboration、interface/differential、Vivado、Trace/feedback 和 finalization 阶段；无法从旧产物分离的时间保留为 `null + reason`。

`trace-build` 只读取 sealed campaign，并拒绝把结果写回输入 campaign。它不调用模型、EDA 或模拟器；输入在构建前后重新哈希。

## 3. 运行入口

```text
chia-boom trace-build --campaign <sealed-campaign> --output <new-directory>

chia-boom cc03t-prepare \
  --config <frozen-runtime-config.json> \
  --manifest <cc03t-experiment.json> \
  --output <new-directory>
chia-boom cc03t-run --output <directory>
chia-boom cc03t-resume --output <directory>
chia-boom cc03t-report --output <directory>
```

配置中的 `model.id` 必须是 `deepseek-v4-pro`，端点必须是官方 DeepSeek API，密钥只从 `DEEPSEEK_API_KEY` 读取。prepare 阶段不读取密钥，也不启动 Ray 或硬件工具。

## 4. 离线校准

在封存的 seed41 E1 campaign 上从三个空目录重建 Trace，三个 `TRACE_MANIFEST.json` 文件逐字节相同：

- manifest 文件 SHA-256：`e93aa8611aa79effdcd852496b05b8b9152a3eba411afbd0aff63d75f9463ff4`
- manifest content hash：`8255cc2332204248328f58495647af0092dfaa22d5d917f52619f4b6f35eb850`
- RunTrace content hash：`4d9ac6096e9ded8c331f25b52a7bd15afae1fff3f0801a2f49a04d43eccfe232`
- 重建内容：4 条 transition、46 条真实工具 action
- 新调用：0 model、0 EDA、0 simulator

这证明 Trace 重建是确定的；它不证明 TraceTail 会提高 Agent 的 QoR。

## 5. 真实实验协议

Experiment A 按 D0、Dfail、Dperf 的冻结顺序串行运行。每个 fork 最多 24 turn，只允许一次真实 evaluation。Dfail 的失败候选自身作为 repair parent，并显式标记为 fixture override；它不冒充完整 3+2 SearchPolicy。

只有 Decision-level gate 通过才运行 seed46 的一对完整搜索。完整搜索最多 48 turn、5 次 evaluation，最佳有效候选才进入三 seed differential、post-route 和 MegaBOOM `rsort.riscv` finalization。

最终结论只使用四个标签：`positive_qor_smoke`、`positive_efficiency_smoke`、`negative_smoke` 或 `inconclusive`。单个 matched pair 不支持统计显著性或普遍优势声明。

## 6. 当前限制

- CHIA/Ray 的 scheduler queue 尚无独立时间源，因此保存为 `null` 并说明原因。
- 旧 evaluation 把源码 materialize 与 Chisel elaboration 合并；旧轨迹的 materialize 时间保持 unknown。
- current-candidate generated RTL 仍未成为 Agent 可调用的 TraceTail 输入。
- Trace 只表达可从封存证据确定推导的关系，不生成 root cause、策略标签或因果结论。
