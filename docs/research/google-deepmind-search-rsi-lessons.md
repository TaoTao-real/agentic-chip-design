# Google / DeepMind 搜索、DSE 与 RSI 方向对本项目的启发

状态：研究笔记，不是当前实现合同。  
更新时间：2026-09-26。

本文整理 AlphaEvolve、FunSearch、FunBO、AlphaChip、AlphaDev，以及 2026 年公开的 RRSI / Dream-RSI 对当前 Agentic Chip Design 架构的启发。

当前项目的实现合同仍以 `docs/architecture.md`、`docs/optimization-trace.md` 和 Issue #25 为准。

---

## 1. 一个共同趋势：Generator + Search + Population + Evaluator

Google / DeepMind 这些工作的共同点不是“换一个更强模型”，而是把问题拆成：

```text
Generator / Agent
      ↓
produce candidates
      ↓
Candidate population / search state
      ↓
Evaluator
      ↓
selection / branching / pruning
      ↓
next generation
```

这和当前项目逐渐形成的 `Agent → Candidate DAG → Evaluation → OptimizationTrace → SearchPolicy` 非常接近。

> DSE 的核心价值未必来自“让 Agent 看更多信息”，而可能来自更好的搜索状态、候选群体、分叉策略和客观评估闭环。

---

## 2. AlphaEvolve：Candidate Pool + Parent Selection

AlphaEvolve 的关键不是沿最新程序线性修改，而是维护程序数据库 / population，再从历史候选中选择 parent 继续变异。

映射到本项目：

```text
AlphaEvolve program database ≈ Candidate DAG / Candidate Pool
prompt sampler               ≈ SearchPolicy / ParentSelection
LLM mutation                 ≈ DS hardware Agent
evaluator                    ≈ correctness + Vivado + final gate
```

因此我们不应该固定 `C0 → C1 → C2 → C3`，而应该支持从多个历史节点重新分叉。当前 #25 的 “baseline breadth → current-best exploitation” 是这个方向的最小版本。

参考：
- https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/
- https://deepmind.google/blog/alphaevolve-impact/

---

## 3. FunSearch：必须主动保护搜索广度

FunSearch 很重视 idea diversity，避免所有搜索过早收敛到同一方向。

这正好对应当前项目最大的风险：第一轮 `C0 → C1` 方向不理想，但后面一直从 C1 refinement，最终陷入局部死胡同。

因此 SearchPolicy 需要显式维护 baseline/restart、current best、promising historical candidates，后续再加入 diverse candidates。

> “从哪里继续搜索”不能等价于“永远从最新 candidate 继续”。

参考：
- https://deepmind.google/blog/funsearch-making-new-discoveries-in-mathematical-sciences-using-large-language-models/

---

## 4. FunBO：未来可以优化“搜索器本身”

FunBO 的长期启发是：不仅搜索 task solution，也可以搜索更好的 optimizer / acquisition function。

映射到本项目：

```text
今天：固定 SearchPolicy → 搜索 BOOM candidate

未来：OptimizationTrace → 评价 SearchPolicy → 改 SearchPolicy → 再搜索 BOOM
```

未来 RSI 更值得学习的可能不是“BOOM Issue Unit 应该改哪一行”，而是“什么时候 restart、什么时候 exploit current best、什么时候扩旧 branch、什么时候停止”。

参考：
- https://arxiv.org/abs/2406.04824

---

## 5. AlphaChip：长期更值得学习 Value / Policy，而不是答案本身

AlphaChip 把 floorplanning 表达成 sequential decision problem。对我们更重要的是其抽象：

```text
State
 ↓
Policy: 下一步做什么？
Value: 这个 state / candidate 值不值得继续？
```

未来 Knowledge / RSI 层不一定优先存“历史最佳 patch”，更值得研究的是 candidate value、branch priority 与 restart/stop 决策。

参考：
- https://deepmind.google/blog/how-alphachip-transformed-computer-chip-design/

---

## 6. AlphaDev：proposal 可以智能，但 reward 必须来自真实 evaluator

对本项目：

```text
Agent reasoning ≠ reward

真实 reward / gate:
  correctness
  delay
  area
  EDA cost
  final regression
```

LLM 的“这个方向应该更好”只能算 hypothesis，不能替代 differential / Vivado / final gate。

参考：
- https://deepmind.google/blog/alphadev-discovers-faster-sorting-algorithms/

---

## 7. RRSI：改的不是 backbone，而是 Agent Harness

