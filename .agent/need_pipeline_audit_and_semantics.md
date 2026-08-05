# Stage 2M Need 生成链路审计与评测语义分析（决策输入材料）

- 用途：本文件是 OpenCode 对 C60（ZTJ-P003）Need 生成 → 检索 → 输出链路的**代码事实审计**，
  以及**评测语义（盲式 A / 计划条件 B / 双 Arm C）**的分析与待决策项清单。供 Codex 评审与
  架构决策使用；不包含任何已批准的设计决定。
- 日期：2026-08-05
- 依据：`src/novel_agent/services/task_focus.py`、`task_conditioned_need_generation.py`、
  `retrieval_routing.py`、`retrieval.py`、`search_retrieval.py`、`r1.py`、
  `stage2_paired_pilot.py`、`memory_benchmark_evaluation.py`、`gold_evidence_matching.py`、
  `benchmark_importer.py`、b11 真实工件（`/tmp/ns-stage2m-v31-think-c60-b11-20260804`）与
  `benchmarks/private/ztj_memory_pilot_v0.1`。
- 标注约定：[已核验] = 代码/工件直接确认；[主张] = 外部分析（GPT-5.6）观点，部分已核验；
  [待核验] = 尚未确认。

---

## 1. 端到端流程全解（已核验）

### 1.1 总体链路

```text
原文第 1—60 章
  → ModelCurator 提取+验证 → WorldRootDocument（实体/状态/关系/事件/义务 + evidence_refs）
  → TaskFocusExtractor → 焦点集合（谁值得关注）
  → TaskPlanConditionedNeedGenerator → ~24 个 Need（问什么）
  → DeterministicChannelPlanner + ROUTES → RoutePlan（去哪查）
  → RetrievalOrchestrator + FusionService → 8 通道检索 + RRF 融合
  → RetrievalUnitNormalizer + ContextCompiler → RetrievalUnits
  → TrustedClaimSupportProducer 走廊（家族折叠/workset/chunks/thinking 合成/验证）
  → WriterContextAssembler → Writer 4000 / Ledger 12000
  → GoldEvidenceMatcher + MemoryBenchmarkEvaluator → verdict
```

### 1.2 关键事实

- 检索对象是**图谱投影**（R1 版本化知识图谱，PostgreSQL；OpenSearch anchor/grounded 索引
  带 bge-m3 向量与 `text.standard/cjk/exact_terms` 字段），不是原文自由检索。
- gold 只在 evaluator 侧；运行走廊（含 prompt）全程不可见，且明确禁止推断。
- C60 运行档位：`visible_at_cutoff`，`plan=None`（传未来 PlanRoot 直接抛错）。
- C60 运行时 task contract 的 `task_text` 为**模板文本**：
  「为目标章节 61-65 准备必要的历史记忆 ContextPackage。恢复与截止点相符的当前状态、关系与
  情绪、因果历史、角色知识边界、未决义务和长程回收提醒。不要续写; 不要猜测或泄漏截止点之后
  的正文、评测 Gold、精确目标计划或准备材料; 每条确定性结论必须引用合法历史证据。不得使用
  截止点之后的作者粗纲、计划节点或任何未来材料。」（提取自 b11 proposal 输入工件）
- `input.yaml` 的具体任务语义（「特别检查序章伏笔、婚约动机和角色知情边界」）、`visible_outline`、
  `target_plan` **从未进入运行时**——运行时只用模板 task_text。

---

## 2. 焦点提取规则（已核验，task_focus.py）

| 来源 | 规则 | C60 实际来源 |
|---|---|---|
| TASK(0) | task_text 命名实体匹配（注释明说"对合成/未见任务有用"） | **C60 为空**——模板 task_text 无命名实体 |
| OPEN_OBLIGATION(1) | OPEN/PROGRESSED 义务的 owner | 陈长生（婚约）、徐有容/秋山君（联姻）、落落（拜师） |
| PLAN_INTENT(2) | 仅 AUTHOR_PLAN_CONDITIONED 且 plan 非空 | C60 不适用 |
| CUTOFF_FRONTIER(3) | 最近 12 个事件（`_recent_event_limit=12`）+ 参与者 | 黑龙遭遇 + 陈长生/黑龙 |
| ALIAS_EXPANSION(4) | 别名归并 | — |
| ONE_HOP_RELATION(5) | 焦点实体一跳关系两端（`_max_relation_expansions=12`） | 轩辕破/落衡/茅秋雨等 |

