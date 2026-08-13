# Stage 2～5 真实小说纵向闭环与可用性 Pilot 执行文档

> 文档生命周期：`ACTIVE`
>
> 设计状态：`PRODUCTION_CONTRACT_BASELINE_2026-08-13`
>
> 当前执行状态：`ENGINEERING_READY_FOR_REAL_PILOT / REAL_PILOT_NOT_RUN`
>
> 本文职责：定义面向真实长篇写作的 Stage 2～5 纵向产品闭环、最近正文输入合同、当前代码断点、
> 修复顺序和一组宽松但真实的小说可用性 Pilot。
>
> 本文不替代各 Stage 总设计；`20→5 / 60→5 / 100→10` 只是首轮测试实例，不是产品硬编码。

## 1. 结论

目标产品不是“给一份人工 target intent，单独测一次 Memory”，而是让同一本书持续执行：

```text
作者初始简报 / 世界设定 / 长期粗纲 / 风格偏好
→ Genesis 五 Root
→ 已有正文逐章进入 TextRoot + Curator/World/Memory
→ Stage 4 根据长期粗纲、当前 Canon 和历史规划一个可配置滚动窗口
→ 接受并提交近期 Plan
→ 对窗口中的下一章构造 WritingTask
→ Stage 2M 按 accepted Plan 检索 Writer Memory
→ 最近正文连续性上下文
→ Stage 3 Writer / Editor / Observer / reconciliation
→ 接受候选
→ 正文与正式 Curator Memory write 原子结算
→ Projection / Freshness
→ 下一章，或在 horizon 不足、偏差出现时回到 Stage 4
```

首轮 Pilot 只需要证明这套机制能生成大体合理、连续、基本完成规划目标的多章正文。它不要求复刻完成稿，
也不以逐句相似或精确情节重合为成功条件。但运行成功、未来隔离、accepted Plan 使用、上一章承接、
逐章 Canon/Memory 前进和无严重连续性冲突不能放宽。

## 2. 生产闭环，而不是 Benchmark 特例

生产入口应接受任意项目、任意已提交历史和任意合法滚动窗口：

```text
project / current commit / exact snapshot
author-intent artifacts
current committed chapter
target end chapter
rolling planning policy
recent-prose policy
model/context/retrieval budgets
acceptance policy
```

实现不得读取 `benchmarks/private/ztj_*` 路径、识别特定书名、假设固定 checkpoint，或把未来完成稿当作
Planner/Writer 输入。测试 runner 只负责把一个 benchmark case 适配成上述通用入口，并在生成冻结后把
完成稿未来章节交给独立 evaluator。

滚动窗口也不写死为三章。生产默认可由 ProjectProfile 选择 1～3 章；显式创作运行或 Pilot 可以要求
5～10 章。Stage 4 仍用同一个 `CHAPTER_SET` 合同，不能为 Pilot 增加第二种 Planner。

## 3. Writer 必需的最近正文合同

### 3.1 为什么它不应由检索命中决定

写第 N 章时，第 N-1 章完整写了什么、场面停在哪里、最后一次动作和对白是什么，是确定性的叙事接缝，
不是“可能相关”的历史知识。它不能依赖 Need 生成、RAG 排名或 Writer 临时提问才出现。

因此最近正文与 `WriterContextPackage` 分责：

- `WriterContextPackage` 继续拥有按任务检索的事实、状态、关系、因果、披露、义务和远程 callback；
- `RecentProseContext` 由当前 accepted `TextRoot` 机械投影，拥有不可缺省的近端叙事接缝；
- 两者都放入同一个 `WritingLoopRequest`，共同形成 Writer 的初始 `AgentContextView`；
- 这不是外部 Hook。Writer 不获得任意文件读取或底层检索 Tool。

### 3.2 通用默认策略

第一版生产默认值：

```text
target = N
full previous chapter = N-1 完整正文，mandatory
recent compact trail = N-2、N-3 的摘要或章尾，optional
expandable refs = 策略允许的更早近章原文 ArtifactRef
```

ProjectProfile 可以调整 compact trail 数量和展开预算；“上一章完整正文”在 N > 1 时仍为默认不变量。
第 1 章没有上一章，使用明确的空历史状态。

完整正文和可展开正文均来自当前 commit 的 `TextRoot`，按章节顺序拼接 scene/block 文本。Stage 5 只建立
typed refs；Stage 3 在生成 work plan 前读取默认可见部分并放入 `ContextLayer.MEMORY`：

- 上一章完整正文标记 `mandatory=true`，不得被普通 compaction 丢弃；
- N-2/N-3 compact trail 可在压力下丢弃或压缩；
- 展开动作由外层 Context/Memory owner 把允许的 ref 转成 `ContextDelta`，不是模型直接读对象库；
- provider hard limit 无法同时容纳最小任务、上一章和输出 reserve 时明确 `CONTEXT_LIMIT`，不静默截断
  上一章后冒充完整输入。

### 3.3 与 Plan/Profile 的同一问题

