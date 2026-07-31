# 长篇小说 Agent 技术与执行评审建议

**版本**：v0.1  
**日期**：2026-07-20  
**文档生命周期**：`HISTORICAL_REVIEW`

**当前进度来源**：`docs/project_status.md`
**评审对象**：

- 《长篇小说 Agent 资产、世界模型、控制平面、运行与自演化总体架构设计》v2.2；
- 《长篇小说 Agent 技术实施与选型设计》v0.1；
- 《长篇小说 Agent 正式开发执行规划》v0.1。

**评审边界**：总体架构按当前方案冻结讨论，不提出重构五 Root、Canonical / Derived、L0/L1/L2、R0/R1/R2、受控提交等总体结构的意见。本文件只评审技术实现细节、三份文档的一致性，以及执行规划是否足以验证蓝图。

**2026-07-20 处置记录**：建议 1～4 已由用户确认并写入正式开发执行规划；真实 Benchmark 由用户后续构造，当前工程只实现通用 BenchmarkBundle 接入与 Runner。模型预训练记忆污染不纳入当前执行门禁。

---

## 1. 总体判断

三份文档的主方向一致，且执行规划将“先实现 Writer，再补检索”调整为“先验证 Memory & State Kernel，再进入生成闭环”是正确的。当前真正需要补强的不是总体架构，而是以下几个具体执行契约：

1. 将“优先命中 L1 Anchor”从一句原则，落实为可测试的分层级联检索流程；
2. 明确 Anchor 与 BM25 不是互斥技术：Anchor 是被检索的对象层级，BM25 / Dense 是检索 Anchor 的算法；
3. 区分必须绕过 Anchor 的查询，例如当前状态精确查询、原句定位和连续文风取样；
4. 为 Stage 1B 补上最小 Derived Snapshot、Outbox、Freshness 和索引传播闭环，否则连续 Commit Replay 会读取旧 Anchor 或旧 OpenSearch 文档；
5. 将 Stage 1A 的检索实验拆成“定位 Anchor”和“展开 L0 证据”两个阶段分别测量，不能只测最终 Recall@20；
6. 明确 Stage 1 尚未启用 R2 Memory Controller 时，谁负责 Query Intent 分类、通道路由和停止判断；
7. 对未来正文隔离、模型可能记忆公开小说、Gold 标注可靠性和统计置信度增加更严格的实验控制；
8. 对 OpenSearch 中文分词、原生 RRF 版本能力、reranker 截断和候选池配额形成明确的专项验证项。

因此，当前建议是：**总体架构不动，技术主栈也不急于更换；先修订检索执行契约和 Stage 1 的实验/传播闭环，再开始开发。**

---

## 2. 最重要的概念澄清：Anchor 与 BM25 不在同一个选择维度

当前讨论容易被表述成：

```text
先 Anchor，还是先 BM25？
```

这不是准确的问题。应拆成三个正交维度：

```text
检索对象：L1 Anchor / L0 Grounded Chunk / Canonical Record
检索算法：Exact / BM25 / Dense / Graph / Temporal / Hierarchy
执行顺序：先定位 / 再排序 / 再展开 / 再判断是否补搜
```

推荐的默认语义检索路径是：

```text
先用 BM25 + Dense + 结构过滤检索 L1 Anchor
    → 在 Anchor 候选上融合、去重和 rerank
    → 只对入选 Anchor 展开对应 L0 Evidence
    → 对展开后的证据做支持性、冲突和预算判断
    → 必要时才补搜 L0、图或更多 Anchor
```

这正好实现用户提出的目标：先通过紧凑锚点缩小阅读范围，再决定是否读取 Scene、Block、Span 或整章，从而控制延迟、reranker 输入和最终 Context token。

但它不应成为所有查询的绝对固定路径。R0/R1 精确读取、精确原句检索和文风连续片段检索存在更短、更可靠的路径。

---

## 3. 推荐的检索级联

### 3.1 查询先分类，再选择入口

