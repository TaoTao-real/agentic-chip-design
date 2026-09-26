# LayerFS：可替换的工作区状态后端，不是验证真值

核对日期：2026-09-24。阅读范围：官方 README 与 0.1.6 限制文档；未安装 FUSE 或运行 Chipyard/Vivado 兼容性测试。

## 原项目支持的机制

LayerFS 使用 SQLite、内容寻址存储、CDC 和 COW，提供 LayerStack、Layer、Branch、Commit 与 Workspace，支持主机物化和 FUSE 投影。目标是让工作区状态可隔离、分支、保存与复用，而不是自动判断 Agent 应阅读哪些信息。[S1]

0.1.6 文档将其标为 Developer Preview：不保证进程/系统崩溃或掉电后的耐久性，没有跨主机同步；尚未捕获的共享 mmap 脏写可能不进入 Commit；它不是恶意代码安全边界。[S2] 本笔记不将默认分支状态当成已固定的可部署版本。

## 对本项目的分析

一个实验应有确定的起始状态和产物关联，而不是只有一个可被覆盖的 slot 路径。可将 WorkspaceBackend 接到 Loop Engine，从冻结 base 建立多个候选工作区，失败后放弃或回退，必要时保存 checkpoint。

但文件系统状态不是整个工程世界：Ray 任务、活进程、工具环境、许可证和未落盘内容不会仅因目录恢复而恢复。source checkpoint 也不包含 Agent 当时可见的历史；它不能独立支持 Dream-RSI 式策略回放。

LayerFS 可能降低复制与恢复成本，却不会自动修复错候选缓存、错误 parent_id 或评估合同漂移。身份键仍需绑定设计、参考、工具、约束、阶段和种子。

## 建议采用范围

先作为可切换的实验工作区后端。原始权威证据独立封存，不能只有一个预览文件系统副本。工具静止、写入完成后再提交一致快照；不能在后台 EDA 仍写文件时把任意切面叫作最终结果。

不预先承诺“零复制使整个 EDA 快很多”。分支元数据操作、物化、FUSE 访问、提交捕获、清理与最终构建是不同成本，应分别统计。

## 最小验证

同一候选集比较隔离目录、Git worktree/可用的 reflink 基线与 LayerFS。检查实际磁盘分配、初始化/回退、构建时间、并发干扰及正确性一致性。

兼容性用例覆盖小文件、符号链接、文件锁、rename、可执行位、mmap、异常退出和超时清理。恢复测试必须验证子进程退出后才重用 slot，不能只检查目录可打开。若复制只占总时间极小部分，即使局部改善明显也不能夸大端到端收益。

## 来源

- [S1] [Ephemeral-AI-Lab/layerfs](https://github.com/Ephemeral-AI-Lab/layerfs)
- [S2] [0.1.6 limitations](https://github.com/Ephemeral-AI-Lab/layerfs/blob/main/docs/versioned/0.1.6/limitations.md)
- 相关：[Dream-RSI](dream-rsi.md)、[领域数据契约](../data-contracts.md)
