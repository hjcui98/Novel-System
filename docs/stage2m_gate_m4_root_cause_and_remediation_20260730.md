# Stage 2M Gate M4 根因分析与修复方案

文档生命周期：`HISTORICAL_DIAGNOSTIC`
日期：2026-07-30
状态更新：2026-07-31；部分修复已进入 WP8 诊断运行，当前结论见 `docs/project_status.md`
范围：WP7；`visible_at_cutoff`（VAC）与 `author_plan_conditioned`（APC）各
P001-P005 / C20-C95；Arm A deterministic
当前决策：**Gate M4 HOLD；不得启动 WP8**

## 1. 结论

本轮失败不是模型服务、数据库、snapshot/index、profile 隔离或 token 硬预算失败。
基础设施和安全契约已经成立，真正阻塞 Gate M4 的是：

1. **F-EVIDENCE / F-ASSEMBLY：**119 个 Gold 中有 22 个
   `UNTRACEABLE`。其中 13 个在最终 Evidence Ledger 中没有命中任何 accepted
   evidence ref，9 个只命中 accepted evidence set 的一部分。Writer Context
   能表达相近结论，但 claim 没有携带完整、合法、截止点安全的支持链。
2. **VAC 是多段损失叠加，而不是单一 top-k 问题：**
   - 关键 Need 在全局 48-call 顺序预算下被饿死；
   - 合法证据已进入候选池时，rerank/selection 仍大量丢失；
   - 已选证据进入 Writer Context 前又被 4000-token packer 大量淘汰；
   - Assembly 把“某 mandatory Need 的第一个合法 claim”误当成该 Need 已闭环，
     没有验证多事实、多证据的完整性。
3. **APC P002/C40 的 typed failure 已精确定位：**
   `EVIDENCE_INSUFFICIENT` 不是 writer 或 ledger 超预算，而是
   `attitude_toward_event` 两条同证据、同语义但不同枚举文本的 current-record
   冲突触发 fail-closed。
4. **Evaluator 还有一个不改变 fail-closed 结论、但妨碍诊断的缺口：**
   semantic verifier 可在 `traceable_context_item_ids=[]` 时返回
   `traceable_claims_support=SUPPORTS`；当前 host 未拒绝这种自相矛盾的 receipt。

因此不能通过放宽 accepted evidence、放宽整章重叠、增大 Writer token、盲目增大
top-k，或把 typed failure 改成 READY 来过门。修复必须先建立公开 completion contract
和可信 support proof，不能让运行时读取私有 Gold component。正确顺序是：

> 诊断字段与 evaluator host 校验
> → 冻结 gate_metric_formula.v1 与自包含 Gold metric contract
> → APC canonical alias 修复
> → public NeedFacet / NeedCompletionSpec
> → 公平预算调度和 typed budget trace
> → 基于 public facet 的 support group / closure
> → Controller support-aware selection
> → deterministic Assembly validation/packing
> → 同配置重跑 WP7

## 2. 判定依据与复核方法

### 2.1 权威输入

- 最新 WP7 验收：
  `reports/stage2m/wp7_five_checkpoint_deterministic_acceptance_20260730.md`
- Gate M4 定义：
  `docs/stage2_memory_benchmark_task_closure_execution.md` 第 14.7 节
- 两个 WP7 run：
  - `reports/stage2m/writer_context_benchmark/visible_at_cutoff/qwen36_wp7_v1_20260730/`
  - `reports/stage2m/writer_context_benchmark/author_plan_conditioned/qwen36_wp7_v1_20260730/`
- 两个只读 C95 项目的 content-addressed frozen artifacts：
  - `.../visible_at_cutoff/qwen36_real_a_profile_v1_20260729/objects/`
  - `.../author_plan_conditioned/qwen36_real_a_profile_v1_20260729/objects/`

本分析以最新 `qwen36_wp7_v1_20260730` 为准，不用更早 run 的粗粒度指标替代新的
per-Gold 结果。

### 2.2 只读分层复核

对十个 frozen deterministic artifact，按
`gold_evidence_matcher.v3` 的同一身份/`object_hash + precise span overlap`
规则，检查 accepted evidence 在四层是否存在：

1. 全部 route candidates；
2. per-Need rank-selected candidates；
3. Stage 1 selected Context；
4. 最终 Writer Context Evidence Ledger。

该分层统计是根因诊断，不是新增 Gate 分数，也没有把 Gold 暴露给生产 retrieval。
accepted alternative 仍按“一个 alternative 内所有 refs 必须完整解析”的正式语义处理。

### 2.3 关键实现证据

| 机制 | 当前实现位置 |
|---|---|
| tool-call 耗尽后返回空结果 | `src/novel_agent/services/paired_controller.py:46-63` |
| Need 按输入顺序串行消费全局预算 | `src/novel_agent/services/paired_controller.py:257-281` |
| route stop 由 selected 是否非空决定 | `src/novel_agent/services/paired_controller.py:416-440` |
| Need 先按 mandatory/priority 排序再截断到上限 | `src/novel_agent/services/task_conditioned_need_generation.py:692-716` |
| optional claim 逐项填充 writer/ledger budget | `src/novel_agent/services/writer_context_assembler.py:159-179` |
| 第一个合法 claim 关闭 mandatory Need | `src/novel_agent/services/writer_context_assembler.py:525-544` |
| unresolved current conflict 触发 typed failure | `src/novel_agent/services/writer_context_assembler.py:200-210` |
| accepted evidence alternative 必须完整解析 | `src/novel_agent/services/gold_evidence_matching.py:33-92` |
| semantic alias 仍比较 presentation tail | `src/novel_agent/services/retrieval_unit_normalizer.py:143-174` |
| model traceable judgment 未做空 ID host 校验 | `src/novel_agent/services/memory_benchmark_evaluation.py:372-404` |
| public `Stage1MemoryNeed` 目前只有字符串 completion criteria | `src/novel_agent/domain/memory.py:148-176` |
| Gold component 只存在 benchmark/evaluator domain | `src/novel_agent/domain/benchmark.py:200`、`src/novel_agent/domain/memory_benchmark.py:75` |
| `PerGoldComparison` 尚不携带 Gold 类型、适用 profile 或 contract identity | `src/novel_agent/domain/memory_benchmark.py:236-246` |
| 现有 `ContextAssemblySpec` 只含 selected/mandatory unit 与 token budget | `src/novel_agent/domain/stage2.py:807-817` |
| Controller 是 `ContextAssemblySpec` 的现有生产者 | `src/novel_agent/runtime/memory_controller.py:866-871` |