| Query Intent | 第一入口 | Anchor 的作用 | L0 展开策略 |
|---|---|---|---|
| 当前人物位置、物品持有人、当前伤势、指定 Plan Node | R0 / R1 Exact + Temporal | 通常不参与 | 仅在需要证据核验时展开 EvidenceRef |
| 实体别名、已知 ID、确定 Predicate | R1 Exact / Entity | 可作补充导航 | 发生歧义、冲突或需原文时展开 |
| 某段历史是否与当前场景相关 | L1 Fact/Event/Scene Anchor 的 BM25 + Dense | 主入口 | 展开排名靠前且能闭合 Need 的证据 |
| 伏笔、因果、物品流转、人物关系链 | Graph / Hierarchy 定位 Anchor | 主入口或图路径节点 | 对关键边和关键结论展开原文 |
| 全卷、人物弧、长期主题 | Arc/Volume/Chapter Anchor，自上而下 | 主入口 | 先下钻 Scene/Fact Anchor，再展开原文 |
| 精确台词、罕见短语、原句出处 | L0 lexical/BM25 或 quote index | Anchor 只作旁路补充 | 直接返回命中 Span 及小范围窗口 |
| 文风、人物声音、对话节奏 | L0 Scene/Block 样例 + 可选 Style Anchor | Anchor 负责选样本范围 | 必须读取连续原文，摘要不能替代 |
| 高风险事实审计、真值晋升 | R1/Canonical + L1 导航 | 不能作为最终证明 | 必须展开 Canonical Record 与 L0 Evidence |

结论是：**“Anchor-first”应是语义历史检索的默认路径，而不是 Exact、Quote、Style 和 Constraint 查询的统一入口。**

### 3.2 建议的在线流程

```text
0. 固定 QueryContract
   base_commit / snapshot / worldline / story_time / narrative_position
   POV / audience / access_scope / truth_policy / token & latency budget

1. R0 Context-local Resolve
   已在 ContextPackage / Working State 中则直接返回

2. R1 Exact + Temporal + Mandatory Closure
   精确状态和硬约束直接进入 mandatory 区，不参与相关性竞争

3. Query Intent 与 Need 分解
   entity / predicate / event / time / plan / evidence / style / quote

4. 选择候选空间
   默认语义历史查询：L1 Anchor corpus
   原句查询：L0 corpus
   全局查询：Hierarchy 上层 Anchor
   多跳查询：Typed Graph + Anchor refs

5. 在检索前执行 Scope Filter
   project / source_commit or snapshot / worldline / time / lifecycle
   access_scope / information_label / truth_class / anchor_type

6. 粗召回
   Anchor BM25 + Anchor Dense，可按 Query Intent 加入 Graph / Hierarchy

7. 候选融合和去重
   每通道保留独立 rank、来源、命中理由和配额

8. Anchor rerank
   只对小候选集运行 cross-encoder；输入使用紧凑 Anchor，不送整章

9. Evidence Expansion
   Anchor → claim-level SourceRef → L0 Span + bounded surrounding window

10. Evidence-level 检查
    current support / conflict / truth / time / scope / provenance

11. Sufficiency 判断
    mandatory gaps、支持证据、冲突、未知、预算与停止原因

12. 最多一轮补搜
    Anchor 漏召回 → L0 BM25/Dense fallback
    关系缺口 → Graph expansion
    原文不足 → 扩大到 Scene，必要时才整章

13. Context Compiler
    mandatory 与 relevance 分区、去重、预算、Manifest 和警告
```

### 3.3 不建议默认“阅读全文”

Evidence Expansion 的常用最小单位应按以下顺序扩大：

```text
claim-level Span
    → Span 所在 Block
    → 相邻 Block 的有限窗口
    → 所在 Scene
    → Chapter
```

只有满足下列条件之一时才读整章：

- Anchor 的证据覆盖不足，无法确定其语义边界；
- 同章多个相距较远的证据共同决定结论；
- 需要判断跨场景语气、节奏或叙述误导；
- 证据冲突必须读取完整叙事上下文；
- Chapter Summary 与 claim-level provenance 不一致；
- 高风险人工/模型审计明确要求完整章级证据。

“为了保险而整章读取”不应是默认策略；它会直接消除 Anchor 带来的成本和上下文收益。

