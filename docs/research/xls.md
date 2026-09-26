# XLS：表示、调度与多后端验证的工程参考

核对日期：2026-09-24。阅读范围：google/xls 官方 README 与文档入口；默认分支尚未固定 commit，未本地编译或复现 PPA。

## 原项目支持的内容

XLS（Accelerated HW Synthesis）将高层功能描述转换为可综合 Verilog/SystemVerilog，支持函数与有状态 proc。工具栈包括 DSLX、IR、优化、调度、代码生成、解释/JIT 与验证相关工具；官方还提供跨执行引擎的测试和 fuzzing 设施。[S1][S2]

这说明“高层设计作为软件执行，同时产生硬件”有成熟的工程研究路径，但不能由此推导任意旧 RTL 都可自动迁移，或高层设计天然获得更好物理结果。

## 与 PyCircuit 的关系

本次材料支持比较二者的表示与工具设计，不支持将 PyCircuit 描述为 XLS 的官方前端、fork 或已经完成互通的后端。任何真实依赖或 lowering 关系，都应由固定版本代码确认，不能因为都生成 Verilog 就认定相互兼容。

我们的近期任务是保持已有模块的周期可见行为；自动调度形成流水线可能改变这个合同。采用高层工具前，要先回答延迟、吞吐、状态、reset 和接口约束，而不是仅比较语法好不好生成。

## 对本项目的建议

借鉴分层表示、明确的调度约束、可定位诊断，以及解释器/IR/RTL 的交叉检查。PyCircuit 扩展应复用已有命名与类型机制，逐步构建查询视图，而非把一切信息混成一个新的大 IR。

可让 LegacyDesignAdapter 与 PyCircuitAdapter 产生共同的设计快照和产物 manifest。XLS 如被选为额外对照，也通过相同契约接入；不要求本项目重写其优化器、调度器或综合工具。

## 最小验证

选小型组合运算或周期合同明确的子块，先验证独立参考的一致性，再比较生成、检查与定位成本。若比较调度策略，必须将允许增加的延迟或寄存器代价预先纳入合同。

共享上游 IR 的解释器和 RTL 后端可能具有共同缺陷；跨后端一致是有用证据，不等于对业务规范的完整证明。最终收益仍通过目标工具和 workload 测量。

## 来源

- [S1] [google/xls README](https://github.com/google/xls)
- [S2] [XLS 官方文档](https://google.github.io/xls/)
- 相关项目笔记：[PyCircuit](pycircuit.md)、[CAKE](cake.md)
