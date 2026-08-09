# Stage 2M P002 实际运行耗时与 Claim Support 失败分析

- Document type: `DIAGNOSTIC_INPUT_FOR_CODEX`
- State: `READY_FOR_ARCHITECTURAL_DECISION`
- Date: `2026-08-09 +08:00`
- Scope: Stage 2M APC，P002/C40 bounded serial 及相关历史实跑证据
- Owner: Codex
- Implementation owner after direction is approved: OpenCode default `build`

## 0. 文档地位与边界

本文只固化真实运行日志所支持的事实、诊断和建议顺序，供 Codex 决定下一轮修复方向。它不是新的
上位架构，也不替代以下权威文档：

1. `.agent/need_pipeline_audit_and_semantics.md`；
2. `.agent/concurrent_scheduling_plan.md`；
3. `.agent/plan.md`；
4. `.agent/review.md`；
5. `docs/stage2_memory_benchmark_task_closure_execution.md`。

在 Codex 明确修改现有 plan/review 前，OpenCode 不得根据本文自行改变检索语义、frozen inputs、
上下文/evidence/chunk/output budget、Gate 公式或正式实验口径。本文不授权正式 Phase 4 全量运行。

最低充分工程原则适用于后续所有修复：优先修复现有 Claim Support、Planner、Validator、Gateway 和
artifact owner，不新增并行调度平台、动态自调参系统、DAG、缓存服务、配置 DSL、遥测平台或新报告
体系。机制必须足够小，但类型、失败语义、lineage、可复现性和 Gate 证据不能削弱。

## 1. 执行结论

最新 P002/C40 串行检查点证明：当前主要耗时不是 scheduler 排队，也不能笼统归因于整个 D9、
lineage 或 Gate 架构。首要瓶颈是 Claim Support multi-slice proposal 的输出策略失效：大量请求持续
生成到 4096-token 上限后被丢弃，最终约 132 分钟的 Support 运行没有产生一个可验证的 multi
proposal，也没有进入一次 verifier。

因此当前状态应判定为：

```text
bounded serial: no scheduling timeout, but semantic execution evidence incomplete
proposal -> verification chain: FAIL (0 chains)
concurrent parity admission: NOT READY
formal P001-P005 Phase 4 admission: NOT READY
```

直接继续 concurrent P002 只能测量“同一失败 workload 能否更快结束”，不能满足当前 review 的有效
parity 条件。直接进入正式全量矩阵会放大端点成本并产生不可接受的无效实验。

## 2. 主要证据源

最新完整 bounded serial P002/C40：

- root: `/tmp/ns-stage2m-repair-parity-serial-p002-20260809-v1`
- run log: `/tmp/ns-stage2m-repair-parity-serial-p002-20260809-v1.run.log`
- experiment manifest: `experiment_manifest.json`
- support progress: `support_progress.json`
- flow summary: `flow_summary.json`
- case report: `stage2m_case_C40_A.json`
- paired report: `e2e_paired_report.json`

冻结 Planner artifact：

- root: `/tmp/ns-stage2m-repair-focused-p002-projectworld-20260809-v3`
- final Planner artifact hash:
  `sha256:7f19f4cf489ea2a3aa449e42855235c70207a55448e82872a7811c9d4a5e210e`

旧 Phase 4 传输/重放诊断：

- root: `/tmp/ns-stage2m-phase4-apc-20260807`

本文引用 `/tmp` 产物作为本机现存诊断证据；正式验收不能依赖其永久存在，后续有效运行仍应把必要
摘要、hash 和 checkpoint-scoped artifact 写入正式 evidence owner。

## 3. P002/C40 串行运行事实

### 3.1 时间与 scheduler

| 项目 | 实际结果 |
|---|---:|
| Support 首个 audit 时间 | `2026-08-09T02:08:37Z` |
| Support terminal 时间 | `2026-08-09T04:20:19Z` |
| Support wall time | 约 `7,901.7s` / `131.7min` |
| Evaluator 7 次串行调用 | 约 `113.2s` |
| scheduler acquired/released | `81 / 81` |
| scheduling timeout | `0` |
| scheduler 累计等待 | 约 `0.001s` |
| peak in-flight request | `1` |
| peak effective KV | `31,118` |
| lease leak | `0` |

