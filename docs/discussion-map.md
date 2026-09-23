# 讨论演进与当前结论

本页根据可见项目讨论及架构对齐稿整理，不是缺失回复的逐字恢复，不包含账号排障等非项目聊天。

| 议题 | 保留的方向 | 当前定位 |
|---|---|---|
| AI 业务负载与 infra 驱动设计 | workload → compiler → 资源 → 物理反馈 | 长期目标，BOOM 是方法验证对象 |
| CHIA / CAKE 等启发 | Agent 与可执行工具、表示协作 | 文献题名、版本和数字独立核验 |
| 不训练专门模型、不重造 simulator | 研究同模型的环境增益 | 阶段范围选择，不否定他人路线 |
| PyCircuit / XLS / ACIR | 区分显式硬件、架构建模与计算块综合 | 不假定已有自动互转或整芯片 lowering |
| PGO / feedback-enriched IR | 反馈关联设计而非污染规范语义 | DesignIndex + 外部 Evidence |
| Context 与 Memory | 当前观测、过程知识和历史假设分开 | 知识库不只是日志向量库 |
| ChipAgents / ChipStack | orchestration 本身不是充分差异化 | 厂商主张另行核实，不继承营销数字 |
| BOOM 初期 POC | 从建议到可验证硬件改动 | 带提示及工程修复不等于独立发现 |
| BOOM v2 K0/K1/K2 | 自主恢复、通用知识和目标复用的区分 | 单次配对信号，不证明完整两段式或 PyCircuit |
| DSH 插件 | 模型前交付已整理证据 | 重解析在评估后做，runtime 只薄适配 |
| 专家 Prompt 挑战 | 专家 Prompt + 脚本是强基线 | 用内容、反馈、表示和成本消融回答 |
| 最终对齐 | 四个领域模块、两段式执行、快慢循环 | 是同一系统的不同视图 |

未完成事项：多次独立搜索统计；完整可独立 EDA 复现材料；DSH/PyCircuit 实际版本与接口锁定；外部论文和公司主张核验。参见 [核验队列](references-to-verify.md)。
