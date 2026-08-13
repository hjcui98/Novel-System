# Stage 3 Writer Agent 与 Writing Context Loop 总设计

> 文档生命周期：`ACTIVE`
>
> 设计状态：`DESIGN_BASELINE_2026-08-10`
>
> 开发状态：`ENGINEERING_INTEGRATED / REAL_SEMANTIC_GATE_PENDING`
>
> 阶段：Stage 3 — Writer Agent and Writing Context Loop
>
> 上位决定：ADR-0006、ADR-0007、ADR-0008（ADR-0004 为历史 claim-first 基线）
>
> 本文职责：定义 Writer 产品闭环、动态上下文窗口、Skill、Memory 请求和验收边界

## 1. 设计结论

Stage 3 的目标是把**已经给定并接受的章节/场景规划**、作者本轮写作指令和 Stage 2 Memory 变成
可审阅的正文候选：

```text
WritingTaskContract + accepted Chapter/Scene Plan
→ Stage 2M plan-conditioned Writer Memory
→ WriterContextPackage
→ dynamic AgentContextView
→ Writer work plan / Skills / Draft steps
→ reactive REQUEST_MEMORY / ContextDelta / safe compaction / resume
→ Editor / bounded repair or rewrite
→ Curator observation / reconciliation
→ DRAFT_CANDIDATE_READY
```

本阶段回答：

1. Writer 能否把章节规划、人物、情节、对话、语言、POV、节奏、Hook 和记忆约束实现为正文；
2. 初始记忆不足时，Writer 能否在不直接控制检索的情况下安全补充信息并继续；
3. 长生成循环中的上下文窗口能否持续补充、压缩、重建并保持 provenance；
4. WriterContextPackage 是否相对 recent text/simple retrieval 提升连续性、计划遵循和文学质量。

Stage 3 只输出候选，不写 TextRoot、WorldRoot、PlanRoot 或 Commit。

## 2. 当前实现起点

当前 `main` 已有 Stage 2M WriterContext、检索、Evidence、Model admission、RunEventLog 和
content-addressed Skill/Prompt receipt。独立分支 `codex/stage3-writer-context-loop` 的 `bab4451`
已经完成：

- Writer `DRAFT / CONTINUE / MAJOR_REWRITE`、`WriterWorkPlan` 和固定 Skill 选择；
- candidate-only `DraftArtifact`、sidecar、父子 lineage 和 typed failure；
- reactive `REQUEST_MEMORY`、共享 `AgentContextView`/ContextDelta/compaction 和 checkpoint recovery；
- Writer Agent/Service、Editor Review/Repair、最终候选观察、变化对账和三方案评价 runner；
- `WriterContextPackage` handoff、endpoint admission 接线和 versioned Stage 3 schemas。

该分支报告 1893 deterministic tests、100% coverage 和 full pre-commit，工程实现已完成。尚未完成的是
最终共同 executable identity 上的合并/独立验收、真实基础设施 Gate 和正式三方案真实模型语义实验，
因此当前状态是 `CONDITIONAL_GATE`，不是生产 PASS。Stage 5 只能消费其 public candidate terminal，
不能据此授权 Writer 直接写 Canon。

## 3. Writer Memory 的专用前提

Stage 2M 当前 Memory 流程以冻结章节规划为目标：

```text
Plan obligations + target chapters
→ TaskPlanConditionedNeedGenerator
→ exact evidence retrieval / selection
→ evidence-first WriterContextPackage + EvidenceLedger
```

这正是 Stage 3 所需输入。Writer 负责阅读 evidence-first package 中的原始材料并形成当前写作理解，
Memory 不预先替 Writer 生成唯一标准 Claim。Writer 不负责重新决定全书/卷/章节集目标；如果上游没有可执行章节规划，
Stage 3 返回 `BLOCKED/MISSING_ACCEPTED_PLAN`，由 Stage 4 Planner 或人工规划解决。不得让 Writer 为了
继续生成而现场补造 PlanRoot。

`WriterContextPackage` 是初始、不可变、可审计的 Context Seed。后续补搜不会原地改写该 Package，
而是产生绑定其 basis 的 `ContextDelta` 和新的 `AgentContextView` revision。

### 3.1 最近正文是独立的确定性连续性输入

