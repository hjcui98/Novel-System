# arc_volume_planning 1.0.0

构建分卷与剧情弧线阶段，排期情节主线与关键承诺，验证依赖关系，保留锁定意图，并暴露未决的叙事节奏或结构抉择。所有规划内容必须遵守受信 `ProjectProfile` language。

必须覆盖 Profile 规定的全部卷范围，不能只规划开头几卷。每卷给出可支持滚动 ChapterSet 的事件阶梯；每个事件尽量说明 cause、participants、conflict、cost、state_delta、obligation_action 与 chapter_window。后卷的武器、地点和真相不得提前；若某卷无法安全规划，返回明确阻断项，不以不完整 coverage 宣称 PLAN_READY。
