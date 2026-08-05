# OpenCode implementation and evidence

- State: `RETURN_TO_CODEX`（§15：人工授权 thinking 重测 b11 完成——模型合成层被证明**机械上能**
  跨章合成（b11 的 verified claim 均为真实多章合成），但**不产出** G06/G09 所需的完整结论：
  verdict 层 G06 **UNTRACEABLE**、G09 **MISS**、weighted=0.0；结论-targeting/问题对齐成为
  残余首层损失，需 Codex 架构决策）
- Campaign: `.agent/plan.md`（CAMPAIGN 模式）；原计划 C60 预算 3/3 已用尽并判定
  `CAMPAIGN_HOLD`；**人工明确指令「预算放宽，修复使得满足门槛并完成 phase6」**，
  本会话继续在既有责任走廊内修复并以新 attempt 复验，直至 C60 机制门槛（G06/G09 各一条
  引用完整证据组、verifier 通过的 Writer claim）达成后运行 C95。
- Stage: Stage 2M；Writer: OpenCode default `build`（全程唯一写入者）
- Baseline: `e2c9705` + 已批准 dirty 基线（v25 机制 + 11 回归），全程保留未 reset

## 1. Changed files

- `src/novel_agent/services/claim_support.py`（Phase 0 无改动；Phase 4 三次修复 + 版本 v25→v27）
- `tests/unit/test_claim_support_selection.py`（Phase 0 typing 收口；Phase 4 新增 2 回归 +
  调整 1 既有断言）
- `.agent/implementation.md`（本文件）
- `docs/stage2m_support_closure_campaign_20260802.md`（attempt 表 + 结论）
- `docs/stage2_memory_benchmark_task_closure_execution.md`（§0.1.9、§0.2、§21.2）
- `docs/stage2m_gate_m4_root_cause_and_remediation_20260730.md`（§7 + P1-2 表行）
- `docs/project_status.md`（§4.7）、`docs/README.md`（文档表）
- 未修改 paired pilot / memory_pipeline / domain / schema / evaluator / Gate / Stage 3

## 2. Phase 0 — typing 收口与全仓质量

按计划指引修复新增测试区的 19 个 strict-MyPy 错误（cast json.loads 边界、evidence_units 收窄、
completion_spec 局部断言、retrieval_unit_id 收窄），未用 Any/ignore/弱断言。

```bash
.conda-env/bin/mypy            # Success: no issues found in 284 source files
make quality                   # 1402 passed, 9 deselected; TOTAL 19487 stmts 0 miss 0 branch-partial; 100%
```

## 3. Real run ledger（VAC C60 Arm A，real_hybrid，Qwen3.6/8002，模型 `qwen36-27b-nvfp4`）

| Attempt | Experiment / root | Producer | Proposals | Verifier | Endpoint | Semantic receipts | G06 Ledger | G09 Ledger | Comparable |
|---|---|---|---|---|---|---|---|---|---|
| A0 | `stage2m-support-v25-c60-a0-20260802` `/tmp/ns-stage2m-support-v25-c60-a0-20260802` | v25 | 11（batch 10 内容级 OpenAIChatEndpointError；vLLM 该请求 200 OK，服务未切换） | 5×3 | 0 timeout/0 HTTP error | 11（5 compound） | 0/2 | 1/3 | yes |
| A1 | `stage2m-support-v26-c60-a1-20260802` `/tmp/ns-stage2m-support-v26-c60-a1-20260802` | v26 | 11/11 | 6×3 | 0/0 | 17 | 0/2 | 1/3 | yes |
| A2 | `stage2m-support-v27-c60-a2-20260802` `/tmp/ns-stage2m-support-v27-c60-a2-20260802` | v27 | 11/11 | 7（3+3+3+3+3+3+2） | 0/0 | 18 | 1/2 | 1/3 | yes |

- 三次均 weighted=0、mandatory=0、9/9 MISS、assembly READY、Writer 3999/4000、Ledger ≤8496/12000、
  future leakage=0、profile 隔离 0、请求前后 `/models` 200。
- A0 diagnostics: `SEMANTIC_SUPPORT_INCOMPLETE_NEED_COVERAGE`、`PRODUCER_OPENAICHATENDPOINTERROR`、
  `VERIFIER_INCOMPLETE_DECISIONS`；A1 diagnostics: `[]`；A2: `SEMANTIC_SUPPORT_VERIFIER_INCOMPLETE_DECISIONS`（fail-closed）。
- untraceable_rate：A0/A1 0.111 → A2 0.0。

## 4. Per-attempt first-loss diagnosis（frozen artifacts）

### A0（v25）

- G06：rank 2/2 完整，但无任何 pool 含完整 pair —— event Needs 的 `historical_grounded_rescue`
  分支只显示 `SEMANTIC_SUPPORT_LATE_GROUNDED_UNIT_LIMIT=4` 的 late-grounded 窗口，direct block
  56.0 被挤出；knowledge pool 有 56.0 无 2.0。真实 producer + recording endpoint 精确复现：
  11 批 pool 与 retained prompt 一致，`G06 co-occurrence: False`。
- G09：knowledge pool 已含完整 triple（block 36/56 + anchor 载体 ch50），但 batch 4
  （history+knowledge）模型只返回 1 条 history claim 且漏标 knowledge insufficient →
  覆盖契约失败整批丢弃（`SEMANTIC_SUPPORT_INCOMPLETE_NEED_COVERAGE`）。
- 其余 Gold（G01-G05/G08）candidate 阶段 incomplete（F-NEED_ROUTE_RETRIEVE）、G07 F-RANK——
  更早检索/排序层，无 in-scope 修复证据。

### A1（v26）

- Fix A/B 生效：G06 pair 于事件 pool 共现；batch 4 覆盖通过；零诊断；17 receipts。
- 新 first loss（draft）：knowledge claim 引用 history pool 的 block 27.0（knowledge 池外）→
  normalization 静默丢弃；G06 事件 pool 有完整 pair 但模型引用 block 24.0 单单元。
- 行：`pool complete, draft partial/missing → improve generic compound-claim instruction`。

### A2（v27）

- Fix C 生效：knowledge claim 引用池内 3 units（relationship-attitude / enrollment-status /
  marriage-intent，均为 ch56 载体）并形成 receipt；G06 block 2.0 经 black-dragon claim 首次进
  Ledger（0/2→1/2）；untraceable 0。
- 残余 first loss（无 in-scope 杠杆）：模型合成层——knowledge claim 覆盖 ch56 局部结论
  （徐有容/婚约），未表达 G09 完整结论（落落入学/轩辕破第三学生/学院庇护与风险）；G09 三 ref
  落入三个不同 Ledger group，无单个完整 claim。host 走廊（pool/prompt/contract）已耗尽。

## 5. Repairs（每项都有失败前置/通过后置的 license-free 回归）

- **Fix A（v26）**：rescue 分支保留全部 direct grounded evidence（rescue 集仍最前）。
  回归 `test_semantic_pool_keeps_all_direct_grounded_units_beyond_late_rescue_window`
  （修复前 1 failed）；调整 long-range 测试断言（distractors 由禁止改为在 rescue 之后）。
- **Fix B（v26）**：prompt 强化 per-Need 记账（遗漏即整批丢弃）。覆盖校验未放宽。
- **Fix C（v27）**：claim 只能引用同 entry 的 evidence_units（池外引用使 claim 失效）+ 完整组
  可用时必须引用完整组。回归 `test_semantic_claim_citing_unit_from_another_need_pool_is_rejected`
  （v26 1 failed / v27 passed，含 prompt 内容断言）。
- **Fix D（v28，人工放宽预算后）**：证据多样化 pool —— 当候选池超过 20 窗口时，折叠只重复
  已保留证据的 unit（同一 passage 以 grounded block + relation/state anchor + curator 副本
  形式出现），让有界窗口偏向证据多样性，使其他 route 的独立兼容证据能进入饱和池。证据身份 =
  evidence_id 相等或 object_hash 相同且精确 span 重叠 ≥50%（与 provenance matcher 一致）。
  回归 `test_saturated_pool_collapses_duplicate_evidence_and_admits_distinct_compatible`
  （修复前 1 failed / 修复后 passed）+ `test_evidence_ref_covered_requires_precise_overlapping_spans`
  （100% 覆盖 `_evidence_ref_covered`）。小池（≤20）不折叠 —— counter-evidence 与重复内容
  精确 span 契约不受影响（既有测试保持）。
- Version：`v25 → v26 → v27 → v28`（public 配置身份每次更新）。
- 每轮修复后 focused pytest、ruff、format、strict mypy、`git diff --check` 通过；A1/A2/A3 前置
  `make quality` 全绿（1401 / 1402 / 1404 passed，100% coverage）。

### v28 离线复现（真实 A0 冻结 trace 重建全部 22 个 pool）

