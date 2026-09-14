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

## f2241ee 后的统一修复接续（未冻结、未正式运行）

本段记录当前工作树的未提交增量，不能替代前述历史运行结论。执行前已将 formal4 目录、D0 `/tmp/yujin-d0-codex-20260914` 目录及 formal4 依赖的 v25 对象原样复制到被 Git 忽略的 `tmp/yujin-evidence-preserved/`，并在 `manifest.json` 保存逐文件 SHA-256/大小，回读校验通过。D0 原始 review、候选和 response 未改写；`d0-adjudication.json` 是 Codex 的独立诊断，不是模型 receipt 或 ACCEPT。

- N1：目标修改须由该 finding 对目标自身及对应字段的引用支持；有效意见的结构化引用进入保留工件，引用修复失败时不丢弃同次已成立意见。D0 测试不再截取共同短语或筛掉初审的第四卷意见。现存最终候选 `e16d6d1…` 未修第四卷 midpoint；最终复审 `74f4d5e6…` 的“没有新因果/代价”理由与完整段落不符，但第七、八卷重复击败同一首领并取得同一秘密仍是可成立的独立修订目标。因此当前 D0 为有界 REVISE，不能报 ACCEPT。
- N2：复用原有正式义务窗口接线；生产审查/窗口的定向回归 **77 passed**。
- N3：正式 reject 后继现在从可验证上游恢复作者意图，只绑定当前父候选、当前有效审查和 directive；后继依赖已成功的候选生产任务。两次经 `CreativeRuntimeService` 的 reject→生成→acceptance 定向用例通过。未决合成规则升为 `scoped-revision.v2`，范围授权只改 `affected_chapters`，保留原 Memory 责任、来源、禁止假设及阻断属性；对 formal4 父项/审查/修订的离线复算验证三个窗口分别覆盖完整 1—800、201—300、350—500，并使用原始 parent/review 字节和新 proof 经公共 `PlanCandidateMaterializer` 校验。旧 v1 proof 保留字节，但不按 v2 规则默许通过。真实 Stage4 loop 的完整生产后继仍待验证。
- N4：SQL ledger 的消费时间可以落库、跨 Session 读回，后续 settle 不清空；解析后、任务结算前的 raw 保持未消费；有持久终态工件时才随 task/attempt SQL 结算标记。通用 retry 已按结算失败分类拒绝确定性失败、未决发送和有 raw 的已完成响应；恢复服务领取新 Attempt 前也检查这些证据，防止绕过 retry 控制。**恢复服务尚未把旧 Attempt 的 raw 交回新 Attempt 的 Stage4 逻辑阶段**：后者会使用新的模型 request 身份。该路径当前安全停止，不能宣称持久恢复闭合，也不能启动新的 formal 或 D0 模型修订。

本增量的 N1/N2/N3/N4 相关定向确定性测试合并为 **208 passed**，改动文件 Ruff 通过，改动源文件 MyPy 单独检查通过。全仓 `make quality` 曾在执行测试前被既有 49 个 Ruff 问题阻断；没有把定向结果称为整体质量通过。v25 Canon 仍以原 Genesis 为基，未提交 STORY、卷、义务或章节计划；未创建 formal5/6、v26 或新模型调用，未增加 attempts。后续先闭合 N4 恢复与 N3 Stage4 完整入口，再对保全的 D0 候选做真实有界修订与复审，随后才可冻结正式配置。

## 本轮接续结果（当前工作区，未冻结）