81 个调度请求由 24 个 single proposal、50 个 multi proposal 和 7 个 evaluation 组成。等待时间相对
131.7 分钟 Support wall time 可以忽略，因此本次耗时不能归因于 admission queue 或 lease contention。

run log 文件从本地约 09:35 延续到 12:22，早于首个 Support audit 的约 33 分钟目前缺少足够细分
telemetry，本文不臆测其具体归属。它不影响“Support 内部 131.7 分钟主要由模型输出解码解释”的结论。

### 3.2 Claim Support 请求结果

| 项目 | 数量 |
|---|---:|
| proposal 请求总数 | `74` |
| single-slice 请求 | `24` |
| multi-slice 请求 | `50` |
| multi `output_length_truncation` | `43` |
| multi structured-content validation error | `5` |
| multi missing structured content | `1` |
| multi 端点成功但无可用 proposal | `1` |
| 可用 multi proposal | `0` |
| verified multi proposal | `0` |
| verifier 调用/descriptor | `0` |

multi 请求失败率为 100%，其中仅截断就占 86%。这不是轻微质量波动，而是同时破坏正确性、验收
证据和运行成本的主路径失败。

24 个 single 请求的端点调用较短；其中有 9 个成功返回但没有产生可用 claim。由于当前早退路径没有
统一写 proposal progress 终态，`support_progress.json` 只出现 64 个 proposal 事件，不能与 74 个
真实请求闭合。

### 3.3 token 下界与 wall-time 闭合

当前 multi 请求 `max_output_tokens=4096`。仅 43 个明确截断请求已经至少生成：

```text
43 * 4096 = 176,128 completion tokens
```

再加上已被 call ledger 记录的成功 Support 输出，Support completion 至少约为 189,988 tokens；缺失
结构化内容等失败调用的实际 usage 未完整落账，因此这是下界，不是总量上界。

此前同一真实端点的 C1 串行实测生成速度约为 24.2 decode token/s。按此计算：

```text
189,988 / 24.2 = 7,851s = 130.9min
```

该估算与观察到的 Support wall time 131.7 分钟几乎一致。因此本轮耗时可由失败 completion 的模型
解码解释，无需假设数据库、检索、artifact 写入或 scheduler 存在同量级隐藏瓶颈。

`thinking_token_budget=500` 没有阻止请求达到 4096 total output tokens。需要由端点校准确认它只限制
reasoning token，还是当前 endpoint/model 没有按代码假设执行；在此之前不能把该参数视为有效的总输出
保护。

## 4. Need/workset fan-out 放大

冻结 fallback Planner 最终生成 27 个 Needs，其中 20 个得到非空 workset：

- deterministic stages 共解析 14,924 个 exact slices；
- 7,647 个 slice memberships 被保留；
- 7,277 个因预算被丢弃；
- 最终暴露 50 个 semantic chunks。

十个 `plan-history.goal.*.facet.*` Needs 贡献 7,599/7,647，即 99.4% 的保留 slice memberships，
并贡献 40/50 个 multi 请求。它们实际只涉及 1,734 个不同 slice，membership 复用倍率约 4.38 倍；
部分 facet workset 几乎相同，例如 ch44 facet 1 与 facet 2 重叠 750 个 slice，Jaccard 约 0.975。

已证实的放大链为：

```text
Planner fallback 产生多个相近 goal/facet Need
  -> 每个 Need 构造接近预算上限且高度重叠的 workset
  -> 每个大 Need 再拆为 4 个 multi 请求
  -> 未产生 verified facet，现有 early-stop 无法触发
  -> 多数请求生成到 4096 token 后失败
```

