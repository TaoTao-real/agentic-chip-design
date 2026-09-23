# CHIA：复用执行基础，保留领域控制边界

核对日期：2026-09-24。阅读范围：CHIA v1 论文与官方仓库，以及本项目 PR #4 的固定版本。未重新运行 CHIA、模型或 EDA。

## 原工作与实际依赖

论文题名为 **CHIA: An open-source framework for principled, agentic AI-driven hardware/software co-design research**。它将软硬件协同设计流程组织成可复用节点与有向循环工作流，允许程序化和 Agent 决策共存，并提供运行、资源及观测机制。它的范围不只是一个任务调度器。[S1][S2]

本项目不是只在概念上参考 CHIA。[PR #4 的 `pyproject.toml`](https://github.com/TaoTao-real/agentic-chip-design/blob/878e90d5dba643d6a1069614a2e9796ff8540720/experiments/boom-v2/harness/pyproject.toml) 固定依赖 `chialoops==1.0.1`；Python 导入名为 `chia`，本项目自己的包名为 `chia_boom`。这属于依赖复用，不是将上游源码整份搬入仓库。

| 实际复用入口 | 本项目用途 |
|---|---|
| `ChiaFunction`、`.chia_remote(...)`、`get(...)` | 将模型与候选评估组织为 CHIA 任务 |
| `LLMCallBase`、`QueryResult` | 自行实现 DeepSeek 官方 API 适配 |
| `ChiselBuildNode`、`BuildTarget.VERILATOR` | 最终处理器仿真构建 |
| `VerilatorRunNode` | 执行回归程序 |
| profiler collector | 获取执行事件 |

调用依据见固定版本的 [campaign.py](https://github.com/TaoTao-real/agentic-chip-design/blob/878e90d5dba643d6a1069614a2e9796ff8540720/experiments/boom-v2/harness/chia_boom/campaign.py)、[finalize.py](https://github.com/TaoTao-real/agentic-chip-design/blob/878e90d5dba643d6a1069614a2e9796ff8540720/experiments/boom-v2/harness/chia_boom/finalize.py)、[deepseek.py](https://github.com/TaoTao-real/agentic-chip-design/blob/878e90d5dba643d6a1069614a2e9796ff8540720/experiments/boom-v2/harness/chia_boom/deepseek.py)。这些链接指向已审查的 PR 版本，不表示其已经合入默认分支。

## 对本项目的分析

应当称为“本项目领域控制逻辑建立在 CHIA/Ray 和部分现成硬件节点之上”，而不是“CHIA 自动提供了完整可信实验”。精确编辑、知识处理组、候选谱系、缓存身份、重试语义和最终验收规则仍有本项目实现。

这也解释了为什么引用 CHIA 不能免除对这些组合逻辑的审查：上游节点成功，并不证明调用者拿的是同一候选、冻结的 golden 或正确的合同。修复领域完整性问题不应被错误归因成 CHIA 的缺陷。

## 建议吸收的机制

保留现有执行后端，先为 DesignSnapshot、EvaluationManifest 和阶段事件建立明确适配边界。未来 DSH 负责 Agent 会话与上下文交付，CHIA 继续执行工程任务；两者不必二选一。PyCircuit 产生新的硬件输入，也不要求重写所有 CHIA 节点。

主要研究增量应放在设计原生可观察性、当前运行证据准备和可验证的效率改善，而非重复建设已有调度系统。

## 最小验证与边界

用不依赖真实 EDA 的替身任务验证“同一候选遇到基础设施失败后继续、没有额外模型调用、结果身份不变”；再在固定版本真实环境做构建与回归 smoke。端到端可用性、所有成本与故障恢复不能仅由 import 成功或装饰器断言证明。

外部 CHIA 实验的 token 数、模型和硬件结果需要逐项核验，不作为本项目收益数字。参考 [数据契约](../data-contracts.md) 和 [评估协议](../evaluation.md)。

## 来源

- [S1] [CHIA 论文 v1](https://arxiv.org/html/2606.27350v1)
- [S2] [ucb-bar/chia 官方仓库](https://github.com/ucb-bar/chia)，默认分支材料；采用前固定 commit。
