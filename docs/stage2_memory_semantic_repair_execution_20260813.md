# Stage 2M Clean-Genesis 五点语义审计与产品修复执行设计

- Lifecycle: `ACTIVE`（ADR-0005 词汇表：当前 Stage 执行计划/runbook）
- Status: `HOLD / ANALYSIS_COMPLETE / IMPLEMENTATION_NOT_STARTED`
- Date: `2026-08-13 +08:00`
- Scope: `P001-P005 real-novel semantic audit and repair execution guidance`
- Upper-level authority: `docs/stage2_memory_architecture_repair_execution_20260811.md`
- Governing ADR: `docs/adr/0008-evidence-first-writer-context-product.md`
- Clarifies: 保留上位文档与 ADR-0008 的 package mechanical `READY`；§29 仍是有效历史机械验收，
  但不再据此宣称真实小说语义产品通过。本轮另以 mandatory facet closure 决定 repair campaign PASS/HOLD。
- Execution state: 本文只完成原因分析、代码定位与修复设计；尚未执行代码修复或正式 benchmark。

## 1. 结论、证据边界与本轮目标

§29 通过的是 ADR-0008 mechanical gate：产物可解引用、cutoff/leakage 正确、空图能 fail closed，且
五点可以机械生成 package。它没有证明 Writer 获得了写下一章真正需要的历史事实。使用 GPU1:8003、
`qwen36-27b-nvfp4` 从 Genesis 连续构建到 C95 后，P001-P005 的语义分数与逐层产物共同证明：**当前
Stage 2M 仍不能作为真实小说写作的完备记忆产品，最终产品 gate 必须保持 HOLD。**

本节不是针对五组 Gold 写规则，也不是重开已经关闭的 exact quote、relation owner 或 Graph page
grammar。它以五点为采样窗口，修复可推广到长篇小说的能力缺口：

1. Planner 应把真实章节目标转成可执行 Need，而不是因为模型没有逐字复述目标就整批回退；
2. World 应保存长期状态、重要事件、未决义务与可靠关系，同时保留 assertion/rumor 的认识论边界；
3. Retrieval 应按 Need facet 判断是否完成，并在未完成时弹性扩展候选，不以“有一个候选”为停止条件；
4. L0 exact evidence 应公平进入 Evidence-First Writer Package，不能在 claim-first support/全局预算中消失；
5. package `READY` 继续表达机械可交付；mandatory facet closure 单独表达证据是否找齐。typed gap 可以让
   package 可交付和问题可诊断，但不能使本轮 repair campaign 通过。

本次分析使用同一 C1-C95 memory lineage，但运行期间代码曾随真实失败做修复：P001/P002 来自 v10
snapshot，P003 来自 v16，P004/P005 来自后续 snapshot。因此这些产物足以定位断点，不构成“同一代码
identity 的正式五点 PASS”。最终验收必须在修复完成后以一个固定 commit 从 clean Genesis 重跑。

证据位置（2026-08-13 审核时状态）：

- canonical project/CAS：`/tmp/ns-stage2m-genesis-gpu1-8003-20260813-v4`（快照目录已被清理，不再可读）
- database：`na_s2m_gpu1_8003_20260813_v2`（仍存续；本文 §3/§5/§6/§7 的 World 计数已用其复核）
- P001/P002 progress：`/tmp/ns-stage2m-genesis-gpu1-8003-20260813-v10/support_progress.json`（已清理）
- P003 progress：`/tmp/ns-stage2m-genesis-gpu1-8003-20260813-v16/support_progress.json`（已清理）
- P004：`/tmp/ns-stage2m-genesis-gpu1-8003-20260813-v20-p004`（已清理）
- P005：`/tmp/ns-stage2m-genesis-gpu1-8003-20260813-v21-p005`（已清理）

证据复核：`na_s2m_gpu1_8003_20260813_v2` 中的 97-commit lineage、五个 checkpoint 的
entity/state/relation 计数、relation `truth_class` 全部为 assertion、`luo-luo/luo-heng` 分裂、
黑龙/桐宫在 C60 各有 0 state，均已按本文 §3/§5/§6/§7 的数值逐项复核一致。上列 /tmp 快照是分析期
工作产物，未随仓库持久化；funnel slice 数、语义分数与逐 Gold 明细目前只存在于本文，实现轮如需
精确回溯应视为已丢失，不得据此重跑旧身份。

## 2. Benchmark 实际测量链

五点 benchmark 的正确含义不是对一批现成 JSON 做关键词查找。它应测量完整生产链：