## 3. 已确认通过的契约

| 项目 | 结果 |
|---|---|
| 两个 profile 各五点完成 | 100% |
| future leakage | 0 |
| profile cross-contamination | 0 |
| snapshot/index/basis attestation | 10/10 exact |
| READY 或 typed failure | 9 READY + 1 EVIDENCE_INSUFFICIENT |
| READY Writer Context 超 4000 tokens | 0 |
| silent overflow / silent fallback | 0 |
| canonical writes during evaluation | 0 |

这些通过项应保持为回归不变量，不能用内容质量修复破坏。

## 4. 失败事实

### 4.1 正式 per-Gold 结果

| Profile | Gold | HIT | PARTIAL | MISS | UNTRACEABLE | Weighted coverage | Mandatory HIT |
|---|---:|---:|---:|---:|---:|---:|---:|
| APC | 72 | 11 | 4 | 45 | 12 | 0.0807 | 0.1774 |
| VAC | 47 | 0 | 4 | 33 | 10 | 0.0294 | 0 |
| 合计 | 119 | 11 | 8 | 78 | 22 | - | - |

Gate M4 要求 trace completeness = 100%，因此 22 个 `UNTRACEABLE`
本身已足以判定 FAIL。VAC 五点无一个 HIT、mandatory HIT 为 0，又说明问题远超
trace 指标本身。

### 4.2 VAC accepted evidence 的层间损失

| Checkpoint | Gold 数 | Candidate 完整 set | Rank-selected 完整 set | Writer Ledger 完整 set |
|---|---:|---:|---:|---:|
| C20 | 8 | 5 | 3 | 0 |
| C40 | 9 | 7 | 6 | 1 |
| C60 | 9 | 0 | 0 | 0 |
| C80 | 10 | 5 | 0 | 0 |
| C95 | 11 | 1 | 0 | 0 |
| 合计 | 47 | 18 | 9 | 1 |

含义：

- 29/47 Gold 在全候选池中已经没有完整 accepted evidence set，存在
  F-NEED / F-ROUTE / F-RETRIEVE；
- 18 个 candidate-complete 到 rank-selected 只剩 9 个，存在明确 F-RANK；
- rank-selected 的 9 个到 Writer Ledger 只剩 1 个，存在明确 F-ASSEMBLY；
- 唯一 ledger-complete 项仍只得到 `PARTIAL`，说明证据齐全不等于结论表达完整。

按 checkpoint 展开 accepted alternative 中的 evidence refs，可看到同样的漏斗
（该行只用于观察层间损失，不作为 Gate 分数）：

| Checkpoint | Candidate refs | Rank-selected refs | Writer Ledger refs | 展开 refs |
|---|---:|---:|---:|---:|
| C20 | 13 | 10 | 3 | 21 |
| C40 | 22 | 21 | 15 | 24 |
| C60 | 9 | 1 | 1 | 24 |
| C80 | 20 | 9 | 8 | 28 |
| C95 | 18 | 10 | 6 | 38 |

### 4.3 VAC Assembly 的预算形态

| Checkpoint | Writer tokens | Mandatory conclusion tokens | Optional conclusion tokens | 因 writer budget 被丢弃的 optional items |
|---|---:|---:|---:|---:|
| C20 | 3998 | 135 | 3903 | 170 |
| C40 | 3991 | 154 | 3877 | 340 |
| C60 | 3997 | 154 | 3883 | 280 |
| C80 | 3996 | 154 | 3882 | 304 |
| C95 | 4000 | 154 | 3886 | 327 |

所有 VAC package 都“刚好填满”4000 tokens，但 mandatory 部分只有
135-154 tokens。这个形态不代表 mandatory evidence 已闭环；它反而证明当前
mandatory 标记过早关闭，后续真正有用的支持证据被当作 optional 大量淘汰。

## 5. 根因分析

### 5.1 F-EVIDENCE：claim 与 accepted evidence set 没有闭环

22 个 `UNTRACEABLE` 可进一步分为：

| 类型 | 数量 | 含义 |
|---|---:|---|
| Writer Ledger 对 accepted evidence 命中 0 个 ref | 13 | 结论来自别的 span/structured record，或 semantic verifier 找到相近表达，但合法来源完全没有进入 ledger |
| Writer Ledger 只命中 accepted set 的一部分 | 9 | 多章/多事实结论缺少支持链的一段或多段 |

代表例：

- VAC C40 `P002-G01` 需要 C32+C34+C37 的完整关系证据，最终只保留 C34+C37；
- VAC C40 `P002-G03` 需要 C35+C36+C39，最终只保留 C36+C39；
- VAC C40 `P002-G06` 需要 C26+C27+C39，最终只保留 C26+C39；
- VAC C95 `P005-G09` 需要 C24+C26+C39+C92，最终只保留 C26+C39；
- APC C20 多个 Gold 的 semantic receipt 判断结论存在，但 ledger 对 accepted
  prelude/章节证据命中为 0。

