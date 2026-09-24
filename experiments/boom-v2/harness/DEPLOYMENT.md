# BOOM v2 实验环境、部署与重放

本文提供两种可审计的复测等级：

- **公开复测**：从冻结的公开提交重新生成 baseline，运行资格门禁、
  baseline-vs-baseline smoke，并使用复测者自己的 DeepSeek API key 发起新的
  盲盒搜索。
- **受控精确重放**：在公开复测基础上额外挂载未公开的 Q1 fixture 与受控
  EvidenceStore，用于核对内部历史事件。公开仓库不能独立完成这一等级。

API key 不属于实验输入包。所有命令只从 `DEEPSEEK_API_KEY` 环境变量读取
使用者自己的 key；`doctor`、请求记录和 smoke 只保存凭据是否存在，不保存、
回显或哈希 key 的值。

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
| Java | OpenJDK 11（受控环境） | BOOM Chisel elaboration |
| Scala | 由冻结 Chipyard 环境提供 | BOOM Chisel elaboration |

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

先克隆本仓库并进入 harness 目录：

```bash
git clone https://github.com/TaoTao-real/agentic-chip-design.git
cd agentic-chip-design/experiments/boom-v2/harness
```

安装 Python 环境：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -c 'import chia, ray; print("ray", ray.__version__)'
chia-boom --help
chia-chipcontext --help
```

### 3.1 只复测 ChipContext CC-01

CC-01 的公开样例只使用 Python 标准库，不要求 Chipyard、Ray、Vivado、
Verilator 或 DeepSeek key。仍建议在上面的 Python 3.12 环境中执行：

```bash
python -m unittest chia_boom.tests.test_chipcontext -v
OUT=/tmp/chipcontext-success
chia-chipcontext prepare \
  --request chia_boom/chipcontext/fixtures/success/request.json \
  --output "$OUT"
cat "$OUT/context.md"
```

success 和 failure fixture 内的 `expected.json`、`expected-context.md` 是固定
预期；测试会分别从三个空输出目录重建并核对内容哈希。它们是 synthetic 数据，
不能替代下面真实 BOOM 环境的 qualification、smoke 或私有证据 parser 校准。

`pyproject.toml` 固定 `chialoops==1.0.1`；其依赖固定 Ray 2.54.0。
本仓库不复制 CHIA 源码，依赖来源和许可证见
[`THIRD_PARTY.md`](THIRD_PARTY.md)。

## 4. 准备 Chipyard 工作区

准备 Ubuntu 构建依赖、Git、OpenJDK 11、Conda/Miniforge 和 Vivado 2024.1。
Vivado 必须安装 Zynq-7000 `xc7z020clg400-1` 器件支持。Chipyard 的 Conda、
RISC-V 工具链、CIRCT 与 Scala 依赖由冻结提交中的 `build-setup.sh` 建立。

仓库提供确定性的 checkout 脚本。以下命令创建两个独立 checkout，并执行
冻结 Chipyard 提交的 lean setup；该步骤耗时较长且占用大量磁盘：

```bash
export CHIPYARD_WORKSPACE_ROOT=/srv/chipyard-slots
bash chia_boom/scripts/bootstrap_chipyard.sh --slots 2 --run-setup
```

如果服务器已有匹配的 Chipyard 工具链，可以省略 `--run-setup`，但每个槽
仍必须具有可用的 `env.sh`。脚本拒绝覆盖已有目录，也不读取 DS key。

完成后逐槽检查：

```bash
git -C "$CHIPYARD_WORKSPACE_ROOT/slot-01/chipyard" rev-parse HEAD
git -C "$CHIPYARD_WORKSPACE_ROOT/slot-01/chipyard/generators/boom" rev-parse HEAD
```

两个输出必须分别等于上面的 Chipyard 和 BOOM 提交。Chipyard 自带的
`env.sh` 必须能够设置 RISC-V 工具链和 Scala 构建环境；`rsort.riscv`
必须存在于配置指定的位置。

## 5. 配置路径和密钥

复制示例配置到受控实验目录后设置以下环境变量。不要把 API key 写进 JSON、
shell 脚本、命令参数或 `.env` 文件：

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

## 6. 部署诊断、冻结输入与 Q0/Q1

先运行资格前诊断。此时不请求模型，也不要求已有 qualification：

```bash
chia-boom doctor \
  --config chia_boom/config/issueq-blind.example.json
```

输出必须为 `"passed": true`。它检查 Python、Ray、Java、Verilator、Vivado、
CPU、内存、磁盘、两个 Chipyard/BOOM 提交、`env.sh`、回归程序和 harness
工具。结果只记录 `DEEPSEEK_API_KEY` 是否存在。公开配置要求至少 24 个可用
逻辑核、32 GiB 内存和 200 GiB 可用磁盘；资格阶段会进一步用实际峰值决定
是否允许两个物理任务并行。

先从已验证工作区复制基线源码并记录哈希：

```bash
python chia_boom/scripts/prepare_inputs.py \
  --config chia_boom/config/issueq-blind.example.json
```

在启动付费搜索前，可显式检查使用者 key 是否能访问冻结模型：

```bash
chia-boom doctor \
  --config chia_boom/config/issueq-blind.example.json \
  --check-api