```text
正文 C1..checkpoint
  -> chapter-reveal Memory Write
  -> immutable TextRoot + WorldRoot
  -> R1 / L1 Anchor / L2 indexes / typed graph projection
  -> 下一阶段 visible Plan + checkpoint cutoff
  -> model Planner drafts
  -> Grounder + NeedValidator
  -> NeedQueryCompiler + channel routing
  -> candidate retrieval/fusion/rerank
  -> exact L0 dereference
  -> facet-aware evidence selection and packing
  -> Evidence-First WriterContextPackage + EvidenceLedger + typed gaps
  -> post-freeze Gold evaluator and stage-loss diagnostics
```

Gold 只能在最后 evaluator 中出现。运行时 Planner、World extraction、retrieval、packing 和 readiness
不得读取 Gold。验收的单位不是某个专名是否被硬编码命中，而是每个 Gold 所代表的通用能力是否由
cutoff-safe raw evidence 支撑。

## 3. 五点总览：失败不是只发生在 P004/P005

| Case | World at checkpoint | Planner | Support/L0 funnel | Package | Semantic result |
|---|---|---|---|---|---|
| P001 / C20 | 30 entities、42 states、2 relations；events=0、obligations=0；graph edges=0 | 11 个合理 draft 全因 `trigger_goal_mismatch` 被拒，回退为 24 个宽泛 Need | 18,983 slices；5,522 workset budget drop；13,344 ledger drop；33 insufficient；7 whole-support rejection | package 机械 READY；ledger 102 entries/约 11,990 tokens，但 mandatory closure 未完成且证据高度偏向首个 Need | weighted 0.0556；mandatory 0；4 UNTRACEABLE、3 MISS、1 PARTIAL |
| P002 / C40 | 55 entities、83 states、4 relations；events=0、obligations=0；summary 截断 7 entities/19 states；graph edges=0 | 11 drafts 中 9 accepted，无整批 fallback；但落落状态覆盖为 0，Need 被可见但偏题的状态带偏 | 11,633 slices；4,639 budget drop；6,879 ledger drop；9 insufficient；2 whole rejection；1 transport | 16/18 facets closed，2 typed gaps；package 可机械交付但 mandatory closure 未完成 | package 不可评分后 9 项全 MISS；evidence recall 0.666，暴露“有部分证据但全局状态遮蔽诊断” |
| P003 / C60 | 84 entities、126 states、9 relations；events=0、obligations=0；summary 截断 36 entities/62 states；graph edges=0 | 10 个合理 draft 全被拒（9 goal mismatch、1 plan scope），回退为 25 个 Need | 26,722 slices；11,416 budget drop；15,167 ledger drop；21 insufficient；10 whole rejection；2 transport | package 机械 READY；mandatory closure 未完成，ledger 111/约 11,940 tokens 且混入大量弱相关 callback | weighted 0.0769；mandatory 0；5 MISS、2 PARTIAL、2 UNTRACEABLE |
| P004 / C80 | 88 entities、186 states、28 relations；events=0、obligations=0；28 relations 全是 assertion；graph edges=0 | 无整批 fallback，但 Need recall 仅 0.3 | 12,061 slices；3,851 budget drop；8,061 ledger drop；5 insufficient；6 whole rejection；1 transport | package 机械 READY；mandatory closure 未完成 | weighted 0.2879；mandatory 0.2222 |
| P005 / C95 | 92 entities、229 states、37 relations；events=0、obligations=0；37 relations 全是 assertion；graph edges=0 | 11 drafts 全拒（10 goal mismatch、1 plan scope），fallback rate=1.0；核心人物未进入 Need | 22,155 slices；9,124 budget drop；12,882 ledger drop；12 insufficient；10 whole rejection；1 transport | package 机械 READY；mandatory closure 未完成 | weighted 0.1757；mandatory 0 |

表中的大 slice 数量不表示“正文太长，所以预算只能增大”。它表明上游 Need/route 过宽、typed graph
不可用、停止条件过早，导致候选先泛滥，再由下游预算盲目裁切。正确方向是按 facet 渐进扩展和公平保留
exact evidence；固定把 20 改成 100 只会把噪声与模型成本一起放大。

## 4. P001：早期角色设定与关键约束在 package 前丢失

### 观测事实

- C20 的 WorldSummary 尚未因总量发生明显截断，因此 P001 失败不能归咎于“后期小说太长”。
- Planner 实际生成了位置/藏书阁、阅读背景、身体限制、碑文字符等合理历史问题；Grounder 也完成了
  实体绑定。`NeedValidator` 要求 `trigger_plan_goal` 与 host canonical goal 全文规范化后完全相等，11 个
  draft 因模型使用了语义等价的概述而全部被拒。