代码中的 matcher 行为是正确的 fail-closed：一个 accepted alternative 内任一
evidence ref 未解析，整个 alternative 不算 matched；`object_hash` 相同时仍要求
精确 span。这个规则不得放宽。

真正的问题在 matcher 之前：

1. durable claim 的 R1/Anchor 记录只携带局部或最新 supporting evidence，
   没有保留形成复合结论所需的完整 evidence group；
2. grounded extraction 以单 Need、单 unit 生成 claim/ledger entry，缺少
   “一个多子句 claim 对应多条证据”的一等模型；
3. Assembly 按 item 选取，不按 claim support group 的闭包选取，允许 claim
   留下而其完整证据链未被选入。

### 5.2 F-ASSEMBLY：第一个 claim 被误当成 mandatory Need 已完成

`WriterContextAssembler._claims()` 使用
`satisfied_mandatory_need_ids`。某 mandatory Need 遇到第一个合法 unit 时，就把
该 Need 加入 satisfied set；同一 Need 的其余 claim 全部变成 optional。

这个机制只保证“至少有一条有证据的 claim”，不保证：

- Need 的所有 public NeedFacet 已覆盖；
- multi-hop / relationship / capability boundary 已闭环；
- supporting evidence group 完整；
- 当前结论、历史原因和知识边界同时存在。

随后 optional packer 只按单 item 的边际顺序填到 4000 tokens，VAC 每个 case
丢弃 170-340 个 optional item。结果就是“Context 很满，但 mandatory Gold 为 0
HIT”。

这也是不能简单增加 top-k 的原因：更多 candidates 会制造更多 optional items，
但不会修复 mandatory closure，反而加剧 packing 竞争。

### 5.3 F-NEED / F-RETRIEVE：Need 饱和且停止条件过弱

VAC 的 retrieval traces 显示：

- C60/C80/C95 都达到 32 个 Need 上限；
- C40-C95 都达到 48 次 deterministic tool call 上限；
- C80/C95 的 `knowledge`、`callback`、三个 obligation Need 以及 relation
  Need 全部出现各 channel count = 0、channel failure = 0；
- 同时更早执行的 9 个宽泛 primary-entity Need 各自拥有约 54-61 个 candidates。

因此后段 Need 不是“索引查过但没找到”，而是**顺序执行时全局预算已经耗尽**。
当前 `_BudgetedBackend.search()` 在预算耗尽后只返回空 tuple，runner 仍继续遍历
后续 Need；这些 Need 最后被记成 `FALLBACK_EXHAUSTED` 或
`CANDIDATES_EXHAUSTED`，没有得到精确的 `NOT_EXECUTED_BUDGET_EXHAUSTED`
状态。

此外，Need 的 completion contract 普遍是“一条 current claim + minimal legal
evidence set”。对 relationship、knowledge boundary、long-range callback、
capability history 这类复合 Need，这个 closure 明显过弱。

### 5.4 F-ROUTE：精确路由无结果时缺少注册过的补救路径

`CURRENT_STATE` 有 canonical entity id 时被固定路由到 R1 exact/temporal。
C80/C95 多个 secondary entity state Need 得到 0 candidate 后直接结束，没有进入
任何已注册的 semantic-history fallback。

不能在运行时静默扩大 channel；正确做法二选一：

1. Need Generator 对没有 current state record、但对任务重要的 secondary entity
   同时生成独立的 semantic-history Need；或
2. 在版本化 RouteProfile 中显式注册
   `exact_current_record_absent -> bounded semantic evidence` fallback，并在 trace
   中保留 route attestation。

relation-chain 的 typed graph/anchor 路由也需要单独验证，但 C80/C95 的相关 trace
没有执行任何 channel，当前首先是预算调度问题，不能先归罪于索引。

### 5.5 F-RANK：候选存在，但 top-20/重排没有保护 evidence closure

C80 有 5/10 Gold 在 candidate pool 中拥有完整 accepted set，C95 有 1/11；
经过 rank selection 后两者都变为 0。

当前 reranker 对每个 Need 独立排序，selection 关注单 candidate 相关性，没有：

- 同一 Need 内的 evidence-source diversity；
- multi-hop support group 完整性；
- 章节/时间跨度的边际覆盖；
- 实际生成 Need 的公平覆盖；
- mandatory gap 的最小保留槽。

因此一个宽泛 query 的多个近义 current-state candidates 可以占满 top-20，而能补齐
因果链或长程证据的低频 candidate 被淘汰。

### 5.6 APC P002/C40：同证据语义别名被误判为 current conflict

P002/C40 的真实 budget 状态：

| 项目 | 值 |
|---|---:|
| Writer | 3995 / 4000 |
| Mandatory conclusions | 1177 |
| Evidence Ledger | 8050 / 12000 |
| Conflict gaps | 1 |

唯一 conflict：

```text
conflicting current records for
state_anchor|('entity.chen-changsheng',)|attitude_toward_event
```

两条值为：

```text
indifferent_to_ivy_feast
indifferent_to_fame_from_ivy_feast
```

它们引用完全相同的 C40 两个 evidence spans，Anchor 文本也相同。Normalizer
本来支持 semantic alias，但当前 alias 判定还要求解析后的 text tail 一致：
Anchor 带证据摘要，R1 unit 不带摘要，导致相同语义/相同证据的记录被判成冲突。

因此 C40 的直接修复是 canonical value 归一化和 normalizer alias 修正，而不是：

- 增加 4000-token budget；
- 增加 12000-token ledger budget；
- 忽略 conflict；
- 强制把 typed failure 改成 READY。

修复 conflict 后，C40 只能进入正常 per-Gold 评测；不能据此预判 C40 已达到质量
目标。当前历史 Gold 的 Writer Ledger 仍有明显 evidence set 丢失。

### 5.7 F-EVAL：receipt 缺少 host-side 一致性校验

