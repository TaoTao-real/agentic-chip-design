# PyCircuit：作为设计核心，而不只是 Agent 的输出语法

核对日期：2026-09-24。阅读范围：上游 README 与 PYC IR 文档的默认分支快照；未固定整体代码 commit、未构建或重跑后端，采用前需要锁定版本。

## 上游已经表达的能力

当前 README 区分两个前端：`pycircuit` 面向信号、寄存器和显式周期的硬件构造；`agentic_circuit` 面向进程、队列、资源与架构状态。PYC 是硬件路径中的表示，具有生成 C++ 模型和 Verilog 的路径；ACIR 提供更高层建模路径。[S1]

PYC 文档中已存在 `pyc.alias`、`pyc.instance`、断言等机制。调试名称、模块边界和检查信息是可复用基础，不应被宣传成本项目从零提出的能力。[S2]

这些能力不等于本项目已经具有完整 DesignIndex、物理来源回映或对原始 BOOM 的等价证明。事务级队列抽象也不能因名字相同，就被视为现有 IssueQueue 的精确周期等价替代。

## 我们拟增加的增量

| 目标能力 | 面向 Agent 的作用 |
|---|---|
| 只读 DesignIndex | 查询依赖、周期边界、修改影响和来源，不成为第二份可编辑设计 |
| source–IR–RTL 关联 | 减少从生成信号反推源码的重复工作 |
| 局部契约与结构化诊断 | 将部分接口/类型/周期违规提前变成明确失败 |
| 反例关联与重放 | 连接失败、涉及子图、本次修改和独立参考 |

这些是提案，不是当前已提供的完整上游 API。跨版本实体关系应基于 lineage/origin；综合后允许多对多、ambiguous 和 unmapped，不能假装名字永久稳定，也不默认增加影响 PPA 的 keep 约束。

## 与 Prompt 的区别及公平对照

Prompt 可以要求定位和验证，但工具要实际计算依赖、建立映射、运行检查。Chisel/RTL 的侧车也能实现类似能力，所以应证明 PyCircuit 原生产生信息的成本、可靠性和维护优势，而非声称不可复制。

比较前先给两种语言相同的功能、接口和周期合同；单独测量移植引入的初始差异。保持同一主模型、知识、评估工具和预算，避免把编译器自动优化误计为 Agent 搜索收益。

## 推荐接入顺序

局部硬件子块 → 独立 golden 等价验证 → 原工程集成回归 → DesignIndex 查询 → ChipContext 关联 → Agent 修改与消融。新子块只以 PyCircuit 为可编辑源，生成 RTL 不再人工维护另一份真相。

动态运行反馈继续保存在外部 EvidenceStore。两个后端来自相同 IR 只能提供一致性检查，不能替代独立 golden；局部仿真也不替代最终物理验收。

## 来源

- [S1] [PTO-ISA/pyCircuit](https://github.com/PTO-ISA/pyCircuit)
- [S2] [PYC IR reference](https://github.com/PTO-ISA/pyCircuit/blob/main/docs/reference/pyc-ir.md)
- 项目内的 [PyCircuit 实施路线](../pycircuit.md) 与 [XLS 调研](xls.md)
