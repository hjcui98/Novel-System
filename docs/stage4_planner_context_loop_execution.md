# Stage 4 Planner Agent 与 Planning Context Loop 详细执行文档

> 文档生命周期：`ACTIVE`
>
> 执行状态：`ENGINEERING_INTEGRATED / REAL_SEMANTIC_GATE_PENDING`
>
> 更新日期：2026-08-13
>
> 阶段：Stage 4 — Planner Agent and Planning Context Loop
>
> 上位设计：`docs/stage4_planner_core_overall_design.md`
>
> 上位决定：ADR-0006、ADR-0007
>
> 关联技术基线：技术实施与选型设计 §28、agentmemory 调研 §5.4/§7、InkOS 调研 §14、
> Long-running Runtime 调研 §19/§23
>
> 当前 caller：项目初始规划、全书/卷/章节集/章节/场景规划、滚动规划和 Replan 候选运行
>
> responsible layer：Planner application service；复用 Stage 2 Memory/Context/Model owners

> 当前实现证据：`codex/stage4-planner-context-loop`；`2d76c3c` 已形成 Planner Context Loop
> candidate，`88e1027` 已接共享 Context Runtime。全量质量、真实规划评价和独立 Gate 尚待完成。

> Stage 5 production handoff：`ProductionStage4InvocationFactory` 已把 Stage 5 durable task 的 author-intent
> refs、current roots、exact snapshot 和 rolling horizon 投影成正式 `CHAPTER_SET` request；CLI/纵向 runner
> 只经显式 production assembly 调用 `Stage4PlanningLeafAdapter`。20→25 focused evidence 已确认请求覆盖
> 21～25；真实 Planner 语义运行仍待 endpoint 空闲。

## 1. 本阶段的完成定义

Stage 4 是独立 Planner 产品，不是 Writer 的前置 prompt。它把作者意图、当前已接受 Plan/Canon 和历史
记忆转化为可审阅的 Plan candidate：

```text
PlanningLoopRequest
→ PlanningInquiry / GoalProposal / alternatives
→ independent inquiry review
→ accepted inquiry
→ PlanningInquiryConditionedNeedGenerator
→ Stage 2 Memory Gateway
→ PlannerContextPackage seed
→ AgentContextView
→ PlanProposal
→ independent plan review
   ├─ ACCEPT
   ├─ REVISE → one bounded revision → re-review
   └─ HUMAN_REQUIRED
→ PLAN_CANDIDATE_READY | REVIEW_REQUIRED | SUSPENDED | BLOCKED
```

Stage 4 支持：

```text
PROJECT_BOOTSTRAP
STORY
ARC_VOLUME
CHAPTER_SET
CHAPTER
SCENE
REPLAN
```

`PLAN_CANDIDATE_READY` 是最高成功终态。Stage 4 不写 `PlanRoot`、`WorldRoot`、`TextRoot`，不调用
Commit。Stage 5 才负责“显式接受 Plan candidate → PlanRoot Commit → 交给 Writer”。

## 2. 与 Stage 2 验收、Stage 3 开发并行的方式

### 2.1 worktree 和可执行基线

Stage 4 使用独立 clean worktree。开发 manifest 固定：

```text
stage2_base_commit
stage2 Memory Gateway/retrieval/configuration fingerprint
document_baseline_commit
stage4_branch/worktree identity
```

可以在 Stage 2 最终语义验收期间开发，因为 deterministic Memory Gateway、cutoff/access/future safety
已经冻结；但正式 Stage 4 Gate 必须在最终被接受的同一 Stage 2 executable identity 上统一 rebase/merge
并验证。不得从主 worktree 的 dirty diff 建分支。

### 2.2 与 Stage 3 的共享合同

Stage 3 是 `AgentContextView`/`ContextDelta`/compaction projector 的唯一代码 owner。Stage 4：

- 在 Planner 自有模块定义窄 `PlannerContextRuntimePort` 消费接口；
- 开发期间用严格 fixture/fake 实现 Planner 业务流；
- 不创建 Planner 专用 compactor、session store 或第二套 RunEvent store；
- Stage 3 共享合同提交稳定后吸收该提交，删除 fixture adapter 与正式路径之间的重复逻辑；
- 最终 Gate 使用共享真实 projector，不以 fake Context Runtime 签发 PASS。

Stage 4 拥有 Planner/Reviewer/Planner Context/高级读取和规划评价；Stage 3 拥有 Writer/Editor/正文评价。
两边不并行修改同一个实现文件。共同 enum/export 由共享合同提交一次性冻结，后续只受控合并。

### 2.3 连续开发、最后统一测试

本文的开发包是代码实现顺序和所有权划分，不是测试 Gate。进入 Stage 4 开发后连续完成 domain、Agent、
Memory、Context、Reviewer、retrieval adoption、runner 和 report；中间不安排“先跑测试再获准继续”的
停点。所有代码完成并完成自检阅读后，才进入 §11 的统一测试和真实模型验收。

### 2.4 Stage 5 lookahead 使用合同

Stage 5 可以在 Writer 正在实现第 N 章时调用真实 Stage 4 leaf，预先产生后续 horizon 的
`PLAN_CANDIDATE`，但这仍是普通 Stage 4 candidate loop，而不是第二个简化 Planner：

