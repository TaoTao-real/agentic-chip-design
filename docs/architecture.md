# 目标架构与执行映射

状态：讨论整理和目标设计，不是已部署组件清单。

## 历史分层 → 最终责任

| 历史组件 | 归属 | 职责 |
|---|---|---|
| L0 Optimization Contract | Loop Engine | 目标、合法变化、最终门禁、停止条件 |
| L1 Replaceable Agent | Design Agent / runtime | 查询、诊断、假设、修改、提交 |
| L2 Context Compiler | ChipContext 的选择部分 | 有预算地选择当前事实、设计和允许历史 |
| L3 PyCircuit Design Core | Design Core | 源设计、派生 IR/索引、RTL、局部检查 |
| L4 Multi-fidelity Evaluation | Loop Engine 的评估适配层 | 调用已有工具，记录不同阶段的结果 |
| L5 Feedback Compiler | ChipContext 的解析关联部分 + EvidenceStore | 提取、校验、关联、比较、保留出处 |
| L6 Experience Plane | KnowledgeStore | 经验与适用范围，受控慢循环 |
| Experiment Controller | Loop Engine 的横切能力 | 候选 DAG、权限、版本、预算和审计 |

两段式是该系统的执行视图，不是替代逻辑分层的新架构。

## 快循环

1. 冻结合同、设计、工具、资源和知识可见性，完成必要基线评估。
2. 评估结束后封存产物，ChipContext 按产物和解析器版本增量解析。
3. 关联对应设计版本，计算同条件差异；缺失和不确定性显式化。
4. 模型调用前选择并交付 ContextPacket，保留有界下钻。
5. Agent 提出假设、修改设计，并提交 parent、patch 和证据引用。
6. 控制器调用局部门禁和分级评估；快指标通过只代表晋级。
7. 按冻结合同完成最终验证，接受、继续或预算终止。

第一段主要由确定性程序完成，不默认另建一个 LLM 来扫描日志。Agent 可调查和设计，但不能自行认证测量或修改验收规则。

## 慢循环

Episode → 跨任务复验 → 经验候选 → 人工审核 → 诊断、清单或通用操作提案。

一次成功不直接升级为全局规律；慢循环输出需要版本化并在新的留出任务上再做消融。运行中的正式实验不得自行改变编译器、解析器或策略版本。

## 两种接入模式

Legacy：保留 Chisel/RTL，增加侧车索引和反馈适配，用于低迁移成本验证。

Native：PyCircuit 是相应模块唯一可编辑源，IR、索引、RTL 从其派生；保留独立 golden。不能维持两份需要人工同步的设计真相。

v1 可以采用本地模块化代码、文件存储和 SQLite。图是关系模型，不代表必须部署图数据库或微服务。

## 信任边界

Agent 只能修改允许的候选设计；golden、测试、约束、评分器、证据和知识访问策略受保护。工具输出是数据，不是权限指令。“确定性控制器”指规则可审计，不意味着 EDA 天然逐位可重复；布局随机性和资源竞争需单独控制与测量。
