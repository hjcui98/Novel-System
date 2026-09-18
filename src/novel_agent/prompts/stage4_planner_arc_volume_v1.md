# Stage 4 Planner ARC_VOLUME v1

将已接纳的故事方向细化为完整分卷架构、角色与关系弧线、情节线推进阶段与承诺排期。严格遵守 `PLANNING_PHASE`：inquiry 仅返回 `PlanningInquiryDraft`，plan 仅返回 `PlannerProposalDraft`。所有内容必须遵守受信 `ProjectProfile` language。

当 `SOURCE_DATA` 提供受控修订指令和 `REVISION_PARENT_SCOPE` 时，这是一个有界修订请求，不是重新复制或另起一份方案：只输出 scope 点名的 item/字段，未点名内容由宿主从父候选逐字恢复。每个被点名的字段必须产生具体、可观察的内容变化；不得原样返回被引用的缺陷文本，也不得因为不确定而返回未变化的目标字段。对结构化叙事键，只改被授权字段。宿主仍会独立组合并核验范围，模型不得把范围文字当作写入权限。

ARC_VOLUME（包括有界修订）的顶层输出只使用 `mode`、`plan_items`、`unresolved` 和
`coverage`；必须显式返回 `project_intent_items: []`、`world_design_items: []`、
`profile_items: []` 与 `strategy: null`。每个卷可使用 `kind: "volume"` 或
`kind: "arc_volume"`，但其 payload 必须声明 `plan_level: "arc_volume"`。不要把卷
放入 bootstrap 专用的 `project_intent_items`，不要输出 bootstrap intent 或 strategy。

硬性结构要求：每个卷 item 必须填写 `opening_state`、`trigger_event`、`first_escalation`、`first_cost`、`midpoint_reversal`、`second_escalation`、`volume_climax`、`climax_cost`、`ending_state`、`next_volume_hook`、`protagonist_arc`、`supporting_arc`、`faction_arc`、`capability_ceiling`、`equipment_ceiling`、`entry_conditions`、`exit_conditions`、`reveal_window`、`obligation_plan`、`chapter_set_roadmap` 全部非空；卷范围必须连续覆盖 1..`target_chapters` 且数量等于 `expected_volume_count`。无法安全规划时返回结构化 `unresolved`（blocking=true，kind 取自 AUTHOR_INTENT_CONFLICT / CURRENT_STATE_UNKNOWN / POWER_LEVEL_UNKNOWN / KNOWLEDGE_BOUNDARY_UNKNOWN），不要以不完整 coverage 宣称 PLAN_READY。

【卷级粗章集路线图】每个卷 item 还必须填写 `chapter_set_roadmap`：把**本卷自己的章节范围**
切成连续、有序、无缺口也无重叠的粗剧情块。它是**未来意图的粗路线图**，不是已经细化、已经
发生或已经接纳的逐章剧情；正式 CHAPTER_SET 仍按当前滚动窗口实例化。

- 每项是对象：`slot_id`（稳定局部 ID）、`chapter_start`、`chapter_end`、
  `plot_summary`（这一段连续情节的起因/行动/对抗/转折/结果，不得是“推进剧情”之类的占位标签）、
  `key_cast`（这一段的关键出场）、`element_refs`（这一段要用到的元素/伏笔/义务引用）、
  `expected_turn`（这一段结束时局势发生什么变化）。
- 段落必须从本卷第一章开始、到本卷最后一章结束，前一段的 `chapter_end + 1` 必须等于后一段的
  `chapter_start`；不得留洞、不得重叠、不得越出本卷范围。
- 段落边界必须尊重真实卷内阶段，不能把任意节拍硬切成整齐的等长块。
- 多段路线图不得每段都用同一句 `plot_summary` 充数；宿主会按区间连续性、覆盖完整性和
  内容重复逐项校验。
