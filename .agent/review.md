# Codex acceptance review

## T2/T3/T4/T5/T6 最小优化实施验收（2026-09-07）

- 结论：`REPAIR / NOT_ACCEPTED`
- 审查对象：`feat/hierarchy-progressive-skill-patch`，HEAD `b759675307c` 之上的工作树改动
- 审查范围：71 个已修改文件、1 个未跟踪测试文件，约 `+1879/-612`
- 约束：本轮仅审查与验证；未修改生产代码
- 权威计划：`yujin-jiuxu/Novel_System_T2_T3_T4_T5_T6_Optimization_Execution_Plan_optimized.md`

## 按优化方案逐项核对（静态代码映射）

本节是本轮验收的主结论；状态含义为：`已实现`、`部分实现`、`未实现/实现偏差`、`待运行证据`。

| 方案条目 | 状态 | 代码核对结论 |
|---|---|---|
| 保持现有五 Root，不新增质量/路由/Obligation模块 | 已实现 | 未增加 Canonical Root、Python模块或Editor Skill文件，改动仍在既有 owner 内。 |
| SourceClass筛选story authority | 已实现 | 初始run只携带 `AUTHOR_INITIAL_BRIEF`、`AUTHOR_KNOWN_FUTURE_PLAN`；baseline/style/external未被提升为story authority。 |
| 完整authority进入Story、Arc、ChapterSet Planner | 已实现 | Stage4不再按mode清空；PlanningContextLoop直接透传；PlannerContextAssembler按protected+mandatory读取完整artifact。 |
| 同一authority/context进入独立Reviewer | 已实现 | Proposal Reviewer读取同一次 `PlannerContextPackage.rendered_context`；Inquiry Reviewer读取authority正文。 |
| authority缺失时调用前fail-close | 部分实现 | 正常路径能以 `task.source_ids` 检查；但source_ids由当前refs派生，缺少独立“初始run本应有authority”的持久断言。 |
| STYLE_GUIDE→Profile、BASELINE_SETTING→World | 部分实现 | baseline进入World；独立Style Guide没有通用Profile路由，只可能被少数brief正则偶然提取。 |
| Profile语言/题材/长度/段落/时间锁及source_ids | 部分实现 | 映射键和长度中值已加；rich style guide与逐项constraint source绑定没有可靠生成。 |
| PlanNode/ChapterGoal保留source_ids+完整payload | 已实现 | 两个domain字段和Genesis/Materializer映射均存在。 |
| Genesis节点经Story接纳后保留 | 基本实现 | 不再按“无层级根节点”整批删除；仍需方案要求的Story接纳public回归证明。 |
| Story→Volume→ChapterSet→Chapter完整层级 | 部分实现 | 父级/range校验已加；但ChapterSet proposal的每个chapter item会被物化成 `plan_level=CHAPTER_SET` 节点，并未形成清晰的单一ChapterSet→Chapter层。 |
| 8/8卷、时间锁、≤3 history needs Reviewer gate | 实现偏差 | 时间锁已加；卷数gate未限定Arc mode，会把ChapterSet判成0/8；history needs>3在Need Generator抛错，而不是Reviewer返回REVISE。 |
| Story/Arc/ChapterSet Planner Skill正文 | 已实现 | 三个Skill文件已按方案扩写并清除项目剧情。 |
| Planner mode Skill生产装配 | 实现偏差 | `AgentSpec`/Runner实际会加载Story/Arc专属Skill；但production allowlist缺少对应ID且没有约束Runner，配置与真实模型输入不一致；alternative仍每次常驻。 |
| Story/Arc声明long-range Obligation，ChapterSet只引用 | 未实现 | declaration helper未接收trusted level，ChapterSet仍可创建durable World obligation。 |
| Plan+World同bundle原子物化 | 已实现 | Materializer可在同一CandidateChangeBundle同时提出PlanRoot/WorldRoot。 |
| Obligation稳定ID=item+ordinal+kind | 未实现 | direct声明复用item ID；nested ID缺少kind。 |
| unknown obligation与early payoff fail-close | 部分实现 | 独立reference helper及not-before检查存在；temporal helper仍对unknown执行continue，且正文结算未核对计划是否允许payoff。 |
| 正文证据驱动OPEN→RESOLVED | 部分实现 | 既有Curator/validation支持evidence和not-before；缺少与本章obligation action联结的完整闭环及方案指定回归。 |
| Event/Obligation类型感知semantic verifier | 基本实现 | verifier输入已收窄为kind+record+source/span并避免语言字面误拒；Ch1/Ch48真实中文正负fixture未实现。 |
| Writer获得完整祖先链、当前/后续1—2章目标 | 已实现 | Stage3与Writer loop均递归parent并投影当前至后续2章及最近goal摘要。 |
| WritingTask映射beats/state/entities/actions/Profile/length | 已实现 | 复用现有合同并增加entities/actions，长度从Profile取3000/4000/5000。 |
| production Need只来自0—3个history_needs | 已实现偏差 | 不再实体fan-out且硬上限3；超过3直接抛ValueError，未按方案先由Reviewer要求缩减。 |
| zero-Need直通；非零单Gateway、backend≤12 | 已实现 | 单一BASE tier；zero路径不调用Gateway，非零request为max_rounds=1/max_tool_calls=12。 |
| 非零Need semantic judge；UNASSESSED/gap→REVIEW_REQUIRED | 基本实现 | 有BATCH endpoint时接现有judge；无judge/非COMPLETE在Writer dispatch前失败并由现有runtime收口，仍缺public路径证据。 |
| trace schema不改、compaction不强触发 | 已实现 | 保留现有full trace和compactor阈值。 |
| Writer DRAFT固定scene/style+最多1个optional | 已实现 | production allowlist及DRAFT归一化存在。 |
| 保留CONTINUE/MAJOR_REWRITE的模式Skill | 实现回归 | DRAFT的2+1归一化被应用到所有mode，两个已有模式专属Skill无法加载。 |
| Writer Skills改成可执行检查步骤 | 已实现 | scene/POV/voice/dialogue/hook/style文件均已具体化。 |
| Writer surface：marker/copy/乱码/非目标语言 | 已实现 | `_writer_draft_surface_error()` 已覆盖。 |
| Draft Materializer最终Canon surface gate | 部分实现 | 严格长度与中文标题已恢复；内部/未来/evaluator泄漏及大段复制未在最终Materializer重验。 |
| base Editor每次检查计划/连续性/重复/题材风格 | 已实现 | base prompt、payload checklist与最近goal/prose上下文已接入。 |
| planner_replan_required→现有REVIEW_REQUIRED | 已实现 | initial/local/major re-review路径均停止，不再把Plan问题继续交给Writer大改。 |
| Canary关闭plan/draft auto-accept | 已实现 | 两个布尔值均为false。 |
| 稳定配置指纹覆盖prompt/skill/model/retrieval/Profile | 部分实现 | attestation收集了这些内容，但含进程对象ID，且未成为run/policy的持久冻结身份。 |
| resume配置漂移→RUN_CONFIGURATION_CHANGED | 未实现 | 没有该terminal/code或等价resume比较。 |
| 14项高价值public/E2E测试 | 未实现 | 文件中有14个测试名，但多数只测私有helper/常量/源码字符串；Ch1/Ch48、真实Gateway计数、Story接纳保真、Canon surface和5章E2E均未覆盖。 |
| Phase A两章smoke、Phase B五章Canary与指标记录 | 待运行证据 | 当前没有符合方案的新namespace clean-Genesis两章/五章工件，不能声称阶段完成。 |