Writer 初始 Context 中的 accepted Plan 和 ProjectProfile 必须是可读内容，不只是 artifact id/revision。
生产投影至少包含当前章目标、当前 rolling window、active obligations、POV/披露和实际 style profile。
可以从绑定 artifact 做目标相关投影，但不能只把 `AcceptedPlanBinding.model_dump_json()` 或
`project_profile_revision` 交给模型。

## 4. 当前代码已经闭合的部分

以下结论来自 2026-08-13 当前工作树代码，而不是只读文档状态：

- Stage 2 teacher-forced 路径已经能把揭示章节和 Curator World 变化通过
  `MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC` 一起提交，并在每章后刷新 Projection/Freshness；
- Stage 2M 已有 plan-conditioned Need、Memory Gateway、evidence-first
  `WriterContextPackageV2 + EvidenceLedgerV2`；
- Stage 3 已有 `WriterContextLoopService`、WriterWorkPlan、一次 reactive Memory、动态 Context、Editor、
  repair/rewrite、最终 Observation 和 reconciliation；
- Stage 4 已有七模式 Planning Context Loop、独立 Reviewer、Planner Memory 和 `CHAPTER_SET` horizon；
- Stage 5 已有 Task/Attempt、acceptance、PlanRoot/TextRoot materializer、Commit、Projection/Freshness、
  Stage 3/4 public adapters 和同书单写者约束；
- Stage 5 可以表达 Writer 与 lookahead Planner/只读维护两路候选并发，endpoint admission 仍独立限流；
- 当前工作树已有生产 Writing request factory、Stage 4 invocation factory、Stage 2W atomic Chapter
  Settlement 接线和真正执行 dispatcher 的通用纵向 runner。

这些代码证明各段主体存在，但尚不能证明真实小说纵向闭环已经可运行。

### 4.1 2026-08-13 本轮实现检查点

当前工作树已完成并做过 focused offline 验证：

- `RecentProseContext` 与 `RecentProseAssembler`：第 N-1 章全文 mandatory，N-2/N-3 章尾 optional；
- `WritingLoopRequest` 同时绑定 WCP、RecentProse、Plan、Profile 和同一 commit/snapshot；
- Writer Context 实际读取 accepted PlanRoot 与 ProjectProfile 内容，不再只见 id/revision；
- Stage 3 可正式消费 `WriterContextPackageV2 + EvidenceLedgerV2`，按 Need/用途投影去重后的 exact raw
  evidence；旧 v1 仅保留给现有比较/回归夹具；
- `CreativeRunRequest.current_chapter` 与绝对 `target_chapters` 已进入 initial task，初始 horizon 为
  `current+1..target`；
- 离线 focused checks 已覆盖 recent-prose projection、20→25 起跑、v2 ledger handoff，以及一次
  Stage 3 real loop 与一次 Stage 3→5 Draft chain；未调用 8002。

本轮继续关闭了 G4、G5、G7、G8 的工程实现。focused offline evidence 为：生产 factory 2 项、
Text+World 原子结算 1 项、Stage 5 fenced external commit 1 项、通用 runner 2 项及 CLI 13 项，共 19 项
直接验收通过；相关 source 的 Ruff 与 strict MyPy 通过。未调用 8002，也未重跑全仓或 Stage 2 benchmark。

因此当前可以运行真实纵向 Pilot，但尚未产生任何真实模型多章质量结论。部署/Pilot 必须用
`ProductionRuntimeAssembly` 注入当前环境的 Stage 2M retrieval provider、Stage 3/4/Curator model services
和数据库/对象库；仓库不把 benchmark frozen inputs 或 scripted leaf 冒充 production composition root。

### 4.2 2026-08-13 长期自主运行成熟度审计与预算裁决

本轮继续沿当前代码、本文、Stage 3/4/5 详细执行文档、ADR-0006/0007/0008，以及公共研究目录
`/home/cuihengjia/agent-source-research` 的固定源码检查长期运行语义。参考基线为 OpenCode
`d92d1e6`、OpenClaw `8fdf757`、OpenHands Software Agent SDK `be6cd3b`、Hermes Agent
`326bdfb`、PydanticAI `fc6a3ac` 和 PydanticAI Harness `5e18085`。它们不是新的产品依赖；这里只吸收
已能解决当前 caller 的状态与调度不变量。

审计结论不是“把所有上限调大”。生产系统必须区分四类控制：

