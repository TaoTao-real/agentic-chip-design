# 领域数据契约

状态：CC-01 的 `CandidateRef`、`WorkingState`、`ArtifactRef`、
`EvaluationManifest`、`Measurement`、`CheckRecord`、`EvidenceSnapshot`、
`EvidenceBundle` 和 `ContextPacket` 已作为独立离线层实现；其余对象仍是草案。
这些 schema 不是上游 DSH/PyCircuit API。

| 对象 | 内容 | 不变量 |
|---|---|---|
| OptimizationContract | objective、allowed_changes、frozen_paths、budget、final_gates、stop_policy、revision | 实验前冻结，修改产生新实验版本 |
| DesignSnapshot | source_hash、compiler_revision、parameters、artifact_refs | 指向唯一源版本，不混入历史测量 |
| DesignIndex | revision、entities、origins、edges、interfaces、cycle_boundaries | 编译派生只读，不是第二份设计真相 |
| EvaluationManifest | candidate/attempt、合同、工具、参考、原始/规范 stage、artifacts、status、binding status | CC-01 已实现；结果绑定实际源码/attempt，baseline 与 qualification 绑定冻结清单后才可比较 |
| EvidenceSnapshot | manifest、checks、measurements、observations、missing、source_refs | CC-01 已实现；数值、单位、阶段、环境和候选不可分离 |
| EvidenceBundle | recipe、facts、conditions、coverage、open_needs、drilldown | CC-01 已实现 failure/delta 两个最小配方 |
| ContextPacket | snapshot_refs、policy_revision、selected、missing、drilldown_refs、content_hash | CC-01 已实现静态选择；可重建选择视图，不是测量权威 |
| DesignEpisode | observation、hypothesis、patch_ref、validation_refs、outcome、applicability、visibility | 假设和事实分开，知识隔离可审计 |

## 设计、证据和经验

设计 IR 保存规范语义。动态时序、活动率、面积和失败放外部 Evidence。历史推断放 Episode。可以生成带反馈的 IR 分析视图，但不能把不同运行的数字写成设计常量。

## 比较

先校验指标定义、单位、目标器件、工具链、约束、阶段和 workload。跨 fidelity 不直接相减。失败候选的未测性能为 unavailable；旧有效候选只能以明确历史身份展示。

## 映射

允许 exact / derived / ambiguous / unmapped 及多对多关系。生成名称不是永久身份；跨 revision 用 lineage 与 origin，并记录映射依据。不为了保留名称而默认加入破坏正常优化的约束。

## 缓存和交付

CC-01 的内容键绑定候选复合身份、合同、所有登记输入的内容哈希、parser revision
和 packet policy。发布时深度冻结对象图，alias、内容寻址记录和 content hash
必须一致；packet 字节预算覆盖带 schema/hash/budget 的最终规范记录。选择键另含
策略与知识权限。runtime 交付状态尚未实现；后续应按
session、branch、context generation 和 packet 隔离，并在消息持久化后记录已交付。

[清单草案](../configs/evidence-policy.example.json) 仅描述建议行为。
