# OptimizationTrace v1：先结构化优化轨迹，再讨论 Trace Mining

状态：设计冻结候选。v1 只定义确定性 Trace 事实与关系，不引入额外 LLM analyzer / miner。

Refs #20 #21

---

## 1. 为什么现在需要 OptimizationTrace

当前 BOOM Agent 优化闭环已经能够保存：

- model request / response；
- tool call / tool result；
- Chisel source / diff；
- candidate lineage；
- correctness / PPA / timing evidence；
- token / wall time；
- best candidate / finalization。

这些记录已经足够做审计，但还没有被统一组织成：

~~~text
State
  ↓
Action
  ↓
Candidate
  ↓
Evaluation
  ↓
Outcome
  ↓
Next State
~~~

PR #19 的首轮 E0/E1 结果说明，仅把工程证据压缩成 structured summary 并不能自动改善搜索质量。后续工作的重点应从“增加更多 summary 字段”转为：

> **先把 DS 的真实优化过程结构化成可计算的 transition trace，再根据 trace 研究哪些信息、动作和轨迹模式真正与好的设计决策相关。**

因此 v1 的目标不是做“智能 Trace Miner”，而是先建立可靠、低成本、可重建的 OptimizationTrace。

---

## 2. 核心原则

### 2.1 Trace 不是聊天记录

完整 Agent transcript 仍保留用于审计，但 OptimizationTrace 只表达优化过程中的稳定工程实体和关系：

~~~text
Run
 ├─ DecisionPoint
 ├─ DesignState
 ├─ Action
 ├─ Candidate
 ├─ Evaluation
 ├─ Transition
 └─ CostSpan
~~~

### 2.2 Trace 不是新的事实真相库

能从已有事实 deterministic derive 的内容，不应复制成第二份不可校验的“真相”。

例如 parent_relative_delay 应由：

~~~text
Evaluation(parent).delay
Evaluation(current).delay
~~~

计算得到，而不是由 Agent 或另一个模型填写。

因此：

> **ChipLedger / sealed run artifacts 保存事实；OptimizationTrace 是事实之上的关系视图，可以按需 materialize，但必须可追溯到 source refs。**

### 2.3 v1 不引入 LLM

v1：

- 0 trace-miner model calls；
- 0 judge model；
- 0 semantic labeling model；
- 不自动判断“优化策略是什么”；
- 不判断“为什么这个 candidate 成功”；
- 不把相关性包装成因果。

只做 deterministic capture / derivation / query。

### 2.4 当前 branch 的历史可以用，其他 branch 的历史不能串入 blind experiment

允许：

~~~text
C1 → C2 → C3
~~~

同一 run / branch 自己刚发生过的 transition。

禁止：

- sibling arm trajectory；
- 其他 replicate；
- 旧实验 best solution；
- target-specific Design Episode；
- audit-only 分析结果；
- post-fork future evidence。

---

## 3. OptimizationTrace v1 的最小数据模型

### 3.1 RunTrace

~~~text
RunTrace {
  run_id
  experiment_id
  branch_id
  arm

  frozen_run_fingerprint
  toolchain_fingerprint
  model_identity
  memory_mode

  initial_candidate_id
  final_candidate_id?
  status

  transition_refs[]
  cost_span_refs[]

  provenance
}
~~~

### 3.2 DesignState

DesignState 表示某个 Agent 决策时刻的工程状态，而不是模型内部思维。

~~~text
DesignState {
  state_id

  run_id
  branch_id

  current_candidate_id
  current_source_sha256

  parent_candidate_id?
  current_best_candidate_id?

  evaluation_ref?
  validation_state

  remaining_turn_budget?
  remaining_evaluation_budget?

  raw_evidence_refs[]
  chipcontext_snapshot_ref?

  provenance
}
~~~

### 3.3 DecisionPoint

DecisionPoint 明确发生在 Agent 生成下一版 candidate 之前。