- N0 当前身份已重新核对：分支 `codex/yujin-unified-remediation`，HEAD 为 `f2241ee3ff7dab941bd3735b03d7fbc0c113f101`；工作区保留原有未提交修改及本轮定向修复，未回退、覆盖或合并其他工作区内容。8003 实际 profile 仍为 `qwen38_27b_nvfp4_8003`，模型为 `qwen38-27b-nvfp4`。
- N1/N3 新增的拒绝后继 directive 来源保留已通过定向回归：字段路径、约束 ID、逐条 citations/quote、实际/期望、未满足条件和问题身份不再被压缩丢失；`5 passed` 的公共后继/恢复交叉验证包含该路径。此前 N3 的 Stage4 完整生产后继和公共物化 bundle 仍未以正式任务闭环证明。
- N4 跨进程恢复证据已通过：`attempt_no=2`、逻辑阶段 `plan_revision`、Memory 调用计数 `1`、provider 调用计数 `1`、旧 raw 已消费；本轮相关确定性集合 `39 passed`，证据仍保留在 `/tmp/yujin-n4-cross-process-gxie4n/evidence.json`。
- D0 从保全的 `e16d6d1…` 复用 r2 Planner raw/draft，宿主合成候选为 `sha256:e3f5e7c3aea724345cb9633b717f7a4db41868b66b1154476350f3bac290325d`。真实 Reviewer 经一次有界 citation repair 后仍为 `REVISE`，保留两个有效阻断：`vol-7/vol-8 midpoint_reversal` 重复，以及 `vol-5/vol-6/vol-7 ending_state` 模板重复；r6 工件在 `tmp/yujin-d0-rereview-20260914-r6/`，本轮按边界停止，未继续采样。
- 因 D0 尚未 ACCEPT，当前没有 v26 冻结 SHA、正式 G0 运行或 Canon 根变化；旧 READY/续跑记录仍不构成放行依据。

## 本轮 Stage4 公共后继补证（仍未冻结）

- 已补一条无模型调用的公共物化预检：真实 `ProductionStage4InvocationFactory` 生成详细请求，真实 `Stage4PlanningLeafAdapter` 调用公共候选物化入口并写入对象库；作者意图、父候选、operator review 和 operator directive 仍分别留在对应边界。定向测试 `test_production_stage4_factory_reaches_public_leaf_materialization` 通过。
- 又把同一生产叶端接入 `CreativeRuntimeService` 的拒绝后继：同一运行连续两次经 operator review 拒绝，第二、第三代任务分别仅携带上游作者意图、当前父候选、当前 review 和新 directive，再次经过生产 Stage4 工厂与公共叶物化；旧父项和旧审查不再累加。定向测试 `test_runtime_revision_successor_uses_production_stage4_public_leaf` 通过；两项均为确定性 loop 预检，不是 8003 模型质量验收。
- 上述两项连同既有四项 Stage4 工厂契约测试合计 **6 passed**，Ruff 与 `git diff --check` 通过。`test_stage5_production_factories.py` 中未选取的三个旧 Writer 夹具仍分别卡在无效冻结 checkpoint、同章重复 goal 和不完整 readiness/projection 证据；不把这些失败改写成 Stage4 通过，也不扩大本轮修复范围。
- N4 的真实跨进程 replay 和 D0 r6 的真实复审结论不变：N4 已验证不重跑 Memory/不重复计费；D0 仍为两个有效阻断的 `REVISE` 并按界停止。因此本段仍不能产生 v26、正式 G0 或 Canon 根变化。
- G0/G1/G2/G3 根审计与阶段驱动停止点回归再核验 **27 passed**；这只证明阶段出口保持 fail-closed，不代表任何正式阶段已经取得 Canon 证据。

## N1 范围语义复核（当前工作区，仍未冻结）

- 复核了当前 `PlanReviewer` 的引用与目标边界：逐条 citation 必须解析到被引用 item 的声明字段，跨条目措辞不同必须逐条引用；comparison item 不会因出现在 `affected_item_ids` 中自动获得写权限，目标必须同时出现在候选、比较证据和对应字段引用中。宿主身份、实际/期望值、授权操作与授权目标仍由宿主生成，模型不能自填。
- 复核了 unresolved 范围：`affected_chapters` 按章节集合而非端点对处理，201 与 300 不会掩盖 202；窗口范围只作为宿主期望条件，不能静默写回模型输出。既有窗口归一化和来源/责任保留测试继续通过。
- 本次定向集合为 **90 passed**：`test_plan_review_citation_verification.py`、`test_plan_unresolved_scope.py`、`test_plan_composition_source_proof.py`；未调用模型。该结果是 N1 工程契约证据，不改变 D0 r6 的真实复审结论。
- D0 r6 仍保留 `vol-7/vol-8 midpoint_reversal` 与 `vol-5/vol-6/vol-7 ending_state` 两个有效阻断；因此不冻结 v26、不启动正式 G0，也不把本次 N1 测试通过写成产品验收通过。

