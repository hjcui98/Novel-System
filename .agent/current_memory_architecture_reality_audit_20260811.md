# Stage 2M 记忆架构实现现状审计：分层存储、KG/Graph、Need/Planner、检索与 Writing Package

- 日期：2026-08-11
- 作者：Codex
- 状态：现状审计报告；不是 OpenCode 交接计划；不是局部修复验收
- 范围：当前 `src/novel_agent/` 生产代码、上位文档、五个 frozen checkpoint 产物
- 主要产物来源：
  - `/tmp/ns-stage2m-frozen-checkpoint-repair-project-20260811-v1/`
  - `/tmp/ns-stage2m-frozen-checkpoint-repair-eval-20260811-v1/`
  - `/tmp/ns-stage2m-frozen-checkpoint-evaluator-rescore-20260811-v3/`

## 0. 结论先行

当前实现没有真正完成用户设计中的 Stage 2M Memory Agent 产品闭环。

它已经实现了一个较重的分层存储与检索骨架：

```text
TextRoot / EvidenceRef / CAS
  -> WorldRoot(entity/event/state/relation/obligation)
  -> R1 Postgres rows
  -> L2 anchor / grounded OpenSearch index + BM25 / dense / hierarchy / typed_graph routes
  -> EvidenceRef 展开回 L0 raw text span
```

但现有 frozen 产物显示：

1. **KG/Graph 没有真正建起来。**
   - 71 个 `world-root` artifact 里的 `relations` 全部为 0。
   - C20/C40/C60/C80/C95 的 Planner `world_summary.relation_count=0`，`key_relations=[]`。
   - 五个 checkpoint 的 `retrieval_attestation.graph_edge_count=0`，`typed_graph` coverage 全部是
     `expected_units=0 / ready_units=0`。
   - 代码里有 `RelationRecord`、R1 relation row、typed graph traversal；但实际 World 没有关系边，
     所以 Graph channel 在这批产物里是空壳。

2. **分层存储骨架是存在的，但实际内容主要是 state + 少量 event + raw text，不是成熟 KG。**
   - 最大 WorldRoot 大约是 `28 entities / 124 states / 0 relations / 1 event / 3 obligations`。
   - 一些关系事实被编码成 `StateRecord` 文本值，例如 `落衡 teacher "陈长生"`、
     `陈长生 teaches "luo_luo"`、`唐三十六 enrollment_status "加入国教学院"`。
   - 这些 relation-like states 不会进入 `typed_graph`，也不能作为结构化 subject→predicate→object
     边参与 alias/coreference/entity grounding。

3. **Need/Planner 不是唯一问题，但它吃到的 World 投影有硬伤。**
   - Planner 本身能生成一些合理的目标条件问题，例如 P004 `d81_01` 问
     “陈长生对落落的具体教学内容是什么”。
   - 但它的输入 `key_relations` 为空，C60/C80/C95 state 被固定截断 17/44/60 条。
   - Grounder 对 `落落` 被 `落落` canonical entity 与 `落衡.aliases=["落落","殿下"]` 撞名卡成
     `ambiguous_label_match`；最终最大 World 中也没有 `国教学院` 机构实体。
   - 因此 Need 质量和 entity binding 受 World/KG 构建失败拖累。

4. **检索不是完全没工作，L2→raw 的一部分已经通了。**
   - 五个 checkpoint 的 R1、anchor、grounded index 均有 ready units；embedding/reranker attestation
     没降级。
   - Evidence ledger 中每个 checkpoint 都保留了上百条 evidence entry，其中 raw/no-support entries
     约 40-46 条。
   - v3 rescore 显示，在排除 plan-axis-only 后的 47 条 observed/claim 口径中，有 33 条能绑定到
     legacy Ledger 原文 evidence。
   - 所以“正文 raw material 完全检索不到”不是当前最准确的判断。

5. **真正偏离用户产品意图的是最后产品边界：代码仍是 claim-first，不是 evidence-first writing package。**
   - `WriterContextItem` 仍强制 `claim: str`。
   - `EvidenceLedgerEntry` 仍叫 `claim_excerpt`。
   - `WriterContextAssembler.assemble_from_spec()` 的输入仍是
     `ClaimSupportGroup / ClaimVariant / ClaimSupportReceipt`。
   - `raw_evidence_ledger_entries` 的注释明确说它们 “never become Writer items”。
   - `stage2_paired_pilot.py` 默认仍构造 `TrustedClaimSupportProducer`，跑 Claim Support，再喂给
     assembler。
   - 虽然某些对象已标 `contract_version="writer_context.v2"`，但公共 fingerprint 仍写
     `writer_context_profile="writer_context.v1"`，且 Writer-facing rendered context 仍是 claim 列表。