写第 N 章时，第 N-1 章完整正文不是检索候选，而是默认叙事接缝。Stage 3 使用通用
`RecentProseContext` 与 `WriterContextPackage` 分责：前者由 accepted TextRoot 机械投影，默认包含上一章
完整正文、较早近章的摘要/章尾和可展开引用；后者继续负责按 Planning Need 检索的状态、历史、关系、
披露和计划义务。两者共同进入一个 `WritingLoopRequest`，不通过外部 Hook，也不授予 Writer 任意读取 Tool。

上一章完整正文属于 mandatory Memory；较早 trail 可压缩。更详细的生产合同、当前代码断点和 Stage 2～5
纵向测试见 `docs/stage2_to_stage5_real_novel_vertical_pilot_execution.md`。

截至 2026-08-13，`ProductionWritingRequestFactory` 已关闭正式 handoff：它从当前 accepted roots 自动构造
WritingTask，调用 Stage 2M evidence-first provider，持久化 v2 WCP/EvidenceLedger，装配 RecentProse，并把
同一 exact basis/snapshot 交给 Stage 3。生产 runtime 不再允许测试 lambda 手工拼 WCP/attestation；真实模型
语义 Gate 仍未运行。

### 3.2 长运行预算是 invocation slice，不是 Writing Task 寿命

Writer 的 provider context hard window、output reserve、basis/snapshot、access scope 与前一章完整正文是
不可自动放宽的边界。`WritingLoopBudgets` 中的 reactive turn allowance 只表示一次 invocation 能做多少
工作：到达 slice 后写入 `WritingLoopCheckpoint(REACTIVE_MEMORY_PENDING)` 并返回 `YIELDED`；Stage 5 将同一
Task 恢复为 `READY`，不扣 retry budget。Checkpoint 保存已结算 WorkPlan、pending Writer turn、exact Context
View、累计模型轮次、Memory request fingerprints 和 lineage，下一 invocation 从 pending reactive Memory
继续，不重复 WorkPlan 或已结算模型调用。

Memory 报告 `BUDGET_EXHAUSTED` 时进入 `BUDGET_REVIEW`，而不是自动无限扩容；明确补充预算后仍从同一
checkpoint 继续。重复相同 request、没有新 evidence 或 provider hard window 无法安全压缩仍属于有界
no-progress/physical stop。

2026-08-13 的下一实现增量继续复用同一个 `WritingLoopCheckpoint`，把真实生产链已经存在的安全点扩展为
`EDITOR_PENDING / OBSERVER_PENDING / RECONCILIATION_PENDING`。Checkpoint 只保存已结算的 typed artifact、
exact Context View、最终候选选择和累计 ModelCall；恢复时按 phase 跳过已经结算的 Writer、Editor 或 Observer
调用。它不是任意 Agent 状态机，也不捕获 Python coroutine/local mutable state。post-draft model-call allowance
是 invocation slice：到达后 `YIELDED`，Stage 5 续发同一个 Writing Task；Editor 的 PASS/repair/rewrite Gate、
Observer 独立观察和 reconciliation Gate 均不得因恢复而省略。

## 4. Writer 正式循环

### 4.1 Preflight 与 WriterWorkPlan

Writer 开始前验证：

- WritingTask、Plan、WCP 使用同一 base commit/snapshot/profile；
- WCP `READY`，无 blocking/conflict gap 和未来泄漏；
- task target、Plan obligations、must keep/must avoid 与 WCP 绑定；
- 最终渲染 Prompt 在实际模型输入预算内。

随后生成非 Canon 的 `WriterWorkPlan`：

```text
scene / beat order
participating characters and current state
POV / epistemic and reader disclosure boundary
dialogue intent / character voice
pacing / transition / emotional movement
hook setup / advance / payoff / defer
selected writing skills
known risks and unresolved questions
```

它是本次执行方法，不是新的 ChapterPlan 或 PlanRoot。

### 4.2 Skill 使用

Writer 只可使用 `WritingTaskContract.allowed_skills` 与 ProjectProfile 固定的版本，例如：

- scene/beat realization；
- character voice and dialogue；
- POV/epistemic discipline；
- pacing and transition；
- hook/foreshadowing realization；
- style/genre method；
- continuation/major-rewrite method。

Skill 通过现有 `SkillRegistry`、AgentSpec 和 `SkillExecutionReceipt` 固定内容 hash、输入输出、完成/跳过
checkpoint 和 unresolved；Skill 不授予 Retrieval、Memory write 或 Canon 权限。