Model verifier prompt 要求 `traceable_claims_support` 只考虑明确列出的
`traceable_context_item_ids`，但真实 receipt 中出现：

```text
traceable_context_item_ids = []
traceable_claims_support = SUPPORTS
```

当前 evaluator 对 model judgment 直接取值。由于 HIT 仍额外要求
`evidence.matched=true`，这个缺口没有把无证据结果升级成 HIT，Gate 仍然安全；
但它会产生自相矛盾的 receipt，并使 `UNTRACEABLE` 无法定位到具体 semantic claim。

这应作为 evaluator hardening 修复，不能替代 Writer Context 主链修复。

## 6. 目标设计

### 6.1 先建立 public NeedFacet / NeedCompletionSpec

运行时不得复用 `GoldItem.target_components` 或
`EvidenceSet.component_ids`。这些字段属于 freeze 后 evaluator-only 数据。新增的
机器可判定 completion contract 必须是独立 public 类型：

```text
NeedFacet
  need_facet_id
  need_id
  facet_kind
  expected_claim_scope
  derivation_refs[]           # Task / legal Plan / Focus
  producer
  producer_version
  information_scope

NeedCompletionSpec
  need_id
  required_need_facet_ids[]
  irreducible_need_facet_ids[]
  evidence_requirement_by_facet{}
  min_distinct_evidence_sources
  min_distinct_chapters
  require_current_claim
  require_causal_history
  uncertainty_policy
  gap_policy
  producer
  producer_version
```

边界：

1. facet 只能由 public Task、该 profile 合法可见的 Plan、TaskFocus 和 cutoff-safe
   Canon 派生；
2. VAC 的生成进程不能反序列化任何 Plan/Gold component；
3. `completion_criteria: str` 保留为人读解释，不再作为机器 closure 依据；
4. runtime 统一使用 `need_facet_id`，不得出现 Gold component identity；
5. freeze 后 evaluator 才能执行
   `Gold component ↔ NeedFacet / frozen claim` 匹配；
6. `NeedCompletionSpec` 和 derivation refs 必须进入 configuration fingerprint。

必须添加物理 taint 测试：runtime domain、Need Generator、Controller、Retrieval、
Assembler 不得导入或持有 evaluator-only component 类型/ID；序列化的 frozen
production artifact 中也不得出现 Gold component。

### 6.2 ClaimSupportGroup 必须携带可信支持证明

在 Controller selection 之前增加一等支持组，但其身份只引用 public Need facet：

```text
ClaimSupportGroup
  support_group_id
  claim_id
  need_ids[]
  need_facet_ids[]
  retrieval_unit_ids[]
  evidence_refs[]
  plan_node_ids[]
  evidence_resolution_status
  semantic_support_status
  support_receipt_ref
  producer
  producer_version
  counter_evidence_refs[]
  cutoff_attestation_ref
```

职责规则：

1. `evidence_resolution_status` 只说明引用可在指定
   commit/snapshot/cutoff 解析，不等于语义支持；
2. `semantic_support_status` 只能由版本化、受信 support producer 根据
   `support_receipt_ref` 给出；
3. CanonicalStatement 可携带写侧已经验证的 support group 和 receipt；
4. grounded 单 span 默认只生成忠实的窄 claim，不自动提升成复合结论；
5. 多 span 复合 claim 必须有联合 entailment/support receipt，并记录反证；
6. 缺 receipt、receipt basis 不一致或存在未解决 counter-evidence 时不得成为
   deterministic READY claim；
7. Assembler 只验证 receipt、basis、scope、完整性和原子打包，不能自行推断多条
   span 联合蕴含某个结论。

support producer 可以是受信的 Canon write-side validator 或单独的冻结前
support-verification service；无论哪种，都必须版本化、可回放并受 public/private
taint 边界约束。不能让 evaluator 的 Gold-aware semantic verifier 充当运行时
producer。

### 6.3 用 public completion spec 判断 mandatory closure

替换“第一个 unit 满足 Need”的逻辑：

```text
UNSEEN
  -> FACET_CLAIM_FOUND
  -> EVIDENCE_RESOLVED
  -> SEMANTIC_SUPPORT_VERIFIED
  -> REQUIRED_FACETS_CLOSED
  -> SELECTED
  -> ASSEMBLED_AND_BUDGET_VERIFIED
```

只有 `NeedCompletionSpec.required_need_facet_ids` 全部由可信 support group 闭合，
mandatory Need 才能从 pending set 移除。这个过程完全不读取 Gold accepted set。

若 public mandatory facets 无法闭合：

- Controller 可在剩余预算内继续执行合法 route；
- 无法继续时输出 typed `EVIDENCE_INSUFFICIENT` 和未闭合 public facet；
- 不得生成表面 READY、实际 mandatory 内容为空的 Context；
- 也不得把所有 Need 粗暴标为 mandatory、借 typed failure 掩盖质量退化。

每类 Need 必须有公开、版本化的 completion spec。例如：

| Need 类别 | 公开完成条件示例 |
|---|---|
| current state | current facet + 至少一个 cutoff-current source；冲突为 0 |
| relationship/emotion | relation-state facet；若声明变化原因则另需 causal facet |
| capability boundary | usable/unusable facet + limitation facet；二者不可缩减 |
| knowledge boundary | knows/does-not-know facet；uncertain 只能输出 gap |
| long-range callback | setup facet + unresolved-status facet；可要求跨章节 |
| author-plan obligation | 合法 Plan node facet；仅 APC 生成 |
| observed unresolved obligation | 正文中的 commitment facet + cutoff 时 unresolved-status facet；APC/VAC 均可生成 |

### 6.4 对实际 Need 做 max-min 公平调度

把顺序耗尽改为 deterministic max-min/deficit round-robin，但不为固定 section
预留空槽：

