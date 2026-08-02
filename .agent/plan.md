# 已批准执行切片候选：Stage 2M 跨 Need support closure

- Plan state: `WAITING_FOR_HUMAN_APPROVAL`
- Stage: `Stage 2M`
- Production-code baseline: `90f05b1` (`test(stage2m): close consolidation quality gaps`)
- Required baseline check: `git merge-base --is-ancestor 90f05b1 HEAD`
- Writer: OpenCode default `build` agent via `/implement`，本轮唯一写入者
- Planner / reviewer / merge owner: Codex
- Revision budget: `0/1`
- Merge policy: `CODEX_ON_PASS`
- Real endpoint authorization: `NOT_AUTHORIZED_IN_INITIAL_IMPLEMENTATION`

## 1. 权威来源与本计划的边界

本轮继续服从以下权威文档，不以本计划替代它们：

1. `docs/stage2_memory_benchmark_task_closure_execution.md`：0.1.7、0.2；
2. `docs/stage2m_gate_m4_root_cause_and_remediation_20260730.md`：7、8.3；
3. `AGENTS.md`：Codex–OpenCode Development Workflow。

本文件只定义一次开发迭代的具体实现与验收切片。P2 已经
`ACCEPTED_AS_DIAGNOSIS`；本轮不是重新证明 schema、lifecycle、reviewer 或 Gate，也不启动
P3。OpenCode 不得自行改变这里的设计决策；发现计划与代码不符时写入
`.agent/implementation.md` 的 `BLOCKED`，交回 Codex。

## 2. 已确认的真实现状

真实 VAC C60 产物：

- 目录：`/tmp/ns-stage2m-v21-v6-qwen-c60-20260802`；
- endpoint：`http://127.0.0.1:8002/v1`，Qwen3.6，128K；
- 22 个 Need 是自然生成结果，未触发兼容上限；
- 11 个两-Need proposal 批、3 个 verifier 批全部完成；
- `0 timeout / 0 endpoint error`；
- 最终 `weighted=0`、`mandatory=0`、9 个 Gold 全部 `MISS`；
- G06：candidate/rank/Stage1/Ledger accepted refs 为 `2/2 -> 2/2 -> 0/2 -> 0/2`；
- G09：candidate/rank/Stage1/Ledger accepted refs 为 `3/3 -> 3/3 -> 1/3 -> 0/3`。

对冻结 retrieval trace 的 evaluator-only 复核进一步确认：

- “rank complete”是跨所有 retrieval Need 汇总后的完整，不代表任一单独 Need 已拥有完整组；
- G06 的两段 rank evidence 被分配到不同的 public Need 路径；knowledge Need 只直接看到其中
  一段，另一段在其他 Need 的 selected candidates 中；
- G09 的三段 evidence 分散在 entity-history、knowledge、relation/event 等 public Need 路径；
  没有一个 Need 的当前 `units_by_need` 同时包含三段；
- `TrustedClaimSupportProducer._produce_semantic_support()` 当前只把
  `unit_need_ids[unit_id]` 直接列出的 unit 放入目标 Need 的 proposer evidence pool；
- 因而模型即使正确工作，也无法为目标 Need 生成需要 2–3 个 unit 的完整 compound claim；
- `ContextCompiler` 的 fair packing 已经改善 Stage1 预算分配，但不是此处的充分修复；
- 先前 `ns-6` 把精确 evidence ref 扩大到 enclosing block 的尝试失败 4/8 测试，已拒绝。

以上 G06/G09 信息只能用于 freeze/reveal 后诊断与验收。任何 Gold ID、accepted ref、章节号、
Gold 文本或 case/checkpoint 常量都不得进入 production 分支、prompt 选择规则或 synthetic test
fixture。

## 3. 根因判断

当前主要错误不是 endpoint、预算总量或模型调用次数，而是把 Need 同时当成了：

1. 检索请求；
2. 路由选择；
3. 排他的 evidence ownership 边界；
4. claim synthesis 的唯一证据池。

前三者耦合后，检索前对 Need type 的一次判断会永久限制后续证据组合。真实写作结论往往同时
依赖“人物历史 + 关系状态 + 知识边界”等证据；要求它们在检索前恰好落入同一细粒度 Need，
不符合真实任务。

本轮设计决策是：

> Need 继续是预算和检索路由单元，但不再是排他的后检索 evidence silo。Controller support
> synthesis 可以为一个目标 Need 使用由公共锚点证明兼容、已经被任一合法 Need 选中的证据；
> 最终 claim 仍只关闭目标 Need 的公开 facets，并由独立 verifier 对完整上下文验真。