- G06 完整 pair {G06a(ch2), G09c(ch56)} 在事件 pool 中共现：black-dragon、
  luo-luo-disables-tian-hai-ya-er（Fix A 之后即成立）；
- G09 完整 triple {G09a(ch36), G09b(ch50), G09c(ch56)} 在 knowledge pool 中经 anchor 载体
  完整共现；relationship pool {G09a, G09b}（缺 ch56 载体）。
- 结论：pool 层门槛已满足；A3 起验证模型合成层是否引用完整组。

## 6. Safety / budget / provenance

- future leakage=0、profile 交叉=0（三次）；Writer ≤4000、Ledger ≤12000 全程满足；
- 每次 claim 均带精确 cutoff-safe evidence、receipt/attestation/basis/snapshot；
- 未知 scope、未决证据、taint、无 origin、counter evidence 均 fail-closed（既有测试保持）；
- 模型调用串行；PROPOSAL_BATCH_SIZE=2、300/120s timeout 未变；无并发/重试膨胀；无新 Need 上限。

## 7. Documentation updates

- `docs/stage2m_support_closure_campaign_20260802.md`：attempt 表（A0/A1/A2）+ 结论 +
  下一步架构建议；
- `docs/stage2_memory_benchmark_task_closure_execution.md`：§0.1.9、§0.2 链条、§21.2；
- `docs/stage2m_gate_m4_root_cause_and_remediation_20260730.md`：§7 + P1-2 表行；
- `docs/project_status.md` §4.7、`docs/README.md` 文档表。

## 8. Final campaign outcome

**`CAMPAIGN_HOLD / C60_BUDGET_EXHAUSTED`**（3/3 C60 预算用尽）：

- 机制 gain 成立：语义 verified receipts 11→17→18；compound 引用持续（跨 Need 兼容池 +
  池内引用约束 + 覆盖契约）；G06 一 ref 首次进 Ledger；untraceable 0；A1/A2 零 endpoint 错误；
- 目标闸门未达：G06/G09 未形成引用完整证据组的单个 verified Writer claim；残余 first loss
  在模型合成层；host 责任走廊已耗尽；
- C95 未运行（准入未满足）；C40/C80/P3/五点/A-B-C/WP8/Stage 3 未运行；未提交、未合并、
  未改 Gate；`.agent/review.md` 未触碰。

## 9. Residual risks and next recommendation（供 Codex 决策）

- 残余风险：v26/v27 的修复是 pool/prompt 层，模型合规性（池内引用、完整组、覆盖）无法被
  host 强制；池外引用仍静默丢弃（fail-closed）；local-follow-up 分支对 history 类 Need 的
  截断为既有回归背书行为；G01-G05/G07/G08 的 candidate/rank 损失在责任走廊外。
- 下一步架构方向（campaign 内不实施）：
  1. 跨 Need verified drafts 的 claim fusion（新语义 owner，需新计划）；
  2. G01-G05/G07/G08 的 candidate/rank 层修复（memory_pipeline 走廊，需独立 first-loss 证据）；
  3. G06/G09 自然 owner Need 的查询对齐。
- 禁止仅以 reference/candidate 计数提升作为机制证据；建议下一条执行链只接受「完整证据组 →
  一条 verifier 通过的 Writer claim → Ledger 完整 refs」作为成功信号。

---

## 10. 预算放宽后的执行链（A3 起；本文件为单一执行链证据源）

- 人工指令「预算放宽，修复使得满足门槛并完成 phase6」后，本链在既有责任走廊内继续。
  基线保持 `e2c9705` + dirty（v25→v29，未 reset），全程不 commit、不 merge、不改 Gate/domain/
  schema/evaluator。
- 运行配方固定为 Gate 文档 §8.3 的 P2 单点模板：`--arms A`、`real_hybrid`、
  `--allow-dirty-diagnostic`、`qwen36-27b-nvfp4`@8002、C60 resume
  `sha256:da501411530ab54da79233e0b10da173639888f918f73d13762ac955cb8d52d7`、
  隔离项目 `reports/stage2m/isolated_projects/precise_p13_v2_20260730/visible_at_cutoff`、
  DB `na_s2m_vac_v1_20260729`（凭据来自 `.env` 的 POSTGRES_USER/PASSWORD/PORT）。

### 10.1 运行台账（真实冻结产物，非比较性 canary）

| Attempt | 代码状态 | G06 stage1 | G09 stage1 | G06 Ledger | G09 Ledger | 首层损失 |
|---|---|---|---|---|---|---|
| A4/v29 | 池折叠 Fix D | 0/2 | 1/3 | 0/2 | 1/3 | F-ASSEMBLY（4 金块全 dropped） |
| A5/v29 | +stage1 compact 基础版 | 0/2 | 1/3 | 0/2 | 1/3 | F-ASSEMBLY（顺序/预算饱和） |
| A6 | +grounded 轮询 tier + 大块恒 compact | 2/2 | 3/3 | 0/2 | 1/3 | F-ASSEMBLY（writer 侧重排） |
| A7 | +compact 保留 content_hash | 2/2 | 3/3 | 0/2 | 1/3 | 同上 |
| A8 | +selected_units 纳入 style 层 compact（parent 血缘） | 2/2 | 3/3 | 1/2 | 1/3 | 模型合成（组不完整） |
| A9 | +full-passage 证据优先排序 | 2/2 | 3/3 | 0/2 | 1/3 | 模型合成（run 间方差） |

- A4→A9 全程：future leakage=0、profile 交叉=0、Writer ≤4000、Ledger ≤12000、
  `/models` 200、无 timeout/HTTP error（A5 一次因 shell 超时误杀进程后 setsid 重启）。

### 10.2 三层根因与修复（每项均有失败前置/通过后置回归）

- **E1（装配层，A4/A5 确认）**：4 个金块（block 2.0/36.0/50.0/56.0，携带
  `evidence.full.block.ZTJ-P005.*` ref、object_hash 恰为 gold 对象哈希）在 stage1 打包时全部
  进入 `dropped_optional_unit_ids`（预算 4000 被其他全文块/展开先耗尽）。修复
  `memory_pipeline._assemble_context`：
  - `include_if_fits` 对单个 grounded 单元：full 超 `COMPACT_FULL_TEXT_TOKEN_CAP=200` 或
    remaining 不足时一律以 bounded excerpt（`COMPACT_BLOCK_EXCERPT_LIMIT=320`）代表，保留
    原 evidence_refs、parent 血缘与 content_hash（同 passage 身份）；
  - 新增 grounded 轮询 tier（每 Need 每轮一个组，round-robin），深排位直接证据不再被先填满
    的尾备选饿死；full 已入时同 passage 的 compact 视为重复表示直接拒绝（避免同一 passage
    重复计费）。
  - 回归：`test_context_compiler_compacts_large_block_even_when_budget_allows`、
    `test_context_compiler_deep_grounded_block_survives_budget_competition` 及既有 23 项全部保持。
  - A6 生效：G06/G09 stage1 由 0/2、1/3 → 2/2、3/3；四个 `compact.grounded.block.ZTJ-P005.*`
    携带 gold object_hash 进入 writer context。
- **E2（血缘层，A7 确认）**：compacts 是新 id，`selected_units`
  （`stage2_paired_pilot._assemble_stage2m_comparison`）只取 traces 候选 + raw_evidence_spans，
  compact 单元丢失 need 血缘 → producer pool 完全看不到金块。修复：把
  `style_or_reference_optional` 一并纳入（经 parent_unit_id 反查 allocation），与
  raw_evidence_spans 同路径。回归：`test_compact_excerpt_of_selected_block_keeps_need_lineage`。
  A8 生效：15 个 support group 引用 compact 单元（含 `{36.0, 28.0, 2.0}`、`{32.0, 36.0, 2.0}`、
  `{50.0}`），Ledger 首次出现 gold 对象哈希（`evidence.full.block.ZTJ-P005.2.0` → e468fe0，G06
  1/2）。
- **E3（池排序层，A9）**：knowledge 池中 anchors/curator 片段排在 blocks 之前，模型倾向引用
  窄 span 的 curator ref（与 gold span 无数值重叠 → 不匹配）。修复：直接池排序
  `(not full-passage, input_order, id)`，full-passage 单元（blocks/compacts）优先。回归：
  `test_semantic_pool_ranks_full_passage_refs_before_narrow_fragments`。A9 生效：knowledge 池以
  block 20.0 领队，Ledger 出现 `evidence.full.block.ZTJ-P005.36.0`（887fb，G09a，G09 1/3）。

### 10.3 机制结论与残余层