### 3.4 Anchor 类型必须分层，不能混成一个候选池

建议至少区分：

```text
fact_anchor
state_anchor
relation_anchor
event_anchor
scene_anchor
chapter_anchor
arc_or_volume_anchor
plan_obligation_anchor
style_or_skill_anchor
```

一次检索应先按 Query Intent 决定可参与的类型和每类配额，再排序。否则 Chapter Summary 往往因覆盖词多而压过更精确的 Fact/Event Anchor；短 Fact Anchor 又可能因信息太少在 Dense/Rerank 中被长摘要压制。

建议 OpenSearch 中至少保留两个逻辑候选空间：

```text
Anchor Index / Alias
    面向 L1；默认语义入口

Grounded Index / Alias
    面向 L0 Text / Reference chunks；用于 quote、style 和 fallback
```

二者可以位于同一个 OpenSearch 集群，也可以使用同一物理 index 的严格 `retrieval_unit_kind` 过滤，但 benchmark 必须能够分别统计、分别限额，不能把 L0 与 L1 混在同一个 top-k 中直接比较原始分数。

这不违反总体架构“L2 同时索引 L0 与 L1”的要求；它只是把两个来源在同一个 L2 系统内分池管理。

### 3.5 排序应分两次，而不是只做一次统一 rerank

推荐：

```text
第一次排序：Anchor relevance
    判断哪个语义区域值得展开

第二次排序：Expanded evidence utility
    判断哪些原始证据真正进入 ContextPackage
```

第二次排序不一定再调用大型 reranker，可以采用：

- claim-level SourceRef 是否直接支持 Need；
- 当前支持状态和 Truth Class；
- Story Time / Narrative Position 距离；
- 是否包含冲突证据；
- 与已选证据的重复度；
- 每 token 的新增信息量；
- 是否是 mandatory 证据。

只做一次 Anchor rerank 然后把所有对应原文展开，会在一个高层 Anchor 关联很多 Span 时再次造成上下文膨胀。

### 3.6 预算应按“候选预算、展开预算、上下文预算”分开

不要只维护一个最终 `token_budget`。建议 QueryContract 或 ContextAssemblyPlan 进一步表达：

```text
retrieval_candidate_budget
rerank_pair_budget
evidence_expansion_token_budget
context_relevance_token_budget
mandatory_constraint_tokens  # 不参与淘汰，但必须报告
max_anchor_expansions
max_scene_expansions
max_full_chapter_reads
max_retrieval_rounds
```

这样才能明确省下的到底是搜索延迟、reranker token、L0 读取量，还是 Writer 的最终上下文 token。

---

## 4. 三份文档的一致性问题与建议表