- fallback 生成 24 个模板化 Need。两个 relation Need 因 graph 为空得到 0 candidate；其他宽 Need
  常在约 20 个 selected candidates 后直接记为 `budget_satisfied` 或 `exact_satisfied`。
- P001 的 Gold exact quotes 全部存在于 Stage1 context；多数没有进入 Writer ledger/package。只有 G04 的
  2/3 quotes 与 G08 的 1/3 quotes Writer 可见。这证明主要损失发生在 context 之后的 support、ledger
  retention 与 package assembly，而不是正文没有入库。
- raw ledger 分配严重倾斜：首个 Need 获得 54 entries，后面的 history/knowledge/relationship/capability
  Need 多数只得到 1 entry。全局按 Need 顺序消耗 retention budget，使后序 mandatory facet 饥饿。

### 根因与修复责任

1. `src/novel_agent/services/need_validator.py:163-180` 把模型复述文本当成 plan binding 身份。
   `trigger_plan_chapters` 已是 host 可验证的稳定绑定，全文等值校验既重复又误杀。
2. `src/novel_agent/services/retrieval.py:385-400` 与
   `src/novel_agent/services/paired_controller.py:682-750` 把任意 selected candidate 当作 Need 完成。
3. `src/novel_agent/services/claim_support.py:1705-1765` 以一个全局 token 计数器顺序保留 raw ledger；它没有
   先保证每个 mandatory Need/facet 获得一份最佳 exact slice。
4. benchmark 仍把 claim-support completion 当作 Writer package 的主要入口；已存在的
   `EvidenceFirstWriterContextAssembler` 没有成为该生产路径的默认最终边界。
5. World 对九条经脉、三千道藏、碑文字符、短剑、婚约等长期约束的结构化覆盖不足，但这只是候选质量
   的一部分，不能用 World 补齐替代 raw evidence read path。

### 产品化修复

- Validator 先校验章节 ID 在 visible target range 内，再由 host 把该章节的 canonical goal 绑定到 Need；
  model 的 `trigger_plan_goal` 只作可审计 explanation，不再作身份等值门。继续拒绝越界章节、无章节绑定、
  plan-as-fact 和 future factualization。
- retrieval completion 改为 `required facets -> supported facets`。候选非空但没有支撑目标 predicate/facet
  时继续下一页/合法 fallback；只有全部 mandatory facet 获得可解引用 evidence 或明确 exhaustion 才停止。
- ledger 使用两遍 packing：第一遍按 Need/facet round-robin 放入每组最佳 exact slice；第二遍再按相关性
  使用剩余全局 token。共享 span 只计费一次。不得按 Gold 优先级分配。
- benchmark/workflow 默认以 selected `EvidenceSlice` 直接装配 v2 package；Claim Support 可提供摘要或
  evaluator 诊断，但不能决定 raw evidence 是否对 Writer 可见。

## 5. P002：Planner 未全退化，但 World 视图和完成语义仍失败

### 观测事实

- P002 是重要对照：9 个 draft 被接受，没有 `all_drafts_rejected`，说明 Need/Planner 不是整体不可用。
- C40 summary 中陈长生有 35 个可用 state，只选 12；落落为 0。World 同时存在
  `entity.luo-luo`（落落）与 `entity.luo-heng`（落衡，alias 含落落），导致教学、战斗、身份和情感状态
  分散，exact relation join 可能得到 `ambiguous_relation_match`。
- accepted Need 转而关注宴席座次、阵营与唐三十六动机等“summary 中可见”的内容，没有覆盖目标真正
  需要的角色关系与历史约束。
- 某 relation/commitment Need 的 route 在只有 relation facet 被候选覆盖时记录 `exact_satisfied`，而后续
  package 又正确为 relation 与 commitment 输出 gaps。说明 route completion 与 package completion 使用
  两套矛盾定义。
- P002 的 mandatory facet closure 为 `INCOMPLETE` 是正确的 fail-closed 行为，应保留；package 本身只要
  机械合法仍可按 ADR-0008 `READY` 并携带 typed gaps。错误在于上游可见信息与 route completion 使证据
  没有被充分找齐。Evaluator 把整个 package 不合格映射为九个
  Gold 全 MISS，又遮蔽了 evidence recall 0.666 的局部进展。

### 产品化修复

- `PlannerWorldSummaryBuilder` 不再对所有 target 做固定均分。先按 target/predicate 聚合 current state，
  为每个被 Plan 明确提及且唯一解析的目标保留 current/recent state；历史重复 state 可折叠成带 evidence
  refs 的简要组，再用剩余 token 填全局相关 state。预算按序列化 token 控制，不靠 `_MAX_STATES=64`
  这一单一硬常数决定语义覆盖。