| 类型 | 当前例子 | 达到边界后的正确动作 | 是否允许自动放宽 |
|---|---|---|---|
| 物理/一致性硬边界 | provider context window、output reserve、basis/snapshot、权限、future isolation、单书 Canon writer | 先压缩/重建；仍不合法则 typed wait/review，绝不截断或越权 | 否 |
| dispatch slice | 每次 dispatcher 最多启动多少 task、可选 wall-clock slice | 持久状态不变，返回 `YIELDED`，同一 run 可自动进入下一 slice | 是；它不是失败预算 |
| task-local retry tranche | 某一个 Task 对 provider/projection 等失败的可重试次数 | 进入 `BUDGET_REVIEW`，保留 checkpoint；可显式补充 tranche、replan 或取消 | 是；不得让 READY task 带 0 额度 |
| Planner Memory tranche | Stage 4 一次 inquiry/reviewer Memory 的 rounds、tool calls、time/token 工作额度 | 进入 `BUDGET_REVIEW`，由操作员显式增加 Planner Memory tranche 后从 checkpoint 续跑 | 是；不能把 retry 次数假装成检索额度 |
| semantic/no-progress guard | 重复相同 Need、无新增 evidence、同一 review issue、无法安全压缩 | review/human/stuck；不能靠增加次数制造无限循环 | 仅在输入、证据或策略真正变化后 |

OpenCode/OpenClaw 的共同做法是：完整历史仍持久保存，接近真实 context hard limit 时先建立 durable
compaction checkpoint，再续接原逻辑 turn；overflow recovery 有小的 no-progress 上限，避免重复副作用。
OpenHands 把 iteration/cost 约束放在一次 run 上，并保留 conversation 供后续恢复；Hermes 把 Task 与
Attempt、failure owner 和 block cause 分开。NS 采用同样的责任分离，但继续使用现有 RunEvent、Artifact、
Task/Attempt 和五 Root，不新增 Conversation DB、通用 DAG 或第二预算平台。

面向小说产品，本轮冻结以下运行状态语义：

```text
ACTIVE
  ├─ YIELDED             # 本次调度切片让出，可自动续跑
  ├─ WAITING_CAPACITY    # endpoint/定时容量等待，不消耗失败额度
  ├─ WAITING_RETRY       # 已知安全的 transient retry
  ├─ WAITING_INPUT       # Plan/Draft 接受或作者选择
  ├─ BUDGET_REVIEW       # 当前 Task retry tranche 用完，可补充/重规划/取消
  ├─ RECOVERY_PENDING    # 先对账不确定 effect
  ├─ BLOCKED             # basis/权限/验证等前置条件必须改变
  ├─ COMPLETED
  └─ CANCELLED
```

`YIELDED/WAITING_* / BUDGET_REVIEW/RECOVERY_PENDING/BLOCKED` 都保留同一 run lineage 和明确的下一合法
动作；只有 `COMPLETED/CANCELLED` 是不可自动继续的产品终态。`BLOCKED` 也不是删除任务，而是要求新的
事实、权限、作者决定或修复证据。

### 4.3 本轮确认的真实闭环缺口与最小修复边界

以下问题能由当前支持的多章/两路运行实际触发，必须先于真实 Pilot 修复：

1. **Context stream 错误地按 run 全局唯一**：Stage 3/4 的 checkpoint/restore 只按 `run_id`，Context
   event identity 又没有 `task_id`。同一 Stage 5 run 的第二个 Planner 或 Writer 可能读取前一个 Agent
   的 View，或因全局 RunEvent 中存在别的 task event 而无法 replay。修复 owner 是 ADR-0007 的共享
   Context Runtime，stream identity 固定为 `(run_id, task_id, consumer)`；其他 task event只推进全局
   event position，不进入当前 Agent View。
2. **滚动规划在 runtime 中没有真正滚动**：初始 Task 当前把 horizon 写成 `C+1..T`，与 Stage 4
   `CHAPTER_SET` 的近程窗口设计冲突。生产 policy 增加 `planning_horizon`，初始只规划
   `C+1..min(C+H,T)`；accepted horizon 消耗完后，Stage 5 在最新 exact commit/snapshot 上创建下一普通
   Planning task，再继续 Writer。测试的 5/10 章窗口只是配置值。
3. **retry budget 被整条后继链继承**：前面 Task 一次失败会减少以后所有章节的额度；0 额度重试还会
   产生不可 claim 的 READY task，并使双路 dispatcher 零进展自旋。每个新业务 Task 必须从 pinned
   policy 得到自己的 retry tranche；最后一次失败进入 `BUDGET_REVIEW`，由显式 budget extension 恢复。
4. **纵向 runner 把 slice 当失败**：`max_tasks` 实际是一次 poll slice，却报告
   `TASK_BUDGET_EXHAUSTED`，脚本再以 exit 2 结束。生产 runner 应在 slice 间自动继续；只有显式的
   slice/deadline checkpoint 才返回 `YIELDED`，waiting/yield 都不是进程失败。
5. **Stage 3 evidence-first 渐进展开被绕过**：正式 Writer 当前读取整个 Ledger，把 WCP v2 已做的
   `raw_preview` 预算重新放大；typed gap 又没有被渲染给模型。初始 View 应消费 purpose + bounded exact
   preview + typed unresolved Need，完整 Ledger 继续作为可追溯/按需 Memory handle，而非默认全文。
