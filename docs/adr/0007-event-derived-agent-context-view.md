# ADR-0007: Event-derived Agent Context View 与安全压缩合同

- Status: accepted
- Date: 2026-08-10
- Decision owners: project architecture and runtime correctness
- Related: ADR-0004、ADR-0006
- Evidence: InkOS、agentmemory 与 Long-running Runtime 固定提交源码研究

## Context

Writer 和 Planner 都需要在多次模型调用中持续加入 Memory、工具结果和新指令，并在窗口压力下压缩。
初始 Context Package 不能表达整个运行窗口；直接修改 messages、让 Agent 自行删除内容或新增第二套
session/step store 会破坏 provenance、恢复和信息边界。

## Decision

`WriterContextPackage` 和未来 `PlannerContextPackage` 是 consumer-specific 初始 Seed。
`AgentContextView` 是从 `RunEventLog`、Seed、ContextDelta、Artifact 和最新有效 compaction event
构造的可重建运行投影，不是 Canon 或新的运行事实源。

语义取舍由 Memory Controller 负责；Context Compiler/View Projector 负责机械 token 预算、结构分组、
安全 cut、压缩、渲染和最终 dispatch 校验。Agent 只能提交 `REQUEST_MEMORY`/context pressure，不能
选择底层通道或任意删除窗口内容。

压缩必须：

1. 保留原始 RunEvent 和 Artifact；
2. 保持 tool batch、thinking/tool loop、claim/evidence 和 pending effect 原子性；
3. 保护当前 task/intent/plan、mandatory constraint、信息边界和 unresolved Need；
4. 先 deterministic reduction，再 compact handle，再 provenance-bound summary；
5. 使用 basis event position、View generation 和 CAS 发布确定性 receipt；
6. soft failure no-op，hard limit 无安全结果时 typed suspend/block；
7. compaction 后使旧 provider prefix/cache identity 失效。

## Consequences

- Stage 3/4 现在实现单 Agent 窗口管理，但不因此获得长期 Task Scheduler。
- Stage 5 复用同一 View/receipt 做跨天恢复，不新增 conversation DB 或 StepStore。
- 外部 Hook 只能形成 RunEvent/OperationalObservation，不能在请求路径压缩或注入 Context。
- 全量 replay 与增量 View 必须有等价性和安全属性测试。

## Acceptance evidence

- full replay 与 incremental projection property tests；
- tool/action/result、claim/evidence、no-leak/basis safe-cut regression；
- soft no-op、hard typed suspension、旧 compactor CAS rejection；
- summary artifact provenance、deterministic receipt 和 provider prefix invalidation；
- Writer/Planner Context exposed/used receipt 能回溯到 Seed、Need、Retrieval 和 output artifact。