| 优先级 | 问题 | 当前影响 | 建议 | 验收方式 |
|---|---|---|---|---|
| **P0** | 技术设计写了“优先命中 L1”，但主流程仍将 BM25、Dense、Hierarchy 平铺并行 | 开发者可能实现为多路全库 top-k，再一次性 rerank | 增加“Query Intent → Anchor/L0 候选空间 → Evidence Expansion”的正式状态机 | 用 exact、semantic、quote、style、global 五类 case 验证路由不同 |
| **P0** | Stage 1B 连续 Commit Replay 需要逐章更新 L1/OpenSearch，但 Derived Snapshot、Outbox、Readiness 被技术设计放到后续 Phase 3 | 第 N 章提交后，第 N+1 章可能读取旧 Anchor、旧向量或旧 BM25 | 将 `Derived Snapshot Lite + Outbox + Freshness Gate` 前移至 Stage 1B | 故障注入索引延迟；不得静默读取旧 snapshot |
| **P0** | Stage 1 不启用 R2 Controller，却要求生成 MemoryNeed、Hybrid Retrieval、Evidence Expansion 和 Sufficiency | 检索策略的执行所有者不明确，可能偷偷形成一个未命名 Agent | 定义 Stage 1 的 `Deterministic Retrieval Orchestrator / Rule-based Query Planner`；只执行固定路由，不做开放式多轮 Agentic 决策 | 所有分支由配置和测试可枚举；无隐藏 LLM tool loop |
| **P0** | 未来正文虽声明冻结后才揭示，但未规定物理隔离和模型记忆污染控制 | 公开小说可能已存在于模型预训练数据；实验会把模型记忆误当系统检索能力 | evaluator-only store/credential；未来文本不进任何索引；优先用未公开授权稿或做模型污染对照 | Future Leakage 自动扫描 + 污染对照组 + 访问日志 |
| **P0** | 总体架构交接要求声明 Capability Profile 和 ADR 验证计划，技术/执行文档只有分阶段文字 | 开发时容易把 proposed 能力误做成永久接口，或漏掉已启用条件不变量 | 每阶段增加 `Implementation Conformance Manifest` | 列出 enabled/deferred profile、ADR hypothesis、fallback、required schema |
| **P1** | 技术设计 Phase 1/2 与执行规划 Stage 1 的顺序不同 | 两份蓝图会给 Issue 排期和依赖不同答案 | 明确执行规划覆盖技术设计第 22 节的阶段顺序，并在技术文档发 v0.2 同步 | 只有一份当前阶段映射表 |
| **P1** | Stage 0 的 Atomic Test Commit 未明确即便 Root 为空也要提交五 Root Manifest | 早期可能实现成只提交 Text/World 的简化事务，之后再迁移核心语义 | Stage 0 从第一天就提交五 Root refs；空 Root 使用合法 empty manifest | Commit contract test 验证五 Root 齐全 |
| **P1** | Effect Journal 在技术设计中重要，但执行 Stage 0 工作包只笼统记录 effect 事件 | “外部成功、本地未记 completed”的关键崩溃窗口可能未测 | F0-06/F0-08 增加 EffectReceipt 与 uncertain recovery case | 模拟 API 成功后进程崩溃，恢复不得盲重试 |
| **P1** | OpenSearch 中 L0 与 L1 候选如何隔离没有落实 | 长正文 chunk、章节摘要、原子事实会在同一分数空间互相挤压 | 两个逻辑 alias/候选池，或强类型过滤和独立配额 | 输出每种 unit type 的候选数、命中率、淘汰原因 |
| **P1** | Exact/Temporal、Graph Exact Edge 与 Graph multi-hop 的 R1/R2 边界未完全写清 | 确定性事实可能误入 RRF，或开放多跳被当作 Fast Path | R1 只允许注册模板、有限结果、无冲突的查询；多跳/冲突/未知进入慢路径 | Fast-path eligibility contract tests |
| **P1** | RRF 的“融合所有通道”没有唯一所有者 | OpenSearch 可能先融合 BM25+Dense，应用层再融合 Graph/Hierarchy，形成不可解释的双重融合 | Stage 1 先以应用层 RRF 作为可解释基线，各通道返回独立 rank；原生 Hybrid 作为等价优化候选 | 两种实现结果/指标对照，RunEvent 保存通道 rank |
| **P1** | Context Compiler 的预算只有总 token 概念较强，展开阶段预算较弱 | Anchor 已缩小候选，但 L0 Expansion 仍可能爆炸 | 增加 ExpansionBudget、最大 Scene/Chapter 展开次数 | 记录 Anchor→L0 转换率和读取 token |
| **P1** | Stage 1A 的消融没有单独测 Anchor-first | 无法回答“Anchor 先行是否比直接 L0 搜索更高效” | 增加 A0～A6 消融，见第 6 节 | 同召回下比较延迟、L0 读取量和 Context Utility |
| **P1** | Gold 的三类定义合理，但标注可靠性流程不足 | Operational Constraint 很容易标漏，100% Coverage 门禁可能建立在不完整 Gold 上 | 双人标注、分歧裁决、IAA、Gold debt 和版本修订协议 | 报告一致性、争议率、修订影响 |
| **P1** | `Mandatory Constraint Coverage = 100%` 可通过塞入大量内容取巧 | 高覆盖但上下文失控也可能过门禁 | 与 mandatory token 上限、重复率、错误约束率绑定 | Coverage 与成本/精度联合门禁 |
| **P2** | Stage 0 同时要求 PostgreSQL、OpenSearch、MinIO、OTel、LangGraph 全量真实集成 | 工程底座可能拖延核心实验 | 保留真实集成门禁，但将目标限定为最薄 smoke path，并设置时间盒 | 无算法优化、无生产化封装；只证明契约和恢复 |
| **P2** | 事件命名在总体架构、技术设计和执行规划中存在 `model_call_requested` / `model.requested` 等差异 | Schema、日志查询和回放工具会产生早期迁移 | 在 Stage 0 冻结一份带版本的 canonical event taxonomy | Schema contract + upcaster test |
| **P2** | Stage 1 最小 Epistemic 能力后置，但 ContextPackage 包含 truth_and_knowledge_boundaries | 容易对外声称已有完整认知隔离 | 明确 Stage 1 只支持粗粒度 `author_only / writer_safe / assertion_not_fact` | Profile/报告中标为 degraded capability |