主实体排序：`min(key=(非TASK源, -状态数, -事件数, 源优先级, 顺序))`。C60 无 TASK 实体焦点
→ 第一项恒 True → 状态/事件数决胜 → 陈长生（主实体，mandatory，priority 94-97）。

---

## 3. Need 生成规则（已核验，task_conditioned_need_generation.py）

### 3.1 query 构造公式（实体焦点模板，逐条）

| # | need_type | query 公式（代码） | 真实产物（截断） |
|---|---|---|---|
| 1 | current_state | `label + 前16个状态谓词`（`query = " ".join((label, *predicates[:16]))`，line 318） | `陈长生 arrival_behavior name philosophy morning_routine breakfast...` |
| 2 | continuity_constraint | `{label} 当前连续性 限制 条件 目标 动机 承诺 未解决问题 {entity_context}` | `陈长生 当前连续性 限制 条件 目标 动机 承诺 未解决问题 arrival_behavior direct_to_mansion ...` |
| 3 | capability_boundary | `{label} 当前能力 能力边界 已完成 未完成 可用 不可用 理论 实践 限制 {state_context}` | `陈长生 当前能力 能力边界 已完成 未完成 ... 限制 arrival_behavior ...` |
| 4 | entity_history | `{label} 重要历史 前因 变化 决定 结果 {event_context} {relation_context} {obligation_context}` | `陈长生 重要历史 前因 变化 决定 结果 exam_completion exam_appeal destiny_star_ignition confrontation ... entity_encounter teacher_of 落衡 student_of 轩辕破...` |
| 5 | relationship_emotion | `{label} 关系 情绪 责任 信任 冲突 选择 {relation_context} {target_plan_text}` | `陈长生 关系 情绪 责任 信任 冲突 选择 teacher_of 落衡 student_of 轩辕破` |
| 6 | knowledge_boundary | `{label} 知情边界 知道 不知道 公开 未公开 推测 不可断言 {obligation_context[:300]} {knowledge_state_context[:300]} {relation_context[:500]} {target_plan_text}` | `陈长生 知情边界 知道 不知道 公开 未公开 推测 不可断言 marriage_contract Enroll in Guojiao Academy ...` |
| 7 | long_range_callback | `{label} 长线伏笔 早期建立 首次出现 来源 物件连续性 未解决因果 {state_context} {target_plan_text}` | `陈长生 长线伏笔 早期建立 首次出现 来源 物件连续性 未解决因果 arrival_behavior ...` |
| 8 | target_transition | 依赖 target_plan_text | C60 不生成（plan=None） |

### 3.2 四类 context 构造（已核验）

- `state_context`：该实体全部状态的 `predicate + value`，[:2000]；**重要/琐碎无区分、当前/历史混合**。
- `relation_context`：`predicate + 对方标签`，[:1000]。
- `event_context`：**去重后的 `event.event_type` 拼接**，[:1000]——最严重的信息压缩点
  （参与者/地点/动作/因果/章节全部丢弃）。
- `obligation_context`：OPEN/PROGRESSED 义务的 `description` 去重拼接，[:1000]。
- `knowledge_state_context`（第五种，line 709）：按关键词白名单（attitude/contract/know/
  marriage/secret/teacher/信任/关系/婚/态度/承诺/知/秘密…）筛该实体状态，[:900]。

### 3.3 非实体焦点模板（退化重灾区，已核验）

| 焦点 | need_type | query 公式 | C60 真实产物 |
|---|---|---|---|
| EVENT | causal_history | **`query = event.event_type`**（line 556） | `entity_encounter`、`confrontation`、`combat_outcome` |
| RELATION | relationship_emotion | `subject标签 + predicate + object标签` | `轩辕破 student_of 陈长生`、`茅秋雨 teacher_of 落落` |
| OBLIGATION | unresolved_obligation | `owner标签 + description` | `陈长生 marriage_contract`、`徐有容 秋山君 Marry Qiu Shan Jun to unite Southern Sect` |
| STATE | current_state | `predicate + value` | `秋山君 azure_cloud_rank guardian_status` |

### 3.4 facet / 完成契约（已核验，`_completion_contract`）

