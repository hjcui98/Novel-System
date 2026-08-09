# Stage 2M Need 生成链路审计与评测语义分析（Codex 增补版 v2）

- 用途：对 C60（ZTJ-P003）Need 生成 → 检索 → 输出链路的**代码事实审计**，
  评测语义（盲式 A / 计划条件 B / 双 Arm C）分析，以及基于审计结论的**框架改进方案**。
  v2 整合全部 Codex 决策，替换 v1 中被否定的设计。
- 状态：**Implementation accepted / formal Phase 4 admitted / Gate 0-3 pending**
- 日期：2026-08-05（初版）→ 2026-08-06（Codex 增补与修订 v2）→ 2026-08-08（最小充分工程边界补充）→ 2026-08-09（实现与正式运行准入验收）
- 依据：全部 `src/novel_agent/` 生产代码交叉核验 + 上位文档（`docs/project_status.md`、
  ADR-0004、`docs/stage2_memory_benchmark_task_closure_execution.md`）+ b9-b11 工件
- 标注：[已核验] = 代码直接确认；[Codex 新增] = 增补发现；[Codex 决策] = 架构决定

---

## 1. 端到端流程全解（已核验，增补 bundle 编译阶段）

### 1.1 总体链路（含 bundle 编译层）

```text
input.yaml (task / visible_outline / target_plan / mode)
  → HumanBenchmarkCompiler._plan_root()
    [visible_outline → PlanNode(node_type="visible_outline")]   ✅ 保留
    [target_plan → ChapterGoal(chapter_index, summary)]         ✅ 保留
    [task → 完全丢弃]                                            ❌ 丢弃
    [mode → 完全丢弃]                                            ❌ 丢弃
  → BenchmarkScenarioCompiler.compile()
    [PlanRoot → evaluator-only metadata, 不进 BootstrapSource]
    [gold → READ_GOLD source, evaluator-only]
  → BenchmarkBundleImporter (读预编译 JSON, 不读 input.yaml)
  → memory_benchmark_contract.py::build_safe_task_contract()
    [task_text = 硬编码模板 (memory_context_task.v1)]
    [assert_safe_public_payload 阻断 target_plan/gold/future 字段名]
  → build_public_checkpoint_case()
    [VISIBLE_AT_CUTOFF → plan_root_ref = None]
    [AUTHOR_PLAN_CONDITIONED → plan_root_ref 保留]
  → stage2_paired_pilot.py::resolve_state_case()
    [verify_public_checkpoint_case → SHA-256 hash 绑定]
    [_scope_needs → 非 APC 时强制 access_scope="writer_safe", allow_plan=False]
  → ModelCurator 提取+验证 → WorldRootDocument
  → TaskFocusExtractor → FocusSet
  → TaskPlanConditionedNeedGenerator → ~24 个 Need
  → DeterministicChannelPlanner → RoutePlan（仅供 controller/legal_actions）
  → RetrievalOrchestrator → ROUTES[query_intent] 静态路由 → 8 通道检索 + RRF
  → 走廊（家族折叠/workset/chunks/合成/验证）
  → WriterContextAssembler → Writer 4000 / Ledger 12000
  → GoldEvidenceMatcher + MemoryBenchmarkEvaluator → verdict
```

### 1.2 关键事实

- 检索对象是**图谱投影**（R1 + OpenSearch anchor/grounded），不是原文自由检索。
- gold 只在 evaluator 侧；运行走廊全程不可见。
- C60 运行档位：`visible_at_cutoff`，`plan=None`，task_text 为模板。
- `input.yaml` 的 `task` 和 `mode` 在 bundle 编译阶段即丢弃；`visible_outline` 和
  `target_plan` 编译为 `PlanRootDocument` 但仅在 APC 模式下保留。

### 1.3 [Codex 新增] 字段血缘完整追踪

| input.yaml 字段 | 编译阶段 | 运行时 | 实际用途 |
|---|---|---|---|
| `task` | `_plan_root()` 不读 → **丢弃** | 不存在 | 被 `build_safe_task_contract` 模板替代 |
| `mode` | `_plan_root()` 不读 → **丢弃** | 不存在 | 档位由 CLI `--information-profile` 决定 |
| `visible_outline` | → `PlanNode(node_type="visible_outline")` | APC 时进入 `PlanRootDocument.nodes` | `TaskFocusExtractor.PLAN_INTENT` 可读 |
| `target_plan` | → `ChapterGoal(chapter_index, summary)` | APC 时进入 `PlanRootDocument.chapter_goals` | Need 生成 `plan_conditioned_history/plan_obligation` 模板可读 |

---

## 2. 焦点提取规则（已核验，task_focus.py）

| 来源 | 规则 | C60 实际来源 |
|---|---|---|
| TASK(0) | task_text 命名实体 substring match | **C60 为空**——模板 task_text 无命名实体 |
| OPEN_OBLIGATION(1) | OPEN/PROGRESSED 义务的 owner | 陈长生（婚约）、徐有容/秋山君（联姻）、落落（拜师） |
| PLAN_INTENT(2) | 仅 APC 且 plan 非空 | C60 不适用 |
| CUTOFF_FRONTIER(3) | 最近 12 事件 + 参与者 | 黑龙遭遇 + 陈长生/黑龙 |
| ALIAS_EXPANSION(4) | 别名归并 | — |
| ONE_HOP_RELATION(5) | 焦点实体一跳关系两端（max=12） | 轩辕破/落衡/茅秋雨等 |

主实体排序：`min(key=(非TASK源, -状态数, -事件数, 源优先级, 顺序))`。

---

## 3. Need 生成规则（已核验，task_conditioned_need_generation.py）

### 3.1 query 构造公式（实体焦点模板，逐条）