### 4.3 Reactive Memory

初始 WCP 应主动覆盖可预测需求；只有执行中出现未预料关键未知时才发起：

```text
REQUEST_MEMORY
  need/question
  purpose and blocked action
  known context
  requested evidence type
  base commit/snapshot/scope/POV/audience
  current draft/scene checkpoint
```

Runtime 先尝试 Context-local R0，再按注册合同执行 R1；需要语义裁决、多跳、冲突或充分性判断才进入
Memory Controller R2。Writer 不能指定底层通道或无限 top-k。

Controller 返回 `RESOLVED / PARTIAL / INSUFFICIENT / DENIED / BUDGET_EXHAUSTED`。只有合法结果才能
形成 `ContextDelta`；basis、POV/access、Profile 或 plan revision 改变时必须重新编译完整 Seed/View，
不能套用旧 Delta。

第一版每个 Draft 只允许有界 Memory 往返；重复同 fingerprint、无新增证据或预算耗尽时 typed
suspend/block，不能无限自问自搜。

### 4.4 Context View 与压缩

`AgentContextView` 由以下层组成：

```text
protected: system/tool policy, WritingTask, accepted Plan, mandatory constraints, author intent
memory: recent prose + WCP evidence items / exact Ledger refs + legal ContextDelta
working: WriterWorkPlan, current scene/draft state, unresolved questions
recent tail: settled model/tool batches
compacted prefix: provenance-bound summary + kept boundary
```

Memory Controller 决定语义保留，Context Compiler/View Projector 机械执行压缩。顺序为去重/替代、
compact handle、抽取式缩减、必要时带 provenance 摘要。tool call/result、thinking/tool loop、
claim/evidence、pending effect 不可拆开；mandatory 和 unresolved gap 不可被摘要伪造填补。

soft compaction 失败保持原 View；hard limit 无法安全关闭时返回 `SUSPENDED/CONTEXT_LIMIT`。原始
RunEvent/Artifact 永不删除，`context.compacted` 只改变下一次 dispatch projection。

### 4.5 候选、审阅和对账

Writer 完成 Draft 后：

1. Editor `REVIEW` 返回 `PASS / LOCAL_REPAIR / MAJOR_REWRITE`；
2. `LOCAL_REPAIR` 最多一次，按冻结 scope 修复并独立复审；
3. `MAJOR_REWRITE` 保留父 Draft，最多一次生成子 Draft 并完整复审；
4. Curator 只观察最终通过审阅的候选；
5. Writer declarations 与 Curator observation 形成 reconciliation；
6. mismatch、修复耗尽或重要决定转为 `REVIEW_REQUIRED`。

Stage 3 Curator observation 只用于候选对账，不生成或提交正式 MemoryPatch。

## 5. Hook 和运行边界

Stage 3 内部所谓 Hook 只指类型化状态转移：`REQUEST_MEMORY`、`CONTEXT_PRESSURE`、
`SCENE_SETTLED`、`DRAFT_SETTLED`。它们由服务直接 append `RunEvent`，不走 shell/HTTP Hook。

Stage 3 不建设外部 Hook ingress、Task/Attempt DB、lease、Supervisor、Temporal、通用 DAG、
Operational observation index、consolidation 或 Viewer。所有模型调用共享当前
`ModelRequestAdmissionController`。

## 6. 验收

工程验收：

- 最新 Stage 2M 主线上的严格 typing、schema、migration、tests 和 100% branch coverage；
- WCP/Plan/task/basis 强绑定，Prompt 实际 token admission 无重复 Context；
- Memory request、ContextDelta、compaction、resume 和 typed failure regression；
- full replay 与 incremental Context View 等价，provider/information properties 通过；
- Writer/Editor/Curator/Commit 权限边界保持。

语义验收使用同一 Writer 模型、参数和相近预算比较 recent text、simple retrieval、deterministic
Writer Context；正式 Runner 必须执行真实 Writer→Editor→Curator observation→reconciliation，不能用
fixture verdict/observation 代替。评价计划遵循、人物/状态/时间/关系一致性、对话声音、Hook/揭示、
修复轮次、文学质量、Memory 往返收益和 Context exposed/confirmed-use。

Stage 3 `PASS` 只表示正文候选产品闭环可用，不授权 Canon Commit 或 Stage 5 长期自治。
