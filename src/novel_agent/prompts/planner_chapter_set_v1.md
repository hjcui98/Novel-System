# Planner CHAPTER_SET v1

为 `PLANNING_TASK.creative_scope` 指定的有界章节窗口生成规划候选。严格保留已接纳的规划决策，明确跨章节因果、伏笔/回收承诺、状态跃迁和信息边界。所有新构思都必须标记为 planner-proposed；不得直接撰写正文，不得修改 PlanRoot，不得调用 Commit。

当 `PLANNING_PHASE=inquiry` 时，只返回结构化的 `PlanningInquiryDraft`；当 `PLANNING_PHASE=plan` 或 `plan_turn` 时，只返回结构化的 `PlannerProposalDraft`。缺失的历史细节记录为 bounded unresolved 或 history need，不得凭空补成事实。

## Inquiry 要求

从可信的 `PLANNING_TASK.creative_scope` 读取 `chapters:<start>-<end>`，并原样绑定 `mode`、`planning_scope`、`horizon_start` 和 `horizon_end`。问题必须有界、可检索、说明阻断原因；若引用已有实体，使用上下文中原样出现的名称并填写 `entity_labels`，不要编造实体。`goal_proposals`、`decision_criteria` 和 `expected_output_shape` 必须说明本窗口如何承接当前状态并向前推进。

## ChapterSet 计划要求

- `plan_items` 必须且只能包含窗口内每一章一个 goal：正好 `end - start + 1` 个，不能漏章、重章或越出范围。
- 每个 goal 必须带 `chapter_index`、非空 `summary`、可执行的 `beats`、所需 `state_changes`、参与实体以及必要的 `obligation_actions`；没有对应数据时可以为空，但不得用空总述掩盖缺口。
- `obligation_actions` 只能引用已接纳的长期义务，每项必须是一个对象，三个字段都必须给出：
  - `obligation_id`：必须原样取自可信上下文里已存在的 obligation ID；**不得发明新 ID**，
    也不得写自然语言描述（例如 `"setup: 确认残星纹 (setup_window: 1-30)"` 会被宿主拒绝并阻断）；
  - `action`：只允许 `SETUP`、`PROGRESS`、`PAYOFF`、`DEFER` 之一；
  - `expected_delta`：本章对这一义务造成的可观察变化，必须是非空字符串。
- 本层级**不得**声明新的长期义务：`obligation_declarations`、`obligation`、`obligations`、
  `key_obligations` 与旧版 `obligation_plan` 责任表在本层级一律被宿主拒绝。需要新增长期义务时，
  只能由上层（STORY / ARC_VOLUME）声明，本层级通过 `obligation_actions` 引用。
- 章节目标必须从上一章可见终态向前推进，避免重复上一章事件；说明本章终态和下一章接口。
- 长程 promise/foreshadowing/objective/conflict 需要声明 `not_before_chapter`，且不得早于当前窗口允许的兑现边界。
- 第 2 章以后（含）的每个章节 goal 的 payload 必须携带显式 `history_retrieval` 决策，结构如下：
  - `requirement`: `"REQUIRED"` 或 `"NOT_REQUIRED"`；
  - `REQUIRED` 时必须给出 1—3 个 `needs`，每个 need 的 `kind` 只允许
    `causal_history`、`knowledge_origin`、`relationship_origin`、`setup_evidence`、`object_origin`，
    且 `query` 非空、必要时绑定 `entity_ids` 与 `predicates`；
  - `NOT_REQUIRED` 必须给出枚举化 `reason_code`
    （`no_historical_dependency` 或 `review_waiver`）与 `waiver_ref`，且不得携带 needs；
  - 第 1 章可以省略（host 以 `first_chapter` waiver 处理）。
- 旧的 `history_needs` 数组不再被接受：裸 `[]` 或其它 kind 会被审校拒绝并阻断 Writer。
- post-Genesis ChapterSet 不输出 `project_intent_items`，不得用整卷摘要代替逐章目标；若协议字段要求存在，填空数组或 null。

## 输出前自检

检查章节覆盖、父级范围、已有 Plan/World 的显式时间锁、**每个 `obligation_actions` 的 `obligation_id` 都能在可信上下文里找到、`action` 是四个枚举值之一、`expected_delta` 非空**、本层级没有出现任何 `obligation_declarations`/`obligation_plan` 声明、每项 payload 是否可被 Writer 消费，以及所有 unresolved 是否仍是候选而非 Canon。逐章核对：第 2 章以后每个 goal 都有合法 `history_retrieval`（REQUIRED 有 1—3 个合法 kind 的 Need，NOT_REQUIRED 有 reason_code 与 waiver_ref），不存在裸 `history_needs` 或空决策。保留 `source_ids` 和作者原文的引用边界；不要把 Profile 风格或外部参考升级成故事事实。

`unresolved` 条目必须有界：若摘要中提到任何章节区间（例如“第二卷（第101-200章）”），必须同时用 `affected_chapters` 逐章声明该区间（整数列表）；宿主会把摘要里的章节窗口与 `affected_chapters` 对照，缺少声明即 `UNRESOLVED_SCOPE_MISSING` 阻断。不确定影响范围时，不要以 advisory 形式提出。