因此，按文档本身而非按“改过多少文件”判断：T4核心检索收敛、Writer上下文、base Editor和人工
replan路径接近完成；T2的Skill装配/层级gate、T3的Obligation闭环、T6最终Canon surface、Phase 0
配置冻结以及全部阶段验收仍未完成。整体状态只能是 `部分实现，尚未达到Phase A开跑Gate`。

## 决策摘要

本轮不能验收。实现已经覆盖了若干正确方向：三级规划的 author authority 传递不再主动清空；
`PlanNode`/`ChapterGoal` 增加了 `source_ids` 与 payload 保真字段；Writer request 开始组装目标、
状态、实体、动作和约束；Stage2M 收敛为单档预算并加入 zero-Need 分支；plan/draft auto-accept
默认关闭；共享 Prompt/Skill 的项目专用词静态检查通过。

但当前仍有两个主流程阻断错误，以及配置冻结、Profile、Obligation、Writer repair mode、Canon
表面校验和公共 schema 等多处未闭环。新增的 14 个测试虽然全部通过，却多数只调用私有 helper
或做源码字符串断言，没有穿过真实生产边界；现有一章 fake production smoke 已在 Plan Commit
阻断，因此不满足计划规定的 Phase A 两章 smoke，更没有 Phase B 五章 Canary 证据。

