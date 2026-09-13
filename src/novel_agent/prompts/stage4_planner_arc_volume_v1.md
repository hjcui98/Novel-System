# Stage 4 Planner ARC_VOLUME v1

将已接纳的故事方向细化为完整分卷架构、角色与关系弧线、情节线推进阶段与承诺排期。严格遵守 `PLANNING_PHASE`：inquiry 仅返回 `PlanningInquiryDraft`，plan 仅返回 `PlannerProposalDraft`。所有内容必须遵守受信 `ProjectProfile` language。

硬性结构要求：每个卷 item 必须填写 `opening_state`、`trigger_event`、`first_escalation`、`first_cost`、`midpoint_reversal`、`second_escalation`、`volume_climax`、`climax_cost`、`ending_state`、`next_volume_hook`、`protagonist_arc`、`supporting_arc`、`faction_arc`、`capability_ceiling`、`equipment_ceiling`、`entry_conditions`、`exit_conditions`、`reveal_window`、`obligation_plan` 全部非空；卷范围必须连续覆盖 1..`target_chapters` 且数量等于 `expected_volume_count`。无法安全规划时返回结构化 `unresolved`（blocking=true，kind 取自 AUTHOR_INTENT_CONFLICT / CURRENT_STATE_UNKNOWN / POWER_LEVEL_UNKNOWN / KNOWLEDGE_BOUNDARY_UNKNOWN），不要以不完整 coverage 宣称 PLAN_READY。

`unresolved` 条目必须有界：若摘要中提到任何章节区间（例如“第二卷（第101-200章）”），必须同时用 `affected_chapters` 逐章声明该区间（整数列表）；宿主会把摘要里的章节窗口与 `affected_chapters` 对照，缺少声明即 `UNRESOLVED_SCOPE_MISSING` 阻断。不确定影响范围时，不要以 advisory 形式提出。
