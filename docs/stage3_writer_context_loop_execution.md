# Stage 3 Writer Agent 与 Writing Context Loop 详细执行文档

> 文档生命周期：`ACTIVE`
>
> 执行状态：`ENGINEERING_IMPLEMENTATION_COMPLETE_IN_ISOLATED_BRANCH / CONDITIONAL_GATE`
>
> 更新日期：2026-08-11
>
> 阶段：Stage 3 — Writer Agent and Writing Context Loop
>
> 上位设计：`docs/stage3_writer_core_overall_design.md`
>
> 上位决定：ADR-0006、ADR-0007、ADR-0008（ADR-0004 为历史 claim-first 基线）
>
> 关联技术基线：技术实施与选型设计 §28、InkOS 调研 §12/§14、Long-running Runtime 调研
> §19/§23
>
> 当前 caller：单章节/单场景正文候选运行、Stage 3 生成评价 Runner
>
> responsible layer：`services` 应用编排；复用 Stage 2 Memory、Model Gateway、Artifact、RunEvent owners

> 当前实现证据：`codex/stage3-writer-context-loop` / `bab4451`；1893 deterministic tests、
> 100% coverage、full pre-commit。真实模型语义、真实基础设施和最终共同基线 Gate 仍待完成。

## 1. 本阶段的完成定义

Stage 3 只解决一个产品问题：Writer 已经拿到**被接受的章节/场景规划**后，能否使用 Stage 2 Memory、
动态上下文和固定 Writing Skills，形成一份经过 Editor、最终候选观察和对账的正文候选。

```text
WritingLoopRequest
  + accepted Chapter/Scene Plan
  + WritingTaskContract
  + READY WriterContextPackage
→ AgentContextView(seed revision)
→ WriterWorkPlan + pinned Skill selection
→ Writer turn
   ├─ REQUEST_MEMORY → Memory Controller → ContextDelta → rebuild/compact → resume
   └─ DRAFT_READY
→ Editor REVIEW
   ├─ PASS
   ├─ LOCAL_REPAIR → one repair → independent re-review
   └─ MAJOR_REWRITE → one child Draft → full re-review
→ read-only final-candidate observation
→ Writer declarations / observation reconciliation
→ DRAFT_CANDIDATE_READY | REVIEW_REQUIRED | SUSPENDED | BLOCKED
```

`DRAFT_CANDIDATE_READY` 是本阶段最高成功终态。Stage 3 不修改 `TextRoot`、`WorldRoot`、`PlanRoot`，
不生成正式 `MemoryPatch`，不调用 `CommitService`。

## 2. 与 Stage 2 最终验收并行的开发方式

### 2.1 基线和 worktree

Stage 2 最终验收继续使用当前主 worktree。Stage 3 使用独立 clean worktree；不得把当前主 worktree
中未提交的 Stage 2 修复、运行产物或私有 benchmark 复制过去。

Stage 3 开始时在执行 manifest 中固定：

```text
stage2_base_commit
stage2_schema/configuration fingerprint
stage2 deterministic Memory Gateway policy identity
document_baseline_commit
stage3_branch/worktree identity
```

开发可以从“最后一个已接受、可执行的 Stage 2 commit”开始，不必等待最终 APC/TIO 语义结论；但 Stage 3
正式语义 Gate 前必须受控 rebase/merge 到**最终被接受的同一 Stage 2 executable identity**，重跑全量
质量、Stage 2 回归和 Stage 3 正式实验。未提交 dirty tree 不能作为共同基线。

### 2.2 现有 Stage 3 候选的正确使用方式

当前可复用候选以 `codex/stage3-acceptance` 中的代码候选 `3db41e6` 为主要来源，已有：

- `DRAFT / CONTINUE / MAJOR_REWRITE` Writer 合同和不可变 `DraftArtifact` lineage；
- `WriterGenerationService`、Writer Agent、content-addressed Prompt/Skill；
- Editor `REVIEW / LOCAL_REPAIR`、修复复审和 typed report；
- WriterContext handoff、最终候选观察接口、reconciliation；
- 三种 Context 方案评价基础、schema、fixture 和 offline runner；
- 当时 `make quality` 1594 tests、100% branch coverage 的历史工程证据。