这不是“把所有 Need 合并”，也不是默认三路并发。它是在检索完成后、claim 生成前建立一个
有界、可审计、fail-closed 的 compatible support pool。

## 4. 目标行为

当同一公共任务结论需要 2–3 个 cutoff-safe unit，而这些 unit 因检索路由分别被不同 Need
选中时：

1. Controller 能识别这些 unit 与目标 Need 具有明确公共锚点关系；
2. proposer 在目标 Need 的 `evidence_units` 中看到完整兼容组；
3. proposer 最多引用现有合同允许的 3 个 unit，生成一条完整、Writer 可读的 compound claim；
4. verifier 同时看到被引用和未引用的全部兼容 context，发现反证时拒绝；
5. `ClaimSupportReceipt`、`ClaimSupportGroup`、`ClaimVariant` 保留每个引用 unit 的精确 provenance；
6. `ControllerSupportSelector` 把该 support group 原子选入 `ContextAssemblySpec`；
7. deterministic Assembler 只校验和打包，不新增语义判断；
8. Evidence Ledger 中出现完整证据组和完整 claim，而不是几个互不关联的局部句子。

## 5. 明确的修复算法

### 5.1 只扩展 semantic proposer 的可引用池

在 `TrustedClaimSupportProducer._produce_semantic_support()` 内，为每个目标 Need 构造：

- `direct_units`：现有 `unit_need_ids` 直接绑定给该 Need 的合法 unit；
- `compatible_units`：由其他 Need 路径选中，但满足 5.2 全部条件的 unit；
- `support_pool = direct_units + compatible_units`，稳定去重并保持确定性顺序。

静态 deterministic `_claim_candidates()` 仍只处理直接绑定 unit。不要让未经模型验证的静态
claim 因跨 Need 扩池而扩大事实范围。

建议将兼容判断提取为小型私有 helper，例如 `_compatible_support_units()`；名称可调整，但必须
可被窄单测直接覆盖，不能把规则埋进 prompt 文本。

### 5.2 compatible unit 必须同时满足的条件

一个非 direct unit 只有满足下列全部条件才能进入目标 Need 的 support pool：

1. 该 unit 已经存在于本轮 Controller 提供的 `units` 中，即至少被一个合法 retrieval Need
   rank-selected；不得回看未选择的全库候选；
2. `unit_need_ids` 至少记录一个真实 origin Need；不得构造无来源的隐式绑定；
3. `_legal_for_need(task, target_need, unit)` 为真，继续执行 VAC/plan/scope lattice；
4. unit 的 `source_commit`、snapshot/basis 与当前生产输入一致；
5. `_resolution_status()` 对目标 checkpoint 返回 `RESOLVED`；future evidence、basis mismatch、
   无 evidence/plan provenance 的 unit 不得进入；
6. `derivation_taint` 为空；不得借跨 Need 机制传播 tainted evidence；
7. 目标 Need 与 unit/origin Need 之间存在明确公共锚点：
   - 优先使用 `target_need.entity_ids/focus_ids` 与 `unit.entity_ids` 的非空交集；或
   - target Need 与某个 origin Need 的 `entity_ids/focus_ids` 有非空交集，且 unit 是该 origin
     Need 的直接绑定 unit；或
   - unit 是目标 Need 直接绑定 anchor 的精确 parent/child lineage unit；
8. 仅有相似文本、相同 `object_hash`、相同 span 长度、相同 Need type 或同一 checkpoint
   均不构成兼容关系。

若目标 Need 没有任何 entity/focus/lineage 公共锚点，则保持旧行为：只使用 direct units。

### 5.3 排序与边界

support pool 必须确定性排序：

1. direct units 在前，保留 Controller 输入顺序；
2. compatible units 依次按“unit 与目标锚点直接相交”“origin Need 与目标锚点相交”排序；
3. 再按已有 retrieval 输入顺序、unit ID 稳定排序；
4. 相同 unit 只出现一次；同一 evidence 不通过扩大 span 或复制 unit 制造多个位置；
5. 继续使用现有 `SEMANTIC_SUPPORT_INPUT_LIMIT` 和
   `SEMANTIC_SUPPORT_MAX_UNITS_PER_CLAIM=3`；不得新增固定 Need 数量上限；
6. 不增加 proposal 并发。继续保持 `PROPOSAL_BATCH_SIZE=2`、单 endpoint 串行调用和现有
   `300s/120s` proposal/verifier timeout；
7. 兼容池只改变每个 Need 可审计的候选证据，不改变 Writer 4000 / Ledger 12000 预算。