6. **失败 checkpoint 没有进入下一 Attempt**：Stage 4 非 READY 结果的 artifact refs 当前未落到 Task，
   production invocation 也没有恢复 ref。先让失败 settlement 保留 checkpoint lineage；真正跨 phase
   resume 由同一 Stage 4 loop 消费，不能由 Stage 5 猜 Planner 私有状态。
   `extend_budget` 必须区分 `additional_attempts` 与 `additional_planner_memory_tranches`：前者只恢复
   Task retry eligibility，后者才按 Stage 4 pinned policy 增加本 Task 的 retrieval work allowance；两者都不
   改 provider context、basis、权限、future isolation 或无进展 guard。
7. **异常与后继创建存在 crash window**：leaf settle→acceptance、acceptance→commit、commit→projection、
   projection→下一 foreground task 必须由现有 Stage 5 command owner 在各自单个数据库事务中完成。
   pinned AUTO policy 下，进程恢复还必须扫描遗留的 `WAITING_INPUT` acceptance 并重放同一个稳定 command，
   不能要求人工再提交一次。Stage 2W Chapter Settlement 是独立事务中的真实外部副作用，Stage 5 必须在
   调用前登记 Attempt-scoped outer effect，并以 acceptance-scoped Stage 2W idempotency key 表示稳定业务请求；
   恢复时只按 Stage 2W 的既有 idempotency receipt 对账。若 receipt 证明 Canon
   已提交，则在一个 Stage 5 reducer 中完成 effect terminal、原 Attempt settlement 与 Projection successor；
   若权威结果未知，保持 `RECOVERY_PENDING`，不得盲目重放，也不得把旧 basis task 重新 READY。上述修复
   只修改 Stage 5 caller、共享 Commit receipt read API 和 production adapter，不修改 Stage 2 workflow。

8. **Planner 的选择目标与 provider 硬窗仍被混用**：Stage 2 `ContextBudget` 继续只拥有一次 Memory
   package 的选择预算；Stage 4 `PlanningBudgets.planner_context_target_tokens` 单独表达 Planner View 的
   软选择目标。protected/mandatory 内容超过软目标时不能在 assembler 里冒充物理失败，而要报告
   `soft_overflow_tokens`，再交给共享 Context Runtime 按真实 tokenizer、output reserve 和 provider hard
   window 压缩或 suspension。为避免长篇 PlanRoot 本身持续膨胀，accepted Plan 只投影当前 rolling
   horizon 的 goals、相关 obligation nodes/祖先与顶层方向；原 Root ref 仍保留。ProjectProfile 的实际
   style/capability/model 内容也必须进入 protected Planner Context，而不只是保存一个 ref。

9. **lookahead 不能成为 foreground 的唯一后继**：Draft Freshness 完成后，若匹配的 lookahead acceptance
   可在最新 exact basis 上晋升或 replan，则沿用该结果；若 lookahead 仍在安全执行中则显式等待；若它已
   `FAILED/CANCELLED/BLOCKED/BUDGET_REVIEW` 或根本不存在，则创建普通 rolling Planner task。后台候选失败
   不得让小说主链永久停在没有 READY task 的 `lookahead_pending`。

### 4.4 本轮实现状态与仍需真实 Pilot 证明的边界

截至 2026-08-13，本轮实现目标分为两类：

- 已进入代码的成熟化：task/consumer Context stream、Writer 最近正文默认输入、Stage 3/4 slice yield 与
  checkpoint continuation、Planner rolling horizon/软 Context target、独立 retry/Planner Memory tranche、
  `BUDGET_REVIEW`、纵向 runner 的 dispatch slice，以及 G4/G5/G7/G8 production wiring；
- 本轮 P0 收尾：Stage 5 各固定拓扑后继的事务内创建、AUTO acceptance 恢复、Stage 2W outer-effect 对账、
  lookahead dead-end fallback，以及 Writer reactive phase 的最小跨 invocation resume。

这些机制完成 focused offline evidence 后，只能说明“可恢复纵向 Pilot 入口成立”，不能代替真实小说质量
结论。下一阶段仍按本文 §8 的 20→5、60→5、100→10 场景，以真实长纲/设定、真实历史章节和真实模型验证
情节连贯、章节目标实现、Memory 命中与长程稳定性。Stage 4 真正由 Planner turn 主动提出多轮
`REQUEST_MEMORY`、超长 author artifact 的进一步渐进投影、多进程 worker lease，以及跨项目 4/6/8 endpoint
admission，继续作为有真实 caller/容量证据后再扩展的工作；它们不应阻塞本轮单项目、最多两路 Pilot。

Stage 4 还有三个已定位但未伪装成“完成”的成熟度项：

1. `PlanningTurnOutput.REQUEST_MEMORY` 目前只有合同，没有进入 `PlannerAgent.run()` 的生产循环；当前可恢复
   Memory round 由 inquiry/reviewer 驱动。首轮 Pilot 可用，但 Planner 还不能在看到新 Context 后自主提出下一
   tranche；