该候选早于当前 Stage 2M，且真实语义实验没有执行。实现时：

1. 从当前 Stage 2 基线建立新 Stage 3 分支；
2. 选择性迁移 Stage 3 自有文件和行为，不整分支 merge、不直接 cherry-pick 旧集成 merge；
3. `domain/stage2.py`、Memory、retrieval、Model Gateway、WriterContext、schema exporter 等共享文件以
   当前基线为准，只做本文明确要求的最小兼容扩展；
4. 重新生成 Stage 3 schema 和 golden，不把旧 schema 当作正确性来源；
5. 旧 `CONDITIONAL_PASS` 只作历史证据，不继承为新 Gate 结果。

### 2.3 与 Stage 4 的并行边界

Stage 3 是共享 Context Runtime 的唯一实现 owner。Stage 4 在独立 worktree 通过冻结 port/fixture 开发，
不得复制 compactor、Event View 或 session store。Stage 3 尽早形成一个可独立审阅的“共享合同提交”，
包含：

- `AgentContextView`、`ContextDelta`、`ContextCompactionReceipt`；
- active Stage 所需的 typed RunEvent payload/event type；
- full replay/incremental projector port；
- provider-valid dispatch receipt。

Stage 4 可以先并行开发 Planner 自有 domain/agent/reviewer/evaluation；共享合同提交稳定后再受控吸收。
这只是集成接缝，不是第三个 Stage，也不授权两边各写一套实现。

### 2.4 连续开发、最后统一测试

Stage 3 开始后连续完成当前主线收敛、共享 Context Runtime、Writer cognition/Skill、Reactive Memory、
Editor/Observer/reconciliation、评价 Runner 和报告代码。中间工作包只定义设计、代码产出和依赖，不设置
测试 Gate，不要求“先跑一轮测试再获准继续”。所有代码、schema、Prompt/Skill、runner/report 完成并
经过整体代码阅读后，才统一进入 §10 的 focused、全量、集成和真实模型验收。

### 2.5 Stage 5 整合与并发边界

Stage 3 只拥有单个 Writing leaf 内部的固定依赖：Writer turn、REQUEST_MEMORY/ContextDelta、Editor
review/repair/rewrite、最终 Observer 和 reconciliation。它不实现跨 leaf scheduler。Stage 5 可以让一个
Stage 3 `DRAFT_CANDIDATE` Attempt 与 Stage 4 lookahead Planner 或只读历史维护同时运行，但必须满足：

- WritingLoopRequest 的 accepted Plan、base commit/snapshot/Profile/scope 在整个 leaf 内冻结；
- 并发 sibling 不得提交会改变该 basis 的 Plan/World/Text/Memory Canon；
- Writer、Editor、Observer 的每次模型调用都使用同一 endpoint-global admission controller；
- Context View、RunEvent、checkpoint 和 artifact identity 按各自 run/task 隔离，不共享 mutable messages；
- Draft settled 后可以并行运行确定性 checks，但正式 Observer 仍只读取最终通过 Editor 的候选；
- Stage 5 只能消费 public terminal/receipt，不得进入 Writer 内部跳过 review、reconciliation 或压缩 Gate。

Runtime 两路并发只改变 leaf 何时执行，不改变 Writer prompt、WCP evidence、token/sampling、repair budget
或失败语义。串行与并行必须通过同一 Stage 3 contract/lineage 测试。

## 3. 当前代码事实和缺口