- **机制已打通到 Ledger 层**：金块证据（G06 2/2、G09 3/3）→ stage1 writer context（compact
  + 精确对象哈希）→ producer pool（血缘绑定）→ Writer claim 引用 full-passage ref → Ledger
  命中 gold 对象哈希（A8: e468fe0 两次；A9: 887fb 两次）。装配/血缘/池排序三层宿主走廊修复
  全部生效且有回归背书；`make quality` 全程 100% 覆盖（1422 passed, 9 deselected）。
- **残余层 = 模型合成（完整组组合）**：G06 需 {2.0+56.0} 同一条 claim、G09 需
  {36.0+50.0+56.0} 同一条 claim；模型 run 间方差（A8 引 2.0、A9 引 36.0），从未把完整 gold
  组合成单条 verified claim。Fix C 的「完整组可用时必须引用完整组」prompt 约束无法被 host
  强制；跨 Need verified drafts 的 deterministic claim fusion 即 implementation.md §9 已列出的
  架构方向（需新计划/新语义 owner），超出本链责任走廊。
- **G08（F-NEED_ROUTE_RETRIEVE）**与 G01-G05/G07 的 candidate/rank 层损失仍在走廊外（既有结论）。

### 10.4 安全/预算/产物

- 每项修复均 license-free 回归 + 全量 `make quality`（1422 passed, 100%）；无新 Need 上限、
  无 budget 膨胀（4000/12000 未动）、无并发/重试膨胀；金块/检查点/用例专用逻辑零新增；
  未触碰 domain/schema/Gold/matcher/evaluator/Gate/Stage 3；未 commit、未 merge。
- 真实产物保留：A4-A9 在 `/tmp/ns-stage2m-support-v29-c60-aN-20260803/`，冻结对象在
  `reports/stage2m/isolated_projects/precise_p13_v2_20260730/visible_at_cutoff/objects/sha256/`
  （frozen-paired-context + writer-evidence-ledger 按 mtime 区分 attempt）。

### 10.5 下一步建议（供 Codex 决策）

- 机制成功信号（§5 链条）已走到「pool→Ledger 含精确 refs」；剩余唯一缺口是模型把完整组
  合成单条 claim。选项：(a) 批准 claim fusion 新计划（deterministic 组完整化，需 Codex 定
  义语义 owner 与边界）；(b) 接受「机制已在宿主走廊证明」并据此评审；(c) 对 C95 的准入
  判定由 Codex 决定（当前 C60 尚未达「完整组单 claim」终态，不建议直接跑 C95 当通过）。


---

## 11. Codex REPAIR 后的 R1-R3 实施（2026-08-03 二次评审方向）

- Codex 评审结论（`.agent/review.md`）与修订方向（`.agent/plan.md` §8）：
  - E1-E3（装配/血缘/池排序）作为有效阶段成果保留；
  - 批准 claim composition/fusion 方向，但 deterministic host 不得自创语义；必须有明确语义
    owner、独立 verifier、宿主侧 facet/reference 闭合校验；
  - compact excerpt 必须区分「模型实际可见的精确支撑跨度」与「整段原文的父级血缘」；
  - C95 仍不准入。
- 本阶段实施（Phase R1-R3，全部在既有 support producer/controller 走廊内）：

### 11.1 实施内容

- **R1 raw-support 工作集**（独立于 Writer 4000 产品预算）：`_raw_support_slices` 按 passage
  身份（object_hash 集合）去重，优先 grounded block/span 的精确整段 slice；anchors/curator
  片段不再挤占可用 raw slice；`ordered_pool_by_need` 保留池序（E3 全段优先排序）。
- **compact 精确支撑血缘**（评审 #6）：compact 单元携带逐句 `evidence.segment.*` 精确 span
  ref（quote_hash=句文本、span=句在源块的精确区间），整段 ref 仅作为 parent 血缘保留；
  `_clean_claim` 对纯空白 claim 返回空串（fail-closed）。
- **R2 atom 流程**：每个 slice 由语义 owner 产出恰好一个局部 `SemanticAtomDraft`（一条由该
  slice 直接蕴含的命题）；逐 atom 独立验证（evidence=该 slice，context=同 Need 有界反证）；
  同一 Need 的已验原子按 slice 序取至多 3 个做零改写连接（标点连接，不改写/不推导）；组合后
  整体语义验证；宿主校验：facet 闭合 + 引用并集精确（构造即保证）+ basis/snapshot/scope/
  taint/截止 + verifier 决策完整，任一失败 fail-closed。
- **R3 逐项隔离 + 漏斗**：未知 need/slice 的 atom 只丢弃该 atom；缺决策/非法决策只拒绝该
  claim；`SupportFunnel` 类型化计数（slices→atoms→compounds→emitted）随
  `_record_progress(stage="funnel")` 输出。
- 回归测试（license-free）：`test_atom_flow_*`（两/三原子并集、整体验证 fail-closed、逐项
  隔离、空文本、未决证据、超长 atom schema 拒绝、缓存）、`test_saturated_pool_collapses...`、
  `test_semantic_pool_ranks_full_passage_refs...`、`test_compatible_pool_rejects_*`、
  `test_evidence_ref_covered_*`、`test_anchorless_target_borrows_exact_lineage_unit`、
  `test_semantic_atom_duplicate_of_deterministic_claim...` 等；`make quality` 全绿
  （1425 passed、9 deselected、100% 覆盖、strict mypy/ruff clean）。

### 11.2 真实 C60 运行台账（A10-A13，最终代码）

| Attempt | Proposal 批 | 漏斗（atoms proposed / verified / compounds verified） | 知识批次 | Ledger G06/G09 |
|---|---|---|---|---|
| A10 | 6 完成 5 失败 | 108 / 107 / 10 | 失败（endpoint） | 0/2, 0/3 |
| A11 | 6 完成 5 失败 | 108 / 106 / 10 | 失败（endpoint） | 0/2, 0/3 |
| A12 | 1 完成 3 失败 | — | 失败（endpoint） | — |
| A13（600s 超时 + 4096 输出上限） | 10 完成 1 失败 | 228 / 227 / 18 | **仍失败** | 0/2, 0/3 |

- 失败批恒为 `{knowledge, history}`（陈长生知识/历史 Need，slice 最多的批）：
  `OpenAIChatEndpointError`（httpx 层请求失败，重试耗尽）。600s 超时与 4096 输出上限修复后
  其余大批（continuity/callback/event 等）均成功；唯 knowledge+history 批仍被端点丢弃——
  A0 时代即有同类端点行为记录（`PRODUCER_OPENAICHATENDPOINTERROR`）。
- 漏斗证明 atom 流程机制成立：A13 全程 227 原子独立验证通过、18 个 compound 组合并整体
  验证通过、0 verifier transport failure；stage1 金匹配回落到 0/2、0/3 是 compact 段级 refs
  （评审 #6 的诚实血缘）的预期后果。

### 11.3 结论与交接（不再 CONTINUING）

- **R1-R3 全部实施并验证**；`make quality` 100%；漏斗使首层损失可判定：
  candidate→atom（提出/拒绝计数）→atom verified→compound composed/verified→emitted。
- **C60 所需成功信号未达成**：G06 2/2、G09 3/3 单条完整 verified claim 未出现。阻塞点是
  真实端点确定性丢弃 knowledge+history 批（demonstrated host/runtime defect），不是代码路径
  或语义层。按计划 §4/§6，端点失败不得推断检索质量；gold 相关批不可比较。
- 计划 §8.6.5 返回条件命中：「真实 API/基础设施对该批不可用」——需要 Codex 决策：
  (a) 服务层修复后重跑；(b) 批准缩小每 Need atom 窗口（会丢失 deep-rank 的 G09a/b slice，
  属设计取舍）；(c) 其他端点/批次策略。C95 未运行；P3/五点/正式 Gate 未运行；未 commit。

## 12. v30 exact-slice 走廊 A1-A4 台账与 RETURN_TO_CODEX（2026-08-04）

> **RETURN_TO_CODEX**：本轮为终止交接，不继续 A5、不继续调整 packing、不实施新的
> retrieval/packing/端点修复、不再跑真实 API。以下为 frozen 产物证据与诊断局限。

### 12.1 本轮走廊与运行前提

- 按 review 方向实施 v30 producer：`src/novel_agent/services/claim_support.py` 由 v29 atom 流程
  改为 exact-slice 走廊（`_resolve_exact_slices` 段落/句窗切片、`_pack_workset` token 有界、
  `_propose_single_slice` / `_propose_multi_slice`、`_verify_claim_whole` 整条验证、raw-slice
  `EvidenceLedgerEntry` 留存）。配套 `writer_context_assembler.py`（raw entries 参数）、
  `stage2_paired_pilot.py`（funnel 接线）、`teacher_forced_benchmark_e2e.py`（`_support_terminal_state`），
  并重写 `tests/unit/test_claim_support_selection.py`（90 用例）。当时 `make quality` 1466 passed、
  100% 覆盖、strict mypy/ruff clean。
