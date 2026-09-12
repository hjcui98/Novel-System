# R2 执行证据（规划版本、阶段要求与内容审查）

日期：2026-09-12（+08:00）
范围：方案第 6 节 R2、第 8 节 A09–A11、A22（覆盖部分）
状态：进行中。已完成范围/图守卫与四类 coverage；阶段语义、来源权限、Editor 正文证据待续。

## 1. 已完成

### 1.1 规划图守卫（A09/A10）

`PlanRootDocument` 原先只校验节点唯一、父级存在、每章唯一 goal。补入：

| 守卫 | 拒绝对象 |
|---|---|
| 父链环（含自指） | `parent` 互相指向或指向自身，导致祖先投影无定义 |
| 同级范围重叠 | 同一 `plan_level` + 同一父级下两个节点覆盖同一章 |
| 子节点越界 | 子范围超出父范围 |

同级判定按 `(plan_level, parent_id)` 分组，因此 ARC_VOLUME 与其下的 CHAPTER_SET 可以覆盖同一章，
而同一层的两个章节组不能。证据：`tests/unit/test_plan_graph_scope_guards.py`（7 项）。

### 1.2 四类 coverage（A22 覆盖部分）

新增 `domain/planning_coverage.py`，把单一 `coverage` 浮点拆成四个独立比率，每个都带
可信分母来源与缺失清单：

| 比率 | 分母来源 | 说明 |
|---|---|---|
| `chapter_coverage` | 声明的规划窗口 | 缺章逐个点名，不再只给一个数字 |
| `author_constraint_coverage` | 编译后的作者约束根 | 只认约束自身措辞，不按类别给分 |
| `obligation_coverage` | 声明的责任表 | 只有宿主能编译的声明才计入覆盖，不可读条目进 missing |
| `history_decision_coverage` | 本提案的章节 goal | 逐章点名缺显式历史决策的章 |

**层级适配**：ARC_VOLUME 提案拥有的是章节区间而非单章。早期实现对它报出
`chapter_coverage 0/800`——一个看起来真实、实际无意义的 0。现在按 `mode` 判定适用性，
不适用时输出 `applicable=false` 且比率按 1.0 处理，不参与 complete 判定。

宿主 Review 通过 `PlanReviewDraft.coverage_evidence` 附带这四行证据，ACCEPT 路径同样保留，
因此评审记录里能直接看到“哪个分母、缺了什么”。

### 1.3 真实产物测量（v6 八卷）

```text
chapter_coverage             applicable=False 0/0   missing=0
author_constraint_coverage   applicable=True  0/1   missing=1
obligation_coverage          applicable=True  16/16 missing=0
history_decision_coverage    applicable=False 0/0   missing=0
```

作者约束缺失项是 `author-constraint.language.0`（“正文语言必须为 zh-CN”）。
**重要边界**：v6 对象库里的作者约束根只含这 1 条语言约束，而三条 advisory 引用的
“第四卷前半（301-350）不得揭露”“第五卷起正式推进”等时间锁**不在该约束根内**——
它们来自 brief 的自由文本。说明该运行的 Profile→约束编译覆盖本身不完整，
本轮不修改冻结产物，把这一点留给 v7 装配时的约束编译核对（方案 R2 第 2 项）。

### 1.4 未来锁定义务的作用域（A06）

`writer_readiness` 原先用 `obligation_active_for_chapter` 构造“本章必须携带的义务”集合。
该函数是**兑现语义**：未来锁定义务在 `not_before_chapter` 之前返回 False。于是第 1 章会把
“最早第 101 章才能兑现”的责任视为不存在——这既违反方案第 5.2 节（`not_before` 约束兑现/揭露，
不把待埋设/推进的义务从注入与检索集合删除），也让 readiness 的义务绑定检查在该处失效。

修复：新增 `obligation_in_scope_for_chapter`（作用域语义，忽略兑现锁），readiness 改用它；
`obligation_active_for_chapter` 保留兑现语义并更正文档字符串。
证据：`tests/unit/test_writer_history_retrieval_gate.py` 用真实 v6 义务形状
（只有 `not_before_chapter`、无 target 窗口）断言作用域包含、兑现仍被锁、已解决义务出作用域、
带 target 窗口时窗口外不出现在作用域内。

## 2. R2 未完成项

| 项 | 内容 |
|---|---|
| 阶段语义 | 卷入口条件/持续约束/出口结果与 acceptance criteria 分别在进入、范围内、末端检查；不每章提前兑现卷目标 |
| 来源权限 | 工作计划/指令/候选 vs 真实历史证据分开；Editor 不能用 avoided 绕过强制 Need |
| 内容审查 | Editor outcome/evidence 检查、章节目标需明确变化与完成证据 |
| scope supersession 全量 | 已做同级重叠与父子越界；重规划父卷时后代处理与已写前缀冻结待续 |

## 3. 回归与失败身份

`tests/unit tests/contract`：**76 failed / 2957 passed / 1 skipped**；
失败身份集合与整合前基线**逐项完全相同**（0 新增、0 修复）。
R2 至今新增 16 项通过测试（图守卫 7、coverage 8、作用域 1）。