十个最大 Need 的 multi prompt 中，thinking prompt 字符数中位数约 49,717，最大约 52,701。前十个
大 Need 的全部 proposal prompt 合计约 1.897M 字符，其中 thinking multi prompt 约 1.721M 字符。

高 evidence overlap 证明存在 fan-out 优化空间，但不自动证明不同 facet 在语义上可合并，也不证明
完整 response 可以按 prompt 缓存。任何 facet merge 必须由现有 Validator 基于兼容语义完成；任何
缓存只允许精确 prompt/config/content hash 命中，不能把 evidence overlap 当作输出等价。

## 5. 损失位置：不是原始检索可用性

P002 原始 deterministic retrieval 报告：

| 指标 | 结果 |
|---|---:|
| `gold_evidence_recall` | `0.827586` |
| `observed_use_coverage` | `1.0` |
| `operational_constraint_coverage` | `1.0` |
| future leakage | `0` |

Need-bound five-segment 报告：

| 指标 | 结果 |
|---|---:|
| plan goal coverage | `0` |
| need recall | `1/9 = 0.111` |
| evidence recall | `0` |
| completion accuracy | `0` |
| planner fallback rate | `1.0` |
| weighted/mandatory | `0 / 0` |

这表明证据原本能够被 raw retrieval 找到，主要损失发生在 Planner fallback/Need binding 以及
Claim Support proposal 转换之后。扩大检索范围或预算不是首要修复；在当前输出策略下，它更可能扩大
prompt 和失败生成成本。

本次冻结 Planner artifact 的两个真实 Planner attempts 均为 `OpenAIChatEndpointError` 且零 usage，
最终状态为 `PLANNER_FALLBACK`。该 artifact 满足 Gate 1 对受控 fallback 的诊断用途，也适合 scheduler
复现，但不适合作为正式 APC 质量基线。正式矩阵前应取得并冻结非 fallback Planner artifact。

另外，manifest 已绑定冻结 artifact hash，但本次 `flow_summary.json` 中
`need_planner_artifact_refs=[]`，five-segment 中 planner artifact ref 为空。Codex 应确认这是报告 wiring
遗漏还是预期分层；若不是预期，必须在正式运行前恢复可解引用 traceability。

## 6. 历史串并行证据的正确解释

### 6.1 Claim Support

旧 P001 实跑中：

- serial Support 约 52.5 分钟；
- concurrent Support 约 16.9 分钟；
- wall time 改善约 3.1 倍；
- concurrent 出现 4 个明确 `SchedulingTimeoutError`，且完整请求集合发生变化。

因此 Support concurrency 对吞吐有价值，但旧 concurrent 结果不能作为 semantic input parity 证据。
并发不能替代先修复输出成功率。

### 6.2 Evaluator

旧实跑中：

- serial 7 次 evaluation 调用约 87.7 秒；
- concurrent evaluation 约 122.5 秒；
- concurrent 输出更长，其中一个 batch 约 2,287 output tokens、104 秒。

这反驳了“所有阶段统一提高并发就会更快”的假设。Evaluator 只占当前 P002 总时间约两分钟，不是
优先瓶颈；在得到独立相反证据前，应保持 evaluator concurrency=1。

## 7. 全量 replay 成本与可复用边界

旧 `/tmp/ns-stage2m-phase4-apc-20260807` 的语义指标已被审计判为无效 Gate 证据，但 accepted terminal
receipts 仍可用于传输耗时诊断：

- 82 个 terminal result（ch0-ch81）；
- accepted final path 约 197 次模型调用；
- 约 2,170,647 total tokens；
- model elapsed 总和约 119.4 分钟；
- 每章 median 约 53.95 秒，p95 约 283.5 秒，最大约 411 秒；
- 23 个 no-op 章节仍产生 48 次调用，耗时约 33.7 分钟。

因此 canonical replay 是真实但次于 Claim Support 的成本来源。现有上位执行设计已经允许在 profile
attestation 一致时复用不可变 canonical commits/snapshots。正式 APC/TIO 与多个 checkpoint 不应为
相同 canonical state 重复 replay；必须使用已有 content-addressed owner 和精确 attestation，不新增
模糊缓存服务，也不得跨 profile 复用语义产物。