| # | need_type | query 公式 | 真实产物（截断） |
|---|---|---|---|
| 1 | current_state | `label + 前16个状态谓词` (line 318) | `陈长生 arrival_behavior name philosophy...` |
| 2 | continuity_constraint | `{label} 当前连续性 限制 条件...{state_context}` | 中文停用词 + 状态谓词拼接 |
| 3 | capability_boundary | `{label} 当前能力 能力边界...{state_context}` | 同上 |
| 4 | entity_history | `{label} 重要历史...{event_context} {relation_context} {obligation_context}` | 生成 3 个 query_hints |
| 5 | relationship_emotion | `{label} 关系 情绪...{relation_context} {target_plan_text}` | C60 无 plan_text |
| 6 | knowledge_boundary | `{label} 知情边界...{obligation_context} {knowledge_state_context} {relation_context}` | — |
| 7 | long_range_callback | `{label} 长线伏笔...{state_context} {target_plan_text}` | C60 无 plan_text |
| 8 | target_transition | 依赖 target_plan_text | C60 不生成 |

### 3.2 四+1 类 context 构造

- `state_context`：全部状态 `predicate + value`，[:2000]；**重要/琐碎无区分**
- `relation_context`：`predicate + 对方标签`，[:1000]
- `event_context`：**去重后 `event.event_type` 拼接**，[:1000]——最严重的信息压缩点
- `obligation_context`：OPEN/PROGRESSED 义务 description，[:1000]
- `knowledge_state_context`（第五种，line 709）：关键词白名单筛选，[:900]

### 3.3 非实体焦点模板（退化重灾区）

| 焦点 | need_type | query 公式 | 退化路径 |
|---|---|---|---|
| EVENT | causal_history | `query = event.event_type` (line 556) | BM25 命中图谱谓词 → entity filter 成为主约束 → 候选扩展为全部上下文 |
| RELATION | relationship_emotion | `subject + predicate + object` | predicate 是内部标签，同样退化 |
| OBLIGATION | unresolved_obligation | `owner + description` | 稍好（description 含自然语言） |
| STATE | current_state | `predicate + value` | predicate 是图谱键名，退化 |

### 3.4 [Codex 新增] predicates 字段空值问题

```
代码事实（已核验）：
- Stage1MemoryNeed 定义 predicates: tuple[str, ...] = ()
- TaskPlanConditionedNeedGenerator.add() (line 209-246) 从不设置 predicates 参数
- 局部变量 predicates 仅用于格式化 query_text (line 253/318)，不传给 Need 构造
- TierRouter._r1_eligible() 检查 bool(need.predicates)：
  - MANDATORY_CONSTRAINT 和 PLAN_NODE 因 entity_ids 非空仍可走 R1
  - 但 predicates-only 的精确匹配路径永远失效
```

**影响**：R1_EXACT 路由对仅需要谓词匹配的查询无法生效。

**修复约束**：在修复前必须先审计 R1 对 predicates 的实际语义：
- 多 predicate 是 AND 还是 OR？
- 是一次联合过滤，还是逐 predicate 查询后融合？
- predicates 只是路由 eligibility，还是实际 SQL 过滤条件？
- predicate 与 entity_ids 如何组合？

确认后按 need_type 精确填充，且不能把整个 `state_context` 的谓词全部塞入：
- `current_state`→经过筛选的状态谓词
- `capability_boundary`→仅能力和限制相关谓词
- `knowledge_boundary`→仅 knowledge-state 白名单命中的谓词
- `relationship_emotion`→关系谓词（非状态谓词）
- `long_range_callback`→不直接填当前 state predicate，应走 historical/anchor
- 其他类型保持空，除非有明确语义

**此修复为独立 patch（Phase 0C-1），不与 plan 通道改造混入同一阶段。**

### 3.5 facet / 完成契约

- capability_boundary → (CAPABILITY_STATUS, LIMITATION)
- relationship_emotion → (RELATION_STATE)
- knowledge_boundary → (KNOWLEDGE_BOUNDARY)
- long_range_callback → (SETUP, UNRESOLVED_STATUS)，`min_distinct_chapters=2`
- unresolved_obligation → (COMMITMENT, UNRESOLVED_STATUS)
- causal_history/历史 → (CAUSAL_HISTORY)；其余 → (CURRENT_STATE)
- mandatory 或 facet>1 → irreducible

### 3.6 Stage1MemoryNeed 完整 schema（已核验，domain/memory.py:236）

`need_id, run_id, task_id, base_commit, chapter_target, horizon_target, need_type,
query_intent, query_text, entity_ids, predicates, time_scope, access_scope, allow_plan,
hierarchy_parent_unit_ids, why_needed, risk_level, requirement, preferred_resolution_path,
allowed_candidate_pools, expected_evidence_types, stop_condition, purpose, expected_section,
focus_ids, priority, query_hints, completion_criteria, need_facets, completion_spec`

**不存在 `semantic_question` 字段**。`query_text` 是唯一查询文本。

### 3.7 [Codex 新增] query_hints 消费审计

```
代码事实（已核验）：
- 生成侧：仅 entity_history 类型生成 3 个 query_hints
- 消费侧：仅 paired_controller.py::_historical_fallback_need() (line 576-584) 读取
  → 该函数属于 Agentic Arm B 路径的回退机制
  → Arm A 的 RetrievalOrchestrator 和所有 OpenSearch Backend 完全不读 query_hints
- 结论：query_hints 在确定性 Arm A 管线中被完全忽略
```

---

## 4. 检索规则（已核验，增补路由断裂审计）

### 4.1 ROUTES 静态字典

按 `query_intent` 定通道（retrieval.py:53-97）：

- CURRENT_STATE/KNOWN_ID → R1_EXACT
- RELATED_EVENT → anchor_bm25 + anchor_dense（备 grounded）
- SEMANTIC_HISTORY → anchor/grounded
- RELATION_CHAIN/CAUSAL_MULTI_HOP → TYPED_GRAPH
- 等

### 4.2 过滤与融合

- 过滤器：project/basis/snapshot/kind/access_scope/observed-only/truth_class/
  entity_ids（grounded 不做实体过滤）/时间范围/章节目标
- **`information_label` 过滤**（已核验）：`need.allow_plan=False` 时追加
  `{"term": {"information_label": "observed"}}` → plan 记录不进历史检索
- BM25 multi_match：`text.standard^1.0, text.cjk^1.2, exact_terms^3.0`
- Dense：query_text 的 bge-m3 向量 knn
- Fusion：RRF（k=60）→ TypedCandidateSelector；per_channel_limit=20，fused_limit=20