一句话：

> 当前系统建了很多存储、检索、验证、评分、receipt 的机制，但没有真正按用户意图把“L2 检索命中 →
> 回到 L0 raw → 按 Need/facet 输出 Writer 可读 evidence package”作为默认产品闭合。Graph/KG
> 层实际为空，最终产品又被 Claim Support / Verifier / Evaluator 截走了。

## 1. 用户设计意图的可执行解释

根据上位文档与用户本轮澄清，当前 Stage 2M Memory Agent 的目标不是替 Writer 生成标准答案式 Claim，
也不是内置 Gold evaluator。正确产品边界应是：

```text
public Task / Author Plan / visible history
  -> Planner / Need
  -> L2 projection retrieval
       - BM25
       - dense
       - graph / typed relation
       - hierarchy / temporal / exact
  -> 展开回 L0 raw text span
  -> 按 Need/facet/scope 组织成 Writing Package
  -> EvidenceLedger 保存完整、可解引、cutoff-safe raw evidence
  -> Writer / 外部强模型 / 人工再做语义理解和评分
```

这个目标在 ADR-0008 中已经被写成正式决策：

- 默认产品是 evidence-first `WriterContextPackage + EvidenceLedger`。
- Claim proposal、multi-slice synthesis、whole-claim verifier、semantic receipt、逐 Gold semantic
  evaluator 不再是默认 Agent 路径、package READY 条件或产品 Gate。
- Agent 自身只负责机械可验证不变量：权限/cutoff/leakage、精确 provenance、引用可解引用、预算、
  typed gap、manifest/reproducibility、无 source mutation。

## 2. 与成熟 GraphRAG/KG-RAG 的对比

这里使用 Microsoft GraphRAG 官方文档作为成熟 GraphRAG 的外部基线：