1. 实际生成、profile 合法的 mandatory/high-risk Need 各获得一次 primary route
   allocation；
2. profile 不适用或没有 Need 的 section 不占预算；
3. 第二轮按未闭合 public facet、risk、priority 和预计信息增益分配；
4. 单一宽泛 Need 不得在其他 mandatory/high-risk Need 未执行前消费第二轮；
5. fallback 只能使用 RouteProfile 预注册的 channel 和预算；
6. 预算不足时，trace 明确为 `NOT_EXECUTED_BUDGET_EXHAUSTED`，不能伪装成真实
   `CANDIDATES_EXHAUSTED`。

这不要求提高 48-call 上限；先证明同一预算下分配正确，再做独立预算消融。

### 6.5 原子化 Need 与注册式 route fallback

- 将宽泛 primary-entity Need 拆成公开、可验证的 NeedFacet，避免一个 query
  同时包含数十个 state/value tokens；
- 给 relationship、knowledge、callback、capability 等 Need 定义不同的 public
  evidence requirement；
- secondary entity 的 exact current state 为空时，走显式版本化 fallback 或生成
  paired semantic-history Need；
- route stop 不再用“selected candidate 非空”代表 `EXACT_SATISFIED`，而用
  NeedCompletionSpec closure；
- 保持 VAC 无 Plan 输入，所有 fallback 仍受 writer-safe scope 和 cutoff
  attestation 约束。

### 6.6 Controller 负责 support-aware selection

ranking/selection 的所有“价值”均指 public Need facet，不指 Gold component。
Controller 的候选评分至少考虑：

- NeedFacet relevance；
- evidence resolution 与可信 semantic support 状态；
- 新增 public facet / evidence source / chapter 的 marginal gain；
- freshness、supersession 和 counter-evidence；
- duplicate/near-duplicate penalty；
- writer/ledger cost estimate。

Controller 使用有界 set-cover 或分层配额形成选择，并输出扩展后的
`ContextAssemblySpec`。任何可能进入 Writer Context 的文本形态，必须先成为独立、
可寻址且已验证的 claim variant：

```text
ClaimVariant
  claim_variant_id
  claim_id
  support_group_id
  claim_text
  claim_text_hash
  covered_need_facet_ids[]
  support_receipt_ref
  token_cost
  reduction_level
  producer
  producer_version
```

每个 variant 的 `claim_text → covered_need_facet_ids → ClaimSupportGroup` 关系必须由
可信 support producer 独立验证并绑定 receipt。Controller 只能选择 receipt 合法的
variant，并在 spec 中给出每个 support group 可用 variant 的确定性优先顺序；不得把
自由文本留给 Assembler 临时摘要或压缩。

```text
ContextAssemblySpec
  selected_support_group_ids[]
  mandatory_support_group_ids[]
  allowed_claim_variant_ids_by_support_group{}
  mandatory_claim_variant_ids[]
  closed_need_facet_ids[]
  unresolved_need_facet_ids[]
  ordered_optional_support_group_ids[]
  writer_token_budget
  evidence_ledger_token_budget
  reduction_policy
  selection_policy_version
```

选择顺序：

1. 先闭合 mandatory public facets；
2. 再覆盖 optional NeedFacet；
3. 最后才添加同 Need 的近义冗余；
4. 每 Need candidate 上限仍为 20，除非独立消融证明需要调整。

### 6.7 Assembler 只做确定性验证与打包

数据流固定为：

```text
Controller
  -> support-aware selection
  -> ContextAssemblySpec
  -> deterministic Assembler validation/packing
  -> WriterContextPackage + EvidenceLedger
```

Writer 4000 tokens 与 Evidence Ledger 12000 tokens 保持不变。Assembler 只能：

1. 校验 support receipt、basis、scope、cutoff 和 counter-evidence 状态；
2. 按 `ContextAssemblySpec` 原子装入 mandatory support groups；
3. 仅从 spec 列出的、receipt 合法的 claim variants 中按固定优先级选择，并按
   optional 顺序做确定性 packing；
4. 去重 ledger 中的重复 evidence payload，但保留所有 support edges；
5. 校验 variant ID、content hash、token cost 和 receipt 引用一致；
6. mandatory group 无合法 variant、超预算或 receipt 不合法时返回准确 typed
   failure。

Assembler 不得自行改写/摘要 claim、判断自由文本是否覆盖 NeedFacet、根据“新增
facet 价值”重新排序或创造 support group。若未来确实要让 WriterContextAssembler
兼任语义选择器，必须先用 ADR 明确偏离
Controller Selection / ContextAssemblySpec / deterministic Compiler 的总架构。

### 6.8 安全修复 APC current-record alias

相同 evidence 不能证明两个解释等义；presentation tail 也不能简单从冲突判定中删除。
自动合并只允许：

1. 两条记录具有相同 `canonical_value_id` 和 `canonicalizer_version`；或
2. 版本化 alias registry 对同一 predicate 明确声明两个 canonical enum value
   等价，并生成 `canonical_alias_receipt_ref`。

本次
`indifferent_to_ivy_feast ↔ indifferent_to_fame_from_ivy_feast`
可作为 predicate-scoped 的通用 registry entry，不能写成 P002/C40 case-specific
分支。没有 canonical receipt 时继续保留 conflict 并 fail-closed。

长期在 memory write/curation 侧：

- 将 `attitude_toward_event` 归一到版本化 canonical enum；
- 同一 `(entity, predicate, timepoint)` 不得写入两个未映射的 current values；
- 原始表述、presentation tail 和全部 evidence 只作为 provenance 保留。

测试必须覆盖“相同 evidence、真正矛盾的 value”仍触发
`UNRESOLVED_CURRENT_RECORD_CONFLICT`。