### 4.3 [Codex 新增] ROUTES vs DeterministicChannelPlanner 断裂

Arm A（deterministic）走 ROUTES[query_intent] 静态字典；Arm B（agentic）走 RoutePlan →
DeterministicChannelPlanner → legal_actions。两套机制并行存在。

### 4.4 退化机制链（系统性模式）

**所有非实体焦点模板**都存在同类退化：

1. query_text = 图谱内部标签（event_type / predicate / value）
2. BM25 在 `exact_terms` 索引上命中图谱谓词而非原文叙事词
3. entity_ids filter 成为实际主约束
4. 候选扩展到该实体的全部历史上下文
5. 走廊 compatible/家族保留把远距章节拉进 workset
6. 模型回答它从 context 推断的问题，而非 gold 要求的问题
7. → **query specification failure**

---

## 5. 评测语义分析

### 5.1 定义 A / B / C

- **A：盲式**——只看截止点前正文，预测通用长距记忆。
- **B：目标条件式**——给定 target_plan，backward chaining 检索必须携带的历史记忆。
- **C：双 Arm**——A 测盲式维护，B 测精准装配，量化计划增益。

### 5.2 [Codex 决策] Profile 语义拆分（新增）

原 `visible_at_cutoff` vs `author_plan_conditioned` 不足以区分中间情形。

| Profile | 定义 | 可用输入 | 盲式可评性 |
|---|---|---|---|
| `HISTORY_ONLY` | 纯历史 + 通用任务模板 | 历史正文 + 模板 task_text | ✅ 全盲式 |
| `TASK_INTENT_ONLY` | 历史 + 具体任务意图，无章节计划 | history + input.yaml.task | ⚠️ 部分定向 |
| `AUTHOR_PLAN_CONDITIONED` | 历史 + task + visible_outline + chapter goals | 全部计划输入 | ❌ 计划条件式 |

当前模块正式运行使用 **APC**。`TASK_INTENT_ONLY` 用于分离"任务意图增益"和"章节计划增益"
的消融实验（不阻塞当前开发）。

### 5.3 用户确认的模块目标

- **当前模块正式目标 = 定义 B（APC）**：在未来规划 + 历史正文都存在时，为写作检索重要记忆。
- 半 A 场景暂不纳入。评测以 B 为主轴。

### 5.4 当前运行实际状态

- 正式运行 = **定义 A（HISTORY_ONLY）**：档位 `visible_at_cutoff`、plan=None、
  task_text 模板、input.yaml 的具体任务/outline/plan 全部未进入运行时。
- **gold 是 B 型语义**：G06/G09 的 why_needed 指向 61-65 章叙事决策。
- **错配**：A 档运行 + B 型 gold + input.yaml 暗示 B 意图但从未使用。

### 5.5 G06/G09 组件拆分

| 组件 | 性质 | 盲式可恢复性 | 判定依据 |
|---|---|---|---|
| G06 xu_is_in_south | ch2 长期状态 | ✅ 盲式可恢复 | 纯历史事实 |
| G06 no_direct_conversation | ch56 关系事实 | ✅ 盲式可恢复 | 纯历史事实 |
| G06 **cannot_claim_her_intent** | epistemic 边界 | ⚠️ plan-dependent | why_needed 引用未来写作约束 |
| G09 luoluo_enrolled / third_student | ch36/ch50 事实 | ✅ 盲式可恢复 | 纯历史事实 |
| G09 academy_protective_effect | ch56 事实 | ✅ 盲式可恢复 | 纯历史事实 |
| G09 **impact_is_risk** | 风险边界判断 | ⚠️ plan-dependent | why_needed 引用未来决策 |

### 5.6 [Codex 新增] 全部 5 个 Case Gold 概览与数据划分

| Case | Target | Gold 数 | 类型 | 关键观察 | 数据角色 |
|---|---|---|---|---|---|
| P001 | [21,25] | 8 + 5 plan | G04=PLAN_PATH | 仅 APC 可评 | **开发集** |
| P002 | [41,45] | 9 + 5 plan | — | 无 plan-only 类型 | **开发集** |
| P003 | [61,65] | 9 + 0 plan | G06/G09=operational_constraint | 计划暗示但 applicable 含 VAC | **验证集** |
| P004 | [81,85] | 待审计 | 待审计 | 待补字段完整性检查 | **测试集（冻结）** |
| P005 | [96,100] | 待审计 | 待审计 | 待补字段完整性检查 | **测试集（冻结）** |

**数据划分原则**（[Codex 决策]）：P001/P002=开发集；P003=验证集；P004/P005=冻结测试集。
允许根据 P003 暴露的问题调整 Planner prompt 和 Need 生成规则，但调整必须针对**可泛化的
失败机制**（如加强知情边界识别、加强跨实体组合 Need），不得针对具体 Gold 答案进行特化。
具体防过拟合规则见 §10.8。

### 5.7 [Codex 决策] plan-dependent 操作性判据

1. **blind-recoverable**：全部证据在截止点前，结论可由纯历史叙事显著性合理预期
2. **plan-dependent**：why_needed 直接引用未来章节目标/动作，或结论需知道作者计划才能定向提取
3. **hindsight-only**：结论只能在读过未来正文后才能判定（不应出现在评测中）

---

## 6. 关键核查结论（代码事实）

**原始 8 条（全部已交叉核验为正确）：**

1. [已核验] TASK 焦点在 C60 为空。
2. [已核验] current_state 的 query = label + 前 16 个状态谓词。
3. [已核验] knowledge_state_context 是第五种 context。
4. [已核验] 事件 `query = event.event_type`。
5. [已核验] `operational_constraint_gold` 被运行时读取用于指标，不注入检索。
6. [已核验] P001-P005 manifest target 配置正确。
7. [已核验] 无 G06/G09/章节号/实体名硬编码于生产代码。
8. [已核验] `plan_conditioned` 路径存在（~70%）但从未正式运行。

**Codex 新增 8 条：**