| 能力 | 当前代码事实 | Stage 3 动作 |
|---|---|---|
| Writer Memory Seed | ADR-0008 evidence-first `WriterContextPackage + EvidenceLedger` 正在 Stage 2M 收敛 | 绑定新 contract/version 作为初始 Seed；不建第二个 Writer ContextPackage |
| Need 生成 | `TaskPlanConditionedNeedGenerator` 已绑定给定规划 | 继续作为初始 Writer Memory 入口；反应式问题只生成 bounded follow-up Need |
| Memory resolve | `MemoryGateway`、`MemoryResolutionRequest`、R0/R1/R2、Evidence 已实现 | 只通过应用服务调用；Writer 不接底层检索 Tool |
| Context compile | `ContextCompiler`/`WriterContextAssembler` 已实现静态 package | 增加事件派生 View/Delta/安全压缩，不改写 Seed |
| Run facts | `RunEventLogRepository` 已有 append/replay/idempotency/sequence；`RunCheckpointRepository` 已有 checkpoint | 扩展 active payload 和 View projector；不建 StepStore/conversation DB |
| Model capacity | `ModelGateway` 已接 `ModelRequestAdmissionController` | Writer、Editor、Observer 的所有真实请求都必须使用同一个 endpoint-global controller |
| Writer/Editor | 只存在于旧 Stage 3 分支候选，不在当前 main 生产路径 | 选择性迁移并与最新合同收敛 |
| Reactive Memory | 当前无 `REQUEST_MEMORY`、`ContextDelta` | 新增有界 Writer turn action 和外层 loop |
| Dynamic Context | 当前无 `AgentContextView` 和 compaction receipt | Stage 3 建唯一共享实现 |
| Skill 过程 | 候选按 mode 固定一个 Skill | 扩展为 allowlist 内的组合、选择、receipt；不动态安装 |
| 完整评价 | 候选 runner 可读 fixture/editorial 结果，真实全链未跑 | 改为运行真实 Writer→Editor→Observer→reconciliation，不注入 fixture verdict |

### 3.1 新增/收敛组件责任矩阵

下表的 evidence 均在全部开发完成后的 §10 统一产生，不是中间测试停点。

| 组件 | 当前 caller | responsible layer | 保护的不变量 | 最终验收 evidence |
|---|---|---|---|---|
| `WriterContextLoopService` | 单章节/场景候选运行 | `services` application orchestration | 固定步骤、有限 repair、candidate-only、只重试失败层 | 全终态 integration + 真实完整链 report |
| `AgentContextProjector` | Writer loop；后续 Planner port | `services/agent_context.py` | RunEvent 为事实、View 可重建、full/incremental 等价 | property/replay/compaction evidence |
| `ContextCompactor`（Projector 内部策略） | Context pressure/dispatch Gate | 同一 Context owner，不独立成平台 | safe cut、no-leak、claim/evidence/tool batch 原子性 | soft/hard/CAS/provider-valid regression |
| `WriterReactiveNeedAdapter` | Writer `REQUEST_MEMORY` action | `services`，复用现有 Need pipeline | Writer 只提语义问题，不取得 channel/budget/access authority | 越权拒绝、稳定 lineage、forced-gap 真实 case |
| `WriterWorkPlan` | Writer prewriting step | `domain/generation.py` + Writer Agent | 是运行方法而非 PlanRoot，引用 task/Plan/WCP | schema/lineage + plan-following report |
| `CandidateObservationAgent/Port` | Editor 通过后的最终候选 | Stage 3 read-only Agent/port | 不写 Memory/Canon，只观察本次最终 Draft | ToolPolicy audit + reconciliation lineage |
| Stage 3 evaluation extension | 独立验收 runner | `services/stage3_evaluation.py` | 三方案同条件、真实链、freeze-before-evaluator | formal manifest + machine/human reports |

## 4. 核心合同

### 4.1 `WritingLoopRequest`

外层请求至少绑定：

```text
run_id / task_id / project_id
base_commit / snapshot_id
WritingTaskContract ref/hash/revision
accepted Plan artifact ref/hash/revision
ProjectProfile ref/hash
WriterContextPackage ref/hash
information_scope = writer_safe
mode = DRAFT | CONTINUE | MAJOR_REWRITE
allowed_skills
memory/model/context/repair budgets
```

Preflight 必须验证 task、Plan、WCP、Profile、future-isolation attestation 和 runtime configuration 使用
同一 basis。缺少 accepted Plan 时终止为 `BLOCKED/MISSING_ACCEPTED_PLAN`，不得让 Writer 现场创建 Plan。

### 4.2 `WriterWorkPlan`

`WriterWorkPlan` 是本次生成方法 Artifact，不是 PlanRoot 或 ChapterPlan。它由 Writer 的结构化 prewriting
step 产生，服务层验证后进入 working context：