- request 标记 `purpose=LOOKAHEAD`，绑定 Writer 启动前的 exact base commit/snapshot；
- inquiry/goal、Planner-specific Need、PlannerContextPackage、Reviewer 和 bounded revision 全部保留；
- current Writer chapter/scope 是 protected exclusion，lookahead 不得修改其 accepted contract；
- 结果只能持久化 candidate/review/basis，不能在 Writer 运行中写 PlanRoot；
- 当前 Draft Commit/Freshness 后由 Stage 5 做 deterministic affected-scope 检查；受新事实、义务或偏差
  影响时必须返回 Stage 4 revision/replan，不能由 Runtime 改写 candidate；
- stale basis、跨 project/run、future/private leakage 和不完整 lineage 均 fail closed。

Stage 4 仍不拥有 endpoint scheduler。Planner/Reviewer 请求与 Writer/Editor 请求共同进入现有
`ModelRequestAdmissionController`，由 request-count + KV-token capacity 决定单卡实际并发 1 或 2。

## 3. 当前代码事实和缺口

### 3.1 已存在的正确基础

当前 main 已有：

- `domain/stage2.py::PlanningTask`、`PlannerProposalDraft`、`PlanProposal`、
  `PlannerExecutionResult`；
- `ProposalProvenance.AUTHOR_SUPPLIED / PLANNER_PROPOSED`；
- 六模式 `agents/planner.py::PlannerAgent`；
- 六套 Planner Prompt/Skill 和 content-addressed Agent/Skill receipt；
- `PROJECT_BOOTSTRAP` 无 base commit、其他模式必须有 base commit 的校验；
- `NORMALIZE_ONLY` 不允许 Planner 新造内容，`REPLAN` 必须携带 deviation；
- author source binding 和 foreign source rejection；
- `Stage1MemoryNeed`、`NeedDraftGrounder`、`NeedValidator`、`NeedQueryCompiler`；
- `MemoryGateway`、R0/R1/R2、Exact/Temporal、Anchor/Grounded BM25+Dense、Hierarchy、Typed Graph；
- `FusionService` 单一 application RRF owner；
- `AccessScope.AUTHOR_PLANNING`、plan read/cite 分层权限；
- endpoint-global model admission、RunEvent/Artifact/receipt 基础。

### 3.2 还缺少的产品能力

| 缺口 | 当前表现 | 本阶段实现 |
|---|---|---|
| Planner 自主提出目标 | 当前 `PlanningTask` 直接进入一次 Planner call | 先产生 inquiry/goal/alternatives，并独立审核 |
| Planner-specific Need | 现有 `TaskPlanConditionedNeedGenerator` 依赖给定未来规划 | 新增 inquiry-conditioned generator，不复用 Writer Need 目标 |
| Planner Context | 只有 Stage1/Writer Context，没有 Planner package | 新增 consumer-specific `PlannerContextPackage` |
| 动态窗口 | 无 Planner Context View/Delta/compaction | 消费 Stage 3 共享 Context Runtime |
| 独立 Reviewer | 当前无 Plan Reviewer | 新增 inquiry review + plan review，同一 reviewer contract 两类 target |
| 滚动章节集 | 当前六模式，没有 `CHAPTER_SET` | 增加 1～3 章或 Profile 配置窗口 |
| 高级读取收据 | `GraphPath` 已含路径信息，但 `R1RetrievalBackend` 折叠为普通 `ChannelHit` | 持久化 path receipt 并贯穿 trace/context |
| 条件图扩展 | relation/causal 当前注册 graph+anchor 并行 | anchor 先行，仅未闭合 facet 才 graph expand |
| compact→expand | ContextCompiler 已有部分 compact block 行为 | 给 Planner 工具/Context 增加显式 compact handle→selected expand receipt |
| 真实 Gate | 只有六模式 unit tests | 新增七模式真实 planning benchmark、消融和独立评价 |

### 3.3 新增/收敛组件责任矩阵

下表 evidence 只在全部代码完成后按 §11 统一产生，不作为开发中断点。

| 组件 | 当前 caller | responsible layer | 保护的不变量 | 最终验收 evidence |
|---|---|---|---|---|
| `PlanningContextLoopService` | 七模式规划候选运行 | `services` application orchestration | 固定 inquiry→Memory→Plan→Review 流程、candidate-only、bounded revision | 七模式 full-loop integration + formal report |
| `build_planner_contract_bundle()` | Planner/Reviewer runtime assembly | `agents/planner.py` 及 registries | 七模式 Prompt/Skill/ToolPolicy/Schema 只有一个固定注册入口 | hash/registry/schema audit |
| `PlanReviewerAgent` | inquiry review、PlanProposal review | 独立 Agent + sealed Prompt/Skill/ToolPolicy | 不共享 Planner reasoning、不写 PlanRoot、HUMAN_REQUIRED 不被吞掉 | 独立输入/receipt + 预声明缺陷发现率 |
| `PlanningInquiryConditionedNeedGenerator` | reviewed inquiry | `services`，复用 Grounder/Validator/Compiler | 不从未接受未来 Plan 反推 Need，保留 inquiry/goal lineage | stable Need lineage + future-factualization rejection |
| `PlannerContextAssembler` | post-Genesis Memory result | Planner application service | consumer-specific Seed、author/accepted Plan protected、Evidence 可追溯 | budget/drop/path lineage + real Context artifact |
| `PlannerContextRuntimePort` | Planner loop | `ports/planning_context.py` | 正式路径只消费共享 Context owner，不复制 compactor/store | fake-to-real contract parity + shared replay evidence |
| `TypedGraphPathReceipt` adoption | relation/causal Planner Need | 现有 R1/retrieval owners | path/basis/access/evidence 不在 ChannelHit 折叠中丢失 | graph-only expand/path/cutoff regression |
| conditional graph expansion | unclosed relation/causal facet | RoutePlan/Controller/Tool owners | anchor entity 是唯一派生 seed，非默认 triple | conditional call trace + same-corpus ablation |
| Stage 4 evaluation runner | 独立验收 | `services/planning_evaluation.py` | 七模式、同 corpus、同预算、freeze-before-evaluator | formal manifest + ablation/human report |

