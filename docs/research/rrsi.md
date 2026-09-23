# RRSI：对 Harness 自我改进施加可评估的约束

核对日期：2026-09-24。阅读范围：论文 v1、官方仓库说明与选择逻辑；未运行其演化 benchmark。

## 原工作支持的内容

**RRSI: Regularized Recursive Self-Improvement of Agent Harnesses** 在固定基础模型权重的条件下改进提示、控制流、工具、上下文和记忆等 Harness 组件。重点是减少开发任务过拟合、噪声追逐和无收益复杂度，而非仅允许 Agent 修改更多文件。[S1]

公开实现包含独立候选 worktree、修改历史、领域适配及选择规则。关键机制包括逐步约束独立编辑数量、评估前的 critic、成本和噪声约束，以及对无效组件的剪枝。[S2][S3]

## 结果口径

论文 Table 2 的 workspace 消融中，RRSI 的 OOD 平均分高于初始 Harness 与无正则演化。其每 trial 的 policy token 为 2.42M，低于无正则演化的 3.80M，但高于初始 Harness 的 1.56M。[S1]

因此不能概括为“自优化总能同时省 token 并提高质量”。这些是作者在指定任务上的结果，不是本项目硬件实验，也不是全部离线提案、审查和验证成本。

## 对本项目的慢循环建议

```text
多个 Design Episode
  → 找出重复失误和信息成本
  → 提出一项小范围 Harness 修改
  → 检查泄漏、权限、成本与合法性
  → 开发集评估
  → 留出集与真实任务复验
  → 接受、拒绝或删除旧机制
```

一次提案尽量只改变一个可解释机制，例如当前候选查询、失败上下文或历史选择。不要同时切换 DSH、JEV、PyCircuit 和停止策略后再归因。对包含多个改动的成功候选，整体收益不能自动分配给每个编辑；仍需消融。

## 与快循环的权限隔离

Design Agent 只改任务允许的硬件；Harness 改进 Agent 只改允许的策略/接口。二者都不能修改 golden、最终评分、留出集或既有原始结果。正式设计实验全程冻结 Harness 版本，升级必须形成新实验版本。

模型 critic 不能证明不泄漏。还需文件权限、冻结任务清单、隐藏未来结果和人工审核。有效机制也可通过更简单规则实现；不用因为它曾出现在架构图上就永久保留。

## 最小实验

比较固定专家 Harness、普通自动演化、受约束演化。按模块家族划分开发/留出任务，不将同一目标的相邻候选拆成伪泛化数据。报告失败率、同 QoR 交付成本、固定预算结果、Harness 搜索成本和后续摊销条件。

与 Dream-RSI 的组合是本项目提案：回放筛选减少真实评估候选，RRSI 式约束决定是否值得保留；不存在已核验的两者直接对战结论。

## 来源

- [S1] [RRSI v1](https://arxiv.org/html/2609.24972v1)
- [S2] [google-research/rrsi](https://github.com/google-research/rrsi)
- [S3] [selection.py](https://github.com/google-research/rrsi/blob/main/rrsi/selection.py)

源码说明来自核对日默认分支，采用前需固定 commit、验证实际领域适配。详见 [Dream-RSI](dream-rsi.md)。