- entity resolution 继续 exact internal label first、unique alias second、ambiguity fail closed；对已由真实
  World 产生的 `luo-luo/luo-heng` 分裂，增加 evidence-backed identity repair，复用现有 entity owner，
  不做 fuzzy merge，不为 benchmark 专名写特判。
- route、selection、package 共享同一 facet receipt。P002 的 mandatory closure 必须保持 `INCOMPLETE`，
  直到两项 gap 都被 exact evidence 关闭；typed gap 不是 repair campaign 的成功证据。
- Evaluator 分开报告 `package_eligibility` 与每项 evidence coverage。package fail closed 时不得给出产品
  PASS，但仍保留每项 HIT/PARTIAL/MISS 诊断，避免把局部证据损失误归到最早阶段。

## 6. P003：长程 callback 与当前危机缺少 Event/Obligation 主干

### 观测事实

- C60 的 World 已有 126 states，但 `events=0`、`obligations=0`；黑龙与桐宫实体存在却各有 0 state。
  WorldSummary 还截断 36 entities/62 states。仅靠实体状态无法表达保护链、冲突起因、未决承诺与事件后果。
- Planner 生成的 10 个 draft 仍然合理，但 9 个因 goal 全文 mismatch、1 个因 plan scope 被拒，随后回退
  为 25 个模板 Need。其中包含 10 个 Plan 文本片段和大量“陈长生 history/knowledge/state”宽查询。
- relation Need 在空 typed graph 上为 0 candidates；黑龙/桐宫 Need 同样为 0。其余宽查询产生 26,722
  L0 slices，再大量预算丢弃。
- 多数 P003 Gold quotes 已进入 Stage1 context，Writer/ledger 仅保留了 G07 的一处；Writer package 反而
  包含宴席规则、政治背景、章节标题和过期目标等弱相关 callback。
- package 机械上可以 READY，但 21 insufficient、10 whole-support rejection、2 transport failures 没有
  形成独立 mandatory closure，导致机械状态被误读成语义完成。

### 产品化修复

- ordinary Curator 当前 contract 已允许 `CuratorEventRecord` 与 `CuratorObligationRecord`，但真实 C1-C95
  始终产出 0。应在同一 Curator owner 内把“causally important event”“open/progressed/resolved obligation”
  变成明确的 durable extraction 类别，并给 Event/Obligation 单独的 bounded coverage receipt；不得新建
  第二事件库或把所有场景动作都写成 Event。
- Event 只保存改变角色/世界后续状态的因果节点及 effect refs；Obligation 保存对未来写作仍开放的承诺、
  伏笔、目标和 unresolved conflict。短暂见面、即时情绪和普通动作继续留在 L0。
- WorldSummary 优先暴露与目标实体相连的 recent events、open obligations、current states 和 accepted
  relations，而不是只按扁平 entity/state 常数截断。
- long-range callback Need 应能编译 event participants/effect、obligation owner/status 和 graph predicate；
  若结构化渠道没有数据，lexical/dense 仍可从 L0 找到 exact evidence，并返回 `graph_unavailable_reason`。
- readiness 明确分层：package 机械合法可 `READY`；mandatory Need 的 facet receipt 中只要存在
  unresolved/insufficient/transport failure，closure 就是 `INCOMPLETE`，repair campaign 保持 HOLD。

## 7. P004/P005 与前三点的共同链条

P004/P005 不是新的孤立问题，而是 P001/P003 的缺陷随小说增长后的放大：

- 28/37 条 relation 全是 `ASSERTION`，R1 正确只遍历 `ACCEPTED_WORLD_FACT`，因此 typed graph 为 0。
  这里 R1 fail closed 是正确的；错误在写入时没有根据叙事模态区分 narrator fact、人物声称、传闻与假设。
- P005 的合理 Planner drafts 再次被 goal 全文等值门整批拒绝，fallback 让落落、天海牙儿、金玉律、
  天海胜雪、轩辕破等关键实体没有稳定进入 Need。修复 P001 的 binding 即同时修复这一系统原因。
- alias 分裂与缺实体让 relation endpoint 无法唯一解析。Graph admission 应保留 typed missing/ambiguous
  receipt；identity repair 必须回到 entity owner，以 exact evidence 决定复用或新建。
- 泛化 Need 召回巨大 L0 池，随后 budget 与 support synthesis 稀释证据。应修 Need、facet completion 和
  packing，而不是只提高总候选数或 Writer token。