## 4. Planner Memory 的架构边界

### 4.1 为什么不能复用 Writer 的 Need 生成

Stage 2M `TaskPlanConditionedNeedGenerator` 的输入是已冻结的未来章节规划，目标是找“写这份规划需要的
历史状态和证据”。Planner 在开始时经常还没有未来章节规划，必须先决定要规划什么、验证什么、比较
什么。如果直接复用 Writer 入口，会发生：

- 把 Planner 自己刚提出的候选当成已接受 Plan；
- 用未来目标反向决定检索，从而掩盖目标本身是否合理；
- 把 `planner_proposed` 结果错误提升成 PlanRoot；
- WriterContextPackage 的人物/正文执行布局污染规划上下文。

所以正式流程固定为：

```text
Author Intent/current Plan/Canon
→ inquiry/goal proposal
→ independent review
→ reviewed inquiry artifact
→ bounded MemoryNeed
```

### 4.2 `PROJECT_BOOTSTRAP` 的特殊边界

当前 `PlanningTask(PROJECT_BOOTSTRAP)` 明确没有 `base_commit`，而 `MemoryResolutionRequest` 必须绑定
真实 `base_commit/snapshot`。因此第一版：

- bootstrap 只消费作者明确批准的 source/reference、ProjectProfile 模板和本轮 inquiry；
- 不伪造 Genesis commit、不调用 commit-scoped Memory Gateway；
- 先输出 `ProjectIntentModel / WorldDesignProposal / ProjectProfileProposal / PlanProposal` candidate；
- candidate 经人工/Stage 5 bootstrap 接受形成 Genesis 后，STORY/ARC 等 post-Genesis 模式才能读取
  project Memory；
- 如果将来确有“导入旧项目后 bootstrap”的 caller，必须提供真实 imported base commit/snapshot，
  不能在本任务中通过 optional flag 绕过。

### 4.3 post-Genesis 模式的 Memory 权限

STORY、ARC_VOLUME、CHAPTER_SET、CHAPTER、SCENE、REPLAN 使用：

```text
AccessScope.AUTHOR_PLANNING
base_commit = current accepted commit
snapshot = exact or explicitly typed degraded policy
planner_may_read_plan = true
retrieval_may_return_plan = only for plan-related Need
claim_may_cite_plan = only for reviewed inquiry that requires accepted Plan evidence
evaluator/gold/private future text = forbidden
```

检索出的历史和 Plan 只作为 evidence；任何新目标仍是 `planner_proposed`。

## 5. 核心领域合同

### 5.1 `PlanningLoopRequest`

外层请求包住现有 `PlanningTask`，避免把所有 runtime 字段继续塞入 Stage 2 老模型：

```text
run_id / task_id / project_id
PlanningTask(mode/base_commit/source_ids/creative_scope/strategy)
author_intent_artifact refs
accepted Plan/World/Text/Profile refs（按 mode 必需）
snapshot/freshness receipt（post-Genesis）
explicit author overrides
planning horizon / target range / rolling window
allowed skills
inquiry/memory/context/review/model budgets
configuration/model fingerprints
```

`PlanningTask` 保留现有 source/mode/provenance 语义；`CHAPTER_SET` 加入合法 post-Genesis mode。滚动窗口
默认表达 1～3 章或 ProjectProfile 配置值，不写死全项目统一章数。

### 5.2 `PlanningInquiry` 和 `GoalProposal`

Planner 第一步结构化输出：

```text
planning mode/scope/horizon
author intent refs and explicit overrides
goal proposals
alternative directions
assumptions to validate
questions for current state/history/relations/obligations/style
decision criteria
expected Plan output shape
human choices that cannot be inferred
```

每个 goal/assumption/question 必须标记 provenance：

- `author_supplied`：必须引用允许的 source id；
- `accepted_plan_derived`：引用当前 Plan item；
- `canon_derived`：引用当前 commit/evidence；
- `planner_proposed`：明确是候选，不得带伪造 author source。

### 5.3 `PlanReviewerAgent`

一个独立 Reviewer 合同审核两类 target，不创建两个审查系统：

```text
target_kind = INQUIRY | PLAN_PROPOSAL
decision = ACCEPT | REVISE | HUMAN_REQUIRED
coverage issues
contradiction / feasibility / obligation / pacing issues
alternative comparison issues
memory gaps
preserve decisions
bounded revision instruction
```

独立性要求：

- Reviewer 使用自己的 AgentSpec、Prompt、Skill、receipt；
- 输入是原始 author/current-state refs + 被审对象，不读取 Planner chain-of-thought；
- Reviewer 没有 Memory write/PlanRoot/Commit Tool；
- inquiry 与 final Plan 的 reviewer-directed revision 使用每次 invocation 的有界 work slice，默认一次；
- slice 用完时保存 checkpoint 并 `YIELDED`，后续 invocation 可在有进展时续跑；相同内容/issue 无进展时
  进入 review，而不是靠扩大次数循环；