- capability_boundary → (CAPABILITY_STATUS, LIMITATION)
- relationship_emotion → (RELATION_STATE)
- knowledge_boundary → (KNOWLEDGE_BOUNDARY)
- long_range_callback → (SETUP, UNRESOLVED_STATUS)，`min_distinct_chapters=2`
- unresolved_obligation → (COMMITMENT, UNRESOLVED_STATUS)
- causal_history/历史 → (CAUSAL_HISTORY)；其余 → (CURRENT_STATE)
- 证据要求：CUTOFF_CURRENT_SOURCE / DISTINCT_HISTORICAL_SOURCE / TRACEABLE_CUTOFF_SOURCE
- mandatory 或 facet>1 → irreducible

### 3.5 Stage1MemoryNeed 完整 schema（已核验，domain/memory.py:236）

`need_id, run_id, task_id, base_commit, chapter_target, horizon_target, need_type,
query_intent, query_text, entity_ids, predicates, time_scope, access_scope, allow_plan,
hierarchy_parent_unit_ids, why_needed, risk_level, requirement, preferred_resolution_path,
allowed_candidate_pools, expected_evidence_types, stop_condition, purpose, expected_section,
focus_ids, priority, query_hints, completion_criteria, need_facets, completion_spec`

**不存在 `semantic_question` 字段**：Need 的问题即 `query_text`（单条），无独立的语义问题层。
`why_needed` 是解释性字段（来自 focus.reason），[待核验]是否参与检索——检索代码
（search_retrieval.py）仅用 `query_text`/`entity_ids`/`predicates`/`time_scope` 等，
`query_hints` 的用途 [待核验]（生成时写入 need，走廊/检索侧未见消费点）。

---

## 4. 检索规则（已核验）

- ROUTES 表按 `query_intent` 定通道（retrieval.py:53）：
  CURRENT_STATE/KNOWN_ID → R1_EXACT；RELATED_EVENT → anchor_bm25+anchor_dense（备 grounded）；
  SEMANTIC_HISTORY → anchor/grounded；RELATION_CHAIN/CAUSAL_MULTI_HOP → TYPED_GRAPH 等。
- 过滤器：project/basis/snapshot/kind/access_scope/observed-only/truth_class/实体过滤
  （grounded 索引不做实体过滤）/时间范围/章节目标。
- 检索词：BM25 `multi_match`（`text.standard^1.0, text.cjk^1.2, exact_terms^3.0`）；dense 为
  query_text 的 bge-m3 向量 knn。
- R1_EXACT：PostgreSQL 版本化谓词精确匹配（entity_ids + predicates）。
- Fusion：RRF（k=60）→ TypedCandidateSelector；per_channel_limit=20，fused_limit=20；
  主通道空 → grounded 兜底。
- **退化机制链（已核验）**：`event_type="entity_encounter"` → `query=event.event_type` →
  BM25 命中的是 `exact_terms` 索引里的图谱谓词而非原文词 → 实体过滤（entity_ids）成为主约束
  → 候选扩展到"陈长生/黑龙全部上下文" → 走廊 compatible/家族保留把 ch2/ch56 等拉进 workset
  → 模型回答它推断的问题（黑羊场景）→ **query specification failure**。

---

## 5. 评测语义分析：定义 A / B / C 与当前档位

### 5.1 三种定义（主张，结构合理）

- **A：盲式未来记忆准备**——只看到截止点前正文；系统预测后续最可能需要的通用长距记忆。
  `visible_at_cutoff` 是正确档位；不能注入 target_plan，甚至不能注入过于具体的任务意图；
  Need 依赖历史未闭合事项、近期前沿与叙事显著性；Gold 应评估"盲式条件下合理可预期的记忆"。
- **B：目标条件式记忆检索**——给定未来章节目标/作者计划，检索完成这些目标必须携带的历史
  记忆。target_plan 必须进入 Need 生成（backward chaining）；计划不能当已发生事实；
  `plan_conditioned` 是准确名称。
- **C：双 Arm**——A 测"不知未来时维护长期状态"，B 测"知道下一阶段计划时精准装配上下文"，
  量化计划增益；Gold 需区分 blind-recoverable 与 plan-dependent。

### 5.2 用户对模块目标的澄清（已确认）

- **当前模块的正式目标 = 定义 B**：在未来规划 + 历史正文都已存在的情况下，为未来正式写作
  检索重要记忆。
