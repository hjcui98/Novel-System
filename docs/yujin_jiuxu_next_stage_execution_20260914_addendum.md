# 《余烬九序》接续执行记录（2026-09-14）

本记录追加于执行期间，保留旧指导、旧运行和旧对象；不把诊断运行改写成正式验收。

## 身份与基线

- 修复工作树：`/home/cuihengjia/agent/novel/NS/.worktrees/yujin-unified-remediation`。
- 分支：`codex/yujin-unified-remediation`。
- 真实恢复切片所用的可执行源码 SHA：`43f9e14833203947eb361b0ab96a0007b377a277`；其后宿主范围漏检修复为 `d36ce99`，该修复尚未回写到 v25 的已生成候选，避免把不同源码身份混成一次正式运行。
- v24 冻结配置记录的旧源码为 `8e4b063…`；该差异继续作为历史，不复用为当前验收身份。
- v25 使用独立 project/run、object store、state、receipts、logs 和 output；其逻辑 Genesis basis 为 `sha256:851fcfdae3c4d874386f4d700723881ce481937e28485de50cbd6e061baa1f25`。在最终正式阶段开始前仍需以最终源码 SHA 再次冻结并核对，v25 早于 `43f9e14` 的 Genesis 过程不单独充当最终正式冻结证明。
- 真实服务预检：8003 `/health` 和 `/v1/models` 均 HTTP 200，实际模型 `qwen38-27b-nvfp4`；使用的 profile 为 `qwen38_27b_nvfp4_8003`。检索配置沿用 `real_hybrid`、OpenSearch 9200、embedding 8081、reranker 8082；凭证未写入记录。

## N0—N4 与 D0

- N1/N2 的已完成路径复用既有证据；当前增量修复为 `c745a82`、`8eb6e4b`、`d6025c6`、`4cdbb0c`、`43f9e14`、`d36ce99`。`43f9e14` 把 Planner turn 的 provider 草稿和提示词约束接到真实 `run_turn` 边界：模型不得提供 `issue_id`，宿主负责 ADD 身份，`parent_issue_id` 仅服务于授权 MODIFY/CLOSE。`d36ce99` 又把同源候选条目的显式窗口接入宿主 unresolved 范围审校，仅作为缺失范围证据，不替问题填写范围。
- `d36ce99` 后的本 SHA 定向确定性集合（引用核验、义务窗口、合成来源证明、结构化 unresolved、attempt/ledger 恢复、阶段停止、Planner/Stage4 契约）结果为 **269 passed**；模型调用被禁止。改动文件 Ruff、格式和 `git diff --check` 均通过。全仓 `make quality` 仍受 51 个既有 Ruff 问题阻断，未把它写成整体质量通过。
- 在上述集合基础上，`6a56ec0` 将恢复前沿固定为当前/最新已结算 Attempt：历史 `REQUESTED/UNCERTAIN` 仍阻断，只有当前 Attempt 且有 raw-response artifact 的模型完成响应进入 replay；已完成 commit/projection effect 不再伪装成模型响应；完成但缺 raw 证据的 ledger 行进入 `reconcile_first`。新增的 N4 回归后定向集合为 **272 passed**；v25 旧数据库未被改写。
- N3 的真实候选检查已被离线重放为可审计失败：候选 `sha256:9f8bd9d7c3c957e3dcb8a0697ca0b05de340edd268d2469ea537312307dbddf6` 的三条 source-bound unresolved 现在分别被识别为 `1-800`、`201-300`、`350-500` 的 `UNRESOLVED_SCOPE_MISSING`；六条 active issue 的宿主 ID、Memory-gap 来源和责任仍保留，但候选范围证明尚未闭合。
- N4 旧失败任务真实状态为 `leaf_schema_rejected`，failure budget 按策略为 2；旧模型响应已留存，未盲重发。修复证据 `sha256:b8661195f00e0873e128be7612e007b410e9e0cb5739b9c4c78c575772935233` 通过 `runtime unblock` 解阻 revision 4→5，随后一个有界真实切片调用 4 次 8003 请求并成功生成候选；没有增加 attempts 或预算补丁。
- `6a56ec0` 后重新只读核对 v25：worktree clean、当前代码 SHA 为 `6a56ec0bb562d347f29fe873916c552e46864f5e`；v25 canonical commit 仍是 Genesis basis `sha256:851fc…`，roots 为 0 卷/0 正式义务/0 章节计划，`g0/g1/g2/g3_evidence_complete=false`，计划接受任务仍 `waiting_input`。这次读回没有推进或写入旧运行。
- D0 的 v23/v24 原始工件、真实 Reviewer、Planner 局部修订、复审和公共物化预检证据继续保留；本次没有把诊断对象写入旧 Canon。既有真实 D0 证据包括候选 `f5422937…`、旧审校 `47f9a758…`、复审对象 `sha256:2b7e062c…` 和真实请求 `model-request.run.d0.diagnostic.plan-rereview`；其范围和“外部给定问题/真实发现”边界仍按旧记录区分。

## 人工审核与 G0 当前停止点

恢复后的 plan task 已成功，候选绑定为 `sha256:4c1752f335231faae57b68d1b9db4242d48e84600782921f898cdbe274568aa1`，但运行停在 `run.yujin-jiuxu.v25.plan.accept` 的 `WAITING_INPUT`。模型 Reviewer 的两个 `ACCEPT/0 issues` 工件（`sha256:112661d8…`、`sha256:fff58964…`）不代替人工结论。

按用户授权，Codex 作为独立 Reviewer 审阅了 STORY 候选并写入 `sha256:2af18f0080f1a9de680f9e994f18971e0e12f6205652cfac2b59ffe56921c6b6`，结论为 `REVIEW_REQUIRED / REVISE_BEFORE_AUTHOR_ACCEPTANCE`。审阅确认当前候选未直接违反已核对的 101/201/350/401 时间边界，但不能作为 G0：只显式包含第一、第三卷结构，没有八卷 1—800 完整覆盖；没有逐条正式义务 ID/World-Plan 映射；没有已接受提交并投影的 CHAPTER_SET 1—5；unresolved 范围字段为空。

审阅者只记录独立结论，不生成 `ActorKind.AUTHOR` 的 `accept-plan` 或 `reject-plan` 回执。当前 canonical roots 的事实为：`committed_volumes=0`、`formal_obligation_count=0`、`committed_chapters=0`，`g0/g1/g2/g3_evidence_complete` 全为 `false`。因此不得报告 G0、G1、G2 或 G3 通过，也不得派发 Writer。

下一步需要作者依真实作者锁对当前候选作出接受/拒绝或要求修订的决定；在此之前本记录的状态是“工程修复和真实受控规划切片已完成，正式阶段验收未通过”，不是“直接续跑”。
