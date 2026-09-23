# BOOM v2 实验环境、部署与重放

## 1. 已验证环境

受控实验使用 Linux 执行，Mac 只提交任务和收集结果。已验证的服务器
配置如下；主机身份、地址和凭据不属于公开配置。

| 项目 | 已验证值 | 用途 |
|---|---|---|
| OS | Ubuntu 22.04.5 LTS | Chipyard、CHIA、EDA 执行 |
| CPU | 64 个逻辑核 | 最多两个物理任务并行，每任务最多 12 核 |
| RAM | 32 GiB | Q0 实测两任务峰值合计不超过 24 GiB 才开放双槽 |
| 磁盘 | 约 250 GB 可用 | 两个隔离 Chipyard 工作区与 EDA 产物 |
| Python | 3.12.3 | 固定 BOOM/CHIA 实验环境 |
| CHIA | `chialoops==1.0.1` | `ChiaFunction`、Ray、Chipyard/Verilator 节点 |
| Ray | 2.54.0 | 服务器端持久调度与资源槽 |
| Vivado | 2024.1 | `xc7z020clg400-1` 综合与布局布线 |
| Verilator | 5.020 | 差分验证与处理器回归 |
| Icarus | 12.0 | 可选 RTL 辅助检查，不是正式评分器 |
| Java/Scala | 由冻结 Chipyard 环境提供 | BOOM Chisel elaboration |

冻结的源码版本：

- Chipyard `4ab72313087580a44d647b52923389a06ec0712f`
- BOOM `3229345a4f6388562f81ce1955deefe1a4f9acbd`
- MegaBOOM 配置 `MegaBoomChiaBigCacheConfig`

## 2. 目录约定

创建两个完整、互不共享可变文件的工作区：

```text
$CHIPYARD_WORKSPACE_ROOT/
├── slot-01/chipyard/
└── slot-02/chipyard/

$AGENTIC_CHIP_LAB_ROOT/boom-public-v1/
├── frozen/
│   ├── inputs/<target>/baseline-source.scala
│   ├── baseline-rtl/<target>/
│   └── q1-fixtures/              # 私有，可选
├── qualification/
└── campaigns/
```

不要让两个 Ray 任务使用同一个 Chipyard checkout。候选应用前，节点会把
BOOM 子仓恢复到冻结提交；候选只能修改配置中允许的单个 Scala 文件。

## 3. 安装 Python 环境

在本目录执行：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -c 'import chia, ray; print(ray.__version__)'
```

`pyproject.toml` 固定 `chialoops==1.0.1`；其依赖固定 Ray 2.54.0。
本仓库不复制 CHIA 源码，依赖来源和许可证见
[`THIRD_PARTY.md`](THIRD_PARTY.md)。

## 4. 准备 Chipyard 工作区

每个槽都必须是递归初始化的 Chipyard checkout，并分别冻结顶层和 BOOM
子仓提交。可从内部已有镜像复制，也可按上游方式递归克隆。完成后逐槽检查：

```bash
git -C "$CHIPYARD_WORKSPACE_ROOT/slot-01/chipyard" rev-parse HEAD
git -C "$CHIPYARD_WORKSPACE_ROOT/slot-01/chipyard/generators/boom" rev-parse HEAD
```

两个输出必须分别等于上面的 Chipyard 和 BOOM 提交。Chipyard 自带的
`env.sh` 必须能够设置 RISC-V 工具链和 Scala 构建环境。

## 5. 配置路径和密钥

复制示例配置到受控实验目录后设置以下环境变量：

```bash
export AGENTIC_CHIP_HARNESS_ROOT="$PWD"
export AGENTIC_CHIP_LAB_ROOT=/srv/agentic-chip-lab
export CHIPYARD_WORKSPACE_ROOT=/srv/chipyard-slots
export CHIPYARD_ROOT="$CHIPYARD_WORKSPACE_ROOT/slot-01/chipyard"
export CONDA_SETUP=/opt/miniconda3/etc/profile.d/conda.sh
export VIVADO_SETTINGS=/tools/Xilinx/Vivado/2024.1/settings64.sh
read -rsp 'DeepSeek API key: ' DEEPSEEK_API_KEY && echo
export DEEPSEEK_API_KEY
```

程序会展开配置中的环境变量；若仍含未展开的 `$...`，配置门禁会拒绝
启动。请求记录只保存密钥环境变量名，不保存密钥值。

正式盲测时，Agent 工作区不得包含本仓库之外的历史 IssueQueue 补丁、
目标专用 Episode、人工根因分析或原始会话。

## 6. 生成冻结输入并执行 Q0/Q1

先从已验证工作区复制基线源码并记录哈希：

```bash
python chia_boom/scripts/prepare_inputs.py \
  --config chia_boom/config/issueq-blind.example.json
```

再启动 Ray。密钥必须在启动 Ray 前进入 head 节点环境：

```bash
bash chia_boom/scripts/start_ray.sh
```

执行资格门禁：

```bash
chia-boom qualify \
  --config chia_boom/config/issueq-blind.example.json