## 8. 架构判断

当前证据不支持“整个 Stage 2M 架构都过重”的结论：D9、权限/leakage 边界、lineage、冻结重放和
Gate 并没有消耗本次两小时的 GPU 解码时间，而且它们仍是正式实验可信性的必要条件。

当前证据支持更窄且更强的判断：

> Claim Support 的 multi 输出预算/思考策略与 fallback Need fan-out 组合不满足最低充分工程。
> 它花费约 132 分钟却产生零个可验证 multi proposal，既是正确性失败，也是性能失败。

修复目标不是削弱证据强度，而是用更小、更可靠的模型输出完成同一既定 typed contract。

## 9. 需要 Codex 先决定的上位约束冲突

`.agent/review.md` 当前要求 bounded parity 不得缩减 context、evidence、chunk 或 output budget；
`.agent/concurrent_scheduling_plan.md` 也把语义输入/预算变化排除在 scheduler repair 外。

但本次实际日志证明，当前 4096 output budget 与 thinking 行为是首要失败和耗时来源。因而：

1. OpenCode 不能在现有 narrow scheduler repair 中自行降低 output cap 或关闭 thinking；
2. Codex 必须先判断“更紧的结构化输出策略”是否属于保持同一 completion contract 的 transport 修复，
   或者属于需要修改 `.agent/need_pipeline_audit_and_semantics.md` / `.agent/plan.md` / `.agent/review.md`
   的语义预算变更；
3. 在该判断写回权威文档之前，不应启动下一次完整 P002 parity run。

建议的架构原则是：completion contract、evidence 输入和可表达语义保持不变；总输出上限只保留生成
该 typed JSON 所需的实测余量。不能把“允许更多无效 reasoning/尾部文本”误当作语义能力。

## 10. 建议修复顺序

### P0：暂停无效的大运行

- 不运行 formal APC/TIO P001-P005；
- 不直接运行完整 concurrent P002；
- 保留本次 serial 和旧 4-timeout concurrent 产物，禁止覆盖或重分类。

### P1：用冻结单 chunk 做最小真实端点校准

从本次 P002 选择一个已截断的大 multi chunk，保持 Need、evidence、workset、prompt semantic content、
模型和输入 hash 不变，做少量有界校准：

1. current thinking + 4096 作为 baseline；
2. 验证关闭 thinking 是否能稳定输出 typed JSON；
3. 测量合法 `MultiSliceProposalBatch` 或明确 insufficient 所需的最小充分 total output cap；
4. 运行至少一个合法 proposal 到 verifier 的完整链；
5. 不增加 max output，不启用已在历史中出现挂起风险的 strict grammar/schema generation；继续使用
   现有 `json_object + Pydantic` fail-closed 校验。

本步骤不是调参竞赛。找到一个稳定产生紧凑 typed result 的配置后即停止，不建立动态输出控制器。

### P2：先补最小失败 telemetry

复用现有 model-call ledger、progress event 和 content-addressed artifact：

- error/length/validation 路径记录开始时间、结束时间、latency、finish reason 和可获得的 usage；
- 保存 invalid raw output，避免在 Pydantic ValidationError 前丢失诊断主体；
- 为 insufficient/no-claim 写明确 proposal terminal event；
- 使 proposal request、progress terminal、scheduler descriptor 和 model outcome 数量可以对账。

不要增加独立 telemetry 服务或第二套报告。

### P3：取得成功的 Planner artifact

- 让 P002 Planner 真实调用至少成功一次；
- 冻结并验证非 fallback artifact；
- model-disabled replay 必须得到相同 Need/completion/query；
- 修复 manifest、Need lineage、flow/five-segment artifact ref 的可解引用一致性；
- 只有成功 Planner topology 才用于正式 APC 质量证据。

### P4：仅在 P1-P3 后仍过慢时处理 fan-out