```text
scene/beat order
participating characters and current states
POV / epistemic boundary / reader disclosure
dialogue intent / per-character voice
pacing / transition / emotional movement
hook setup / advance / payoff / defer
must keep / must avoid / unresolved risk
selected skill ids and expected checkpoints
```

它必须逐项引用 WritingTask/accepted Plan/WCP 的公开 lineage；无法绑定的“未来事实”只能作为创作提案，
不能伪装成 Canon 状态。

### 4.3 `WriterTurnOutput`

不要把 Memory Controller 暴露为 Writer Tool。Writer 每一轮只返回一个结构化 action：

```text
action = DRAFT_READY | REQUEST_MEMORY
draft_text                    # DRAFT_READY 时必需
memory_request                # REQUEST_MEMORY 时必需
declared_memory_hints
unresolved_questions
self_observations
work_plan_checkpoint
```

`REQUEST_MEMORY` 与 `DRAFT_READY` 互斥。模型输出同时携带二者、缺少必需字段或请求底层 channel/top-k 时，
按 schema/semantic violation 拒绝，不做猜测性修复。

### 4.4 `WriterMemoryRequest`

允许 Writer 表达语义问题，不允许表达检索权限：

```text
question / purpose / blocked_action
known_context_item_ids
requested_evidence_type
scene_or_draft_checkpoint
risk / mandatory suggestion
```

服务端补齐并锁定 run/task/base commit/snapshot/access/POV/audience/target chapter。一个 draft 第一版最多
一次 reactive round；一轮可携带少量去重问题，由 service 统一限制。相同 fingerprint、无新 Evidence、
越权、basis 变化或预算耗尽时停止，不无限自问自搜。

`WriterReactiveNeedAdapter` 只做：

1. 去除 prompt 注入式 channel/budget 指令；
2. 复用 `NeedDraftGrounder`、`NeedValidator`、`NeedQueryCompiler`；
3. 生成绑定当前 accepted Plan/task/basis 的 `Stage1MemoryNeed`；
4. 由 RoutePlan/Memory Controller 决定 Exact/BM25/Dense/Graph；
5. 返回 typed `RESOLVED / PARTIAL / INSUFFICIENT / DENIED / BUDGET_EXHAUSTED`。

不得再次运行整套未来章节规划 Need 生成来掩盖一个局部问题，也不得让 Writer 设置
`allow_future_plan`、`access_scope`、`retrieval_budget`。

### 4.5 共享 `ContextDelta` 和 `AgentContextView`

`WriterContextPackage` 永远不原地修改。合法补搜结果产生：

```text
ContextDelta
  delta_id / request_ref / resolution_ref
  parent_view_revision
  base_commit / snapshot / profile / plan revision
  added memory item refs
  superseded item ids
  resolved and unresolved Need ids
  evidence/path/ledger refs
  token impact / information scope
```

View 最小字段按技术设计 §28.2：

```text
run/task/consumer/revision
basis + profile + information_scope
seed_package_ref
protected_items
active_memory_items
working_items
recent_settled_tail
unresolved_needs
compacted_prefix_ref / covered_event_range / kept_boundary
token_report / provider_validity_receipt / context_hash
```

全量 replay 是正确性 oracle，incremental projection 是优化。两者输出必须内容等价；basis、Profile、
Plan revision、POV/access 或 compaction generation 改变时强制 full rebuild。

### 4.6 typed event 和 settled checkpoint

本阶段按真实 caller 增加 payload，不把所有未来 Stage 5 event 一次实现：

```text
writer.work_plan_settled
context.memory_requested
context.memory_resolved
context.delta_applied
context.pressure_detected
context.compacted
writer.turn_settled
draft.candidate_settled
editor.review_settled
editor.repair_settled
candidate.observation_settled
candidate.reconciliation_settled
```

每个 payload 是 versioned Pydantic model；`RunEvent.payload` 虽仍为 JSON 存储，append 前必须通过对应
payload schema。不要更换 event store。checkpoint 只在以下 settled boundary 建立：

