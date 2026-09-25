# ChipContext：确定性证据准备

状态：**CC-00／CC-01、CC-02a 与 CC-02b 离线查询层已实现；Agent/runtime 接入和效果消融尚未实现。**

首批实现位于
[`experiments/boom-v2/harness/chia_boom/chipcontext/`](../experiments/boom-v2/harness/chia_boom/chipcontext/)，
基于 `main@257b26079f11f6a03c23d5136d6910e78c6a5bab`。它不调用模型、
不初始化 Ray、不运行 EDA，也不改变现有 `campaign.py` 或 `interactive.py`。

## CC-00：继承的强基线

现有 BOOM harness 已经提供候选谱系、冻结合同、阶段结果、哈希、原始工具
产物和最终验证状态。当前 raw Agent 能读取 baseline source、baseline timing、
允许范围内的 baseline RTL 和紧凑评估结果，并能请求既有构建/验证工具。

它还不能查询当前候选的完整生成 RTL、完整 Vivado 报告或运行任意分析脚本。
因此后续消融的对照名称必须是“相对当前 raw-tool harness”，不能把它描述为
没有工具的裸模型基线。

## CC-01：已实现的数据路径

```text
CandidateArtifact + result.json + EvaluationArtifact + frozen manifest
                              ↓ legacy adapter
                    EvaluationManifest
                              ↓
                     EvidenceSnapshot
                              ↓ recipe
                      EvidenceBundle
                              ↓ policy
                       ContextPacket
                              ↓
                 Markdown + bounded drilldown
```

领域层只使用 Python 标准库。内容身份来自规范 JSON 的 SHA-256；时间戳只进入
独立事件记录，不参与 manifest、snapshot、bundle 或 packet 的内容哈希。
文件存储采用临时文件加原子发布，已有同哈希记录只读复用。

### v1 对象与不变量

| 对象 | v1 内容 | 关键不变量 |
|---|---|---|
| `CandidateRef` | 实验命名空间、候选 ID、源码/合同哈希、真实父版本 | 相同候选 ID 在不同实验中不是同一对象 |
| `WorkingState` | 当前源码、最后评估对象及版本关系 | 当前源码变化时 packet 显示 `not_evaluated`，不继承历史 delta |
| `ArtifactRef` | 内容哈希、attempt、受控根内位置、大小和访问级别 | 只能用已登记引用读取，读取前重新校验哈希 |
| `EvaluationManifest` | 原始/规范 stage、合同/工具/参考身份、状态和产物 | `synthesis` 映射为 `post_synth`，原值仍保留 |
| `Measurement` | 定义版本、有限数值或 null、单位、阶段、范围和来源 | 拒绝 NaN、Inf 和未知单位 |
| `CheckRecord` | `executed` 与 `pass/fail/inconclusive` | 未执行不能表示为成功或失败 |
| `EvidenceSnapshot` | 某 attempt 的检查、测量、观察、缺失和来源 | 缺失不填零，解析失败不静默吞掉 |
| `EvidenceBundle` | 针对问题的失败摘要或严格可比差值 | 跨阶段、器件、时钟、工具、定义或单位不做 delta |
| `ContextPacket` | 选择结果、遗漏、缺失、下钻引用和字节预算 | 是可重建视图，不是新的测量权威 |

缺失原因固定为 `not_run`、`not_collected`、`parse_failed`、
`not_comparable`、`not_supported` 和 `inconclusive`。

Legacy 评估只有同时绑定候选 ID、实验、源码和 attempt 才会被接纳；同 ID 的旧
源码结果、缺失 ID 或错误 attempt 会被拒绝。baseline 与 qualification 还必须
通过 prepare request 的 `manifest_bindings` 指向冻结清单中的逻辑文件项，并在
读取时重新校验文件 SHA-256。没有该绑定时仍可形成部分证据，但不得产生性能
差值。

## 配方

`failure_summary_v1` 保存失败阶段、类别、检查范围、候选记录引用、原始错误
引用、seed、expected/actual 及明确缺失项。摘要不会复制完整日志。

`comparable_delta_v1` 只比较同阶段、同器件、同时钟、同工具指纹、同参考输入、
同指标定义和同单位的结构化数值。任一条件不成立时返回 `not_comparable`，
不产生差值；当前工作源码尚未评估时同样禁止继承旧性能。基线缺失的器件、
时钟和工具条件只有在 baseline/qualification 均由同一冻结清单验证后才可从
合同继承。基线阶段必须来自记录中的显式字段或 parser 版本所定义的 legacy
stage 映射；不能从当前候选的阶段反向推断，缺失时保持未知且不计算差值。

同一检查的 payload、stage 和可用 summary 会统一核对。来源相互矛盾时不会按
字段优先级选择成功或失败，而是记录 provenance conflict，并将检查结果标为
`inconclusive`；基础设施中断和默认 `false` 也不会被解释成设计功能失败。

## 离线使用

在 harness 目录安装后运行：

```bash
chia-chipcontext prepare \
  --request chia_boom/chipcontext/fixtures/success/request.json \
  --output /tmp/chipcontext-success

chia-chipcontext read-artifact \
  --store /tmp/chipcontext-success \
  --artifact <registered-ref-id> \
  --start-line 1 --line-count 20 --limit-bytes 8192
```

查询默认上限 8 KiB，硬上限 64 KiB，返回原始内容哈希、实际 byte/line span、
截断标志和下一页 cursor。`controlled` 产物必须显式授权。路径穿越、符号链接
逃逸、未知引用、内容变化和引用元数据篡改均会拒绝读取。