- 路线图里出现的未来事件、人物关系与奖励**都不是当前既有事实**：不要把它们写成 World relation，
  也不要让后续窗口把它们当成已经发生。当前窗口只细化自己覆盖的未完成后缀。

`obligation_plan` 的每条责任必须包含非空 `owner_ids`，且只能使用 SOURCE_DATA 中逐字存在的 canonical World entity ID；名称、`planner-context.*` 展示句柄和自造 ID 都不合法。`owner_ids` 表示必须持续追踪其状态以判断该责任是否设立、推进或兑现的叙事主体/检索锚点；奖励授予者、导师、信息持有者、地点或偶然参与者不是 owner，除非该实体自身的持续状态就是责任的一部分。多条责任可以基于事实绑定同一主体，不得为了多样性改绑其他实体。

`unresolved` 条目必须有界：若摘要中提到任何章节区间（例如“第二卷（第101-200章）”），必须同时用 `affected_chapters` 逐章声明该区间（整数列表）；宿主会把摘要里的章节窗口与 `affected_chapters` 对照，缺少声明即 `UNRESOLVED_SCOPE_MISSING` 阻断。不确定影响范围时，不要以 advisory 形式提出。

`unresolved` 必须输出结构化生命周期操作，不得只写自由文本：每项填写
`operation`（`ADD`/`MODIFY`/`CLOSE`）、`kind`、`summary`、`affected_chapters`、
`resolution_owner`、`allowed_assumptions`、`forbidden_assumptions`、`source_ids` 与
`source_artifact_refs`。`MODIFY`/`CLOSE` 必须引用宿主先前返回的 `parent_issue_id`；
`CLOSE` 还必须填写 `closure_reason`。不要输出 `issue_id`，稳定 ID 由宿主依据
`kind + affected_chapters + resolution_owner + source_ids` 派生；不得以摘要、数组下标或
整份输出摘要冒充身份，也不得擅自关闭、删除或重开未知问题。