- 每次运行：VAC `visible_at_cutoff`、`real_hybrid` 检索、本地 qwen36-27b-nvfp4@8002、
  resume commit `sha256:da501411…`（resume ch60，`precise_p13_v2_20260730/visible_at_cutoff`）、
  C60 单 case（ZTJ-P005）、`--allow-dirty-diagnostic`、quality-repair 配置
  `/tmp/quality_repair_flags.json`。实验目录 `/tmp/ns-stage2m-exactslice-v30-c60-a{1..4}-20260804/`。
  本报告不含数据库/端点凭据。
- A1-A4 运行时走廊为「整 workset 单次 multi-slice 调用」。评审到此前在**工作树**新增了
  chunked multi-slice 循环与 `SEMANTIC_SUPPORT_SEMANTIC_INPUT_TOKEN_BUDGET=24_000`
  （claim_support.py:110、:1547 `_workset_chunks`）——该改动未参与 A1-A4、未经真实端点验证，
  且最后 `make quality` 停在 99.98% 覆盖未收口。按 Codex 指令不继续修复；当前代码基即此状态。

### 12.2 Manifests 与终态

| Attempt | 时间窗（UTC） | token budget | 失败事件 | support 终态 | scenario | 诊断（mandatory / untraceable） |
|---|---|---|---|---|---|---|
| A1 | 08-03 16:48→17:44（~57 min） | 2400 | 9 个 proposal | `failed` | completed=true, READY | 0.0 / 0.222 |
| A2 | 08-03 18:08→19:18（~69 min） | 12000 | 11 个 proposal | `failed` | completed=true, READY | 0.0 / 0.111 |
| A3 | 08-04 00:43→01:36（~53 min） | 40000+精简提示 | 7 个 proposal | `failed` | completed=true, READY | 0.0 / 0.111 |
| A4 | 08-04 01:55→02:41（~46 min） | 40000+12K 单窗 | 7 个 proposal | `failed` | completed=true, READY | 0.0 / 0.333 |

- 四次的 `support_progress.json` 顶层 `state` 均为 `failed`（`terminal` 事件 `state=failed`），
  而事件流末尾另有 legacy `freeze completed` 事件、`scenario_run.completed=true`——运行生命周期
  跑完，support 终态失败，二者脱节（见 §12.6）。
- 金结局：G06 UNTRACEABLE、G09 MISS（A1/A4 均如此）；`current_state_accuracy` 0.0/16 未过门槛。

### 12.3 Funnel 关键计数（A1-A4，来自 terminal/funnel 事件）

| Attempt | slices_resolved | slices_budget_dropped | ledger_dropped | slices_not_proposed_transport | proposals (req/fail) | single (proposed/verified) | multi (proposed/verified) | verifier 拒绝 | needs_insufficient / facet_not_closed |
|---|---|---|---|---|---|---|---|---|---|
| A1 | 11884 | 11159 | 611 | 500 | 37 / 9 | 4 / 3 | 8 / 4 | 4 | 6 / 15 |
| A2 | 11884 | 9405 | 2300 | 2213 | 38 / 11 | 3 / 2 | 8 / 7 | 2 | 4 / 13 |
| A3 | 11884 | 4562 | 7197 | 4381 | 34 / 7 | 9 / 6 | 7 / 2 | 5 | 1 / 14 |
| A4 | 11884 | 4562 | 7197 | 4320 | 34 / 7 | 8 / 6 | 7 / 3 | 5 | 1 / 13 |

- `slices_resolved` 四次恒为 11884：候选池确定性。A3/A4 的 40K workset 预算把 budget-drop
  从 11K 压到 4.5K，但随之 transport 失败面扩大（2.2K→4.3K not-proposed）。
- 0 verifier transport failure；整条验证拒绝为 2-5 次/run，非失败主因。

### 12.4 目标 slice 成员审计（workset/传输成员 = 全部 proposal+verification 事件的 slice ID）

| 章节 | A1 | A2 | A3 | A4 | 含义 |
|---|---|---|---|---|---|
| ch2（G06 所需） | **0** | **0** | **0** | **0** | 从未进入任何 workset |
| ch50（G09 所需） | **0** | **0** | **0** | **0** | 从未进入任何 workset |
| ch36（G09 所需） | 142 | 228 | 922 | 526 | 进入 workset |
| ch56（G06/G09 所需） | 10 | 10 | 548 | 289 | 进入 workset |

- 传输事件是 workset 抽取的窗口全集（`semantic_input_dropped=0`），故「传输缺席」即
  「workset 缺席」：G06 需要 {ch2,ch56}、G09 需要 {ch36,ch50,ch56}，四轮均**天然无法闭合**。
- 这命中 `.agent/plan.md` §7 停止条件第一条（plan.md:241）：「required source blocks or exact
  slices are absent before SupportWorkset construction」。**首要失败点是候选源缺失，不是验证过严**。

### 12.5 第一丢失边界未定位（诊断局限）

- 代码在切片**之前**先行截断候选 handles：
  - `claim_support.py:60` `SEMANTIC_SUPPORT_INPUT_LIMIT=20`（固定上限）；
  - `claim_support.py:733` `ordered[:SEMANTIC_SUPPORT_INPUT_LIMIT]`（ranked 截断）；
  - `claim_support.py:756-758` compatible 合并后再次 `combined[:SEMANTIC_SUPPORT_INPUT_LIMIT]`；
  - `claim_support.py:797` 截断后才 `_resolve_exact_slices`。
- 产物只记录了传输层 slice ID；**没有记录 input units → ranked → compatible → capped pool →
  resolved blocks → exact slices → packed workset 任一边界成员**。因此无法区分 ch2/ch50 是
  在更上游检索未检出，还是被 top-20 丢弃——此边界审计是 Codex 修复方向的先决条件。

### 12.6 端点失败与预算（诊断局限）

- 失败事件仅持久化 `error_type=OpenAIChatEndpointError`、`prompt_bytes`、`input_hash`、
  `slice_unit_ids`；**无 HTTP 状态、异常文本、响应体、超时/重试类别、失败 prompt**；
  `console.log` 为单行诊断 JSON，无失败明细。无法区分 HTTP 400 / 超时 / 输出截断 / 结构化
  响应错误。
- 失败请求尺寸：A1 全为 47.5-67.2KB；A2 72.9-258.2KB；A3/A4 为 25.9KB、38.3KB +
  247.7-258.4KB。A3/A4 中 25.9/38.3KB 的小请求也失败，而同规模请求有成功——
  **不能断言七次失败全是尺寸问题**。
- 预算只计 slice 正文 token（24K，见 §12.1）；未计入长 slice ID、chapter/start/end 字段、
  Need/facet/task JSON、提示指令、结构化输出与 4096 输出预算。实测序列化 prompt 最高
  约 258KB。此前的端点探针：25K/26K/28K/30K token 成功、35K token 报 HTTP 400。
- 单次 multi-slice 超时 600s（`SEMANTIC_SUPPORT_MULTI_SLICE_PROPOSAL_TIMEOUT_SECONDS=600`），
  对应 46-69 分钟/run 的运行时长。

### 12.7 多 chunk 循环缺提前退出（工作树新改动的问题）

- 新增的 chunk 循环在进入前只计算一次 `covered_facets`（claim_support.py:905 附近）；某 chunk
  已产出使 Need 闭合的 verified claim 后，循环不复查闭合并继续请求剩余 chunk，增加端点失败面。

### 12.8 交接结论（RETURN_TO_CODEX）

- v30 exact-slice 走廊 A1-A4 全部 `support failed`；ch2/ch50 四轮从未进入任何 SupportWorkset，
  G06/G09 天然不可闭合；命中 plan.md §7 停止条件。阻塞点在 SupportWorkset 之前，不在验证层。
- 需要 Codex 给出技术方向（按评审给出的顺序）：
  1. 逐 Need 候选成员审计（input→ranked→compatible→capped→resolved→slices→workset），
     先确定 ch2/ch50 的第一丢失边界；
  2. 调整 retrieval-handle 与 exact-slice 次序：完整合法 selected/retrieved handles →
     Need-aware、source/chapter 多样候选 → 段落/句窗切片 → 按完整序列化请求尺寸预算与 packing；
     不写 G06/G09 或 ch2/ch50 特例；
  3. transport 预算必须覆盖完整序列化 prompt（含 ID/字段/Need/facet/task JSON/指令/输出
     headroom），而不是只算正文 token；
  4. required facets 闭合后立即停止其余 chunk 请求；
  5. 失败产物持久化 HTTP 状态/超时类别/异常文本/请求预算/可定位失败输入引用；
  6. 确认 ch2/ch50 均进入 workset 后，才值得运行下一次真实 C60。