~~~text
DecisionPoint {
  decision_point_id

  state_before_ref
  model_request_ref
  visible_context_hash
  tool_schema_hash

  decision_kind =
    initial_design
    | repair_after_failure
    | optimize_after_valid_result
    | continue_after_non_best
    | finish_or_stop

  action_ref?
  outcome_transition_ref?

  provenance
}
~~~

### 3.4 Action

第一版只记录确定事实，不做语义策略分类。

~~~text
Action {
  action_id

  decision_point_id
  action_type =
    inspect
    | edit
    | evaluate
    | revert
    | finish

  tool_call_ref

  patch_ref?
  source_before_sha256?
  source_after_sha256?

  changed_file_count?
  changed_line_count?
  modified_source_regions?

  provenance
}
~~~

v1 不写：

~~~text
optimization_strategy = "priority mux simplification"
~~~

除非以后有独立、可验证的语义分类方案。

### 3.5 Candidate

~~~text
Candidate {
  candidate_id
  run_id
  branch_id

  parent_candidate_id?
  source_sha256
  patch_ref

  generated_rtl_ref?
  generated_rtl_sha256?

  evaluation_refs[]

  provenance
}
~~~

其中 current-candidate generated RTL 是当前已知 gap，应在后续实现中调查是否能无额外 EDA 成本注册为 ArtifactRef。

### 3.6 Evaluation

~~~text
Evaluation {
  evaluation_id
  candidate_id
  attempt_id

  correctness
  stage
  candidate_valid
  promotable

  post_synth_ref?
  timing_ref?
  utilization_ref?

  critical_delay_ns?
  slice_luts?
  wns_ns?
  tns_ns?
  failing_endpoints?
  total_endpoints?

  validation_timeline_ref?

  provenance
}
~~~

### 3.7 Transition

Transition 是 v1 最重要的关系对象。

~~~text
Transition {
  transition_id

  run_id
  branch_id

  state_before_ref
  decision_point_ref
  action_ref

  from_candidate_id?
  to_candidate_id?

  evaluation_ref?

  parent_delta {
    correctness_transition?
    delay_delta_ns?
    lut_delta?
  }

  best_delta {
    delay_delta_ns?
    lut_delta?
    became_new_best?
    promotable?
  }

  timing_transition_ref?
  cost_ref?

  availability {
    fields[]
    missing[]
    not_comparable[]
  }

  provenance
}
~~~

关键原则：

> Transition 只保存可以 deterministic 证明的关系；不能证明的关系显式 missing / unknown。

### 3.8 CostSpan

与 #20 ValidationTimeline 对齐：

~~~text
CostSpan {
  span_id
  parent_span_id?

  run_id
  candidate_id?
  transition_id?

  stage

  started_at_utc
  finished_at_utc

  wall_time_ns
  active_time_ns?
  queue_time_ns?

  model_tokens?
  tool
  attempt?
  cache_status?

  provenance
}
~~~

---

## 4. v1 必须能表达的关系

至少覆盖：

~~~text
candidate parent
candidate source hash
candidate patch
candidate → evaluation
evaluation → correctness
evaluation → PPA
candidate → current best
parent → current QoR delta
best → current QoR delta
decision → action
action → candidate
candidate → outcome
transition → validation cost
~~~

并能够重建：

~~~text
C1
  ↓ action A1
C2
  ↓ evaluation E2
PASS, -5.3 ns
  ↓
C2 becomes best
  ↓ action A2
C3
  ↓ evaluation E3
PASS, -1.2 ns
~~~

---

## 5. v1 暂时不做的“语义关系”

以下关系虽然有研究价值，但第一版不要自动生成：

- “这两个 patch 属于同一种优化方向”；
- “Agent 正在 exploitation 旧 bottleneck”；
- “这次成功是因为 priority mux 被简化”；
- “Agent 缺少全局架构视角”；
- “应该放弃当前设计思路”；
- “这条 evidence 导致了 QoR 改善”。

这些属于未来 Trace Mining / semantic feature hypothesis，而不是 v1 工程事实。

---

## 6. Trace 与 ChipLedger / ChipContext / ChipLoop 的职责边界

### ChipLedger / sealed artifacts

负责保存事实：

