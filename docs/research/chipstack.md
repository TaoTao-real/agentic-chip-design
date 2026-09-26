# ChipStack / Cadence：共享设计上下文与 EDA 集成

核对日期：2026-09-24。阅读范围：Cadence 官方 ChipStack AI Super Agent 产品简介与发布材料；未使用产品或核验客户效果。

## 公开资料支持的内容

官方材料以共享的设计理解/mental model 连接规范、RTL、层级、关系和历史，并组织专门 Agent 与工程工具协作。EDA 集成是其产品能力的重要部分。[S1][S2]

这是厂商的公开架构与产品描述，不构成我们独立验证的实现细节或性能结果。本文不重复未经必要核验的收购时间、自治等级与加速倍数。

## 对本项目的启发

单纯维护一份“设计说明+历史记录供多个 Agent 检索”并不是足够清晰的新颖性。我们的 ContextPacket 必须区分权威设计事实、运行证据和待验证解释，且都能关联到具体版本。

LLM 从规范或 RTL 生成的理解可以用于导航和提出假设，但不能自动升级为设计 invariant。优先从编译器和工具获取可计算关系；缺失时明确未知，而不是用一份流畅描述掩盖。

## 与 PyCircuit 的互补关系

我们拟让原生表示直接提供接口、依赖、周期与来源，再让上下文选择层组织这些事实。这个方向与共享 mental model 并不冲突；区别需要通过源事实准确率、更新成本、定位效率和最终设计质量实测，而不是通过产品名称判断。

同样，不能因为公开产品页没介绍 Agent-native HDL，就断言对方内部没有结构化表示。我们的立项命题应针对开放、可替换、可验证的机制与证据。

## 建议采用与边界

复用已有 EDA 作为证据提供者，领域状态和规则保持工具无关。某个工具适配器可以增强，但不要让一个产品特有报告成为所有设计的唯一真相接口。

先用一个小模块建立版本化的 design/evidence/context 关联，再考虑协作 Agent。多个角色看到的事实要使用同一候选与合同，不是各自携带容易过期的文本摘要。

## 最小实验

比较“LLM 生成设计摘要”“确定性 DesignIndex+证据关联”“二者组合”，保持主 Agent、任务与最终门禁相同。测错误实体关联、过期事实使用、更新准备成本与达到最终有效设计的成本。厂商 mental model 的成功宣传不能替代这项验证。

## 来源

- [S1] [Cadence ChipStack AI Super Agent 产品简介](https://www.cadence.com/en_US/home/resources/product-briefs/cadence-chipstack-ai-super-agent-pb.html)
- [S2] [Cadence 官方发布材料](https://www.cadence.com/en_US/home/company/newsroom/press-releases/pr/2026/cadence-unleashes-chipstack-ai-super-agent-pioneering-a-new.html)
