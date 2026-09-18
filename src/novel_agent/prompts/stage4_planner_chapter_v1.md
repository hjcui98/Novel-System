# Stage 4 Planner CHAPTER v1

将卷规划分解为章节级目标、必要节拍与悬念义务。严格遵守 `PLANNING_PHASE`：inquiry 仅返回 `PlanningInquiryDraft`，plan 仅返回 `PlannerProposalDraft`。所有章节目标必须遵守受信 `ProjectProfile` language。

## CHAPTER 深化：outline → execution

CHAPTER 任务是**同一章目标的受控深化**，不是第二个并存的章节节点。宿主从当前 PlanRoot 恢复
该章的合法身份：`goal_id`（即 `item_id`）、`chapter_index` 与 `chapter_set` 父级都由宿主给出，
模型不得改用正则猜章节号，也不得在未给父级时落到 STORY 默认父级。

- `payload.contract_version` 必须是 `"chapter.v2"`。
- 深化到 `detail_level: "execution"` 时必须给出有序 `scenes`；每个 scene 含 `scene_id`（Plan 局部身份）、
  `narrative_task`、`location`、`pov`、`participants`、`entry_condition`、`exit_condition`、
  `beats` 与 `budget_characters`。
- 每个执行 beat 含 `beat_id`、`parent_beat_refs`（引用本章必需章级 beat，**不得引用不存在的 ID**）、
  `action`、`resistance`、`choice`、`outcome`、`information_revealed`、`prose_focus`、
  `budget_characters` 与 `close_point`。只填字数的 beat 不是细纲。
- 每个必需章级 beat 都必须至少被一个执行 beat 覆盖；遗漏、重复冒充覆盖、或引用未知父 beat 都会被拒绝。
- 允许补充场景、表达安排与历史依赖的新证据；**修改原章核心结果、提前兑现奖励、改变父级任务或
  跨章调序需要明确的修订授权**。
- 内在认知、情绪或关系的推进也可以是有效结果，不必每个节拍都制造外部状态变化，但必须说明其叙事功能。
- 字数预算是软分配：场景与节拍预算之和必须与整章长度政策有交集；没有交集时应在正文调用前修订细纲，
  而不是生成后硬截断。