- immutable model result 和 `ModelCallRecord` 已完成；
- 对应 Artifact 已内容寻址持久化；
- 当前没有 pending/uncertain effect；
- tool/action/result batch 完整；
- checkpoint event position 已存在。

恢复从最新 settled checkpoint + 后续 RunEvent rebuild，不从进程内 `_replays` 猜状态。候选代码中的
进程内 replay cache 可以保留为 invocation-local 幂等优化，但不能成为恢复真源。

## 5. Context 窗口管理与压缩

### 5.1 层级

| 层 | 内容 | 压缩策略 |
|---|---|---|
| protected | system/tool policy、WritingTask、accepted Plan、author intent、mandatory、POV/access、未决 Need | 不删除；只能等价结构化 |
| memory | WCP evidence items、exact Ledger refs、合法 ContextDelta | 去重/supersede；先 compact handle，按需 expand |
| working | WriterWorkPlan、当前场景/草稿 checkpoint、Editor 指令 | 保留当前有效 revision；旧 revision 可压缩 |
| recent settled tail | 最近完整 model/tool batches | 保持 batch 原子性 |
| compacted prefix | provenance-bound runtime summary | 只替代被 receipt 覆盖的旧 prefix |

### 5.2 固定压缩顺序

```text
deterministic dedupe/supersession
→ compact evidence/path handles
→ extractive reduction
→ provenance-bound summary（只有前三层仍不足时）
→ re-tokenize
→ provider-valid dispatch Gate
```

Memory Controller 决定“哪些语义可保留/替代”，Context projector 只做机械投影。Summary 标记为 runtime
data，不能伪装 user/system instruction；claim/evidence group、tool batch、thinking/tool loop、pending
effect 不可拆分。

soft compaction 无法证明安全时 no-op，继续原 View；hard limit 无法安全闭合时
`SUSPENDED/CONTEXT_LIMIT`，绝不发送结构破损 Prompt。发布 compaction 前先持久化 summary/detail Artifact，
再按 `basis_event_position + parent_generation` CAS 发布 receipt；成功后旧 provider prefix/cache identity
失效。

Context pressure 由实际 tokenizer、模型 sequence limit、reserved output 和 safety allowance 计算，不使用
字符数或“约 80%”之类不可审计阈值。最终调用继续由 `ModelGateway` 生成 scheduling descriptor 并通过同一
`ModelRequestAdmissionController`。

## 6. Skill 实现

### 6.1 第一版允许的 Method Assets

在复用候选已有 scene/continuation/major-rewrite/editor skills 的基础上，补齐当前 Writer caller 需要的：

- character state and voice；
- dialogue intent and subtext；
- POV/epistemic discipline；
- pacing and transition；
- hook/foreshadowing realization；
- style/genre constraints。

这些都是静态、content-addressed、Profile-pinned 的 Markdown Method Asset，不是额外 Agent、动态插件或
可自修改代码。

### 6.2 选择和 receipt

WriterWorkPlan 可以从 `allowed_skills` 提议子集；服务层验证 AgentSpec/Profile/version/hash 后才注入。
每个选择产生 `SkillExecutionReceipt`，记录输入/输出 Artifact、completed/skipped checkpoint、unresolved、
status 和 latency。未选择或未执行不得伪装完成。Skill 不授予 retrieval、Memory write、Commit、Root
update 权限。

## 7. Writer、Editor、Observer 和对账的固定控制流

### 7.1 Writer loop owner

新增或收敛为一个薄 `WriterContextLoopService`，它只负责固定步骤、budget、event、checkpoint、terminal
和 owner 调用。它不实现 retrieval，不复制 Context Compiler，不创建 DAG DSL，不负责长期任务调度。

### 7.2 Editor 路径

保留候选的 typed Editor 合同并补齐自动 major rewrite：

1. 初稿完整 `REVIEW`；
2. `LOCAL_REPAIR` 只按冻结 span/scope 一次，产生子候选并独立 `REVIEW`；
3. `MAJOR_REWRITE` 只执行一次，保留父 Draft，使用 Reviewer 指令和同一 accepted Plan 生成子 Draft；
4. major rewrite 子 Draft 必须完整重审，不能只验证局部；
5. 任一路径预算耗尽、再次要求 repair/rewrite 或需作者抉择时进入 `REVIEW_REQUIRED`。