- 第二次仍非 ACCEPT 或涉及重大作者取舍时 `HUMAN_REQUIRED/REVIEW_REQUIRED`。

同一底层模型可以用于开发，但 Agent/Prompt/context 必须独立；正式评价再使用独立 evaluator 或盲审。

### 5.4 `PlanningInquiryConditionedNeedGenerator`

输入只接受已审核 inquiry/goal artifact 和可信当前 basis。实现过程：

1. 将 inquiry 中的事实问题、关系/因果问题、义务/节奏问题拆成 bounded draft；
2. 复用 `NeedDraftGrounder` 绑定实体/关系 alias；
3. 复用 `NeedValidator` 拒绝未来事实化、越权、重复和无证据停止条件；
4. 复用 `NeedQueryCompiler` 生成通道可执行 query bundle；
5. 按 mode 映射 chapter target 或 horizon；
6. 写入完整 inquiry/goal/source lineage 和 Need completion facets；
7. 输出有限 Need set，交给同一个 Memory Gateway。

映射原则：

| planning mode/问题 | Need 目标 |
|---|---|
| STORY / ARC_VOLUME | horizon/global arc、人物弧、长期义务、历史节奏 |
| CHAPTER_SET | 当前滚动窗口、跨章依赖、hook/payoff、人物状态迁移 |
| CHAPTER | 当前章目标、前置状态、义务、可实现性 |
| SCENE | 场景参与者、知识边界、地点/时间/关系、对话目标 |
| REPLAN | deviation basis、未履行义务、已失效假设、可保留决定 |

Generator 不选择 top-k、权重或底层 tool。相同 inquiry fingerprint 必须生成稳定 Need identity；review
revision 或 author override 改变时形成新 generation，不覆盖旧 artifact。

### 5.5 `PlannerContextPackage`

新增 Planner 专用 Seed，不继承或复制 `WriterContextPackage`：

```text
contract_version
planning task/mode/scope/horizon
basis commit/snapshot/profile
author intent and explicit override
accepted plan and active obligations
current world/text feasibility state
arc/volume/chapter history and deviation
relationship/causal/epistemic evidence
style/reference/process lessons（optional）
unresolved inquiry/conflict/gaps
budget report
need/retrieval/evidence/path lineage
rendered planner context
```

`PlannerContextAssembler` 从 `Stage1ContextPackage`、reviewed inquiry 和当前 root refs 构造以上分区：

- mandatory author/accepted Plan/current hard state 先放；
- Planner 新提目标只放 working proposal 区，不伪装 evidence；
- optional history 按 Need priority、source/chapter/path diversity 竞争预算；
-所有 selected graph result 必须带 path receipt；
- exact token report、drop reason、unresolved gap 必须可审计；
- package 是 immutable Seed，后续 inquiry/review Memory 用共享 `ContextDelta/View`。

### 5.6 `PlanningTurnOutput` 与 Context loop

Planner proposal/revision step 返回：

```text
action = PLAN_READY | REQUEST_MEMORY
plan_proposal_draft             # PLAN_READY 必需
memory_request                  # REQUEST_MEMORY 必需
assumptions / unresolved
selected_skill ids
context use declarations
```

Reviewer 发现新的必要 Memory gap 时，由服务生成 reviewer-bound request，再通过同一 Need generator；
Reviewer 不直接调用 retrieval。第一版每个 planning loop 只允许有限的 inquiry Memory 和一次 plan-review
补充 Memory；重复 fingerprint、无新信息进入 typed review terminal。Memory work allowance 用完进入
`BUDGET_REVIEW`，只有显式增加 Planner Memory tranche 后才能从 checkpoint 续跑；Task retry tranche
不等同于 retrieval rounds/tool calls/token/time budget。

### 5.7 2026-08-13 自主 Planner loop 收尾决定

当前 production loop 已有 inquiry Memory、reviewer-bound Memory、phase checkpoint、每 invocation revision
slice、`YIELDED` 续跑、no-progress guard、独立 Planner-Memory tranche、rolling Plan projection 和 Planner
Context soft target。本轮把三个未闭合点纳入同一 owner，不修改 Stage 2：

1. `PlannerAgent.run_turn()` 的正式输出为 `PlanningTurnOutput`。`PLAN_READY` 继续物化既有
   `PlannerExecutionResult`；`REQUEST_MEMORY` 只能携带问题，服务把问题绑定到当前 accepted inquiry goal，经过
   同一个 inquiry review/Need generator/Memory Gateway/ContextDelta 路径后再次调用 Planner。Planner 本身仍
   不直接调用 retrieval。
2. `PlanningBudgets.model_token_budget` 定义为一次 invocation 的累计软 slice，不再冒充单次 provider output
   ceiling。`PlanningLoopCheckpoint` 累加 call/input/output/reasoning token；每个已结算模型调用后检查 slice，
   达到时在最近安全 phase 写 checkpoint 并 `YIELDED`。单次物理输出上限继续由 production model request
   policy 和 provider hard window 决定。
