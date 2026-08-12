# Stage 2 Memory 架构大修执行设计：Evidence-first Writing Package、L0 证据与 KG/Graph 修复

- Lifecycle: `ACCEPTED_REPAIR_EXECUTION`
- Status: `PASS / STAGE2M_ARCHITECTURE_REPAIR_ACCEPTED / UNIFIED_REAL_GATE_PASS`
- Date: `2026-08-11 +08:00`
- Stage: `Stage 2 Memory Foundation / Stage 2M repair supersession`
- Current gate: `Stage 2M architecture repair PASS`；legacy claim-first/WP8 remains historical
  diagnostic evidence and is not reopened
- Governing ADR: `docs/adr/0008-evidence-first-writer-context-product.md`
- First handoff supplement: `.agent/plan.md`
- Successor: 通过本文件定义的新 Stage 2 repair gate 后，Stage 3/4 才消费
  evidence-first `WriterContextPackage + EvidenceLedger`

## 0. 当前状态与本次改写

本文件现在不再只作为“方向清单”。它必须同时承担两件事：

1. 说明 Stage 2 repair 的产品边界和执行顺序；
2. 给实现者足够具体的文件级修复指令，能判断“改完后是否完整可用”。

### 0.1 初始实现盘点（2026-08-11，已由 §29 最终验收取代）

以下保留 2026-08-11 启动时的基线，第一轮 evidence-first package 当时尚未完成交付；当前接受
结论以 §29 为准。

已看到的代码级进展：

- `src/novel_agent/domain/writer_context.py` 已新增 v2 类型：
  `EvidenceSlice`、`EvidenceLedgerEntryV2`、`EvidenceLedgerV2`、
  `WriterContextEvidenceItem`、`WriterContextPackageV2`、`EvidenceFirstPackageManifest`。
- `src/novel_agent/services/evidence_slice_resolver.py` 已新增 L0 exact slice resolver，
  覆盖段落、句窗、stable id、offset/hash round-trip、heading role。
- `src/novel_agent/services/evidence_first_writer_context_assembler.py` 已新增
  evidence-first assembler，将 selected exact slices 打包成 v2 package + v2 ledger。
- `src/novel_agent/services/evidence_first_checkpoint_runner.py` 已新增 checkpoint runner。
- `scripts/run_evidence_first_frozen_checkpoints.py` 已新增五点 frozen runner，
  包含 package/ledger/manifest/Markdown/output index 导出逻辑。
- `schemas/stage2/*V2.schema.json` 已开始导出。
- `tests/unit/test_evidence_first_writer_context.py` 已新增 resolver/assembler/v2 package 回归。
- `tests/unit/test_plan_conditioned_planner.py` 已补 Grounder、lexical anchor、summary 覆盖回归。

尚未看到的交付证据：

- `.agent/implementation.md` 尚未追加本轮要求的 `§30`。
- 尚未发现 `/tmp/ns-stage2m-evidence-first*` 五点输出目录。
- 尚未看到本轮 `make quality`、pre-commit、offline admission replay、real five-checkpoint run
  的完成证据。
- 尚未看到五个 checkpoint 的 `writer_context_package.json`、`evidence_ledger.json`、
  `package_manifest.json`、`writer_context_package.md` 和 `output_index.json`。

因此当时状态是：

```text
Round 1 code: partially implemented
Round 1 tests: partially implemented, not yet fully verified
Round 1 real artifacts: missing
Codex review readiness: NOT READY
```

### 0.2 本文件的执行形态

本文件后续只保留三轮执行，不再拆成过多 Phase：

1. **Round 1：frozen 五点 evidence-first Writing Package 交付。**
   当前正在做。只修 Writer package、L0 slice、Grounder/Validator/Summary 小修、runner 和测试。
   不修 KG，不重放 World，不改 index。
2. **Round 2：Need/Retrieval/Readiness 收口。**
   只在 Round 1 artifacts 暴露出 route/selection/gap/readiness 问题后进入。仍不做 World/KG 重建。
3. **Round 3：World/KG/R1 typed_graph 重建。**
   只有 Round 1/2 证明 frozen World/KG 确实阻塞 Writer/Planner 时才进入。这里才允许
   entity/alias repair、RelationRecord backfill、R1 graph projection 和 relation anchors。

后文的 Repair A-H 是**子系统卡片**，不是执行阶段。执行顺序以本节三轮为准。

## 1. 权威输入与参考实现

### 1.1 本仓库权威输入

- `.agent/current_memory_architecture_reality_audit_20260811.md`
- `.agent/plan.md`
- `.agent/need_pipeline_audit_and_semantics.md`
- `docs/adr/0008-evidence-first-writer-context-product.md`
- `docs/project_status.md`
- `docs/stage2_memory_benchmark_task_closure_execution.md`
- `docs/stage2_hybrid_retrieval_execution.md`
- `docs/adr/0003-freeze-deterministic-memory-gateway.md`
- `长篇小说Agent总体架构设计_v2.2_完整合并版.md`
- `长篇小说Agent技术实施与选型设计_v0.1.md`
- `长篇小说Agent正式开发执行规划_v0.1.md`

这些文档的合成判断是：

- Stage 2 read-side 产品是 Writer 可消费的 ContextPackage，而不是 Memory 自己生成的标准答案；
- truth-critical 证据必须回到 L0 exact slice；
- L1/L2/Graph 只能做 discovery、ranking、navigation 和 context-building，不是替代 L0 的真源；
- deterministic Gateway 和信息边界保持冻结；
- ADR-0008 已经把 Claim Support / whole verifier / semantic evaluator 移出默认产品路径；
- `.agent/plan.md` 是第一轮执行补充，不是新的上位设计。

### 1.2 已克隆参考代码

Microsoft GraphRAG：

