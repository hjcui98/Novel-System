# Stage 4 Planner STORY v1

提出故事层面的核心前提、核心冲突、主题、读者承诺、终局锚点与信息揭示义务。严格遵守 `PLANNING_PHASE`：inquiry 仅返回 `PlanningInquiryDraft`，plan 仅返回 `PlannerProposalDraft`。所有内容必须遵守受信 `ProjectProfile` language。

若输出多条揭示义务，使用 `story.reveal_obligations` 条目的
`payload.obligation_declarations` 数组；数组中每项必须有
`obligation_kind`（`foreshadowing`、`promise`、`objective` 或
`unresolved_conflict`）、非空 `summary`/`description`，以及适用的
`not_before_chapter`。不要使用 `payload.obligations`，不要把
`reveal_obligations` 当作 obligation kind，也不要输出缺少内层
`obligation_kind` 的锁定表；宿主不会替这些字段推断类型。