RRSI（Regularized Recursive Self-Improvement of Agent Harnesses）把冻结模型外层 harness 当成可演化对象，包括 prompt、control flow、tools、memory、context management、skills/sub-agents。

它解决的核心问题是：固定 benchmark 上持续改 harness 很容易过拟合。RRSI 因此对搜索过程加 regularization：限制一次 candidate 同时改多少组件；利用完整 edit history 避免反复提出已证伪 hypothesis；stalled 时转向未探索组件；用 noise floor 阻止把评测随机波动当收益；加入 token/cost rule；prune 不再有价值的组件。

映射到本项目：

```text
RRSI harness evolution
≈ future ChipLoop / ChipContext / tool / memory policy evolution
```

当前 #25 还没有进入这一步。先要证明一个固定、简单、可审计的 Trace + SearchPolicy 本身有价值。

参考：
- https://github.com/google-research/rrsi
- https://arxiv.org/abs/2609.24972

---

## 8. Dream-RSI：Trace / Discovery Tree 不只是日志，而是 Replay Simulator

Dream-RSI 把一次真实搜索产生的 discovery tree 看成一个已经付费构建好的“世界”：

```text
online search
     ↓
discovery tree + outcomes
     ↓
replay simulator
     ↓
offline test many exploration policies
     ↓
deploy improved policy
     ↓
generate a new tree
```

核心思想是：历史 Trace 不只是给 Agent 阅读的文本，还可以成为搜索策略的离线回放环境。

如果未来 Candidate DAG 保存 parent、candidate、evaluation result、QoR、cost、branch/stopping decisions，那么我们无需重新跑 Vivado，就可以离线比较不同 branch order、stopping、budget allocation 和 parent scheduling。

但 replay 只能重放历史已经真正走到过的节点，不能凭空知道一个从未生成过的新 candidate 的 Vivado 结果。因此它适合改进搜索策略，不能替代真实硬件 evaluation。

参考：
- https://dream-rsi.com/
- https://github.com/zhengkid/Dream-RSI

---

## 9. RRSI 与 Dream-RSI 的区别

```text
RRSI:
改“Agent 系统本身”
prompt / tools / memory / control flow / harness

Dream-RSI:
改“Agent 怎么搜索”
branch / order / parallelism / stopping / exploration policy
```

映射到本项目：

```text
RRSI       ≈ future ChipLoop / ChipContext / tool / memory policy evolution
Dream-RSI  ≈ future Candidate DAG / SearchPolicy evolution
```

当前 #25 更接近 Dream-RSI 的前置基础，而不是完整 RSI：`OptimizationTrace + Candidate DAG + ParentSelection + real evaluator`。

---

## 10. 对当前项目的优先级判断

当前建议顺序：

```text
1. Minimal deterministic OptimizationTrace
2. Candidate DAG / fixed ParentSelection
3. E0 vs E1T
4. 确认 Trace + Search 不退化
5. diversity-aware SearchPolicy
6. offline replay / Dream-RSI 风格 policy improvement
7. 最后才考虑 RRSI 风格 harness recursive evolution
```

当前不建议直接做 LLM Trace Miner、自动改 prompt/tools/memory policy 或 recursive harness self-modification，因为我们还没有证明最基本的 Trace + Search 对 E0 有正收益。

---

## 11. 当前架构与这些工作的对应关系

| 本项目模块 | 外部思想 |
|---|---|
| Agent Runtime | AlphaEvolve / FunSearch generator |
| Candidate DAG / Pool | AlphaEvolve population / Dream-RSI discovery tree |
| SearchPolicy / ParentSelection | AlphaEvolve sampler / Dream-RSI exploration policy |
| Evaluation / final gate | AlphaDev / AlphaEvolve evaluator |
| OptimizationTrace | Dream-RSI replay world 的基础记录 |
| ValidationTimeline / Cost | RRSI cost-aware selection 的前提 |
| Future diversity-aware search | FunSearch |
| Future SearchPolicy learning | FunBO / Dream-RSI |
| Future harness evolution | RRSI |

---

## 12. 当前结论

> **短期：先证明 Trace + Candidate DAG + SearchPolicy 能改善真实 DSE。**

> **中期：把 accumulated Trace / Candidate DAG 变成 replayable search world，离线优化 SearchPolicy。**

> **长期：才进入 RRSI 风格的 harness-level self-improvement，让系统自动改自己的 search/context/tool/memory policy。**

当前 #25 不是 RSI 本身，而是在建设 RSI 真正需要的可验证基础：grounded history + explicit search state + objective evaluator + cost + replayable candidate relationships。