9. [Codex 新增] `need.predicates` 始终为空。
10. [Codex 新增] `query_hints` 在 Arm A 中完全无消费点。
11. [Codex 新增] `ROUTES` 与 `DeterministicChannelPlanner` 两套路由机制并行。
12. [Codex 新增] `input.yaml` 的 `task` 和 `mode` 在 bundle 编译阶段即丢弃。
13. [Codex 新增] Event schema 无 description/location/cause。仅 `event_type`、`participant_ids`、
    `story_time`、`narrative_order`、`effect_refs`、`evidence_refs`、`truth_class`。
14. [Codex 新增] `information_label` 过滤已有代码级闸门。
15. [Codex 新增] `_PRIVATE_FIELD_FRAGMENTS` 阻断 `target_plan` 字段名，但内容通过
    `PlanRootDocument` 间接进入运行时——设计意图，非安全漏洞。
16. [Codex 新增] P001 含 `PLAN_PATH` 类型 gold，profile 区分意识已存在但 P003 未做区分。

---

## 7. 断裂诊断：为什么 G06/G09 反复 MISS

### 7.1 因果链

```text
input.yaml.task 丢弃 → 模板 task_text 无实体 → TASK 焦点为空
                                                      ↓
input.yaml.mode 丢弃 → CLI 强制 visible_at_cutoff → plan=None → 无 PLAN_INTENT 焦点
                                                      ↓
焦点仅来自义务/前沿/一跳 → Need 的 query_text = 机械拼接（标签+谓词+event_type）
                                                      ↓
query_text 不含自然语言问题 → BM25 命中图谱谓词 → entity_ids 成为主约束
                                                      ↓
候选扩展到全部历史 → 走廊保留远距章节 → 模型回答推断的问题而非 gold 要求的问题
                                                      ↓
G06 要求"知情边界"组合 → Need 从未问过 → UNTRACEABLE/MISS
G09 要求"保护效应+风险"组合 → Need 从未问过 → MISS
```

### 7.2 根因分层

| 层 | 问题 | 严重程度 |
|---|---|---|
| 配置层 | VAC 档位运行 + B 型 gold | 🔴 错配 |
| 契约层 | task_text 模板，具体任务语义丢失 | 🔴 根因 |
| Need 生成层 | 无 semantic_question，query 是谓词拼接 | 🔴 根因 |
| 检索层 | event query 退化，query_hints 未消费 | 🟡 加重因素 |
| 走廊层 | 已修复（R4-R7），非当前瓶颈 | ✅ |
| 模型层 | 已证明能力足够（b11 thinking），非瓶颈 | ✅ |

---

## 8. [Codex 决策] 框架改进方案（2026-08-06 v2 最终版）

### 8.1 前提决策

| # | 决策项 | 决定 |
|---|---|---|
| D1 | 模块目标 | **定义 B（Plan-Conditioned / APC）** |
| D2 | 档位策略 | APC 为主；HISTORY_ONLY 保留为对照；新增 TASK_INTENT_ONLY 用于消融；文档中不再使用"VAC"作为模糊统称 |
| D3 | input.yaml 字段 | **全部类型化编译并受控传递**：task/mode/visible_outline/target_plan 均由 HumanBenchmarkCompiler 读取，经校验、规范化后通过 `AuthorPlanningContext` 传递；原始 YAML 字段不进入普通 public payload |
| D4 | LLM Need Planner | **Phase 1 使用 LLM**；Need 生成属问题规划层 |
| D5 | hash 校验 | **全部保留**；`_PRIVATE_FIELD_FRAGMENTS` 不做删除，改为新增 `AuthorPlanningContext` 窄通道 |
| D6 | 实验 | 旧冻结对象保留为 legacy baseline；新架构全量重跑 P001-P005 |
| D7 | Event schema | **暂缓（defer）**：MVP 用 participant_ids + evidence surface terms，检索 trace 后再决定 |
| D8 | 并发调度 | 独立进行，与语义修复正交 |
| D9 | 三层 plan 权限 | `allow_plan` 拆为 `planner_may_read_plan` / `retrieval_may_return_plan` / `claim_may_cite_plan` |
| D10 | P004/P005 | Phase 0 前置审计，与 P001-P003 同步维护 |
| D11 | 数据划分 | P001/P002=开发集，P003=验证集，P004/P005=测试集（冻结） |
| D12 | Planner 输出 | 三层：LLM→PlannedNeedDraft（语义，无图谱ID）→Grounder→GroundedNeedDraft→Validator→Stage1MemoryNeed |
| D13 | 单一事实来源 | `AuthorPlanningContext` 为 task_intent/profile/target_range/outline/goals 的唯一权威来源。Manifest、Task Contract 等只保存 `planning_context_ref` + `planning_context_hash` + 必要派生字段 |
| D14 | `allow_plan` 迁移 | 新增三个分层策略；`allow_plan` 标记 deprecated；过渡期 `legacy_allow_plan = retrieval_may_return_plan`（APC 下保持 False）；完成迁移后删除 |
| D15 | 最小充分工程 | 只实现关闭已核验责任层缺口和 Gate 0-3 所需的机制；优先复用/收窄现有 owner，不新增第二 Need/检索/评测管线、通用规则 DSL、平台或未来扩展层 |

#### 最小充分工程的适用判据

一项新增类型、Service、配置维度或持久化产物只有同时满足以下条件才进入本方案：它对应已核验
失败或已接受合同；有唯一责任层和当前调用方；保护一个命名的不变量或验收信号；现有 owner 的
窄扩展无法正确表达。否则先删除、合并、接线、收窄或保持 `deferred`。

本原则禁止为当前 Need 修复预建第二套 Planner/Query/Evaluator 框架、动态 ontology/rule engine、
插件系统、通用工作流平台或新存储后端。它不允许省略 `AuthorPlanningContext` 单一真源、D9、
hash/profile/Gold 隔离、typed failure、Planner lineage、测试、真实 artifact 和分层 Gate；这些是
“充分”的组成部分，不是可裁剪的工程附加项。

### 8.2 P0-1：拆分 plan 权限（`allow_plan` → 三个独立字段）

旧 `allow_plan: bool` 同时控制「Planner 能否看计划」「Retriever 能否返回计划记录」「Claim 能否
引用计划」，三层行为无法独立配置。

新方案分为三个独立策略字段，且归属不同层级：

