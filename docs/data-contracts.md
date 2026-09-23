# 领域数据契约草案

状态：拟定义，schema 待 M0/M1 评审。本仓库配置不是可直接运行的上游 DSH/PyCircuit API。

| 对象 | 内容 | 不变量 |
|---|---|---|
| OptimizationContract | objective、allowed_changes、frozen_paths、budget、final_gates、stop_policy、revision | 实验前冻结，修改产生新实验版本 |
| DesignSnapshot | source_hash、compiler_revision、parameters、artifact_refs | 指向唯一源版本，不混入历史测量 |
| DesignIndex | revision、entities、origins、edges、interfaces、cycle_boundaries | 编译派生只读，不是第二份设计真相 |
| EvaluationManifest | run_id、candidate_id、parent_id、design_revision、tools、constraints、stage、artifacts、status | 先封存再发布，不依赖全局 latest |
| EvidenceSnapshot | manifest_id、metrics、failures、entity_mappings、source_refs、uncertainty | 数值、单位、阶段、环境和候选不可分离 |
| ContextPacket | snapshot_refs、policy_revision、selected_facts、omissions、drilldown_refs、content_hash | 可重建选择视图，不是测量权威 |
| DesignEpisode | observation、hypothesis、patch_ref、validation_refs、outcome、applicability、visibility | 假设和事实分开，知识隔离可审计 |

## 设计、证据和经验

设计 IR 保存规范语义。动态时序、活动率、面积和失败放外部 Evidence。历史推断放 Episode。可以生成带反馈的 IR 分析视图，但不能把不同运行的数字写成设计常量。

## 比较

先校验指标定义、单位、目标器件、工具链、约束、阶段和 workload。跨 fidelity 不直接相减。失败候选的未测性能为 unavailable；旧有效候选只能以明确历史身份展示。

## 映射

允许 exact / derived / ambiguous / unmapped 及多对多关系。生成名称不是永久身份；跨 revision 用 lineage 与 origin，并记录映射依据。不为了保留名称而默认加入破坏正常优化的约束。

## 缓存和交付

提取键包含产物哈希、解析器版本和配置；选择键另含策略、目标、设计与知识权限。交付状态按 session、branch、context generation、packet 隔离。消息持久化后再记录已交付；压缩后重建必要 snapshot。

[清单草案](../configs/evidence-policy.example.json) 仅描述建议行为。