如果现有 20-unit 窗口导致 direct units 被 compatible units 挤出，属于实现错误；direct units
必须先保留。若完整证据仍因 20-unit 窗口缺失，记录 `BLOCKED`，不得自行提高所有全局上限。

### 5.4 proposer 与 verifier 输入

proposer 的每个 `evidence_unit` 应保留：

- `retrieval_unit_id`；
- `unit_kind`；
- chapter/provenance 摘要；
- 精确 excerpt；
- 是否 direct；
- origin Need IDs（只使用 public IDs，用于审计，不用于声明这些 Need 已闭合）。

prompt 应明确：

- route/Need type 只描述检索来源，不证明 evidence 的语义；
- 可以从 compatible pool 选择 2–3 个 jointly necessary unit；
- 必须生成目标 Need 的完整结论，不能把 origin Need 当作已满足；
- 不得把同一实体下无关事件拼成 compound claim。

verifier 必须继续看到该目标 Need 的完整 bounded support pool，包括未被 claim 引用的兼容 unit。
任何可信反证都必须拒绝，即使模型返回 `supports=true`。不得缩小现有 verifier context 或绕过
`counter_evidence_retrieval_unit_ids` 校验。

### 5.5 support group 的所有权

本轮 compound claim 仍以一个 `target_need_id` 为 claim owner：

- `ClaimSupportGroup.need_ids` 和 receipt 的 `need_ids` 只列目标 Need；
- `need_facet_ids` 只能来自目标 Need 的公开 facets；
- borrowed unit 的 origin Need IDs 保留在 retained producer input 中，不自动关闭 origin Need；
- `retrieval_unit_ids`、`evidence_refs`、attestation 和 receipt 必须包含实际引用的每个 unit；
- 只有 verifier `supports=true`、无 counter evidence、全部 evidence resolution 合法时才生成 group；
- 多 Need 同时闭合属于后续独立设计，本轮禁止顺便实现。

## 6. 允许修改的文件

主要允许：

- `src/novel_agent/services/claim_support.py`
- `tests/unit/test_claim_support_selection.py`
- `.agent/implementation.md`

只有在测试证明现有调用方丢失 origin Need IDs 时，才条件允许：

- `src/novel_agent/services/stage2_paired_pilot.py`
- `tests/unit/test_stage2_paired_pilot.py`

条件文件只能修复“selected unit -> 全部 origin Need IDs”的通用 lineage 传递，不得加入 Gold、
checkpoint、case 或实体名称分支。当前代码已经尝试保留 shared unit 的全部 Need lineage，因此
默认预期无需修改。

以下文件本轮不再允许修改：

- `src/novel_agent/services/memory_pipeline.py`：真实证据已指向 post-retrieval Need silo；
- `src/novel_agent/services/writer_context_assembler.py`：Assembler 不是语义 owner；
- Need generator、scheduler、retrieval route、reranker；
- domain/schema、Gold matcher、evaluator、Gate、benchmark fixture；
- Stage 3 的任何文件或 worktree。

若实现确实需要修改这些文件，停止为 `BLOCKED`，由 Codex重新规划，不得自行扩域。

## 7. 实施顺序

### Phase A：先写失败的 synthetic reproduction

在不使用真实小说文本、Gold ID、章节号或 accepted refs 的情况下构造：

- 一个 target knowledge/compound Need；
- 两个或三个共享公共实体锚点的 sibling retrieval Needs；
- 每个 Need 各自只直接绑定一段 cutoff-safe evidence；
- 全局 selected units 合起来完整，但 target Need 的 direct pool 不完整；
- 旧实现的 proposer input 看不到完整组，测试必须先失败。

测试必须检查的是 public producer input、support group、claim 和 ledger 结构，不是模拟 Gold
matcher 分数。

### Phase B：实现 compatible support pool

按第 5 节实现私有 helper、稳定排序和 public provenance 字段。保持 direct-only deterministic
claim 路径不变。不要先改 prompt 再用模型输出掩盖 pool 仍不完整的问题。

### Phase C：闭合完整链路

使用 fake proposer/verifier 让 target Need 返回一个引用 2–3 个 compatible unit 的完整 claim，
并验证：

```text
selected units
  -> compatible support pool
  -> proposer compound draft
  -> independent verifier
  -> ClaimSupportReceipt / ClaimSupportGroup
  -> ControllerSupportSelector
  -> ContextAssemblySpec
  -> deterministic Assembler
  -> WriterContextPackage + EvidenceLedger
```

