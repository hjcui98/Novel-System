# 滚动章节集合规划（Rolling chapter-set planning）

仅规划有界的滚动窗口（如指定的章节区间）。为每一章设定可行且清晰的章节目标与终态设想，维护跨窗口的剧情承诺，并使伏笔/回收依赖以及状态跃迁依赖明确化。在独立审校和 Stage 5 接纳之前，所有备选方案和新目标均保持为候选草案状态。所有章节目标描述必须遵守受信 `ProjectProfile` language。

每章输出 beats、参与实体、至少一个 state change、obligation actions，以及显式 `history_retrieval` 决策：第 2 章以后必须给出 `requirement`（REQUIRED/NOT_REQUIRED）；REQUIRED 时列 1—3 个 Need（kind 仅限 causal_history、knowledge_origin、relationship_origin、setup_evidence、object_origin），NOT_REQUIRED 时给出 reason_code 与 waiver_ref。裸 `history_needs: []` 或其它 kind 一律视为未决并被审校拒绝。规划时内部考虑本章功能、欲望/阻力、信息释放、成长反馈、结果与章尾张力，但不新增 craft enum。查看最近 3—5 章的 goal/beats，避免连续重复相同冲突机制、事件顺序或章尾。只对显式 obligation 安排 setup/progress/payoff/defer；过渡、余波和休整可以低强度，但仍须改变状态、关系、知识、位置或决定。

## V2 复合提案（1+N）

一个章集不是“N 个章目标的批量提交”，而是一个有独立剧情内容的窗口。输出恰好一个
`chapter-set.v2` 父项（窗口总纲、戏剧问题、入口处理、有顺序的 plot turns、逐章职责分配、
引入责任、元素动作、出口目标、不得提前兑现项）和恰好 N 个 `chapter.v2` 章纲子项。

父子对应必须同时成立：范围上父项 `chapter_assignments` 恰好覆盖 horizon 且不重不漏；
任务上父项每个必需转折都有具体子章承接，且每个子章说明自己承担的父级任务；因果上本章依赖
的条件要么有已提交 Canon 证据，要么明确由本窗口更早章节建立，要么由本章更早场景建立。
不得把“计划以后会建立”写成“现在已经具备”。

“完整”不等于每层重复同一段摘要。把父项摘要拆成十句同义句仍然不合格；每个子章必须给出
这一章独有的叙事功能、必需节拍、进入状态依赖、预期变化与下一章接口。
