# 《余烬九序》生产交接（2026-09-02）

本文件记录 `main` 上尚未推送的生产代码、当前跑书状态，以及流程上仍未实现的缺口。
不包含父工作区 `codex/stage2m-semantic-closure` 的 ZTJ 改动，也不包含
`yujin-jiuxu/` 对象库、Postgres 与导出正文。

## 1. 本提交改了什么

围绕 800 章 AUTO 跑书，把已经踩到的运行时卡点补进现有 Stage 4/5 路径，不新建规划层级。

| 主题 | 行为 |
|---|---|
| Genesis | Curator 空抽取拒绝、非法 citation 回写、CHAPTER_SET 可从 Reference 取 author-intent |
| Writer | 一章可合并多条 `chapter_goal`；生产 Writer `enable_thinking=False` |
| 结算 | `settlement_token_budget` 可覆盖；Guardian 用 Curator `revealed_text`；图抽取超时后重试用 `.retry.{attempt}` 新请求身份 |
| 调度 | 过期 lookahead 不再占住前台；AUTO 下 Planner `BUDGET_REVIEW` 最多自动加 3 档 Memory |
| Planner Memory | SUPPORTED 后给名字加【】不再当成新问题，走已有 `PLAN_READY` fallback |
| 操作 | PLAN/DRAFT_COMMIT 的 `unblock` 清零 `writer_generation` |

配套测试在 `tests/unit/test_creative_runtime_recovery.py`、
`test_stage4_planning_loop_and_evaluation.py`、
`test_stage5_creative_runtime_edges.py` 等。

跑书工作区 `/home/cuihengjia/agent/novel/NS/yujin-jiuxu` 不在本 git 树：
`state/runs.json` 已设 `settlement_token_budget=128000`、`settlement_timeout_seconds=120`；
正文导出在 `yujin-jiuxu/output/`。

## 2. 当前跑书状态（提交时）

- 项目 `project.yujin-jiuxu`，run `run.yujin-jiuxu.v1`，策略 AUTO，`planning_horizon=5`
- 第 1–23 章已 commit；第 21 章曾因图抽取 60s 超时 `effect_uncertain`，unblock 后已提交
- 停在 `plan.24-28`：`blocked` / `leaf_review_required`
- watch 收据 `task identity collision`：blocked 的 `plan.24-28` 被排除出「活后继」，恢复逻辑又创建同一 task id

`plan.24-28` 内容：inquiry 把剧情写回断星六号 / 再升银铭 / 再进内府；Planner 改问 ER-07 之后的碎片、修为、队友；Memory Reviewer 给 `revise` 而非 `accept`，循环以 `PLANNER_MEMORY_REVIEW_NOT_ACCEPTED` 停成 blocked。

## 3. 现在实际流程

设计有 `STORY / ARC_VOLUME / CHAPTER_SET / CHAPTER / SCENE`。本 run 只用滚动 `CHAPTER_SET`：

```text
Genesis（一次）：brief → Curator World/Profile/Reference
                → Planner PROJECT_BOOTSTRAP（1–5 章）→ 人审 commit

之后每章：
  已 commit 章 N
  → plan.(N+1)-(N+5) 重新 inquiry 5 个 goal
  → Reviewer / REQUEST_MEMORY / PlanProposal
  → AUTO 接受进 PlanRoot
  → Writer 只写 N+1 的 chapter_goal
  → AUTO 接受 → Curator 结算 TextRoot/World
  → 窗口前移 1 章
```

没有卷任务，没有冻结的 100 章卷纲。Writer 看不到卷义务。计划与草稿均自动接受。

## 4. 未实现 / 流程缺口

1. **没有卷 → 段 → 章。** `ARC_VOLUME` 未进入生产调度。5 章 inquiry 把大纲里的「第 N 卷末」当成眼前窗口，第 20 章已写「第二卷终章、银铭到手」。大纲是 800 章、八卷 × 100 章。
2. **brief 只有设定和稀疏卷节点**，没有 100 章节拍。滚动 Planner 每次重读大纲，会和已写正文打架（21–25、24–28 都把人拉回断星六号）。
3. **章长短于合同。** Writer 目标 3000–5000 字，已导出章多数 1100–2300 字，第 17 章约 520 字。长度门未拦住提交。
4. **blocked 规划会撞 task id。** `_repair_post_draft_projection` 把 `BLOCKED` 当成「没有后继」，再 `create_task` 同一 `plan.A-B`，dispatch 以 `RuntimeCommandConflictError` 退出。
5. **结算图抽取默认 60s 偏紧**（Writer 120s）。本提交让 run 可覆盖 timeout，并给 retry 新 identity；未改默认、未做卷规划。
6. **导出不是流水线步骤。** `output/` 需人工从 TextRoot 抽 markdown。

要续跑第 24 章，需先处理 blocked 的 `plan.24-28`（不能只重启 watch）。要解决「20 章过完一卷」，需要生产先提交并遵守卷/大段 Plan，滚动 5 章只能填细节。
