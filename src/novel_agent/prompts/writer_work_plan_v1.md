# WriterWorkPlan contract v1

为给定的已接纳写作任务创建结构化的执行方案。只能返回一个符合 `WriterWorkPlan` 结构的 JSON 对象。只能选择白名单中允许的技能 ID（Skill ids）。将情节节拍、出场角色、叙事视角（POV）、对话要点、节奏把控、悬念伏笔（hooks）、必须保留（must-keep）、必须规避（must-avoid）以及未决风险绑定到给定的任务、已接纳的规划工件以及作者上下文工件。未落地的创意设想只能放入 `creative_proposals` 中；切勿将其作为既定正史（Canon）。

【语言规范】：
计划中的创意设想、节拍分解、对话及场景构想等内容必须遵守受信 `WritingTaskContract` 与 `ProjectProfile` language，以确保后续正文写作遵循已批准的语言约束。

`writing_task_ref`、`accepted_plan_ref` 与 `writer_context_ref` 字段是系统不透明的可信血缘绑定（opaque trusted lineage bindings）。必须从可信输入中的 `OPAQUE_LINEAGE_BINDING` 区块逐字节完整复制对应的 JSON 对象。该最终绑定区块是这三个字段的唯一有效来源；忽略渲染上下文中先前出现的任何工件 ID。不得计算、哈希、拼接、缩写、规范化或替换任何 `artifact_id`、`media_type`、`byte_length` 或 `schema_version`；任何不匹配都将被系统严厉拒绝。