| 字段 | 归属 | APC 固定值 | 语义 |
|---|---|---|---|
| `planner_may_read_plan` | `AuthorPlanningContext` / 运行 Profile | `True` | Planner 可读取计划 |
| `retrieval_may_return_plan` | Retrieval/Evidence Access Policy | `False` | 检索只返回 observed history |
| `claim_may_cite_plan` | Claim Support Policy | `False` | claim 只能引用 observed evidence |

最终 Need 可保存一份解析后的 policy snapshot（审计用），但不允许单条 Need 自行放宽策略。
现有 `information_label="observed"` 过滤和 `_is_plan_information` 闸门保留作为代码级防御。

**`allow_plan` 迁移方案**：
1. 新增三个分层策略字段（上表）；
2. `Stage1MemoryNeed.allow_plan` 标记为 `deprecated`；
3. 过渡期内统一派生：`legacy_allow_plan = retrieval_may_return_plan`；
4. APC 固定 `retrieval=False` → 旧检索路径中的 `allow_plan` 保持 `False`——不能因为
   Planner 可看计划就设成 `True`；
5. 完成所有调用方（检索过滤、controller legal actions、scope_needs）迁移后，
   从 `Stage1MemoryNeed` 删除 `allow_plan`。

### 8.3 P0-2：类型化 `AuthorPlanningContext` 窄通道（替代"删除黑名单字段"）

**全部保留** `_PRIVATE_FIELD_FRAGMENTS`（含 `target_plan`、`preparation`、`gold`、`future`、
`accepted_evidence`）。不做任何删除。

新增仅由 `HumanBenchmarkCompiler` 生成的类型化结构：

```python
@dataclass(frozen=True)
class AuthorPlanningContext:
    profile: InformationProfile
    task_intent: str
    target_range: tuple[int, int]
    visible_outline_nodes: tuple[VisibleOutlineNode, ...]
    chapter_goals: tuple[ChapterGoal, ...]
    source_hash: str
```

运行时接收的是经过 schema、profile 和 source_hash 验证的 `AuthorPlanningContext`，不使用裸
`target_plan`/`preparation` 字段名。内部已编译为 `chapter_goals` 等规范化字段，不会触发 taint。

### 8.4 P0-5：拆分 Planner、Grounder、Validator、Compiler

| 文件 | 职责 |
|---|---|
| `plan_conditioned_need_planner.py`（新） | Plan + task_intent → `PlannedNeedDraft`（LLM prompt、JSON 解析、backward chaining） |
| `need_draft_grounder.py`（新） | 自然语言 entity/relation mention → canonical entity_id/relation_id |
| `need_validator.py`（新） | 时间边界、事实化检查、去重、预算、facet、completion contract |
| `need_query_compiler.py`（新） | Stage1MemoryNeed → `RetrievalQueryBundle`（各通道编译查询） |
| `task_conditioned_need_generation.py`（保留） | 旧模板 fallback、最终 Need 构造、Planner/Grounder/Validator 的轻量 orchestrator |

### 8.5 三层 Planner 输出（LLM → Grounder → Validator）

**第一层：LLM Planner → `PlannedNeedDraft`**

LLM 只输出语义信息，不得包含图谱 ID：

```yaml
draft_id:
semantic_question:        # 自然语言问题
entity_mentions:           # [{label, role_in_need}]
relation_mentions:         # [{subject_label, relation_label, object_label}]
trigger_plan_chapters:
trigger_plan_goal:
why_needed:
required_claim_scopes:
suggested_facets:
historical_time_scope:
```

`entity_mentions[].label` 是自然语言（如"落落"），不是 entity_id。

**第二层：Grounder → `GroundedNeedDraft`**

确定性代码从图谱中通过名称/别名查找 canonical entity：

```yaml
grounded_entities:
  - mention: 落落
    canonical_label: 落落
    entity_id: entity-luoluo-001
    confidence: 1.0
    grounding_method: exact_alias_match
    grounding_status: GROUNDED       # GROUNDED | AMBIGUOUS | UNRESOLVED
```

同名/歧义时按章节范围与关系上下文消歧；无法消歧时标记 `AMBIGUOUS` 或 `UNRESOLVED`，
不允许 LLM 猜测 ID。

**第三层：Validator → 最终 `Stage1MemoryNeed`**

包含所有图谱 ID、focus_ids、facets、completion spec、evidence policy 和 time scope。
`focus_ids` 由 grounded entities 确定性派生，不来自 LLM。

### 8.6 分阶段修复方案

#### Phase 0A：语义和输入链路

| # | 文件 | 修改 |
|---|---|---|
| 0A.1 | `human_benchmark_compiler.py` | 新增 `_compile_planning_context()`：从原始 input.yaml 编译 `AuthorPlanningContext`（task/mode/visible_outline/target_plan → 规范化字段） |
| 0A.2 | `domain/benchmark.py` | 新增 `AuthorPlanningContext`、`VisibleOutlineNode`；`BenchmarkCaseManifest` 保存 `information_profile`（规范化后）和 `planning_context_ref`、`planning_context_hash`，不再重复保存完整 task_text |
| 0A.3 | `domain/writer_context.py` | `BenchmarkTaskContract` 新增 `task_intent: str`（从 PlanningContext 派生）、`planning_context_ref`、`planning_context_hash` |
| 0A.4 | `memory_benchmark_contract.py` | `build_safe_task_contract` 采用**组合**（非替代）：固定安全契约（不续写/不使用 Gold/不泄漏未来/确定性结论引用历史证据）+ 规范化后的 task_intent；`_PRIVATE_FIELD_FRAGMENTS` 不动；taint/hash 逻辑全部保留 |
| 0A.5 | `scripts/run_stage2_teacher_forced_e2e.py` | `--information-profile` 默认从 `AuthorPlanningContext.profile` 解析 |
| 0A.6 | `stage2_paired_pilot.py` | APC 时注入 `planner_may_read_plan=True`、`retrieval/claim=False` |
| 0A.7 | schemas/ | 新增 schema 导出 |
| 0A.8 | P001-P005 manifest/data | 全部 5 个 case 的 input.yaml 字段完整性审计与补全 |