- **半 A 场景暂不纳入**：作者写完第一卷后规划第二卷大纲时"不知道未来规划、只需历史重要记忆"
  的维护型场景，属于维护 Agent 压缩记忆的另一职责，本模块暂不考虑。
- 因此：评测语义需要以 B 为主轴；A 档的 G06/G09 批评需拆分指标（见 §7）。

### 5.3 当前运行实际状态（已核验）

- 正式运行 = **定义 A（超严格版盲式）**：档位 `visible_at_cutoff`、plan=None、task_text 为
  模板、input.yaml 的具体任务语义/visible_outline/target_plan 全部未进入运行时。
- **gold 是 B 型语义**：G06/G09 的 `why_needed` 直接指向 61-65 章叙事决策
  （G06「避免把推测写成双方共识」；G09「扩大公开表态的责任范围…」），且被标记为
  `operational_constraint_gold`。
- **错配**：A 档运行 + B 型 gold + 作者材料（input.yaml 的 mode: plan_conditioned 与
  visible_outline/target_plan）暗示 B 意图但从未被使用。

### 5.4 G06/G09 组件拆分（已核验 gold.yaml）

| 组件 | 性质 | 盲式可恢复性 |
|---|---|---|
| G06 xu_is_in_south | ch2 长期状态事实 | ✅ 盲式可恢复 |
| G06 no_direct_conversation | ch56 关系事实 | ✅ 盲式可恢复 |
| G06 **cannot_claim_her_intent** | epistemic 边界（写婚约戏的约束） | ⚠️ plan-dependent |
| G09 luoluo_enrolled / third_student | ch36/ch50 已发生事实 | ✅ 盲式可恢复 |
| G09 academy_protective_effect | ch56 已发生事实 | ✅ 盲式可恢复 |
| G09 **impact_is_risk** | 面向未来决策的风险边界判断 | ⚠️ plan-dependent |

结论：证据组全部在截止点前（从证据角度 A 档公平）；但结论规格（跨族组合 + epistemic 边界
组件）是 B 型要求。**G06/G09 不应被简单归为"模型层失败"——先决条件是问题规格与评测语义。**

---

## 6. 关键核查结论（代码事实）

1. [已核验] TASK 焦点在 C60 为空（模板 task_text 无命名实体）；陈长生/徐有容来自
   OPEN_OBLIGATION，黑龙事件来自 CUTOFF_FRONTIER，其余来自 ONE_HOP_RELATION。
2. [已核验] current_state 的 query = label + 前 16 个状态谓词（非仅 label）。
3. [已核验] knowledge_state_context 是第五种 context（关键词白名单筛选）。
4. [已核验] 事件模板 `query = event.event_type`；`entity_encounter` 是 R1 投影的事件类型
   字段本身，非原文词。
5. [已核验] `operational_constraint_gold` **被运行时读取**（stage2_paired_pilot.py:398-431）
   用于 Arm 评测指标 `operational_constraint_coverage`——不注入检索/Need 生成。
6. [已核验] P001-P005 manifest：target=[21,25]/[41,45]/[60?→P003 61-65]/[81,85]/[96,100]；
   operational_constraint_gold 各 case 不同（P003=G06,G09）。
7. [已核验] 无任何 G06/G09/章节号/实体名硬编码于生产代码（反复验证）。
8. [已核验] `plan_conditioned` 模式在代码中存在（TaskFocusExtractor.PLAN_INTENT 分支 +
   NeedGenerator plan 分支），但 C60 正式运行从未走该路径。

---

## 7. 审计要求清单（供 Codex 决策的任务说明）

### 7.1 调用链审计（input.yaml → ContextPackage）

需要逐层列明：文件、函数、代码行、输入/输出 schema、字段保留/删除/重生成、LLM 调用点、
截断/排序/预算、工件路径。尤其查清 `input.task` / `visible_outline` / `target_plan` /
`mode` / `information_profile` 分别在**哪一层第一次被读取、又在哪一层消失**。

### 7.2 Need 生命周期 vs Query 生命周期审计

- 是否真的存在 semantic_question（当前：无，只有 query_text）；
- why_needed 是解释还是参与检索；
- query 是否唯一、各路由是否共享同一 query（当前：是）；
- entity filter 如何生成、route 是否能看到事件 participants（当前：检索用 entity_ids，
  query 不含 participants）；