## 阻断发现

### P0-1：Planner Skill 生产 allowlist 与实际 Runner 加载路径脱节

`production_assembly_spec.json` 的 `planner_skill_ids` 只登记了
`skill.planner.chapter_set`，没有 `skill.planner.story` 和 `skill.planner.arc_volume`。与此同时，
`ProductionStage4InvocationFactory._allowed_skill_ids()` 只检查“交集是否完全为空”；因为 core/optional
skills 仍在交集中，Story 和 Arc/Volume 不会 fail-close。

进一步追踪真实调用链后确认：`build_planner_contract_bundle()` 已把各 mode Skill 写入
`AgentSpec.skills`，而 `StructuredAgentRunner.prepare()` 会直接加载全部 `spec.skills`。Stage4 产生的
`PlanningLoopRequest.allowed_skill_ids` 没有传给 Runner。因此 Story/Arc Skill 当前实际上会加载；真正的
问题是 production allowlist 没有约束真实模型输入，部署策略与执行事实相互矛盾，且
`alternative-comparison` 随所有调用常驻。

最小修复：在既有 assembly spec 中补齐所有生产规划 mode 的专属 skill ID，让 Stage4 factory明确要求
`skill.planner.{mode}` 必须存在，并把受信 allowlist 传到 Runner，只渲染本轮允许的 Skill；不能以 core
skill 的非空交集代替模式契约。增加真实 invocation/receipt 断言，检查 Story/Arc/ChapterSet 的实际
Skill 内容，并验证 alternative 只在备选比较或修订时加载，而不是只测配置集合。

位置：

- `src/novel_agent/runtime/production_assembly_spec.json:29-35`
- `src/novel_agent/adapters/runtime/stage4_planner.py:124-136`
- `src/novel_agent/agents/planner.py:65-76`

### P0-2：8 卷覆盖 Host gate 未限定规划层级，会把 ChapterSet 判为 0/8 卷

`apply_host_plan_review_constraints()` 只要拿到 `expected_volume_count`，就对任何 `PlanProposal`
统计 arc-volume item。`PlanReviewerAgent.review()` 又在所有 mode 上无条件从 context 传入该值。
因此一个完全合法的 ChapterSet（只含 ChapterGoal）在 Profile 期望 8 卷时会得到：
`expected 8 arc volumes but received 0`，被永久退回 `REVISE`。

这是执行计划中“Arc 层检查 8/8 卷”的层级作用域错误，不应下沉到 ChapterSet/Chapter/Scene。

最小修复：把受信 mode/plan level 传入 host constraints，仅在生成完整 Arc/Volume 集合的提案上
执行卷数覆盖校验；新增同一 Profile 下 Arc 3/8→REVISE、Arc 8/8→ACCEPT、ChapterSet 不触发
volume gate 的三个 public-review 路径测试。

位置：

- `src/novel_agent/agents/plan_reviewer.py:168-176`
- `src/novel_agent/agents/plan_reviewer.py:277-282`

## 高优先级发现

### P1-1：配置指纹只生成了易变的内存 attestation，没有成为可恢复 run 的冻结契约