### 6.9 Evaluator host-side 加固

receipt schema 统一使用领域对象名 `traceable_context_item_ids`：

1. verifier 返回 `all_context_item_ids[]` 与 `traceable_context_item_ids[]`；
2. host 验证 IDs 必须是 frozen package 的子集；
3. `traceable_context_item_ids=[]` 时强制
   `traceable_claims_support=NONE`；
4. traceable context IDs 必须引用 matcher 解析出的 ledger IDs；
5. receipt 违规时 typed fail-closed，不接受模型自行声明；
6. per-Gold explanation 同时输出：
   semantic claim、matched support group、missing Gold component；
7. Gold component 与 public NeedFacet 的映射只存在于 freeze 后 evaluator
   artifact，不回流 production artifact。

prompt、receipt domain、host validator、序列化 schema 和测试必须在同一个 schema
version 中使用该字段名；任何其他 traceable-ID 别名不得被静默兼容或忽略。

## 7. 实施顺序

| 顺序 | 工作项 | 主责任层 | 完成信号 |
|---|---|---|---|
| P0-1 | 诊断字段、stage-loss 基线和 evaluator receipt host 校验 | F-EVAL / observability | 空 traceable IDs 不可声明 SUPPORTS；修改前基线可复现 |
| P0-2 | 冻结 `gate_metric_formula.v1`、GoldMetricDescriptor 与聚合器 | Gate M4 | 公式/hash/分母 fixture 固定；聚合不读取工作区 YAML |
| P0-3 | APC canonical value + 版本化 alias registry | F-FRESHNESS / F-EVIDENCE | 有 receipt 的 alias 合并；无 receipt/真冲突继续 fail-closed |
| P0-4 | public NeedFacet / NeedCompletionSpec 与 taint boundary | F-NEED / F-SCOPE | runtime 不导入 Gold component；closure 可由 public spec 判定 |
| P1-1 | max-min call scheduler 与 typed budget trace | F-NEED / F-ROUTE | 实际 mandatory/high-risk Need 获得 primary allocation |
| P1-2 | ClaimSupportGroup、可信 support receipt 与 closure | F-EVIDENCE | 多 span claim 无 receipt 不可 READY |
| P1-3 | Controller support-aware selection、ClaimVariant / ContextAssemblySpec | F-RANK | 只输出已验证 variant；candidate-complete support 不再系统性归零 |
| P1-4 | deterministic Assembler validation/packing | F-ASSEMBLY | 只选 spec 中合法 variant 并原子打包；不执行语义判断 |
| P2 | APC C40、VAC C60/C80/C95 小点验证 | 全链路 | 每次算法变化按冻结公式产生可信前后对照 |
| P3 | 同版本双 profile 五点 WP7 正式重跑 | Gate M4 | 达到第 9 节全部准入条件 |

不把所有修复一次混入一个无法消融的大版本。诊断和 evaluator 校验先行，确保后续
每个工作项都能输出可信的 candidate、selected、assembled、ledger 四层前后对照。
在任何 alias、retrieval、selection 或 packing 行为变化之前，必须完成 P0-2；
后续实验不得移动公式、Gold contract 或分母 fixture。

## 8. 测试与诊断要求

### 8.1 单元/契约测试

必须新增：

1. runtime 模块不能导入、反序列化或输出 Gold component；
2. VAC NeedFacet 不能由 Plan/Gold 派生；
3. NeedCompletionSpec 的 mandatory/irreducible facet closure；
4. global 48-call budget 下，实际 mandatory/high-risk Need 的 max-min 首轮；
5. profile 不适用/无 Need 的 section 不占调用槽；
6. budget 未执行与真实 empty result 的状态区分；
7. exact current-state miss 的注册 fallback；
8. evidence resolution 与 semantic support 状态不可互相替代；
9. 多 span 复合 claim 无可信 receipt 时不 READY；
10. Controller 只输出 receipt-bound ClaimVariant 和完整 ContextAssemblySpec；
11. Assembler 拒绝未列入 spec、content hash 改变或 receipt 不匹配的 variant；
12. support group 原子 packing 和 variant 降级后 evidence/support edge 不丢失；
13. 相同 canonical value/version 或 alias receipt 的合并；
14. 相同 evidence、真正矛盾的值仍返回
    `UNRESOLVED_CURRENT_RECORD_CONFLICT`；
15. receipt schema 只接受 `traceable_context_item_ids`，空 IDs 校验失败，旧字段
    不得被静默接受；
16. `gate_metric_formula.v1` 的权重、PARTIAL、typed failure、profile 和 N/A
    分母测试；
17. GoldMetricDescriptor hash、accepted evidence contract 和 evaluator manifest
    identity 不完整或漂移时拒绝聚合；
18. aggregator 在移除/改写工作区 Gold YAML 后仍能仅凭冻结 evaluator bundle
    复算相同结果。

### 8.2 冻结产物诊断字段

每个 checkpoint 追加：

```text
need_generation_status
unexpanded_focus_ids
need_completion_spec_version
required/irreducible_need_facet_ids
need_execution_status
calls_allocated_by_need
mandatory_need_facets_total/closed
evidence_resolution_status
semantic_support_status
support_receipt_refs
selected_claim_variant_ids
context_assembly_spec_ref
accepted-evidence diagnostic（仅 evaluator side）
gold_metric_descriptor_ref/hash（仅 evaluator side）
candidate -> selected -> assembled -> ledger loss
dropped_support_group_ids
typed_failure_diagnostic_codes
gate_metric_formula_version（仅 evaluator/unified report）
```

生产 frozen artifact 不得包含 private Gold；accepted-evidence stage loss 只能在
freeze/reveal 后的 evaluator artifact 中生成。

### 8.3 小步真实验证