`max_payload_bytes` 约束最终 ContextPacket 的规范 JSON 记录，包含
`schema_version`、`content_hash` 和 `budget` 本身；`budget.required` 必须等于该
最终记录的实际字节数。达到上限允许发布，少一个字节即以
`required_evidence_overflow` 失败。

## 公开安全样例与校准边界

仓库提交两个明确标为 synthetic 的中性样例：

- success：build、interface、differential 和 post-synth 通过，产生可比差值；
- failure：build 通过而 differential 失败，可从摘要读回原始反例行。

固定输入从空目录连续准备三次时，四级证据哈希和 Markdown 必须完全一致。
这些样例验证 schema、存储和查询，不代表真实 BOOM parser 校准。私有真实证据
校准只公开输入/输出哈希、字段计数、缺失/冲突、耗时和读取字节数，不发布
候选源码、补丁、RTL、完整日志、服务器身份或凭据。

本批的实际测试、受控校准和环境门禁结果见
[`implementation/chipcontext-cc01.md`](implementation/chipcontext-cc01.md)。

## CC-02a：原始工程证据提取

CC-02a 已加入 `vivado-2024.1-v3` 和
`verilator-differential-v3`。它们只解析 EvidenceStore 已校验的同一份字节，
把 PPA、已采集 timing path、差分检查和首个 grounded mismatch 保存为
`chipcontext.extraction.v1` sidecar。每个事实都带 artifact hash 与 JSON pointer
或 byte/line span；timing path 同时保存 top-k 和过滤范围，因此未出现在报告中的
路径仍是未知。

原始报告值会和 `legacy-evaluation-v4` 逐项核对。一致时合并来源，定义、单位、
stage 或数值不一致时记录 conflict，该指标不产生 delta。既有 BOOM 评分入口也
调用同一 Vivado 解析核心，但返回接口保持不变。

字段来源、公开样例、复测命令、受控校准边界和当前限制见
[`implementation/chipcontext-cc02.md`](implementation/chipcontext-cc02.md)。

## CC-02b：显式范围的领域查询

查询基础用明确的 store、snapshot、候选和 attempt 定位封存证据，并回答候选
检查状态、登记产物、有界原文读取、失败观测、严格指标比较和已采集时序路径。
每个确定性答案保留适用源码版本、缺失、
冲突、证据条件和内容引用；当前工作源码变化时只显示 `historical`，不会把历史
PPA 当作当前结果。

store 路径和权限来自可信注册表，查询内容与分页 cursor 不能扩大权限。解析器会
核对 snapshot 到 manifest、candidate、contract、attempt、extraction 和 artifact
的归属及内容身份。artifact 和 extraction 都可以成为事实来源；extraction 会展开
到原始 artifact 做权限与哈希校验。所有查询在筛选前授权候选公共 envelope，空
结果不会泄露受控候选身份；原文分页保持 UTF-8 字符完整，非法文本明确拒绝。
答案不返回机器路径或 artifact 存储位置。指标比较逐项区分可比、缺失、冲突和
不可比，delta 固定为 current-reference；路径查询保留原始 rank、top-k 和解析
coverage，不把过滤结果称为全局最差路径。公开纵向样例与
当前 API 边界见
[`implementation/chipcontext-cc02.md`](implementation/chipcontext-cc02.md)。

### 统一离线查询命令

`chia-chipcontext query` 从受信 registry 选择封存 store，并严格校验
`chipcontext.query-request.v1`。请求只能选择六类已冻结操作：候选状态、产物、
失败、指标比较、时序路径和原文读取；请求本身不能携带路径、权限、正则、shell
或 `latest/best` 别名。

```bash
chia-chipcontext query \
  --registry trusted-stores.json \
  --request requests/candidate-status.json \
  --format json \
  --max-output-bytes 16384
```

默认只授权 `public`。只有受信 registry 已允许 `controlled` 且调用方显式使用
`--allow-controlled` 时，查询才会读取受控证据。store 以只读方式打开，查询不会
创建 alias、事件或缓存。JSON 与 Markdown 由同一个确定性 QueryAnswer 渲染；
动态耗时和读取成本位于独立 QueryCost，不影响 answer hash。

结构化输出默认上限 16 KiB，硬上限 64 KiB，按最终 UTF-8 字节计算。放不下时
返回 `budget_exceeded`，不会删去冲突、来源或范围信息。scoped 原文读取支持在
固定行范围内用 cursor 续页，每页重新检查权限和内容哈希，并保持 UTF-8 字符
完整。接口、示例、错误语义、计量口径与受控校准见
[`implementation/chipcontext-cc02b.md`](implementation/chipcontext-cc02b.md)。

## 当前没有实现

- 没有把 packet 交给 Agent，也没有新增 `feedback_mode`；
- 没有 DesignIndex、源码到 RTL 的实体映射；
- 没有 CC-03 runtime bridge、CC-04 Agent 消融、CC-05 留出模块；
- 没有 CC-06 AI workload 或软硬件协同设计；
- 当前字节预算只防止序列化溢出，不表示已经找到最优 token 预算；
- 尚未声称 ChipContext 提高了优化成功率、速度、token 效率或 QoR。

下一批 CC-02c 将冻结工程问题集并进行系统级成本验收；它仍不接入搜索 runtime
或 Agent。