```

Q0 会三次独立 elaboration、综合和布局布线，生成基线 RTL、原始时序和
PPA；关键延迟重复差异必须不超过 1%。它还依据两项代表作业的内存峰值
决定开放一个还是两个并行槽。

资格证据同时绑定实验配置、资源设置、源文件、黄金 RTL、验证脚本、
回归二进制和 Python/Java/Verilator/Vivado 版本。启动 campaign 时这些
内容会复制进 campaign 私有的 `frozen/` 目录并整体封印；后续全程只读
这份副本。配置、文件或工具版本漂移会拒绝启动，必须重新执行资格门禁。
如果没有外部通用经验包，D 组会冻结仓库内非空的跨目标过程知识，不能
静默退化成 C 组。

公开配置将 `qualification.q1_replay_required` 设为 `false`，因为仓库不能
分发历史候选源码。要严格重放内部 v13 Q1，需在私有目录挂载三类固定
fixture（基础设施恢复、递归 elaboration、no-op），提供 `manifest.json`，
并把该开关设为 `true`。fixture 不得进入 Git 或 Agent 盲测上下文。

## 7. 预检、正式搜索和最终验证

先运行 seed 41 的 B/C 预检：

```bash
CAMPAIGN="$AGENTIC_CHIP_LAB_ROOT/boom-public-v1/campaigns/preflight-01"
chia-boom preflight \
  --config chia_boom/config/issueq-blind.example.json \
  --campaign "$CAMPAIGN"
```

预检要求真实候选变化、正确性通过、C 的后一轮能看到前一轮完整 diff 与
原始错误，且所有模型、CHIA、验证和 EDA 产物能通过哈希关联。

正式 A/B/C/D 搜索、恢复和最终验证：

```bash
CAMPAIGN="$AGENTIC_CHIP_LAB_ROOT/boom-public-v1/campaigns/formal-01"
chia-boom run \
  --config chia_boom/config/issueq-blind.example.json \
  --campaign "$CAMPAIGN"
chia-boom resume --campaign "$CAMPAIGN"       # 仅在中断或 infra_blocked 后
chia-boom finalize --campaign "$CAMPAIGN"
chia-boom report --campaign "$CAMPAIGN" > "$CAMPAIGN/report.json"
```

`run` 完成搜索门禁；`finalize` 从干净工作区执行新随机种子差分、后布局
布线和完整 MegaBOOM `rsort.riscv` 回归。只有 `final_valid=true` 且延迟
优于基线，才计为 `valid_improvement`。

搜索评估、基础设施重试和 finalization 分别保存。finalization 缓存绑定
候选 ID、完整源码哈希、目标、约束、黄金输入和回归二进制；不匹配或可
重试的基础设施失败不会作为成功缓存复用。报告分别列出搜索模型时间、
搜索 EDA 时间、finalization 时间和端到端墙钟时间，finalization 不会改写
原始搜索成本或候选历史。

## 8. 交互式盲盒与知识消融

交互式 Agent 通过工具自主读源码、原始时序、生成 RTL、应用编辑和发起
评估：

```bash
chia-boom interactive \
  --config chia_boom/config/issueq-blind.example.json \
  --output "$AGENTIC_CHIP_LAB_ROOT/boom-public-v1/campaigns/k0" \
  --memory-mode none \
  --max-turns 24 --max-evaluations 5
```

- `none`：K0，无知识。
- `generic`：K1，只读取仓库内跨模块过程知识。
- `target`：K2，必须通过配置 `knowledge.root` 显式挂载私有目录；公开仓库
  不含目标答案。K2 衡量复用，不用于声称盲发现。

交互搜索后仍须单独执行 `interactive-finalize`，不能用搜索期缓存替代最终
验证。

## 9. 结果与安全检查

每个候选至少保存 parent、完整源码、diff、模型可见反馈、脱敏请求/响应、
provider usage、阶段状态、原始错误尾部、PPA、耗时和哈希。正式报告要区分：

- `candidate_valid`：elaboration、差分和后综合通过，面积合格；
- `promotable`：相对父候选更优，可进入下一轮；
- `final_valid`：干净重建、后布线和处理器回归全通过；
- `valid_improvement`：最终有效且优于基线。

`interface_ok` 来自 baseline/candidate 顶层端口名称、方向和位宽的严格
签名比较；它不由 Chisel elaboration 结果代替，也不再把未执行的 lint
标记为通过。

### 运行时信任边界

单文件 allowlist 只约束实验结果，不是 OS 级安全沙箱。配置、冻结输入、
构建脚本和验证程序必须来自可信维护者。生产部署应把模型凭据服务与构建
worker 分开：构建 worker 使用非特权用户或容器，不持有 API key，不能
写入评分器和黄金输入，只读挂载 campaign 的 `frozen/` 目录。当前 harness
保证密钥值不进入提示、命令和证据文件，但同一 Unix 用户下的任意构建进程
仍可能读取该用户可访问的环境和文件。

提交公开 PR 前至少运行：

```bash
python -m unittest discover -s chia_boom/tests -v
python -m compileall -q chia_boom
rg -n 'API[_-]?KEY\s*[:=]|/home/|@[0-9]+\.[0-9]+\.|candidate.*patch' .
```

公开仓只提交代码、配置模板、通用知识和聚合结论。候选补丁、完整请求、
服务器身份、密钥、生成 RTL、完整 EDA 日志与 Vivado 授权材料留在受控
EvidenceStore。