- whole-support verifier rejection 表明“有零散相关片段”没有变成 writer-visible 的完整 evidence set。
  Evidence-First package 应按 Need/facet 聚合 exact slices，不要求先合成为 claim 才能暴露给 Writer。
- 例外信号必须解释而不是忽略：P004 是五点中语义分最高者（weighted 0.2879、mandatory 0.2222），
  说明其 support/package 链路局部可用。修复不得抹掉这一局部能力；实现轮应在新产物上逐 Gold 定位
  P004 已闭合项与 P001-P003 未闭合项的机制差异，并把结论写回本文件。

## 8. 结构化记忆边界：不是所有关系都进 Graph

为了服务真实小说而不是填满 graph，下列语义与其 canonical owner 必须保持清楚：

| 语义 | Canonical owner | 进入条件 | 读取方式 |
|---|---|---|---|
| durable entity-to-entity relation | `RelationRecord` / `WorldGraphExtractionPass` | endpoint 唯一、predicate registry 合法、exact evidence、source truth 保真 | R1 exact；只有 accepted fact 可 typed traversal |
| current or interval value | `StateRecord` | 对后续章节仍有效的属性/位置/能力/知识/身体状态 | R1 temporal/exact + L0 |
| causally important occurrence | `Event` | 改变后续状态、关系或 obligation，具有 participant/time/effect/evidence | event retrieval + L1/L2 + L0 |
| open narrative dependency | `Obligation` | promise/objective/foreshadowing/unresolved conflict 对未来仍开放 | obligation retrieval + L1/L2 + L0 |
| 传闻、角色发言、假设 | 对应 record 但保留 `TruthClass`，或仅 L0 | 不得升级为 accepted world fact | exact/lexical evidence；默认不可 graph traversal |
| 一次性动作、气氛、修辞和细枝末节 | TextRoot/L0 | 无长期世界影响 | lexical/dense/L0 |

当前已正确且必须保留：ordinary Curator 不再写 Relation；Graph 使用单数组 `maxItems=12` page、source-unit
continuation 与弹性并发；exact quote 回到原文生成精确 EvidenceRef；Graph admission 保留 source truth；
R1 只遍历 evidence-backed accepted relation。`12` 是单页生成 grammar，不是整章总关系上限；真实总量
由 source units、continuation、model-call/token budget 和无进展停止共同限定。

仍需修复的是叙事 truth classification：正文叙述者直接陈述且非梦境/假设/转述的关系可成为
`ACCEPTED_WORLD_FACT`；角色台词或内心判断为 `ASSERTION`；未经证实的转述为 `RUMOR`。不得用关键词
黑名单替代模型抽取与 host evidence review，也不得为提高 edge count 把 assertion 批量升级。

## 9. 一轮修复的代码级执行顺序

这不是再拆成许多长期 Round。实现时按下列依赖顺序在一个 Stage 2M repair round 内完成，每一项扩展
现有 owner，不增设平行框架。

### A. 先让诊断与产品边界说真话

Owner：

- `src/novel_agent/services/teacher_forced_benchmark_e2e.py`
- `src/novel_agent/services/stage2_paired_pilot.py`
- `src/novel_agent/services/stage2_benchmark_flow.py`
- `src/novel_agent/services/memory_benchmark_diagnostics.py`
- `src/novel_agent/services/evidence_first_checkpoint_runner.py`
- `src/novel_agent/services/evidence_first_writer_context_assembler.py`

动作：

1. 将已经为 metric builder 创建的 ancestry-aware `GoldEvidenceMatcher` 传给
   `StageLossDiagnosticBuilder`。当前 `teacher_forced_benchmark_e2e.py:3315-3352` 创建正确 matcher 后又
   用默认 matcher 构造 diagnostic，cross-root evidence 会被误报为 F-NEED。
2. benchmark 默认最终 Writer 边界改为 v2 Evidence-First assembler；legacy claim-first context 只作为
   对照臂/诊断，不能继续冒充 v2 产品输出。切换点：`teacher_forced_benchmark_e2e.py:379` 与
   `stage2_benchmark_flow.py:34` 构造的 `Stage2PairedPilotRunner`（其内部
   `stage2_paired_pilot.py:704` 使用 claim-first `WriterContextAssembler`）；五点最终出口应与
   `EvidenceFirstCheckpointRunner`/`scripts/run_evidence_first_frozen_checkpoints.py` 使用同一装配器。