2. `PlanningBudgets.model_token_budget` 当前只约束 invocation factory 的单次 `max_output_tokens`，没有把
   `ModelCallRecord.usage` 累加进 checkpoint 后按 slice yield。provider hard window 仍正确受共享 Context
   Runtime 保护，但 Stage 4 软 token 工作额度尚未形成真正累计治理；
3. `PlanningInquiryConditionedNeedGenerator(max_total_needs=24)` 逐问题调用 validator，导致全局 cap 和跨问题
   dedupe 被重置。正确修复应返回“当前 Need tranche + deferred reviewed questions”，再从同一 checkpoint
   继续；不能简单截掉第 25 项。本轮实际 Chapter-set inquiry 远低于 24，故记录为下一 Stage 4 修复而不扩大
   当前 Pilot 实现。

9. **Writer reactive Memory 配额原本是一次性终止线**：`max_reactive_memory_rounds` 和
   `max_writer_turns` 必须解释为一次 dispatch slice，不是整个 Draft Task 的生命期上限。本轮的最小
   恢复边界只是 `REACTIVE_MEMORY_PENDING`：Writer 已经输出 typed `REQUEST_MEMORY`，但该 Need 尚未被
   Memory 解析并返回下一轮 Writer。`WritingLoopCheckpoint` 持久化 work plan、当前
   `WriterTurnOutput` 及其 artifact/model-call lineage、累计 Memory/Writer 轮次、已见 Memory
   fingerprint 和当前 exact `AgentContextView`。该 checkpoint 的 caller 是 Stage 5 Draft attempt，owner 是
   Stage 3 Writing loop；生产 `WritingRequestFactory` 只从同一 Task 的 terminal artifact refs 选择最新
   checkpoint。单次 slice 用尽时返回 `YIELDED`，Stage 5 将 Task 结算为 `READY`，保留 artifact
   refs 且不消耗 retry；下次 attempt 跳过已完成的 WorkPlan 和当前 Writer turn，从待处理
   Memory Need 继续。Memory Gateway 明确返回的 `BUDGET_EXHAUSTED` 仍是需要独立扩容决策的
   budget review，不能冒充 dispatch yield；但它同样保留当前 pending Need 的 checkpoint，
   扩容后不重跑 WorkPlan/Writer turn。重复 fingerprint/无新证据仍终止为明确的
   insufficient/denied，不得无限续发。本轮不泛化 Editor、repair 或 Observer phase resume。

   最小验收证据是两次 invocation：第一次在有进展的第二个 Memory Need 前 yield；
   第二次从 checkpoint 继续并产出 draft-ready Writer turn，期间 WorkPlan 只调用一次，Stage 5
   的 retry budget 不变。

本轮不把 `runtime_parallelism=2` 简单改成 8。当前同一本书只有 Writer foreground 与一个
candidate-only lookahead/只读维护 lane 能证明独立，`2` 是已实现拓扑的 admission ceiling，不是总预算。
跨项目 4/6/8 并发应由 dispatcher candidate window、project lane 和 endpoint-global KV/request admission
共同决定；必须先有多项目 caller 和容量证据。单书 Canon Commit、Projection/Freshness 和 recovery 继续串行。

## 5. 当前未闭环事实与修复方向

### G1（已关闭核心合同）：最近正文原本没有进入正式 WritingLoopRequest

**状态：`IMPLEMENTED_OFFLINE_VERIFIED`**

**修复前事实**：Stage 3 的 `recent_settled_tail` 是当前 Agent 运行内最近 settled batch，不是小说最近章节；
`CONTINUE` 的 `prior_draft` 也是当前草稿，不是前一已提交章节。

**当前实现**：已新增通用 `RecentProseContext` 及 TextRoot assembler，并按 §3 投影到 Writer Context。
Stage 3 三臂实验中的 `recent_prose` 保持评价基线，不再冒充生产最近正文接线。

### G2（已关闭生产 handoff）：Stage 2M 正式 v2 与 Stage 3 请求原本不一致

**状态：`PRODUCTION_HANDOFF_IMPLEMENTED / LEGACY_FIXTURES_RETAINED`**

**修复前事实**：`EvidenceFirstWriterContextAssembler` 输出 `WriterContextPackageV2`；原
`WritingLoopRequest.writer_context_package` 和 `AgentContextProjector.seed_writer()` 仍接
claim-first `WriterContextPackage` 并只投影 `item.claim`。

**当前实现**：正式 Stage 3 请求可消费 evidence-first v2。Writer 初始 Memory 使用 Need/purpose 加去重后的
exact raw ledger evidence，保留 package/ledger lineage；旧 v1 仅兼容明确的 Stage 3 比较 fixture，不作为
后续生产 factory 的允许输出。没有创建第三个 WCP。

### G3（已关闭核心合同）：accepted Plan 和 ProjectProfile 原本只有绑定，没有有效正文

**状态：`IMPLEMENTED_OFFLINE_VERIFIED`**

**修复前事实**：Writer loop 把 accepted Plan binding JSON 和 profile revision 字符串放入 protected Context，
没有读取绑定的 PlanRoot/Profile artifact。