【卷阶段窗口契约】卷 item 的十个叙事键（`opening_state`、`trigger_event`、`first_escalation`、`first_cost`、`midpoint_reversal`、`second_escalation`、`volume_climax`、`climax_cost`、`ending_state`、`next_volume_hook`）必须写成结构化条目：`{"description": "...", "window": "起始章-结束章"（或单个章号）, "role": "setup|hint|progression|payoff", "serves": "<受信责任句柄，可省略>"}`；窗口必须落在本卷范围内。
- `role` 描述该键实际做的事：`setup` 埋设（只种下状态、代价或伏笔，**不描述任何指向被锁真相的异常、疑点或暗示**）、`hint` 暗示（任何让读者怀疑被锁真相的现象，即使无法解释，也属于 hint，必须标为 hint）、`progression` 正式推进、`payoff` 兑现。
- `serves` 只在**该键确实服务于某个受信责任**时填写，取值必须逐字来自 AUTHOR_CONSTRAINTS 中列出的句柄（去掉方括号，例如 `lock.long-truth.vol4-hint`）或已接纳义务 id；不得自造 id，也不得为凑字段引用无关约束（例如语言约束）。
- **被引用的责任即为权威**：只要填写了 `serves`，该键的起始章就必须不早于该责任的 `not_before_chapter`，与 `role` 标签无关；其他责任的边界不适用。因此把暗示写成 `progression` 不能绕过边界。
- `hint` 与 `payoff` 必然披露被锁信息，必须填写 `serves`；`setup` 可省略（埋设允许早于边界）；`progression` 仅在其推进确有作者约束时填写，否则省略。
- 把实质揭露改标成 `setup` 不会通过：审校器按 `description` 的正文语义判断该键是埋设还是泄底，而不是只看 `role` 标签。
宿主以 `VOLUME_STAGE_WINDOW: <卷item>.<叙事键>.<字段>: ...` 逐条阻断，并直接点名需要修改的字段路径。
- `role` 描述该键实际做的事：`setup` 埋设（只种下状态、代价或伏笔，不向读者透露被锁信息）、`hint` 暗示、`progression` 正式推进、`payoff` 兑现。
- `serves` 只在**该键确实服务于某个受信责任**时填写，取值必须逐字来自 AUTHOR_CONSTRAINTS 中列出的句柄（去掉方括号，例如 `lock.long-truth.vol4-hint`）或已接纳义务 id；不得自造 id，也不得为凑字段引用无关约束（例如语言约束）。
- **被引用的责任即为权威**：只要填写了 `serves`，该键的起始章就必须不早于该责任的 `not_before_chapter`，与 `role` 标签无关；其他责任的边界不适用。因此把暗示写成 `progression` 不能绕过边界。
- `hint` 与 `payoff` 必然披露被锁信息，必须填写 `serves`；`setup` 可省略（埋设允许早于边界）；`progression` 仅在其推进确有作者约束时填写，否则省略。
- 把实质揭露改标成 `setup` 不会通过：审校器按 `description` 的正文语义判断该键是埋设还是泄底，而不是只看 `role` 标签。
宿主以 `VOLUME_STAGE_WINDOW: <卷item>.<叙事键>.<字段>: ...` 逐条阻断，并直接点名需要修改的字段路径。
- `role` 只描述该键实际做的事：`setup` 埋设（只种下状态、代价或伏笔，不向读者透露被锁信息）、`hint` 暗示、`progression` 正式推进、`payoff` 兑现。
- `serves` 只在**该键确实服务于某个受信责任**时填写，取值必须逐字来自 AUTHOR_CONSTRAINTS 中列出的句柄（去掉方括号，例如 `lock.long-truth.vol4-hint`）或已接纳义务 id；不得自造 id，也不得为了填字段而引用无关约束（例如语言约束）。
- `hint` 与 `payoff` 会向读者披露被锁信息，必须填写 `serves`；`setup` 可省略；`progression` 仅在其推进的能力/装备/地点/时间锁确有作者约束时才填写，否则省略。
- 时间边界只来自该条目 `serves` 指向的责任：`hint`/`payoff` 受 `reveal_window` 约束，`progression`/`payoff` 受 `timeline_locks`/`progression_locks`/`equipment_locks`/`location_preconditions` 约束。起始章不得早于该责任的 `not_before_chapter`；其他责任的边界不适用于它。
- 把实质揭露改标成 `setup` 不会通过：审校器按 `description` 的正文语义判断该键是埋设还是泄底，而不是只看 `role` 标签。
宿主以 `VOLUME_STAGE_WINDOW: <卷item>.<叙事键>.<字段>: ...` 逐条阻断，并直接点名需要修改的字段路径。
- `role` 只描述该键实际做的事：`setup` 埋设（只种下状态、代价或伏笔，不向读者透露被锁信息）、`hint` 暗示、`progression` 正式推进、`payoff` 兑现。
- 凡 `role` 不是 `setup` 的条目必须填写 `serves`，取值只能来自宿主在 AUTHOR_CONSTRAINTS 中列出的句柄（形如 `[lock.xxx]` 或 `[author-constraint.xxx]`）或已接纳义务 id；不得自造 id。
- 时间边界只来自该条目自己声明的责任：`hint`/`payoff` 受 `reveal_window` 约束，`progression`/`payoff` 受 `timeline_locks`/`progression_locks`/`equipment_locks`/`location_preconditions` 约束。起始章不得早于该责任的 `not_before_chapter`；本卷或其他责任的边界不适用于它。
- 把实质揭露改标成 `setup` 不会通过：审校器按 `description` 的正文语义判断该键是埋设还是泄底，而不是只看 `role` 标签。
宿主以 `VOLUME_STAGE_WINDOW: <卷item>.<叙事键>.<字段>: ...` 逐条阻断，并直接点名需要修改的字段路径。
