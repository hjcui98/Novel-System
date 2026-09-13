# arc_volume_planning 1.0.0

构建可执行分卷架构。严格覆盖 Profile 规定的全部卷范围（`expected_volume_count` 卷、连续覆盖 1..`target_chapters`），不能只规划开头几卷。所有规划内容必须遵守受信 `ProjectProfile` language，并遵守 Profile 的时间锁、能力/装备里程碑与地点前置条件。

每个卷 item（`kind=volume`/`arc_volume`，payload.plan_level=`arc_volume`）必须给出 chapter_start、chapter_end，并逐项填写以下非空 payload 字段：

- 十个结构槽：`opening_state`、`trigger_event`、`first_escalation`、`first_cost`、`midpoint_reversal`、`second_escalation`、`volume_climax`、`climax_cost`、`ending_state`、`next_volume_hook`；
- 弧线与上限：`protagonist_arc`、`supporting_arc`、`faction_arc`、`capability_ceiling`、`equipment_ceiling`；
- 边界与排期：`entry_conditions`、`exit_conditions`、`reveal_window`、`obligation_plan`；
- 每个结构槽尽量说明 cause、participants、conflict、cost、state_delta 与 chapter_window。

`obligation_plan` 为责任表条目列表，每条包含 `summary`、`kind`（objective/promise/foreshadowing/...）、`setup_window`、`progress_windows`、`payoff_window` 与 `not_before_chapter`。后卷的武器、地点和真相不得提前：`reveal_window` 与 `not_before_chapter` 必须与 Profile 时间锁一致。若某卷无法安全规划，返回结构化 `unresolved` 阻断项（kind 必须使用 AUTHOR_INTENT_CONFLICT / CURRENT_STATE_UNKNOWN / POWER_LEVEL_UNKNOWN / KNOWLEDGE_BOUNDARY_UNKNOWN 之一，`blocking=true`），不要以不完整 coverage 宣称 PLAN_READY。

`unresolved` 条目必须有界：若摘要中提到任何章节区间（例如“第二卷（第101-200章）”），必须同时用 `affected_chapters` 逐章声明该区间（整数列表）；宿主会把摘要里的章节窗口与 `affected_chapters` 对照，缺少声明即 `UNRESOLVED_SCOPE_MISSING` 阻断。不确定影响范围时，不要以 advisory 形式提出。

【卷阶段窗口契约】卷 item 的十个叙事键（`opening_state`、`trigger_event`、`first_escalation`、`first_cost`、`midpoint_reversal`、`second_escalation`、`volume_climax`、`climax_cost`、`ending_state`、`next_volume_hook`）必须写成结构化条目：`{"description": "...", "window": "起始章-结束章"（或单个章号）, "role": "setup|hint|progression|payoff", "serves": "<受信责任句柄>"}`；窗口必须落在本卷范围内。
- `role` 只描述该键实际做的事：`setup` 埋设（只种下状态、代价或伏笔，不向读者透露被锁信息）、`hint` 暗示、`progression` 正式推进、`payoff` 兑现。
- 凡 `role` 不是 `setup` 的条目必须填写 `serves`，取值只能来自宿主在 AUTHOR_CONSTRAINTS 中列出的句柄（形如 `[lock.xxx]` 或 `[author-constraint.xxx]`）或已接纳义务 id；不得自造 id。
- 时间边界只来自该条目自己声明的责任：`hint`/`payoff` 受 `reveal_window` 约束，`progression`/`payoff` 受 `timeline_locks`/`progression_locks`/`equipment_locks`/`location_preconditions` 约束。起始章不得早于该责任的 `not_before_chapter`；本卷或其他责任的边界不适用于它。
- 把实质揭露改标成 `setup` 不会通过：审校器按 `description` 的正文语义判断该键是埋设还是泄底，而不是只看 `role` 标签。
宿主以 `VOLUME_STAGE_WINDOW: <卷item>.<叙事键>.<字段>: ...` 逐条阻断，并直接点名需要修改的字段路径。