**验收（Gate 0）**：
- APC PlanningContext 正确进入 Planner 上游
- `planner_may_read_plan=True`、`retrieval_may_return_plan=False`、`claim_may_cite_plan=False`
- observed-only evidence、零 leakage
- hash/profile 正确
- 暂不改变 Need 生成结果

#### Phase 0B：现有 APC 模板链路冒烟

使用**已有模板**验证 plan 输入恢复：

| # | 修改 |
|---|---|
| 0B.1 | APC 下 plan 可见、影响 Focus 和现有 plan-conditioned templates |
| 0B.2 | historical retrieval 保持 observed-only |
| 0B.3 | claim 不引用 plan |
| 0B.4 | leakage 为零 |
| 0B.5 | VAC 等其他 profile 不会意外看到 plan |

**验收**：plan 输入断链修复确认；旧 `TaskPlanConditionedNeedGenerator` 能生成 plan-related Need。

#### Phase 0C：独立 bug 修复（三个独立 patch）

| Patch | 内容 | 验收 |
|---|---|---|
| **0C-1 （predicates）** | 先审计 R1 predicates 语义（AND/OR/联合/逐条/eligibility），再按 §3.4 规则分 need_type 填充 | 检索 trace 对照，不引入过约束 |
| **0C-2 （EVENT query）** | fallback 模板改进：`query = event_type + participant_labels + evidence_refs 原文表面词` | EVENT Need 不再被 entity_encounter 退化主导 |
| **0C-3 （query_hints）** | 激活 Arm A 对 `query_hints` 的消费（此前仅 Arm B 使用）。注意：此为 legacy template path 修复；新 Planner 路径由 Query Bundle 取代 | 辅助 BM25 查询生效 |

#### Phase 1：LLM Need Planner + Grounder + Validator

**前置数据契约**（新增文件 `domain/planning_memory.py`）：

| 类型 | 用途 |
|---|---|
| `PlannedNeedDraft` | LLM Planner 输出的语义草稿（无图谱 ID） |
| `GroundedEntityMention` | 单个实体的 grounding 结果（mention→entity_id+confidence+method+status） |
| `GroundedRelationMention` | 单个关系的 grounding 结果 |
| `GroundedNeedDraft` | Grounder 输出（含 canonical IDs） |
| `PlannerWorldSummary` | Planner 输入的确定性 WorldRoot 摘要（截止章节、显式实体、义务、近期事件、关键关系、别名、记录计数与截断规则） |
| `PlannerArtifactMetadata` | run 级 lineage：model/revision/prompt_hash/world_summary_hash/raw_response_hash/fallback_status/seed |

Lineage 归属：
- 完整的模型+prompt+world_summary+raw_response hash → `PlannerArtifactMetadata`（run 级）
- 每条 `Stage1MemoryNeed` 只保存 `planner_artifact_ref`、`planned_draft_id`、`validated_need_set_hash`
- 不在每条 Need 上重复整套模型元数据

| # | 文件 | 修改 |
|---|---|---|
| 1.1 | `domain/memory.py` | `Stage1MemoryNeed` 新增 `semantic_question: str`、`trigger_plan_chapters`、`planner_artifact_ref`、`planned_draft_id`、`validated_need_set_hash` |
| 1.2 | **新** `domain/planning_memory.py` | `PlannedNeedDraft`、`GroundedNeedDraft`、`PlannerWorldSummary`、`PlannerArtifactMetadata` 等正式数据契约 |
| 1.3 | **新** `plan_conditioned_need_planner.py` | LLM Planner：输入 `AuthorPlanningContext` + `PlannerWorldSummary` → 输出 `PlannedNeedDraft` 列表 |
| 1.3 | **新** `need_draft_grounder.py` | 确定性 Grounder：entity/relation mention → canonical ID。歧义标记 AMBIGUOUS/UNRESOLVED |
| 1.4 | **新** `need_validator.py` | 时间边界、事实化检查、去重、预算、facet、completion contract |
| 1.5 | `task_conditioned_need_generation.py` | 保留旧模板为 fallback；APC+plan 非空时调用 Planner→Grounder→Validator 链 |
| 1.6 | `task_focus.py` | `extend()` 接口：Grounded entities 回填 FocusSet |
| 1.7 | schemas/ | 重导出 |

**LLM Planner 可复现性要求**：

```
planner_model, planner_model_revision, planner_prompt_version, planner_prompt_hash,
planner_output_schema_version, temperature, requested_seed, effective_seed_supported,
planning_context_hash, world_summary_hash, raw_response_hash, validated_need_set_hash,
fallback_used
```

Benchmark 重跑默认读取冻结 Planner artifact；更换模型或 prompt 后须产生新 run lineage。
Fallback 运行须显式标记 `PLANNER_FALLBACK`，不与正常 APC Planner run 混合统计。

**验收（Gate 1）**：
- target plan goals 覆盖充分
- semantic question 可由历史回答
- 无未来事实化
- Grounding 成功率达标（AMBIGUOUS/UNRESOLVED 有显式处理）
- fallback 率受控
- P003 失败分析驱动通用能力改进（非特化）；遵守 §10.8 防过拟合规则

#### Phase 2：Per-channel Query Compilation

| # | 文件 | 修改 |
|---|---|---|
| 2.1 | **新** `need_query_compiler.py` | 定义 `RetrievalQueryBundle`；Stage1MemoryNeed → 各通道独立查询 |
| 2.2 | `search_retrieval.py` | Dense→`semantic_query`；BM25→`lexical_queries`（不再用旧 `query_text`） |
| 2.3 | `retrieval.py` | R1→`exact_entity_ids + exact_predicates`；Graph→`graph_seeds + graph_relations`；Reranker→`semantic_query` |
| 2.4 | 检索 trace | 区分 direct retrieval 与 corridor expansion |

`RetrievalQueryBundle`：

```python
@dataclass(frozen=True)
class RetrievalQueryBundle:
    semantic_query: str
    lexical_queries: tuple[str, ...]
    exact_entity_ids: tuple[str, ...]
    exact_predicates: tuple[str, ...]
    graph_seeds: tuple[str, ...]
    graph_relations: tuple[str, ...]
    time_scope: TimeScope
    excluded_information_labels: tuple[str, ...]
```