## 精确字段范围修复（当前工作区，仍未冻结）

- 发现并修复一个确定性契约缺口：finding 已引用 `midpoint_reversal.description` 时，旧合成器曾把授权提升为整个 `midpoint_reversal`，允许无意中改变 `window`、`role` 或 `serves`。现在 scope 保留完整 dotted path，公共合成入口只复制/删除被授权叶字段，未点名的同级字段继续来自父候选。
- 该行为改变了 composition proof 的语义，规则版本升为 `scoped-revision.v3`。旧 v1/v2 proof 仅可读取保全，不可作为当前 v3 合成的验证证明；没有改写旧工件。
- 新增嵌套字段反例及旧规则 proof 拒绝回归，并更新旧 D0/poison-chain 契约断言：确定性单元集合 **92 passed**，既有两组 D0 链路回归 **8 passed**；核心改动源码、unit/model 测试的 Ruff 与 `git diff --check` 通过。两份既有 integration 文件仍有原先的 3 个中文全角标点 Ruff 提示，未将其改写为本次缺陷。该修复未启动模型，也未重开已有 D0 r6 轮次。
- 受影响生产入口复核通过：Stage4 factory/公共后继 **6 passed**，CreativeRuntime 连续两次 reject 后继 **1 passed**，replay/恢复与相关 runtime edge **9 passed**；均禁止模型调用。按导入闭包运行 MyPy 时，未出现本次 `plan_composition.py` 的诊断，但仍被 `benchmark.py`、`writer_cognition.py`、`planning_coverage.py` 的 4 个既有错误阻断，未称整体 MyPy 通过。
- 用保全的 `e16d6d1…` 父候选、既有 operator scope 和 r6 已生成候选作为 raw，离线按当前 v3 公共合成器重算得到派生候选 `sha256:f0b0ccb49dd4adb289a8050e5c1d529a7a1f840967a910093218263a43962082`；`vol-4.midpoint_reversal.description` 与 `vol-8.volume_climax.description` 均实际变化，未授权同级字段均保持父值。该派生只证明当前合成边界，不是新的 Reviewer 回执、物化提交或 D0 ACCEPT。
- 因 D0 r6 的真实阻断仍未关闭，且其旧合成证据不能越过当前 v3 身份，v26/G0 仍不放行；后续若重新取得有界 D0 授权，必须在 v3 下重新生成合成证明和复审证据。

## N4 生产叶端接线复核（当前工作区，仍未冻结）

- 复跑跨进程恢复用例 `tests/integration/test_runtime_model_replay_recovery.py`，结果 **1 passed**：旧 Attempt 的 raw 经 SQL ledger 和 object store 读回，恢复工件绑定 `plan_revision`，Gateway 没有新增 provider 请求，Memory 计数保持 1，阶段持久产物结算后旧响应标为 consumed。
- 新增并通过 `test_production_stage4_leaf_consumes_replay_at_the_bound_phase`，与既有 factory phase-rebind 契约合计 **3 个 replay/Stage4 定向用例通过**（2 个 factory/leaf、1 个跨进程）；生产 `Stage4PlanningLeafAdapter` 收到的 `model_request("plan_revision", ...)` 保留旧 request identity 和 source Attempt，而不是改用恢复 Attempt 的新模型身份。该验证禁止模型调用。
- 又补上 replay 前沿的 fail-closed 边界：如果恢复工件含有多个旧响应而 Stage 4 在候选/人工等待前没有消费完，公共叶端拒绝把该阶段当作成功，未消费 response 保留在 replay 工件中，不被结算逻辑批量标记为 consumed。相关 Stage4 factory/leaf 与适配器回归 **5 passed**，跨进程恢复仍 **1 passed**，没有新增模型调用。
- 这闭合了 N4 的“旧响应回到正确逻辑阶段且不重复 Memory/计费”证据链；它不产生新的 D0 Reviewer 回执，也不改变 r6 的 `REVISE` 阻断。N3 的公共后继/物化预检仍是确定性证据，D0 未 ACCEPT，故 v26/G0 继续停止。