只重试失败层：Editor 暂时失败不重跑 Writer；Observer 失败不重跑已通过 Editor 的 Draft；reconciliation
失败不重跑生成。

### 7.3 最终候选观察

不要把 Stage 2 Memory write workflow 直接接进本阶段。Stage 3 使用一个无写权限的
`CandidateObservationAgent/Port`，输入只有最终候选、accepted basis 和允许的已有上下文，输出
`CuratorObservation`。其 ToolPolicy 明确拒绝 Memory write/Commit/Root update。

只有最终通过 Editor 的候选可被观察。Writer `declared_memory_hints` 与独立 observation 进入现有
`WriterChangeReconciliationService`；mismatch 形成 report/`REVIEW_REQUIRED`，不自动提交 MemoryPatch。

## 8. 连续开发工作包（同一 Stage 内的实现流，不是测试 Gate）

以下工作由一个 Stage 3 owner 在同一 worktree 连续完成。每包结束只记录实现状态、未决代码依赖和
Artifact/合同变化，不插入测试阶段，也不以测试结果作为下一包的准入条件。

### S3-A：当前主线收敛和候选恢复

**输入**：当前 accepted Stage 2 base、旧候选 `3db41e6`。

**动作**：

- 迁移 `generation.py`、`editorial.py`、Writer/Editor agents/services、prompts/skills、Stage 3 schema/tests；
- 保留 candidate-only、basis、future-isolation、idempotency、raw response quarantine；
- 用当前 `WriterContextPackage`/`MemoryGatewayResult` 重写 handoff adapter，消除旧 Snapshot 复制和重复
  rendered context；
- 所有共享合同冲突选择当前 main 语义，再补最小 Stage 3 enum/export；
- 恢复 fake/offline adapter 所需的 DRAFT、CONTINUE、local repair、major rewrite、failure 全终态代码。

### S3-B：共享 Context Runtime

**动作**：

- 实现 §4.5/§4.6 的 DTO、projector、typed event 和 compaction receipt；
- 实现 seed→full replay View、incremental apply、safe-cut properties、CAS compaction；
- 接 `ArtifactRepository`、`RunEventLogRepository`、`RunCheckpointRepository`，不加新表；
- 形成供 Stage 4 消费的窄 port/fixture。

### S3-C：Writer cognition、Skill 和 Reactive Memory

**动作**：

- 实现 WriterWorkPlan step 和 allowlist Skill selection/receipt；
- 将 final-only payload 收敛为 §4.3 的 turn action；
- 实现 `WriterReactiveNeedAdapter`、一次 bounded Memory round、ContextDelta 和 resume；
- 接实际 tokenizer、provider-valid dispatch 和 endpoint-global admission；
- 记录 Context exposed、Memory request、selected evidence、confirmed/declared use。

### S3-D：完整候选闭环与评价

**动作**：

- 把 WriterContextLoopService 接到 Editor、一次 local repair、一次 major rewrite、Observer、reconciliation；
- 扩展现有 `scripts/run_stage3_generation_evaluation.py`，正式模式必须实际运行全链；
- 建立 Context/Memory/Skill/lineage/terminal machine-readable report；
- 完成开发 pilot/formal manifest 的配置入口，但本工作包不执行测试或真实实验。

依赖关系：S3-A 与 S3-B 可交错实现；S3-C 依赖 S3-B 合同但可先写 WriterWorkPlan/Skill；S3-D 的 runner
和报告层可与前面代码并行编写，最终统一接完整链。A～D 全部完成后才进入 §10。不得把这些工作包重新
编号成 Stage 3～7。

## 9. 文件所有权和禁止修改范围

### 9.1 Stage 3 owner

主要拥有：

