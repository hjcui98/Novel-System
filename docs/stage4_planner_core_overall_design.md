# Stage 4 Planner Agent 与 Planning Context Loop 总设计

> 文档生命周期：`ACTIVE`
>
> 设计状态：`DESIGN_BASELINE_2026-08-10`
>
> 开发状态：`ENGINEERING_INTEGRATED_IN_STAGE5_CANDIDATE / REAL_SEMANTIC_GATE_PENDING`
>
> 阶段：Stage 4 — Planner Agent and Planning Context Loop
>
> 上位决定：ADR-0006、ADR-0007

## 1. 设计结论

Stage 4 把作者初始设定、当前 Canon/Plan 和长期历史变成可审阅规划候选。Planner 与 Writer 是两个
独立 Agent 产品：Writer 实现已接受规划；Planner 产生、比较和修订规划。

```text
Author Intent + Planning Scope + current Plan/Canon
→ PlanningInquiry / GoalProposal / alternatives
→ Plan Reviewer
→ Planner-specific MemoryNeed
→ PlannerContextPackage / dynamic AgentContextView
→ PlanProposal / bounded review and revision
→ PLAN_CANDIDATE_READY
```

## 2. 当前实现起点

当前主线已有六模式 `PlannerAgent`、`PlanningTask`、`PlanProposal`、`ProjectIntentModel`、
`PlanDeviationRecordCandidate`、provenance、规划 Prompt/Skill、Agent/Skill receipt 和 bootstrap workflow。

Stage 4 工程候选已经收敛到 `0dcf17a`：包含 `PlanningInquiry/GoalProposal`、Planner-specific Need、
`PlannerContextPackage`、独立 Plan Review、有界 revision、Stage 4 schema/evaluation，以及共享
`AgentContextView`/ContextDelta/compaction Runtime。Stage 5 已通过公开的
`Stage4PlanningLeafAdapter` 接入该候选；它仍未完成真实七模式规划质量实验和独立 Stage 4 语义 Gate，
因此当前状态是集成候选，不是产品 PASS。

## 3. Planner Memory 不能复用 Writer 的目标生成

Stage 2M `TaskPlanConditionedNeedGenerator` 以已给定未来章节规划为目标，适合 Writer，不适合 Planner。
Stage 4 的 Memory 入口分两步：

1. Planner 根据 Author Intent、planning mode/scope、当前 Plan/Canon 和明确作者 override，提出
   `PlanningInquiry`、`GoalProposal`、候选方向及需要验证的假设；
2. `PlanningInquiryConditionedNeedGenerator` 将已审核 inquiry 转为 bounded `MemoryNeed`，再交给同一个
   Memory Controller/Retrieval/Claim Support。

Planner Memory 重点包括：全书/卷/人物弧目标、未履行义务、历史节奏与事件密度、前文状态、
Disclosure/epistemic、伏笔/回收债务、Plan deviation、作者修改和必要 Reference。任何新目标仍是
`planner_proposed`，检索结果不能自动变成 PlanRoot。

## 4. PlannerContextPackage

Planner 使用 consumer-specific `PlannerContextPackage`：

```text
planning task/mode/scope
author intent and explicit override
current accepted Plan and obligations
current World/Text state needed for feasibility
arc/volume/chapter history and deviations
selected evidence/reference/process lessons
unresolved inquiry/conflict/gaps
budget/lineage/EvidenceLedger refs
```

它与 WCP 共享 base commit/snapshot/profile、Evidence/Plan provenance、mandatory/optional、精确 token、
typed overflow 和 freeze/receipt；但不包含 Writer 的正文执行布局，也不能把 WCP 原样复用。

`PlannerContextPackage` 同样只是 Seed。Planner 多轮 inquiry、review、revision 使用共享
`AgentContextView`/ContextDelta/compaction；Plan/intent/mandatory、未解决 inquiry 和 Reviewer issues
属于 protected 内容。

## 5. Planner 与 Plan Reviewer

Planner modes：

- `PROJECT_BOOTSTRAP`
- `STORY`
- `ARC_VOLUME`
- `CHAPTER_SET`（滚动 1～3 章或配置窗口）
- `CHAPTER`
- `SCENE`
- `REPLAN`