- 工作树状态：含未收口的 chunked 改动（最后 `make quality` 99.98%），按指令不再继续修复。
  C95/P3/五点/正式 Gate 未运行；未 commit。

---

## 13. Codex REPAIR 执行链（2026-08-04）：全边界审计 → 首损定位 → 边界修复 → 序列化传输预算 → b9 真实 C60

### 13.1 状态与代码指纹

- 代码基线 `e2c9705`（未 commit），最终代码指纹
  `sha256:9eeba714eceeb8d3e5d63ac6648d560e075f6cedbcb785fba6daa6f0a079d2fd`
  （= b9 `experiment_manifest.json.code_source_fingerprint`，同一工作树通过 `make quality`）。
- 最终 `make quality`：**1496 passed, 9 deselected, TOTAL 20176 stmts 0 miss 0 branch-partial,
  100% 覆盖；strict mypy（284 files）与 ruff 全绿**（`make quality` 输出见下，非仅声明）。

### 13.2 实施内容（对应 review §1-§4）

**R4 全边界成员审计（review §1）**：`claim_support.py` 新增 `_AuditRow`/`_emit_audit`，
在既有 `support_progress.json` 流上按 Need 记录 10 个边界的**有序成员 + 类型化 keep/drop 原因**：

```text
legal_input_handles -> direct_ranked_handles -> compatible_handles
-> deduplicated_diversified_handle_pool -> bounded_selected_handles
-> l0_blocks_spans_resolved -> exact_slices_segmented -> support_workset_packed
-> semantic_chunks_exposed -> raw_ledger_entries_retained
```

每行携带 unit/slice 身份、L0 family（`_unit_family_id`：精确 span block_id → parent 血缘 →
自身）、章节号、表示种类、origin Need、稳定序号、字节代价、类型化 drop 原因
（`ranked_cap_dropped`/`family_collapsed:duplicate_representation`/
`diversity_collapsed:duplicate_evidence`/`handle_budget_cap_dropped`/
`compact_preview`/`not_grounded`/`resolution_failed:no_canonical_exact_span`/
`filtered:{taint,basis_commit_mismatch,snapshot_mismatch,access_scope,cutoff_violation}`/
`duplicate_slice`/`workset_budget_drop`/`ledger_budget_drop`）。
`blocks_resolved` 改为按**实际唯一解析 L0 block** 计数（A1-A4 恒为 0 → 现在 45）。
新增 `pre_proposal_trace` 模式：无模型调用跑完全确定性走廊并产出全部审计（CLI
`--support-pre-proposal-trace`）。

**R5 首损定位（冻结 C60 确定性 trace1-4，无模型调用）**：trace1 定位第一丢失边界：

- ch2：`grounded.block.ZTJ-P005.2.0` 在 legal input 存在（10 个句柄），但被
  `_evidence_diverse_pool` 与 `compact.…2.0` 视为同 evidence 去重时**先到先得保留了 compact**
  （compact 携带 parent 的 full-passage ref），随后 compact 在 `l0_blocks_spans_resolved`
  以 `compact_preview` 被拒 → ch2 **0 slice**；
- ch50：`grounded.block.ZTJ-P005.50.0` 在多数 Need 的 `bounded_selected_handles` 被
  `handle_budget_cap_dropped`（20 固定上限，且在 compatible 合并前对 ranked 先截断），
  仅 curator span 产出 2 slice；
- 结论：首损在 **ranked 预截断 + 固定 top-20 + evidence 去重先到先得**，非验证层。

**R6 边界修复（review §2）**：

1. 删除 compatible 合并前的 `ordered[:20]` 预截断（ranked 不再独立封顶）；
2. 新增 `_family_canonicalize`（L0 血缘规范化）：同一 family 的 block/compact/anchor 折叠为
   最 canonical 代表（full-passage block=5 > block=4 > span=3 > compact=2 > anchor=0，
   compact 前缀判定在 kind 之前——compact 即使携带 full ref 也只是派生预览）；
   位置取 family 首个稳定位；`_chapter_diverse_order` 让每个合法源章节的领先句柄
   在预算耗尽前先进入；
3. 显式 handle 预算（`SEMANTIC_SUPPORT_INPUT_LIMIT=20` 保留为报告值）只作用于
   血缘规范化+多样性折叠之后的流，仍用公开 relevance/章节多样性/稳定检索序；
   全部行为无 Gold/章节/实体特例。

**R7 序列化请求传输预算 + 提前退出 + 失败诊断（review §3）**：

- `_workset_chunks` 按**完整序列化 prompt 预算**分块（`SEMANTIC_SUPPORT_SERIALIZED_REQUEST_
  TOKEN_BUDGET=15_000`，含 task/Need/facet JSON、slice 身份与元数据、正文、指令、
  结构化输出框架、输出 headroom=4096）；token 估计按端点实测标定（CJK≈1.23 chars/token、
  ASCII≈4.46 chars/token，估计器保守上溢）；每个请求事件记录
  `estimated_input_tokens/prompt_bytes/max_output_tokens/timeout_seconds/applied_input_token_budget`；
- 每次 emitted verified claim 后重算 required-facet 闭合并**停止后续 chunk**（闭环提前退出）；
- 失败调用持久化**内容寻址 failed_input_ref**（artifact store 保留失败 prompt）+ 消毒分类
  `_classify_failed_call`：HTTP status / connect_read_timeout（httpx cause 链）/
  retry_exhausted（adapter attempts）/ output_length_truncation / invalid_json /
  missing_structured_content / invalid_structured_content（pydantic ValidationError 计为
  proposal rejection，不污染 transport 计数）；`_sanitize_error_message` 剥离 URL/凭据/超长。

**R8 语义调用传输形态（真实端点实证驱动）**：A/B 迭代中发现本地端点在 json_schema strict
grammar 模式下对多 slice 合成写出 >8192 token 的穷举式 claim（b2/b5/b7 批量截断），
json_object 模式则体积受控但形状漂移（v29 旧契约 `claim/cited_slice_unit_ids`）。最终形态：
proposal 两阶段改用 `generate_text`（json_object framing）+ 宿主 pydantic 校验（fail-closed），
模板内显式 shape 约束（`claims[].need_id/need_facet_ids/slice_unit_id(s)/claim_text`）+
400 字符 claim 上限 +「只引用实际依赖的 slice」指令；verifier 保持 grammar 模式（A1-A4 零
verifier transport 失败）。probe 窗口 12K→4K（`SEMANTIC_SUPPORT_SINGLE_SLICE_INPUT_TOKEN_
BUDGET`），probe 输出上限 2048→4096。

### 13.3 回归测试（license-free，全部随 `make quality` 通过）

`tests/unit/test_claim_support_selection.py` 90→120 用例，新增覆盖：10 边界审计事件与行结构、
trace 模式无模型调用、family canonicalization 各优先级/替换/跨 family 保持、chapter 多样序、
`blocks_resolved` 唯一计数、每类 resolution drop 原因、`_unit_family_id` 血缘路径、
`_estimate_prompt_tokens` 标定上溢、`_sanitize_error_message`、序列化请求分块（>1 chunk 且
每 chunk 序列化估计 ≤ 预算-输出）、超大 slice 独占 chunk、空 workset、闭环提前退出（2 chunk
workset 只发 1 次 multi 请求）、失败事件 `failed_call`/`failed_input_ref`/预算字段、
分类器各 category（HTTP/超时/截断/JSON/缺内容/重试耗尽/多级 cause 链）、`endpoint_adapter`
路由错误。fixture `_unit` 改为按章节派生 block_id（生产按章节 TextBlock 的模型）。

### 13.4 确定性冻结 C60 预提案 trace4（最终代码，无模型调用）

`--support-pre-proposal-trace`，resume ch60，`real_hybrid`，产物
`/tmp/ns-stage2m-audit-trace4-20260804/support_progress.json`：

| 边界 | ch2 | ch36 | ch50 | ch56 |
|---|---|---|---|---|
| legal_input_handles | 10 | 31 | 14 | 15 |
| bounded_selected_handles | 18 | 19 | 14 | 19 |
| l0_blocks_spans_resolved | 5 | 14 | 7 | 10 |
| exact_slices_segmented | **345** | 1512 | **476** | 839 |
| support_workset_packed | **345** | 1512 | **476** | 839 |
| semantic_chunks_exposed | 215 | 1020 | 445 | 540 |
| raw_ledger_entries_retained | 215 | 1020 | 445 | 540 |

- terminal：`blocks_resolved=45, slices_resolved=23592, state=completed_with_failures`
  （trace 无模型调用，proposal_requests=0）。
- 按 Need 的 workset 组合：事件类 Need（black-dragon/luo-heng/luo-luo-disables/luo-luo-vs-mo-he）
  同时含 {ch2, ch56}（G06 族）；knowledge/relationship/marriage/xuan-yuan-po 等 Need 同时含
  {ch36, ch50, ch56}（G09 族）。逐边界成员与原因全部落在审计行内，非仅聚合计数。