3. Need generator 先逐问题执行 goal-bound 合法性验证，再跨全部合法结果去重并按 `blocking → inquiry order`
   形成最多 24 项 current tranche；余项作为 `deferred_question_ids` 持久化。下一 invocation 从 deferred
   frontier 继续，不静默丢弃第 25 项，也不把 deferred 当 rejection。

停止语义保持清楚：有 deferred 或新 evidence 时 checkpoint/yield；相同 Planner Memory 问题、相同 Context
artifact 或无新增 facet closure 时进入 typed review/no-progress；provider hard window、basis、access 和 future
leakage 仍是不可自动放宽的硬边界。

## 6. 七种 Planner 模式的具体输出

| Mode | 主要输入 | 主要 candidate 输出 | 特殊不变量 |
|---|---|---|---|
| PROJECT_BOOTSTRAP | author-approved sources，无 base commit | intent/world/profile/story seed | NORMALIZE_ONLY 不得新增 planner content；不调用 project Memory |
| STORY | author intent + accepted roots | premise、主冲突、长程结构、核心 arc | 不把远期候选写成已发生事实 |
| ARC_VOLUME | story plan + history | volume/arc goal、turning point、obligation schedule | 维护跨卷依赖和人物弧 |
| CHAPTER_SET | accepted near-term plan + recent history | rolling 1～3 章 focus、每章目标、跨章 hook/payoff | 是滚动规划，不冻结整书 200 章 |
| CHAPTER | chapter-set/arc + current state | 当前章 goal/beat/constraints/end-state proposal | 必须可交付为 Stage 3 accepted Plan candidate |
| SCENE | chapter goal + current state | scene beat、角色、POV、disclosure、dialogue intent | 不写正文，不替代 WriterWorkPlan |
| REPLAN | deviation + current accepted Plan/Canon | preserve/replace/retire proposal、deviation record | 必须引用 affected items，不静默覆盖旧 Plan |

PlanProposal item 保留 `AUTHOR_SUPPLIED / PLANNER_PROPOSED`，并增加引用 reviewed inquiry、Memory Need、
Evidence、Reviewer revision 的 lineage。Fallback/degraded proposal 必须显式 `DEGRADED`，不得输出
`PLAN_CANDIDATE_READY`。

## 7. 高级检索的代码设计

### 7.1 保留现有 owner

不复制 agentmemory 的进程内 BM25/vector/graph，不新增第二套 hybrid service。继续使用：

```text
R1WorldRepository / R1RetrievalBackend
Stage2ROpenSearchBackend / CompositeRetrievalBackend
RetrievalRoutingService / RoutePlan
BoundedMemoryController / RetrievalToolAdapter
FusionService (the only RRF owner)
ContextCompiler / EvidenceExpander
```

精确状态/已知 ID 使用 R1 Exact/Temporal；精确引用使用 Grounded BM25；风格/声音使用 Grounded
BM25+Dense。不要让所有 Planner Need 默认 triple。

### 7.2 `TypedGraphPathReceipt`

当前 `services/r1.py::GraphPath` 已含 relation row/id、entity path、direction、edge semantics 和
EvidenceRefs，但 `R1RetrievalBackend.search()` 折叠为普通 hit。实现时：

1. 把 path receipt 定义为 versioned domain model；
2. TYPED_GRAPH 路径调用 `typed_graph_paths()`，不先丢失路径；
3. 每条 selected path 持久化 content-addressed receipt；
4. `RetrievalUnit/ChannelHit/Trace` 以 receipt ref 贯穿 Fusion、selection、Context 和 report；
5. receipt 绑定 base commit/snapshot/access、seed entity、relation rows、directions、edge semantics、
   EvidenceRefs、depth；
6. only canonical/evidence edges，depth 默认 1～2、硬上限 3；
7. graph-only hit 也必须能独立 expand 到 L0 Evidence，不能依赖同时被 BM25/Dense 命中。

不改 PostgreSQL graph 存储；只有正式规模 benchmark 证明当前枚举成为瓶颈后，Stage 5 才评估 indexed
adjacency/recursive CTE，当前不引入 Neo4j。

### 7.3 Anchor→Graph 条件扩展

将 relation/causal route 从“graph+anchor 默认并行”改为 Planner 使用的条件路径：

```text
Anchor BM25 + Dense
→ select explicit entity anchors
→ public NeedFacet sufficiency check
→ relation/causal facet closed? yes: stop
→ no: derive trusted graph seed receipt from selected anchor.entity_ids
→ Typed Graph depth 1–2
→ existing FusionService merges independently ranked Anchor/Graph results
→ selected path expands to L0 Evidence
→ sufficiency/conflict check
```

实现落在现有 RoutePlan/RouteBoundControllerPolicy/BoundedMemoryController/Tool adapter 周围：

- 新增注册条件如 `relation_or_causal_facets_unclosed`；
- graph seed 只能来自 Need 已绑定实体或 selected Anchor 的显式 `entity_ids`；
- Controller 生成 trusted seed receipt，Agent/tool 参数不能自报 seed authority；
- adapter 复核 anchor unit、basis、scope、path depth；
- graph 失败形成 `ChannelFailure`，Anchor 结果仍可显式 degraded，不静默吞错。

禁止用 LLM 临时猜实体、inferred/similarity edge 或 query observation 作为 proof edge。

### 7.4 compact→expand

`RetrievalToolAdapter` 第一轮只向 Planner/Controller 暴露 compact result：

