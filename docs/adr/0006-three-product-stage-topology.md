# ADR-0006: Stage 3 Writer、Stage 4 Planner 与 Stage 5 长期运行拓扑

- Status: accepted
- Date: 2026-08-10
- Decision owners: project architecture and delivery governance
- Supersedes: ADR-0005 中 Stage 3～7 的名称和交付顺序
- Preserves: Stage 0～2A/2R/2W/2M 的名称、Gate 与历史证据

## Context

旧编号把 Writer、Advanced Retrieval、完整章卷循环、长期自治和 Skill 演化串成 Stage 3～7。
实际产品边界是两个可以共享 Memory 底座并行开发的创作 Agent，以及一个在二者稳定后才成立的长期
运行平台。继续按旧序列会让 Planner 被 Writer 语义阻塞，也会把上下文窗口管理和跨天任务调度混为
一件事。

## Decision

| Stage | Canonical name | Output |
|---|---|---|
| Stage 3 | Writer Agent and Writing Context Loop | candidate-only Draft、Editor/Curator 对账和 typed terminal |
| Stage 4 | Planner Agent and Planning Context Loop | candidate-only PlanProposal、独立 Plan Review 和 typed terminal |
| Stage 5 | Long-running Creative Runtime | Plan/Write/Commit 固定拓扑、durable task、调度、恢复、维护和受控演化 |

Stage 3 使用给定章节/场景规划条件化的 Stage 2M Writer Memory。Stage 4 先由 Planner 提出
PlanningInquiry/GoalProposal，再生成规划专用 MemoryNeed；它不得直接复用
`TaskPlanConditionedNeedGenerator` 或 `WriterContextPackage`。

Stage 3 与 Stage 4 在共享 `MemoryNeed`、Context View、ContextDelta、compaction、Skill receipt 和
RunEvent 边界冻结后，可以在独立 worktree 并行开发。Stage 5 在两者分别通过 Gate 后开始集成。

## Consequences

- 原 Stage 4 的 Reactive Memory/ContextDelta 变为 Stage 3/4 共享调用协议；检索 owner 仍在 Stage 2。
- 原 Stage 5 的规划进入新 Stage 4，正文链进入新 Stage 3，集成与提交进入新 Stage 5。
- 原 Stage 6 长期自治和原 Stage 7 受控演化进入 Stage 5 的渐进工作包。
- 旧文档、分支、报告和 artifact 中的 Stage 4～7 名称保留为历史标识，不原地改写证据。
- Stage 5 不是提前批准 Temporal、通用 DAG、Hook 平台或第二运行事实源。

## Acceptance evidence

- Stage 3/4 能从同一 Stage 2 basis 独立运行，且无共享文件所有权冲突；
- Writer Need 绑定已接受规划，Planner Need 绑定 inquiry/goal artifact；
- 两个阶段都只输出 candidate，不直接推进 Canon；
- Stage 5 前不存在跨任务 lease/scheduler 的虚假 caller。