最终 Writer claim 必须表达 synthetic fixture 的全部必要子句，Ledger 必须包含每个引用 unit 的
精确 evidence ref。

### Phase D：安全回归与实现记录

完成第 8 节全部测试，将精确命令和结果写入 `.agent/implementation.md`。不要提交、合并、运行
真实 endpoint 或修改计划状态。

## 8. 必须新增或扩充的测试

### 8.1 正向机制测试

1. `test_semantic_support_pool_borrows_compatible_selected_units_across_need_routes`
   - target direct pool 只有 1 个 unit；
   - 两个 sibling Needs 各提供 1 个同锚点 unit；
   - proposer 的 target evidence pool 包含全部 3 个 unit；
   - origin Need IDs 可审计且稳定。
2. `test_cross_need_compound_claim_closes_only_target_need`
   - proposer 引用 2–3 个 unit；
   - group/receipt 只拥有 target Need/facets；
   - origin Needs 不被误标为 closed。
3. `test_cross_need_compound_group_survives_selection_and_assembly_atomically`
   - selector 选择完整 group；
   - spec 列出全部 unit/variant；
   - assembler 不拆 group；
   - Writer claim 和 Ledger 保留全部精确 evidence refs。
4. `test_direct_support_units_precede_compatible_units_under_input_budget`
   - direct units 永不被 spillover 挤出；
   - 顺序和重复运行字节稳定。

### 8.2 隔离与 fail-closed 测试

5. `test_support_pool_does_not_borrow_same_text_from_different_entity`
   - 文本相似但 entity/focus 不相交时不得借用。
6. `test_support_pool_does_not_borrow_unit_without_public_anchor_or_origin`
   - 元数据不足时保持 direct-only，不做自由语义猜测。
7. `test_support_pool_rejects_scope_plan_cutoff_basis_and_taint_violations`
   - VAC 不借 plan；
   - writer_safe 不借 author/evaluator；
   - future chapter、basis mismatch、tainted unit 均不进入 pool。
8. `test_support_pool_preserves_exact_span_identity_for_duplicate_content`
   - 相同 object/content 出现在不同 chapter/span 时不得扩大或替换 evidence ref；
   - 禁止重现 `ns-6` enclosing-block 错误。
9. `test_verifier_rejects_compound_claim_when_compatible_context_contains_counter_evidence`
   - 即使 proposer 引用的 unit 支持，未引用 compatible unit 有反证时仍拒绝；
   - 即使 verifier 错返 `supports=true` 并列出 counter evidence，host 仍拒绝。
10. `test_support_pool_does_not_change_proposal_batching_or_timeout_contract`
    - 继续每批最多两个 Need；
    - proposal/verifier timeout 保持 300/120；
    - 不因 compatible unit 数量拆成更多 proposer 调用。

### 8.3 必须保持通过的现有回归

至少显式运行并确认以下行为仍成立：

- semantic IDs 只能来自 public input；
- 单 facet 可以由 2–3 个 unit 形成 compound claim；
- multi-unit 上限仍为 3；
- non-cited counter evidence 可见并 fail-closed；
- unknown scope 返回空；
- plan/VAC、cutoff、basis、evidence resolution 与 semantic support 不混淆；
- mandatory support group 在预算不足时原子失败；
- long-range grounded rescue 与 causal follow-up 既有测试不回归；
- proposal/verifier malformed output、缺 raw output、endpoint exception 均 typed fail-closed；
- Writer/Ledger 预算不被扩大。

## 9. OpenCode 允许运行的检查

先运行新增测试的精确 node id，再运行文件级聚焦测试：

```bash
.conda-env/bin/pytest -q tests/unit/test_claim_support_selection.py --no-cov
```

若条件修改了 paired pilot，再运行：

```bash
.conda-env/bin/pytest -q tests/unit/test_stage2_paired_pilot.py --no-cov
```

静态检查：

```bash
.conda-env/bin/ruff check src/novel_agent/services/claim_support.py \
  tests/unit/test_claim_support_selection.py
.conda-env/bin/ruff format --check src/novel_agent/services/claim_support.py \
  tests/unit/test_claim_support_selection.py
.conda-env/bin/mypy --strict src/novel_agent/services/claim_support.py
git diff --check
```

如修改 paired pilot，将对应 source/test 文件追加到 Ruff、format 和 MyPy 命令。

OpenCode 本轮不得运行：

- `make quality`；
- 真实 8002 模型调用；
- C60/C95/C80 canary；
- P3、五点矩阵、A/B/C；
- 全小说 replay。

`make quality` 由 Codex 在准备合并时只运行一次。