1. APC P002/C40：确认 conflict 修复且无预算/证据 silent fallback；
2. VAC P004/C80、P005/C95：确认后段 critical Need 得到真实 channel calls；
3. VAC P003/C60：确认 prelude/long-range evidence 的 candidate recall 修复；
4. VAC P002/C40：确认 candidate→rank→ledger 的完整 set 不再从 7→6→1；
5. P001/P003 人工复核 Context 是否可直接交给 Writer；
6. 才执行两个 profile 的正式五点重跑。

## 9. Gate M4 重新准入标准

### 9.1 工程与安全硬门

沿用既有 Gate M4，不降低标准：

- 两个 profile P001-P005 完成率分别 100%；
- future leakage = 0；
- profile cross-contamination = 0；
- basis/snapshot/index attestation = 100%；
- READY 或明确 typed failure = 100%；
- silent overflow / silent fallback = 0；
- Gold → ContextItem → Evidence → Commit 可追溯率 = 100%；
- current-state accuracy >= 95%；
- operational/plan coverage >= 95%；
- key historical evidence recall >= 90%；
- trace completeness = 100%。

### 9.2 `gate_metric_formula.v1`

P0-1 基线固定后、任何算法行为变化前，必须把以下公式实现到 aggregator，并把
`gate_metric_formula_version`、formula content hash 写入 experiment manifest 和
unified report。未知版本或 hash 不一致必须 fail-closed。

#### 9.2.1 输入集合与适用性

现有 `PerGoldComparison` 不是自包含的 Gate 输入。新增 evaluator-only、
content-addressed 描述：

```text
GoldMetricDescriptor
  descriptor_version
  gold_id
  gold_contract_ref
  gold_contract_hash
  gold_type
  gold_kind
  weight
  mandatory
  applicable_profiles[]
  accepted_evidence_contract_ref
  accepted_evidence_contract_hash
  evaluator_manifest_id
  evaluator_manifest_hash
```

`gold_contract_ref` 必须指向同一冻结 evaluator bundle 中可解析的、不可变的 Gold
contract；其中包括计算 historical recall 所需的 accepted evidence alternatives。
`PerGoldComparison` 必须保存 `gold_metric_descriptor_ref` 和 descriptor content
hash。Aggregator 只能读取冻结 score/evaluator bundle 及其 content-addressed
refs，不得重新读取工作区 YAML、当前源码默认值或可变 benchmark 文件。
若 `PerGoldComparison` 继续冗余保存 `weight`、`mandatory`，它们必须与 descriptor
完全一致；`gold_id` 也必须一致，否则拒绝聚合。

这些 descriptor、Gold contract 和 accepted evidence 只允许在 freeze/reveal 后的
evaluator artifact 中出现；production retrieval、Controller、
`ContextAssemblySpec`、Writer Context 和 Evidence Ledger 均不得持有或引用它们。

对每个 profile `p` **分别**计算，禁止先合并两个 profile：

```text
G_p = 所有五个 checkpoint 中满足
      p in g.descriptor.applicable_profiles
      的 per-Gold comparison
```

规则：

- APC 与 VAC 分别产生一组 Gate 结论；只有两者都 PASS，Gate M4 才 PASS；
- cross-profile delta 只作诊断，不进入阈值；
- VAC 不适用的 author-plan Gold 因不在 `G_VAC` 中，不进分母；
- applicable Gold 缺 comparison、weight、descriptor、GoldType、GoldKind、
  applicable profiles、accepted evidence contract 或 evaluator manifest identity
  时，聚合器直接拒绝报告，不能按 0 条/N/A 静默跳过；
- descriptor、Gold contract、accepted evidence contract、evaluator manifest 的
  任一 ref/hash 不一致时 fail-closed；
- 五个 checkpoint 使用 **weight-micro** 聚合，不平均五个 case 的百分比。

#### 9.2.2 status 分值

```text
q(HIT)         = 1.0
q(PARTIAL)     = 0.5
q(MISS)        = 0.0
q(UNTRACEABLE) = 0.0
q(CONTRADICTS) = 0.0
```

`PARTIAL=0.5` 只用于 semantic coverage 轴。`UNTRACEABLE` 除计 0 外仍触发
trace hard veto；`CONTRADICTS` 除计 0 外仍触发 contradiction hard veto。

#### 9.2.3 current-state accuracy

```text
C_p = {g in G_p |
       g.descriptor.gold_type in {
         CURRENT_STATE,
         RELATIONSHIP_EMOTION,
         KNOWLEDGE_BOUNDARY,
         OBJECT_CONTINUITY
       }}

current_state_accuracy(p)
  = sum(g.descriptor.weight * q(g.status) for g in C_p)
    / sum(g.descriptor.weight for g in C_p)
```

阈值：APC、VAC 分别 `>= 0.95`。

#### 9.2.4 operational/plan coverage

```text
O_p = {g in G_p |
       g.descriptor.gold_kind in {
         OPERATIONAL_CONSTRAINT,
         PLAN_OBLIGATION
       }
       or g.descriptor.gold_type == PLAN_OBLIGATION}

operational_plan_coverage(p)
  = sum(g.descriptor.weight * q(g.status) for g in O_p)
    / sum(g.descriptor.weight for g in O_p)
```

这样 VAC 中由历史正文形成的 unresolved obligation 仍可进入 operational 轴，而
只对 APC 适用的 author Plan Gold 会由 `applicable_profiles` 排除。阈值：APC、VAC
分别 `>= 0.95`。

#### 9.2.5 key historical evidence recall

该轴衡量最终 Writer Evidence Ledger 是否取回关键历史支持，不直接复用 semantic
status：