### 13.5 真实 VAC C60 b9（新实验身份，最终代码）

- 实验：`stage2m-exactslice-v31-c60-b9-20260804`，目录 `/tmp/ns-stage2m-exactslice-v31-c60-b9-20260804`，
  resume ch60 `sha256:da501411…`，隔离项目 `precise_p13_v2_20260730/visible_at_cutoff`，
  DB `na_s2m_vac_v1_20260729`，qwen36-27b-nvfp4@8002，`--arms A`，`--allow-dirty-diagnostic`；
  端点监控：`/models` 全程 200。
- terminal funnel：`blocks_resolved=45, slices_resolved=23592, slices_budget_dropped=8168,
  proposal_requests=58（49 completed / 9 failed: output_length_truncation）、
  multi_slice_proposals=27, multi_slice_verified=21, whole_verifier_rejected=4,
  proposals_rejected=2（invalid_structured_content）, needs_insufficient=2,
  facet_not_closed=1, verifier_transport_failures=0, slices_not_proposed_transport=757,
  ledger_dropped=15303, writer_dropped=0`。
- 产物：`support_progress.json` 含 10 边界审计 + 每请求预算字段 + 失败 `failed_call`
  （category/detail/status_code/retry_count/failed_input_ref，全部 content-addressed 于
  隔离项目 objects）；`scenario_run.completed=true`；`future_leakage_count=0`,
  `future_isolation_failure_count=0`；Writer **3993/4000**、Ledger **11964/12000**；assembly READY。
- 金结局（frozen `stage2m_case_C60_A.json`）：G06 **UNTRACEABLE**、G09 **MISS**，
  weighted=0.0、mandatory=0.0、untraceable_rate=0.222 —— verdict 层与 A1-A4 相同（未改善）。

### 13.6 证据驱动的中间修复（A/B 轮，均留下失败前置/通过后置）

- b1-b2：single-slice probe 在 2048/4096 输出上限下被本地模型 deliberation 截断
  （`output_length_truncation`）→ probe 输出上限统一 4096；
- b3-b5：multi-slice 在 json_schema grammar 模式下穷举式输出（154 slice chunk 写出
  >8192 token 仍截断；79 slice chunk 亦 >4096），且 8192 输出无法在 ModelRequest
  domain 上限 600s 内完成 → 序列化请求预算 15K（chunk ≈ 79 slice）+ 4096 输出 +
  400 字符 claim 指令 + shape 约束（R8）；
- b6-b7：24K 常量重复定义导致 15K 未生效（复测后修复）；79-slice chunk 在 grammar 模式
  仍截断 → 传输形态切 json_object + 宿主校验（R8）；
- b9 终局：58 请求中 49 完成，21 条 verified multi-slice claim，0 verifier transport 失败。

### 13.7 残余层与交接（RETURN_TO_CODEX）

- **机制走廊已全部打通并验证**：ch2/ch36/ch50/ch56 源族从 handle 到 workset 到 chunk 到
  raw Ledger 全程存活（trace4 + b9）；21 条独立整条验证通过的 Writer claim 携带精确
  `evidence.slice.*` ref 走完 claim→receipt→variant→spec→Writer Context→Ledger；
  失败全部类型化、失败输入全部内容寻址保留；Writer/Ledger 预算未动。
- **残余首层损失 = 模型合成层**：b9 中事件类 Need 的 verified claim 引用 ch16/ch28/ch32
  等单 slice（黑羊场景被模型当作黑龙事件引用），knowledge/relationship Need 的 claim
  引用 ch56 anchors/curator 片段而未组合 {ch36,ch50,ch56} 完整结论；G06 语义匹配 claim
  无 accepted provenance（gold ref 需完整组匹配），verdict 层未改善。模型在
  json_object 形态下倾向 verbatim 复制 slice 文本/碎片 claim（如单引号 claim 经 verifier
  通过），host 无法强制其合成完整跨 slice 结论——与 review 既往判定「模型合规性无法被
  host 强制」一致。该层需要 Codex 的下一步架构决策（如跨 Need 确定性 claim 完整化或
  语义 owner 精化），不属于本次走廊修复范围。
- 未运行：C95（准入未满足——verdict 层未改善）、P3、五点矩阵、正式 Gate、A/B/C、Stage 3；
  未 commit、未 merge、未触碰 review/plan/task 文档。

---

## 14. b10 真实 VAC C60（2026-08-04，最终代码）：指令强化 + fail-closed 垃圾拒绝 + v31

### 14.1 实施内容（对应 review「Required repair direction」§1-§3，均为有限改动）

1. **版本 bump**：`claim_support.py` `version = "trusted_claim_support_producer.v30"` →
   `"trusted_claim_support_producer.v31"`。
2. **多 slice 合成指令强化**（`_MULTI_SLICE_PROMPT_TEMPLATE` 合成段落整体替换，其余
   shape/scope/cutoff/taint 指引不变）。新合成段落原文（前置五条指令）：
   「Answer ONLY the required facets' questions. If the supplied slices cannot jointly
   establish the complete required-facet conclusion, return `insufficient_need_ids` — never
   write a claim about a background or unrelated slice, and never claim a slice supports a
   conclusion it does not contain. Synthesize ONE complete Writer-facing claim from the
   subset of supplied exact slices whose content jointly establishes the complete
   required-facet conclusion. The claim must be a new sentence combining the slices'
   content; it must not be a verbatim copy of any slice text, and it must not begin with a
   chapter title. Cite in `slice_unit_ids` ONLY the slices whose content the claim's clauses
   directly depend on — never the whole supplied list. The claim must be at most 400
   characters. If the complete conclusion cannot be expressed within that bound, return
   `insufficient_need_ids` instead of exceeding the ceiling.」
   既有「Preserve all material qualifications, negation, and epistemic scope. Treat facet
   kinds as questions to resolve, not asserted values」与 JSON-shape 指引原样保留。
3. **fail-closed 垃圾拒绝**：新增 `TrustedClaimSupportProducer._reject_garbage_claim`——
   剥离 Unicode 空白/标点/符号后 <4 个语义字符即拒绝。实现说明：Python 标准 `re` 不支持
   `\p{...}` 转义（plan 片段中的 `[\s\p{P}\p{S}]` 会 `re.error`），故用 `[\s\W]` 表达同一
   语义：Unicode 空白（Zs/Zl/Zp/控制空白）、全部标点（`\p{P}`）、全部符号（`\p{S}`）都是
   非 word 字符，剥离后剩余即语义长度；纯确定性非语义判断，绝无文本相似度/子串匹配。
   两处调用点均在 `_verify_claim_whole` 之前、各自宿主校验之后：
   - 单 slice 路径（`single_slice_sufficient` 为真、`single_slice_proposals += 1` 之后）；
   - 多 slice chunk 循环（`cited_ids ⊆ chunk_ids` 且 `facet_ids ⊆ legal_facets` 校验通过之后，
     `continue` 到下一 chunk，Need 仍开放）。
   拒绝时 `funnel.proposals_rejected += 1` 并记录类型化事件
   `stage="proposal_rejected", reason="rejected:garbage_claim"`（携带 need_id 与 claim_text）；
   不消耗 verifier 请求。未加任何逐字复制/子串拒绝（gold 结论本身与 slice 文本近逐字，
   子串检查会误杀合法 G06/G09 结论）。

### 14.2 回归测试（license-free，全部随 `make quality` 通过）

新增 15 个用例：
- `test_reject_garbage_claim_rejects_non_semantic_claims`（参数化 7 例：`"”"`/`"。`/纯 CJK
  标点串/纯空白/1-3 语义字符 → True）；
- `test_reject_garbage_claim_accepts_semantic_claims`（参数化 4 例：≥4 语义字符 → False）；
- `test_multi_slice_template_front_loads_required_facet_directives`（首指令先于合成指令的
  位置序断言 + 逐字复制/章节标题禁止 + epistemic-scope 保留 + JSON-shape 保留）；
- `test_producer_version_is_v31`；
- `test_single_slice_garbage_claim_rejected_before_verification`（`。` claim → 无 verifier
  请求，`proposal_rejected` 事件 reason=`rejected:garbage_claim`，仅 2 个 proposal 请求）；
- `test_multi_slice_garbage_claim_rejected_before_verification`（`”` claim 同语义）；
- 既有两用例按 guard 语义更新：`test_emit_verified_claim_skips_whitespace_only_text` 改为
  直接调用 `_emit_verified_claim` 覆盖 emit 层防御性 `_clean_claim` 空分支；
  `test_emit_verified_claim_skips_cleaned_empty_text` → 改名
  `test_whitespace_only_multi_slice_claim_rejected_at_proposal_stage`（纯空白 claim 在
  proposal 层被拒绝、无 verifier 请求）。
  全部新测试不引用 Gold/G06/G09/章节号/实体 ID/case ID/checkpoint。