**路由集成决策**（[Codex 决策]）：

`RetrievalQueryBundle` 接入方式为**取交集**：
- `ROUTES[query_intent]` 仍负责确定允许的通道集合；
- `RetrievalQueryBundle` 提供各通道的具体查询；
- 实际执行通道 = 两者交集；
- `DeterministicChannelPlanner` 暂不重写，后续可与 ROUTES 统一。

```text
allowed channels from ROUTES[query_intent]
∩
queries available in RetrievalQueryBundle
=
本次实际执行通道
```

**验收（Gate 2）**：
- direct evidence recall 提升
- BM25 不再由内部 `event_type/predicate` 主导
- R1 predicates 语义正确
- direct retrieval 与 corridor expansion 可区分
- 候选污染率不恶化

#### Phase 3：评测拆分

**指标五段**（Leakage 独立，不与 Accuracy 合并）：

| # | 指标 | 说明 |
|---|---|---|
| 1 | Plan Goal Coverage | 目标计划中的章节 goal 是否被 Planner 覆盖 |
| 2 | Need Recall | Planner 是否提出了正确的问题（依赖 `gold_need_spec`） |
| 3 | Evidence Recall | 正确 Need 下的证据召回 |
| 4 | Completion / Claim Accuracy | 最终 claim 正确性与完整度 |
| 5 | Plan / Future Leakage | **零容忍**，不被 Accuracy 抵消 |

Leakage 为独立安全指标，不与 Claim Accuracy 合并加权。

**Need Recall 前置数据需求**：仅用现有 Gold claim 无法确定性判断 Planner 是否问对问题。
需为每个 Gold 增加（或在独立 `gold_need_spec.yaml` 中定义）：

```yaml
required_need_scopes:
  - marriage_knowledge_boundary
  - direct_communication_status
required_entities:
  - 陈长生
  - 徐有容
required_facets:
  - KNOWLEDGE_BOUNDARY
  - RELATION_STATE
```

否则 Need Recall 只能依赖另一个 LLM 判断，不可复现。

| # | 修改 |
|---|---|---|
| 3.1 | 五段指标（Plan Goal Coverage / Need Recall / Evidence Recall / Completion+Accuracy / Leakage）；Leakage 独立，不与 Accuracy 合并 |
| 3.2 | 新增 `gold_need_spec`（required_need_scopes/required_entities/required_facets），否则 Need Recall 无法确定性计算 |
| 3.3 | blind/plan gold 分类（按 §5.7 判据） |
| 3.4 | leakage 定量指标（`information_label=="plan"` 引用计数） |
| 3.5 | 报告扩展为五段视图 |

#### Phase 4：P001-P005 全量重跑

- P004/P005：Phase 0A 完成数据审计后立即**冻结**（生成 hash），之后不根据运行结果修改输入或 Gold
- 主要运行：`AUTHOR_PLAN_CONDITIONED`
- 消融一：`TASK_INTENT_ONLY`
- 旧基线：`HISTORY_ONLY`
- 旧冻结对象保留为 legacy baseline（不横向汇总，但用于孤立比较和回归诊断）
- 新架构结果按分层 Gate 0-3 验收

---

## 9. 分层 Gate

### 9.1 可执行条件（即刻固定）

| 条件 | 值 |
|---|---|
| `future_leakage_max` | **0** |
| `plan_evidence_leakage_max` | **0** |

### 9.2 分层标准

| Gate | 名称 | 验收标准 | 待标定阈值 |
|---|---|---|---|
| **Gate 0** | 输入语义正确 | APC PlanningContext 可见；planner_may_read=True, retrieval/claim=False；observed-only evidence；零 leakage；hash/profile 正确 | — |
| **Gate 1** | Need 规划正确 | goal_coverage 达标；semantic question 可由历史回答；无未来事实化；grounding_success_rate 达标；planner_fallback_rate 受控 | `goal_coverage_min`、`grounding_success_rate_min`、`planner_fallback_rate_max` |
| **Gate 2** | 检索编译正确 | direct evidence recall 提升；BM25 不再由内部谓词主导；R1 predicates 语义正确；检索 trace 可分层；candidate_pollution 不恶化 | `evidence_recall_min`、`candidate_pollution_delta_max` |
| **Gate 3** | 端到端正确 | Claim Correctness 提升；Completion Coverage 提升；P004/P005 held-out 不回退；plan/future leakage=0；legacy baseline 和 APC 结果独立可比 | `need_recall_min`、`claim_accuracy_min` |

待标定阈值由 P001/P002 基线确定，冻结于 Gate 配置文件（建议 `benchmarks/gate_config.yaml`）。

---

## 10. 关键约束

1. 不改 Writer 4000 / Ledger 12000 / ADR-0004 / Gate 公式
2. 不改走廊语义（R4-R7 已修复）
3. 不改评估器 Gold Matcher（Phase 3 新增指标，不动现有逻辑）
4. 每 Phase 独立可回退（git baseline `420e163`）
5. Stage 3 冻结
6. hash 校验**全部保留**；`_PRIVATE_FIELD_FRAGMENTS` 不做删除；plan 通道走类型化 `AuthorPlanningContext`
7. P004/P005：Phase 0A 完成数据审计后**立即冻结**（生成 hash），之后不根据运行结果修改输入或 Gold
8. **防过拟合规则**（[Codex 决策]）：
   - 允许基于 P003/G06/G09 暴露的问题改进通用能力（如加强知情边界识别、反向推导、跨实体组合 Need），但 Planner prompt 和生产代码中**不得出现** Gold ID、Gold claim 原文、accepted evidence、或为命中特定 Gold 而加入的特定章节号/角色组合
   - 每次 prompt 调整必须记录：version + hash、修改原因、所针对的通用失败类型、P001-P005 整体结果变化
   - P003 可继续作为验证案例，不要求自动降级
   - P003/G06/G09 不得成为唯一优化目标；修改后必须检查其他 case 是否同步改善或至少不回退
   - 若某项调整只能提高单个 Gold 命中率而无法说明通用机制，或导致其他 case 退化，视为疑似过拟合，不予合并