`freeze_production_attestation()` 的确把 assembly spec、Prompt/Skill pin、模型 revision、检索策略和
Profile hash 放进了一个 fingerprint；但该值只挂在当前进程的 `assembly.attestation` 上，没有进入
`CreativeRunPolicy.policy_hash`、Writer/Stage4 policy fingerprint，也没有在 resume 时持久化比较。
Genesis 后创建 run 的 policy hash 仍只包含 automation flags、project ID 和 run ID。

此外 attestation fingerprint 包含 `id(session_factory)`，同一配置跨进程也会改变，不能作为稳定恢复
身份。仓库中没有 `RUN_CONFIGURATION_CHANGED` 终止码或等价实现（全文检索无匹配）。结果是 Prompt、
Skill、模型、检索策略或 Profile 热变更仍可能在同一 run 中无痕继续。

最小修复：复用现有 run policy/config identity 持久边界，把稳定内容 fingerprint（去掉进程对象 ID）
冻结到 run；Writer、Stage4 和 settlement 从同一冻结值派生；resume 时重算并在不一致时返回既定
`RUN_CONFIGURATION_CHANGED`。增加跨新 session factory 的“同配置相同、任一 pin 改变即拒绝恢复”测试。

位置：

- `src/novel_agent/runtime/production_bootstrap.py:453-494`
- `src/novel_agent/runtime/production_bootstrap.py:998-1037`
- `src/novel_agent/runtime/production_bootstrap.py:1625-1644`
- `src/novel_agent/runtime/production_novel_bootstrap.py:376-396`

### P1-2：独立 STYLE_GUIDE 没有可靠进入 ProjectProfile

Planner authority 正确排除了 STYLE_GUIDE，但 bootstrap 没有建立计划要求的
`STYLE_GUIDE -> ProjectProfile` 路由。Profile 当前只吸收 Planner 的 `project_profile/project_intent`
以及 `_profile_from_brief()` 的少量硬编码正则字段；Planner 又只看到 initial brief/future plan。
因此独立 Style Guide 中的叙述距离、句法、禁用模板、节奏和语气规则除非碰巧符合几个正则键，
否则只留在 ReferenceRoot，Writer 得不到它们。

最小修复：不新增根或服务；在现有 Genesis/Profile builder 中，按 SourceClass 将已审批 STYLE_GUIDE
内容规范化进现有 `style_profile`/`style_requirements`，保留 source ID。增加独立 style-guide sentinel
从 ingestion 到 Profile 再到 Writer request 的 E2E 回归。

位置：

- `src/novel_agent/runtime/production_novel_bootstrap.py:183-223`
- `src/novel_agent/runtime/production_novel_bootstrap.py:438-441`
- `src/novel_agent/runtime/production_novel_bootstrap.py:674-737`
- `src/novel_agent/runtime/production_novel_bootstrap.py:771-798`

### P1-3：Obligation 声明没有按 trusted level 限制，ChapterSet 仍可创建 durable World obligation

Materializer 已能在同一个 change bundle 中同时提出 PlanRoot 与 WorldRoot，这是正确方向；但
`_bind_obligation_declarations(world, proposal)` 没有接收或检查 `trusted_level`，对所有 proposal mode
统一解析 direct/nested declarations。ChapterSet 因而仍可创建 long-range World obligation，违反
“仅 Story/Arc 声明，ChapterSet 只引用并写 action”的约束。

默认 ID 也没有遵循 `plan_item_id + ordinal + kind`：direct declaration 直接复用 item ID；nested ID
只使用 item ID + ordinal，没有 kind。`_validate_temporal_obligation_use()` 对 unknown ID 仍保留
`continue`；虽然当前另一 helper 能抓到部分 node/goal/action 引用，但这个信任边界本身仍不是计划要求
的 fail-close，未来新增 payload 形态容易绕过。

新增所谓“atomic”测试只直接调用私有 `_bind_obligation_declarations()`，没有提交 CandidateChangeBundle，
也没有覆盖 Story/Arc vs ChapterSet、Plan+World 任一侧失败回滚、正文证据驱动 OPEN→RESOLVED。

