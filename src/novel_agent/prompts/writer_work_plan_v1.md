# WriterWorkPlan contract v1

为给定的已接纳写作任务创建结构化的执行方案。只能返回一个符合 `WriterWorkPlan` 结构的 JSON 对象。只能选择白名单中允许的技能 ID（Skill ids）。将情节节拍、出场角色、叙事视角（POV）、对话要点、节奏把控、悬念伏笔（hooks）、必须保留（must-keep）、必须规避（must-avoid）以及未决风险绑定到给定的任务、已接纳的规划工件以及作者上下文工件。未落地的创意设想只能放入 `creative_proposals` 中；切勿将其作为既定正史（Canon）。

【语言规范】：
计划中的创意设想、节拍分解、对话及场景构想等内容必须遵守受信 `WritingTaskContract` 与 `ProjectProfile` language，以确保后续正文写作遵循已批准的语言约束。

为当前章节任务填写 `execution_beats`：每一拍对应 `WritingTaskContract` 的一个 beat 或 scene goal，写明场景展开、阻力、选择、结果、预计篇幅（`expected_characters`）和收束点（`close_point`）。`expected_total_characters` 只用于执行安排，不是质量证明。不要另生成一份 ChapterSet。补长只能深化当前场景，不得靠新增能力、特殊装备或下一章任务填长度。

## 已接纳细纲是不可丢失的叙事骨架

当受信 `WritingTaskContract` 带有 `scene_blueprints` 时，它是**已接纳的单章细纲**：场景顺序、
每个场景的进入/退出条件、以及每个执行 beat 的 `beat_id`、动作、阻力、选择、结果、信息披露、
表达侧重、字数预算与收束点都属于已接纳计划，不是可选建议。

- `execution_beats` 的每一拍必须用 `beat_ref` 引用一个**真实存在**的 `beat_id`，
  并按细纲顺序覆盖**全部**必需 beat；遗漏、引用不存在的 ID、或用重复条目冒充覆盖都会被宿主拒绝。
- 每个 `expected_characters` 之和必须等于 `expected_total_characters`，并且落在
  `length_policy` 的合法区间内。若细纲预算与长度政策没有交集，应返回
  `unresolved_risks` 说明冲突，而不是先生成再靠截断凑长度。
- 你决定的是**怎样写出来**（表达、对白、节奏、Skill 选择），不是重新规划长期剧情：
  不得改变本章核心结果、提前兑现受时间锁保护的奖励，或把其他章的计划成果当成本章已有条件。
- `planned_participants` 是计划身份而非 Canon 实体：可以按给定 `label` 与 `narrative_role`
  写它们，但不得为它们编造 World 关系或把它们当成已经存在的历史事实。

`writing_task_ref`、`accepted_plan_ref` 与 `writer_context_ref` 字段是系统不透明的可信血缘绑定（opaque trusted lineage bindings）。必须从可信输入中的 `OPAQUE_LINEAGE_BINDING` 区块逐字节完整复制对应的 JSON 对象。该最终绑定区块是这三个字段的唯一有效来源；忽略渲染上下文中先前出现的任何工件 ID。不得计算、哈希、拼接、缩写、规范化或替换任何 `artifact_id`、`media_type`、`byte_length` 或 `schema_version`；任何不匹配都将被系统严厉拒绝。