- 走廊何时加入 compatible/family 片段（R4-R7 已审计）；
- assembler 最终依据什么字段选择结论。
- 判定修复应落在：Task Contract / Need Planner / Query Compiler / Retriever / Corridor /
  Assembler 哪一层。

### 7.3 WorldRootDocument 字段丰富度核查

Event 当前 schema（domain/memory.py）[待核验字段清单]：
- `event_id, event_type, internal_label/description?, participants(是否带角色), location?,
  chapter?, time?, cause?, result?, status?, source_terms?, aliases?, evidence_refs, confidence?`
- 关键问题：`entity_encounter` 是 curator 的唯一表示还是上面还有更具体字段；`exact_terms`
  实际索引了哪些字段。
- 若丰富字段已存在 → 只需改 Query Compiler；若不存在 → 需要改 ModelCurator 与 schema。

### 7.4 Gold 分类

按以下类别对 P001/P002/P003 全部 Gold 分类：history-salient / cutoff-frontier / open-loop /
task-intent-dependent / plan-dependent / hindsight-only。若 G06/G09 属 plan-dependent，
不应直接用于批评 blind arm 的 Need 生成；至少拆分指标。

### 7.5 系统约束确认

Need 生成的 LLM 自由度（几次调用/是否确定性/跨模型稳定性）、token 与延迟预算、Need 数量
上限、是否允许两阶段（生成+critic）、是否允许检索后补 Need、可复现性要求、无 LLM 规则
fallback 是否必须保留、未来泄漏用代码级字段隔离还是仅 prompt。

---

## 8. 建议 Codex 产出的工件

1. `benchmark_semantics_and_visibility.md`：评测目标、mode/profile 定义、各字段可见性、
   input.yaml mode 与 manifest profile 解析、Arm A/B 评测什么、Gold 按 blind/plan 分类。
2. `runtime_architecture_as_is.md`：代码事实级调用图、每步文件/函数/代码行/输入输出
   schema/字段血缘/LLM 调用点/截断预算/P003 工件路径。
3. `need_retrieval_trace_P003.md`：逐 Need 展示 焦点来源 → 生成依据 → query → route →
   filter → 直接检索结果 → corridor 新增 → rerank → accepted evidence → 最终 claim →
   对应 Gold；特别追踪 entity_encounter→黑羊、G06、G09、一个成功/漏召回/污染 Need。
4. `repair_options_decision_record.md`：方案 A（保持 blind 仅增强规则）/ 方案 B（visible_at
   cutoff 中保留"安全任务意图"，需 sanitizer 边界定义）/ 方案 C（新增 AUTHOR_PLAN_CONDITIONED
   Arm，backward chaining，plan 不进 historical claim，与 blind 分开评分）；每方案列出语义
   一致性、代码修改位置、泄漏风险、Gold 兼容性、预计收益、所需测试、对论文主张的影响。

---

## 9. 附：验证命令与产物路径

- b11 工件：`/tmp/ns-stage2m-v31-think-c60-b11-20260804/`（support_progress.json 含 10 边界
  审计 + 每请求预算字段；stage2m_case_C60_A.json 为冻结 verdict）
- 冻结 ledger：`reports/stage2m/isolated_projects/precise_p13_v2_20260730/visible_at_cutoff/
  objects/sha256/36/361cf9b9...5150f`（b11 writer_evidence_ledger）
- 关键代码位置：
  - `task_focus.py:71` extract；`task_conditioned_need_generation.py:253-318` context 拼接、
    `:335-609` 各模板 add、`:709` knowledge_state_context、`:753` _completion_contract
  - `retrieval.py:53` ROUTES、`:264` RRF、`:328` Orchestrator
  - `search_retrieval.py:479` OpenSearch search、`:551` _filters
  - `r1.py:653` R1 search、`r1.py:707` anchor 单元构造
  - `stage2_paired_pilot.py:519` Need 生成、`:728` 走廊接入、`:398` operational 指标
  - `gold_evidence_matching.py:33` match、`memory_benchmark_evaluation.py:530` _compare
  - `stage2_paired_pilot.py:399-431` operational_constraint_gold 读取点
- 数据文件：`benchmarks/private/ztj_memory_pilot_v0.1/cases/ZTJ-P003/{gold,input}.yaml`、
  `manifests/ZTJ-P003.json`（target=[61,65]、op_gold=[G06,G09]）