```text
unit/path id
kind / basis / information scope
rank/score/channel
entity ids / path summary
token estimate
receipt ref
```

只有最终 selected unit/path 通过现有 EvidenceExpander 读取全文/原始 span。Expand receipt 记录 compact
identity、selected reason、L0 refs、token cost、drop/failed reason。不得把完整 top-k payload 全塞进 Planner
Context。

### 7.5 diversity 和 Fusion

在现有 kind/chapter/evidence quota 上增加可配置：

- `max_per_source_artifact`；
- `max_per_graph_path_root`；
- chapter/volume coverage；
- mandatory item 豁免，但仍受 access/cutoff/basis Gate。

保持 `FusionService(rrf_k=60)` 为唯一 owner和现有 unweighted baseline。只在同 corpus 消融证明后，才允许
把 per-intent fixed weights 作为候选；learned weights、运行时自动调权进入 Stage 5，不在本阶段开发。

## 8. Skill、Prompt、ToolPolicy 和模型调用

### 8.1 Planner Skills

复用现有：

- project intent modeling；
- story architecture；
- arc/volume planning；
- chapter goal decomposition；
- scene contract planning；
- plan deviation/replanning。

新增当前 caller 需要的：

- rolling chapter-set planning；
- alternative comparison；
- obligation scheduling；
- character arc / hook-payoff planning；
- inquiry formation；
- independent inquiry/plan review。

Skill 是 content-addressed Method Asset。Planner 只在 request allowlist 内选择；Reviewer 使用独立 review
Skills；两者都产生 `SkillExecutionReceipt`，不能动态安装/覆盖 active Skill。

### 8.2 ToolPolicy

当前 unit test 中 Planner ToolPolicy 名义上允许 `memory.request_context` 和 `proposal.validate_plan`，但
现有 `PlannerAgent` 实际仍是单次 structured call。Stage 4 正式路径采用应用层 action：

- Planner/Reviewer 只返回 `REQUEST_MEMORY`；
- `PlanningContextLoopService` 校验后调用 Memory Gateway；
- Planner/Reviewer 不获得 `memory.search_*`、`memory.write`、`root.update`、`canonical.commit`；
- 内部 action 直接 append RunEvent，不走 HTTP/shell Hook。

### 8.3 模型容量

Inquiry Planner、Plan Generator、Reviewer 和 evaluator 的真实调用统一复用 `ModelGateway` 和同一个
endpoint-global `ModelRequestAdmissionController`。每次 dispatch 由共享 Context View 实际 tokenizer
计算 prompt、reserved output、安全余量；不得在 Planner 自己实现 semaphore 或并发池。

## 9. 连续开发顺序（不中断执行测试）

以下内容由一个 Stage 4 owner 在同一 worktree 连续完成。每段完成后只做代码阅读、类型/合同一致性检查
和 implementation log 记录；不在段间插入测试阶段，也不以测试结果作为继续下一段的许可。

### S4-A：收敛现有 Planning domain

- 保留现有 `PlanningTask`/`PlannerProposalDraft`/`PlanProposal`/provenance 行为；
- 吸收共享合同提交中已冻结的 `CHAPTER_SET` 和七模式合法性，不在 Stage 4 分支重复编辑共同枚举；
- 新增 `PlanningLoopRequest`、budget、terminal、lineage；
- 给 PlanProposal 增加 inquiry/memory/review lineage，但保持旧六模式调用可读取；
- 增加 Stage 4 schema exporter/`schemas/stage4`；
- 不迁移或复制整个 `domain/stage2.py`，只扩展当前 planning owner。

### S4-B：Inquiry/Goal 和独立 Reviewer

- 将当前测试/benchmark 中分散的六模式 AgentSpec 注册收敛为 `build_planner_contract_bundle()`，扩展为
  七模式并增加 inquiry step；
- 新增 `PlanReviewerAgent`，同一合同支持 INQUIRY/PLAN_PROPOSAL target；
- 实现 inquiry 一次 revision、final plan 一次 revision 和 HUMAN_REQUIRED；
- 增加 Prompt/Skill/AgentSpec/ToolPolicy/receipt；
- 保证 Reviewer 输入独立，不接受 Planner 内部 reasoning。

### S4-C：Planner-specific Memory

- 实现 `PlanningInquiryConditionedNeedGenerator`；
- 接 Grounder/Validator/QueryCompiler/RoutePlan/MemoryGateway；
- 实现 bootstrap source-only 与 post-Genesis Memory 两条明确路径；
- 实现 mode→chapter/horizon、plan read/cite、AUTHOR_PLANNING scope；
- 将所有 Memory 结果保存为 typed artifact/receipt，不直接改 PlanProposal。

### S4-D：Planner Context 和共享 Context Runtime 接线

- 实现 `PlannerContextPackage` 和 `PlannerContextAssembler`；
- Planner 自有 port 先对接冻结 fixture；
- 吸收 Stage 3 共享 AgentContext 合同后接真实 View/Delta/compaction；
- protected author intent/accepted Plan/reviewer issues，optional history compact→expand；
- 实现 Context pressure、reactive Memory、resume 和 exposed/used receipt；
- 不保留两套正式 projector。

### S4-E：高级读取采用

- 实现 TypedGraphPathReceipt；
- 修改 relation/causal Planner route 为 Anchor→Graph conditional expansion；
- 实现 compact result→selected L0 expand；
- 增加 source/path/chapter diversity；
- 保持现有 Fusion owner和 exact/quote 专用路径；
- 在 report 中保留 channel failure/degraded 原因。