**当前实现**：Writer seed 已从绑定 artifact 读取当前章附近 goals、active obligations 相关 plan nodes，以及
实际 style/capability/model profile；无法解析正式 Root 的隔离 fixture 保留原始 JSON fallback。

### G4：生产级 Stage 5→Stage 2M→Stage 3 Writing request factory

**状态：`IMPLEMENTED_OFFLINE_VERIFIED`**

**代码事实**：`CreativeRuntimeService` 依赖外部 `writing_request_factory`。当前集成测试手工制造
WritingTask、Memory Need、WCP 和 attestation；正式 CLI 则使用确定性假 Writer。

**当前实现**：`ProductionWritingRequestFactory` 从 exact commit 读取 PlanRoot/TextRoot/World/Profile，验证
TextRoot 最后一章与 Draft task 连续，为唯一当前章 goal 构造 WritingTask，装配 RecentProseContext，调用
注入的正式 Stage 2M evidence-first provider，持久化 v2 WCP/EvidenceLedger，并生成同
basis/snapshot 的 `WritingLoopRequest`。缺 goal、WCP 非 READY、ledger 不匹配或 Canon 已变化时 fail closed。

### G5：Stage 4 生产 invocation 与 runtime assembly

**状态：`IMPLEMENTED_OFFLINE_VERIFIED`**

**代码事实**：`Stage4PlanningLeafAdapter` 已存在；当前 Stage 5 “real writer” integration test 中 Stage 4 loop
是 `_MaterializableStage4Loop` 合成结果，CLI `runtime advance` 使用 `StrictFakePlanningLeaf`。

**当前实现**：`ProductionStage4InvocationFactory` 把 author-intent refs、current roots、exact snapshot、
`CHAPTER_SET` horizon 和 pinned model policy 装配成真实 `PlanningContextLoopService` 请求；20→25 factory
evidence 已确认 horizon 为 21～25。CLI 与纵向 runner 共用 `module.path:callable` production composition
入口。`ProductionRuntimeAssembly` 同时核对实际 runtime/adapter/factory/materializer/settlement 的对象身份，
fixture 或“外表真实、内部 lambda”的 assembly 不能通过 production admission。

### G6（核心 runtime 已关闭）：Stage 5 原本不能从已有第 C 章自然开始

**状态：`IMPLEMENTED_OFFLINE_VERIFIED`**

**修复前事实**：`CreativeRunRequest` 没有 current chapter；initial Plan task 的 `chapter_index=0`，Plan commit 后
总是创建 Draft 1。`target_chapters` 在运行中实际按绝对终章比较。

**当前实现**：请求已显式携带 `current_chapter` 和绝对终章 `target_chapters`，并要求 target > current；
initial task/horizon 与 Plan commit 后的下一章均从 current 正确前进。生产 factory 仍须把
`current_chapter` 与当前 TextRoot 最后一章的一致性现在由 G4/G5 production factory 在 leaf 调用前强制检查；
新项目使用 current=0。

### G7：原子 Chapter Settlement

**状态：`IMPLEMENTED_OFFLINE_VERIFIED / REAL_CURATOR_RUN_PENDING`**

**代码事实**：当前 `DraftCandidateMaterializer` 只追加 `ChapterDocument` 到 TextRoot；Stage 3 Observation 的弱
变化不会升级成 World operation。下一章虽然能看到正文，但 canonical current state、关系和事件仍停在旧值。

**当前实现**：保留现有 `DRAFT_COMMIT` 拓扑，但生产执行 owner 已收敛为异步 Chapter Settlement adapter：

```text
accepted final Draft
→ 构造可信 ChapterDocument / Evidence boundary
→ 复用 Stage 2W LocalMemoryWriteWorkflow + 正式 Curator/Validator/Guardian
→ CHAPTER_REVEAL_ATOMIC 同时 materialize TextRoot 与 World/Memory changes
→ 一个 accepted Commit
→ Projection/Freshness
```

Stage 3 Observation 仍只用于候选对账，不能替代正式 Curator。实现上增加一个由 `DRAFT_COMMIT` 当前 caller
使用的窄 `ChapterSettlementPort`，复用 `LocalMemoryWriteWorkflow(CHAPTER_REVEAL_ATOMIC)`、正式 Curator
可见正文接线和 Stage 2W 自有 Projection/Freshness。Stage 5 只在原 Attempt/writer fence 下登记这个外部
accepted commit，再继续既有 freshness task；没有新增 scheduler、Root 或第二套 Memory workflow。离线集成
证据确认 chapter 21 的 TextRoot 与一个真实 World state 在同一 resulting commit 中同时改变。Stage 5 侧另以
Attempt-scoped outer effect 记录调用；正常返回和崩溃后对账都把 effect terminal、原 Attempt settlement 与
Projection successor 放入同一 reducer。按 acceptance 固定的 Stage 2W idempotency receipt 是唯一恢复事实；
无 accepted receipt 时允许安全 retry，确定性 rejected/conflicted receipt 进入 BLOCKED，不形成无限循环。