9. 旧实验保留为 legacy baseline，不横向汇总
10. **最小充分工程**：
   - 先修正现有 owner 的语义和接线，不建立平行 Planner、检索、评测或 artifact/report 体系
   - Event schema、动态规则、学习型路由、插件平台和新基础设施继续延期，除非当前 trace/benchmark 证明没有它们无法关闭 Gate
   - 新抽象必须绑定当前调用方、责任层、不变量和验收；纯未来复用不是准入理由
   - deprecated 兼容路径必须有删除条件，不允许永久双轨
   - 不得以“避免过度工程”为由削弱类型、权限、证据、可观测性、测试、复现或 Gate

---

## 11. 已关闭的开放项

| # | 项目 | 决定 |
|---|---|---|
| O1 | 冻结对象重投影 | 旧保留为 legacy baseline；新架构全量重跑 |
| O2 | P001-P005 同步更新 | Phase 0A 同步完成 |
| O3 | LLM Planner 接口预留 | 不预留，Phase 1 直接上 |
| O4 | hash 校验 | **全部保留**；新增 `AuthorPlanningContext` 窄通道 |
| O5 | 并发调度 | 在 Phase 2 确定最终调用 DAG 和 Need 独立性后重新审计调度边界。并发只改变调用时间，不改变上下文/证据/Prompt/预算；并发度由调用独立性、`max-num-seqs` 和 KV token 容量共同决定。KV 不足时排队或降低并发，不允许截断上下文 |
| O6 | Event schema | **暂缓（defer）**，检索 trace 后再决定 |
| O7 | LLM Planner 文件 | 拆为 planner / grounder / validator / compiler 四个独立文件 |
| O8 | Planner 可复现性 | 完整 lineage 存入 `PlannerArtifactMetadata`（run 级）；Need 只存 ref + hash |
| O9 | allow_plan 拆分 | 拆为三个独立字段 + deprecated 标记 + 过渡派生方案 |
| O10 | LLM 输出 | 三层：PlannedNeedDraft（无图谱ID）→ Grounder → Stage1MemoryNeed |
| O11 | 数据划分 | P001/P002=开发集，P003=验证集，P004/P005=冻结测试集 |
| O12 | 验收门槛 | 从单 C60 改为分层 Gate 0-3（含 machine-checkable 阈值，Gate config 文件） |
| O13 | P004/P005 冻结 | Phase 0A 完成数据审计→生成 hash→冻结；Phase 1 调 prompt 前完成，之后不变 |
| O14 | query_hints 修复 | 标记为 legacy template path 修复；新 Planner 路径由 Query Bundle 取代 |
| O15 | 单一事实来源 | `AuthorPlanningContext` 为权威来源；Manifest/TaskContract 只存 ref + hash + 派生字段 |
| O16 | 评测分段 | Leakage 独立为第 5 段（零容忍）；Need Recall 依赖新增 `gold_need_spec` |
| O17 | 路由集成 | RetrievalQueryBundle 与 ROUTES 取交集执行；DeterministicChannelPlanner 暂不重写 |

---

## 12. 验证命令与产物路径

- b11 工件：`/tmp/ns-stage2m-v31-think-c60-b11-20260804/`
- 冻结 ledger：`reports/stage2m/isolated_projects/precise_p13_v2_20260730/visible_at_cutoff/objects/`
- 关键代码位置：
  - `human_benchmark_compiler.py:395` _plan_root（bundle 编译层，字段消失点）
  - `memory_benchmark_contract.py:47` build_safe_task_contract（模板生成）
  - `memory_benchmark_contract.py:19-26` _PRIVATE_FIELD_FRAGMENTS（**不做删除**）
  - `task_focus.py:71` extract
  - `task_conditioned_need_generation.py:209-246` add()（predicates 空值点，**不在此次改为 Planner**）
  - `task_conditioned_need_generation.py:556` event query 退化点
  - `retrieval.py:53-97` ROUTES
  - `search_retrieval.py:565-566` information_label 过滤
  - `claim_support.py:3348` _is_plan_information 闸门
  - `stage2_paired_pilot.py:1394-1405` _scope_needs
- 数据文件：`benchmarks/private/ztj_memory_pilot_v0.1/cases/ZTJ-P00X/`

---

## 13. 2026-08-09 实现验收与正式运行边界

Codex 已按当前最终源码和真实工件接受 Phase 0A-3、endpoint-global scheduler、Planner 冻结重放和
bounded serial/safe-concurrent 准入机制。接受的 executable identity 为 HEAD `420e163`、producer
`trusted_claim_support_producer.v32`、source fingerprint
`sha256:20daa522f815c88c5ab823d2b03ff896b6751264dd6edac2777a4d93b089b881`；工程证据为
`1642 passed, 9 deselected`、100% branch coverage 和全量 pre-commit 通过。

P002/C40 使用非 fallback Planner artifact
`sha256:a1231b1d4bf4022295b06b44034d2ee8e953fdba70711327490f2db70c3e3ee2` 完成冻结重放。最终
Claim Support multi transport policy 固定为 `thinking=false / reasoning budget=0 / max output=2048`。
当前本地 Qwen endpoint 的实测安全配置为 `support Need concurrency=2 / endpoint request limit=1`；
endpoint limit 2 会使相同 prompt 异常增长并 length-truncate，故不得用于本次正式矩阵。

current-fingerprint serial/safe-concurrent 两侧均完成完整 14-request set、两个
proposal→whole-verifier→verified-receipt 链，语义输入、workset、Ledger 和 five-segment parity 成立；
future/plan leakage、timeout、OOM、context reduction 和 lease leak 均为 0。完整决定与正确 paired
artifact 映射见 `.agent/review.md`。

因此 Phase 4 的**工程准入前置条件**已经满足；下一步按 `.agent/plan.md` §6.3 先形成 clean
executable-source identity，再用新 DB、output root 和 experiment ID 运行 APC P001-P005 与 TIO
ablation。此状态不等于 Gate 0-3 或 Stage 2M PASS；阈值、held-out 结果和最终 Gate 决定仍由正式矩阵
产生。不得在正式运行中临时调整 prompt、2048 guard、Writer/Ledger budget、P004/P005 或 Gate 阈值。