### 14.3 质量门与代码指纹（b10 运行同一指纹）

- 命令：`make quality`（=`ruff check .` + `ruff format --check .` + `mypy` + 禁止模型调用
  的 `pytest -m "not model_required and not integration"`）。
- 结果：**1511 passed, 9 deselected；TOTAL 20186 stmts 0 miss, 5692 branches 0 partial,
  100% 语句与分支覆盖；strict mypy（284 files）与 ruff 全绿**。
- 指纹：基线 commit `e2c9705` + dirty 工作树；`code_source_fingerprint =
  sha256:b06d26313764b73980cd3ba3c11791ebb7e993bd9273f0c147565faabca36be9`，与 b10
  `experiment_manifest.json.code_source_fingerprint` **完全一致**（同一树、无中间改动）。

### 14.4 b10 运行配方与走廊结果（实验 `stage2m-v31-c60-b10-20260804`）

- 目录 `/tmp/ns-stage2m-v31-c60-b10-20260804`；配方与 b9 完全一致：`--source
  benchmarks/private/ztj_memory_pilot_v0.1`、`visible_at_cutoff`、`--arms A`、
  `real_hybrid`、resume `sha256:da501411…` ch60、隔离项目
  `reports/stage2m/isolated_projects/precise_p13_v2_20260730/visible_at_cutoff`、
  DB `na_s2m_vac_v1_20260729`、`qwen36-27b-nvfp4`@`http://127.0.0.1:8002/v1`、
  `local_openai`、max-output 8192、max-retries 1、quality-repair
  `/tmp/quality_repair_flags.json`、`--allow-dirty-diagnostic`。
- 端点监控：`/models` 全程 200（启动前、运行中、结束均探测）。
- terminal funnel（b9 → b10 对照）：

| 计数器 | b9 | b10 |
|---|---|---|
| proposal_requests | 58 | **49** |
| proposals completed / transport failed | 49 / 9 | **47 / 2**（output_length_truncation、retry_exhausted） |
| multi_slice_proposals / verified | 27 / 21 | 26 / **22** |
| whole_verifier_rejected | 4 | **1** |
| proposals_rejected（shape） | 2 | 3 |
| needs_insufficient | 2 | **3** |
| facet_not_closed | 1 | 1 |
| proposal_rejected:garbage_claim 事件 | — | **0**（本次模型未产出 <4 字符/纯标点 claim） |
| verifier_transport_failures | 0 | 0 |
| slices_not_proposed_transport | 757 | **165** |
| blocks_resolved / slices_resolved | 45 / 23592 | 45 / 23592（与 b9/trace4 逐字节一致） |

- 走廊零回归（acceptance 第 2 项满足）：workset 关键族 **ch2=345, ch36=1512, ch50=476,
  ch56=839**（= b9/trace4）；`semantic_chunks_exposed` 审计显示 required 族全部进入模型
  semantic input——G06 族 Need（black-dragon/luo-heng/luo-luo-disables/luo-luo-vs-mo-he/
  xuan-yuan-po-vs-tian-hai-ya-er）每 chunk 携带 {ch2, ch56}，G09 族 Need（knowledge/
  marriage/xuan-yuan-po-student-of-chen-changsheng/tian-dao-yuan-confrontation）每 chunk
  携带 {ch36, ch50, ch56}；raw Ledger 保留 accepted refs（G06 2/2、G09 3/3）。
- 预算/安全：Writer **3999/4000**、Ledger **11979/12000**、`future_leakage_count=0`、
  `future_isolation_failure_count=0`、assembly **READY**、`writer_dropped=0`。

### 14.5 Verdict 与硬停止证据（frozen `stage2m_case_C60_A.json`）

- **G06 = MISS、G09 = MISS**，weighted_coverage **0.0**、mandatory_hit_rate 0.0、
  untraceable_rate 0.111、`gate_passed=False`（b9 的 G06 UNTRACEABLE 在 b10 退化为 MISS：
  本次连语义匹配 claim 都不存在）。
- `stage_loss_diagnostics`：G06/G09 均为 **F-ASSEMBLY**——`writer_ledger` accepted refs
  (2/3) 全部在 Ledger 内但 0 matched，即走廊把 required 切片完整送达、claim 层未合成。
- 模型层硬证据（frozen ledger 共 138 条 entry = 49 `evidence.slice.*`（其中 27 条 raw-slice
  + 22 条语义 verified claim）+ 33 条确定性 claim（`evidence.support.*`）+ 56 条 curator
  span）：
  - **22 条语义 verified claim 中 6 条为多章**：`[20,26]` 荐书/宁婆婆、`[20,26]` 洗髓/深渊枷锁、
    `[9,27]` 三千道藏、`[35,39]` 落衡的先生、`[44,45]` 天海牙儿/青藤宴、`[32,36]` 落落道歉/
    承诺——**无一条覆盖 {ch2, ch56} 或 {ch36, ch50, ch56}**；
  - **0 条语义 verified claim 引用 ch2 或 ch56**（G06 族全部缺席；b9 至少引用
    ch16/ch28/ch32）；
  - **b9 的招牌失败在 b10 原样复现**：black-dragon Need（semantic input 携带 {ch2, ch56}）
    的 verified claim 仍是 ch16 黑羊场景「陈长生喂食黑羊青草…证实了二者间罕见的亲密联结」
    ——模型再次无视前置首指令「never write a claim about a background or unrelated slice」，
    写背景 slice 而非 `insufficient_need_ids`；
  - G09 邻近 claim 只引单族 slice：ch36-only「落落称陈长生为先生，视其为师。」、ch39-only
    「轩辕破是陈长生的学生，落落称陈长生为先生。」；
  - 垃圾 guard 生效证据：**语义路径 0 条 <4 字符/纯标点 claim**（b9 有 3 条碎片 claim 通过
    verifier）——`proposal_rejected:garbage_claim` 事件 0 次是因为模型本次未产出该类垃圾；
  - 章节标题前缀 claim（「第23章 星之海洋…」「第50章 铜针…」）与单字符 `”` claim 仍存在于
    ledger，但均来自**确定性走廊** `_claim_candidates`（`evidence.support.*` 引用 =
    claim_index 后缀），该走廊不经过 `_verify_claim_whole`、不属于 plan/review 授权的两个
    垃圾 guard 调用点（单 slice 路径 + 多 slice chunk 循环），b9→b10 行为一致、非回归。

### 14.6 结论与交接：RETURN_TO_CODEX（硬停止条件命中）

- 强化指令 + fail-closed 垃圾拒绝 + v31 指纹后的一次全新真实 C60（b10）仍无法产出 G06 或
  G09 的任何一条完整 verified Writer claim（verdict 层 MISS/MISS、weighted 0.0），且
  b10 比 b9 更差（G06 UNTRACEABLE → MISS）：**模型合成层在统一合成提示下被证明无法跨
  slice 合成 required-facet 完整结论**——b9 的黑羊背景 claim 在首指令强化后原样复现，
  且模型本次连 G06 族的 ch2/ch56 切片都未引用。review §3 的硬停止条件与 plan §4.3(b)
  同时命中，不再迭代 prompt 工程。
- 走廊（host 责任）两轮内全部达成并零回归：10 边界审计、family canonicalization、
  chapter-diverse 预算、序列化请求预算、闭包提前退出、失败诊断、garbage 拒绝、v31 指纹、
  1511 测试 100% 覆盖。
- 附带发现（供 Codex 决策参考，非本次授权范围）：确定性走廊 `_claim_candidates`
  （`evidence.support.*`）仍会把单字符 `”` 与章节标题前缀文本作为 claim 写入 ledger——
  该走廊不经 `_verify_claim_whole`、不属 plan §2.3 的两个调用点；若后续架构决策涉及
  host 侧 claim 质量防线，可考虑在同一走廊加同样的非语义 guard（需新授权）。

### 14.7 运行时诊断补充（2026-08-04，b10 之后，只读检查）：thinking mode 关闭

- 结论：**qwen36-27b-nvfp4 的思考模式（thinking）全程关闭**，证据三处：
  1. `src/novel_agent/adapters/model/openai_chat.py:200` 每次请求固定发送
     `"chat_template_kwargs": {"enable_thinking": False}`；
  2. vLLM 服务端以 `--reasoning-parser` 启动（Qwen3.6 为混合推理模型，支持 thinking）；
  3. b10 47 条 completed proposal 的原始输出（content-addressed 保留）全部无
     `reasoning_content` 字段——模型贪心直答，无推理过程。
- 与症状的关联：G06/G09 合成属多步推理任务（定位跨族子集→拼接→保留否定/认知边界→严格
  JSON）；关思考的直答模式与观察到的贪心抄片/答非所问（黑羊 ch16）/run 间方差高度吻合。