~~~text
source
patch
candidate
evaluation
tool output
model request
timing report
cost event
provenance
~~~

### OptimizationTrace

负责把事实组织成：

~~~text
state → action → candidate → evaluation → outcome/cost
~~~

它是关系视图，不替代原始 evidence。

### ChipContext

不负责发明 Trace 历史。

它消费 Trace，根据当前 DecisionPoint 选择少量当前决策所需信息，例如：

~~~text
last transition
current vs parent
current vs best
recent verified history
remaining budget
uncertainty
drilldown refs
~~~

### ChipLoop / SearchPolicy

以后可以消费多个 transition 决定：

- continue / stop；
- exploration / exploitation；
- candidate selection；
- 是否调用更昂贵验证。

v1 不实现这些智能策略。

---

## 7. 当前 branch history 的最小表达

v1 可以 deterministic 生成：

~~~text
BranchHistory {
  branch_id

  recent_transitions [
    {
      candidate_id
      parent_candidate_id
      candidate_valid
      delay_delta_vs_parent
      delay_delta_vs_best
      became_new_best
      evaluation_stage
      cost_ref
    }
  ]

  consecutive_invalid
  consecutive_non_improvement
  new_best_sequence
  recent_verified_gain
}
~~~

其中 recent_verified_gain 可以是最近 k 个可比 transition 的数值序列。

第一版不把：

~~~text
marginal_gain_trend = "this strategy is exhausted"
~~~

作为语义结论。

最多提供确定数值：

~~~text
verified_delay_gains_ns = [5.3, 1.2, -0.08]
~~~

由消费者自行判断。

---

## 8. Candidate generated RTL 是 v1 的重要 gap

当前 baseline generated RTL 可读，但 current candidate generated RTL 并未作为稳定 runtime evidence 注册。

需要后续实现调查：

1. elaboration 实际生成在哪；
2. evaluation 完成后是否保留；
3. 是否会被清理；
4. 是否能绑定：
   - candidate_id
   - source_sha256
   - attempt_id
   - rtl_sha256
5. 是否能零额外 EDA 成本注册为 ArtifactRef；
6. 文件规模和 hashing 开销；
7. 是否能同时服务：
   - Trace structural delta；
   - ChipContext drilldown；
   - #18 FormalModelAdapter。

v1 schema 预留 generated_rtl_ref，但缺失时必须显式 unavailable。

---

## 9. Timing transition 的 v1 边界

可以 deterministic 记录：

~~~text
parent:
  source
  destination
  path_group
  logic_levels
  data_path_delay

current:
  source
  destination
  path_group
  logic_levels
  data_path_delay
~~~

在数据完整时可以 derive：

~~~text
source_changed
destination_changed
path_group_changed
logic_levels_delta
data_path_delay_delta
~~~

但不能轻率声称：

~~~text
critical bottleneck moved
~~~

除非先定义稳定的 PathSignature 和跨综合版本的比较规则。

第一版可以输出：

~~~text
TimingTransition {
  parent_path_ref
  current_path_ref
  comparable
  comparison_reason

  source_changed?
  destination_changed?
  path_group_changed?
  logic_levels_delta?
  data_path_delay_delta?
}
~~~

如果 synthesis hierarchy/name 不稳定，必须保留 not_comparable。

---

## 10. 与 Information Sufficiency Audit 的关系

Information Sufficiency Audit 回答：

~~~text
Agent 当时有什么信息？
当前请求真正看到了什么？
历史上消费过什么？
~~~

OptimizationTrace 回答：

~~~text
当前工程状态是什么？
Agent 做了什么？
产生了哪个 candidate？
验证结果如何？
相对 parent/best 怎么变化？
花了多少成本？
~~~

二者互补：

~~~text
OptimizationTrace
       │
       ├─ Information Audit
       ├─ DecisionPacket
       └─ future Trace Mining
~~~

Audit 输出是 analysis_only，不能自动回流 Agent。

---

## 11. Trace Mining 的未来方向，但不进入 v1

我们最终仍希望回答：

> Trace 中哪些 observation / action / transition 特征真正提高 next-candidate quality？