- Local path: `/tmp/ns-ref-microsoft-graphrag-20260811`
- Commit: `14a00ad88fc33cf2b52f4f113f25807556f8e25e`
- Docs: [Architecture](https://microsoft.github.io/graphrag/index/architecture/)、
  [Default Dataflow](https://microsoft.github.io/graphrag/index/default_dataflow/)
- Source focus:
  - `packages/graphrag/graphrag/index/workflows/create_base_text_units.py`
  - `packages/graphrag/graphrag/index/operations/extract_graph/extract_graph.py`
  - `packages/graphrag/graphrag/index/operations/finalize_relationships.py`
  - `packages/graphrag/graphrag/query/context_builder/local_context.py`
  - `packages/graphrag/graphrag/query/input/retrieval/text_units.py`
  - `packages/graphrag-chunking/graphrag_chunking/`

Neo4j GraphRAG Python：

- Local path: `/tmp/ns-ref-neo4j-graphrag-python-20260811`
- Commit: `b38e674d0b4b701087a5e9238c8fd36068d47887`
- Docs: [Knowledge Graph Builder](https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_kg_builder/)
- Source focus:
  - `src/neo4j_graphrag/components/entity_relation_extractor.py`
  - `src/neo4j_graphrag/components/lexical_graph.py`
  - `src/neo4j_graphrag/components/resolver.py`
  - `src/neo4j_graphrag/components/schema.py`
  - `src/neo4j_graphrag/components/kg_writer.py`
  - `src/neo4j_graphrag/retrievers/`

LightRAG：

- Local path: `/tmp/ns-ref-lightrag-20260811`
- Commit: `b93f7c31f10880bcaccc9518c6a7582fbbe94f1b`
- Repo: [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG)
- Docs: [Paragraph Semantic Chunking](https://github.com/HKUDS/LightRAG/blob/main/docs/ParagraphSemanticChunking.md)
- Source focus:
  - `lightrag/chunker/paragraph_semantic.py`
  - `lightrag/chunk_schema.py`
  - `lightrag/operate.py`
  - `lightrag/kg/factory.py`
  - `lightrag/kg/*_impl.py`
  - `lightrag/rerank.py`

### 1.3 参考实现只提供组织原则

成熟 GraphRAG / KG-RAG 共同给出的基线是：

1. **TextUnit / Chunk / Evidence slice 是共同证据根。** 上层 entity、relationship、claim、context
   都必须能回到源文本单位。
2. **Graph extraction 与 claim/covariate extraction 分离。** KG 建设是 entity/relation schema 和
   resolver 问题，不是 Writer package 先生成答案的问题。
3. **Entity resolution 有策略和统计，不靠模糊猜测静默合并。** exact、similarity、schema constraint
   可以存在，但进入 canonical World 前必须有 receipt。
4. **Graph index、lexical/vector index、context builder 分层。** 查询先找 compact handles，再进入
   context builder；context builder 负责预算内组装，不负责生成最终答案。
5. **Chunking 需要结构优先和预算 fallback。** 段落、标题、表格、长段 fallback 都要保 source span 和
   token 上限。

本项目直接采纳：

- L0 EvidenceSlice 是 Writer 可见证据必经出口；
- World RelationRecord 是 typed_graph 的唯一 canonical source；
- Entity/Alias repair 必须有 evidence-backed receipt；
- Query/retrieval 返回 handle，selection 后才 L0 展开；
- ContextPackage 是预算内证据组装器，不是 claim generator。

本项目明确不采纳：

- 不把 Microsoft GraphRAG、LightRAG、Neo4j GraphRAG 代码复制进仓库；
- 不把 Neo4j 作为 Stage 2 repair 前置；
- 不新增 community report、global graph report 或第二 truth store；
- 不用外部项目的 evaluator 替代本项目 gate；
- 不以“成熟项目这样做”为理由绕过 Canon / cutoff / profile / taint 边界。

## 2. 当前故障地图

### 2.1 已有骨架

当前代码不是一片空白。已经存在：

- TextRoot / TextBlock / EvidenceRef / CAS；
- WorldRoot / Entity / StateRecord / RelationRecord / Event / Obligation；
- R1 Postgres read model；
- L1 Anchor / grounded block；
- L2 OpenSearch BM25 / dense / grounded index；
- RRF / reranker / route planner；
- raw span expansion；
- EvidenceLedger 和 artifact freeze。

这说明大修不应重建平台，而应修正责任边界和关键缺口。

### 2.2 已证实断点

| Area | Current reality | Repair direction |
|---|---|---|
| Writer boundary | `writer_context.v2` 名字存在，payload 仍是 claim-first | 默认产品改成 evidence-first package |
| Raw evidence | legacy ledger 有 raw/no-support entries，但注释明确不会成为 Writer items | selected exact slice 直接 materialize 为 Writer-visible evidence item |
| L0 read grain | block/span 骨架存在，paragraph/sentence exact slice 合同不稳定 | 建 EvidenceSliceResolver，所有 exposed evidence 都 exact dereference |
| KG relation | 71 个 WorldRoot 的 relations 全为 0 | 后续单独做 evidence-backed graph extraction/backfill |
| typed_graph | 五 checkpoint `graph_edge_count=0` | relation rows 有效后再投影；当前要显式 `GRAPH_UNAVAILABLE_ZERO_EDGES` |
| Entity alias | `落落/落衡` alias 冲突，`国教学院` 缺 canonical entity | first slice 修 Grounder 规则；KG slice 修 entity/alias receipts |
| Need | completion contract 仍要求 current claim | 改成 evidence readiness 或 typed gap |
| Planner | 空 relations、state 固定截断、missing entity 拖累 | first slice 修 bounded target-aware summary；KG 后再给真实 relation view |
| Retrieval | graph route 空，fallback reason 不够产品化 | route health、fallback、L0 expansion trace 显式化 |
| Benchmark | 旧 Stage 2M 围绕 claim support/evaluator | 新 Gate 测 mechanical readiness；外部语义评分 freeze 后隔离 |

### 2.3 根因拆分

不要把所有失败都归因于“检索差”或“Evaluator 差”。当前根因分四层：

1. **产品出口错位。** Writer 应看到 evidence package，实际看到 claim list。
2. **L0 出口未成为强制入口。** raw evidence 有，但没有成为 package item 的唯一来源。
3. **World/KG 内容质量薄。** 实体少、组织缺失、关系为 0、关系事实混在 state 字符串。
4. **Need/Planner/route 被 claim contract 和空 graph 拖累。** Planner 有可用问题生成能力，但输入
   world view 和 completion semantics 不对。

推进顺序必须先关闭 1 和 2，再判断 3 对质量的实际影响；不能继续修 claim evaluator。

## 3. 目标架构与层边界

Stage 2 repair 后，默认读路径是：

```text
public Task / accepted Plan / cutoff Canon
  -> Focus / Need, with evidence intent
  -> R0 / R1 / L1 / L2 / typed_graph route plan
  -> compact retrieval handles
  -> selected handle L0 exact EvidenceSlice resolution
  -> evidence selection and budget packing by Need/facet
  -> WriterContextPackage(evidence-first) + EvidenceLedger
  -> Writer / external evaluator / human review
```

层边界：

| Layer | Responsibility | Not responsible for |
|---|---|---|
| L0 Text/EvidenceSlice | exact source text, offset/hash/provenance | ranking, summarizing, semantic scoring |
| World/KG | evidence-backed canonical entity/state/relation/event/obligation records | free-text search, Writer-facing packaging |
| R1 typed graph | exact/temporal/graph read model over canonical World records | inventing relation edges |
| L1 Anchor | compact, rebuildable navigation handles | truth source |
| L2 BM25/dense/grounded | lexical/vector discovery and ranking | final evidence truth |
| Need/Planner | public evidence intent, focus, typed gaps | generating standard answer claims |
| Package/Packer | budgeted Writer evidence surface + ledger refs | verifier/evaluator semantics |
| Benchmark | mechanical readiness and frozen artifact identity | in-run Gold scoring |

Core invariants:

1. WriterContextPackage 的默认 item 是 evidence item，不是 claim item。
2. 每个 Writer 可见 item 必须引用同包 EvidenceLedger entry，或显式 typed gap。
3. 每个 Ledger entry 必须能回到 TextRoot L0 exact slice。
4. exact slice 至少包含 parent block id、start/end offset、text hash、source basis、snapshot、
   cutoff、scope、visibility、taint。
5. Need、retrieval、selection、packing 不得读取 future、Gold、private 或未授权证据。
6. budget 不足时必须报告 typed gap，不能静默丢弃 mandatory evidence。
7. Claim-first path 只能作为 legacy diagnostic 显式启用，不能参与默认 READY。
8. World/KG replay 不覆盖旧 accepted artifacts；只能产生新 repair identity。

## 4. 三轮执行顺序（取代 A-E 多阶段）

本轮修复不再用五六个 Phase 拆散。执行只分三轮，每轮都有清晰入口和出口。
任何实现者不得跳轮，也不得把 Round 3 的 KG/Graph 工作塞进 Round 1。

### Round 1：frozen 五点 evidence-first Writing Package

Purpose: 先证明 Writer 产品边界修对。用 frozen C20/C40/C60/C80/C95 的现有 TextRoot/World/R1/L2/index，
交付 Writer 可直接阅读的 evidence-first package。

Entry:

- 使用 `/tmp/ns-stage2m-phase4-v33-apc-20260810` frozen source project；
- 使用 `na_s2m_phase4_v33_apc_v1` frozen Canon DB；
- 使用 frozen C20/C40/C60/C80/C95 commit/snapshot/index；
- 不重放 ch0-95；
- 不修改 Canon/World/TextRoot/index；
- 不调用 Planner 模型，复用 frozen Planner artifact / frozen Needs；
- 不调用 Claim Support、whole verifier、semantic evaluator。

Current code status:

- v2 domain/schema、L0 resolver、evidence-first assembler、checkpoint runner、five-checkpoint
  script 和核心 unit tests 已开始落地；
- 本轮仍缺 `make quality`、pre-commit、offline admission replay、real five-checkpoint run、
  artifacts 和 `.agent/implementation.md §30`。

Implementation must finish:

1. file-level cleanups in §14；
2. focused tests；
3. `make quality`；
4. `PRE_COMMIT_HOME=/tmp/ns-precommit-cache .conda-env/bin/pre-commit run --all-files`；
5. `git diff --check`；
6. offline admission replay；
7. real five-checkpoint run；
8. write `.agent/implementation.md §30` with exact artifact paths and command evidence。

Round 1 acceptance:

- 每个 checkpoint 都有 `writer_context_package.json`、`evidence_ledger.json`,
  `package_manifest.json`、`writer_context_package.md`、`case_record.json`；
- output root 有 `output_index.json`；
- every non-gap package item has ledger refs；
- every ledger entry has exact L0 `EvidenceSlice` and `dereference_receipt="verified_read"`；
- Claim Support / whole verifier / semantic evaluator call count are all zero；
- future/gold/private leakage count is zero；
- immutable root hashes are unchanged；
- package and ledger validate against v2 schemas；
- `.agent/implementation.md §30` reports commands, artifacts, zero-call proof and typed gaps。

Round 1 stop:

- package READY still depends on `ClaimSupportGroup` / `ClaimVariant` / `ClaimSupportReceipt`；
- selected evidence cannot L0 dereference；
- script must rebuild index or mutate DB to run；
- evidence selection requires Gold/future/case hardcoding；
- current retrieval owner cannot deliver existing raw material without adding a model scorer。

### Round 2：Need / Retrieval / Readiness 收口

Purpose: 只修 Round 1 artifacts 暴露出的 read-side 问题。Round 2 仍然不修 World/KG。

Entry:

- Round 1 五点 artifacts 已生成；
- Codex review 指出 gaps 属于 Need、route、selection、budget、trace、readiness 的 read-side 问题；
- 不需要 relation backfill 才能证明下一步。

Implementation:

1. `NeedCompletionSpec` default no longer gates on current claim；
2. `NeedQueryCompiler` keeps lexical/dense path for unresolved public anchors；
3. route trace records eligible/ineligible channels and graph unavailable reason；
4. `EvidenceFirstPackageManifest` / readiness report gains missing mechanical fields；
5. packer fixes any mandatory-drop, dedupe, budget or trace defect found in Round 1。

Acceptance:

- default Need artifact has no active “one current claim” stop condition；
- public query text/hints explain package items；
- graph empty produces typed unavailable reason, not silent success；
- typed gaps enter package and manifest；
- no Gold/future leakage；
- updated five-point run proves the read-side issue closed。

### Round 3：World / KG / R1 typed_graph 重建

Purpose: 修复已被现实审计确认的 World graph source 缺失，使本项目已有的
`WorldRoot -> R1 -> L1/L2 -> typed_graph -> L0` 链路真正可运行。Round 3 不以填高关系数、
命中固定专名或通过几个 benchmark case 为目标；WorldRoot 仍采用 open-world 语义，完成口径是通用
构建机制和真实投影闭环，而不是宣称正文语义已被全量抽取。

#### Round 3 当前状态（2026-08-12）

`.agent/implementation.md` §31 已完成最小关键实现：

- `src/novel_agent/services/world_graph.py` 已有 18-predicate registry、relation-like State
  audit、exact internal-label/alias resolver、evidence/type/dedupe validation 和新 WorldRoot hash；
- `scripts/backfill_world_graph.py` 可以对一组 World/Text JSON 做只读 backfill 并导出 repair root/receipt；
- `src/novel_agent/services/r1.py` 已有 row-verifiable/L0-verifiable `GraphPathReceipt`；
- `src/novel_agent/services/memory_pipeline.py` 已能生成 entity/relation anchors 并保留 source refs。

这只是 MVP，不是 Round 3 完成。当前 pass 主要把 scalar `StateRecord.value` 解释为指向已有 entity 的
关系；missing entity 仍只能拒绝；L0 正文没有形成 bounded graph candidate stream；JSON CLI 没有进入
现有 Overlay、Validation、Artifact/Commit 和 `FullDerivedProjectionBuilder`；真实 repair identity 下的
R1/L1/L2 尚未重建。因此下一步不是继续给当前 pass 加样例分支，而是补齐以下唯一生产链：

```text
existing World records + TextRoot/L0 evidence units
  -> evidence-bound entity/relation candidates
  -> exact entity resolution or evidence-backed entity admission
  -> predicate/type/time/truth/evidence/dedupe validation
  -> ChangeOperation / ObservedChangeSet
  -> WorldOverlay + Stage1Validator + existing artifact/commit corridor
  -> immutable repair WorldRoot identity
  -> FullDerivedProjectionBuilder
  -> R1 relation rows + L1 anchors + L2 indexes
  -> GraphPathReceipt
  -> exact L0 dereference
```

#### Entry 与并行边界

- Reality audit 已证明 graph source 是独立结构缺陷：71 个 WorldRoot relation 全空、五点 graph edge
  全空，Round 3 已由用户明确授权，不再等待 Round 2 先证明“空图确实存在”；
- Round 2 可同时修 Need/Retrieval/Readiness，但 Round 3 开发期间不得修改 Round 2 owner；
- 两边只在最终 manifest/readiness 接线处整合：Round 2 消费 projection attestation、graph counts、typed
  unavailable reason 和 verified path receipt，不反向拥有 World/KG；
- 所有 repair 在新的隔离 identity/workspace 中执行，frozen DB/index/Commit/World/Text roots 只读。

#### 从克隆代码采纳的实现组织

本轮只借鉴结构，不复制代码或依赖：

1. Microsoft GraphRAG 的 `TextUnit -> extract -> merge -> orphan filter -> relationship context`：
   extraction 必须按稳定 source unit 运行；合并后先去重并拒绝 dangling endpoints，再进入查询；
2. Neo4j GraphRAG Python 的 `extractor -> resolver -> writer`：模型输出只是 batch-local candidate，
   entity resolution 和 canonical write 是独立步骤；本项目的 writer 是现有 World mutation corridor，
   不是 Neo4j；
3. LightRAG 的 chunk source-id retention 和 rebuild/merge：entity/relation 聚合后仍保留全部 source
   lineage，并能基于同一输入确定性重跑；本项目使用 `EvidenceRef`、repair receipt 和 CAS identity
   实现，不新增 graph storage abstraction。

不采纳 fuzzy/similarity entity merge、description summary 作为事实、community/global reports、自动关系
权重、独立 graph writer、第二向量实体库或新的 storage plugin family。这些都不是当前空图的必要修复。

#### 下一步操作：一个连续开发单元

下面 5 项是同一轮连续实现顺序，不再拆成 Round 3A/3B/3C，也不在每一项之间启动大测试轮。

##### 1. 把 structured audit 与 L0 extraction 统一为 candidate stream

保留当前 relation-like State audit，同时增加 bounded L0 graph candidate generation：

- 复用 `ModelCurator.extract_reported_v2()` 已有的 EvidenceCandidate catalog、semantic quote grounding、
  exact `EvidenceRef`、ModelGateway、structured output 和 model-call ledger；
- 在 `ModelCurator` 现有 owner 内增加窄的 graph-repair entry point/profile，只允许提出 `entity CREATE`
  和 `relation CREATE`；不新建第二个 extractor service；
- candidate 按现有 paragraph/sentence EvidenceCandidate 或 TextUnit 分批。若 chapter-wide “最多四项”
  会截断关系发现，就缩小 source batch，不提高为无界输出；
- 每个 batch identity 由 source TextRoot、basis commit、chapter/TextUnit/evidence candidate ids 和 policy
  version 确定；同一输入重跑得到相同 candidate ids/order；
- relation-like State candidate 与 L0/model candidate 最终进入同一 admission API，并转换成现有
  `ChangeOperation` / `ObservedChangeSet` 可消费的操作；
- model 只负责 candidate discovery，不能决定 canonical entity id、不能直接构造 accepted WorldRoot，
  也不能新增自由 predicate。

禁止 regex 从正文直接宣判关系，禁止在 prompt/host code 中加入固定专名、case id、checkpoint-specific
relation、Gold 或测试期望值。State/Event/Obligation 的常规写入不在这次 graph repair 中顺手重做。

##### 2. 完成通用 entity admission

所有 relation endpoint 先经过 `EntityAliasRepairPolicy`，并按下面唯一顺序处理：

1. unique exact `internal_label`：复用 canonical entity；
2. 无 canonical label、但有 unique exact alias：复用 canonical entity并保留 receipt；
3. exact label/alias 均不存在：只有当同批 candidate 给出明确 `entity_type`、正文中的 exact surface
   mention 和合法 EvidenceRef 时，host 才能生成 stable entity id 并提出 entity CREATE；
4. canonical label collision 或 alias collision：fail closed，保留 typed ambiguity receipt；
5. pronoun-only、type-only inference、fuzzy/similarity match：不得创建或合并 entity。

model-emitted target id 不是 canonical authority。host 必须根据 admission policy 生成或映射 stable id，并
同步重写同批后续 relation candidate 的 endpoint。entity CREATE 必须排在引用它的 relation CREATE 前，
再由 `WorldOverlay` 做 dangling-reference 校验。

当前没有真实证据要求建设通用 alias merge/split engine，本轮不实现该引擎；已有 entity 冲突继续 typed
reject。只有出现带明确 identity evidence、且 exact admission 无法表达的真实候选时，才返回 Codex 决定
是否增加最小 merge/split operation。

##### 3. 把 relation admission 接到现有 mutation contract

`WorldGraphExtractionPass` 继续作为 Round 3 唯一 admission owner。structured 和 L0/model candidates
统一执行：

- predicate 必须存在于 `PredicateRegistry`；
- subject/object type 满足 domain/range；
- endpoints 已存在或有同批 admitted entity CREATE；
- truth class 允许进入 accepted World fact；
- valid time/worldline 合法，未有证据时不得虚构结束时间；
- EvidenceRef 在当前 TextRoot/cutoff/basis 可解引用，quote hash 和 span round-trip 成立；
- local evidence 对该 relation 有直接支持；
- stable relation identity、exact duplicate 和 registry multiplicity 规则一致。

local evidence support 复用已有 evidence binding/support gate；不得重新引入 Claim Support、whole
verifier、semantic evaluator 或 Gold matcher。发现 registry 缺少正文中反复出现的直接关系时，保留
candidate/evidence 统计并返回 Codex；实现者不得让 model 生成任意 predicate，也不得趁机扩建 ontology。

accepted candidate 必须转换为 `ChangeOperation` / `ObservedChangeSet`。当前 pass 直接构造的
`repaired_world` 可以保留为便捷返回值，但其内容必须由 `WorldOverlay.apply()` 从同一 change set 得到并
与 direct result 相等；不再让 `world_graph.py` 成为第二套 mutation protocol。

relation-like State 暂时全部保留。Predicate Registry 已声明某属性属于 Relation，并不自动授权删除历史
State；State 的 supersede/migration 需要单独证据和上位决策，不能在 graph backfill 中静默执行。

##### 4. 形成 immutable repair identity

把 `scripts/backfill_world_graph.py` 从单 root 诊断入口扩展为 Round 3 唯一 operational runner；不要再
新增并行 runner。它应支持 root JSON 诊断输入，也支持 source project/artifact location + 一个或多个
source commit/checkpoint + repair workspace/output location。需要模型候选时，只接受现有 ModelGateway
配置和锁定 model identity。

正式路径必须复用现有 owner：

```text
accepted candidate receipts
  -> ObservedChangeSet
  -> WorldOverlay
  -> Stage1Validator
  -> ArtifactRepository / CAS
  -> CommitService or existing replay corridor in isolated repair workspace
```

不得写 frozen project。若现有 CommitService 不能在不改变 frozen lineage 的情况下表达 repair child，
就在隔离 workspace/project identity 中导入只读 basis 并记录 source -> repair mapping；不得为这次离线
repair 给生产 Commit 模型增加通用 branch framework。

runner 只导出完成闭环所需的最小产物：

- `repaired_world_root.json`；
- `world_graph_extraction_receipt.json`，包含 source batches、entity resolution/admission、accepted/rejected
  relation candidates 和 rejection basis；
- `repair_manifest.json`，绑定 source roots/commit、policy/model identity、repair identity、new root 和
  candidate/accepted/rejected/deduped counts；
- projection 完成后复用现有 projection attestation 和 `GraphPathReceipt` artifact。

不新增 Markdown 报告、dashboard、graph database dump、第二 output-index family、queue 或 control plane。
重跑与 resume 只依赖 stable batch id、content-addressed artifacts 和一个 repair manifest。

##### 5. 用真实 repair identity 重建 R1/L1/L2

对 repair commit/identity 调用现有 `FullDerivedProjectionBuilder`，不得单独手写 graph projection：

- 每个 accepted `RelationRecord` materialize 为一个 R1 relation row，并写 subject/object role association；
- `graph_edge_count` 从该 repair identity 下可见且有 evidence 的 accepted relation rows 计算；
- `AnchorBuilder` 从同一 World/Text basis 生成 entity/relation anchors，保留 World root 和 L0 evidence
  source refs；
- anchor/grounded L2 indexes 按现有 build profile 发布，不增加 graph-owned text store；
- `typed_graph_paths()` 先形成 relation-row-verifiable receipt；进入 package lineage 前，必须调用
  `validate_graph_path_receipts()` 变成 L0-verifiable receipt；
- graph 只能做 discovery/navigation。最终 Writer 可见材料仍通过 Round 1 的 EvidenceSliceResolver 和
  evidence-first package 展开 L0，不把 relation anchor 或 path 文本当作真值。

Round 2 完成后，只在其现有 manifest/readiness contract 上接入 projection attestation、
`graph_edge_count`、graph unavailable reason 和 verified receipts。不得新增独立 graph-readiness service。

#### 文件级修复指导

| File / owner | 必须完成的改动 | 保持不变的边界 |
|---|---|---|
| `src/novel_agent/domain/world.py` | 只补现有 receipt 无法表达的 source batch、entity admission 和 rejection basis；保持 accepted/rejected accounting 可核对 | 复用 `ChangeOperation`，不新增第二 mutation DTO/ontology |
| `src/novel_agent/services/world_graph.py` | 统一 structured/L0 candidate admission；host-owned entity id mapping；predicate/type/time/truth/evidence/multiplicity validation；输出 change set + receipt | `WorldRoot.relations` 是唯一 graph truth |
| `src/novel_agent/services/model_curation.py` | 在现有 V2 evidence-grounded Curator 内增加 bounded graph candidate mode；限制 entity/relation create 和 registered predicate | model 只提候选，不写 root、不决定 identity |
| `src/novel_agent/services/overlay.py`、`validation.py` | 复用现有 apply/validate；只补真实暴露出的 registry/domain/range/multiplicity validation hook | 不复制 World mutation/commit protocol |
| `src/novel_agent/services/artifacts.py`、`commits.py`、`replay.py` | 复用 CAS、Commit/replay 形成隔离 repair identity 和 source lineage | 不改 frozen identity，不建设分支平台 |
| `scripts/backfill_world_graph.py` | 成为 candidate -> admission -> mutation -> artifact -> projection 的唯一 runner，保留简易 JSON 模式 | 不新增第二 runner/report family |
| `src/novel_agent/services/projection.py` | 对 repair identity 调用 existing `FullDerivedProjectionBuilder`，发布 attestation | 不直接写独立 graph index |
| `src/novel_agent/services/r1.py`、`memory_pipeline.py` | 保留 §31 receipt/anchor 实现；只修真实 projection 发现的契约缺口 | 不增加 inferred/similarity edge，不把 L1 当 truth |

每个新增或扩展契约必须遵守 repository minimum-sufficient rule：其 current caller、responsible layer、
protected invariant 和 acceptance artifact 必须能在上表和本 Round 3 数据流中指出。没有当前 caller 的
抽象、配置项、存储、状态机和报告不得加入。

#### 开发节奏

先把上述一个连续链路开发完整，再交给 Codex 做统一测试、真实运行、验收与 Round 2 整合。开发中只做
防止明显 typing/contract 破裂的窄检查，不穿插 full `make quality`、pre-commit、五点真实矩阵，也不根据
现有 fixture 反复调 prompt/阈值。测试代码最终只验证通用 invariant，不写专名、case id 或
checkpoint-specific expected relation。

#### Round 3 完成验收

1. 同一 runner 能处理任意合法 source root/commit，不含固定 case/checkpoint/专名分支；
2. source candidate accounting 可闭合：每个 candidate 都进入 accepted、rejected 或 exact-deduped，且有
   receipt；
3. 有合法 evidence-backed relation candidate 的 repair identity 能产生 `relations > 0`；每条 accepted
   relation 的 endpoints、predicate、time、truth、EvidenceRef 和 admission lineage 均合法；
4. missing entity create 走通用 exact-evidence admission；ambiguous/missing/unsupported 走 typed reject，
   不存在 fuzzy fallback；
5. old accepted WorldRoot/Commit/DB/index 不变，repair root、manifest 和 source lineage 可验证；
6. R1 `graph_edge_count` 精确等于 repair identity 下可见 accepted relation rows，不是硬编码非零值；
7. Need-derived seed 存在 canonical path 时，typed graph receipt 可回到 relation rows 和 exact L0 slices；
   不可达时返回 typed reason，不伪造路径；
8. entity/relation anchors 和 L2 indexes 与同一 repair identity、scope、cutoff、basis 对齐；
9. Round 2 整合后 readiness 能区分 graph ready、zero-edge、missing-seed、filtered/no-path，Writer package
   继续 evidence-first；
10. 没有第二 graph truth store、Neo4j/GraphRAG/LightRAG dependency、community report、Claim/evaluator
    回流或 Stage 3+ 改动。

实现完成后只做一轮统一验收：focused invariant/contract tests、full quality/pre-commit、隔离 repair
workspace 的真实 checkpoint run、projection attestation、graph path L0 dereference 和 immutable-root
verification。失败只能修通用 contract/root cause，不能添加 case-specific shortcut。

#### Stop conditions

出现以下情况必须携带 candidate/receipt/artifact evidence 返回 Codex，不得自行扩大架构：

- existing `ChangeOperation` / `ObservedChangeSet` 无法表达 evidence-backed entity+relation 同批 create；
- registry 缺失项需要新的 semantic owner、closed-world、对称/传递或复杂 cardinality 决策；
- repair identity 只能通过修改 frozen project 才能持久化；
- candidate 只有依赖 pronoun/coreference、fuzzy merge 或 unsupported model inference 才能通过；
- current R1 row/CTE 无法表达 canonical path receipt，且真实查询证据证明需要 schema migration；
- Round 2 整合要求改写 evidence-first truth boundary 或重新引入 Claim/evaluator。

## 5. Repair A：Evidence-first WriterContextPackage

### 5.1 Current reality

`src/novel_agent/domain/writer_context.py` 当前的 `WriterContextItem` 仍强制 `claim: str`。
`EvidenceLedgerEntry` 仍有 `claim_excerpt`。
`WriterContextAssembler.assemble_from_spec()` 的输入仍是：

- `ClaimSupportGroup`
- `ClaimVariant`
- `ClaimSupportReceipt`

`raw_evidence_ledger_entries` 的注释还明确说 raw entries 不会成为 Writer items。这个实现与 ADR-0008
相反。

### 5.2 Desired contract

默认 package contract 使用仓库最终命名，但语义必须是 evidence-first。建议字段：

`WriterContextEvidenceItem`:

- `item_id`
- `need_id`
- `facet_id`
- `purpose`
- `evidence_ledger_ids`
- `raw_preview`
- `preview_truncated`
- `source_scope`
- `source_kind`
- `validity`
- `mandatory`
- `selection_reason`
- `gap_id`

`EvidenceLedgerEntry`:

- `ledger_id`
- `evidence_text`
- `evidence_slices`
- `source_locator`
- `retrieval_unit_ids`
- `basis_id`
- `snapshot_id`
- `cutoff_chapter`
- `scope`
- `visibility`
- `taint`
- `text_hash`
- `span_hash`
- `quote_hash`
- `dereference_receipt`
- `need_ids`
- `facet_ids`

Legacy claim fields can remain in legacy diagnostic schema, but cannot be required by the default
runner.

### 5.3 Implementation steps

1. Add or migrate domain models in `src/novel_agent/domain/writer_context.py`.
2. Update `schemas/stage2/*WriterContext*` and `schemas/stage2/*EvidenceLedger*`.
3. Extend `writer_context_assembler.py` so default input is selected exact slices plus Need/facet
   metadata.
4. Keep old claim assembler behind explicit legacy/diagnostic profile.
5. Update `stage2_paired_pilot.py` or the active runner to stop at evidence selection/packing.
6. Remove `claim_support_producer_version` from default product identity; keep it only for legacy
   diagnostic fingerprints.
7. Write deterministic Markdown projection from the same JSON artifact.

### 5.4 Component registration

Component: `EvidenceFirstWriterContextAssembler`

- Current caller: Stage 2 package runner, future Writer read adapter.
- Responsible layer: `services/` read-side packaging.
- Protected invariant: Writer default surface contains only cutoff-safe, scope-safe, ledger-backed
  evidence items or typed gaps.
- Inputs: validated public Need/facet metadata, selected `EvidenceSlice`, selection trace, budgets.
- Outputs: `WriterContextPackage`, `EvidenceLedger`, package manifest, Markdown projection.
- Acceptance evidence: default run produces READY package without claim groups/variants/receipts;
  package item refs are 100% ledger-backed; ledger refs are 100% L0-dereferenceable.

### 5.5 Tests

- default package does not require `claim`;
- no Claim Support calls when evidence-first profile is active;
- raw evidence becomes Writer-visible item;
- package item without selected evidence becomes typed gap;
- ledger ref validation rejects dangling ids;
- Markdown projection is deterministic and derived from JSON;
- legacy claim package remains readable but is not default.

## 6. Repair B：L0 EvidenceSliceResolver

### 6.1 Purpose

Storage block 粒度可以继续较大，但 Writer 可见证据必须是 exact read grain。L0 resolver 是 first slice
的强制前置，不是后续优化。

### 6.2 Slice rules

1. 不改 TextRoot / TextBlock storage identity。
2. 段落边界优先。
3. 单段超预算时，按中文/英文句末标点生成 contiguous sentence window。
4. 仍超预算时 fail closed 或生成 typed budget gap，不能把整章塞入 package。
5. slice id 稳定：

   ```text
   slice.<sha256(parent_block_id + start_offset + end_offset + normalized_text_hash)>
   ```

6. slice text 必须原文摘取，不摘要、不改写。
7. offset round-trip 必须能回 parent block text。
8. heading/title-only 单元需要 source-role metadata；默认不作为正文 evidence，除非 Need 明确询问标题。
9. preview 只是 Ledger exact text 的有界前缀或完整短 slice，必须标注 truncation。

### 6.3 Implementation steps

1. 新增或扩展 `src/novel_agent/services/evidence_slice_resolver.py`。
2. 在 `EvidenceExpander` / retrieval selection 后调用 resolver。
3. grounded candidate 先是 handle，被选中后才解析成 exact slice。
4. ledger entry 只接受 exact slice 或 typed gap。
5. budget packing 只能裁剪 preview，不能破坏 ledger full exact evidence。

### 6.4 Component registration

Component: `EvidenceSliceResolver`

- Current caller: retrieval evidence expander, package assembler, diagnostic replay.
- Responsible layer: `services/` L0 read-grain resolver.
- Protected invariant: Writer 可见证据都能回到 TextRoot 原文 offset/hash。
- Inputs: parent block text/ref, candidate span hints, token budget, source-role metadata.
- Outputs: one or more `EvidenceSlice` plus dereference receipts.
- Acceptance evidence: paragraph, long paragraph sentence fallback, mixed punctuation, offset
  round-trip, hash stability, oversized rejection and heading-role tests pass.

## 7. Repair C：Need / Planner evidence contract

### 7.1 Current reality

Need 层仍包含 claim-first 语义：

- `expected_claim_scope`
- `NeedCompletionSpec.require_current_claim`
- `claim_may_cite_plan`
- `stop_condition="one current claim with a minimal legal evidence set"`
- `completion_criteria="claim is supported by cutoff-valid evidence"`

这会把 retrieval 和 package 牵回 claim closure。

### 7.2 Desired semantics

默认完成条件改为：

```text
Need/facet is served by one or more cutoff-safe exact evidence slices, or an explicit typed gap.
```

Need/facet 应表达：

- `evidence_question`
- `evidence_purpose`
- `required_evidence_facets`
- `allowed_evidence_kinds`
- `minimum_source_diversity`
- `selection_stop_condition`
- `typed_gap_policy`

### 7.3 First-slice Grounder fixes

`NeedDraftGrounder` 使用唯一规则：

1. normalized mention 若唯一精确匹配 `internal_label`，直接选择该 runtime entity；
2. 其他实体同名 alias 不得把 canonical exact match 变成 ambiguous；
3. 无 internal-label exact match 时，唯一 alias exact match 才 grounding；
4. 多 internal-label exact、多 alias、未知 label 仍 fail closed；
5. bounded mention closure 只扫描同一 public draft 的 semantic question、query hints、why-needed、
   trigger goal 和显式 mentions；
6. audit 记录 explicit/derived、source field、match kind、chosen id 或 typed rejection。

已知机制样本：P004 `落落` 应绑定 canonical `落落`，不能因 `落衡.aliases` 含同名而拒绝。

### 7.4 Unresolved lexical anchors

`国教学院` 这类合法 public mention 即使没有 runtime entity id，也必须：

- 保留在 semantic question/query hints/BM25+dense query text；
- 记录 `unresolved_lexical_anchor/no_label_match`；
- 不生成伪 entity id；
- 不执行依赖该 id 的 exact/graph route；
- 只要 Need 还有合法 public semantic question，就不能丢掉整个 Need。

### 7.5 Target-aware WorldSummary

短期不伪造 KG。WorldSummary 修 fixed-first-64 state selection：

1. 从 public task/Plan goals 提取 visible target labels/aliases；
2. 在同一总 token/条目预算内，保证每个目标实体有代表 current/relation-like states；
3. relationship-like states 可以作为 state 原样进入 summary；
4. 不写回 World，不伪造成 `RelationRecord`；
5. manifest 记录 per-target available/selected/truncated counts；
6. `relation_count=0` 如实保留。

### 7.6 Tests

- exact internal label 优先于他实体同名 alias；
- 多 canonical / 多 alias 仍 ambiguous；
- missing organization 保留 lexical/dense query；
- target-aware summary 覆盖目标实体状态；
- empty relations 不被伪造；
- Gold/future fields 无法进入 Need/query/summary。

## 8. Repair D：Evidence selection 与 package packing

### 8.1 Purpose

retrieval candidate 不是 Writer evidence。selection/packing 负责把候选变成预算内、Need/facet 可解释、
Ledger-backed 的 Writer package。

### 8.2 Allowed ranking signals

Allowed:

- route rank；
- BM25/dense/hybrid rank；
- RRF rank；
- rerank score；
- exactness；
- facet coverage；
- mandatory priority；
- source/chapter diversity；
- cutoff distance or chapter recency when the public task needs it。

Forbidden:

- Gold answer；
- future text；
- private notes；
- same-run evaluator feedback；
- legacy claim support verdict as default gate。

### 8.3 Packing rules

1. mandatory evidence first；
2. 每个 Need/facet 至少保留可解释的最小证据，除非 typed gap；
3. span hash 去重；
4. source/chapter diversity cap；
5. budget 不足记录 `BUDGET_EXCEEDED_GAP` 或 `NEEDS_REDUCTION`；
6. package item 放短 preview，完整 text 在 ledger；
7. 同 slice 可服务多个 Need，但 ledger 只存一份；
8. excluded candidate 必须有 reason。

### 8.4 Component registration

Component: `EvidencePackagePacker`

- Current caller: `EvidenceFirstWriterContextAssembler`.
- Responsible layer: `services/` read-side package composition.
- Protected invariant: package 在预算内最大化 public Need/facet evidence coverage，并显式报告未满足项。
- Inputs: selected exact slices, Need/facet priority, route/selection trace, budgets.
- Outputs: item plan, ledger inclusion plan, typed gaps, budget receipt.
- Acceptance evidence: mandatory overflow、dedupe、facet fairness、source diversity、budget gap、
  same-ledger dereference tests.

## 9. Repair E：World / KG 构建质量

### 9.1 Current reality

审计显示：

- 71 个 WorldRoot 的 `relations` 全是 0；
- 五个 checkpoint 的 `graph_edge_count=0`；
- typed_graph 是空壳；
- Entity 层很薄，最大 World 只有 28 entities，其中 organization 只有 `天道院`；
- `国教学院` 等核心机构缺 canonical entity；
- `落落/落衡` label/alias 冲突；
- 大量关系事实混在 StateRecord value 中。

这不是 typed_graph 查询 bug，而是 World graph source 没建起来。

### 9.2 Repair principles

1. KG canonical source 是 evidence-backed World records。
2. RelationRecord 只表达 entity-to-entity durable edge。
3. StateRecord 表达 entity-to-scalar 或 entity property。
4. Event 表达发生过的行动/场景变化。
5. Obligation 表达未来写作债务、承诺、伏笔、未解决事项。
6. 每条 relation 必须有 subject、predicate、object、valid_time、evidence_refs、truth_class。
7. repair WorldRoot 是新 identity，不覆盖旧 accepted artifacts。

### 9.3 Predicate registry MVP

先做当前小说写作高频关系，不做大 ontology：

- `affiliated_with`
- `member_of`
- `enrolled_in`
- `mentor_of`
- `teacher_of`
- `travels_with`
- `protects`
- `opposes`
- `possesses`
- `owns`
- `transfers_to`
- `knows_about`
- `hides_from`
- `discloses_to`
- `promised_to`
- `owes`
- `located_at`
- `resides_at`

每个 predicate 登记：

- current caller；
- owner layer；
- protected invariant；
- acceptance evidence；
- inverse predicate；
- temporal validity；
- multiplicity；
- allowed subject/object entity types；
- example evidence ref。

### 9.4 Entity / alias repair policy

Required behavior:

1. missing organization/location can be created only from exact evidence；
2. alias merge/split requires receipt；
3. exact canonical label collision fails closed；
4. alias collision is reported to Need/Planner as typed gap unless disambiguated by evidence；
5. no fuzzy merge of major entities without Codex-approved policy and receipts；
6. active label map can report `unique_label`, `unique_alias`, `ambiguous`, `missing`。

Component: `EntityAliasRepairPolicy`

- Current caller: `NeedDraftGrounder`, curation validation, graph extraction validation.
- Responsible layer: `services/` entity resolution policy.
- Protected invariant: alias collision 不会静默污染 retrieval 和 graph traversal。
- Inputs: current World entities/aliases, candidate mentions, evidence refs, predicate/object context.
- Outputs: resolve/split/merge/missing receipts and typed gaps.
- Acceptance evidence: known collision cases PASS/AMBIGUOUS/SPLIT；missing org entity repaired with
  evidence；Need artifact reports ambiguous labels.

### 9.5 Graph extraction/backfill

Component: `WorldGraphExtractionPass`

- Current caller: Stage 2 repair replay/backfill runner, future memory curation pipeline if accepted.
- Responsible layer: `services/` memory curation.
- Protected invariant: WorldRoot relation graph only contains evidence-backed canonical records.
- Inputs: TextRoot/L0 evidence units, existing World entities, predicate registry, alias policy.
- Outputs: candidate entity/relation/event/state/obligation operations with exact evidence refs.
- Acceptance evidence: repair checkpoint relations > 0；each relation has legal subject/object,
  predicate and evidence ref；validator has no dangling references.

Implementation route:

1. inventory relation-like states；
2. for each candidate, recover evidence refs；
3. resolve subject/object with alias policy；
4. validate predicate registry；
5. produce candidate relation op；
6. validate cutoff/scope/truth/evidence；
7. write to repair WorldRoot；
8. keep or supersede original StateRecord according to owner invariant。

Do not:

- blindly convert state strings with regex；
- accept relation without evidence；
- infer object entity from unsupported model guess；
- create a second graph truth store。

## 10. Repair F：R1 / typed_graph projection

### 10.1 Current structure

The repo already has:

- `R1RecordRow`
- `R1RecordEntityRow`
- `WorldRecordKind.RELATION`
- `R1WorldRepository.typed_graph_paths()`

PostgreSQL typed graph MVP matches the technical design. Neo4j remains deferred.

### 10.2 Required behavior

1. typed_graph input is accepted relation rows only；
2. `graph_edge_count` reflects canonical relation edge count；
3. zero relation rows returns `GRAPH_UNAVAILABLE_ZERO_EDGES`；
4. graph path result includes `GraphPathReceipt`；
5. graph route fallback to anchor/grounded is allowed only with reason；
6. no graph path can enter package lineage without relation row and L0 evidence dereference。

Component: `GraphPathReceipt`

- Current caller: typed graph retrieval trace, package lineage.
- Responsible layer: `domain/` or `services/` retrieval receipt model.
- Protected invariant: graph traversal result can be explained as evidence-backed relation chain.
- Inputs: relation row ids, entity path, predicate, direction, valid_time, evidence refs.
- Outputs: immutable path receipt.
- Acceptance evidence: typed_graph path result 100% dereferences to relation rows and L0 slices.

### 10.3 Derived graph-edge table rule

Do not add independent `graph_edge` table unless one of these is proven:

- canonical relation, mention, similarity and evidence edges must be queried together；
- recursive CTE cannot produce required path receipt；
- benchmark proves current query performance fails；
- edge semantics require stable separation of `canonical/evidence/mention/inferred/similarity`。

Until then, `WorldRecordKind.RELATION` remains typed_graph MVP source.

## 11. Repair G：Anchor / L1

### 11.1 Current structure

`AnchorBuilder` can build anchors from states、events、relations、obligations、plan and grounded block
units. It is not the first root cause. It is starved by empty relations and thin entity metadata.

### 11.2 Required behavior

1. relation anchors appear naturally after relation rows exist；
2. entity anchors include label、aliases、type、source refs；
3. relation anchors include predicate、subject/object labels、valid_time、evidence_refs；
4. grounded anchors remain discovery handles；
5. every Writer visible result returns to L0 EvidenceLedger；
6. L1 summary never becomes truth source。

### 11.3 Community reports deferred

Microsoft GraphRAG community detection/reporting is useful for large global sensemaking. It is not
authorized for this repair because current blockers are product boundary, exact L0, entity/relation
construction and route health. Reassess only after relation graph is stable and benchmark proves a
global summary need.

## 12. Repair H：L2 hybrid retrieval 与 route health

### 12.1 Current structure

Already present:

- OpenSearch BM25；
- dense index；
- grounded index；
- anchor index；
- RRF fusion；
- reranker；
- route planner；
- typed graph channel。

Do not replace this with an external RAG framework.

### 12.2 Required behavior

1. query returns compact handles；
2. selected handles are expanded by L0 resolver；
3. no all-channel broadcast by default；
4. exact quote / rare phrase prefers grounded BM25；
5. relation chain prefers typed_graph only when graph has edges；
6. graph unavailable falls back to anchor/grounded with reason；
7. reranker processes bounded candidate set；
8. selection trace records inclusion and exclusion reasons。

### 12.3 Trace schema minimum

Every retrieval trace records:

- route plan；
- eligible channels；
- ineligible channels and reasons；
- per-channel candidate count；
- RRF inputs；
- rerank inputs；
- selected candidate ids；
- L0 expansion receipts；
- package inclusion/exclusion reason；
- graph fallback reason。

Acceptance:

- graph empty no longer looks like successful empty result；
- selected evidence all has exact L0 slice；
- scope/cutoff excluded candidates never enter package；
- legacy 33/45 evidence-recovery rows are either delivered or explained by typed gaps in the new
  package audit。

## 13. Repair I：Benchmark / evaluator boundary

### 13.1 New default gate

Stage 2 repair default gate is mechanical readiness:

- contract version；
- package nonempty or typed gap；
- scope；
- cutoff；
- leakage；
- provenance；
- dereference；
- budget；
- no source mutation；
- no default Claim/Evaluator calls；
- reproducibility；
- trace completeness；
- root hash stability。

It is not “claim semantically matches Gold”.

### 13.2 External semantic scoring

After package freeze, user/human/strong model may score:

- evidence covers Gold；
- evidence helps Writer；
- KG relation is semantically correct；
- Need missed important facets。

This scoring:

- can read Gold/future because it is outside the Agent；
- cannot modify the frozen package；
- cannot feed back into same experiment identity；
- must be reported separately from mechanical readiness。

### 13.3 Historical reports

Old WP8/P2/Stage 2M claim-first runs remain historical. Do not rewrite them. New reports must use a
new evidence-first profile/contract/version and cannot aggregate old claim-first metrics as the same
score.

## 14. Round 1 文件级修复矩阵

本节是当前实现者应直接执行的代码级指导。表中 `Status` 是 2026-08-11 本次检查的状态，
不是最终验收。

| File | Status | Required repair / completion | Do not |
|---|---|---|---|
| `src/novel_agent/domain/writer_context.py` | v2 types 已新增 | 确认 `WriterContextPackageV2` 不 require `claim`；`WriterContextEvidenceItem` 非 gap 必须有 ledger refs；`EvidenceLedgerEntryV2` 必须 require exact slices、Need ids、hashes 和 `verified_read`；`EvidenceFirstPackageManifest` 必须有 package/ledger refs、call counts、immutable roots、status | 不删除 v1 legacy 类型；不让 v2 继承 claim-first required 字段 |
| `schemas/stage2/EvidenceSlice.schema.json` and v2 schemas | 已导出 | 用 schema tests 验证 package/ledger/manifest/readiness 能 validate；确认 media type 与 runner 写出的 artifact 一致 | 不把 old `WriterContextPackage.schema.json` 直接改成 v2 导致历史 artifact 不可读 |
| `src/novel_agent/services/evidence_slice_resolver.py` | 已新增 | 保证 `resolve_evidence()` 只接受 hash/quote/offset round-trip 的 span；heading role 默认返回空；oversized paragraph 走 sentence window 或 fail closed；异常在 caller 转 typed gap | 不改 TextRoot storage；不对原文做 normalization 后再计算 offset |
| `src/novel_agent/services/evidence_first_writer_context_assembler.py` | 已新增 | 在 assembly 前 reverify every slice；dedupe by span；preview 只能来自 ledger exact text；mandatory drop 必须变 gap；budget status 与 diagnostic_codes 可解释；`assert_safe_public_payload` 覆盖 package | 不调用 `ClaimSupportGroup`、`ClaimVariant`、`ClaimSupportReceipt`；不生成事实 claim 作为 item payload |
| `src/novel_agent/services/evidence_first_checkpoint_runner.py` | 已新增 | 确认只走 frozen Need -> retrieval -> exact slice -> package；`evaluator_only_artifacts=()`；不调用 Planner model；trace_records 包含 channel/selected/excluded/gap；`future_leakage_count` 可靠 | 不重建 index；不写 Canon/World；不调用 Claim Support/whole verifier/evaluator |
| `scripts/run_evidence_first_frozen_checkpoints.py` | 已新增 | 补齐 output contract：每 case 写 package/ledger/manifest/Markdown/case_record；root before/after 比较；output_index；loopback URL 校验；read_verified；call counts zero；失败时非 0 exit | 不扫描 object store 猜最新结果；不覆盖旧 artifact；不把 embedding/rerank 计为 forbidden model call |
| `src/novel_agent/services/need_draft_grounder.py` | 已修改 | exact internal label 优先；无 internal exact 时才考虑 unique alias；多 alias / 多 internal fail closed；closure 只扫 public draft fields；audit 记录 method/source | 不 fuzzy；不因他实体 alias 同名拒绝 canonical exact label |
| `src/novel_agent/services/need_validator.py` | 已修改 | unresolved public lexical anchor 不能 drop whole Need；保留 query_text/query_hints；禁用依赖 missing id 的 exact/graph route | 不生成伪 entity id；不把 missing org 当成功 grounding |
| `src/novel_agent/services/need_query_compiler.py` | 需核对 | unresolved lexical anchor 时 BM25/dense 仍 eligible；exact/graph 无 seed 时 fail closed with reason；graph_relations 可保持空但 reason 必须可见 | 不做 broad exact/graph search |
| `src/novel_agent/services/plan_conditioned_need_planner.py` | 已修改 | target-aware WorldSummary 必须有 per-target available/selected/truncated counts；`relation_count=0` 保持真实；relation-like states 只能作为 state 展示 | 不伪造 RelationRecord；不扩大 summary 到全 World |
| `src/novel_agent/services/task_conditioned_need_generation.py` | 已修改 | `generate_evidence_first()` 只能复用 frozen Planner artifact；Need active completion 语义必须是 evidence readiness / typed gap；version/hash 变化写 manifest | 不调用 Planner endpoint；不生成标准答案 claim |
| `tests/unit/test_evidence_first_writer_context.py` | 已新增 | 必须覆盖 resolver、assembler、gap、budget、taint/future、Markdown deterministic、legacy v1 compat；随 `make quality` 通过 | 不只跑 focused 后宣告完成 |
| `tests/unit/test_plan_conditioned_planner.py` | 已补 | P004 `落落` canonical binding、P005 `国教学院` lexical anchor、query compiler channel eligibility、summary coverage 必须过 | 不硬编码 case id/chapter 通过测试 |
| `.agent/implementation.md` | 未完成本轮 §30 | 追加 `## 30. Stage 2M evidence-first Writing Package Round 1`，写 code/test/run/artifact evidence、zero-call proof、root hash comparison、typed gaps、return status | 不把旧 §24/§25 当本轮证据 |

## 15. Round 1 剩余交付清单

当前 Round 1 还不能交给 Codex review。实现者必须继续完成以下顺序：

1. Run focused tests:

   ```bash
   .conda-env/bin/pytest tests/unit/test_evidence_first_writer_context.py tests/unit/test_plan_conditioned_planner.py
   ```

2. Run full quality:

   ```bash
   make quality
   PRE_COMMIT_HOME=/tmp/ns-precommit-cache .conda-env/bin/pre-commit run --all-files
   git diff --check
   ```

3. Run offline admission replay over frozen public artifacts:

   ```text
   Grounder -> Validator -> Need -> Query -> package contract fixtures
   ```

   Required assertions:

   - P004-like `落落` binds canonical internal label despite another entity alias；
   - P005-like `国教学院` remains lexical/dense query with exact/graph fail-closed；
   - target-aware summary records per-target counts；
   - no Gold/future/private field enters Need/query/package。

4. Run real five-checkpoint package export with `scripts/run_evidence_first_frozen_checkpoints.py`：

   - source project: `/tmp/ns-stage2m-phase4-v33-apc-20260810`；
   - database: `na_s2m_phase4_v33_apc_v1`；
   - cases: P001/P002/P003/P004/P005；
   - output root must be a new `/tmp/ns-stage2m-evidence-first-*` directory；
   - OpenSearch/embedding/reranker may use existing frozen real-hybrid local services；
   - do not rebuild projection/index。

5. For each case, verify outputs:

   - `writer_context_package.json`
   - `evidence_ledger.json`
   - `package_manifest.json`
   - `writer_context_package.md`
   - `case_record.json`

6. Verify run-level output:

   - `output_index.json`
   - immutable roots before/after identical；
   - forbidden model calls zero；
   - v2 schema validation passes。

7. Append `.agent/implementation.md §30` with:

   - changed files summary；
   - test commands and exact result lines；
   - offline admission replay result；
   - real five-checkpoint command；
   - output root and per-case artifact paths；
   - package/ledger/manifest refs；
   - zero-call proof；
   - root hash comparison；
   - typed gaps；
   - final status `EVIDENCE_FIRST_WRITING_PACKAGE_COMPLETE / RETURN_TO_CODEX_REVIEW` or
     explicit `RETURN_TO_CODEX / <blocked_reason>`。

## 16. Acceptance checklist

### 16.1 Evidence-first package

- default runner does not need claim groups；
- package item has no required claim；
- raw evidence directly appears as Writer-visible item；
- every evidence item has ledger ids or typed gap；
- every ledger entry dereferences to L0 exact slice；
- no future / Gold / private leakage；
- Claim Support / whole verifier / evaluator calls are zero。

### 16.2 L0 slice

- paragraph slicing stable；
- long paragraph sentence fallback stable；
- offset round-trip pass；
- hash recomputation pass；
- oversized selected evidence rejected or reduced with gap；
- heading/title-only units filtered by source role。

### 16.3 Need / Planner

- no default claim completion wording；
- exact canonical label beats alias collision；
- unresolved lexical anchors remain searchable；
- target state coverage has per-target counts；
- missing entity / ambiguous alias / graph unavailable / budget insufficient typed gaps appear。

### 16.4 KG

- repair checkpoint has `relations > 0`；
- relation subject/object exist；
- relation predicate in registry；
- relation evidence refs legal；
- missing key entities repaired with evidence；
- alias collisions have receipts；
- no old accepted WorldRoot overwritten。

### 16.5 R1 / Anchor / L2

- projection attestation `graph_edge_count > 0` after KG repair；
- typed graph path receipts dereference；
- relation anchors appear；
- graph unavailable reason explicit；
- RRF and rerank inputs retained；
- selected candidate scope/cutoff valid；
- selected evidence expands to L0 exact slice。

### 16.6 Benchmark

- report profile is evidence-first；
- mechanical readiness separated from external semantic coverage；
- old claim-first runs historical only；
- artifacts have reproducible profile, snapshot and root identity；
- external scorer cannot write into same experiment identity。

## 17. Stop conditions

Stop and return to Codex if:

1. evidence-first package still requires claim group to be READY；
2. selected Writer evidence cannot dereference to L0 exact slice；
3. fixing package requires reading Gold/future/private data；
4. frozen text/index lacks required raw material and evidence shows World/KG replay is necessary；
5. KG repair after admission still produces `relations=0`；
6. relation extraction cannot produce legal evidence refs；
7. graph route empty result lacks unavailable/fallback reason；
8. benchmark code mixes external semantic score into default readiness；
9. satisfying the task seems to require a parallel platform, second truth store or external framework import。

## 18. Non-goals

- Do not continue improving evaluator/verifier/claim matcher as the default Stage 2M gate。
- Do not introduce Neo4j as a prerequisite。
- Do not import Microsoft GraphRAG / LightRAG / Neo4j GraphRAG code。
- Do not build community report or global graph summaries in P0/P1。
- Do not make all-channel broadcast the default。
- Do not create a large ontology without demonstrated caller。
- Do not rewrite historical reports。
- Do not give Writer raw retrieval tools。
- Do not modify Stage 3/4 implementation as part of Stage 2 repair。

## 19. Final success definition

Stage 2 repair succeeds when the system can reliably produce:

1. evidence-first `WriterContextPackage`；
2. exact L0 `EvidenceLedger`；
3. cutoff/scope/leakage safety；
4. Need/facet explainability；
5. typed gaps instead of silent drops；
6. KG relation graph with evidence-backed nonzero edges；
7. typed_graph paths with receipts；
8. L1/L2 discovery that always returns to L0；
9. benchmark readiness that is independent from claim-first evaluator；
10. frozen artifacts that a Writer, human or external strong model can read directly。

At that point Stage 3/4 may consume Stage 2 Memory as a real writing memory foundation, rather than a
claim production/evaluation loop that hides raw evidence behind failed intermediate answers.

## 20. 可执行细节补强：Repair card 格式

后续每个 repair 切片都必须能用同一种卡片验收。仅写“修 KG”“修检索”“修 package”不够。
每个切片进入实现前，必须在本文件或 `.agent/plan.md` 中明确以下字段：

- Entry state：允许读取哪些 frozen / repair artifacts，哪些 artifact 必须保持不变。
- Current caller：当前真实调用方，不允许写“未来会用”。
- Input contract：输入对象、版本、hash/basis、是否允许模型调用。
- State mutation：允许改哪些 domain record / projection / index / artifact；不允许改哪些。
- Output artifacts：必须产出的 JSON/Markdown/manifest/trace 路径和 media type。
- Ready predicate：什么叫 READY，什么叫 typed gap，什么叫 failure。
- Negative checks：哪些捷径一旦出现就 fail。
- Regression tests：unit、contract、integration、real frozen run 各覆盖什么。
- Return-to-Codex condition：approved direction 不够时如何停下，而不是顺手扩平台。

实现报告也要按这个格式返回证据。没有 entry、output、test 和 stop evidence 的 repair，
不能被 Codex 接受为“完整可用”。

## 21. Repair A-D 的可执行卡片

### 21.1 Repair A：Evidence-first WriterContextPackage

Entry state:

- ADR-0008 已接受；
- frozen C20/C40/C60/C80/C95 可读；
- old `WriterContextItem.claim` 和 `EvidenceLedgerEntry.claim_excerpt` 仍存在；
- Claim Support 旧代码可保留，但默认路径必须不调用。

Input contract:

- `BenchmarkTaskContract`
- public Need/facet metadata
- selected exact `EvidenceSlice`
- route/selection trace
- budget config
- basis commit / snapshot / cutoff

Forbidden input:

- `ClaimSupportGroup`
- `ClaimVariant`
- `ClaimSupportReceipt`
- Gold descriptor / Gold answer
- future chapter text
- same-run evaluator verdict

State mutation:

- allowed: domain writer-context models, stage2 schemas, assembler default path, runner profile,
  artifact manifest and deterministic Markdown projection；
- forbidden: Canon/World/TextRoot/index content, old artifact rewrites, Stage 3/4 code, new evaluator。

Output artifacts:

- `writer_context_package.json`
- `evidence_ledger.json`
- `package_manifest.json`
- `writer_context_package.md`
- `case_record.json`

Minimal schema rule:

- New evidence item must have `item_id`, `section`, `purpose`, `need_ids`, `facet_ids`,
  `evidence_ledger_ids`, `raw_preview`, `preview_truncated`, `validity`, `mandatory`,
  `selection_reason`。
- A non-gap item with empty `evidence_ledger_ids` is invalid。
- A gap item must have `gap_id`, `gap_code`, `need_ids`, `explanation`,
  `retriable_by_round`。
- Ledger entry must have `ledger_id`, `evidence_text`, `evidence_slices`, `source_commit`,
  `basis_snapshot_id`, `information_scope`, `need_ids`, `facet_ids`,
  `retrieval_unit_ids`, `text_hash`, `span_hash`, `dereference_receipt`。
- `claim` and `claim_excerpt` may exist only under legacy profile, not as evidence-first required
  fields。

Ready predicate:

```text
READY =
  package validates against evidence-first schema
  and all non-gap items have same-ledger refs
  and all ledger refs verified-read
  and all ledger entries L0-dereference
  and writer and ledger budgets pass
  and no default Claim/Evaluator calls
  and manifest hashes are stable
```

Negative checks:

- READY package contains generated factual claim text as the primary payload；
- raw evidence retained in ledger but absent from Writer-visible items；
- package READY depends on support receipt validation；
- Markdown contains text not derivable from JSON。

Tests:

- contract: schema validates package, ledger, manifest, readiness。
- unit: dangling ledger ref rejected；gap item accepted only with `gap_code`。
- regression: old claim package remains readable under legacy profile。
- integration: five frozen package exports validate and verified-read。

### 21.2 Repair B：L0 EvidenceSliceResolver

Entry state:

- TextRoot/TextBlock identity remains unchanged；
- `EvidenceRef` can locate source block/span；
- selected retrieval candidates may still be block-sized。

Input contract:

- parent TextBlock text；
- parent `EvidenceRef` / `TextSpanRef`；
- optional candidate span hints；
- writer preview budget；
- ledger evidence budget；
- source-role metadata when available。

State mutation:

- allowed: read-side resolver objects, selection receipts, ledger entries；
- forbidden: modifying TextRoot storage, normalizing source prose, inventing evidence spans。

Slice algorithm:

1. Verify parent block hash and basis commit。
2. If candidate span hint exists, clamp only to paragraph/sentence boundaries inside the legal parent span。
3. Prefer paragraph boundary。
4. If paragraph exceeds ledger max slice length, split contiguous sentence windows。
5. If a single sentence exceeds max, keep exact sentence only if ledger budget allows；otherwise emit
   `EVIDENCE_SLICE_TOO_LARGE` typed gap。
6. Compute `slice_id`, `text_hash`, `span_hash`, `quote_hash` from original text。
7. Store dereference receipt with parent ids and offsets。

Output object minimum:

- `slice_id`
- `source_commit`
- `snapshot_id`
- `text_root_hash`
- `parent_block_id`
- `start_offset`
- `end_offset`
- `evidence_text`
- `text_hash`
- `span_hash`
- `quote_hash`
- `source_role`
- `truncation_status`

Ready predicate:

```text
READY =
  every exposed ledger evidence has at least one exact slice
  and each slice text == parent_text[start:end]
  and hashes recompute
  and heading-only filtering is metadata-driven
```

Negative checks:

- whole chapter enters ledger as one evidence entry；
- preview truncation is mistaken for ledger evidence text；
- fixed Chinese string or case id special-casing controls slicing；
- offsets are based on normalized text rather than source text。

Tests:

- paragraph boundary；
- Chinese punctuation sentence fallback；
- English punctuation sentence fallback；
- mixed punctuation；
- heading/title-only negative control；
- overlong sentence gap；
- byte/character offset round-trip；
- deterministic id stability。

### 21.3 Repair C：Need / Planner evidence contract

Entry state:

- existing Need domain may still contain legacy claim fields；
- frozen raw Planner drafts are reused in first slice；
- World relations may still be zero。

Input contract:

- public task；
- accepted Plan visible at cutoff；
- frozen World summary；
- public raw Planner draft；
- allowed information profile。

State mutation:

- first slice allowed: Grounder semantics, query compilation, summary selection, Need validation；
- later slice allowed: Need domain defaults and schemas；
- forbidden: Planner model prompt expansion that reads Gold/future, creating World relations from
  summary-only text。

Need field rule:

- default Need may retain legacy fields for compatibility, but its active completion semantics must
  be evidence readiness。
- `require_current_claim` must be false or ignored by default evidence profile。
- `stop_condition` text must not require “one current claim”。
- every Need must have enough public text to compile at least one lexical or semantic query, or it
  becomes a typed gap。

Grounder rule:

```text
if unique internal_label exact match:
    choose canonical entity
elif no internal_label exact and unique alias exact:
    choose alias target
elif mention is public text but unresolved:
    keep lexical anchor, disable exact/graph routes needing entity id
else:
    typed gap AMBIGUOUS_ENTITY_LABEL
```

WorldSummary rule:

- target-aware state selection is read-only；
- relation-like states may be shown as states；
- `relation_count=0` stays zero until World has RelationRecord；
- per-target selected/truncated counts are mandatory manifest fields。

Ready predicate:

```text
READY =
  Need/query/package can be explained from public inputs
  and unresolved public mentions still feed lexical/dense retrieval
  and entity ambiguity is explicit
  and no claim completion contract gates package READY
```

Negative checks:

- dropping an entire Need because one mention lacks runtime entity id；
- fuzzy merging `落落/落衡` without receipt；
- converting state text into RelationRecord during first slice；
- hiding `relation_count=0` by synthetic graph edges。

Tests:

- P004-like canonical label beats alias collision；
- P005-like missing institution remains lexical query；
- multiple alias conflict fails closed；
- no-Gold query compilation；
- target-aware summary count manifest；
- legacy claim wording absent in default generated Need artifact。

### 21.4 Repair D：Evidence selection and package packing

Entry state:

- retrieval returns candidates/hits with route traces；
- resolver can produce exact slices；
- budgets are configured。

Input contract:

- validated Need/facet list；
- candidate hits grouped by channel；
- exact slices；
- route/rank/rerank trace；
- writer budget and ledger budget；
- package section mapping。

State mutation:

- allowed: package selection plan, ledger inclusion plan, readiness report；
- forbidden: rerunning retrieval with hidden query expansion from Gold, changing source hashes,
  asking a model to decide semantic support。

Selection order:

1. Remove cutoff/scope/taint invalid candidates。
2. Expand remaining selected candidates to exact L0 slices。
3. Deduplicate by `span_hash`。
4. Assign each slice to Need/facet owners。
5. Rank mandatory facets before optional。
6. Apply per-Need minimum coverage。
7. Apply source/chapter diversity cap。
8. Enforce writer preview budget。
9. Enforce ledger full-text budget。
10. Emit typed gaps for uncovered mandatory facets or budget overflow。

Budget receipt minimum:

- configured writer budget；
- rendered writer tokens；
- configured ledger budget；
- rendered ledger tokens；
- mandatory tokens；
- optional tokens；
- dropped optional ids；
- dropped reasons；
- unresolved mandatory facets；
- gap codes。

Ready predicate:

```text
READY =
  mandatory evidence fits budgets
  or every missing mandatory facet has typed gap
  and no invalid candidate survives
  and all package/ledger refs are closed
```

Negative checks:

- silent drop of mandatory evidence；
- same slice duplicated into multiple ledger entries；
- source diversity removes the only mandatory evidence without gap；
- budget code trims ledger evidence text instead of preview。

Tests:

- mandatory overflow；
- optional dropping；
- same-span dedupe；
- multi-Need shared ledger entry；
- source diversity cap；
- invalid scope exclusion；
- deterministic selection under tied scores。

## 22. Repair E-H 的可执行卡片

### 22.1 Repair E：World / KG construction and backfill

Entry state:

- Round 1/2 artifacts reviewed；
- Codex admits KG repair as a separate slice；
- new repair replay/backfill identity exists；
- old accepted WorldRoot artifacts remain immutable。

Input contract:

- TextRoot/L0 evidence units；
- current WorldRoot entities/states/events/obligations；
- predicate registry；
- alias repair policy；
- cutoff/profile/basis；
- graph extraction model config if model extraction is admitted。

State mutation:

- allowed: new repair WorldRoot/snapshot identity, candidate receipts, alias receipts, relation
  backfill receipts；
- forbidden: overwriting old accepted WorldRoot, relation without evidence, second graph truth
  store, blind regex conversion。

Batching rule:

- process by chapter or TextUnit batch with stable batch id；
- each batch records source TextRoot hash and input evidence ids；
- model extraction, if used, returns structured candidate ops only；
- validation is deterministic after candidate generation；
- failed candidates remain in audit, not silently discarded。

Candidate operation minimum:

- `candidate_id`
- `source_batch_id`
- `operation_kind`
- `subject_label`
- `subject_entity_id`
- `predicate`
- `object_label`
- `object_entity_id`
- `valid_time`
- `evidence_refs`
- `confidence_label`
- `validation_status`
- `rejection_reason`

Validation gates:

1. subject/object entity exists or has admitted evidence-backed create op；
2. predicate in registry；
3. subject/object types allowed by predicate；
4. evidence refs legal at cutoff；
5. evidence text supports relation locally；
6. truth_class allowed；
7. no duplicate active relation unless predicate multiplicity allows it；
8. alias resolution receipt attached。

Ready predicate:

```text
READY =
  repair WorldRoot validates
  and relations > 0
  and key missing entities repaired where evidence exists
  and every accepted relation has evidence refs
  and rejected candidates are auditable
```

Negative checks:

- accepting relation whose object label never appears in evidence；
- entity merge by fuzzy similarity only；
- deleting relation-like StateRecord without explicit supersession rule；
- using Neo4j as the canonical source。

Tests:

- predicate registry validation；
- missing organization create from evidence；
- alias split/merge receipts；
- duplicate relation policy；
- relation-like state candidate rejected without evidence；
- accepted relation serializes and validates in WorldRoot；
- repair replay leaves old WorldRoot hash unchanged。

### 22.2 Repair F：R1 typed_graph projection

Entry state:

- repair WorldRoot has accepted relations；
- relation evidence refs verify；
- Postgres R1 schema still uses `r1_record` and `r1_record_entity`。

Input contract:

- repair source commit；
- repair WorldRoot；
- optional PlanRoot；
- projection profile；
- allowed access scopes。

State mutation:

- allowed: R1 rows for repair source commit, projection attestation, graph path trace；
- forbidden: schema migration unless CTE/path receipt cannot satisfy benchmark, graph edges from
  non-canonical sources。

Materialization rule:

- one `WorldRecordKind.RELATION` row per accepted RelationRecord；
- `r1_record_entity` stores subject/object roles；
- `graph_edge_count` equals accepted relation rows visible to graph profile；
- zero rows is a readiness failure for graph-dependent repair, but only a typed unavailable reason
  for Round 1/2。

GraphPathReceipt minimum:

- `path_id`
- `source_commit`
- `snapshot_id`
- `seed_entity_ids`
- `relation_row_ids`
- `relation_ids`
- `entity_path`
- `predicates`
- `directions`
- `valid_time`
- `edge_semantics`
- `evidence_refs`
- `dereference_status`

Ready predicate:

```text
READY =
  graph_edge_count > 0
  and typed_graph_paths returns evidence-backed paths for known seeds
  and every path receipt dereferences to relation row and L0 evidence
```

Negative checks:

- graph route returns empty success with no unavailable reason；
- graph path includes inferred/similarity edge under canonical profile；
- row role missing subject/object；
- relation evidence missing from path receipt。

Tests:

- R1 counts relation rows；
- typed graph 1-hop and bounded 2/3-hop；
- predicate filter；
- time filter；
- access scope filter；
- zero-edge unavailable reason；
- receipt dereference。

### 22.3 Repair G：Anchor / L1

Entry state:

- AnchorBuilder exists；
- relation anchors are starved until relations exist；
- grounded block units may be coarse。

Input contract:

- WorldRoot；
- TextRoot；
- PlanRoot if visible；
- snapshot and canonical commit；
- relation rows after KG repair。

State mutation:

- allowed: rebuildable anchor units and index manifests；
- forbidden: treating anchor text as truth, adding summary-only facts。

Anchor minimum:

- entity anchors include entity id, label, aliases, type, source refs when available；
- relation anchors include relation id, predicate, subject/object ids and labels, valid_time,
  evidence refs；
- grounded anchors include parent block and EvidenceRef；
- plan anchors retain `author_planning` label and access scope。

Ready predicate:

```text
READY =
  relation anchors appear when relation rows exist
  and every anchor-derived package item returns to L0 ledger
  and anchor metadata preserves access scope
```

Negative checks:

- L1 summary text enters Writer package without ledger evidence；
- plan anchor leaks under writer_safe query；
- alias ambiguity hidden in anchor label text。

Tests:

- relation anchor creation；
- entity metadata retention；
- access scope exclusion；
- anchor-to-L0 expansion；
- no package item from L1 without ledger ref。

### 22.4 Repair H：L2 hybrid retrieval and route health

Entry state:

- OpenSearch anchor/grounded indexes exist；
- NeedQueryCompiler produces lexical/semantic/exact fields；
- typed_graph may be unavailable before Round 3 projection repair。

Input contract:

- compiled Need query bundle；
- route plan；
- channel capability manifest；
- per-channel budgets；
- access scope and cutoff。

State mutation:

- allowed: retrieval traces, index manifests for repair projection, package selection receipts；
- forbidden: all-channel broadcast default, hidden query from Gold, broad unanchored graph query。

Route health minimum:

- `eligible_channels`
- `ineligible_channels`
- `ineligible_reason`
- `channel_candidate_count`
- `channel_failure`
- `fallback_reason`
- `rrf_input_ids`
- `rerank_input_ids`
- `selected_ids`
- `excluded_ids`
- `excluded_reasons`

Ready predicate:

```text
READY =
  every channel decision is explainable
  and graph unavailable is typed
  and selected candidates are cutoff/scope valid
  and selected handles expand to exact slices
```

Negative checks:

- graph channel skipped without trace；
- no entity id causes entire Need to be dropped despite lexical query；
- broad exact search without filters；
- reranker receives excluded/private/future candidates。

Tests:

- lexical-only unresolved anchor route；
- graph seed missing route exclusion；
- zero-edge graph unavailable；
- RRF deterministic ordering；
- rerank bounded input；
- scope/cutoff exclusion；
- selected handle L0 expansion。

## 23. Repair I 与全局 artifact/readiness 细节

### 23.1 Round 1 readiness contract

Round 1 不新增独立 readiness 报告家族。每个 checkpoint 的 readiness 由
`case_record.json` + `package_manifest.json` 共同承担；run-level navigation 由
`output_index.json` 承担。

每个 `case_record.json` 至少记录：

- `case_id`
- `checkpoint`
- `contract_version`
- `source_commit`
- `basis_snapshot_id`
- `text_root_hash`
- `world_root_hash`
- `index_manifest_hash`
- `package_ref`
- `ledger_ref`
- `manifest_ref`
- `markdown_ref`
- `claim_support_call_count`
- `whole_verifier_call_count`
- `semantic_evaluator_call_count`
- `need_planner_model_call_count`
- `retrieval_call_count`
- `embedding_call_count`
- `rerank_call_count`
- `package_status`
- `gap_codes`
- `ledger_entry_count`
- `evidence_item_count`
- `dereference_failures`
- `scope_failures`
- `cutoff_failures`
- `leakage_failures`
- `budget_status`
- `root_hashes_unchanged`

READY requires all failure counts to be zero, budgets pass, and call counts for claim/verifier/evaluator
are zero. Embedding/rerank calls may be nonzero only when they are already part of the approved
real-hybrid retrieval profile.

If implementation keeps these fields split between `case_record.json` and `package_manifest.json`,
`.agent/implementation.md §30` must name the exact field locations. Do not create a second
readiness platform just to satisfy naming.

### 23.2 Five-checkpoint index

The five-point run also needs one output index:

- `run_id`
- `profile`
- `started_at`
- `completed_at`
- `source_project`
- `frozen_db`
- `cases`
- child artifact refs
- per-case readiness status
- aggregate mechanical status

The index is navigation only. It must not compute Gold HIT/MISS or semantic verdict。

### 23.3 External semantic scoring boundary

External scoring input is allowed only after package freeze and must read frozen package/ledger refs。
It may produce a separate report, but that report cannot update:

- package JSON；
- ledger JSON；
- manifest；
- readiness report；
- source artifacts；
- retrieval selection。

If an external scorer finds missing evidence, the result is a new repair input for Codex review,
not an in-place benchmark retry。

## 24. Cross-round completeness gates

每一轮进入下一轮前，需要显式通过 gate。

| From | To | Required evidence |
|---|---|---|
| Round 1 | Round 2 | 五点 evidence package 可读；L0 dereference pass；claim/verifier/evaluator calls zero；immutable roots unchanged |
| Round 2 | Round 3 | Need/query/route/readiness gaps 可解释；仍有 Writer/Planner quality blocker 指向 missing entity/relation/KG |
| Round 3 | Stage 2 repair review | repair WorldRoot relations > 0；R1 `graph_edge_count > 0`；typed graph receipts dereference；external semantic report separated |

禁止跳过：

- Round 1 未证明 product boundary，就直接重建 KG；
- Round 2 未证明问题指向 World/KG，就做 relation backfill；
- Round 3 未产生 relation rows，就调 typed_graph；
- typed graph 无 receipts，就把 graph path 放进 Writer package；
- external scorer 未隔离，就宣告 Stage 2 repair PASS。

## 25. 最小测试矩阵

| Test layer | Must cover | Example target |
|---|---|---|
| Unit | schema validators, slice resolver, grounder, packer, graph receipt | no claim required, offset round-trip, alias collision |
| Contract | JSON schemas and media types | package/ledger/manifest/readiness validate |
| Regression | known Stage 2M failures | P004 label collision, P005 unresolved institution, raw evidence not exposed |
| Integration | frozen five checkpoints | JSON/Markdown exports, zero claim calls, root hash unchanged |
| Property | deterministic ids and dedupe | same span hash, stable package order under tied scores |
| Model-required | only approved retrieval profile | embeddings/rerank as already allowed, no evaluator |
| Golden/offline | post-freeze external scoring only | semantic coverage, never default READY |

`make quality` 仍是基础工程验收，但不足以单独证明 Stage 2 repair 通过。
必须同时提供 frozen artifact evidence 和 readiness report。

## 26. 仍然缺证据时的处理规则

如果实现者发现某个 repair 还不能完整落地，必须返回可诊断证据，而不是扩大范围：

- 缺 schema owner：指出现有 domain model 和 schema 冲突，给出最小替代。
- 缺 source evidence：列出 Need/facet、query、candidate、失败的 dereference receipt。
- 缺 World entity：列出 public mention、source field、lexical query、resolver rejection。
- 缺 relation：列出 relation-like state candidate 和证据 ref 状态。
- 缺 graph path：列出 relation rows、R1 materialization counts、graph filters。
- 缺 budget：列出 mandatory/optional token、drop reason、gap code。
- 缺 model/API：列出被允许和被禁止的 call sites，不能切到 evaluator。

这些证据进入 Codex review，Codex 再决定是否修改本大修文档或 `.agent/plan.md`。
OpenCode 或实现者不得因为卡住就引入新平台、第二 truth store、Gold scorer 或未授权 KG 服务。

## 27. 2026-08-12 historical pre-authorization checkpoint

- Status at this checkpoint: `ENGINEERING_PASS / UNIFIED_REAL_GATE_PENDING_AUTHORIZATION`。
- Round 1/2/3 的最小生产机制已经落入现有 owner：Evidence-First package/readiness、canonical
  World relation repair、R1 graph materialization、typed graph path receipt、L1 anchor、L2 projection
  与 projection attestation 共用同一 repair commit/snapshot。
- 五点 Evidence-First runner 已能把指定 checkpoint 接到隔离 repair workspace 的同一 repair
  commit/project/attestation/R1/L2 basis；source commit、TextRoot、PlanRoot 任一不一致均拒绝。package
  artifacts 只写全新 output CAS，source/repair workspace 保持只读；run index 含 §23.2 所需生命周期、
  source/DB identity、child refs、per-case readiness 与 aggregate mechanical status。
- `make quality` 通过：Ruff/format、304-file strict MyPy、1840 deterministic tests、24,228
  statements + 6,936 branches 100% coverage。
- source-project isolation、candidate accounting、Stage1 independent validation、CAS identity、exact
  L0 dereference 与 graph readiness fail-closed 均有 focused regression evidence；详见
  `.agent/implementation.md §26`。
- 在该 checkpoint 尚未通过本文件 §24 的最终 cross-round gate：本轮全新 real API/real-hybrid 五点 + Round 3
  联合运行未执行。锁定服务均未监听，且执行环境拒绝在没有 human explicit approval 时启动
  persistent local infrastructure。当时状态保持 architecture-repair gate HOLD，不得用确定性测试替代
  real gate；后续授权、运行和最终 PASS 见 §28-§29。

## 28. 2026-08-12 authorized real evidence and fail-closed iterations

- 授权后锁定基础设施与模型服务已启动并健康。Round 3 最终 real-hybrid workspace 为
  `/tmp/ns-stage2m-round3-world-repair-20260812-v4`；repair commit
  `sha256:93982fbd6bdd8ecdd9442444d1cdcde4f20e1900d58dabb0fade4114f27c36c6`，包含 4 条
  relation rows、4 条 R1 graph edges、6 份 typed graph path receipts，全部 L0 verified；198 条
  R1 records、169 条 entity associations，且 exact real-hybrid projection attestation 存在。
- source C95 commit、TextRoot、WorldRoot bytes 与 source head 均未改变。模型章节 28/70/72 通过
  既有 Curator evidence catalog、support gate、semantic verifier 与 host admission；没有放宽 alias、
  truth、predicate/domain/range 或 evidence exactness。
- 真实失败驱动三项最小修复：graph structured profile 显式关闭 thinking；schema retry 保留
  `response_schema`；模型选择规则 relation-first 且禁止 standalone/重复 entity candidate。另修复
  evidence surface 只接受在 exact span 中出现、且唯一解析回 canonical entity 的 label/alias，以及
  checkpoint mechanical failure 不再误报 READY。
- 首次检查的 handoff 目录未暴露五个 paired inputs，runner 因而 fail closed。随后定位完整 CAS mirror
  `/tmp/ns-stage2m-frozen-checkpoint-repair-project-20260811-v1`，逐个验证五个 paired object hash 与五个
  top manifest；没有从旧 package 推断或重建 Need/Planner 输入。
- v4 随后的 joint attempt 又正确拒绝了 C95 commit PlanRoot 与 P005 checkpoint PlanRoot 不同 basis；
  冻结 P005 plan 经 official compiler 导出并验证内部 root
  `sha256:7c577cb3c03e2150c1705315cc138ed0a7d459e78ebaa0c3db7c9fd31914670a`。v5 使用该 checkpoint
  PlanRoot，同时继续以 source commit PlanRoot 检查 source 不变性。下一次 joint attempt 暴露 repair
  SQLite 缺 persisted `derived_snapshot` row；canonical projection publisher 补齐并 read-back 后才允许
  repair 成功。
- 最新工程基线：strict MyPy 304 files；1843 deterministic tests passed、9 deselected；24,246
  statements / 6,942 branches 100% coverage。

## 29. 2026-08-12 final cross-round acceptance

- Accepted Round 3 workspace：`/tmp/ns-stage2m-round3-world-repair-20260812-v5`；repair commit
  `sha256:b3488cd83bcae744afa4131ff6ca6d676afee841dac189bc241f56f260b5582b`，snapshot
  `snapshot.b3488cd83bcae744afa4131ff6ca6d676afee841dac189bc241f56f260b5582b`。它与 P005 exact
  checkpoint PlanRoot、冻结 TextRoot 同 basis，source C95 commit/WorldRoot/TextRoot/PlanRoot/head 均不变。
- Round 3 产物含 2 relation rows、2 typed graph edges、1 份 `l0_verified` graph-path receipt、169 R1
  records、165 entity associations、265 anchor docs、96 grounded docs；projection attestation 为
  `exact`，8 个通道全部 ready，0 failed/degraded。
- Final joint output：`/tmp/ns-stage2m-evidence-first-joint-20260812-v4/output_index.json`；aggregate
  mechanical status `PASS`。P001-P005 全部 `READY`，每个 case 的 gap、dereference、scope、cutoff、
  leakage 均为 0，root hashes unchanged；P005 明确 `joint_repair=true` 并绑定 v5 repair commit、
  snapshot、project、physical indexes、exact P005 PlanRoot 与原 C95 source checkpoint commit。
- 最终全量门禁：`make quality` PASS（strict MyPy 304 files；1843 passed、9 deselected；24,246
  statements / 6,942 branches，100% coverage）；full pre-commit PASS。
- §24 cross-round gate 结论：`PASS / STAGE2M_ARCHITECTURE_REPAIR_ACCEPTED`。ADR-0008 定义的外部
  model/human scoring 仍为 post-freeze 可选评估，不属于 Agent READY 前置条件。没有 commit、merge、push。

## 30. 2026-08-12 final Memory Write integration

§29 接受的 Round 3 能力此前只由隔离 backfill runner 调用；它证明 graph owner、admission、R1、
L1/L2 和 Evidence-First read path 正确，但不证明新章节默认会构建 graph。本节关闭该最后缺口。

正式 chapter-reveal 路径现在按以下顺序执行：

1. 同一 revealed chapter、TextRoot、base WorldRoot 和 cutoff 下，并发调用 ordinary Curator profile
   与 graph candidate profile；两个 profile 使用独立 `ModelCurator` 实例，共享同一 `ModelGateway`
   和 endpoint-global request/KV admission controller。
2. ordinary proposal 先在 host 内形成 provisional World overlay；`WorldGraphExtractionPass` 再针对
   该 provisional World 审核 canonical relation-like states 和本章 graph candidates。这样同章新建
   entity 可供 relation admission 使用，但 Canonical World 在 gate 前不变。
3. accepted entity/relation operations 与 ordinary operations 合入一个 `ObservedChangeSet`，随后只走
   一次现有 normalize、validate、risk/Guardian、atomic `CommitService` 和 full projection。没有第二
   graph truth store、第二 commit 或旁路 projection。
4. graph candidate batch 与 `WorldGraphExtractionReceipt` 作为 proposal transform lineage 持久化；
   composite Curator receipt 引用全部 ordinary/graph model calls 和最终 merged changes。
5. chapter/commit 顺序保持串行。C1 必须基于 C0 commit，C2 必须基于 C1 commit；不得为了 GPU
   batching 并行 Canon writer。可并发的是同一章内无数据依赖的 model profiles，以及既有 Need、
   evaluator batch 和 checkpoint read corridors。

工程验收：正式 scripted lifecycle 从 Genesis 连续完成 96 次 atomic commits、C20/C40/C60/C80/C95
freeze 与 projection；`make quality` 通过 strict MyPy 304 files、1847 tests（9 deselected）和 100%
statement/branch coverage。该 smoke 证明 wiring/commit/projection，不是 real-model graph quality score。

本次改动改变了 C1-C95 默认 World 构建 executable identity。因此 §29 的 frozen source 与 P005
repair 仍是有效、不可变的历史 acceptance evidence，但不能宣称“五个 checkpoint 的 World 都由新
默认路径重建”。最终产品 benchmark 的下一次运行必须从 clean Genesis 重新 reveal C1-C95，在同一
新 commit identity 下冻结五点并重跑 Evidence-First retrieval/package；不得把旧 WorldRoot 与新
package 拼成全量重建结论。