## 10. `.agent/implementation.md` 证据合同

OpenCode 必须记录：

1. `Revision: 0/1`；
2. 实际修改文件；
3. root cause 是否与第 2/3 节一致；
4. 新 helper/算法如何决定 direct、compatible、rejected unit；
5. synthetic reproduction 在修改前为什么失败、修改后为什么通过；
6. target Need、origin Need 与 group ownership 如何区分；
7. 所有命令、退出码和 passed 数；
8. 是否修改 paired pilot；若修改，证明 origin lineage 原先在哪里丢失；
9. 未运行真实 endpoint、canary 和 P3 的声明；
10. 剩余风险，尤其是公共锚点过宽或元数据缺失情况。

可记录冻结 trace 的结构性计数 `2/2 -> 0/2`、`3/3 -> 0/3`，但不得把 private Gold 文本、
accepted refs、小说段落或由 Gold 推导出的 runtime 关键词复制进实现、测试或 handoff。

## 11. Codex 初审验收标准

Codex 只有在以下全部成立时才给 `PASS`：

1. diff 仅在允许文件内，或 paired pilot 条件修改有明确证据；
2. 新机制解决“跨 route 的 compatible evidence 无法进入一个 target support pool”，而不只是
   改 prompt 文案；
3. runtime 不包含 G06/G09、P003、C60、章节号、Gold component 或 accepted-ref 分支；
4. 没有新的固定 Need 小上限、默认三路并发或调用膨胀；
5. direct unit 优先、兼容规则确定、无公共锚点时 fail-closed；
6. compound group 的 exact evidence、receipt、attestation、variant、spec 和 Ledger 闭合；
7. counter evidence、scope、cutoff、basis、taint、profile isolation 不回归；
8. focused tests、Ruff、format、strict MyPy、`git diff --check` 全部通过；
9. `.agent/implementation.md` 证据完整；
10. Codex 独立 spot-check 通过。

若首次 review 失败，只允许 OpenCode 按 `.agent/review.md` 修复一次并写 `Revision: 1/1`；第二次
失败立即停止，交回人工。

## 12. 后续真实 C60 canary 的独立准入门

本计划的初始 `/implement` 不授权真实模型调用。Codex 离线 review PASS 后，才可以另行把
`.agent/review.md` 写为 `PASS_OFFLINE / C60_CANARY_AUTHORIZED`，并允许一次 C60 Arm A：

1. 先检查 `http://127.0.0.1:8002/v1/models`，10 秒内失败则停止；
2. 确认仍为单并发 Qwen3.6 128K 配置；
3. 使用新 experiment ID，只跑 VAC C60 diagnostic/non-formal Arm A；
4. 不运行 C80、C95、P3、五点或 A/B/C；
5. 任一 endpoint error/timeout => `RUNTIME_FAILED / NON_COMPARABLE`，不自动重试；
6. future leakage、profile cross-contamination、Writer/Ledger 超预算必须为 0；
7. 输出 claim 必须有合法 cutoff-safe evidence；
8. evaluator-only stage loss 必须显示完整 evidence 从 rank 进入完整 Writer-readable claim/
   Ledger，而不只是 candidate 数或 reference matching 上升；
9. G06/G09 至少各出现一个引用完整 2-unit/3-unit 组、verifier 通过的 Writer claim，才算目标
   机制成立；这些 Gold ID 只用于 freeze 后验收，不得影响 runtime；
10. 若只产生更多局部 claim、只改善 prompt receipt、或 Ledger 仍为 `0/2`、`0/3`，判定
    `NO_MECHANISM_GAIN`，不运行 C95。

C60 机制明确成立后，Codex 才能另行批准同类 C95 canary。即使 C60/C95 有改善，也只有接近
正式门时才进入 P3；本轮绝不签发 Gate M4 PASS。

## 13. 立即停止条件

出现任一情况，OpenCode 不继续猜测修复：

- 需要修改 Need generator、scheduler、retrieval route、domain/schema 或 Assembler；
- 需要 Gold/case/checkpoint/实体专用规则才能让测试通过；
- 公共 entity/focus/lineage 元数据不足以安全建立 compatible pool；
- 必须提高全局 candidate、Need、Writer 或 Ledger 上限；
- 需要并发模型调用或自动 endpoint 重试；
- 发现 frozen trace 与第 2 节结论矛盾；
- 无法在 synthetic test 中同时证明正向 closure 和跨实体隔离。

此时只写 `.agent/implementation.md` 的 `BLOCKED`、证据和最小需要决策，不修改禁区文件。