未来可能比较三条路线：

### Route A — deterministic feature mining

先分析：

- parent/best delta；
- repeated reads；
- invalid streak；
- new-best sequence；
- validation cost；
- timing transition；
- patch size；
- recent QoR gain。

优点：可审计、便宜。

### Route B — 直接给 Optimizer 有界 TraceTail

例如只给最近 2–3 个 Transition，而不是完整 transcript。

用于验证：

> 强 Agent 是否已经可以自行从结构化 Trace 中提炼规律？

### Route C — 离线 LLM semantic miner

未来可以让独立 LLM 对比：

~~~text
new-best transitions
vs
non-best transitions
~~~

提出 FeatureHypothesis。

但要求：

~~~text
LLM hypothesis
↓
measurable proxy
↓
Frozen DecisionPoint experiment
↓
validated feature
~~~

LLM 不能直接成为 production truth provider。

**v1 不实现 Route B/C。**

---

## 12. 为什么 v1 不直接引入 LLM Miner

当前只有很少量真实 BOOM trajectory，且 PR #19 已经说明：

> 额外的信息组织如果方向不对，可能降低而不是提高优化效果。

现在立即加入第二个 LLM 会同时改变：

- 信息抽取；
- feature selection；
- context；
- token；
- latency；
- Agent 搜索行为。

会再次失去因果可解释性。

因此当前顺序固定为：

~~~text
Reliable Trace
    ↓
Deterministic Transition
    ↓
Decision-level experiments
    ↓
确认基础 Trace 是否有价值
    ↓
再决定是否需要 semantic mining
~~~

---

## 13. 实施顺序建议

### T0 — Gap analysis

先回答：

- 当前实体是否有稳定 ID；
- 哪些 Transition field 是 direct；
- 哪些可以 derive；
- 哪些 missing；
- generated RTL 生命周期；
- timing transition 可比性；
- validation/cost span 来源。

### T1 — Minimal Trace schema + builder

只消费已有 sealed artifacts：

~~~text
run
candidate
evaluation
tool events
model request
cost
~~~

输出 content-addressed Trace records。

要求：

- 0 model calls；
- 0 new EDA；
- deterministic；
- provenance complete；
- unknown 不填 0。

### T2 — ValidationTimeline 接入

与 #20 D0 对齐，补：

~~~text
elaboration
differential
synthesis
route
regression
model
queue
~~~

的 wall / active / queue span。

### T3 — Candidate RTL ArtifactRef

若调查证明可行，再把 current-candidate generated RTL 注册到统一 evidence / trace。

### T4 — TraceTail / DecisionPacket experiment

在 Frozen DecisionPoint 上只测试 deterministic Trace 信息是否帮助 next-candidate decision。

LLM mining 仍不进入。

---

## 14. v1 完成标准

- [ ] OptimizationTrace 有独立 schema/version。
- [ ] Trace 可以从 sealed run deterministic rebuild。
- [ ] 每个 Transition 可追溯到 source/action/candidate/evaluation refs。
- [ ] parent/best delta 由真实 Evaluation deterministic derive。
- [ ] 当前 branch history 可以重建，不读取 sibling/replicate。
- [ ] unknown / unavailable / not_comparable 显式化。
- [ ] Trace builder 0 model / 0 EDA。
- [ ] Trace 输出不会自动进入 KnowledgeStore。
- [ ] Audit-only 数据不能进入 Trace 的 Agent-visible 路径。
- [ ] 可生成至少一条真实 BOOM Transition 示例。
- [ ] 能报告 Trace 构造 CPU/hash/disk 开销。
- [ ] 不引入 semantic strategy label。
- [ ] 不声称 Trace feature 与 QoR 的因果关系。
- [ ] LLM miner 保持 deferred。

---

## 15. 设计判断

当前阶段的核心不是：

> “怎样让第二个 LLM 帮我们总结 Trace？”

而是：

> **先把现有 DS 优化过程中的事实和变化关系变成稳定、可计算、可实验的 OptimizationTrace。**

如果以后实验表明：