```text
src/novel_agent/domain/generation.py
src/novel_agent/domain/editorial.py
src/novel_agent/domain/stage3_evaluation.py
src/novel_agent/domain/agent_context.py             # shared contract first owner
src/novel_agent/agents/writer.py
src/novel_agent/agents/editor.py
src/novel_agent/agents/candidate_observer.py
src/novel_agent/services/writer_generation.py
src/novel_agent/services/writer_draft_integration.py
src/novel_agent/services/writer_context_loop.py
src/novel_agent/services/editorial.py
src/novel_agent/services/writer_change_reconciliation.py
src/novel_agent/services/agent_context.py            # single shared implementation
src/novel_agent/services/stage3_evaluation.py
src/novel_agent/prompts/writer_*.md / editor_*.md / candidate_observer_*.md
src/novel_agent/skills/*writing*.md / editor_*.md
schemas/stage3/
scripts/run_stage3_*.py
Stage 3 focused tests/fixtures/golden
```

允许最小公共扩展：`domain/stage2.py` 一次性冻结 Stage 3/4 已确认的 Writer、Editor、Candidate Observer、
Plan Reviewer Agent/mode enum 和 `CHAPTER_SET` 合法 planning mode；`domain/runtime.py` 增加 active event
type；更新公共 export。Stage 4 不再并行修改这些共同枚举；共享合同提交后通过受控合并吸收。

### 9.2 禁止范围

- 不修改 Stage 2 Need/retrieval/ranking/fusion 的既有语义来提高 Writer 评分；
- 不弱化 `WriterContextPackage` evidence、cutoff、future-isolation 或 token Gate；
- 不新增第二个 Model Gateway/admission controller、Artifact Store、RunEvent store、Context store；
- 不接 `CommitService`、Root repository、Memory write workflow；
- 不实现外部 Hook ingress、Task/Attempt、lease、Scheduler、Supervisor、Temporal；
- 不实现 Skill 自动生成/晋升、跨运行 experience/consolidation。

## 10. 全部开发完成后的统一测试与验收

只有 §8 的 A～D 代码、schema、Prompt/Skill、runner/report 全部完成后，才开始本节。测试发现问题时由
同一个 Stage 3 owner 连续修复；修复完成后重跑受影响范围和最终全量，不回到按小工作包逐段签发。

### 10.1 Domain/schema/contract

必须覆盖：

- WritingLoopRequest 的 task/Plan/WCP/Profile/basis 交叉校验；
- WriterWorkPlan 引用、Skill allowlist 和 source provenance；
- WriterTurnOutput action 互斥；
- Memory request scope/budget/channel 注入拒绝；
- ContextDelta parent revision/basis/信息域；
- CompactionReceipt event range/generation/CAS；
- Draft parent/child lineage、repair/rewrite budget；
- terminal 必需/禁止字段；
- JSON schema 与 Pydantic contract golden parity。

### 10.2 Context/property/regression

- full replay == incremental projection；
- event duplicate/idempotency collision、乱序、unknown payload version fail-closed；
- tool request/result、同一 model action batch、claim/evidence、pending effect 不可拆；
- mandatory/accepted Plan/POV/access 不被 summary 覆盖；
- soft compaction no-op、hard context typed suspend；
- 旧 generation 的 provider prefix/cache identity 在 compaction 后失效；
- Context exposed/confirmed-use 只能引用当前 View 中可见 item。

### 10.3 Service/fake/offline

覆盖完整终态：

```text
DRAFT_CANDIDATE_READY
REVIEW_REQUIRED_LOCAL_REPAIR_EXHAUSTED
REVIEW_REQUIRED_MAJOR_REWRITE_EXHAUSTED
INPUT_NOT_READY
MISSING_ACCEPTED_PLAN
MEMORY_INSUFFICIENT
MEMORY_DENIED
CONTEXT_LIMIT
MODEL_UNAVAILABLE
WRITER_FAILED
EDITOR_FAILED
OBSERVER_FAILED
RECONCILIATION_FAILED
BASIS_CHANGED
```

每条失败路径断言只重试责任层、保留已完成 Artifact、无 Canon/MemoryPatch/Commit 调用。

### 10.4 集成与全量质量

开发完成至少运行：

```bash
.conda-env/bin/pytest <Stage 3 focused unit/contract/property/regression tests>
make integration                 # RunEvent/PostgreSQL/ObjectStore/真实 retrieval adapter 合同
make quality                     # strict MyPy/Ruff/deterministic tests/100% branch coverage
.conda-env/bin/pre-commit run --all-files
git diff --check
```