## N4 证据收紧（当前工作区，仍未冻结）

- 复核发现原跨进程脚本的 Memory 计数曾由固定写入初始化，不能单独证明恢复读取了既有 Memory 结果；已将其改为发送进程执行一次确定性的 Memory 边界函数，把 `memory_call_count=1` 写入 checkpoint state，并由恢复进程读回该 state 后再核对计数未增加。
- 该修复没有新增模型调用、没有改写旧数据库或旧 raw；`tests/integration/test_runtime_model_replay_recovery.py` 在禁止模型标记下 **1 passed**，脚本 Ruff 和 `git diff --check` 通过。该证据证明恢复使用已持久化的 Memory checkpoint 且不重新执行该边界，仍与 Stage4 生产叶端的逻辑阶段 replay 契约共同成立。
- 另修正 Stage4 生产 `ModelRequest`：创建时显式写入 `scheduling_stage=phase`，账本优先保存真实逻辑阶段；旧 raw 仍保留按 request identity 的兼容回退解析。生产叶端 replay、runtime edge 和跨进程用例 **5 passed**，未新增模型调用。
- 随后复跑 `tests/unit/test_stage5_runtime_edges.py` 与跨进程用例，完整结果为 **29 passed**；未触及 D0 的真实 Reviewer 轮次。
- N4 阶段停止复核发现一个边界回归：无已结算 Attempt、无模型/效果账本证据且已耗尽预算的旧 `WAITING_RETRY` 任务被误拒绝。现仅在这一明确条件下允许进入 `BUDGET_REVIEW`；任一模型账本终态（包括 `VALIDATION_REJECTED` 等）、历史响应、未决发送或已结算失败都继续按分类拒绝盲重试。补充孤立终态 ledger 反例后，runtime command/分类/阶段出口集合 **51 passed**。
- 受影响的完整 `u8b` runtime command 与 Stage5 CLI 入口随后 **38 passed**；测试夹具补齐了真实持久 checkpoint 和已成功候选生产者，未放宽生产约束。没有新增模型调用。
- D0 r6 仍为 `REVISE`，没有新的 Reviewer 调用；因此本节只收紧 N4 证据，不产生 v26、G0 或任何 Canon 提交。

## 本轮跨包确定性回归（仍未冻结）

- N1/N3 引用核验、结构化 unresolved 与 v3 合成来源证明重新执行 **92 passed**；Stage5 creative runtime、D0 冻结候选机械链和 v23 poison-loop 回归执行 **11 passed**。两组均禁止模型调用。
- SQL ledger、runtime edge 与生产工厂选定集合为 **44 passed、2 failed、2 deselected**。两个失败均为已知旧 Writer fixture：使用 `{"checkpoint":"latest"}` 伪造冻结 checkpoint，以及同章重复 active goal；它们不属于本轮 N4/Stage4 生产叶端改动，未把失败改写成通过，也未扩大修复范围。此前已补齐并通过真实持久 checkpoint 与成功候选生产者的相关入口 fixture。
- `git diff --check` 与本轮涉及源码/测试的 Ruff 检查通过；当前 HEAD 仍为 `f2241ee3ff7dab941bd3735b03d7fbc0c113f101`，无新的 8003 调用、v26 冻结、G0 提交或 Canon 根变化。结论仍是：**工程修复完成，真实推进未验证**；D0 r6 的两个真实 `REVISE` 阻断继续生效。