~~~text
E0 + deterministic TraceTail > E0
~~~

说明 Trace 本身有价值。

只有在：

~~~text
Trace 已可靠
但 Optimizer 仍无法自己提炼有效模式
~~~

时，才有理由引入额外 LLM semantic miner。

因此当前 v1 冻结为：

> **Deterministic Trace first; LLM mining later, only if experiments justify it.**

---

## 16. Candidate DAG：Trace 记录历史，但不能把搜索锁成一条链

如果每一轮都默认从最新 candidate 继续修改，搜索会退化成：

~~~text
C0 → C1 → C4 → C6 → ...
~~~

这会把第一轮的方向偏差持续放大。OptimizationTrace 因此不能被解释成“下一轮必须继承上一轮”。

正确模型是 Candidate DAG：

~~~text
                C0 baseline
               /    |     \
              /     |      \
            C1      C2      C3
            |               |
           C4              C5
~~~

其中：

- Trace 记录所有 node / edge 发生了什么；
- Candidate Pool 保存哪些历史节点仍允许作为 parent；
- SearchPolicy 决定下一轮从哪个 parent 出发；
- Agent 只负责基于选定 parent 生成下一版设计。

> **Trace 是 memory；Candidate DAG 是 search state；SearchPolicy 是 parent selector。三者必须分离。**

### 16.1 Candidate Pool

~~~text
CandidatePool {
  baseline_candidate_id
  current_candidate_id
  current_best_candidate_id

  eligible_parent_ids[]
  recent_candidate_ids[]
  verified_candidate_ids[]
  invalid_candidate_ids[]

  remaining_evaluation_budget
  provenance
}
~~~

Candidate Pool 不判断哪个设计思路更聪明，只维护候选身份、验证状态和可选 parent。

### 16.2 ParentSelection

~~~text
ParentSelection {
  decision_point_id
  selected_parent_id

  selection_reason =
    baseline_exploration
    | current_best_exploitation
    | recent_promising
    | explicit_restart

  policy_revision
  provenance
}
~~~

第一版不需要 LLM 决定 parent。

### 16.3 最小 breadth 规则

为了避免第一条优化路径把整个搜索带进死胡同，第一版建议使用非常简单、可审计的规则：

~~~text
前 N 个有效评估槽位：
  从 baseline C0 独立分叉

后续槽位：
  从 current best 分叉
  必要时保留一次 baseline restart
~~~

例如 5 次 evaluation budget：

~~~text
Eval 1: C0 → C1
Eval 2: C0 → C2
Eval 3: C0 → C3

选择当前 best，例如 C2

Eval 4: C2 → C4
Eval 5: C2 → C5
~~~

这样前半程保证 exploration，后半程做 exploitation。这个规则不是宣称 3+2 最优，而是先保证搜索广度不依赖 Agent 自己记得回退。

### 16.4 不把 sibling branch 内容自动塞给 Agent

Candidate DAG 可以保存多个 sibling branch，但详细 patch / reasoning 是否可见必须由 VisibilityManifest 决定。SearchPolicy 可以使用候选状态和 QoR 选择 parent，不等于把 sibling 解法泄漏给当前 Agent。

---

## 17. 最小 SearchPolicy v1：先固定、后学习

当前阶段不要直接做复杂 Bayesian、evolutionary 或 LLM search policy。先实现一个 deterministic policy，目标只有两个：

1. 防止线性死胡同；
2. 让 E0/E1T 的搜索成本可比。

建议 v1：

~~~text
Policy A — baseline breadth then best exploitation

phase 1:
  first K evaluation slots
  parent = baseline

phase 2:
  remaining slots
  parent = current_best

optional:
  reserve 1 restart slot from baseline
~~~

所有 parent selection 都要进入 Trace。

如果同时改变 Trace 表达、feedback、parent selection、exploration budget、LLM 和 EDA budget，就无法归因。因此第一轮验证中 E0 和 E1T 必须共享同一个 deterministic SearchPolicy，差异只能是 E1T 额外看到 deterministic TraceTail。