---

## 5. 具体技术选型意见

### 5.1 OpenSearch 可以保留，但要冻结“能力要求”，不只冻结产品名

技术设计把 OpenSearch 同时用于 BM25、Dense、Hybrid 和 RRF，方向可行，但应在部署契约中注明最低能力：

- OpenSearch 原生 `score-ranker-processor` 的 RRF 是从 2.19 引入的；如果实际镜像不具备该能力，必须使用应用层 RRF，而不是运行时临时换算法；
- 当前 OpenSearch Hybrid Query 支持顶层 `filter` 做 pre-filter，Commit、worldline、time、scope 等关键过滤应尽量在召回前执行；最终权限仍由 Retrieval Service / Context Compiler 二次验证；
- 搜索 pipeline、mapping、analyzer、rank constant、weights、candidate depth 都必须进入 `index build profile` 和实验 Manifest；
- 不能只固定“OpenSearch latest”，需要锁镜像 digest，并用 capability test 验证过滤、RRF、pagination depth 和 rerank 行为。

官方依据：

- [OpenSearch Hybrid Search 与 pre-filter](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/index/)
- [OpenSearch RRF score ranker](https://docs.opensearch.org/latest/search-plugins/search-pipelines/score-ranker-processor/)
- [Hybrid Query 的处理顺序与限制](https://docs.opensearch.org/latest/query-dsl/compound/hybrid)

### 5.2 Stage 1 建议优先用应用层 RRF 做实验基线

理由不是否定 OpenSearch 原生 RRF，而是 Stage 1 需要可诊断性：

- 分别保存 BM25、Dense、Hierarchy、Graph 的原始 rank；
- 能做通道消融、候选配额和失败归因；
- 避免 OpenSearch 已融合 BM25+Dense 后，再和应用层 Graph/Hierarchy 做第二次融合；
- 可以通过一次 `_msearch` 批量请求减少往返。

当结果和行为稳定后，再将“BM25 + Dense 的应用层 RRF”替换为 OpenSearch 原生 Hybrid/RRF，并做结果、延迟和可解释性对照。原生实现属于性能 Adapter，不应改变领域检索契约。

### 5.3 中文 analyzer 必须单独做小说语料专项实验

技术设计中的 `content.standard_cn` 尚未定义。OpenSearch 内置 `cjk` analyzer 使用重叠二元切分，官方也明确建议对具体文本比较 ICU analyzer。小说语料还有大量虚构姓名、称号、功法、地名和拆字误导，普通中文分词 benchmark 不足以决定 mapping。

建议最少比较：

```text
keyword exact alias
normalized alias keyword
CJK bigram field
ICU / 项目选定中文 analyzer field
phrase field
character/name dictionary boost
```

别名和专有名必须保留 exact keyword 通道；不得只靠模糊匹配或分词字段。自定义词典、normalizer 和 analyzer 版本进入 Derived Snapshot build profile。

官方依据：[OpenSearch CJK analyzer](https://docs.opensearch.org/latest/analyzers/language-analyzers/cjk/)。

### 5.4 BGE-M3 适合作为候选 baseline，但不要把“支持长输入”理解成“应该输入整章”

BGE-M3 官方模型卡标明支持 dense、sparse、multi-vector，并支持最长 8192 token 的多粒度输入。这只说明模型能力上限，不代表长章 embedding 是最优粒度。当前设计坚持 Scene / Chapter / Fact 分别嵌入是合理的。

建议固定实验变量：

```text
anchor_type
input max tokens
head/tail/semantic truncation strategy
pooling and normalization
embedding batch profile
query instruction/template
```

同一 Snapshot 不得混用不同 profile。第一轮重点比较 Fact/Event/Scene Anchor；Chapter/Arc 向量只作上层路由，不能直接取代 Scene/Fact 证据。

官方依据：[BAAI BGE-M3 model card](https://huggingface.co/BAAI/bge-m3)。

### 5.5 Reranker 的真实输入长度和截断必须进入 benchmark

BGE-reranker-v2-m3 可以保留为候选 baseline，但其官方 Transformers 示例显式设置 `truncation=True, max_length=512`。无论模型理论上可支持多长输入，实际部署的 tokenizer、batch、显存和服务配置都会决定有效窗口。

因此：

- 第一阶段 rerank 的主要对象应是紧凑 Anchor，不是整章；
- 对 Scene/Chapter 候选必须定义截断/摘要策略；
- 记录 query token、passage token、被截断比例和证据是否落在截断区；
- 将“reranker 截断导致关键证据丢失”设为独立失败类别。

官方依据：[BAAI bge-reranker-v2-m3 model card](https://huggingface.co/BAAI/bge-reranker-v2-m3)。

### 5.6 PostgreSQL Exact / Temporal 路径应继续优先于搜索引擎

当前选型正确。需要补的是明确的 R1 Contract：

```text
registered query template
bounded result cardinality
commit/worldline/story_time/access complete
no known conflict
fresh canonical basis
deterministic truth semantics
evidence/provenance retained
```

只有全部满足才允许 Fast Path。R1 miss 仍然不等于不存在；没有 completeness certificate 时返回 unknown/unmodeled，而不是 false。

### 5.7 Graph 的两类用法要分开

```text
Canonical/Exact Edge Lookup
    例如当前持有关系；可属于 R1

Derived Multi-hop Discovery
    例如三跳因果、信息传播和伏笔链；属于 L2/R2 候选召回
```

前者不应进入 RRF；后者可以返回 Anchor refs 和 path explanation，再参与候选融合。这样可以避免把确定性当前状态和近似图发现混成一个分数。

---

## 6. 建议增加的 Anchor-first Benchmark

当前 B0～K4 消融建议保留，并补充以下组：

```text
A0  L0 BM25 direct
A1  L1 Anchor BM25 only，不展开 L0
A2  L1 Anchor BM25 → L0 Evidence Expansion
A3  L1 Anchor BM25 + Dense RRF → L0 Expansion
A4  Anchor Hybrid → Rerank → L0 Expansion
A5  A4 + L0 fallback / Graph or Hierarchy targeted supplement
A6  Anchor 与 L0 并行全量召回  # 作为成本较高的上界对照
```

每个方案至少报告：

```text
Anchor Recall@k
Anchor Precision@k
Anchor-to-Gold-Evidence Conversion Rate
Evidence Recall after Expansion
Expansion Precision
平均展开 Anchor 数
平均读取 L0 Span / Block / Scene / Chapter 数
Full Chapter Read Rate
Retrieval Candidate Count
Reranker Pair Tokens
L0 Evidence Tokens Read
Final Context Tokens
P50 / P95 latency
Additional Search Rate
Context Utility per 1K Tokens
Mandatory Gap Closure
```

应按 Query Intent 分层报告，不能只给总体平均：

```text
exact_current_state
entity_alias
exact_quote
semantic_history
temporal_order
causal_multi_hop
foreshadowing
style_voice
plan_obligation
```

预期结果不是要求 Anchor-first 在所有类型上获胜，而是证明路由策略能够：

- 在 semantic/history/global 查询上减少 L0 阅读量；
- 在 exact/quote 查询上正确绕过 Anchor；
- 在 style 查询上选择少量连续原文；
- 在高风险查询上完成 Anchor 导航后强制回到 L0/Canonical 证据；
- 在同等关键覆盖率下提高 Context Utility per Token。

---

## 7. 对执行规划的具体补充

### 7.1 Stage 0：保持“薄”，但核心语义不能是假实现

建议 Stage 0 补充四个明确验收点：

1. **五 Root 空 Manifest 也必须真实提交**：不得先做三 Root 或单 Text Commit；
2. **Effect crash-window 测试**：外部模型调用成功、本地 completed 事件未落库时，恢复能查询、补记或标记 uncertain；
3. **Event taxonomy v1**：统一事件名、必填字段、ArtifactRef 和 upcaster；
4. **Implementation Conformance Manifest**：声明 Baseline Profile、Stage 0 启用的 Autonomous 子集、明确 deferred 能力。

Stage 0 的 OpenSearch、MinIO、OTel 集成只需要 smoke path，不做 relevance tuning、复杂 dashboard 或生产运维封装。

### 7.2 Stage 1A：建议拆成四个内部 Gate

```text
Gate 1A-O1  Oracle Query + Verified Memory + Direct Retrieval
    先证明索引和证据链能工作

Gate 1A-O2  Oracle Query + Verified Memory + Anchor Cascade
    单独判断 Anchor-first 的召回和效率

Gate 1A-E1  Generated MemoryNeed + Verified Memory
    隔离“有没有想到要找”

Gate 1A-E2  Generated MemoryNeed + System-built Memory
    测完整读侧误差
```

只有 O2 通过后才讨论 reranker、Sparse、Graph 等优化；只有 E2 通过才说明端到端 Memory Construction 足以支持下一阶段。

### 7.3 Stage 1A：未来正文隔离需要落实到权限和基础设施

建议：

- `future_text_root_private` 使用 evaluator-only 存储位置和独立 credential；
- 导入 future text 时禁止触发 L1、embedding、BM25、cache 和全文 analyzer；
- Retrieval Service 运行身份无权读取 future store；
- Context 冻结后记录 hash，再由 evaluator 读取未来正文；
- 每个 case 自动扫描所有 returned EvidenceRef 是否越过 history boundary；
- Prompt、Trace、人工标注 UI 的可见范围也纳入泄漏审计；
- 对公开小说增加“模型预训练记忆污染”说明，最好加入未公开授权文本或别名/设定扰动对照。

### 7.4 Stage 1A：Gold 与统计门禁

建议对 Fine-grained Set 使用：

```text
annotator A
annotator B
adjudication
annotation guideline version
disagreement reason
gold confidence / debt
```

门禁不能只看单点平均：

- 按案例宏平均，防止长章支配总体；
- 报告 bootstrap confidence interval；
- 对 B0/B1/Naive RAG 使用配对比较；
- Mandatory Coverage 与错误约束率、token 成本联合报告；
- Pilot 阈值只用于校准，不应用一个 20→3 case 决定技术晋升。

### 7.5 Stage 1B：必须增加最小 Derived 传播闭环

建议在 M1-12 与 M1-13 之间增加：

```text
M1-12A  Derived Snapshot Lite / Outbox / Freshness
    Commit 同事务写 projection_outbox
    构建新 L1 Anchor
    增量更新 Anchor/L0 search docs
    更新 embedding
    创建 source_commit 精确匹配的 snapshot
    原子切换 alias 或显式发布 snapshot id
    计算当前 replay scope readiness
```

第 N+1 章开始前必须满足：

```text
Canonical Commit = C_N
R1 current-state view basis = C_N
Retrieval snapshot source_commit = C_N，或被显式标记 degraded
ContextManifest 记录实际 snapshot
```

如果 OpenSearch 更新失败：

- Canonical Commit 不回滚；
- 下一章 semantic retrieval 不得静默使用 C_(N-1)；
- 可以降级到 R1/Canonical/L0 direct，或阻断该 scope；
- RunEventLog 和 Evaluation Ledger 必须记录 degradation。

这是总体架构与执行规划之间目前最需要补齐的一处。

### 7.6 Stage 1B：连续回放的错误传播要分两条曲线

建议同时运行：

```text
Pure E2E Replay
    错误不人工修复，测自然污染传播

Audited Replay
    每个门禁点按协议修复，测真实运营成本和可恢复性
```

分别报告首次污染章节、污染传播深度、人工修复 Commit 数和每章修复时间。只运行“发现错误后立即人工修复”的 replay 会低估系统漂移；完全不修又不能代表实际生产路径。

---

## 8. 建议新增的最小数据契约

### 8.1 RetrievalUnit

```yaml
retrieval_unit:
  unit_id: ...
  unit_kind: anchor | grounded_chunk
  semantic_kind: fact | event | state | scene | chapter | arc | style | quote
  source_commit: ...
  snapshot_id: ...
  source_refs: [...]
  story_time_coverage: ...
  narrative_range: ...
  worldline: ...
  truth_class: ...
  support_status: ...
  access_scope: ...
  information_label: ...
  build_profile: ...
```

`unit_kind` 是 L2 搜索文档字段，不是给 Canonical Record 重新增加已经废止的 `representation.level`。

### 8.2 RankedCandidate

```yaml
ranked_candidate:
  unit_ref: ...
  channel: bm25 | dense | graph | hierarchy | exact
  channel_rank: ...
  channel_score: ...
  fused_rank: ...
  matched_terms_or_path: ...
  filter_receipt: ...
  stale: false
  exclusion_reason: null
```

### 8.3 EvidenceExpansionReceipt

```yaml
evidence_expansion:
  anchor_ref: ...
  expanded_source_refs: [...]
  expansion_level: span | block | window | scene | chapter
  reason: support | conflict | style | missing_context
  tokens_read: ...
  support_result: supported | conflicting | insufficient
  retained_in_context: true
```

### 8.4 RetrievalStageReport

```yaml
retrieval_stage_report:
  query_intent: ...
  route: ...
  r0_hit: ...
  r1_hit: ...
  anchor_candidates: ...
  reranked_anchors: ...
  expanded_anchors: ...
  l0_tokens_read: ...
  full_chapter_reads: ...
  fallback_used: ...
  stop_reason: ...
  unresolved_gaps: [...]
```

这些对象首先用于 Trace / Evaluation；不需要全部进入 RunEventLog。RunEventLog 只保留恢复和审计所需的摘要与 ArtifactRef。

---

## 9. 建议的文档修订顺序

本轮讨论确认后，再修改原文，建议顺序如下：

1. 技术设计第 12 节：增加 Query Intent 路由、Anchor/L0 双候选池、两阶段排序和 Evidence Expansion 预算；
2. 技术设计第 22 节：同步执行规划的新 Stage 顺序；
3. 执行规划 Stage 1A：增加 Anchor-first 消融、分阶段指标和未来正文物理隔离；
4. 执行规划 Stage 1B：前移 Derived Snapshot Lite、Outbox 和 Freshness Gate；
5. Stage 0：补五 Root empty manifest、Effect crash-window、事件分类和 Conformance Manifest；
6. 最后将确认的机制写为一份 Retrieval ADR，例如：

```text
TADR-23  Semantic narrative retrieval uses routed anchor-first cascade
Status   proposed / experimental
Scope    semantic/history/global queries
Bypass   R0/R1 exact, exact quote, style raw-span retrieval
Fallback direct L0 lexical/dense on anchor insufficiency
Gate     held-out benchmark improves evidence-per-token without reducing mandatory coverage
```

---

## 10. 建议优先讨论的五个决定

为了避免一次讨论过多细节，建议先确认以下五点：

1. 是否接受“按 Query Intent 路由的 Anchor-first”，而不是所有查询强制 Anchor-first；
2. Stage 1 是否采用 Anchor Index 与 Grounded Index 两个逻辑候选池；
3. Stage 1 的 RRF 是否先由应用层统一实现，以保留每通道可诊断数据；
4. 是否把 Derived Snapshot Lite + Outbox + Freshness Gate 前移到 Stage 1B；
5. 首轮 Pilot 的真实 Benchmark 由用户后续构造；当前系统只负责完成通用数据包接入、运行和评测能力，模型预训练记忆污染不纳入当前门禁。

这五点确认后，Stage 0/1 的开发任务才能稳定拆解；其余阈值、top-k、模型和 analyzer 都可以在 Pilot 中通过数据决定。
