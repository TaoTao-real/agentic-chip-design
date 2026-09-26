# 外部材料核验队列

核对更新：2026-09-24。研究入口已整理到 [docs/research](research/README.md)，包含逐项笔记、[综合映射](research/synthesis-and-plan.md) 和 [来源登记](research/sources.md)。

“已阅读原始资料”不等于“已独立复现”。论文按所读版本登记，仓库默认分支资料采用前仍需固定 commit；历史聊天中的数字和 API 不能直接成为工程事实。

| 材料 | 本轮完成 | 下一项核验 |
|---|---|---|
| [CHIA](research/chia.md) | 论文/项目定位与 PR4 实际依赖、调用关系 | 固定依赖下真实执行、成本与恢复 |
| [AgentDSE / 2606.21836](research/agentdse.md) | 识别正式题名与仿真闭环方法 | 各实验条件及复现 |
| [CAKE](research/cake.md) | 表示/反馈方法及对照范围 | 固定实现、GPU 结果复现和芯片迁移 |
| [PyCircuit](research/pycircuit.md) | 双前端与既有 IR 机制 | 固定版本、局部等价与来源覆盖 |
| [XLS](research/xls.md) | 编译工具栈与适配边界 | 固定构建、周期合同、独立参考 |
| [DSH](research/dsh.md) | context/loop/compaction 接入机制 | 真实版本化插件、取消/压缩/恢复 |
| [JEV DataOps](research/jev-dataops.md) | 记录级筛选与类型化判断的边界 | EDA rubric、shadow 校准、全部成本 |
| [LayerFS](research/layerfs.md) | 工作区机制及 0.1.6 限制 | durability/兼容性风险与 I/O 实测 |
| [RRSI](research/rrsi.md) | 受约束 Harness 演化及成本口径 | 芯片任务留出实验、演化总成本 |
| [Dream-RSI](research/dream-rsi.md) | 历史回放、探索边界和与 RRSI 的区别 | 完整代码、正文/附录目标函数、在线验证 |
| [ChipAgents](research/chipagents.md) / [ChipStack](research/chipstack.md) | 厂商公开机制与我们的可检验差异 | 不以产品主张代替独立同模型实验 |

继续跟踪 ACD-016。各笔记中的迁移建议与尚未实现 API 明确标记；文档 PR 不改变现有实验合同、ADR 状态、运行代码或历史测量。