独立 `PlanReviewerAgent` 只审核 proposal，不生成 PlanRoot，输出：

```text
ACCEPT | REVISE | HUMAN_REQUIRED
coverage / contradiction / feasibility / obligation / pacing / option issues
memory gaps that must be resolved before revision
preserve decisions and bounded revision instruction
```

第一版最多一次 reviewer-directed revision；重大作者取舍、互斥候选或无法证明可实现性进入
`REVIEW_REQUIRED/HUMAN_REQUIRED`，不做无限讨论。

## 6. 高级检索的准确落点

Stage 4 不重新建设 BM25+Dense+Graph。已有 Exact/Temporal、Anchor/Grounded BM25+Dense、Typed Graph 和
application RRF owner 保留。按 Planner inquiry 条件增加：

- graph path receipt；
- Anchor explicit entity → Typed Graph 条件扩展；
- compact result → selected evidence/path expand；
- source/path/chapter diversity；
- recent/lexical/dense/dual/graph/triple 同 corpus 消融；
- 通道失败、dimension mismatch、access/cutoff 和 no-ghost degradation tests。

所有查询不默认三路并发；精确状态和引用继续 Exact/BM25。Typed Graph 只使用 canonical/evidence edge，
depth 默认 1～2、上限 3。不复制上游全局固定权重；per-intent weights 必须由独立实验晋升。

## 7. Skill、Hook 和权限

Planner 使用 Profile-pinned bootstrap/story/arc/chapter-set/chapter/scene/replan、候选比较、义务调度、
人物弧与伏笔规划 Skill。Skill 是 Method Asset，不允许动态修改 active Skill。

内部 `REQUEST_MEMORY`、`PLAN_REVIEW_SETTLED` 和 `CONTEXT_PRESSURE` 直接写 RunEvent，不走外部 Hook。
Planner/Reviewer 不直接使用底层 Retrieval、Memory write、Commit 或 PlanRoot mutation Tool。

## 7.1 Production hierarchy and future-locked obligations (2026-09-02)

Production Planner is no longer CHAPTER_SET-only. `PlanLevel` is the structural authority
(`STORY / ARC_VOLUME / CHAPTER_SET / CHAPTER / SCENE`). `AgentMode` maps 1:1 except `REPLAN` and
`PROJECT_BOOTSTRAP`, which are actions/bootstrap, not levels. `PlanNode.node_type` remains a
literary label and never overrides `PlanLevel`.

One post-Genesis `PLAN_CANDIDATE` produces exactly one `PlanLevel`. STORY / ARC_VOLUME do not use
TaskRecord rolling `horizon_start/end`; their chapter range lives on `PlanNode.chapter_start/end`.
CHAPTER_SET keeps rolling horizon and the per-chapter ChapterGoal coverage gate.

CHAPTER_SET context no longer consumes the full raw author brief by default. Parent nodes, current
horizon goals, active obligations and future-locked obligation summaries remain visible.
`PROMISE` / `FORESHADOWING` without `not_before_chapter` are rejected at trusted Plan validation.
Resolving an obligation before `not_before_chapter` is fail-closed in Plan review/materializer and
Curator write validation.

Lookahead stays frozen (`enable_planner_lookahead=False`) for this hierarchy migration. Event-triggered
multi-level replan and typed `PlanningImpact` remain later admission.

Review input folded from
`docs/Novel-System_分层规划与渐进Skill_收敛版补丁执行设计_v2_ee8849a.md`.

## 8. 终态与验收

终态：`PLAN_CANDIDATE_READY / REVIEW_REQUIRED / SUSPENDED / BLOCKED`。

验收至少覆盖：六/七模式合同、inquiry→Need lineage、Planner Context 预算与压缩、Reviewer 独立性、
一次有界 revision、作者来源与 Planner 新提内容分离、无未来事实提升、Graph/compact-expand 消融、
Plan obligations/arc/feasibility 质量和零 PlanRoot/Canon 直接写入。

Stage 4 PASS 只表示规划候选产品闭环可用。Stage 5 才按显式接受策略把 Plan candidate 提交到 PlanRoot，
并把接受的滚动规划交给 Stage 3 Writer。