3. readiness 分为 package mechanical status 与 mandatory facet closure，写进同一 `case_record.json`/
   `package_manifest.json`（字段位置沿用上位文档 §23.1，不新建 readiness 报告家族）。机械合法但
   mandatory facets 未闭合时，package 可按 ADR-0008 `READY` 并保留可用 slices 与 typed gaps，但
   closure 必须是 `INCOMPLETE`，case/campaign 不得 PASS。

输出：逐 Gold stage-loss 能准确区分 F-NEED、F-ROUTE/RANK、F-L0/PACK 与 F-ASSEMBLY；Writer package
直接携带 exact slices、Need/facet lineage、预算与 gap。

### B. 修 Need admission 与 WorldSummary，而不是扩大 fallback

Owner：

- `src/novel_agent/services/need_validator.py`
- `src/novel_agent/services/task_conditioned_need_generation.py`
- `src/novel_agent/services/plan_conditioned_need_planner.py`
- `src/novel_agent/services/need_draft_grounder.py`

动作：

1. 以 `trigger_plan_chapters` 和 visible PlanRoot 为 canonical binding；host 附加 canonical goal，移除 model
   explanation 的全文等值拒绝。保留 cutoff、plan-as-fact、future factualization、无锚点与非法 facet 门。
2. 若部分 drafts 合法，只保留合法 drafts 并为缺失 facet 做有限补全；不得因一个 draft 失败整批 fallback。
3. Summary 改为 token-bounded、target/predicate-aware selection：目标实体的 current/recent state、recent
   event、open obligation、accepted relation 先进入；历史重复 state 压缩；剩余预算按 plan relevance 填充。
4. `max_total_needs=32` 作为宽安全上限可以保留，不将 benchmark 的 9/11 项 Gold 数量写进 contract。
   默认实际 Need 数由去重后的 mandatory facets 决定。

输出：P001/P003/P005 不再因 goal 改写整批 fallback；P002 保留其正常 accepted path；summary receipt
报告每个目标在 state/event/relation/obligation 各类的 available/selected/truncated。

### C. 让 query、Graph 和停止条件按 facet 工作

Owner：

- `src/novel_agent/services/need_query_compiler.py`
- `src/novel_agent/services/retrieval.py`
- `src/novel_agent/services/paired_controller.py`
- `src/novel_agent/services/stage2_paired_pilot.py`
- `src/novel_agent/services/r1.py`

动作：

1. `NeedQueryCompiler` 不再固定 `graph_relations=()`；从 grounded relation mentions、Need predicates 和
   facet 编译允许的 graph predicates/endpoints。unresolved lexical anchor 继续只启用 lexical/dense，
   exact/graph fail closed。
2. `max_candidates=20` 定义为初始 page/每轮窗口，不是一个 Need 的最终候选总数。按 unresolved facets
   请求下一页或 fallback，达到全部 facet、候选耗尽、call/token ceiling 才停止；现有上限 100 可作为
   宽 ceiling，不默认一次取满。
3. `EXACT_SATISFIED` 必须有 exact evidence 覆盖全部 mandatory exact facets；`BUDGET_SATISFIED` 必须表示
   facet 已闭合且预算内完成。只有 selected 非空不能触发二者。
4. rerank 覆盖 compact grounded units 与 anchor units；同一 evidence family 优先 exact L0 slice，父 block
   只保留导航作用。共享实体但 predicate/facet 不匹配的候选不得进入 direct-support pool。
5. graph 为空或全为非 accepted relation 时明确 `graph_zero_accepted_edges`，随后合法回退 L0；不得把通道
   `ready` 与“该 Need 有可遍历路径”混为一谈。

输出：每条 route trace 携带 required/supported/unresolved facets、pages/calls、typed stop reason；P002
不再出现 route `exact_satisfied` 而 package 同 facet 出 gap 的矛盾。

### D. 修 L0 展开与 evidence packing 的公平性

Owner：

- `src/novel_agent/services/evidence_slice_resolver.py`
- `src/novel_agent/services/claim_support.py`
- `src/novel_agent/services/evidence_first_writer_context_assembler.py`

动作：

1. Anchor/grounded block 先按 EvidenceRef 解引用到 exact paragraph/sentence slice；只有 semantic need 要求
   上下文时才扩相邻句窗，不能把一个 handle 无差别展开成大批 L0 slices。
2. 每个 Need/facet 先选一组最小充分 exact slices，再 round-robin 放入 ledger；第二遍才使用剩余 token
   扩上下文。现有 unique-span 计费保留。
3. whole-support synthesis 可以生成解释，但失败不得删除已验证 raw slices；Writer 可看到 slices 并看到
   `insufficient_for_claim_synthesis` gap。
