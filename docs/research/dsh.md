# DeepSeek Harness：上下文交付载体，不是领域真值所有者

核对日期：2026-09-24。阅读范围：官方仓库及 context/agent-loop/compaction 文档和 time-context 示例。默认分支未固定 commit；本项目尚未据此完成真实插件集成。

## 上游机制

DSH 提供可组合的 Agent runtime。官方 context 示例在 `agent/pre-step` 中准备带来源的消息；agent-loop 文档说明消息提交、模型请求和历史处理边界；compaction 提供历史压缩和大工具输出裁剪。[S1–S4]

这些挂点有助于交付领域证据，但不能把旧对话里的伪代码直接称为可运行 DSH API。开发预览阶段采用前必须固定版本，并用真实 smoke 核对生命周期。

## 对本项目的两段式接入建议

| 时机 | 领域职责 |
|---|---|
| 评估完成、产物封存 | 解析一次新增日志，校验候选/环境，生成 EvidenceSnapshot |
| 模型调用前 | 选择已经准备的数据，按证据版本变化交付 ContextPacket |
| Agent 请求详情 | 返回有界设计子图、具体路径或原始错误片段 |
| 会话压缩/恢复后 | 从持久化证据重建当前 snapshot，不依赖丢失的旧 delta |

重解析不应发生在每个 pre-step。评估完成是 Loop Engine 事件，需要我们显式连接，不是 DSH 自动认识的硬件事件。模型步骤中途也可能获得新评估，因此不能只在 `step == 1` 注入。

## 状态、缓存与边界

交付去重至少按 session、branch、context generation 和 packet identity 隔离。消息实际持久化后才记录已交付；取消请求不能提前消耗交付状态。动态证据尽量追加而非反复修改 system prompt 的前缀，但不重复注入并不等于历史不再占 token；实际缓存与费用必须按 provider usage 统计。

领域数据、解析器、候选验收和预算放在独立模块。DSH 薄插件调用这些模块；未来更换 runtime 不应改变证据格式或最终门禁。CHIA 仍执行工程任务，二者不是互斥替代。[项目内接入规划](../dsh-integration.md)

## 最小实验

先在当前 direct-API runtime 上实现静态清单 EvidencePacket，隔离其收益；再做同领域接口的 DSH 适配。测试消息持久化、重复交付、取消、并发候选、压缩与恢复。不能同时换模型、语言、检索和 runtime 后，把整体收益归给插件。

## 来源

- [S1] [DeepSeek Harness 官方仓库](https://github.com/deepseek-ai/deepseek-harness)
- [S2] [time-context 示例](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/context/time-context/src/index.ts)
- [S3] [agent-loop 文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent-loop/README.md)
- [S4] [compaction 文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/README.md)