后续只有基础实验有效后，再考虑 epsilon-greedy、top-k pool、diversity-aware population、Bayesian acquisition、evolutionary selection 或 learned SearchPolicy。

---

## 18. 快速验证 Trace + Search 是否值得继续

当前最优先的问题不是把 Trace 做完整，而是尽快回答：

> **在相同搜索预算、相同 parent-selection policy 下，给 DS 最小 deterministic Trace 信息，能不能至少持平甚至超过 E0？**

### 18.1 实验组

~~~text
E0:
  raw-feedback Agent
  + fixed SearchPolicy

E1T:
  same E0
  + same SearchPolicy
  + minimal deterministic TraceTail
~~~

PR #19 的 E1-old state-oriented timing push 只保留作历史负面对照，不作为下一版主 treatment。

### 18.2 E1T 第一版 TraceTail

只给：

~~~text
current candidate
current best

last transition:
  parent
  current
  correctness transition
  delay delta
  LUT delta
  became_new_best

recent branch history:
  last 2–3 verified outcomes
  numeric QoR gains only

remaining evaluation budget
missing / unavailable
raw drilldown refs
~~~

不要自动 push top-N timing paths、LLM strategy summary、sibling branch patch、旧实验 solution、semantic diagnosis 或 optimization advice。

### 18.3 先做 Decision-level 实验

先选三个 Frozen DecisionPoint：

~~~text
D0: baseline → first optimization
Dfail: invalid candidate → repair decision
Dperf: valid candidate + PPA → next performance decision
~~~

每个 DecisionPoint 中，E0/E1T 使用相同 state、evaluation、SearchPolicy 和预算，各自只生成下一版 candidate 并做一次真实 evaluation。

测：

~~~text
next_candidate_valid
next_candidate_new_best
delta_vs_parent
delta_vs_best
model tokens
tool turns
decision wall time
validation wall/active time
~~~

如果 E1T 在单步实验里持续不如 E0，就不要继续跑昂贵 end-to-end。

### 18.4 再做固定 EDA 成本的 end-to-end

Decision-level 不退化后，再跑完整 E0 / E1T。必须同时报告：

~~~text
fixed evaluation-count result
fixed synthesis-count result
fixed EDA-active-time result
~~~

同样数量的 candidate 不等于同样数量的 synthesis。一个 candidate 可能在 elaboration / correctness 阶段失败，另一个会进入昂贵 synthesis。

最终至少看：

~~~text
best verified QoR / synthesis call
best verified QoR / EDA active minute
new-best yield / synthesis call
T_delivery
tokens
finalization cost
~~~

### 18.5 先测 E0 自身方差

provider sampling 当前不能由 seed 完全复现。在宣称 E1T 持平 E0 之前，应做少量 E0 repeat，估计 first improvement、best post-synth、post-route、token 和 wall-time 的方差。

### 18.6 Promotion gate

E1T 只有满足以下条件才进入更昂贵实验：

~~~text
Decision-level:
  D0 no material regression
  Dfail repair efficiency >= E0
  Dperf next-candidate QoR/new-best yield >= E0

End-to-end:
  verified QoR approximately >= E0
  AND at least one of:
    lower T_delivery
    fewer tokens
    fewer synthesis calls
    lower EDA active time
~~~

像 PR #19 seed41 那种 token 少一点但 verified QoR 明显更差，不能算成功。

---

## 19. 当前最快的实施顺序

~~~text
S0  merge/freeze Information Audit (#22)
 ↓
S1  Minimal OptimizationTrace builder
    + parent/current/best relation
    + BranchHistory
 ↓
S2  ValidationTimeline
    + stage wall/active/queue
 ↓
S3  Deterministic Candidate DAG / ParentSelection
 ↓
S4  Frozen DecisionPoint:
    E0 vs E1T
 ↓
S5  only if S4 no-regression:
    end-to-end E0 vs E1T
~~~

Candidate generated RTL、timing movement 和 LLM mining 都可以后置。

当前最快能验证核心假设的最小集合是：

> **parent/current/best + bounded own-branch history + fixed breadth/exploitation SearchPolicy + 真实成本时间线。**