- 限制说明：不能断定 thinking 是唯一原因（开启后仍可能失败）；且现有传输预算（multi-slice
  输出上限 4096、600s 超时、15K 序列化预算、proposal 300s）均按"无 thinking"标定，开启后
  输出 token 与时延显著上升，需重新标定并复验 json_object framing，否则会转成传输失败。
- 建议（供 Codex 决策，未执行）：若授权，先做单点 thinking 冒烟（同一 C60 Need 输入，开
  thinking 重放单次 multi-slice 请求，验证能否产出 {ch2,ch56}/{ch36,ch50,ch56} 完整结论），
  再决定是否重跑完整 b11；该运行时变更超出当前 plan 授权，硬停止条件已命中，不自行重跑。
- 下一步需要 Codex 的架构决策（§9/§11.3 已预期的方向）：跨 Need verified drafts 的
  claim fusion / 确定性跨族合成 / 新语义 owner，或 G06/G09 自然 owner Need 的查询对齐，
  或（新增选项）开启 thinking 后的重试验证。
- 未运行：C95（准入未满足）、P3、A/B/C、正式 Gate、Stage 3；未 commit、未 merge；
  未触碰 review/plan/task 文档；`.agent/implementation.md` 为唯一实施证据源。

---

## 15. 人工授权 thinking 重测 b11（2026-08-05）：思考预算机制 + 全量 C60

### 15.1 背景与授权

- 人工明确指令「把这些标定修改了，输出预算加大，开思考来重新测试一遍」——超出 plan §6
  硬停止范围的一次**授权运行时重测**，目标：验证「模型不开思考」是否是 b9/b10 失败的根本
  原因。全部改动限于模型调用运行时（thinking 开关、思考预算、输出/传输预算），未改任何
  prompt 语义、检索/排序/预算结构、domain 公共契约语义、evaluator/Gold/Gate。
- 先前只读诊断（§14.7）已证实：原适配器固定 `chat_template_kwargs.enable_thinking=False`
  （`openai_chat.py:200`），服务端带 `--reasoning-parser qwen3` 但从未启用思考。

### 15.2 实施内容（模型调用运行时标定，全部实测驱动）

1. **按请求 thinking 开关**：`ModelRequest` 新增可选 `enable_thinking` 与
   `thinking_token_budget`（域内增量字段，`ModelRequest.schema.json` 已重新导出）；
   适配器默认 `enable_thinking=True`、显式 False 时关闭、`thinking_token_budget` 透传。
2. **思考预算机制**：该 vLLM 构建原生支持 `thinking_token_budget`（采样器在预算耗尽时
   强制闭合 thinking 块，保证 JSON 收口）。实测标定（重启后的 8002 端点）：
   - 40 切片 + 预算 2000 → 77s / 2083 tokens / VALID 跨族 claim；
   - 40 切片 + 预算 500 + 4096 上限 → 22.3s / 581 tokens / VALID；
   - **170 切片 + 预算 500 + 4096 上限 → 33.9s / 583 tokens / VALID 跨族 claim**；
   - 端点实际可接受 **~48K token 中文 prompt**（旧「~30K 会被拒」标定在重启后过时，
     实测 48,020 prompt_tokens 200 OK，prefill 22s）。
3. **最终标定**（`claim_support.py`）：multi-slice 输出上限 4096、思考预算 **500**、
   序列化请求预算 **30K**（→ 每 Need 1-4 chunk，全 C60 共 **77 次** multi-slice 请求，
   较 15K/8192 时代的 304 次大幅缩减）；单 slice 探针与 whole-claim 验证器保持
   `enable_thinking=False`（探针为布尔充分性判定、验证器为 grammar 模式布尔蕴含，均不需
   思考；实测 thinking 下验证器输出 3/3 次 `{"{"` 损坏）；evaluator 的 post-freeze 语义
   验证请求同样显式 `enable_thinking=False`（`teacher_forced_benchmark_e2e.py`，否则继承
   适配器新默认值在无预算下卡死——b11 首跑即卡在评估阶段，修复后复跑）。
4. **服务端**：b11 首跑探针（thinking 无预算）触发端点 0% GPU 卡死（max_tokens 未强制、
   请求永不闭合），经人工授权以完全相同参数重启 8002（`serve_qwen36_nvfp4.sh`，
   max-model-len 131072 / TP1 / GPU3 / fp8_e4m3 / mtp 投机解码），重启后一切正常。
5. 回归测试：`test_generate_passes_through_thinking_token_budget`、
   `test_semantic_stage_thinking_configuration`（probe=False / multi=True+预算 / verifier=
   False）、`_extract_json_payload`（thinking 模式下偶发 markdown 围栏的确定性剥离，两处
   proposal 解析点接入）；`make quality` **1516 passed, 9 deselected, 100% 覆盖**，strict
   mypy/ruff 全绿。

### 15.3 b11 真实 VAC C60（最终指纹 `sha256:e7c0bee9…`）

- 实验 `stage2m-v31-think-c60-b11-20260804`，目录 `/tmp/ns-stage2m-v31-think-c60-b11-20260804`，
  配方与 b9/b10 一致（resume ch60、real_hybrid、DB、qwen36-27b-nvfp4@8002、`--arms A`）；
  manifest 指纹 = 本地 `make quality` 后计算的同一指纹。
- terminal funnel：`proposal_requests=55`（**49 completed / 6 failed**：5×
  output_length_truncation + 1× invalid_structured_content）、`multi_slice_proposals=27`、
  `multi_slice_verified=20`、`whole_verifier_rejected=4`、`needs_insufficient=4`、
  `facet_not_closed=2`、`verifier_transport_failures=0`、`proposals_rejected=0`（garbage
  guard 零触发——模型未产出 <4 字符/纯标点 claim）、`slices_not_proposed_transport=852`。
- 走廊零回归：workset 族计数 ch2=345/ch36=1512/ch50=476/ch56=839 与 b9/b10/trace4 一致；
  semantic chunks 中 G06 族 Need 每 chunk 携带 {ch2, ch56}、G09 族 Need 每 chunk 携带
  {ch36, ch50, ch56}（每 Need 2-4 chunk，共 77 次请求）。
- 预算/安全：Writer **3996/4000**、Ledger **11995/12000**、`future_leakage_count=0`、
  `future_isolation_failure_count=0`、assembly READY；总运行约 **1.5 小时**（预期 10 小时
  的方案经大 chunk + 小思考预算优化落地）。

### 15.4 verdict 与结论：thinking 不能改变结论，但重定义问题性质

- **G06 = UNTRACEABLE、G09 = MISS**，weighted_coverage **0.0**、mandatory 0.0、
  untraceable_rate 0.222（= b9 水平；b10 的 G06 MISS 在 b11 回到 UNTRACEABLE——语义匹配
  存在但无 accepted 来源，即匹配来自 raw Ledger 切片而非 Writer claim，与 review §7 分析
  一致）。G06/G09 均 0 supported components。
- **thinking 生效证据（机械层面成功）**：20 条语义 verified claim 全部是多章/多句真实合成
  （如「陈长生面对轩辕破时表面平静行礼，但内心并不平静；他让轩辕破尝试兽化右臂……」），
  不再是 b9/b10 的逐字复制/碎片；黑羊 claim 也升级为准确的「莫雨养大的黑羊…亲昵蹭其
  掌心」；JSON 全部收口、0 碎片、0 verifier transport 失败。
- **结论-targeting 失败（残余首层损失）**：模型能合成、但**不产出** G06/G09 所需的完整
  结论——唯一引用 ch2 的 verified claim 是婚约对峙（徐夫人），未与 ch56 组合；ch36 仅剩
  raw R1 三元组（无 verified claim 引用 ch36 的「添名册」切片）；无任何 claim 覆盖
  {ch2, ch56} 或 {ch36, ch50, ch56}。模型按它自己推断的问题作答（黑羊/婚约/落落身世），
  而非金标准要求的结论——这正是「问题与结论错位 / 自然 owner Need 查询对齐」的证据，
  也支持人工提出的「信息充分性与问题对齐」判断：**问题不指向结论时，模型给出的是问题的
  答案，不是 gold 的结论**。
- 交接判定：thinking 假设已被端到端证伪（verdict 不变），但模型「无法合成」被重新定义为
  「合成可达成、结论未定向」——架构决策（claim fusion / 确定性组完整化 / 结论规格注入 /
  G06/G09 自然 owner 查询对齐，§9/§11.3）仍然需要，且新增了更强的证据：机械合成可行，
  缺口在结论规格与问题对齐。
- 未运行：C95（准入未满足）、P3、A/B/C、正式 Gate、Stage 3；未 commit、未 merge；
  未触碰 review/plan/task 文档。