最小修复：把现有 `trusted_level` 传给同一 helper；仅 Story/Arc 接受 declaration aliases；统一使用
item+ordinal+kind 生成 ID；unknown 在唯一验证 owner 中 fail-close。补一条从 accepted Plan candidate
到原子 commit 的回归，以及一条 chapter settlement evidence 使 OPEN→RESOLVED 的回归。

位置：

- `src/novel_agent/adapters/runtime/materializers.py:204-267`
- `src/novel_agent/adapters/runtime/materializers.py:794-894`
- `src/novel_agent/adapters/runtime/materializers.py:998-1039`
- `tests/unit/test_t2_t6_optimization_boundaries.py:324-359`

### P1-4：DRAFT 的 2+1 skill 归一化被误用到 CONTINUE/MAJOR_REWRITE

生产 writer allowlist 移除了已有的 `skill.continuation` 和 `skill.major-rewrite`。更关键的是，
`WriterCognitionService` 虽只对 DRAFT 做特殊合法性检查，却在所有 mode 上都把最终 skills 重写成
scene/style + 最多一个 optional。结果 CONTINUE 和 MAJOR_REWRITE 模式即使注册了专属 prompt/skill，
也无法加载其专属 skill；模型若选择它还会先因 allowlist 拒绝。

执行计划的“scene/style + 0/1 optional”只约束 DRAFT，不应破坏既有 continuation/major rewrite。

最小修复：只在 DRAFT 分支执行 2+1 归一化；恢复非 DRAFT 模式的 mode skill allowlist 与原选择逻辑；
分别新增 DRAFT、CONTINUE、MAJOR_REWRITE 的真实 work-plan 测试。

位置：

- `src/novel_agent/runtime/production_assembly_spec.json:20-28`
- `src/novel_agent/services/writer_cognition.py:303-334`
- `src/novel_agent/agents/writer.py:64-83`

### P1-5：最终 Canon 边界没有执行计划要求的 Draft surface checks

`DraftCandidateMaterializer` 在写入 TextRoot 前只检查非空和长度；正文中的 `Chapter N`、内部 marker、
未来信息和大段复制没有在最终 trust boundary 重验。新增测试仅用 `inspect.getsource()` 断言生成的标题
是“第N章”，完全没有向 materializer 注入这些非法正文，也没有验证 Canon 拒绝。

上游 Writer/Editor 检查不能替代最终 materializer 的防御，尤其是恢复、旧候选和人工接纳路径仍可能
把非法文本送到这里。

最小修复：复用现有 surface/copy validators，在 materialize 前执行确定性校验；不要新增并行校验器。
测试至少覆盖过短、过长、英文 `Chapter N`、内部 marker、大段 recent-prose copy 五种 accepted
candidate，并断言 TextRoot 未变化。

位置：

- `src/novel_agent/adapters/runtime/materializers.py:1215-1247`
- `tests/unit/test_t2_t6_optimization_boundaries.py:484-493`

### P1-6：现有公共 contract 与回归套件未同步，质量门不可通过

此次给 `ChapterGoal`/`PlanNode` 和 `MemoryWriteBudget` 改了公共模型，但只更新了部分 Stage3 schema。
当前 domain、Stage1、Stage2 schema contract 均失败。新增测试文件自身也有 Ruff/MyPy 问题，并大量
依赖私有 helper、`SimpleNamespace` 和源码字符串断言，无法证明生产路径。

最小修复：通过现有 schema export owner 更新所有受影响的 domain/Stage1/Stage2/Stage3 schema；
更新那些因有意契约变更而过时的测试预期，但不能删除仍揭示真实回归的断言；把 14 项测试改为计划
表格中的 public boundary/E2E 语义。

## 非必要范围扩张

### P2-1：把 Stage2M 的 12 次 retrieval backend 上限误扩成全局 Curator settlement 预算