```text
H_p = {g in G_p |
       g.descriptor.gold_type in {
         CAUSAL_HISTORY,
         LONG_RANGE_CALLBACK
       }}

ref_recall(g, accepted alternative A)
  = matched_text_evidence_ref_count(A, final Writer Evidence Ledger)
    / text_evidence_ref_count(A)

historical_ref_recall(g)
  = max(ref_recall(g, A)
        for A in resolve(g.descriptor.accepted_evidence_contract_ref)
        if A contains text evidence refs)

key_historical_evidence_recall(p)
  = sum(g.descriptor.weight * historical_ref_recall(g) for g in H_p)
    / sum(g.descriptor.weight for g in H_p)
```

ref matching 必须复用锁定的 `gold_evidence_matcher.v3` 身份/span 规则；不得用语义
相似度或整章 arbitrary overlap。typed failure 时 final ledger 不提供支持，
`historical_ref_recall(g)=0`。阈值：APC、VAC 分别 `>= 0.90`。

#### 9.2.6 typed failure、contradiction 与 trace

- typed failure 不从质量分母豁免。该 case 的全部 applicable Gold 按 `MISS/0`
  进入上述公式；
- typed failure artifact 若没有逐 Gold zero-score comparisons，聚合器必须拒绝；
- 任一 applicable Gold 为 `CONTRADICTS`，该 profile 直接 FAIL；
- 任一 applicable Gold 为 `UNTRACEABLE`，该 profile 直接 FAIL；
- 所有 frozen确定性 claim 的
  `Claim → SupportGroup → Evidence/PlanNode → Commit/Snapshot/Cutoff` 链必须
  100% 可解析；
- MISS 没有声称结论时不伪造 ContextItem lineage，但必须有 typed explanation 和
  stage-loss 归因；
- PARTIAL/HIT/CONTRADICTS 对应的 claim lineage 必须完整，否则转为
  `UNTRACEABLE`。

#### 9.2.7 空分母与 checkpoint 报告

- 某质量轴分母为 0 时不能自动记 1.0；只有 benchmark contract 显式声明
  `metric_not_applicable(profile, axis)` 才可记 N/A；
- 当前冻结 Gold contract 的基线分母如下，P0-2 测试必须逐项断言：

| Profile | Applicable Gold | Current-state | Operational/plan | Historical |
|---|---:|---:|---:|---:|
| VAC | 47 | 36 / weight 100 | 26 / weight 71 | 9 / weight 29 |
| APC | 72 | 36 / weight 100 | 51 / weight 96 | 9 / weight 29 |

- 因此当前双 profile benchmark 的三个轴都非空；任一数量或权重变化都表示 Gold
  contract/公式发生漂移，不能继续比较；
- unified Gate 使用每 profile 的 weight-micro 值；
- 同时强制发布每 checkpoint 的分子、分母、weighted score、mandatory status
  分布和 typed failure 状态，供定位但不替代统一公式。

### 9.3 聚合器验收测试

至少覆盖：

1. PARTIAL 的 0.5 权重；
2. macro 与 weight-micro 结果不同的 fixture；
3. APC/VAC 分开判定；
4. VAC plan-inapplicable Gold 不进分母；
5. observed historical obligation 进入 operational/plan 轴；
6. typed failure 全量计零且不能丢 denominator；
7. UNTRACEABLE、CONTRADICTS hard veto；
8. accepted alternative 取最大合法 evidence recall；
9. 空分母无 contract 声明时 fail-closed；
10. formula version/hash 漂移时拒绝聚合；
11. GoldMetricDescriptor、Gold contract、accepted evidence contract 或 evaluator
    manifest ref/hash 漂移时拒绝聚合；
12. comparison 与 descriptor 的 gold ID、weight 或 mandatory 不一致时拒绝聚合；
13. 不读取工作区 YAML 仍可复算；
14. 六组冻结分母数量和权重与 9.2.7 完全一致。

### 9.4 执行约束

1. `UNTRACEABLE=0` 是硬门，不允许用 semantic similarity 抵消；
2. APC C40 修复后必须正常评分，不能只以“变成 READY”为通过；
3. VAC 每个 checkpoint 都必须报告 mandatory per-Gold 结果，不能只看 macro；
4. 质量阈值必须从新的 per-Gold 结果计算；
5. 除代码和对应 version attestation 外，benchmark content、profile isolation、
   model、Writer/ledger budget、per-Need candidate limit、tool-call budget 保持锁定；
6. 任何预算或 matcher 规则变化都必须作为独立消融，不能混入正式 Gate run。

满足以上条件并关闭 WP6 第二位人工 reviewer 治理项后，Gate M4 才能从 HOLD
转为 PASS。此前继续禁止启动 WP8。

## 10. 最终责任归因

| 现象 | 主责任层 | 次责任层 | 结论 |
|---|---|---|---|
| 22 UNTRACEABLE | F-EVIDENCE | F-ASSEMBLY / F-EVAL observability | claim support group 不完整；matcher 不应放宽 |
| VAC C20-C95 HIT=0 | F-NEED / F-RETRIEVE | F-RANK / F-ASSEMBLY | 多段漏斗，不是单一 top-k |
| VAC C80/C95 后段 Need 0-call | F-NEED / scheduler | F-ROUTE | 顺序全局预算饿死 |
| VAC candidate-complete 到 selected=0 | F-RANK | F-NEED | 排序未保护 evidence closure |
| VAC selected 到 ledger 大量丢失 | F-ASSEMBLY | F-EVIDENCE | first-claim mandatory closure + optional packing |
| APC P002/C40 typed failure | F-FRESHNESS | F-EVIDENCE normalization | 同证据语义别名被判冲突 |
| verifier 空 traceable IDs 仍 SUPPORTS | F-EVAL | - | 不改变 fail-closed，但 receipt 必须加固 |

正式状态保持：

> **Gate M4 HOLD；基础设施/隔离/预算契约通过，证据追溯与内容质量未通过；
> WP8 未获授权。**