- GraphRAG indexing 标准 pipeline 会从 raw text 抽取 `entities, relationships and claims`，做
  community detection、community summaries/reports，并写 embedding。
  参考：[GraphRAG Indexing overview](https://microsoft.github.io/graphrag/index/overview/)。
- GraphRAG architecture 的核心流程是 `ChunkDocuments -> ExtractGraph -> ExtractClaims ->
  DetectCommunities -> GenerateReports -> Embed...`。
  参考：[GraphRAG Architecture](https://microsoft.github.io/graphrag/index/architecture/)。
- GraphRAG dataflow 的 knowledge model 包含 `Document / TextUnit / Entity / Relationship /
  Covariate / Community / Community Report`，其中 TextUnit 为原文 provenance，Phase 3 明确抽取
  entity、relationship、claim。
  参考：[GraphRAG Dataflow](https://microsoft.github.io/graphrag/index/default_dataflow/)。
- Query Local Search 会结合 AI-extracted knowledge graph 与 raw document text chunks。
  参考：[GraphRAG Query overview](https://microsoft.github.io/graphrag/query/overview/)。

对照当前 NS 实现：

| 维度 | 成熟 GraphRAG/KG-RAG 基线 | 当前 NS 代码/产物现实 |
|---|---|---|
| raw text chunk | 有 TextUnit/chunk 并保留 provenance | 有 TextRoot、TextBlock、EvidenceRef、CAS，可回 raw |
| entity extraction | 抽实体并做 linking/coref/type | 有 Entity schema；但 alias/coref 未闭合，`落落/落衡` 分裂 |
| relationship extraction | 抽实体间关系边 | 有 `RelationRecord` schema；实际 71 个 WorldRoot relations 全 0 |
| claim/covariate | 可选抽 claim | 当前反而把 Claim Support 做成默认产品路径，过重 |
| community/subgraph summary | 有社区/报告/图增强 | 当前没有 community 层；typed_graph 只走 R1 relation rows |
| hybrid retrieval | graph + text chunks + vector/BM25 | BM25/dense/grounded 有；graph 因 relation rows=0 空跑 |
| final user product | 可把 KG 与 raw chunks 给回答/应用 | 当前 Writer-facing product 仍是 claims；raw evidence 是 ledger/诊断副产物 |

所以准确说：

> NS 当前路线“像”GraphRAG：有 raw、World、R1、L2、hybrid route、provenance。
> 但实际不是成熟 KG-RAG：关键关系图没建起来，entity/coref 没闭合，final product 也没有按 raw evidence package 输出。

## 3. 代码事实：已有骨架与实际断点

### 3.1 World/KG domain model 是存在的

`src/novel_agent/domain/world.py` 定义了：

- `Entity`
- `Event`
- `StateRecord`
- `RelationRecord`
- `ObligationRecord`

其中 `RelationRecord` 是真正的二元关系边模型：

```python
class RelationRecord(DomainModel):
    relation_id: StableId
    predicate: str
    subject_id: StableId
    object_id: StableId
    valid_time: StoryTime
    evidence_refs: tuple[EvidenceRef, ...]
    truth_class: TruthClass
```

这说明代码层不是完全没有 KG 概念。

### 3.2 Curator 可以输出 relation，但不是系统性 KG 构建

`src/novel_agent/domain/changes.py` 有 `WorldRecordKind.RELATION` 与 `CuratorRelationRecord`。
`src/novel_agent/services/model_curation.py` 的 Curator prompt 允许
`entity / event / state / relation / obligation`。

但同一 prompt 又要求：

- “Return at most four durable operations”
- “Prefer one or two precise operations”
- 排除 transient encounters、temporary feelings、plans、unresolved possibilities

这是一种严控的 durable world delta extractor，不是成熟 GraphRAG 式的系统性 entity/relationship/claim
抽取流水线。它可以安全，但产物证明它没有建出关系图。

### 3.3 L1/L2 projection 有，但 graph 只依赖 RelationRecord

`src/novel_agent/services/memory_pipeline.py::AnchorBuilder` 会把：

- `world.states` 投成 `STATE_ANCHOR`
- `world.events` 投成 `EVENT_ANCHOR`
- `world.relations` 投成 `RELATION_ANCHOR`
- TextRoot block 投成 `GROUNDED_BLOCK`

`src/novel_agent/services/projection.py::FullDerivedProjectionBuilder` 会：

- materialize R1
- build/publish OpenSearch anchor + grounded indexes
- 写 `ProjectionAttestation`

`src/novel_agent/services/r1.py::typed_graph_paths()` 只查询：

```python
R1RecordRow.record_kind == WorldRecordKind.RELATION.value
```

也就是说，typed graph 不是从 state 文本值、BM25 hit 或 embedding 里推断边。它只遍历 canonical
`RelationRecord` rows。因此当 `world.relations=0` 时，typed graph 必然没有任何边。

### 3.4 Retrieval routes 已有，但 graph route 在产物中不可用

`src/novel_agent/services/retrieval.py` 中：

- `CAUSAL_MULTI_HOP -> TYPED_GRAPH`
- `RELATION_CHAIN -> TYPED_GRAPH`
- `SEMANTIC_HISTORY -> anchor_bm25 + anchor_dense + grounded fallback`
- `EXACT_QUOTE / RARE_PHRASE -> grounded_bm25`

这是合理的路由骨架。但五个 checkpoint 的 `typed_graph` ready units 全部为 0，因此关系类、多跳类 Need
实际无法从 graph 获益，只能退到 text/hybrid 或失败。

### 3.5 Need/Planner 当前仍带 claim-completion 语义

`Stage1MemoryNeed` 当前已经有 `semantic_question`、`query_hints`、Planner lineage 字段，这是进步。

但它也仍有：

- `expected_claim_scope`
- `NeedCompletionSpec.require_current_claim`
- `claim_may_cite_plan`
- `completion_criteria`
- `stop_condition`

`TaskPlanConditionedNeedGenerator` 仍使用类似：

```python
stop_condition="one current claim with a minimal legal evidence set"
completion_criteria="claim is supported by cutoff-valid evidence"
```

所以 Need 层虽然可服务 evidence-first，但当前语义仍被 claim closure 牵引。

### 3.6 WriterContext v2 名字出现了，但产品还是 claim-first

`src/novel_agent/domain/writer_context.py` 当前仍定义：

```python
class WriterContextItem(DomainModel):
    claim: str
    evidence_ledger_ids: tuple[StableId, ...]
    claim_variant_id: StableId | None
    support_group_id: StableId | None
```

`EvidenceLedgerEntry` 仍有：

```python
claim_excerpt: str
support_group_id: StableId | None
support_receipt_ref: ArtifactRef | None
```

`WriterContextAssembler.assemble_from_spec()` 的签名仍要求：

```python
support_groups: tuple[ClaimSupportGroup, ...]
claim_variants: tuple[ClaimVariant, ...]
support_receipts: tuple[ClaimSupportReceipt, ...]
```

并且注释明确写着：

```text
raw_evidence_ledger_entries are exact raw slices retained in the separate EvidenceLedger
under raw identity ... They never become Writer items
```

这与 ADR-0008 的 evidence-first 产品边界相反。

### 3.7 Stage2 paired pilot 默认仍调用 Claim Support

`src/novel_agent/services/stage2_paired_pilot.py` 中主路径仍构造：

```python
selector = ControllerSupportSelector(
    TrustedClaimSupportProducer(...)
)
selection_a = selector.select(...)
assembled_a = assembler.assemble_from_spec(... support_groups / claim_variants / receipts ...)
```

同文件的 public configuration fingerprint 仍包含：

- `claim_support_producer_version`
- `support_selection_policy_version`
- `writer_context_profile: "writer_context.v1"`

这说明当前代码没有真正把默认路径切成“检索/选择 raw evidence 后直接 package freeze”。

## 4. 产物事实：World/KG 是否构建成功

### 4.1 WorldRoot 总体

在 `/tmp/ns-stage2m-frozen-checkpoint-repair-project-20260811-v1/objects/sha256/` 中：

- CAS payload objects：6390
- metadata objects：6390
- payload bytes：约 296MB
- `world-root` artifacts：71
- `projection-receipt` artifacts：96
- `evaluator-writer-evidence-ledger` artifacts：11
- `planner-invocation` artifacts：10

这说明 artifact/CAS/receipt 分层机制确实存在。

但 71 个 `world-root` artifact 全部满足：

```text
relations = 0
```

最大 WorldRoot 的规模：

```text
entities=28
states=124
relations=0
events=1
obligations=3
```

### 4.2 五个 checkpoint 的 Planner world summary

| Case | Checkpoint | entities | states | relations | events | obligations | truncated_states | fallback |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| P001 | C20 | 10 | 29 | 0 | 0 | 2 | 0 | planner_fallback / insufficient_target_goal_coverage |
| P002 | C40 | 14 | 52 | 0 | 0 | 3 | 0 | planner_fallback / insufficient_target_goal_coverage |
| P003 | C60 | 23 | 81 | 0 | 1 | 3 | 17 | planner_fallback / all_drafts_rejected |
| P004 | C80 | 26 | 108 | 0 | 1 | 3 | 44 | planner |
| P005 | C95 | 28 | 124 | 0 | 1 | 3 | 60 | planner |

五个 `key_relations` 都是空数组。

这说明当前 World 给 Planner 的结构化关系入口为 0。P004/P005 虽然 Planner 正常，但它不是依赖
真正 KG 关系边成功，而是依赖 state 文本和目标计划生成问题。

### 4.3 五个 checkpoint 的 projection / retrieval attestation

| Case | R1 records | entity associations | anchor docs | grounded docs | graph edges | typed_graph coverage |
|---|---:|---:|---:|---:|---:|---|
| P001/C20 | 75 | 41 | 86 | 21 | 0 | 0/0 |
| P002/C40 | 103 | 69 | 130 | 41 | 0 | 0/0 |
| P003/C60 | 142 | 109 | 180 | 61 | 0 | 0/0 |
| P004/C80 | 172 | 139 | 227 | 81 | 0 | 0/0 |
| P005/C95 | 190 | 157 | 258 | 96 | 0 | 0/0 |

所有 available channels 中都列出了 `typed_graph`，但 coverage 是：

```text
expected_units=0
failed_units=0
ready_units=0
```

所以 runtime 不是 degraded；它是在准确报告 graph 没有可用 units。

## 5. 产物事实：raw evidence 是否存在，是否进入 Writer-facing package

### 5.1 Legacy EvidenceLedger 中确实有 raw evidence

五个 checkpoint 的 `writer_evidence_ledger_ref` 指向的 ledger：

| Case | ledger entries | support-linked entries | raw/no-support entries | rendered tokens |
|---|---:|---:|---:|---:|
| P001/C20 | 102 | 59 | 43 | 11988 |
| P002/C40 | 107 | 61 | 46 | 11975 |
| P003/C60 | 106 | 66 | 40 | 11999 |
| P004/C80 | 104 | 61 | 43 | 11997 |
| P005/C95 | 105 | 59 | 46 | 11953 |

P004 raw/no-support sample：

```text
ledger.raw-slice.slice.grounded.block.ZTJ-P005.53.0.2031...
preview:
那夜之后，很多人都查过国教学院，从教枢处方面知晓了陈长生的大概来历，
但依然没有人能够查到落落的身份...
need_ids: ["need.stage2m.planner.d81_01"]
evidence_refs: exact TextRoot span with chapter/block/object/root hashes
```

这说明 raw exact slice 被 resolver 解出、写进 ledger，并绑定 Need。

### 5.2 但 raw evidence 不是 Writer-facing item

CAS 中 `writer_context.v2` 样本对象显示：

```text
writer_context contract = writer_context.v2
ledger contract = evidence_ledger.v2
writer items = 36
ledger entries = 110
ledger support/raw = 45 / 65
```

第一个 Writer item：

```json
{
  "claim": "陈长生默然，想起婚约曝光对自己的影响，很多人不想让自己和徐有容成亲...",
  "evidence_ledger_ids": ["ledger.support-group...."],
  "support_group_id": "support-group....",
  "support_receipt_ref": ...
}
```

`rendered_context` 也是 claim 列表：

```text
[当前必须遵守]
- 陈长生默然，想起婚约曝光对自己的影响... [ledger.support-group...]
- 陈长生震撼无比。 [ledger.support-group...]
...
```

所以虽然 ledger 里有 raw entries，它们没有作为 “Need/facet -> raw evidence previews/refs” 的
Writer-facing package item 暴露。Writer 看到的还是模型/claim support 组织后的 claim。

## 6. Need/Planner 现状判断

### 6.1 Planner 有价值，但输入世界不够

P004 `d81_01` 是一个合理的 Planner 问题：

```text
semantic_question:
陈长生对落落的具体教学内容是什么？特别是针对妖族经脉或战斗技巧的改进点。

query_hints:
- 陈长生教了落落什么剑法或技巧？
- 落落从陈长生处学到了什么？
- 陈长生的教学对落落战斗力的具体影响。
```

这说明 Planner 并非完全胡写。它能基于 author plan 反向提出有效记忆问题。

### 6.2 Grounder/entity binding 暴露结构问题

同一个 P004 `d81_01` 的 entity grounding：

```text
陈长生 -> exact_label_match -> entity.bootstrap.chen-changsheng
落落   -> ambiguous_label_match -> None
```

原因是最大 WorldRoot 同时有：

```text
entity.luo-luo: label=落落, aliases=[]
entity.luo-heng: label=落衡, aliases=["落落", "殿下"]
```

而 `NeedDraftGrounder` 当前把 internal label 和 alias 合在一个 exact candidate 池里判断。由于
`world.relations=0`，它无法用 relation context 消歧。

P005 中 `国教学院` 也不是 canonical entity；最大 WorldRoot 中能看到 `天道院` 组织实体，但未看到
`国教学院` 组织实体。`国教学院` 只出现在 state value 里，例如：

```text
陈长生 identity "国教学院的新生"
陈长生 location "国教学院藏书馆"
故事世界 status "国教学院已经废了"
唐三十六 enrollment_status "加入国教学院"
```

这会导致机构级 Need 难以获得稳定 entity id / graph anchor。

### 6.3 Need 层仍被 claim closure 牵引

Need 当前虽有 `semantic_question`，但整体 completion contract 仍围绕 Claim：

```text
stop_condition: one current claim with a minimal legal evidence set
completion_criteria: claim is supported by cutoff-valid evidence
```

这不适合 evidence-first package。正确目标应该是：

```text
Need/facet is served by one or more cutoff-safe exact evidence slices, or a typed gap.
```

Memory Agent 不需要在这里先压成唯一 claim。

## 7. 检索现状判断

### 7.1 健康部分

检索基础设施不是全坏：

- R1 records 随 checkpoint 增长。
- anchor/grounded indexes 有文档，embedding/reranker attestation 正常。
- Grounded raw slices 能展开进 ledger。
- v3 rescore 的 conservative evidence-recovery 结果为：

| Case | observed_claim_count | ledger-bound rows |
|---|---:|---:|
| P001 | 8 | 4 |
| P002 | 9 | 8 |
| P003 | 9 | 2 |
| P004 | 10 | 8 |
| P005 | 11 | 11 |
| Total | 47 | 33 |

这说明很多 Gold 相关 raw evidence 已经被 legacy ledger 找到了。

### 7.2 失败部分

检索当前无法依赖 graph：

- relation facts 没有转成 `RelationRecord`。
- `typed_graph` 没有边。
- relation-like states 只是文本值，不能 graph traversal。

另外，Need/entity 绑定错误会使精确 R1/graph/filter 失效，剩下 BM25/dense 更容易找到“相关背景”
而不是“精确关系闭合材料”。

## 8. 为什么这次结果看起来比上次差

上次用户保留的四点结果是：

| Case | checkpoint | old gold_evidence_recall |
|---|---:|---:|
| P001 | C20 | 0.654 |
| P002 | C40 | 0.793 |
| P003 | C60 | 0.552 |
| P004 | C80 | 0.545 |

当前 formal/frozen 产物的 legacy claim-first evaluator 给出：

```text
weighted=0
mandatory=0
MISS/UNTRACEABLE 大量存在
```

这两个数字不能直接当同一指标比较：

1. 旧报告是 `gold_evidence_recall`，偏向“是否找到了证据”。
2. 当前 `mandatory/weighted=0` 混入了：
   - final WriterContext claim 是否表达 Gold 结论；
   - claim-to-ledger provenance 是否对齐；
   - semantic evaluator / matcher 版本；
   - plan-axis-only 排除策略。
3. v3 rescore 证明当前 raw evidence 不是 0，而是 33/47 observed rows 有 ledger-bound evidence。

但这不代表当前系统没问题。更准确的说法是：

- 旧分数较高说明 raw evidence 检索潜力存在。
- 当前 0 分说明 claim-first 产品/评分链严重不适合用户目标。
- KG/Graph 为空、entity/coref 不闭合、Need 被 claim-completion 牵引，这些问题仍会限制真正的
  evidence-first package 质量，尤其是 P003。

## 9. 当前过度工程点

当前代码和文档里的石山主要不是“验证太严格”一个点，而是职责边界被错误扩大：

1. Memory Agent 被要求先生成 canonical claim。
2. Claim 还要 multi-slice proposal。
3. Proposal 要 whole verifier。
4. Verifier output 还要 semantic receipt。
5. Benchmark 再逐 Gold semantic evaluator。
6. Evaluator 又需要 matcher/provenance/ancestry/scoring formula。

这些机制有些是安全/诊断需要，但它们不应该是当前生产产品路径。它们把真正需要交付给 Writer 的
raw evidence package 挤成了副产物。

最低充分工程在这里应该解释为：

> 保留现有 Root/CAS/R1/L2/EvidenceRef/预算/权限/manifest，不新建第二套平台；但必须把默认产品
> 收缩到 evidence-first package。Claim Support/Verifier/Evaluator 可以保留为 legacy diagnostic，
> 但不能阻塞 package READY，也不能作为 Memory Agent 的主路径。

## 10. 现状判定：四个问题逐项回答

### 10.1 “有没有把原始正文按记忆体系、KG、原始文档分层存起来？”

部分实现。

- 原始 TextRoot / block / span / EvidenceRef / CAS 是存在的。
- WorldRoot / R1 / L2 projection 是存在的。
- EvidenceRef 能回到 raw text span。
- 但是 KG 关系层没有建成；实际 World 是 state-heavy，不是 entity-relation graph。

### 10.2 “是不是远离成熟知识图谱构建流程？”

是。

当前实现有 KG schema 和 graph route，但实际不是成熟 KG-RAG：

- 没有稳定 entity linking/coreference。
- 没有关系边。
- 没有 community/subgraph summary。
- 没有把 relation-like state 正规化为 graph edge。
- `typed_graph` 只依赖空的 `RelationRecord` rows。

### 10.3 “有没有实现分层储存？”

框架实现了，内容质量不达标。

可以说：

```text
分层存储框架：基本有
分层内容闭环：不够
KG/Graph 层：实际失败
raw evidence 可解引：已有
Writer-facing evidence package：未实现成默认产品
```

### 10.4 “Need/Planner 有没有太大问题？检索措施呢？”

Need/Planner 有问题，但不是唯一首损：

- P004/P005 Planner 能提出合理问题。
- P001/P002/P003 fallback 显示目标覆盖或 draft acceptance 有问题。
- Planner summary relations 空、state 截断。
- Grounder entity binding 被 alias/coref 问题拖垮。
- Need completion contract 仍 claim-first。

检索措施：

- BM25/dense/grounded/R1 基础设施在跑。
- L2→L0 raw expansion 已经部分成功。
- graph retrieval 在当前 frozen World 中不可用。
- 当前真正缺的是：把 selected raw slices 直接 materialize 为 Writer-facing package，而不是先走 Claim Support。

## 11. 建议的推进顺序

这不是 OpenCode 交接计划，只是从现状出发的技术优先级。

### Priority 1：先纠正产品边界

立即停止把 Claim Support / Verifier / Evaluator 当默认产品路径。

默认路径应改成：

```text
Need
  -> Retrieval / Rerank / exact L0 expansion
  -> selected exact slices
  -> WriterContextPackage evidence items
  -> EvidenceLedger full refs
  -> Markdown/JSON readable export
```

验收看：

- package item 不再强制 `claim`。
- package item 暴露 `need_id / facet / purpose / evidence_ledger_ids / raw_preview / typed_gap`。
- raw/no-support ledger entries 可以成为 Writer-facing items。
- ClaimSupport model call count = 0。
- semantic evaluator model call count = 0。

### Priority 2：单独承认 KG/Graph 未建成，并开 World/KG 构建质量任务

当前不要再声称 GraphRAG 已经完成。需要单独给 World/Curator/KG 建最小质量闭环：

- 明确哪些 relation-like states 应成为 `RelationRecord`，例如 teacher/student、member-of、located-in、
  enrolled-in、protects、opposes、family/engagement。
- 对确定二元关系，Curator 应输出 subject/object entity id 的 `RelationRecord`，而不是 state string。
- `落落/落衡` alias/coref 必须统一或至少消歧策略明确。
- `国教学院` 这类组织实体必须 canonicalize。
- 每个 checkpoint 至少对 graph edges 给出非零建设目标和人工抽样验证。

这属于 World/KG 构建质量，不应该混进 evaluator 修补里。

### Priority 3：再修 Planner view

在 World/KG 改善前，不要指望 Planner 靠空 `key_relations` 做关系推理。

短期可做：

- target-aware state selection，避免 C60+ 固定 64 条截断丢目标实体状态；
- exact internal label 优先于其他实体 alias；
- 未绑定的 public lexical anchor 保留自然语言 BM25/dense query，不丢 Need。

长期应在 `World.relations` 有边后，让 Planner summary 真实包含 key_relations。

### Priority 4：重新定义 bench 观察指标

不要再用内置 claim/evaluator 分数作为 Memory Agent 通过条件。

建议分两层：

1. Agent 内部机械指标：
   - leakage=0
   - evidence refs 可解引
   - package refs 全在 ledger
   - 每个 exposed evidence 有 Need/facet owner
   - graph edge count / relation coverage / entity grounding coverage
   - budget/cutoff/profile manifest
2. Agent 外部质量指标：
   - 用户人工或强模型读取 `writer_context_package.md + evidence_ledger.json + Gold` 后打分。
   - 该评分不得反馈同一实验身份。

## 12. 最终判断

当前代码没有实现用户真正要的 Agent 产品。

准确拆分是：

- **存储/索引骨架：有。**
- **L2→L0 raw 展开：部分通。**
- **真实 KG/Graph 构建：没有建成功。**
- **Need/Planner：有可用部分，但被空关系、实体别名、state 截断和 claim contract 拖累。**
- **Writing Package：名字上开始写 v2，实际上仍是 claim-first；raw evidence 仍是 ledger/诊断副产物。**
- **过度工程焦点：Claim Support、verifier、semantic evaluator、matcher/provenance/scoring 被推成主战场，
  偏离了“给 Writer 原始材料包”的核心目标。**

下一步如果要真正推进，不应该继续修 evaluator 或 claim verifier，而应先把默认产品路径收缩为
evidence-first Writing Package；同时单独承认并修复 World/KG 构建失败，尤其是 `relations=0`、
alias/coreference、organization entity、relation-like states 未结构化的问题。

## 13. 追加核验：不是只有“落落/国教学院”两个例子，最终 Entity 层整体也很薄

用户追问是成立的：不能用 P004 `陈长生/落落` 这个局部例子替整个 World/KG 背书。追加抽查最大
WorldRoot：

```text
artifact:
/tmp/ns-stage2m-frozen-checkpoint-repair-project-20260811-v1/objects/sha256/44/44946e62af32d5bb207e2abff907dafe0df04f521c69ed5561ad40ee88a06794

counts:
entities=28
states=124
relations=0
events=1
obligations=3

entity_type_counts:
character=24
organization=1
creature=2
setting=1
```

### 13.1 全量 entity 列表显示覆盖很窄

最终 World 的 entity roster：

| entity | type | aliases | states |
|---|---|---|---:|
| 陈长生 | character | — | 45 |
| 故事世界 | setting | — | 12 |
| 落衡 | character | 落落、殿下 | 11 |
| 唐三十六 | character | 青衣少年三十六、青云榜三十六名 | 8 |
| 徐有容 | character | 天凤真女 | 8 |
| 黑色巨龙 | creature | — | 5 |
| 关飞白 | character | 青云榜第四 | 5 |
| 徐世绩 | character | 御东神将 | 3 |
| 落落 | character | — | 3 |
| 摩河 | character | — | 3 |
| 庄换羽 | character | 青云榜第十、天道院的大师兄 | 3 |
| 费典 | character | 瘦高老人 | 3 |
| 薛醒川 | character | 大周御天神将、薛醒州神将 | 2 |
| 轩辕破 | character | — | 2 |
| 茅秋雨 | character | 两袖清风茅秋雨 | 2 |
| 陈留王 | character | — | 2 |
| 莫雨 | character | 莫大姑娘 | 2 |
| 黑羊 | creature | — | 1 |
| 天海牙儿 | character | — | 1 |
| 苟寒食 | character | — | 1 |
| 金玉律 | character | 妖族四大神将之首、金科玉律 | 1 |
| 七间 | character | 神国七律 | 1 |
| 天道院 | organization | — | 0 |
| 中年妇人 | character | — | 0 |
| 天海胜雪 | character | — | 0 |
| 平国公主 | character | — | 0 |
| 徐夫人 | character | — | 0 |
| 霜儿 | character | — | 0 |

这说明当前并没有构建出一个覆盖 95 章人物、组织、地点、派系、关系的稳定实体宇宙。它更像是：

- 主角陈长生的状态轨迹；
- 少量高频人物；
- 一个泛化 `故事世界` bucket；
- 极少数组织/地点；
- 大量关系事实塞在 state value 字符串里；
- 没有 relation edge。

### 13.2 缺失与错位不是个例

已经明确看到：

- `国教学院` 没有 canonical entity，却是这段剧情核心机构；
- 最终 organization 只有 `天道院` 一个；
- `天海家`、`离山`、`南溪斋`、`神将府`、`教枢处`、`未央宫`、`离宫` 等重要组织/地点不是稳定
  canonical entity；
- 多个人物有 entity 但无 state，例如 `天海胜雪`、`平国公主`、`徐夫人`、`霜儿`；
- `徐有容 identity` 出现过明显错位值 `"东御神将府的大丫环霜儿"`；
- `落落` 和 `落衡` 同时存在，且 `落衡.aliases` 含 `落落`，导致 exact mention 被 Grounder 判歧义。

这不是“一个 alias bug”。它表明 entity extraction、canonicalization、coreference、typing 和 state
assignment 都没有达到成熟 KG 或可靠 Memory World 的最低闭环。

### 13.3 relation-like state 不能替代 KG

最终 World 中确实有不少关系线索，但它们是 state：

```text
陈长生 teaches "luo_luo"
落落 learns "zhongshan_fengyu_jian"
落衡 teacher_request "wants Chen Changsheng as teacher"
落衡 teacher "陈长生"
陈长生 teacher "落落殿下"
唐三十六 enrollment_status "加入国教学院"
金玉律 identity "妖族四大神将之首"
薛醒川 location "arrives at Guojiao Academy gate"
故事世界 guojiao_academy_enemy "莫雨"
```

这些状态能让 Planner prompt 偶尔看见关系线索，所以“关系问题先天没有入口”这句话需要修正为：

> 关系线索有时进入了 Planner 的 state summary；但它们没有被结构化成 `RelationRecord`，因此不能进入
> typed graph、不能参与关系遍历、不能稳定消歧实体、不能形成成熟 KG。

### 13.4 更新后的更精确判断

所以最终判断应再收紧：

```text
Graph 层：空壳。
Entity 层：不是 0，但非常薄，且 canonicalization/coreference/typing 不可靠。
State 层：有内容，但把关系、机构、地点、身份大量混成字符串。
Retrieval 层：text/BM25/dense/raw expansion 部分可用。
Product 层：仍被 claim-first 截走，没有 evidence-first package。
```

因此，用户说“如果其他人物实体没有真的构建出来，那就是空壳”是成立的，但要精确表述为：

> 存储与检索框架不是空壳；KG/Graph 与完整实体世界基本是空壳/薄壳。当前系统靠 BM25/dense/raw
> evidence 与少量 state 硬顶，而不是靠成熟知识图谱记忆体系在工作。
