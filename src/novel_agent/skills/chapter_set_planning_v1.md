# 滚动章节集合规划（Rolling chapter-set planning）

仅规划有界的滚动窗口（如指定的章节区间）。为每一章设定可行且清晰的章节目标与终态设想，维护跨窗口的剧情承诺，并使伏笔/回收依赖以及状态跃迁依赖明确化。在独立审校和 Stage 5 接纳之前，所有备选方案和新目标均保持为候选草案状态。所有章节目标描述必须遵守受信 `ProjectProfile` language。

每章只输出 beats、参与实体、至少一个 state change、obligation actions 和 0—3 个 history needs；规划时内部考虑本章功能、欲望/阻力、信息释放、成长反馈、结果与章尾张力，但不新增 craft enum。查看最近 3—5 章的 goal/beats，避免连续重复相同冲突机制、事件顺序或章尾。只对显式 obligation 安排 setup/progress/payoff/defer；过渡、余波和休整可以低强度，但仍须改变状态、关系、知识、位置或决定。