### G8：通用纵向 runner

**状态：`IMPLEMENTED_OFFLINE_VERIFIED / REAL_MULTI_CHAPTER_RUN_PENDING`**

**代码事实**：`scripts/run_stage5_runtime_evaluation.py` 只读取并验证一份已有 report；没有创建项目、回放
历史、运行 Planner/Memory/Writer 或比较正文。当前 full-chain tests 是 synthetic/fake model 合同测试。

**当前实现**：`VerticalCreativeRunner` 创建/恢复 run 后真正驱动 bounded dispatcher，直到完成、人工/修复
停点或 task budget；`Stage5VerticalRunReport` 从 durable task truth 汇总已完成章节，只在目标章 Draft
freshness 成功后冻结输出。`scripts/run_stage5_runtime_evaluation.py` 已从“读取旧报告”改为实际运行入口，
CLI `runtime advance` 也必须使用同一 production assembly factory。Runner 不实现或替换任何
Planner/Memory/Writer/Curator。

## 6. 修复与实现顺序

顺序按可执行依赖，而不是另起 Stage：

1. **Writer 输入闭合**：`RecentProseContext`、Plan/Profile 内容投影、Stage 2M v2→Stage 3 handoff；
2. **已有历史起跑**：`current_chapter/target_chapter` 进入 runtime task/horizon；
3. **生产 leaf assembly**：真实 Stage 4 invocation factory 与 Stage 2M→Stage 3 writing request factory；
4. **逐章状态闭合**：`DRAFT_COMMIT` 接 Stage 2W atomic Chapter Settlement；
5. **通用纵向 runner**：先离线/假模型证明拓扑，再在空闲 endpoint 上跑真实模型；
6. **宽松可用性评价**：所有生成冻结后才读 reference future，输出机制判断和诊断。

四项工程修复的唯一 caller、owner、不变量和当前证据如下：

| 项目 | 当前 caller | 负责层/复用 owner | 必须保护的不变量 | 当前最小验收证据 |
|---|---|---|---|---|
| G4 Writing request factory | `CreativeRuntimeService.writing_request_factory` | Stage 5 leaf assembly；复用 Stage 2M evidence-first owner | Text/World/Plan/Profile/WCP/RecentProse 同一 exact basis；只接受 WCP v2 READY | C20 的 Draft 21 task 自动形成完整 request，无手工 WCP/attestation，已通过 |
| G5 Planning invocation factory | `Stage4PlanningLeafAdapter` | Stage 5 leaf assembly；复用现有 `PlanningContextLoopService` | author intent 与当前 Canon 分层；CHAPTER_SET horizon 等于 runtime task | 20→25 task 自动形成 21～25 Stage 4 request，production object identity fail-closed，已通过 |
| G7 Chapter Settlement | Stage 5 `DRAFT_COMMIT` | Stage 2W `LocalMemoryWriteWorkflow` | 同一 accepted commit 原子更新 TextRoot 与 Curator World/Memory；Stage 3 observation 不越权；外部 commit 可对账 | 一章 commit 后 Text 与 World state 在同 commit 可读；正常/遗留 Attempt 均原子产生 Projection successor，已通过 |
| G8 vertical runner | Pilot script/生产 CLI | Stage 5 runtime runner | runner 只编排 production assembly；future 仅 evaluator 冻结后读取 | dispatcher 执行、task budget/重入完成与冻结语义已通过；真实 C20→25 待 8002 空闲 |

中间只运行能改变下一步的 focused tests：合同变更测 request/projection，runtime 变更测从 C 起跑，settlement
变更测一章 Text+World 原子前进。代码全部接通后统一跑一次完整确定性套件和真实 Pilot；不逐小步反复跑全仓。

### 6.1 下一步唯一执行入口

部署/Pilot composition factory 必须返回 `ProductionRuntimeAssembly`，并将当前环境实际的 Stage 2M backend、
Stage 3/4/Curator Model Gateway、PostgreSQL/SQLite runtime repository、Artifact store 和 Projection owner 接入。
然后执行：

```bash
.conda-env/bin/python scripts/run_stage5_runtime_evaluation.py \
  --request <creative-run-request.json> \
  --manifest src/novel_agent/runtime/stage5_development_manifest.json \
  --database-url <runtime-database-url> \
  --object-store-root <isolated-object-root> \
  --assembly-factory <deployment.module:build_production_assembly> \
  --max-tasks <bounded-task-budget> \
  --output <vertical-run-report.json>
```

`CreativeRunRequest` 必须是 AUTO policy 才能无人值守跨 acceptance 停点；MANUAL/SEMI 返回 waiting 是正确
产品行为。首个真实命令只跑 C20→25；若 8002 仍在 Stage 2 benchmark 中使用，保持 deferred，不抢占。

## 7. 通用纵向测试协议

### 7.1 输入

每个 scenario 包含：