### S4-F：Planning Context Loop

新增薄 `PlanningContextLoopService`，固定调用顺序：

```text
preflight
→ inquiry propose/review/(one revision)
→ memory resolve
→ PlannerContextPackage/View
→ plan propose
→ review
→ optional reviewer Memory + one revision
→ terminal
```

Service 负责 event、artifact、budget、checkpoint、resume、terminal；不实现 retrieval，不写 PlanRoot，不建
Scheduler/DAG。失败只重试责任层：Reviewer 失败不重跑 Memory；Context projection 失败不重跑 inquiry；
plan revision 不覆盖父 proposal。

### S4-G：评价 Runner 和报告代码

- 新增 `scripts/run_stage4_planning_evaluation.py`；
- 组装七模式 case loader、configured/fake endpoint adapter、blind evaluator export；
- 输出 inquiry→review→Need→retrieval→Context→Plan→review/revision 完整 lineage；
- 支持 retrieval ablation、no-memory/current-plan-only baseline；
- 先完成 runner/report 代码，不在开发中段执行正式测试或真实实验；
- 所有功能代码完成后统一进入 §11。

## 10. 文件所有权和禁止范围

### 10.1 Stage 4 owner

建议在现有 owner 周围实现：

```text
src/novel_agent/domain/stage2.py                  # 只消费共享枚举；必要的旧 planning 输出兼容扩展
src/novel_agent/domain/planning.py                # 新 Stage 4 loop/reviewer/package 合同，确定新增
src/novel_agent/domain/planning_memory.py         # 复用/扩展 Need lineage，不复制 Writer 语义
src/novel_agent/agents/planner.py
src/novel_agent/agents/plan_reviewer.py
src/novel_agent/services/planning_inquiry_need_generation.py
src/novel_agent/services/planner_context_assembler.py
src/novel_agent/services/planning_context_loop.py
src/novel_agent/services/planning_evaluation.py
src/novel_agent/ports/planning_context.py          # 对共享 Context Runtime 的窄 port
src/novel_agent/prompts/planner_*.md / plan_reviewer_*.md
src/novel_agent/skills/*planning*.md / plan_review_*.md
schemas/stage4/
scripts/run_stage4_*.py
Stage 4 focused tests/fixtures/benchmarks/golden
```

为高级读取允许最小扩展：

```text
src/novel_agent/domain/memory.py
src/novel_agent/domain/retrieval_routing.py
src/novel_agent/services/r1.py
src/novel_agent/services/retrieval_routing.py
src/novel_agent/runtime/memory_controller.py
src/novel_agent/tools/retrieval.py
src/novel_agent/services/retrieval.py             # 只扩展既有 Fusion/selector，不新增 owner
```

这些共享 retrieval 文件在 Stage 2 验收 worktree 不修改；Stage 4 合并前必须统一跑 Stage 2 回归。

### 10.2 禁止范围

- 不复用 `TaskPlanConditionedNeedGenerator` 作为 Planner 初始目标生成；
- 不传入或返回 `WriterContextPackage`；
- 不新增平行 BM25/vector/graph、第二 Fusion、全局固定 0.4/0.6/0.3 权重；
- 不默认所有 Need triple，不把 similarity/inferred edge 当 proof；
- 不在 bootstrap 伪造 base commit；
- 不直接写 PlanRoot/WorldRoot/TextRoot/MemoryPatch/Commit；
- 不建设外部 Hook、Task/Attempt、lease、Scheduler、Supervisor、Temporal；
- 不做 Experience/Skill 自动演化或长程 consolidation 平台。

## 11. 全部开发完成后的统一测试与验收

只有 §9 的 A～G 代码、schema、Prompt/Skill、runner/report 全部完成后，才开始本节。测试中发现问题由
同一个 Stage 4 owner连续修复，修复后重跑受影响范围和最终全量；不回到按小工作包逐段签发。

### 11.1 Domain/schema/contract

统一覆盖：

- 七模式 task/output/terminal；
- bootstrap no-base/no-Memory 和 post-Genesis basis；
- author/planner/canon/accepted-plan provenance；
- inquiry/reviewer decision/revision budget；
- inquiry→Need stable lineage和未来事实化拒绝；
- PlannerContextPackage section/budget/drop/gap；
- PlanProposal parent/revision/reviewer lineage；
- fallback/degraded 不得进入 READY；
- Pydantic/schema/golden parity。

### 11.2 Context、Memory 和检索

- shared full replay == incremental Planner View；
- basis/profile/intent/Plan revision 变化强制 rebuild；
- protected author intent/accepted Plan/reviewer issue 不被压缩；
- context hard limit typed suspend、soft compaction no-op；
- exact/quote 不进入 default triple；
- graph path receipt 从 GraphPath 到 L0 Evidence 完整；
- anchor explicit entity→conditional graph，facet 已闭合时零 graph call；
- graph-only identity 可 expand；
- source/path/chapter diversity 且 mandatory 不被 quota 丢弃；
- channel failure、vector dimension mismatch、cutoff/access、deletion/no-ghost 降级；
- compact→expand 只展开 selected unit/path。

### 11.3 Service/offline/integration

统一覆盖终态：