4. `not_grounded:anchor_or_preview_only` 只淘汰没有可解引用 L0 的 preview，不得同时丢失其 source receipt；
   可解引用 anchor 应转成 exact slice 后再判断 relevance。

输出：resolved/dropped 计数从“巨量展开后裁切”变为按 Need/facet 可解释；早序 Need 不再独占 ledger；
Writer package 中每项可直接 round-trip 到 TextRoot。

### E. 补 World 的真实结构质量并重建 projection

Owner：

- `src/novel_agent/services/model_curation.py`
- `src/novel_agent/services/world_graph.py`
- `src/novel_agent/domain/changes.py`
- `src/novel_agent/domain/world.py`
- `src/novel_agent/services/memory_pipeline.py`
- `src/novel_agent/services/r1.py`

动作：

1. 保留现有 ordinary/graph 单 owner 与 exact quote binding。普通 Curator 明确覆盖 durable Event、State、
   Obligation；Graph profile 只覆盖 entity 与 relation。
2. 对每个 source unit 记录各 record kind 的 proposed/accepted/rejected/no-durable-delta，不用固定“每章必须
   有事件/关系”的配额。若全书 Events/Obligations 仍为 0，receipt 必须能区分模型没提议、host 全拒和
   章节确实无长期变化。
3. 根据叙事模态保留 truth class；accepted relation 才进入 typed graph，assertion/rumor 仍可 exact/lexical
   检索。禁止用 edge-count 目标驱动 truth promotion。
4. 在 clean-Genesis 写入的 entity admission 边界阻止 split：唯一 label 优先、唯一 alias 次之，复用已有
   canonical entity id，歧义保留 typed receipt；复用 `world_graph.py` 的 `EntityAliasRepairPolicy` 解析规则，
   不新增历史 World 的通用实体合并机制或 identity engine。
5. 用修复后的 WorldRoot 走现有 `FullDerivedProjectionBuilder` 一次性重建 R1/L1/L2/typed graph，不增加
   Neo4j 或第二 truth store。

输出：五个 checkpoint 不要求固定 graph edge 数，但任何 narrator-factual durable relation 都不应被全量
写成 assertion；C60 以前应能看到由真实正文支持的重要 Event/Obligation；GraphPathReceipt 继续 relation
row/L0 verifiable。

## 10. 能力验收，不做 case-specific 优化

最终五点用以下通用能力验收，不把 Gold 文本放入 runtime：

| Case | 代表的通用能力 | 必须证明 |
|---|---|---|
| P001 | 早期人物设定、身体/知识/物件约束 | 合理 Planner drafts 不因 goal 改写被整批拒绝；context 中的 exact evidence 公平进入 Writer package |
| P002 | 多角色关系、身份别名、部分证据 fail-closed | 唯一实体绑定稳定；未闭合 facet 保持 typed gap；已有局部证据仍可诊断 |
| P003 | 长程事件回调、保护/冲突链、当前危机 | Event/Obligation 或合法 L0 fallback 能支撑因果链；不以过期/弱相关 callback 填满 package |
| P004 | 状态转变、公开关系与持续后果 | accepted state/relation 与 exact raw evidence 同时可见；support 失败不删除 raw evidence，也不让 mandatory closure PASS |
| P005 | 大规模人物共同体与跨事件连续性 | Planner 不整批 fallback；graph、event、state 与 L0 能协作；alias/缺实体有准确 resolution receipt |

共同门：

- 一个固定 source commit 从 clean Genesis 连续 reveal C1-C95，并在 C20/C40/C60/C80/C95 冻结；
- 五点 retrieval/package 均基于各自新 WorldRoot 与新 projection，不复用旧 index/package；
- 0 cutoff/leakage/dereference failures，canonical roots 不被 read-side 修改；
- 每个 mandatory facet 要么有 exact evidence，要么有真实 typed gap；typed gap 不阻止机械 package
  `READY`，但使 mandatory closure 为 `INCOMPLETE`，case/campaign 不得 PASS；
- Gold evaluator 只在 freeze 后运行，并能给出可信 stage-loss；
- 语义分数必须报告实际结果，不再用 mechanical READY 代替产品通过。

### 10.1 正式 rerun 的输入与身份约定

- benchmark 输入 bundle：`benchmarks/private/ztj_novelmem_v0.5`（`benchmark_id=novelmem-eval-ztj`、
  `version=0.5-seed.2`，checkpoints `[20,40,60,80,95,…]`）。五点运行时 Gold 位于
  `private/gold/track_b.json`，原始 provenance 来自
  `../ztj_memory_pilot_v0.1/cases/ZTJ-P00x/gold.yaml`；Gold 只允许 freeze 后 evaluator 读取。