```

该检查只调用官方 `/models` 只读接口，不生成候选，不记录 key。

再启动 Ray。密钥必须在启动 Ray 前进入 head 节点环境：

```bash
bash chia_boom/scripts/start_ray.sh
```

脚本默认拒绝停止已有 Ray 集群，避免影响共享服务器上的其他任务。只有确认
这是专用实验主机并需要重建集群时，才显式设置 `RESET_RAY=1`；Ray worker
必须在 key 已导出的环境中启动，后续不能靠修改配置注入凭据。

执行资格门禁：

```bash
chia-boom qualify \
  --config chia_boom/config/issueq-blind.example.json
```

Q0 会三次独立 elaboration、综合和布局布线，生成基线 RTL、原始时序和
PPA；关键延迟重复差异必须不超过 1%。随后同时提交两个代表性的
elaboration＋Vivado 综合作业；只有二者均通过且进程树峰值内存合计不超过
24 GiB，才开放两个物理槽，否则固定为一个槽。

资格完成后再次检查冻结工具版本和证据：

```bash
chia-boom doctor \
  --config chia_boom/config/issueq-blind.example.json \
  --require-qualification
```

资格证据同时绑定实验配置、资源设置、源文件、黄金 RTL、验证脚本、
回归二进制和 Python/Java/Verilator/Vivado 版本。启动 campaign 时这些
内容会复制进 campaign 私有的 `frozen/` 目录并整体封印；后续全程只读
这份副本。配置、文件或工具版本漂移会拒绝启动，必须重新执行资格门禁。
如果没有外部通用经验包，D 组会冻结仓库内非空的跨目标过程知识，不能
静默退化成 C 组。

交互模式也在第一次模型调用前建立相同结构的私有 `frozen/` 快照，并将
golden RTL、验证脚本、回归程序和不可变合同绑定到
`INTERACTIVE_MANIFEST.json`。候选评估和独立 finalization 只读取该副本；
共享 qualification 目录随后被改写不会改变已经开始的交互实验。

公开配置将 `qualification.q1_replay_required` 设为 `false`，因为仓库不能
分发历史候选源码。要严格重放内部 v13 Q1，需在私有目录挂载三类固定
fixture（基础设施恢复、递归 elaboration、no-op），提供 `manifest.json`，
并把该开关设为 `true`。fixture 不得进入 Git 或 Agent 盲测上下文。

### 6.1 无模型 baseline-vs-baseline smoke

在消耗模型 token 前，用 Q0 生成的 golden RTL 建立独立私有冻结快照，并让
baseline 与自身进行 10,000 周期 directed＋random 差分：

```bash
SMOKE="$AGENTIC_CHIP_LAB_ROOT/boom-public-v1/campaigns/smoke-01"
chia-boom smoke \
  --config chia_boom/config/issueq-blind.example.json \
  --output "$SMOKE" \
  --cycles 10000 --seed 20260924
```

通过条件为命令返回 0、`SMOKE.json` 中 `passed=true`、
`differential.interface_ok=true` 和 `model_calls=0`。冻结输入、qualification、
接口、测试结果和哈希都保存在该输出目录；smoke 不读取 DS key。

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

`preflight` 和后续搜索使用调用者在启动 Ray 前提供的
`DEEPSEEK_API_KEY`。仓库没有默认 key，也不会回退到 OpenCSI、DSH 或其他
网关。

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
原始搜索成本或候选历史。首次最终验收时间来自不可变的成功 attempt 事件，
重复读取缓存只记录查询时间；无候选或非法响应的模型调用也单独计入耗时和
provider usage 完整性。

每个物理作业持有显式 workspace lease。超时或异常退出时会终止并回收
整个进程组；无法确认清理完成时，slot 写入 `QUARANTINED`，后续调度不会
再次使用该 slot，必须由操作者核查和清理。

出现 `QUARANTINED` 时，先确认该槽没有遗留 Java、Verilator、Vivado 或
make 进程，再恢复 BOOM 子仓并删除标记；不要只删除标记后立即复用：

```bash
SLOT="$CHIPYARD_WORKSPACE_ROOT/slot-01"
python chia_boom/tools/cleanup_workspace_processes.py \
  --install-root "$SLOT/chipyard"
git -C "$SLOT/chipyard/generators/boom" reset --hard \
  3229345a4f6388562f81ce1955deefe1a4f9acbd
git -C "$SLOT/chipyard/generators/boom" clean -fd
rm -f "$SLOT/QUARANTINED"
```

仅在 `doctor --require-qualification` 再次通过后恢复 campaign。服务器重启或
Ray 丢失时，重新导出使用者 key、运行 `start_ray.sh`，然后调用 `resume`；
不要用新的 campaign 目录代替恢复。

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

## 10. 复测完成判定

公开复测至少保存以下证据：

1. `doctor` 资格前结果；
2. `frozen/inputs/INPUT_MANIFEST.json`；
3. `qualification/QUALIFICATION.json`，以及三次 Q0 PPA 和双作业资源探针；
4. `SMOKE.json`，且 baseline-vs-baseline 差分通过；
5. 预检或正式 campaign 的 `manifest.json`、CHIA profile 和 `report.json`；
6. 最佳候选的独立 finalization 结果，或明确的无有效候选结论。

公开仓库可以据此验证“环境、冻结、搜索和最终验证链路是否重新运行”。由于
已知候选、历史完整会话、内部 Q1 fixture 和原始 EvidenceStore 不公开，它
不能单独证明历史搜索会生成同一个候选或取得相同 PPA 数值。