- 模拟作者在开写前拥有的长期简报、世界设定、粗纲和风格偏好；
- 截止 C 的真实历史正文，仅用于构造当前 accepted Canon/Memory；
- 目标终章 T，`T > C`；
- evaluator-only 的 C+1..T 完成稿参考正文；
- 固定模型、Prompt/Skill/Profile、预算、采样和 acceptance policy。

长期粗纲可以来自完成稿逆向恢复，但必须始终标记 `simulated_author_input / PLAN_INTENT`。它不能作为已发生
World fact，也不能细到把 evaluator-only 正文逐段泄漏给 Planner。

### 7.2 执行

每个 scenario 使用全新 project/run/output identity：

1. Bootstrap 初始 sources，形成 Genesis；
2. 逐章回放到 C；每章通过正式 Stage 2W atomic write，C 点检查 exact freshness；
3. Stage 4 `CHAPTER_SET` 对 C+1..T 生成每章 goal 和必要跨章义务；
4. 独立 Plan Reviewer 接受后，由 Stage 5 提交 PlanRoot；
5. 对 N=C+1..T 串行：WritingTask → Stage 2M v2 → RecentProse → Stage 3 → acceptance → atomic
   Chapter Settlement → freshness；
6. N 章生成和结算成功后，N+1 必须读取生成的 N，而不是偷偷切回原著 N；
7. 全部输出冻结后，独立 evaluator 才读取参考未来章节。

Planner lookahead/历史维护并发不是首轮可用性 Pilot 的必要条件。基本串行闭环通过后，再以同一 scenario
运行 `runtime_parallelism=2`，确认 Writer N 与 lookahead Planner 或只读维护可重叠且不改变输入语义。

### 7.3 宽松但有效的判断

硬失败只有：流程未完成、未来泄漏、缺失 accepted Plan、跳过 Editor/Curator/Commit/Freshness、下一章未读取
上一生成章、严重 Canon 冲突或状态没有逐章前进。

对创作质量不做逐句复刻评分。独立 evaluator 综合判断：

- 是否大体承接 C 章和每个生成章的结尾；
- Planner 给出的主要章节目标是否多数被实际实现；
- 五/十章作为一段是否形成可理解的推进，而不是互相独立的短篇；
- 人物、位置、关系、能力、知识边界是否没有明显硬冲突；
- 与完成稿未来段落相比，是否在长期粗纲允许的方向内，允许不同桥段、顺序和表达。

最终语义结论只需 `MECHANISM_USEFUL / PARTIAL / NOT_USEFUL` 加具体理由。首轮以“大致合适且能连续跑通”
为目标，不用一个虚假的精细总分掩盖流程或严重连续性问题。

## 8. 首轮真实小说 Pilot 配置

现有 `ztj` 数据可以实例化通用协议：

| scenario | 历史截止 | 生成窗口 | 用途 |
|---|---:|---:|---|
| `ZTJ-VERTICAL-C020-T025` | 20 | 21～25 | 最短闭环、上一章接续、基础 Planner→Memory→Writer |
| `ZTJ-VERTICAL-C060-T065` | 60 | 61～65 | 长程伏笔、婚约/黑龙/人物知识边界 |
| `ZTJ-VERTICAL-C100-T110` | 100 | 101～110 | 更长历史、十章累计漂移和 rolling plan 可用性 |

前两项已有对应历史与未来正文。第三项的原文可从 300 章 stream 做物理隔离后提供给 evaluator，但当前
`rough_story_outline.md` 只覆盖前 100 章；在作者侧补充覆盖 101～110 的粗粒度长期意图前，第三项必须
报告 `MISSING_AUTHOR_OUTLINE_COVERAGE`，不能从隐藏参考正文临时抽出详细章纲再交给 Planner。

首轮资源策略：

- 先只跑 C020→T025；确认产品闭环后再跑 C060→T065；
- 前两项稳定后补合法作者粗纲并跑 C100→T110；
- endpoint 忙时只完成 deterministic/offline assembly tests，不抢占 8002；
- 真实模型运行默认串行，完成后才做一次两路有界并发对照；
- 每个 scenario 保存 Planner proposal/review、每章 WCP/RecentProse/Writer/Editor/Curator、Commit、
  Projection/Freshness 和最终 evaluator 报告。

## 9. 完成定义

只有以下事实同时成立，才可称 Stage 2～5 真实小说机制形成首个可用闭环：

- 通用生产 assembly 可从任意合法 current chapter 起跑；
- Writer 默认看到上一章完整正文和配置允许的近期 trail；
- accepted rolling Plan、Profile 与 Stage 2M v2 evidence 实际进入 Writer；
- 每章 accepted Draft 通过正式 Stage 2W Curator 原子更新 Text/World/Memory；
- 下一章读取刚生成并结算的上一章及同一 exact snapshot；
- 至少一个 5 章真实 scenario 完成并被独立判断为 `MECHANISM_USEFUL` 或有明确局部修复方向的 `PARTIAL`；
- 真实 API 未运行时只报告 `ENGINEERING_READY_FOR_REAL_PILOT`，不能报告产品质量 PASS。