`MemoryWriteBudget` 默认从 4 个 model calls 改成 12，同时增加 Curator retries 和 wall-clock；这不是
Stage2M 单 gateway 的 backend-call 上限，而是章节结算的另一预算。它会扩大成本和失败等待时间，
也直接破坏“campaign override 不改变 production default”的既有测试与 Stage2 schema。

最小修复：恢复 MemoryWriteBudget 既有默认值；12 只放在现有 Stage2M retrieval budget/gateway owner。

位置：`src/novel_agent/domain/memory_write.py:264-279`

### P2-2：OpenAI adapter 给所有未显式设置的请求注入 repetition_penalty=1.05

这会改变 Planner、Reviewer、Curator 等全部调用的默认采样行为，不属于本执行计划，也破坏
`test_payload_omits_repetition_penalty_by_default`。若 Writer retry 需要 penalty，应继续由具体 request
设置，不能在通用 transport 层全局改写。

位置：`src/novel_agent/adapters/model/openai_chat.py:289-292`

## 验证证据

### 通过

- `git diff --check`：通过。
- 新增边界测试：`14 passed`。
  - 命令：`pytest --no-cov -q tests/unit/test_t2_t6_optimization_boundaries.py`
- 实现中可以确认的正向能力：author authority 不再在 Arc/ChapterSet 显式清空；Plan/Goal payload 与
  source IDs 字段存在；Plan+World bundle 写入路径存在；zero-Need generator 和单一 budget tier 存在；
  auto-accept 默认关闭；项目专用词静态扫描通过。

### 未通过

- 聚焦现有测试：`9 failed, 121 passed`。
  - 其中两项是有意改变 authority 可见性后的旧预期，需要更新；其余包含 Writer context fixture/契约
    不兼容、全局 settlement 默认漂移、domain/Stage1/Stage2 schema 不同步。
- 一章 deterministic fake production smoke：两项失败；运行在 Plan Commit 进入
  `candidate_materialization_rejected / REVIEW_REQUIRED`，0 章完成、没有产生 Draft task。
- 变更 Python + 新测试 Ruff：54 个错误；Ruff format：7 个文件需格式化；strict MyPy：21 个错误。
- 确定性非 model suite：`86 failed, 2969 passed, 1 skipped, 36 deselected`，覆盖率 90.50%，未达到仓库
  100% 门槛。该原始失败数包含工作树缺失私有 benchmark 数据造成的既有/环境失败，不能全部归因于
  本轮；但 schema、transport 默认、production one-chapter、Writer/Stage4 相关失败可直接归因或必须
  在本轮同步。
- `make quality` 无法直接执行：该 worktree 没有本地 `.conda-env`；以上验证使用主工作区已存在的锁定
  环境显式运行。这不是主要产品缺陷，但提交前仍需在可复现环境中取得完整绿色证据。
- 没有计划要求的 clean Genesis `Story -> Arc -> ChapterSet -> 2章` Phase A smoke，也没有 Phase B
  5章 Canary、包大小/调用次数记录、人工正文审读、Ch1/Ch48 真实中文 evidence fixture。

## 修复顺序与重新验收门槛

1. 先修 P0-1/P0-2，使三级规划能加载正确 Skill 且 coverage gate 只作用于 Arc。
2. 修复稳定 run fingerprint/resume mismatch、Style Guide→Profile 和 mode-specific Writer skill。
3. 收紧 Obligation declaration 层级与最终 Draft materializer surface checks。
4. 撤销两项非必要全局行为改动，同步全部 schema 和现有回归测试，清零 Ruff/MyPy/contract failures。
5. 先通过 public-boundary 14 项测试和完整 deterministic quality gate，再执行 clean Genesis 两章 smoke。
6. 两章证据通过后才跑五章 Canary；记录 authority/model-input、8/8 卷、Need 数、gateway/backend calls、
   semantic status、Context Package 大小、Editor 拒绝注入缺陷和人工质量审读。

只有上述证据全部可复现且无 P0/P1 未解决项，才可给出 `PASS`，也才适合扩到 20/100 章生产测试。