使用 P001/P002 development data 离线检查：

- 由现有 Validator 合并或去重同一 goal 下语义兼容、workset 高重叠的 facet；
- 对更小 workset/serialized request budget 做 recall 与 candidate-pollution 对照；
- 不合并语义上不可约的 completion facets；
- 不以 P004/P005 调 prompt、预算或 threshold；
- 一旦 proposal 成功，优先依赖现有 verified-facet early-stop，避免新建 orchestration 机制。

facet topology、workset budget 或 query corridor 的改变属于上位语义设计变更，必须先更新权威文档并
给出回归/Gate 接受证据，不能伪装为 scheduler tuning。

### P5：再做阶段化并发验证

- Support 从小的 bounded concurrency 开始，保持 endpoint-global request/KV 约束；
- Evaluator 暂时为 1；后续若确需优化，只在 P001/P002 单独测试 batch size 2 到 4；
- serial 必须先形成至少一条 proposal -> verification；
- concurrent 必须运行与 serial 相同的完整 request set，而不是只比较共同 admitted 子集；
- request descriptors、prompt/context hash、workset、evidence order、budget 和持久化顺序保持 parity；
- 无 scheduling timeout、OOM、context reduction 或 lease leak。

### P6：复用不可变 canonical state 后再正式全量运行

当且仅当 P1-P5 通过后：

- 形成 clean executable-source fingerprint；
- 使用新的 DB、output root、experiment ID；
- 精确复用 profile-attested immutable canonical commits/snapshots；
- 运行 APC P001-P005 与定义好的 TIO ablation；
- checkpoint report 独立归档，禁止像旧运行一样顺序覆盖顶层 JSON。

## 11. 下一轮接受与停止条件

### 11.1 最小修复接受条件

1. 冻结 P002 大 chunk 在真实端点不再因 output length 截断；
2. 产生合法紧凑 proposal 或 typed insufficient，invalid output 不被静默丢失；
3. 至少一条 proposal -> verifier -> persisted receipt 完整闭合；
4. 74 类请求能够在 descriptor/progress/model outcome 中数量对账；
5. completion contract、evidence 身份、D9、future leakage 和 typed failure semantics 不变；
6. 没有引入新的服务、状态机、动态调参平台或报告体系。

### 11.2 bounded serial/concurrent 接受条件

1. serial 与 concurrent 完整 request set 和所有 semantic input hash 一致；
2. 两者至少各有一条真实 proposal -> verification；
3. 无 scheduling timeout、OOM、context reduction、lease leak；
4. concurrent 的 wall-time 改善来自重叠有效调用，而不是丢失请求或提前失败；
5. Support 与 Evaluator 分别报告吞吐，不用一个全局并发结论覆盖阶段差异。

### 11.3 立即停止并返回 Codex 的条件

- 紧凑输出必须改变 completion contract 才能成功；
- 关闭 thinking 导致必要的跨 slice 合成能力实质下降；
- 必须改变 frozen input、Gold、P004/P005 或 Gate threshold；
- 必须新增平行框架或跨 Stage 能力；
- 非 fallback Planner 仍无法获得，或 artifact lineage 无法闭合；
- serial 仍没有 proposal -> verification，却准备继续并发或正式全量运行。

## 12. Codex 下一步需要形成的明确决定

Codex 应基于本文先更新现有 `.agent/plan.md` / `.agent/review.md`，而不是创建第二套执行体系，并明确：

1. 是否授权 Claim Support 进行关闭 thinking / 收紧 total output cap 的有界校准；
2. 哪些字段属于必须 parity 的 semantic budget，哪些仅是 transport generation guard；
3. 校准通过后采用的固定配置和版本/hash 身份；
4. Planner 非 fallback 成功、proposal -> verification 和 telemetry 对账的准入顺序；
5. bounded concurrent 通过前继续禁止正式 Phase 4。

在这些决定写入权威执行文档前，本文状态保持 `READY_FOR_ARCHITECTURAL_DECISION`。