```text
PLAN_CANDIDATE_READY
INQUIRY_INVALID
INQUIRY_REVIEW_REQUIRED
MEMORY_INSUFFICIENT
PLAN_CONFLICT
REVIEW_REVISION_REQUIRED
HUMAN_REQUIRED
CONTEXT_LIMIT
MODEL_UNAVAILABLE
BASIS_CHANGED
DEGRADED_NOT_PROMOTABLE
SUSPENDED
BLOCKED
```

执行 PostgreSQL R1、OpenSearch BM25/Dense、ObjectStore、RunEvent/Checkpoint 的真实 adapter integration；
断言零 PlanRoot/Canon/Memory write/Commit 调用。

### 11.4 统一命令

最终至少运行：

```bash
.conda-env/bin/pytest <all Stage 4 unit/contract/property/regression tests>
make integration
make quality
.conda-env/bin/pre-commit run --all-files
git diff --check
```

然后重跑相关 Stage 1/2 retrieval routing、R1 repository、Memory Controller、Memory Gateway、PlannerAgent、
model admission 回归，确保 Stage 4 扩展未改变 Stage 2 已接受语义。

### 11.5 真实 Planning benchmark

正式 case 覆盖七模式；bootstrap 使用 source-only，其余使用同一冻结小说 corpus/current roots。至少包含：

- 全书故事结构；
- 人物弧/卷规划；
- rolling chapter-set；
- 单章可写性；
- 场景/知识边界；
- 关系链/因果多跳；
- 发生 deviation 后的 replan。

比较：

```text
author/current Plan only
Exact/Temporal only
Anchor BM25 only
Anchor Dense only
BM25+Dense
Graph only（只在合法 relation/causal case）
Anchor→Graph conditional
legacy registered triple diagnostic
```

所有方案使用同 corpus、cutoff、model、sampling、budget、Fusion owner和 evaluator；不复制上游权重。正式
报告包括：

- author intent/override coverage；
- accepted Plan/Canon contradiction；
- obligation/character arc/hook-payoff continuity；
- rolling hierarchy consistency和 chapter feasibility；
- alternative quality和 decision rationale；
- reviewer issue、revision outcome、human-required rate；
- Need/trace/path/evidence/context exposed/used；
- channel ablation、degradation、token/latency/model audit；
- future leakage、provenance error、unsupported factualization。

先用 pilot 冻结 formal manifest、rubric 和阈值，再执行一次正式 run；不得在正式输出后调 route/权重/阈值。
fake/scripted 只证明管线，不签发语义 PASS。

## 12. Stage 4 Gate

### 12.1 工程硬门禁

- 最终 accepted Stage 2 base 上 quality/integration/schema/property 全通过；
- 七模式、inquiry/goal、Reviewer、Planner Context、revision 全部可重放；
- shared Context Runtime 是唯一正式实现；
- retrieval path receipt、conditional expand、compact→expand 和显式 degradation 完整；
- endpoint-global admission 生效；
- 零 future/evaluator leak，零 PlanRoot/Canon/Commit 直接写入。

### 12.2 语义门禁

- 每种 mode 至少有可解释真实 case；
- inquiry 能产生与作者目标相关、可验证、非自证循环的 Need；
- Memory 对历史连续性/可实现性给出可解释净收益，不只是增加 token；
- Reviewer 能发现预声明矛盾/义务/可行性问题，bounded revision 不掩盖 HUMAN_REQUIRED；
- CHAPTER_SET 能输出可滚动的近程规划，不把整书冻结成脆弱 TaskGraph；
- relation/causal case 的条件图扩展相对默认 triple 给出质量/成本依据；
- 独立 evaluator/盲审无不可接受的意图偏离、未来事实化或 provenance 错误。

工程通过但真实语义资源不足只能 `CONDITIONAL_PASS`。任何 author/planner provenance、future leakage、
basis、PlanRoot 权限或 unsupported factualization 失败都必须 `REPAIR/BLOCKED`。

## 13. 返回架构评审的停止条件

出现以下情况停止扩张：

- 现有 Stage1MemoryNeed 无法表达 Planner horizon，拟建立第二 MemoryNeed 体系；
- 必须复制 Retrieval/Fusion/Graph 才能实现条件扩展；
- bootstrap 需要伪造 commit/snapshot；
- Planner/Reviewer 需要直接写 PlanRoot 才能继续；
- shared Context Runtime 无法满足 Planner，拟保留第二 compactor/store；
- bounded review 无法收敛，拟建设无限 Agent debate；
- 需要跨天调度、lease、Supervisor、Temporal 或通用 TaskGraph；
- 正式消融证明新的检索后端/权重 owner 才能解决，而不是现有 owner 的局部扩展。

## 14. 交付物

Stage 4 完成时交付：

1. 七模式 Planning domain、Planner、Inquiry、Reviewer、Plan candidate loop；
2. Planner-specific Need generator 和 PlannerContextPackage；
3. 共享 Context Runtime 正式接线；
4. path receipt、conditional Anchor→Graph、compact→expand、diversity；
5. versioned schema、Prompt/Skill/AgentSpec/receipt；
6. 全量测试、Stage 2 回归、真实 planning benchmark 与消融报告；
7. `.agent/implementation.md` 的命令、Artifact、失败修复和限制；
8. Codex 独立 review 的 `PASS / REPAIR / CONDITIONAL_PASS`。

本文不授权 Stage 5 的 Plan 接受、Canon Commit 或长期调度。
