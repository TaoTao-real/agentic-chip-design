# 来源、版本与核验范围

核对日期：2026-09-24。本表记录本轮实际阅读的原始入口和范围，不表示完整代码审计、复现实验或产品认证。论文固定 v1；仓库文档来自默认分支且尚未逐项固定 commit，因此下面的可变链接是阅读入口，不是可重放的构建锁文件。

## 来源登记

| 材料 | 阅读来源/版本 | 已支持的范围 | 仍待验证 |
|---|---|---|---|
| CHIA | [论文 v1](https://arxiv.org/html/2606.27350v1)、[官方仓库](https://github.com/ucb-bar/chia)；本项目 [PR4@878e90d](https://github.com/TaoTao-real/agentic-chip-design/commit/878e90d5dba643d6a1069614a2e9796ff8540720) | 工作流方法；固定 harness 对上游的实际 import/call | 完整 CHIA/EDA 重跑、历史成本口径 |
| AgentDSE | [摘要](https://arxiv.org/abs/2606.21836)、[v1](https://arxiv.org/html/2606.21836v1) | 正式题名、仿真闭环与方法场景 | 各场景预算、公平性与复现 |
| CAKE | [v1](https://arxiv.org/html/2608.12629v1) | 显式表示与局部反馈；matched comparison 的研究设定 | 固定实现与 GPU 重跑；芯片领域迁移 |
| PyCircuit | [README](https://github.com/PTO-ISA/pyCircuit)、[PYC IR](https://github.com/PTO-ISA/pyCircuit/blob/main/docs/reference/pyc-ir.md) | 双前端边界、硬件生成路径和既有 IR 机制 | commit 固定、环境、覆盖范围、局部等价接入 |
| XLS | [README](https://github.com/google/xls)、[文档](https://google.github.io/xls/) | 高层综合、IR、调度、执行与验证设施 | 固定构建及与本项目的真实适配 |
| DSH | [仓库](https://github.com/deepseek-ai/deepseek-harness)、[context 示例](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/context/time-context/src/index.ts)、[loop](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent-loop/README.md)、[compaction](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/README.md) | 挂点、交付与压缩的设计入口 | 固定 commit 后真实插件/恢复测试 |
| JEV DataOps | [仓库](https://github.com/RenaGao/jev-dataops)、[领域指南](https://github.com/RenaGao/jev-dataops/blob/main/docs/DOMAIN_GUIDE.md)、[confidence](https://docs.typesafe.ai/confidence) | 记录级筛选、demo/真实调用区分、领域校准要求 | EDA rubric、数据权限和端到端收益 |
| LayerFS | [仓库](https://github.com/Ephemeral-AI-Lab/layerfs)、[0.1.6 限制](https://github.com/Ephemeral-AI-Lab/layerfs/blob/main/docs/versioned/0.1.6/limitations.md) | 工作区状态模型与已声明限制 | 固定版本、兼容性、真实 I/O 和故障测试 |
| RRSI | [v1](https://arxiv.org/html/2609.24972v1)、[仓库](https://github.com/google-research/rrsi)、[selection](https://github.com/google-research/rrsi/blob/main/rrsi/selection.py) | 正则化演化与成本选择；Table 2 口径 | 固定代码复现、芯片任务泛化 |
| Dream-RSI | [v1](https://arxiv.org/html/2609.14858v1)、[仓库](https://github.com/zhengkid/Dream-RSI) | 历史树回放与受限探索接口；正文/附录差异 | 完整实现发布、目标函数核对和真实新搜索 |
| ChipAgents | [RCA](https://chipagents.ai/blogs/chipagents-rca)、[timing closure](https://chipagents.ai/blogs/ai-agent-driven-timing-closure) | 厂商公开机制描述 | 私有实现、客户结果和独立同模型对照 |
| ChipStack | [产品简介](https://www.cadence.com/en_US/home/resources/product-briefs/cadence-chipstack-ai-super-agent-pb.html)、[发布材料](https://www.cadence.com/en_US/home/company/newsroom/press-releases/pr/2026/cadence-unleashes-chipstack-ai-super-agent-pioneering-a-new.html) | 厂商共享上下文与 EDA 集成定位 | 内部表示、实际部署和独立收益 |

## 如何继续维护

采用任何项目之前，补充 tag/commit、依赖锁、配置、许可证与适配范围。发布研究结论时记录相应任务、原始指标、预算、失败与复现产物引用。没有重跑的结果统一称作者报告，不能称我们测得。

本目录中的 SearchTrace、SearchPolicy、Evidence Bundle、WorkspaceBackend 及各组合实验是项目建议；它们并非声称已存在的 DSH/PyCircuit/Dream-RSI 统一 API。Dream-RSI+RRSI 也不是已核验的外部联合实现。

不转载完整论文或第三方源码，不上传私有 EDA、PDK 或历史目标补丁。与旧对话不一致时，以明确版本的原始资料为依据，并记录修正；有矛盾时保留待核验项，不自行填补。