正式 Gate 前还必须重跑 Stage 2 Memory Gateway、WriterContext assembler、model admission 的相关回归，
证明 Stage 3 公共扩展没有改变 Stage 2 最终验收语义。

### 10.5 真实模型实验

扩展现有三 Context 方案：

1. recent text only；
2. simple retrieval；
3. deterministic Stage 2 WriterContextPackage。

使用同一 Writer/Editor/Observer 模型配置、sampling、token/repair budget 和 endpoint admission。正式 case
至少覆盖：连续写作、多人对话/声音、人物关系与知识边界、因果回调、Hook/伏笔兑现、强制 reactive
Memory、Context pressure/compaction。Evaluator 数据和未来文本必须在 Context freeze 后才可见。

报告至少包含：

- plan/obligation following；
- character/state/time/relation/knowledge continuity；
- dialogue voice、POV、pacing、hook、literary quality；
- critical fabrication/leakage；
- Writer/Editor/Observer model audit、repair/rewrite 次数；
- Memory request 前后缺口、evidence added、confirmed use、no-request ablation；
- Context token/revision/compaction receipts；
- latency、input/output tokens、failure distribution。

先用 pilot 冻结正式阈值和 evaluator rubric，再生成 formal manifest；不得看完正式输出后调阈值。fake、
scripted verdict 或 fixture observation 只能证明工程路径，不能签发语义 PASS。

## 11. Stage 3 Gate

### 11.1 工程硬门禁

- 当前最终 Stage 2 base 上全量质量通过；
- schema、typing、branch coverage、migration/integration（若有）通过；
- Context replay/compaction/provider-validity 属性全部通过；
- endpoint-global admission 生效，Context budget 是实际 tokenizer 结果；
- 所有 Artifact/receipt/terminal 可重放；
- 静态和运行证据均为零 Canon/MemoryPatch/Commit 调用、零未来泄漏。

### 11.2 语义门禁

- 正式 runner 的每个可评价 case 都执行真实完整链；
- WCP 方案在计划遵循和连续性上对 simple retrieval/recent text 给出可解释净收益或至少无关键退化；
- reactive Memory 的预声明 case 能证明“缺口→新 Evidence→使用/决策”的因果链，而不是只增加 token；
- 对话/POV/Hook/文学质量无不可接受退化；
- Editor/Observer/reconciliation 结果绑定实际最终 Draft；
- 独立 evaluator/盲审结果、失败和限制完整披露。

若工程门禁通过但正式模型资源或语义证据不足，只能是 `CONDITIONAL_PASS`；如果 Context、leakage、
basis、权限或候选 lineage 失败，则 `BLOCKED/REPAIR`，不能以人工说明覆盖。

## 12. 返回架构评审的停止条件

实现遇到以下情况停止扩张并返回 Codex/架构评审：

- 必须修改 Stage 2 Memory 检索/排序默认语义才能闭环；
- 一个 reactive round 无法证明需求，需要长期多 Agent 调度或无限循环；
- 需要新数据库、消息队列、第二 event/context store 或通用 DAG；
- Candidate Observer 无法保持只读而必须进入 Memory write；
- accepted Plan、Profile、POV/access 或 basis 无法在现有合同中表达；
- 固定 Skill 不能满足需求，拟引入自动安装/演化；
- 正式实验暴露新的产品目标，而不是本文机制的局部缺陷。

## 13. 交付物

Stage 3 完成时应交付：

1. 当前 Stage 2 基线上的 Writer/Editor/Observer/Reconciliation 生产候选代码；
2. 共享 AgentContextView/Delta/Compaction 合同和唯一实现；
3. versioned schema、prompts、skills、AgentSpec/receipts；
4. focused/full/integration/property 测试；
5. 可复现真实三方案生成实验和 machine-readable/human-readable report；
6. `.agent/implementation.md` 中的命令、Artifact、失败修复和剩余限制；
7. Codex 独立 review 后的 `PASS / REPAIR / CONDITIONAL_PASS`。

本文不授权 Stage 5 的 Plan 接受、Canon Commit 或长期运行。