- 代码身份：分析运行期间的 v10/v16/v20/v21 是工作树演化，未形成固定 commit；当前工作树仍含
  `teacher_forced_benchmark_e2e.py`、`adapters/memory_write/teacher_forced.py`、
  `scripts/run_evidence_first_frozen_checkpoints.py`、`schemas/stage1` 与相关测试的未提交修改。正式
  rerun 前必须先把本轮 Stage 2M 修复固化为固定 source commit，并在输出 manifest 记录该 commit；
  无关脏改动不得混入该 identity。OpenCode 完成实现、测试和 smoke 后报告
  `READY_FOR_IDENTITY_FREEZE`，由 Codex/人工完成 identity 固化，再继续正式 rerun；不得以带脏工作树
  宣称单身份验收。
- 运行身份：使用全新 DB（沿用 `na_s2m_gpu1_8003_YYYYMMDD_vN` 命名）与全新输出根（沿用
  `/tmp/ns-stage2m-genesis-gpu1-8003-YYYYMMDD-vN`），不得复用 §1 的任何旧身份，也不得覆盖或改写
  §29 已接受的 2026-08-12 joint/repair 产物与 frozen identity。
- 模型身份：GPU1 本地 `qwen36-27b-nvfp4`；本次分析使用 8003（8002 同期被其它已授权 benchmark
  占用）。正式 rerun 必须显式记录 endpoint 与 model id，不得与其它运行共享或抢占 endpoint，也不得
  在同一 run 内混用两个 endpoint。

## 11. 最小验证与停止条件

实现期间只做能改变下一步决策的检查：

1. focused deterministic tests：检测 goal binding、facet stop、packing fairness、truth/event/obligation materialize
   与 ancestry diagnostic；失败则只修对应 owner。
2. 三段真实 smoke（早/中/晚章节）：检测模型是否实际产出 Event/Obligation、truth 是否仍全部 assertion、
   adaptive retrieval 是否收敛；若失败，先分析 receipt，不反复调测试样例。
3. 上述机制稳定后只做一次 clean C1-C95 与五点正式 rerun。途中 transport timeout 可从 persisted chapter
   checkpoint 续跑；不得在同一正式 run 中换代码后继续宣称单 identity 验收。
4. 最后运行仓库规定的 `make quality` 与 pre-commit。它们验证集成/类型/回归，不替代真实 benchmark。
5. 本轮改动触及 `RetrievalStopReason` 语义、case_record/manifest 的 readiness 字段、summary/route/facet
   receipt 与 World 抽取 receipt：对应 `schemas/stage2` JSON 契约必须同步更新版本并通过 contract tests；
   产物 media type 或字段变化时 manifest 记录新版本，不得静默改写旧 schema 定义。

停止条件：单一 identity 的五点产物完成、机械门通过、mandatory facet closure 通过，且 stage-loss 不再
显示同一系统性断点。closure 通过 = 每个 mandatory facet 都由可解引用 exact L0 evidence 闭合、route 与
package 的同 facet receipt 不再矛盾、且不存在 unresolved/insufficient/transport 未解决项；typed gap
允许 package mechanical `READY`，但不得计作 closure 或 campaign PASS。
五点 Gold 语义分数（weighted/mandatory）按 ADR-0008 在 freeze 后由外部评审/独立强模型报告，作为产品
判断证据而非 Agent 内置 gate。若 World 中某类记录仍为 0、Planner 仍整批 fallback、route 与 package
completion 仍矛盾，或 context 有证据而 Writer package 大量不可见，则保持 HOLD，并按 receipt 回到唯一
owner；不得通过加 Gold 特判、盲目增大候选/上下文、放宽 truth 或把 typed gap 计作成功来过门。

## 12. 明确不做

- 不建立第二套 KG、第二 truth store、Neo4j 依赖或新的 agent orchestration framework；
- 不把所有动作和所有关系写进 Graph；
- 不为落落、国教学院、黑龙等 benchmark 专名写代码分支；
- 不把 graph page 12、Need 32、retrieval 20 简单改成巨大固定数来替代弹性控制；
- 不恢复 claim-first 作为 Writer 产品边界；
- 不修改 Stage 3/4/5 实现或其在 main 上的集成代码（Stage 边界 fail-closed；本轮 owner 全部位于
  Stage 2/2M 与其共享 read-side 基础设施）；
- 不覆盖、重放或改写 §29 已接受的 2026-08-12 joint/repair 产物与 frozen identity；
- 不为了 100% coverage 反复改变产品语义，也不跳过能证明本次根因的基本回归和一次真实验收。
