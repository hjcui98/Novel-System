# OpenCode implementation and evidence

- State: `RETURN_TO_CODEX_REVIEW`（§26：ch32 Curator 反馈修复实现 + 全量质量 + 全新身份真实
  ch32 诊断已闭合；等待 Codex review）

## 26. ch32 Curator 反馈死锁修复（2026-08-10，Codex REPAIR）

### 26.1 修复实现（最小机制，仅两个既有 owner）

- 严格 `resolve_evidence_quotes()` 未改动：歧义/未解析引用继续 fail-closed。
- `EvidenceCandidateGenerator`（`evidence_candidates.py`）：
  - 提取 `_similarity_ratio()` 共享相似度计算（`closest_candidate` 行为不变）；
  - 新增 `copyable_literal_for(quote, candidates, *, max_chars)`：按相似度排序候选，只返回
    **精确字符串经同一 `resolve_evidence_quotes((literal,), candidates)` 唯一解析**且长度
    ≤ max_chars 的 catalog 文本；找不到则返回 None。相似度仅用于反馈，不自动绑定证据。
- `ModelCurator`（`model_curation.py`）：
  - `resolve_quotes` 改为逐 quote 独立解析：反馈与 JSON pointer 绑定到**实际失败的
    quote/index**（多 quote 操作不再把全部 index 当失败、不再用别的 quote 的相似候选）；
  - `_closest_quote_hint` 替换为 `_evidence_quote_feedback(quote, error, candidates)`：
    仅在 `copyable_literal_for` 找到可解析字面量时以 `"copy this exact catalog text verbatim
    as the evidence quote: <literal>"` 广告之；**字面量校验后绝不被 100/240 截断**
    （前缀按 240 预算收紧，literal 完整保留）；无可解析字面量时只返回诚实的
    longer-verbatim/full-sentence 通用指导，不再谎称最近文本可复制。
- 常量：`_QUOTE_HINT_TOTAL_CHARS=240`、`_QUOTE_HINT_PREFIX_CHARS=32`（literal 预算
  240-32-marker，保证完整字面量 + 前缀 ≤ 240）。

### 26.2 回归测试（license-free，全部新增/更新）

- `test_evidence_candidate_generation.py`：
  - `test_copyable_literal_skips_ambiguous_nearest_for_resolver_valid_lower`：最近候选歧义、
    较低排序候选唯一可解析时返回后者；
  - `test_copyable_literal_every_advertised_literal_is_resolver_valid`：每个被广告的字面量都
    是 catalog 原文且被同一 resolver 唯一接受；
  - `test_copyable_literal_never_truncates_validated_literal`：max_chars 只做选择、绝不截断已
    校验字面量（超长唯一字面量返回 None 而非截断前缀）；
  - `test_copyable_literal_none_when_no_literal_resolves`、`..._rejects_non_positive_max_chars`。
- `test_curator_evidence_contract_v2.py`：
  - 更新 `test_closest_quote_hint_advertises_copyable_literal_when_resolvable` 与
    `test_v2_rejection_feedback_advertises_resolver_valid_copyable_literal`：断言广告字面量经
    同一 resolver 唯一解析（取代旧的 "closest catalog text/copy it verbatim" 措辞断言）；
  - `test_v2_multi_quote_rejection_points_at_failing_quote_only`：多 quote 操作的
    `json_pointers == ("/operations/0/evidence_quotes/1",)` 且反馈点名失败 quote；
  - `test_v2_no_resolvable_literal_falls_back_to_generic_guidance`：无可解析字面量时反馈不含
    "copy this exact catalog text"，含 longer verbatim/full-sentence 通用指导；
  - `test_too_short_quote_without_copyable_literal_uses_generic_guidance`。
- 歧义 fail-closed 既有测试（`test_evidence_quote_resolver_ambiguity_fails_closed` 等）全部
  保持通过；`closest_candidate` 相似度测试保持通过。

### 26.3 全量质量与 pre-commit（修复后最终树）

- `make quality`：Ruff lint/format 通过、strict MyPy 通过、Pytest `1650 passed, 9 deselected`
  （252.28s）、`22279 stmts / 6326 branches`、`100.00%` branch coverage（153 files skipped due
  to complete coverage）。
- `PRE_COMMIT_HOME=/tmp/ns-precommit-cache .conda-env/bin/pre-commit run --all-files`：ruff
  check、ruff format、mypy、deterministic pytest 全部通过。
- `git diff --check`：无输出。
- 修复后 executable-source fingerprint（`_code_source_fingerprint` 算法）：
  `sha256:1e7d1f4f48ce86a63a9a808dd1bf8bbb13d2c75be4c437107f329382e7baa2de`（dirty，未提交；
  `git status` 仅 src/novel_agent/services/evidence_candidates.py 与 model_curation.py 及两个
  test 文件改动）。

### 26.4 冻结 ch32 离线复验（修复后，不调模型）

- 用 frozen bundle ch32 文本 + 115 个 catalog 候选复验：
  - `先生，你就收了我吧。` → 广告字面量 `第32章 先生，你就收了我吧\n　　我知道昨夜是我行事
    不妥，我向大家再次道歉，但他对我真的很重要…`，经同一 resolver 唯一解析到
    `78786a855267`，反馈总长 169 ≤ 240；
  - `“拜师礼。` → 广告字面量 `”\n　　落落指着地板上那些事物，说道：“这些是拜师礼。`，唯一解析
    到 `0a068ce8c6d5`，反馈总长 116 ≤ 240；
  - 旧反馈建议的最近文本（`"先生，你就收了我吧。` / `"拜师礼。`）本身仍被同一 resolver 拒绝
    ——证明修复前的死锁确实源于反馈广告了不可解析字面量。

### 26.5 全新身份真实 ch32 诊断（冻结 base context + 真实端点）

- 全新诊断身份（不续跑/不复用正式身份）：experiment `stage2m-repair-ch32-diag-20260810`、
  DB `na_s2m_repair_ch32_diag_v1`（`CREATE DATABASE ... TEMPLATE` 自冻结库复制，正式库
  `na_s2m_phase4_final_apc_v1` 未动）、输出根 `/tmp/ns-stage2m-repair-ch32-diag-20260810`
  （含 `project/` 为冻结 canonical 项目复制，正式输出根未动）。`--resume-project` +
  `--resume-commit sha256:b0061432…（ch31 head）--resume-chapter 31 --max-chapter 32`，
  仅重放 ch32；语义/传输配置与 §25.3 完全一致；修复后 dirty fingerprint
  `1e7d1f4f…ba2de` 经 `--allow-dirty-diagnostic` 如实记录（诊断，非正式）。
- 结果（ch32 提交成功闭合）：
  - 3 次 curator proposal、2 次 rejection、第 3 次 accepted；`poison_loops=0`（旧运行 5 个
    隔离周期 26 次同签名拒绝；本次拒绝签名各不相同，且反馈均广告可解析字面量）；
  - `accepted_candidate_id = candidate.memory-write.teacher-forced.chapter.32.1.63ddcb2a87cb9889`，
    `base_commit = sha256:b00614329469d4c8806bb9a353ab51b47e6aabb6a88445c0046c214fda2848`
    （冻结 base）；
  - `canonical_commit_accepted=true`，`continuation_decision=safe_to_continue`，commit receipt
    artifact `sha256:f985b75669c4736df831eeeef9e8e1b7a103a7a36d737fe43137c53ea0ffe105`，
    checkpoint ref `sha256:72578a45c9512fcdb2a4d1ecdac648ee4f13e28a0c668a8bbaec4d6e56ed9d06`；
  - 进度 manifest：`last_accepted_chapter=32`，commit `sha256:3504a57278d2101515c331d43776d750d4151a117d8b1d47a294a9e56140d011`；
  - accepted change：`changes.model.42ba20081b620bbf34f01a4d`（1 个 obligation operation +
    精确 evidence ref/quote hash）。
- 诊断 run 结束时的 `TeacherForcedBenchmarkError: scenario lifecycle incomplete` 仅因
  `--max-chapter 32` 声明了 C20 但本诊断不重放 ch20（单章走廊语义）；ch32 提交本身成功，
  不构成修复失败。未修改正式实验/DB/output；TIO 未启动。

### 26.6 交回状态（`RETURN_TO_CODEX_REVIEW`）

- 未 commit、未 merge、未 resume 正式身份、未复用正式 DB/output root；未启动 TIO 或新正式
  矩阵。formal §6.3 重跑须等 Codex 接受并形成新 clean executable commit（用户授权）后从 ch0
  以全新身份执行。

## 25. 正式 Phase 4 全量运行（2026-08-09，§6.3 全新身份）

### 25.1 只读身份核查（§6.3 前置）

- HEAD `5ef295fe6a5fedfcef4b02af620dbb988244a58f` 与已接受 manifest 一致；`git diff --check`
  无输出；executable scope（src/scripts/schemas/Makefile/pyproject.toml）`git status` 为空。
- `_code_source_fingerprint()` = `sha256:20daa522f815c88c5ab823d2b03ff896b6751264dd6edac2777a4d93b089b881`
  与 review 接受的 v32 身份逐字节一致；运行只读身份核查不矛盾已接受 manifest，按 §6.3
  不重复 §§5.1-5.5、§6.1、§6.2。
- 冻结 bundle 编译/导入复验：`HumanBenchmarkCompiler().compile()` + `BenchmarkBundleImporter()
  .validate()` 通过；`content_hash = sha256:794b6a91f0b8fb441b5ec5b4af743654411eed7be486c8b6caf0d46e08d5b352`
  与 accepted v32 manifest 的 `benchmark_content_hash` 一致；五个 case（ZTJ-P001..P005，
  history (1,20)/(1,40)/(1,60)/(1,80)/(1,95) → target (21,25)/(41,45)/(61,65)/(81,85)/(96,100)）
  均为 `author_plan_conditioned`，派生 context 编译/绑定通过（§4.1 修复生效）。
- 基础设施：8002 `qwen36-27b-nvfp4`（`--max-num-batched-tokens 2048`、MTP=2、max-model-len
  131072，与接受基线一致）；embedding/reranker 以 accepted v32 manifest 相同的
  `http://127.0.0.1:8281/v1/embeddings` / `http://127.0.0.1:8282/rerank` 启动并通过
  `native_models.py up` 健康校验（本仓 PID 记录缺失，为另一 worktree 启动；已用本仓 native
  infra 在 8281/8282 重新拉起，runtime fingerprint `1d737b51...` 与接受 manifest 一致）；
  OpenSearch 200、PostgreSQL 5432 可达。

### 25.2 证据隔离（§4.0，先于代码变更完成）

- 旧运行 `/tmp/ns-stage2m-phase4-apc-20260807` 及其数据库 `na_s2m_phase4_v1` 保持只读，不
  复用/不覆盖/不升级为正式身份。其五段/Planner-health/并发结论维持
  `DIAGNOSTIC_ONLY_INVALIDATED`（旧公式 global-union + 非 endpoint-global scheduler + 旧
  fingerprint `4a3f3326...`）。原始 prompts/responses/receipts/progress/transport timing、
  六个 content-addressed paired summary（C20 `a9b892f`/`fe15a25`、C40 `0d61e86`/`819fa03`、
  C60 `ada6b4a`、C80 `89d6a88`）、四个 Stage 2M case 报告（C20 `25f7aeb`、C40 `acb89c3`、
  C60 `35f940f`、C80 `b585795`）与旧 top-level 覆盖事实（C20/C40/C60 `e2e_paired_report.json`
  曾被覆盖，不重建）全部保留为诊断证据。
- P004/P005 frozen inputs / Gold / GoldNeedSpec / `frozen_inputs.json` 未修改；未运行旧公式
  P005；未提交、未合并。

### 25.3 全新正式身份（APC 主运行）

- 实验：`stage2m-phase4-final-apc-20260809`
- 输出：`/tmp/ns-stage2m-phase4-final-apc-20260809`（全新，无旧产物）
- 数据库：`na_s2m_phase4_final_apc_v1`（全新创建 + `alembic upgrade head` 到 0007）
- 配方（与 accepted v32 bounded 身份对齐，仅换全新 identity + §6.3.5 固定并发）：
  `--source benchmarks/private/ztj_memory_pilot_v0.1 --arms A --semantic-backend local_openai
  --retrieval-backend real_hybrid --model-base-url http://127.0.0.1:8002/v1 --model
  qwen36-27b-nvfp4 --model-max-output-tokens 8192 --model-max-retries 1
  --support-max-concurrent-needs 2 --support-kv-token-budget 200000 --endpoint-request-limit 1
  --checkpoint-workers 1 --evaluator-max-concurrent-batches 1 --no-support-multi-thinking
  --support-multi-thinking-token-budget 0 --support-multi-max-output-tokens 2048
  --model-scheduling-timeout-seconds 900`
- Manifest 证据：`code_source_dirty=false`、`code_commit=5ef295f`、`code_source_fingerprint
  =20daa...881`、`run_config_hash=sha256:03f0e5be...`（与 accepted v32 serial/concurrent
  完全一致）、`execution_config`（needs=2, endpoint=1, evaluator=1, checkpoint=1, KV
  configured 200000/effective 160000, reserve 0.2, multi thinking=false/0/2048,
  scheduling timeout 900）、Writer 4000、Ledger 12000、`gate_metric_formula.v2`、
  `benchmark_content_hash=794b6a91...`。
- 启动时间：2026-08-09 ~22:19 +08:00；进程 3569131（setsid/nohup，日志
  `/tmp/ns-stage2m-phase4-final-apc-20260809.run.log`）。

### 25.4 运行进程记录（2026-08-09 晚至 2026-08-10）

- 全量 replay 从 ch0 前进，ch9/ch29/ch32 多次触发设计的 `TeacherForcedControlledPause`
  （curator 证据门 fail-closed 隔离 + `--resume` 重试，与旧运行 ch64 同型机制）。每次隔离都
  持久化完整 quarantine package、proposal attempt/rejection/feedback receipts。
- ch9 与 ch29 的重试成功（隔离后重跑提交）；ch32 连续 5 个隔离周期均失败，共 26 次
  `CURATOR_PROPOSAL_INVALID_EVIDENCE` 拒绝，且全部为同一失败模式（见 §25.5）。
- 运行期间基础设施事件：
  1. 首个 `--resume` 段在 ch32 投影构建时触发 OpenSearch `max_shards_per_node` 瞬时上限
     （1600/1600 LOCAL_ONLY shards）。本地单节点 dev cluster 已积累 895 个历史 index
     （1599 shards，来自历次实验；projection 本身按设计 primary-only，见
     `search_retrieval.py:284-291`）。已把瞬时上限提升到 3200（`PUT /_cluster/settings`
     transient，可逆、不删任何旧证据），随后 replay 正常继续。该上限不属于 repo 管理配置，
     不影响 benchmark 语义/预算/身份。
  2. 8002 endpoint 健康全程 200；本仓 embedding/reranker 服务在 8281/8282 拉起后健康稳定。

### 25.5 ch32 阻断根因：curator 反馈提示自相矛盾（首现实现缺陷）

- 章 32 原文同时包含「第32章 先生，你就收了我吧」（章节标题）与对话「"先生，你就收了我吧。」，
  且「拜师礼。」以 `"这些是拜师礼。"` 和独立 `"拜师礼。"` 两种形式出现。`resolve_evidence_quotes`
  （`evidence_candidates.py:98-195`，accepted v32 代码）对任一短引用都判定为 ambiguous
  （2 candidates）——这是正确的 fail-closed 行为。
- 问题在 `_closest_quote_hint`（`model_curation.py:917-943`）：反馈让模型「把最近 catalog 文本
  逐字复制为引用」。但最近候选本身就是不可解析的文本：对 `先生，你就收了我吧。`，`closest_candidate`
  返回对话候选 `"先生，你就收了我吧。`（ratio 1.0），其逐字复制仍触发同一 ambiguous 拒绝；
  对 `"拜师礼。` 同理。模型照提示逐字复制 → 再次被拒 → 同一提示，形成确定性死锁。
- 离线复验（accepted 代码、frozen 原文）：可解析的合法引用确实存在——`第32章 先生，你就收了我吧`
  与 `我知道昨夜是我行事不妥，我向大家再次道歉，但他对我真的很重要` 均唯一解析到 candidate
  `78786a855267`。因此章 32 在原理上可过；但反馈循环从未把模型引向这些形式。
- 回归证据：旧运行（`420e163` / fingerprint `4a3f3326`）在宽松旧解析器下提交过 ch32
  （completed_chapters 含 32）。严格 `resolve_evidence_quotes`（151 行新增）只在最终接受代码
  `5ef295f`（fingerprint `20daa...`）中引入；bounded 准入证据使用冻结 Planner artifact + C40
  单点评估，从未真实 replay ch0-31，因此该严格解析器在完整 replay 走廊上是首现运行。ch32 的
  反馈死锁属于正式运行才暴露的第一实现缺陷。
- 计数证据：5 个 ch32 quarantine package（`9346a70...` ×5）+ 1 个 ch9（`30cd1b...`）；
  26 条 `invalid_evidence` 拒绝；唯一反馈消息 3 条（全部指向不可解析的短引用）。ch32 自
  `last_accepted_chapter=31` 起无法继续提交，canonical chain 中断 → C40/C60/C80/C95 走廊
  全部不可达 → P002-P005 无法评估，正式矩阵无法完成。

### 25.6 停止与交回（§6.3.11 / §0.1）

- 按 `.agent/plan.md` §0.1 与 §6.3.11：正式矩阵的有效执行需要改动代码（`_closest_quote_hint`
  必须只建议可解析引用，或解析器需要返回能唯一解析的较长 span），而正式运行身份绑定接受代码
  fingerprint；在运行中修复会改变实验身份，属明确禁止的「repair in place」。因此停止，交回
  Codex 决定技术方向。
- 未修复代码、未改 prompt/公式/预算/Gold/阈值；未重用旧身份；未启动 TIO ablation（同一 replay
  走廊会在 ch32 遭遇同一死锁，不重复无效运行）。manifest 的 `code_source_fingerprint` 保持
  `20daa...881` 不变。
- 实验身份保留：`stage2m-phase4-final-apc-20260809` / `na_s2m_phase4_final_apc_v1` /
  `/tmp/ns-stage2m-phase4-final-apc-20260809`；ch0-31 canonical commits 已持久化（32 章完成）。
- 建议给 Codex 的技术方向（仅证据，不替 Codex 决策）：
  1. `_closest_quote_hint` 只返回「逐字复制后可解析」的 catalog span（先对候选做
     `resolve_evidence_quotes` 自检，失败则提升到更长窗口/标题前缀），消除反馈死锁；
  2. 或允许 curator 在 N 次同因隔离后按 typed reason 显式跳过该 operation（不改变 evidence
     合同，只改变走廊重试语义——属语义设计变更，须 Codex 定）；
  3. 修复后以新的 experiment/DB/output 身份重跑正式矩阵（§6.3.6/7：不得复用本身份）。

- State（旧行保留为历史）：`RETURN_TO_CODEX`（§15：人工授权 thinking 重测 b11 完成——模型合成层被证明**机械上能**
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

---

## 16. 新执行周期：Need 管道审计 Phase 0A/0B（2026-08-07）

- 依据：`.agent/need_pipeline_audit_and_semantics.md`（Codex 批准版 v2，状态
  "Approved with implementation prerequisites（Phase 0A 可立即执行，其余需补齐前置条件）"）。
  本周期执行 **Phase 0A（语义与输入链路）全部 8 项 + P0-1（allow_plan 拆分）+ P0-2
  （AuthorPlanningContext 窄通道）**，并完成 **Phase 0B（APC 模板链路冒烟）** 验收。
  Phase 0C（predicates/EVENT query/query_hints 三个独立 patch）与 Phase 1（LLM Planner）
  未包含——按文档要求需前置条件（R1 predicates 语义审计 / LLM Planner 数据契约）。
- 代码基线：commit `420e163`（工作树 dirty，未 commit）；代码指纹
  `sha256:08ed9a35812f1a26dba1a379819c244153ebc127634bb1f258a6739bbb06bc52`
  （`_code_source_fingerprint` 算法，含全部 dirty 改动）。
- 质量门：`make quality` **1527 passed, 9 deselected, 100% 语句/分支覆盖**，
  strict mypy/ruff 全绿（`Success: no issues found in 284 source files`）。

### 16.1 改动清单（42 文件，+1980/-217）

| 层 | 文件 | 内容 |
|---|---|---|
| domain | `benchmark.py` | 新增 `VisibleOutlineNode`、`AuthorPlanningContext`；`BenchmarkCaseManifest` 新增 `information_profile/task_intent/planning_context_ref/planning_context_hash`；`BenchmarkBundle` 新增 `planning_contexts` + source_hash 唯一性校验 |
| domain | `writer_context.py` | `BenchmarkTaskContract` 新增 `task_intent/planning_context_ref/planning_context_hash` |
| domain | `memory.py` | `Stage1MemoryNeed` 新增 `planner_may_read_plan/retrieval_may_return_plan/claim_may_cite_plan/legacy_allow_plan`；`allow_plan` 标记 deprecated（校验强制等于 retrieval 策略）；plan-derived facet 校验迁移到 `claim_may_cite_plan` |
| 编译 | `human_benchmark_compiler.py` | 新增 `_compile_planning_context()`（task/mode/visible_outline/target_plan → 类型化 `AuthorPlanningContext`，`source_hash` 绑定原始 YAML 字段）；`compile()` 填充 manifest 与 bundle |
| 契约 | `memory_benchmark_contract.py` | `build_safe_task_contract` 组合式构造：固定安全契约（task_text 不变）+ 规范化 `task_intent` 字段；VAC 拒绝非空 task_intent（fail-closed）；`assert_safe_public_payload` 覆盖 task_intent；`build_public_checkpoint_case` 透传 refs；`_PRIVATE_FIELD_FRAGMENTS`/taint/hash 全部保留 |
| 导入 | `benchmark_importer.py` | `_validate_case` 新增 planning context ref↔bundle 绑定校验（ref 哈希、profile 一致性、孤儿 task_intent 拒绝） |
| 检索/claim 门 | `search_retrieval.py:565`、`claim_support.py:3125`、`controller_legal_actions.py:33`、`runtime/memory_controller.py:460`、`tools/retrieval.py:104` | `allow_plan` 消费点全部迁移到分层策略（检索→`retrieval_may_return_plan`；claim→`claim_may_cite_plan`；工具/controller→intent OR `retrieval_may_return_plan`） |
| 生产者 | `task_conditioned_need_generation.py`（add() 填充 4 字段，版本 v21→v22）、`stage1_benchmark.py`（两处 plan 需要）、`stage2_paired_pilot.py`（`_plan_needs` + `_scope_needs` 重写 + 版本 v0.4→v0.5） | plan 通道需要（`plan_obligation`/`plan_conditioned_history`）显式获得三层 True；历史需要全 False |
| 运行 | `teacher_forced_benchmark_e2e.py` `freeze()` | APC 时透传 manifest 的 task_intent/planning refs 到 `build_public_checkpoint_case` |
| CLI | `run_stage2_teacher_forced_e2e.py` | `--information-profile` 默认改为 None，编译后从 `AuthorPlanningContext.profile` 解析（`_default_information_profile`）；显式传参仍可覆盖（VAC 对照合法） |
| schemas | stage1/stage2 重导出（13 个 schema JSON 变更，0A.7） | — |
| 数据 | P001-P005 input.yaml（0A.8） | 审计结论：5 个 case 的 `task/mode/visible_outline/target_plan` 字段**全部齐全**（均为 `mode: plan_conditioned`）；编译期 fail-closed 校验 + 5/5 context 编译测试覆盖 |

### 16.2 Phase 0A 验收（Gate 0）逐条证据

1. **APC PlanningContext 正确进入 Planner 上游**：编译器产出 5 个类型化 context（P003 例：
   task_intent=「输出写 61-65 章所需的长距记忆…」、2 outline nodes、5 chapter goals）；
   manifest 保存 `information_profile=author_plan_conditioned` + refs；`freeze()` →
   `build_public_checkpoint_case` → `BenchmarkTaskContract.task_intent` 全程透传（0B 冒烟实测
   public.task_contract.task_intent 非空）。
2. **三层策略注入**：`_scope_needs` 在 APC 注入 `planner_may_read_plan=True` 于全部 Need，
   `retrieval_may_return_plan=False`/`claim_may_cite_plan=False` 于历史走廊；显式 plan 通道
   Need（按 need_type 确定性判定，`plan_obligation`/`plan_conditioned_history`）保留三层
   True——与旧 `allow_plan=True` 行为逐位等价（Gate 0「暂不改变 Need 生成结果」）。
3. **observed-only evidence、零 leakage**：0B 冒烟实测 P003 APC：plan-labeled 候选单元
   **仅**出现在 `need.stage2m.plan.*` 通道 Need 的 trace 中（7 个 goal + 2 个 outline Need，
   共 47 个 plan 单元）；历史 Need 零 plan 单元；`future_leakage_count=0`；
   plan_node_ids 仅出现在 plan 通道的 ledger entry 与 support group。
4. **hash/profile 正确**：VAC 路径 task_text/task_contract 与旧行为逐字节一致（task_intent
   为空时不进入 task_text，模板哈希不变）；`verify_public_checkpoint_case` 通过；
   VAC + task_intent → `PublicBenchmarkTaintError`（盲式档位 fail-closed）。
5. **Need 生成结果不变**：模板链路产出的 Need 集合与策略字段与旧 `allow_plan` 语义一致
   （旧 1525→新 1527 测试全部通过，其中仅 3 处测试因字段新增而更新构造方式）。

### 16.3 Phase 0B 验收（APC 模板链路冒烟）

确定性冒烟（scripted_smoke、无模型调用、真实 P003 数据）：compile → planning context →
public case（含 task_intent）→ `resolve_state_case(APC)` → 29 条 retrieval trace、
assembly **READY**、`quality_eligible=True`、`future_leakage_count=0`。

- 0B.1 plan 可见 ✓（plan-related Need 生成：`need.stage2m.plan.goal.ZTJ-P003.6X` +
  outline 2 个）
- 0B.2 historical retrieval observed-only ✓（plan 单元只进 plan 通道 trace）
- 0B.3 claim 不引用 plan ✓（plan_node_ids 只在 plan-obligation support group/ledger）
- 0B.4 leakage=0 ✓
- 0B.5 VAC 不见 plan ✓（`_scope_needs` 强制全 False + `plan_root_ref=None` +
  `build_safe_task_contract` 拒绝 task_intent）

该冒烟固化为契约测试
`tests/contract/test_memory_benchmark_information_profiles.py::test_apc_template_chain_keeps_plan_to_plan_channel_only`
（真实 PILOT 数据，~5s）。

### 16.4 新增回归测试（license-free，无 Gold/实体/章节号硬编码）

| 测试 | 覆盖 |
|---|---|
| `test_human_benchmark_compiler.py::test_planning_contexts_are_compiled_bound_and_typed` | 5/5 context 编译、ref/hash 绑定、outline/goal 与 PlanRoot 一致 |
| `...::test_planning_context_compilation_is_fail_closed_on_raw_yaml_fields` | 非法 mode / 空 task / 非 list target_plan → 编译错误 |
| `...::test_planning_context_rejects_invalid_ranges_and_duplicate_ids` | 范围、重复 goal/outline id、node_type 校验 |
| `test_memory_benchmark_taint_boundary.py::test_task_contract_composes_safe_contract_with_normalized_task_intent` | 组合契约、VAC 拒绝、固定计数 taint、模板版本不变 |
| `...::test_public_checkpoint_case_forwards_planning_context_refs` | APC 透传 + VAC 剥离 + hash 绑定 |
| `test_domain_remaining_edges.py::test_memory_need_enforces_layered_plan_policies` | 4 字段一致性、retrieval⇒planner 蕴含、facet⇒claim 蕴含 |
| `test_stage2_paired_pilot.py::test_scope_needs_injects_run_level_plan_policy` | APC/VAC 注入矩阵 |
| `test_stage1_importer_negative_paths.py::test_planning_context_ref_is_bound_to_bundle_and_profile` + `test_bundle_rejects_duplicate_planning_context_source_hashes` | 导入层 fail-closed 绑定 |
| `test_stage2_experiment_manifest.py::test_default_information_profile_resolves_from_planning_contexts` | CLI 档位默认解析 |
| 契约测试（0B） | plan 通道隔离 |

### 16.5 安全/预算与非目标

- Writer 4000 / Ledger 12000 / ADR-0004 / Gate 公式 / evaluator / Stage 3 未触碰。
- `_PRIVATE_FIELD_FRAGMENTS` 未删除；taint/hash 逻辑全部保留；plan 原文从不进入
  public payload（仅类型化 `AuthorPlanningContext` + 字段名 `planning_context_ref/hash`
  不触发 taint，且 VAC 侧显式拒绝）。
- 未运行任何模型调用（Phase 0A/0B 全确定性；LLM 端点 8002 未使用）。
- 未 commit、未 merge；未触碰 `.agent/task.md`/`.agent/plan.md`/`.agent/review.md` 与
  架构/设计/ADR/状态文档。

### 16.6 遗留与下阶段前置条件（供 Codex 决策）

- **Phase 0C-1（predicates）**：按文档需先审计 R1 对 `predicates` 的语义（AND/OR/联合/
  eligibility）再按 need_type 填充——本周期未实施。
- **Phase 0C-2（EVENT query）/ 0C-3（query_hints）**：legacy template path 修复，独立
  patch。
- **Phase 1（LLM Planner）**：数据契约（`domain/planning_memory.py`：
  `PlannedNeedDraft/GroundedNeedDraft/PlannerWorldSummary/PlannerArtifactMetadata`）、
  `semantic_question` 等 Stage1MemoryNeed 新字段、planner/grounder/validator/query_compiler
  四个新文件。`AuthorPlanningContext`（含 `planner_may_read_plan`）已就绪作为 Planner 输入。
- **Phase 3 评测拆分（gold_need_spec）与 Phase 4 全量重跑**：依赖 Phase 1 完成。
- P004/P005 input.yaml 字段审计完成（0A.8），可按文档冻结。

### 16.7 关键命令与产物

```bash
make quality            # 1527 passed, 9 deselected, 100% 语句/分支覆盖
.conda-env/bin/mypy     # Success: no issues found in 284 source files
.conda-env/bin/ruff check . && .conda-env/bin/ruff format --check .   # clean
.conda-env/bin/pytest tests/contract/test_memory_benchmark_information_profiles.py --no-cov -q  # 0B 冒烟
```

Phase 0A 验收状态：**Gate 0 全部通过（确定性证据）**；Phase 0B 验收状态：**全部通过**。
手over 状态：等待 Codex 审查 Phase 0A/0B 证据并决定 Phase 0C/1 前置条件与放行。

---

## 17. 继续执行：Phase 0C + Phase 1 + Phase 2 + Phase 3（2026-08-07 续）

在 §16（Phase 0A/0B）基础上继续按 `need_pipeline_audit_and_semantics.md` 执行。
质量门：`make quality` **1566 passed, 9 deselected, 100% 语句/分支覆盖**，strict mypy/ruff 全绿
（291 个源文件）。代码指纹随改动变化，以最终 git 记录为准（未 commit）。

### 17.1 Phase 0C：三个独立 patch（全部完成）

| Patch | 内容 | 验收 |
|---|---|---|
| 0C-1 predicates | **R1 predicates 语义审计结论**：SQL `predicate IN (...)` = **OR 语义**、与 entity_ids **AND 组合**、单次联合过滤（`r1.py:161-162`）、eligibility+过滤双重角色（`_r1_eligible`）；按 §3.4 分 need_type 填充：`current_state`→实体状态谓词[:16]、`capability_boundary`→能力关键词谓词、`knowledge_boundary`→knowledge 白名单谓词、`relationship_emotion`→关系谓词（非状态谓词）、`long_range_callback`→空 | OR 语义天然不引入过约束（仅收紧到相关谓词集合） |
| 0C-2 EVENT query | fallback 模板：`query = event_type + participant_labels + effect_refs→state values 表面词`（`task_conditioned_need_generation.py` EVENT 分支） | EVENT Need 查询不再退化为裸 `event_type` |
| 0C-3 query_hints | Arm A 消费激活：`search_retrieval.py` BM25 把 `query_hints`（去重、排除与主查询相同项）编译为辅助 multi_match 子句（主 ^1.0/^1.2/^3.0，hint ^0.6/^0.7/^1.8） | 辅助 BM25 查询生效（测试断言 3-clause vs 1-clause） |

新增测试：`test_entity_need_predicates_are_filled_by_need_type`、`test_event_need_query_is_enriched_with_participants_and_effects`、
`test_predicates_by_keywords_helper_filters_and_limits`、`test_bm25_consumes_query_hints_as_auxiliary_clauses`。

### 17.2 Phase 1：LLM Need Planner + Grounder + Validator（全部完成）

**新文件**：
- `domain/planning_memory.py`：`PlannedNeedDraft`（无图谱 ID）、`EntityMention/RelationMention`、`GroundedEntityMention/GroundedRelationMention/GroundedNeedDraft`、`GroundingStatus`、`PlannerWorldSummary`（确定性有界世界投影）、`PlannerArtifactMetadata`（run 级 lineage：model/revision/prompt_version+hash/output_schema_version/temperature/seed/planning_context_hash/world_summary_hash/raw_response_hash/validated_need_set_hash/fallback_used/usage）、`PlannerRunResult`（含 `PLANNER_FALLBACK` 显式标记）
- `services/plan_conditioned_need_planner.py`：LLM Planner（backward chaining prompt、json_object 输出、`PlannedNeedDraft` 解析、重试+fallback、`PlannerWorldSummaryBuilder`）
- `services/need_draft_grounder.py`：确定性 grounding（exact alias → fuzzy → relation-context 消歧 → AMBIGUOUS/UNRESOLVED 显式标记）
- `services/need_validator.py`：时间边界（trigger chapters ⊆ target range）、事实化检查（plan_goal_as_fact）、grounding 验收（无锚点拒绝）、去重、预算、facet→need_type 映射、draft_id 消毒

**既有文件**：
- `domain/memory.py`：`Stage1MemoryNeed` 新增 `semantic_question/trigger_plan_chapters/planner_artifact_ref/planned_draft_id/validated_need_set_hash` + 完整 lineage 校验
- `task_conditioned_need_generation.py`：APC+plan 非空 + gateway 存在时走 Planner→Grounder→Validator 链；失败回退模板并显式标记 `PLANNER_FALLBACK`；`NeedGenerationResult` 新增 `planner_metadata/fallback_used/planner_fallback_reason`；生成器版本 v22
- `task_focus.py`：`extend()` 接口（grounded 实体回填 FocusSet，有界）
- `stage2_paired_pilot.resolve_state_case` + `teacher_forced_benchmark_e2e.freeze`：透传 planner gateway 与编译后的 `AuthorPlanningContext`
- schemas 重导出（13 个 planning_memory schema）

**Phase 1 真实 LLM 验收（Gate 1，P003，qwen36-27b-nvfp4@8002）**：
- 实验目录 `/tmp/ns-stage2m-phase1-planner-p003-20260807/`（planner_run_v3.json、demo_world_p003.json、world_summary.json、prompt.txt）
- 运行 113.2s、**status READY、fallback False**、input 1157 + output 2834 tokens
- **8 条 planner need** 全部 grounding 成功（陈长生/徐有容/秋山君/莫雨/黑龙 等实体），目标章节 61-65 **全覆盖**：
  - `d_61_01` long_range_callback（黑龙敌意来源）、`d_61_02` capability_boundary（伤势）、`d_62_01/02` capability_boundary + relationship_emotion（桐宫/莫雨）、`d_63_01/02` unresolved_obligation（提亲/婚约见证）、`d_64_01/d_65_01` knowledge_boundary（知情边界）
- 完整 lineage：metadata 17 字段 + 每 need 的 `planner_artifact_ref == content_id(metadata)`、`planned_draft_id`、`validated_need_set_hash` 一致
- 诊断驱动的两轮 prompt 修复：(1) 防照抄章节目标 + 强制实体锚点（空世界时模型正确拒答空 drafts——约束生效证据）；(2) 强制简洁输出（模型过度冗长导致 4096+ tokens 截断 → 加 60/40/30 字限制，max_output_tokens 8192→4096 + timeout 420s）
- **防过拟合合规**：planner prompt 无任何 Gold ID/章节号/角色特化（§10.8）；演示 world 由 gold facts 构建（实验数据，非生产代码）

### 17.3 Phase 2：Per-channel Query Compilation（全部完成）

- `domain/planning_memory.py`：`RetrievalQueryBundle`（semantic_query/lexical_queries/exact_entity_ids/exact_predicates/graph_seeds/graph_relations/time_scope/excluded_information_labels + 唯一性校验）
- `services/need_query_compiler.py`：`NeedQueryCompiler.compile(need)`——`semantic_query = semantic_question or query_text`；`lexical_queries = (query_text, *query_hints) 去重`；`excluded_information_labels = ("plan",) unless retrieval_may_return_plan`；`graph_seeds = entity_ids`
- **路由集成（取交集）**：`ROUTES[query_intent]` 定通道集；bundle 供各通道查询——`search_retrieval.py` Dense→`semantic_query`（knn 向量）、BM25→`lexical_queries`（不再裸用 query_text）、observed 过滤取自 bundle；`retrieval.py` RerankService→`semantic_query`；`r1.py` TYPED_GRAPH→`graph_seeds + exact_predicates`（R1 exact 与 bundle.exact_* 同源）
- **2.4 检索 trace 分层**：`RetrievalTrace.direct_unit_ids`（direct 检索选中单元）与 corridor 扩张单元（raw_evidence_spans/style_or_reference_optional）可区分
- 测试：`test_query_compiler_builds_per_channel_bundle`、`test_dense_uses_semantic_question_from_query_bundle`、`test_exact_current_state...` 断言 direct_unit_ids

### 17.4 Phase 3：评测拆分（全部完成）

- **3.2 gold_need_spec 数据**：P001-P005 各新增 `gold_need_spec.yaml`（47 条 gold 的 required_need_scopes/required_entities/required_facets，类型→scope/facet 确定性映射；P003 G06/G09 按 §5.5 标为 `plan_dependent`，其余 `blind_recoverable`）；compiler `_gold_need_specs()` 读取并存入 manifest（fail-closed：未知 gold/非法 blindness/非法结构均报错）
- **3.1/3.3/3.4/3.5 五段指标**：`MemoryBenchmarkEvaluator.evaluate_five_segments()` 新方法（**不动现有 evaluate 逻辑**，§10.3）：
  1. Plan Goal Coverage（trigger_plan_chapters + plan 通道 query/semantic_question == goal summary）
  2. Need Recall（needs 的 scope/entity/facet 覆盖 gold_need_spec 组件，确定性计算）
  3. Evidence Recall（复用 GoldEvidenceMatcher）
  4. Completion/Claim Accuracy（复用 weighted_coverage）
  5. **Leakage 独立**（future_leakage_count + `information_label=="plan"` 引用计数：plan_citation_count / plan_leakage_count——非 plan 通道 need 引用的 plan 条目）
- `GoldBlindness` 分类（blind_recoverable/plan_dependent/hindsight_only）；`MemoryBenchmarkEvaluationReport.five_segments` 可选字段；`PairedContextComparison.generated_needs`（冻结产物携带 needs）；E2E evaluate 调用点接线

**Phase 3 端到端验证（P003 模板链路 + scripted backend）**：`plan_goals_total=5, plan_goals_covered=5 (1.0)`、`need_recall=18/32 (0.5625)`、`evidence_recall=0.357`、`future_leakage=0`、`plan_citation_count=7`（全为 plan 通道）、`plan_leakage=0`。

### 17.5 遗留与 Phase 4 状态

- **Phase 4（P001-P005 全量重跑）未启动**：需要真实基础设施长时运行（teacher-forced replay + real_hybrid 走廊 + LLM，单 case 约 1-1.5h，5 case 预计数小时）。新架构三层 Gate 0-3 已全部具备确定性验收证据；正式重跑建议由 Codex 批准后作为独立长时任务执行（可在本会话继续或单独安排）。
- 新增数据文件 `gold_need_spec.yaml`（P004/P005 为 Phase 0A 类数据补全，非按运行结果修改；如 Codex 认为违反冻结语义，可回退为评估时人工标注）。
- 关键命令：`make quality`（1566 passed / 100%）；真实 LLM 验收产物见 §17.2。

---

## 16. 并发调度改造 C0-C3（2026-08-07，依据 `.agent/concurrent_scheduling_plan.md` 最终决策版）

### 16.1 准入与 C0（调用 DAG 审计）

- 准入：语义改造质量门全绿（1566 passed 100% 覆盖）后开始。
- C0 结论（新架构调用 DAG）：LLM Need Planner 1 次/run（sync asyncio.run，420s/4096 输出，
  max_retries=1）；走廊 proposal+verify 为 need 级独立（数量取决于 Planner 输出，旧 C60 为
  55+24）；评估器语义验证 batch_size=2 串行。主并发单位 = 独立 Need pipeline。

### 16.2 C2（Evaluator batch 间并发，已完成）

- `ModelSemanticSupportVerifier` 新增 `max_concurrent_batches=4`（1-8 校验）：batch 循环体提取为
  `_verify_batch`，`asyncio.Semaphore` + `asyncio.gather` 并发，按 batch 序归并 judgments/calls。
- 语义不变：prompt 内容/重试策略/fail-closed 行为/request_id 完全保持。
- 测试：并发 vs 串行结果与调用顺序全等 + in-flight 计数验证（+3）。

### 16.3 C1（真实 workload 服务基准，已完成）

`/tmp/opencode/bench_c1_workload.py`（b11 真实分布 8K/16K/25K/verify 混合，8002 端点
max-num-seqs=8 + MTP=2 + prefix caching，输出 /tmp/opencode/bench_c1_c1_round1.json）：

| 并发 | wall | decode tok/s | peak KV | TTFT mean | errors |
|---|---|---|---|---|---|
| 1 | 996.5s | 24.2 | 20% | 6.8s | 0 |
| 2 | 601.7s | 45.6 | 38% | 8.8s | 0 |
| 4 | 366.8s | 75.7 | 64% | 9.1s | 0 |
| 6 | 340.2s | 87.1 | 92% | 13.5s | 0 |
| 8 | 238.0s | 97.1 | 98% | 36.9s | 0 |

结论：串行→8 并发 **4.2x**；8×25K 极端请求 KV 达 98%（验证了 KV-token 双限流必要性）；
真实走廊均值 16K → 8 并发约 60-70% KV。应用层推荐：`max_concurrent_needs=4`（保守，
2.7x/KV 64%）至 6（激进）；KV budget ≈ 200K 序列 token + 20% reserve。

### 16.4 C3（走廊独立 Need 并发 + KV-token 双限流，已完成）

`TrustedClaimSupportProducer` 新增：
- `max_concurrent_needs`（默认 1=串行，1-8 校验）：`for entry in public_needs` 循环提取为
  `_produce_need_pipeline` worker；`ThreadPoolExecutor` 并发；**按 need 顺序确定性归并**
  （funnel 字段级求和、groups/variants/receipts/attestations 顺序拼接、进度事件按 need 序
  重放并重写全局 batch_index、诊断码合并去重）。
- `max_inflight_kv_tokens`（默认 None=关闭）：`_acquire_kv_capacity`/`_release_kv_capacity`
  双限流接入 proposal×2 + verifier 三个模型调用点；等待语义 WAITING_FOR_CAPACITY；
  超预算单请求放行（防死锁，计数不裁剪，vLLM 自身容量调度兜底）。
- 线程安全：`_verification_cache` 加锁；`_record_progress`/诊断码经线程局部收集（worker 内
  不直接写共享状态）；`_emit_verified_claim` 的 groups 按 need 隔离已确认（covered_facets
  只读本 need 组）→ need 间零数据依赖。
- 测试（+5）：并发 vs 串行全等（funnel/group/receipt/事件序/诊断，时间戳与内容寻址 hash
  归一化）、串行路径 max_inflight==1、并发 max_inflight≥2、KV 限流等待/释放/防死锁、
  budget 校验。
- 质量门：**1576 passed, 9 deselected, 100% 覆盖**，mypy/ruff/format 全绿（每次改动后跑）。

### 16.5 未完成（留给后续）

- C4（多 case 并行）、C5（可选 async 化）：按计划文档在 C3 稳定后启动。
- 真实走廊端到端并发验证（b12 单 case，APC 档）：需用户授权一次真实运行，
  验证 funnel/verdict 与串行基线语义一致 + wall time 提升（C1 预期 4.2x 上限）。

---

## 18. Phase 4 前置与启动：TASK_INTENT_ONLY 档位 + 冻结 + 传输修复（2026-08-07 续）

### 18.1 TASK_INTENT_ONLY 消融档位（文档 §5.2/D2，已实现）

- `BenchmarkInformationProfile` 新增 `TASK_INTENT_ONLY = "task_intent_only"`
- `build_safe_task_contract`：TASK_INTENT_ONLY 接受 task_intent，profile_rule = "不得使用作者计划节点或任何未来材料; 但可以使用任务意图作为检索方向。"；VAC 仍拒绝 task_intent
- `TaskPlanConditionedNeedGenerator`：TASK_INTENT_ONLY 与 VAC 同规则拒绝 PlanRoot（"cannot receive a future PlanRoot"）
- `TaskFocusExtractor`：TASK_INTENT_ONLY/APC 把 `task_intent` 折入 TASK 源焦点匹配（命名实体可锚定；消融目的：分离"任务意图增益"与"章节计划增益"）
- `stage2_paired_pilot`：TASK_INTENT_ONLY 无 plan、WRITER_SAFE、三层策略全 False（走既有非 APC 分支，无需改动）
- `freeze()`：TASK_INTENT_ONLY 传递 task_intent，不传 plan refs
- `memory_benchmark_reporting`：冻结分母契约仅覆盖 VAC/APC 两个正式档位；消融档位聚合不虚构分母
- `human_benchmark_compiler._PROFILE_BY_MODE` 增加 `task_intent_only` 模式映射
- 测试：`test_task_intent_only_profile_uses_intent_but_never_plan`（契约）+ 既有 profile 计数测试更新（2→3）

### 18.2 P004/P005 冻结（文档 §10.7，已生成）

`benchmarks/private/ztj_memory_pilot_v0.1/frozen_inputs.json`：P001-P005 各 case 的
`input.yaml / gold.yaml / gold_need_spec.yaml / manifest.json` SHA-256 逐文件哈希 +
bundle content hash（`f359d151...`）。冻结后不再按运行结果修改输入或 Gold。

### 18.3 Teacher-forced agent 传输配置修复（证据驱动，全量重跑前置）

首次全量重跑（非 resume）暴露：genesis 的 bootstrap planner 走 `generate_structured`
（grammar 模式）+ `timeout_seconds=300` + thinking 默认开 → qwen 本地端点 300s 内无法
完成（TimeoutError；与 b11 走廊发现的 grammar/thinking 问题同类）。修复
`TeacherForcedBenchmarkE2ERunner._request()`：`max_output_tokens=4096`、
`timeout_seconds=420`、`enable_thinking=False`——与走廊已验证配置一致，影响全部
teacher-forced agent 请求（genesis planner/curator + 每章 replay curator）。

### 18.4 并发优化（C3 接线 + C4 走廊异步化）

- **C3 接线（重大发现）**：`TrustedClaimSupportProducer` 的
  `max_concurrent_needs/max_inflight_kv_tokens` 机制此前已实现但**生产 E2E 路径未接线**
  （`stage2_paired_pilot.py` 构造 producer 时用默认值 1=串行）。本轮接通：CLI
  `--support-max-concurrent-needs(4) / --support-kv-token-budget(200000) /
  --endpoint-request-limit(8)` → runner → freezer → resolve_state_case → producer。
- **共享 admission controller**：新 `services/model_request_admission.py`
  `ModelRequestAdmissionController`（request-count + KV-token 双限流，跨走廊共享），
  producer 注入后委托 acquire/release；`_kv_scheduler_lock` 保留为无注入时 fallback。
- **C4 走廊异步化**：`TeacherForcedScenarioRunner` 的 checkpoint 走廊
  （freeze→score→reveal）在 `ThreadPoolExecutor(checkpoint_workers=2)` 后台执行，replay
  不再停等走廊；builder 变更加锁；`record_support_progress`/evaluator 结果列表加锁；
  走廊结果按 checkpoint 顺序收集；`ScenarioStateError` 原样传播。
- 测试：`test_model_request_admission.py`（6 个并发/预算用例）、
  `test_runner_runs_corridor_concurrently_with_replay`（阻塞走廊 + replay 继续推进）、
  `test_producer_delegates_capacity_to_shared_controller` 等。
- 服务端 batched-tokens 复测结论已记录于 `concurrent_scheduling_plan.md` §0.1
  （2048 维持不动；16384 在 15K prompt 下 CUDA OOM）。

### 18.5 Phase 4 全量重跑（APC 主运行，进行中）

- 实验：`stage2m-phase4-apc-20260807`，输出 `/tmp/ns-stage2m-phase4-apc-20260807`
- 数据库：新库 `na_s2m_phase4_v1`（旧库 genesis 与新 bundle 冲突 → 干净库 + alembic 0007 head）
- 配方：APC + arms A + real_hybrid + qwen36-27b-nvfp4@8002 + 并发参数
  （needs=4, kv=200K, endpoint=8, checkpoint-workers=2）
- 状态：genesis 成功，replay ch0-4 进行中（约 2 分钟/章，预计数小时完成 95 章 + 5 走廊）

### 18.6 Phase 4 重跑运行期修复（证据驱动，三连修复）

首次从零全量 replay 暴露三个问题（均已在代码修复，make quality 1589 passed/100%）：

1. **genesis/agent 传输配置**（`TeacherForcedBenchmarkE2ERunner._request`）：原 `timeout=300s` +
   thinking 默认开 → genesis planner 300s 超时。改为 `enable_thinking=False` + `max_output_tokens=12288`
   + `timeout_seconds=600`（域上限）。
2. **curator grammar 模式无界输出**（ch5 卡死）：strict grammar 下模型对 ch5 输出 >12288 tokens 仍
   未终止（b11 走廊同型问题）。修复：`ModelGateway.generate_structured(json_object_framing=True)` +
   curator `extract_reported_v2` 走 json_object framing（host pydantic 校验 + 契约反馈重试兜底，
   与走廊 framing 决策同型）。
3. **curator 枚举字面值**（ch5 3 次 schema 拒绝）：模型输出大写 `CREATE`，`ChangeOperationType`
   枚举为小写 `create`。修复：prompt 契约明确小写枚举字面值
   （`create/replace/retire`、`entity/event/state/relation/obligation`，通用契约说明，非 case 特化）。
   ch5 在修复后通过（progress ch0-6）。

### 18.7 Phase 4 重跑状态（APC 主运行进行中）

- 实验 `stage2m-phase4-apc-20260807`，干净库 `na_s2m_phase4_v1`（alembic 0007）
- genesis 成功；ch0-6 已提交；ch7-95 继续（~2 分钟/章，预计 3-4 小时）+ 5 个 checkpoint 走廊
  （ch20/40/60/80/95）经 `checkpoint-workers=2` 与 replay 异步并行
- 走廊并发已接线（needs=4 / kv=200K / endpoint=8）

## 19. Stage 2M Plan-Conditioned 语义闭环与全局调度修复（2026-08-08）

### 19.1 实施前证据隔离与审计基线

- Git HEAD：`420e16303c18354e72fa6486eb8968707842ef71`；本轮在既有 dirty Stage 2M
  工作树上续作，不 reset、不覆盖用户修改、不提交或合并。
- 初始 tracked binary diff 指纹：
  `sha256:e3bb4ec44d569f2d57cd98668ed9bc6fe64de10eced608be2897ef88b5743718`；
  初始完整 porcelain（含 untracked）指纹：
  `sha256:766d130aa25318fdeaad55612eadf6b2c4607aac507df1c6ed42b8092a1b7290`。
  初始差异为 83 个 tracked 文件、`+8465/-1277`，另保留既有未跟踪 Stage 2M
  领域/服务/测试/schema 文件与 `agentmemory_lab/`。
- 旧运行 `/tmp/ns-stage2m-phase4-apc-20260807` 保持只读诊断身份，不复用输出目录或数据库
  `na_s2m_phase4_v1`。Manifest 固定 commit `420e163`、dirty code fingerprint
  `sha256:4a3f33263c27f01c5177d7ad9ca513086e842c2ed5fcb884269cefb5fe7610ef`、
  APC、Arm A、`real_hybrid`、Qwen3.6/8002、Writer `4000`、Ledger `12000`；运行最终 replay
  到 chapter 81，现存 C20/C40/C60/C80 四个 checkpoint 输出。
- 该运行的五段指标、Planner health 与并发结论统一标记
  `DIAGNOSTIC_ONLY_INVALIDATED`：旧公式存在跨 Need/global-union 绑定错误，Planner lineage/call
  计数被丢弃，调度器不是 endpoint-global。原始 prompts/responses/receipts/progress/transport
  timing、四个 case 报告及零 leakage 信号仍保留为诊断证据。
- 六个 surviving content-addressed paired summary（对象 hash）为：
  C20 `a9b892f...`（早期，recall 0.731）与 `fe15a25...`（意图最终，0.654）；
  C40 `0d61e86...`（早期，0.690）与 `819fa03...`（意图最终，0.793）；
  C60 `ada6b4a...`（意图最终，0.552）；C80 `89d6a88...`（意图最终，0.545）。
  当前 top-level `e2e_paired_report.json` 仅指向 C80；C20/C40/C60 的 top-level 文件已被覆盖，
  不重建或冒充原始文件。
- 四个独立 Stage 2M case 报告 SHA-256：C20 `25f7aeb...`、C40 `acb89c3...`、
  C60 `35f940f...`、C80 `b585795...`。其 Per-Gold Writer 端到端零覆盖保留为独立下游基线，
  不与 paired retrieval 指标合并。
- P004/P005 与 `frozen_inputs.json` 不修改；旧公式 P005 不运行；在本计划语义、调度、质量门
  闭合前不启动新的 Phase 4 实验。

### 19.2 架构到代码闭环（SUPERSEDED_BY_REVIEW_2026-08-08）

- **APC 真值源与 Planner lineage**：新增并接通严格绑定的 planning context、world-summary
  root、plan root 与 artifact reference；Planner 多 facet 输出不再被压成单一 Need。Planner 每次
  invocation（包括模型返回空 drafts 后的 deterministic fallback）均持久化 model/prompt/world/raw
  response/token/validated/fallback 元数据；冻结重放校验全部 basis，任何 context 改动 fail-closed。
- **TIO / D9 / 可执行查询边界**：Task-Intent-Only 只消费任务意图，绝不读取 plan；D9 无 planned
  evidence 时严格无 `PLAN_NODE`。查询集合按 Need、许可 evidence 与 profile 做交集，R1 在执行前再次
  拒绝越界查询。
- **Per-Gold 五段评估**：每个 Gold 使用 typed availability、唯一 Need binding、Need-bound ledger，
  分段损失与总分采用版本化 formula/hash；case report 通过不可变 manifest 聚合。单臂运行显式标为
  single-arm，不再伪造 Agentic/paired 指标。
- **endpoint-global 调度**：request-count 与 KV-token 双预算在 `ModelGateway` 入口统一准入，覆盖所有
  真实模型调用；Condition 队列提供公平等待、typed timeout/unsatisfiable/context 错误、lease 与完整
  telemetry。异步入口通过 daemon-thread/Future bridge 获取同步 lease，避免 `asyncio.to_thread` 在线程池
  等待导致 event-loop shutdown 卡死；取消路径会释放迟到 lease。
- **Claim Support 并发一致性**：语义相同的验证 single-flight；并发协调器只按确定性顺序写 artifact，
  串行/并行得到相同 descriptor 与 semantic hash。对应 domain、service、adapter、CLI、schema exporter、
  versioned JSON schema 与 unit/contract/golden/regression 测试已同步。

### 19.3 Planner 空输出回归与冻结重放证据（SUPERSEDED_BY_REVIEW_2026-08-08）

- 首次真实 P001 探针 `/tmp/ns-stage2m-focused-final-20260808-v1` 使模型连续两次返回空 drafts，暴露
  fallback invocation 未携带 replay 元数据；原 artifact
  `sha256:8c0f29ba345e7cbfc4b54fa06ebef50c91eb559bdac1ec59b6813d9c3c5344db`
  原样保留为失败诊断。修复后增加 metadata-bearing fallback replay 回归覆盖；无 gateway 的模板 fallback
  仍保持合法且有分支覆盖。
- 成功探针 `/tmp/ns-stage2m-focused-final-20260808-v2/focused_planner_evidence.json`，文件 SHA-256
  `43f5fd8a4e894f0a77c742ccf1bd109a1eed77a60fdbcd11af74808db848e60d`。Planner artifact
  `sha256:d1608fe3e05eebde9573065d7c75cf4255e9f378b1e05de0b9e38eff732fd2fb`
  （7997 bytes），context hash `sha256:831662a5ac5feacb9b9632ef10f566ab1d796daa9139bdd4787c11461fcb7c6e`，
  world root `sha256:bdb09b005bb86a20dbeb060e3c049e882ee525c83ba6387825e6cda6af01b92e`，
  plan root `sha256:49651a7891085a94e8a61bffcd8e02c3e7e11d89c1c75136719fa7e090353864`。
- 模型两次空 drafts 均被如实记录（每次 883 input / 90 output tokens），生成状态为
  `PLANNER_FALLBACK`；冻结 replay endpoint call delta 为 0。改动 context 被拒绝为
  `Planner artifact replay basis mismatch: planning_context`；D9/no-planned/no-`PLAN_NODE` 断言通过。
  调度账本 acquired/released requests `2/2`、KV `10932/10932`、inflight `0/0`，无 timeout、
  unsatisfiable 或 context reduction。

### 19.4 endpoint-global 串并行真实负载证据（SUPERSEDED_BY_REVIEW_2026-08-08）

- 证据 `/tmp/ns-stage2m-scheduler-parity-final-20260808-v1/scheduler_parity_evidence.json`，文件
  SHA-256 `67b8c6a812ecac256f431a97e2e5662c91de8d0c6ef82ce3cca9c2558de8f39d`；复用冻结
  Planner artifact，不触发 Planner endpoint。
- 两个相同语义输入在 serial 与 concurrent 模式的 descriptor hashes 完全一致：
  `sha256:4372532dc210d42c73d0c3f04b47d13f7166b5533f86f36d260f1d27fa5d0768`、
  `sha256:88c165f1ccb08e882c226a817073b13599b686689792bf877b553f5d7849bd74`；共同 semantic hash
  `sha256:8f70e81e6e472e0cd3dcb83d0d0434cd184742c2d289612441cc44089fe7f4e8`。
- endpoint request limit=1 下，serial 8.275s；concurrent 8.232s 且记录 4.116s 合法排队等待。
  两种模式均 acquired/released requests `2/2`、KV `10932/10932`、peak requests `1`、peak KV
  `5466`、最终 inflight `0/0`；无 OOM、timeout、unsatisfiable 或 context reduction。
- 真实端点模型为 `qwen36-27b-nvfp4`（max model length 131072）；OpenSearch 单节点 yellow，
  `timed_out=false`、pending tasks 0、active primaries 878，yellow 来自 704 个未分配 replica。

### 19.5 最终质量门

- `make quality`：Ruff lint/format、strict MyPy 全通过；Pytest `1631 passed, 9 deselected`
  （248.14s），22031 statements / 6250 branches，100.00% branch coverage。
- `.conda-env/bin/pre-commit run --all-files`：ruff check、ruff format、mypy、deterministic pytest
  全通过。首次 sandbox 执行仅因用户级 pre-commit cache 只读失败；获准在 sandbox 外重跑后通过，
  不属于代码失败。
- 最终 executable-source 指纹：HEAD `420e16303c18354e72fa6486eb8968707842ef71`；
  `git diff --binary -- src scripts schemas Makefile pyproject.toml` SHA-256
  `3f706c3dd137be2dfe62284a631106fdcfaaf879741f251b363598da4b11e3b0`；对应 porcelain
  SHA-256 `49b22e3688272f5d6f7cf852b5860ae524cd018c6166ba466c8437b6da3e8796`。
  当前 executable scope 为 83 条 status，tracked diff 61 files、`+7258/-843`；整个工作树 131 条
  status。未 reset、未覆盖既有修改、未提交、未合并。

### 19.6 正式 Phase 4 门禁结果与交回状态

- 使用新 experiment/output 身份启动正式 APC 探针时，runner 在任何 output、数据库或网络写入前正确
  fail-closed：`formal Stage 2M run requires a clean executable source tree`。本轮实现本身仍在 dirty
  executable tree 中，而工作流明确规定 OpenCode 不提交/合并，因此不能合法产生正式 clean-source
  fingerprint；`--allow-dirty-diagnostic` 也不能冒充 Gate 证据。
- 未启动或复用新的 APC/TIO P001-P005 正式矩阵；旧 `/tmp/ns-stage2m-phase4-apc-20260807`、旧数据库、
  P004/P005 frozen inputs 与 held-out Gold 均未修改。没有依据 held-out 结果调代码。
- **状态：`RETURN_TO_CODEX`**。Codex 下一步应审查并接受本实现，在授权边界内形成干净 executable
  source fingerprint；随后用全新数据库、output directory 与 experiment id 跑 APC 与 TIO 的
  P001-P005 正式 Phase 4，并由 immutable manifest 汇总。只有该 clean-source 矩阵通过后才可宣告
  Stage 2M 完成或合并。

## 20. Codex REPAIR：D9、正式 RoutePlan、Planner final lineage 与 bounded checkpoint（2026-08-08）

### 20.1 本轮边界与旧结论失效标记

- 本轮严格沿用 `.agent/plan.md` 与 `.agent/review.md` 的 repair direction；没有修改 plan、review、
  architecture、ADR、project status、P004/P005 或 frozen inputs，没有提交、合并或运行正式 Phase 4。
- §19.2-§19.4 已显式标记 `SUPERSEDED_BY_REVIEW_2026-08-08`；原始产物和陈述保留为历史，不能再作为
  D9、final-Need replay 或 bounded checkpoint 验收结论。

### 20.2 四个阻塞项的代码修复

- **统一 APC D9**：删除 `plan_obligation` / `plan_conditioned_history` 的 Plan channel 例外。
  `Stage2PairedPilotRunner` 对每个 APC Memory Need 统一固定
  `(planner_may_read_plan=True, retrieval_may_return_plan=False, claim_may_cite_plan=False)`，并固定
  `legacy_allow_plan=False`、`allow_plan=False`、`access_scope=writer_safe`；author Plan 只保留为
  Planner guidance，不进入 observed retrieval / Claim Support / Ledger。
- **唯一正式 query eligibility owner**：`NeedQueryCompiler.eligible_channels()` 成为 Stage 1 与
  Stage 2M 共用规则；`DeterministicChannelPlanner` 在 `RoutePlan` 边界计算
  `ROUTES ∩ allowed pools ∩ snapshot capability ∩ compiled-query eligibility`。`RoutePlan`、direct/fair
  paired traces 现持久化 compiled bundle、effective channels 和 typed exclusion reasons；空交集以
  `NOT_EXECUTED_NO_EXECUTABLE_QUERY` / `NO_EXECUTABLE_QUERY`、calls=0 fail closed。回归从
  `resolve_state_case()` real-hybrid 主路径进入，而不是只测 Stage 1 旁路。
- **Planner final lineage**：Planner artifact v2 记录唯一 attempt IDs、每次 raw response/hash、usage、
  status/error，以及每个最终 Need 的 stable source identity、Need payload hash、completion contract
  hash、query bundle hash。fallback artifact 延后到实际 deterministic Needs 完成后持久化，所有实际
  fallback Needs 引用同一 artifact；validated-set hash 绑定 final manifests，不再绑定空 drafts。
  frozen replay 在现有 generator owner 内免模型重走 Grounder→Validator→Need builder，并逐项比较
  Need/completion/query/final-set；basis、grounding、validation 或 final-set 任一变化均 fail closed。
- **Planner 可用 state surface**：只扩展现有 `PlannerWorldSummaryBuilder`，按 task/plan relevance
  确定性选择 cutoff-safe state records 并记录截断数，没有新增 ontology、规则引擎或摘要系统。
- **bounded evidence wiring**：现有 teacher-forced 主路径可注入 frozen Planner artifact，并可分别设置
  Need/evaluator concurrency；shared admission controller snapshot 现在保留每个 admitted scheduling
  descriptor，flow summary 保留 scheduler telemetry 和 model call records。没有新 scheduler service、
  DAG runtime、检索器、评测器或 artifact store。

### 20.3 回归与质量门

- 增加/改写 D9 三类 Need、retrieval/Claim Support observed-only、paired 主路径无可执行 query、
  RoutePlan validator、fallback final manifests、冻结 final replay、changed basis、unique attempt IDs、
  world state surface、shared scheduler descriptor/ordering/lease/single-flight 等回归；schema exporters 已
  同步 Stage 1 planner artifact 与 Stage 2 RoutePlan contracts。
- 定向主路径回归：`70 passed`（model admission、Stage2 paired pilot、teacher-forced contract）。
- 最终 `make quality`：Ruff、format、strict MyPy 全通过；Pytest `1633 passed, 9 deselected`，
  22190 statements / 6310 branches，`100.00%` branch coverage。
- 最终 `PRE_COMMIT_HOME=/tmp/ns-precommit-cache .conda-env/bin/pre-commit run --all-files`：ruff check、
  ruff format、mypy、deterministic pytest 全通过。

### 20.4 P001 focused final-Need replay 真实证据

- 新 identity：`/tmp/ns-stage2m-repair-focused-20260808-v2/focused_planner_evidence.json`；文件
  SHA-256 `c429175207118bfed0c84cf35cff6ae657d869137eda1e92a7cdd16edbb67f10`；旧 focused v1/v2
  均未覆盖。
- P001 本次端点两次 invocation 均明确记录为 `OpenAIChatEndpointError`，因此合法进入 deterministic
  fallback；artifact `sha256:225d903fefc591caf9e375ff57e814ba4dcdc6d66b2120a25e3a53665ff95195`
  绑定实际 20 个 final Need manifests。20 个 Need、completion、query hashes 与 frozen generator replay
  逐项完全一致，endpoint call delta=0，所有 Needs 只有一个 artifact identity；changed planning context
  被 `basis mismatch` 拒绝，D9/no-PLAN_NODE 全通过。
- 该证据只证明合法 transport-fallback 与 final lineage/replay；不把失败的 provider 调用冒充成功模型
  ledger receipt。

### 20.5 同一 bounded APC checkpoint 串/并行主路径证据

- 冻结 basis：P001 chapter 20 commit
  `sha256:d530bb2a3900df89cd0f8c59297e1d848572dce91b75a4ddc82ddc6fef391ea6`；两次均使用同一
  Planner artifact `sha256:f860237c56e1c4859ed784fb5dbd71bdeed98f031538d56985f72a7c409ab6e4`、
  real-hybrid snapshot、Writer budget 4000、Ledger budget 12000、Arm A 和同一模型策略。
- 串行 output：`/tmp/ns-stage2m-repair-checkpoint-serial-20260808-v1`；flow SHA-256
  `542af9332ea766b8034b4a62bc786ba7d440bea2a8dc8ea7c9fc32309b67e3af`。32/32 leases
  acquired/released，peak request=1，peak KV=31230/160000，inflight=0/0，timeout=0。
- 并行 output：`/tmp/ns-stage2m-repair-checkpoint-concurrent-20260808-v1`；flow SHA-256
  `7f23d52b11d732ff7e6ab254c6a8089377fbc687ec39d4790cc2aa7a7803539d`。5 个 Need proposal 与
  evaluator batches 真实重叠，peak request=4，peak KV=124677/160000，28/28 admitted leases 均释放，
  最终 inflight=0/0，wait 821.366s，无 unsatisfiable、OOM 或 context reduction。
- 两次 frozen replay 都没有 Planner endpoint 调用；65 条 deterministic handle/workset audits 完全相同，
  canonical hash 均为 `e574355141c598a678b97f6eb36fc31969d9fb6a15b825f645bc7609699254d7`；
  Writer/Ledger budgets、READY 状态、selected units=34、writer tokens=4000、evidence tokens=11997
  完全相同。两次共同 admitted 的 28 个请求，其完整 scheduling descriptors/context hashes 完全相同；
  persisted report child identity/order 均为 `(P001, checkpoint=20, arm=A)`。
- 必须如实保留的负载观察：并行 4 路长请求在 120s scheduling deadline 下有 4 个 multi-slice proposal
  以 typed `SchedulingTimeoutError` 停止，因此并行 run 只有 28 个 admitted descriptors，而串行为 32；
  它们没有被误分类、没有泄漏 lease，checkpoint 与 single-arm evaluation 仍 `COMPLETED`。按用户要求
  未继续做调参重跑；该事实交由 Codex 判断是否仍需更低并发/更长 deadline 的补充 evidence。
- equal-key verification single-flight 的 owner/waiter/failure-unblock/retry 由确定性回归覆盖；本次 P001
  真实 workset 未产生可证明的 equal-key provider duplicate，未伪造命中计数。

### 20.6 交回状态

- 正式 APC/TIO P001-P005 继续由 clean-source gate 阻止；未放宽 gate、未复用正式 output identity、未提交
  或合并。
- **状态：`RETURN_TO_CODEX`**。四个 review 阻塞项的代码方向、contracts、100% coverage 与 focused
  final-lineage 证据已完成；bounded checkpoint 同时保留成功的 shared-budget/ordering/workset 证据和
  4 个 typed scheduling timeouts，不隐瞒、不包装为完全无失败的 parity。

## 21. Claim Support compact transport and reasoning telemetry repair (2026-08-09)

### 21.1 Scope and decision

- Implemented directly at the user's request from
  `.agent/claim_support_runtime_bottleneck_analysis_20260809.md`; no formal Phase 4, full P002
  parity, or APC/TIO P001-P005 run was started.
- Repository inspection found only Claim Support multi-slice proposals explicitly enabled model
  thinking. Planner, single-slice proposal, whole verification, and teacher-forced agent requests
  already disable it. Therefore the repair is stage-specific rather than a global thinking-policy
  change.
- The existing owners remain authoritative: `ModelRequest`/OpenAI adapter for request and usage
  telemetry, `TrustedClaimSupportProducer` for Claim Support transport policy/progress, and the
  teacher-forced CLI/experiment manifest for explicit replay identity. No dynamic tuner, DAG,
  cache service, telemetry service, or report family was added.

### 21.2 Frozen real-endpoint calibration

- Frozen input: content-addressed P002/C40 prompt
  `sha256:484b2fa4fb4a28d6746ecd348519f86e18a9c7ae6a9ef71d4eaa206cd5cdf4f5`
  from the preserved diagnostic run; prompt length 48,985 characters, reported endpoint input usage
  25,448 tokens. Model and endpoint remained `qwen36-27b-nvfp4` at the local OpenAI-compatible
  endpoint; prompt/evidence/workset/typed JSON contract were unchanged.
- Calibration setting: `enable_thinking=false`, `max_output_tokens=1024`, timeout 600 seconds.
- Result: valid `MultiSliceProposalBatch` JSON, 283 output tokens, approximately 19.36 seconds,
  with one complete claim and no `insufficient_need_ids`. This replaces the failing nominal
  `thinking_token_budget=500`, `max_output_tokens=4096` transport default for this stage.
- This was a bounded non-formal transport calibration only. It did not assert Gate quality and did
  not execute the complete P002 request set.

### 21.3 Implementation

- `ClaimSupportTransportConfig` now provides one fixed validated policy. Defaults are multi-slice
  thinking disabled, zero thinking budget, and 1024 total output tokens; optional explicit values
  must keep the thinking budget within the total output cap.
- `run_stage2_teacher_forced_e2e.py` exposes `--support-multi-thinking` /
  `--no-support-multi-thinking`, `--support-multi-thinking-token-budget`, and
  `--support-multi-max-output-tokens`. These values flow through the teacher-forced runner and
  paired pilot into Claim Support, and are included in `execution_config` and its content hash.
- Successful model usage now preserves `reasoning_tokens`. Successful proposal progress records
  latency, `finish_reason=stop`, input/output/reasoning usage, and the fixed transport settings.
- OpenAI-compatible length/unexpected/empty-content failures retain finish reason, available token
  usage including reasoning tokens, latency, and partial raw content. Claim Support persists that
  partial output through the existing content-addressed artifact writer and records its reference
  in the existing failed proposal event; no invalid raw output is silently discarded.
- Stage 0/1/2 checked-in schemas were regenerated through the existing export scripts after the
  `ModelUsage` extension.

### 21.4 Verification and remaining boundary

- Focused Claim Support/OpenAI/manifest regressions: `199 passed`.
- Final `make quality`: Ruff clean, format clean, strict MyPy clean for 293 source files;
  `1641 passed, 9 deselected`; 100% statement and branch coverage.
- `git diff --check`: PASS after implementation.
- A successful non-fallback P002 Planner artifact, bounded complete serial/concurrent parity, and a
  real proposal -> whole verifier -> persisted receipt checkpoint run remain later admission steps.
  They were not inferred from the single-chunk calibration and remain prerequisites before any
  formal/full execution.

## 22. Codex REPAIR continuation: invalid JSON, frozen Planner, and serial stop (2026-08-09)

### 22.1 Invalid-JSON telemetry closure

- The provider-success/Pydantic-failure branches now retain the exact raw response through the
  existing content-addressed artifact writer and record failed output ref/hash, input/output/
  reasoning usage, latency, and `finish_reason=stop` while preserving the typed
  `invalid_structured_content` classification. No telemetry service, ledger type, or report format
  was added.
- Regression
  `test_invalid_multi_json_retains_output_usage_latency_and_resolvable_artifact` proves that an
  invalid multi-slice JSON response remains dereferenceable and carries complete usage/timing.
  Focused P2 and Planner replay tests pass (`2 passed` with `--no-cov`).

### 22.2 Non-fallback P002 Planner artifact and replay

- Real P002 Planner artifact:
  `/tmp/ns-stage2m-phase4-apc-20260807/objects/sha256/a1/`
  `a1231b1d4bf4022295b06b44034d2ee8e953fdba70711327490f2db70c3e3ee2`
  (41,651 bytes). It records `fallback_used=false`, one successful attempt, 6 parsed drafts,
  2 accepted final Needs, and validated final-set hash
  `sha256:676696a92fcea1d6247092b7c950e9536f93b68281c63c00fc76fe6ebf57ba31`.
- Frozen raw Planner prompt calibration is durable at
  `/tmp/ns-stage2m-planner-calibration-p002-off8192-20260809/planner_calibration.json`:
  `thinking=false`, 1,962 input / 1,559 output / 0 reasoning tokens, 7 parsed drafts,
  129.63 seconds. The existing 4096 Planner output guard was therefore retained.
- Model-disabled replay output/log:
  `/tmp/ns-stage2m-repair-planner-p002-replay-20260809-v1` and
  `/tmp/ns-stage2m-repair-planner-p002-replay-20260809-v1.run.log`. The first endpoint request is
  Claim Support (18,360-character prompt); no 4,247-character Planner request occurs, so Planner
  endpoint delta is zero. A changed P001/C20 basis fails closed in
  `/tmp/ns-stage2m-repair-planner-p002-changed-basis-20260809-v1.run.log` with
  `Planner artifact replay basis mismatch: planning_context,world_summary,prompt`.
- Frozen replay originally rebuilt the exact Need/completion/query set but omitted
  `planner_artifact_document_ref` from `NeedGenerationResult`. The existing generator owner now
  persists the replayed artifact and returns that ref; the replay regression asserts ref identity
  on every regenerated Need. Serial v3 then reports the same artifact in manifest hash, flow
  `need_planner_artifact_refs`, and five-segment `planner_artifact_ref`, with
  `need_planner_fallback_count=0` and no Planner endpoint call.

### 22.3 Final-policy serial evidence and mandatory stop

- Final serial output:
  `/tmp/ns-stage2m-repair-serial-p002-finalpolicy-20260809-v3`; run log:
  `/tmp/ns-stage2m-repair-serial-p002-finalpolicy-20260809-v3.run.log`. Manifest fixes
  Need/evaluator/checkpoint concurrency to 1, endpoint limit 1, KV budget 200,000,
  `support_multi_enable_thinking=false`, thinking budget 0, output guard 1024, and frozen Planner
  hash `a1231...ee2`.
- Planner lineage is complete, and scheduler leases are balanced: acquired/released requests
  `18/18`, final inflight request/KV `0/0`, timeout 0, unsatisfiable 0, peak request 1. No OOM,
  context reduction, or lease leak occurred.
- The run nevertheless terminates Claim Support as `failed`. Two legal multi-slice requests for
  `need.stage2m.planner.mem_42_01` hit the accepted 1024 output guard with
  `finish_reason=length`: request `bcf3f9...` used 28,714 input / 1,024 output / 0 reasoning tokens
  at 45,485 ms; request `d9a2b3...` used 31,010 / 1,024 / 0 at 54,351 ms. Their raw partial outputs
  are independently resolvable as artifacts `sha256:cb31aa...e0da` (2,376 bytes) and
  `sha256:a76627...85b0` (1,574 bytes); both byte lengths and hashes verify.
- Funnel terminal facts: 9 proposal requests, 2 proposal transport failures, 2 whole-verifier
  rejections, `multi_slice_verified=0`, one insufficient Need, terminal state `failed`. Therefore
  this fingerprint does **not** establish the required real
  `proposal -> whole verifier -> persisted receipt` success chain.
- The prior serial v1 did form real whole-verifier calls and receipt-bound writer entries, but it
  predates the frozen-replay document-ref fix and cannot replace v3 as the final executable
  fingerprint. It is retained only as variance/diagnostic evidence.
- Per `.agent/review.md` stop condition, concurrent parity was not started. The implementation did
  not raise the 1024 guard, restore thinking, introduce dynamic policy, or start formal Phase 4.
  Status is `RETURN_TO_CODEX / REPAIR_STOP_OUTPUT_LENGTH_TRUNCATION`.

### 22.4 Final verification

- `make quality`: Ruff and format clean, strict MyPy clean for 293 source files; Pytest
  `1642 passed, 9 deselected` in 248.77 seconds with 22,253 statements / 6,318 branches and
  100.00% coverage.
- `PRE_COMMIT_HOME=/tmp/ns-precommit-cache .conda-env/bin/pre-commit run --all-files`: Ruff check,
  Ruff format, MyPy, and deterministic Pytest all passed.
- `git diff --check`: passed. No commit or merge was created.

## 23. Human-authorized 2048 guard calibration and bounded parity (2026-08-09)

### 23.1 Evidence-based guard expansion

- After §22 returned the required 1024 length-truncation evidence, the human explicitly authorized
  another larger-budget trial. The minimum next guard, 2048, was tested with thinking still
  disabled and thinking budget still zero; the frozen Planner artifact, prompts, evidence,
  worksets, typed contracts, Writer/Ledger budgets, model, and all non-scheduling settings remained
  unchanged.
- Serial output:
  `/tmp/ns-stage2m-repair-serial-p002-output2048-20260809-v1`; log:
  `/tmp/ns-stage2m-repair-serial-p002-output2048-20260809-v1.run.log`. Both Needs completed the real
  multi proposal and whole verifier chain. Funnel: 5 proposal requests, 3 valid multi proposals,
  2 verified multi claims, 0 transport failures, 0 whole-verifier rejections, 0 insufficient
  Needs, and 0 unclosed facets. The frozen paired artifact
  `sha256:54b62bdcee1a29be7acdd2f0de994e31b356c5935b1f092e3a4278432093cfc6`
  contains 28 receipt-bound groups, including two model-produced verified receipts with the exact
  proposal and whole-verifier call records.
- Serial scheduler: acquired/released `14/14`, peak request 1, peak KV 31,106, final inflight
  request/KV `0/0`, timeout 0, unsatisfiable 0. Planner ref is the non-fallback `a1231...ee2` in
  manifest/flow/five-segment and Planner endpoint calls remain zero.

### 23.2 Unsafe endpoint concurrency result

- A first concurrent diagnostic used Need concurrency 2 and endpoint request limit 2:
  `/tmp/ns-stage2m-repair-concurrent-p002-output2048-20260809-v1`. It failed and is retained as
  negative evidence, not parity evidence.
- The exact `dd858...` input produced 243 output tokens in serial but ran to 2,048 tokens with
  `finish_reason=length` under two simultaneous endpoint requests; latency rose from 20.849s to
  116.791s. A second concurrent request also truncated at 2,048. Scheduler leases still balanced
  `18/18`, peak request 2 / KV 62,190, final inflight `0/0`, timeout 0.
- This isolates the remaining runaway behavior to simultaneous generation at this local endpoint,
  rather than showing that legal typed payloads intrinsically require more than 2,048 tokens.
  Therefore the guard was not blindly raised to 4,096.

### 23.3 Safe concurrent parity

- Safe concurrent output:
  `/tmp/ns-stage2m-repair-concurrent-safe-p002-output2048-20260809-v1`; log:
  `/tmp/ns-stage2m-repair-concurrent-safe-p002-output2048-20260809-v1.run.log`. Need orchestration
  concurrency is 2 while the endpoint request limit remains 1; Evaluator and checkpoint workers
  remain 1. This changes only bounded support scheduling and prevents simultaneous model
  generation.
- Serial and safe-concurrent have the same complete set of 14 scheduling descriptors, including
  request ID, Need, stage, dependency, endpoint, context hash, prompt estimate, output reservation,
  sequence reservation, safety allowance, priority, and scheduling timeout. All 7 proposal/
  verifier progress call identities and hashes match; all 18 pre-model handle/workset audits match.
- Both funnels are identical: 5 requests, 3 valid multi proposals, 2 verified multi claims,
  0 transport/verifier failures, 0 insufficient Needs, and 0 unclosed facets. Both select 31 units,
  render 1,061 Writer tokens and 11,981 Ledger tokens, have 107 semantically identical Ledger
  entries, and produce identical five-segment evaluation. Different Ledger artifact hashes are
  expected because receipt call records retain actual timestamps/latencies.
- Safe-concurrent scheduler acquired/released `14/14`, peak request 1 / KV 31,106, final inflight
  `0/0`, timeout 0, unsatisfiable 0, with 78.423 seconds of legitimate capacity waiting recorded
  across the two Need pipelines. No duplicate equal-key verification, OOM, context reduction, or
  lease leak occurred.

### 23.4 Final implementation policy

- The evidence-backed Claim Support multi output guard is now 2048 in the existing transport
  config and CLI default/fallback. Thinking remains disabled with zero reasoning budget. The
  producer identity is bumped `v31 -> v32`; no other model stage or semantic budget changes.
- Focused transport/telemetry/manifest regressions pass (`13 passed`), and Ruff/format checks pass.
  Final `make quality` also passes: Ruff/format, strict MyPy for 293 source files, and Pytest
  `1642 passed, 9 deselected` in 251.53 seconds with 22,253 statements / 6,318 branches and 100%
  coverage. Full pre-commit (Ruff check/format, MyPy, deterministic Pytest) passes. Formal Phase 4
  and clean-source P6 remain unrun pending Codex review; no commit or merge was created.

## 24. Current-v32 bounded evidence identity replay (2026-08-09)

- Scope was evidence-only per Codex review: no source/schema/test/config changes, no Planner rerun,
  no endpoint-limit-2 probe, no full test rerun, and no formal Phase 4. Both runs used the current
  `trusted_claim_support_producer.v32` executable fingerprint
  `sha256:20daa522f815c88c5ab823d2b03ff896b6751264dd6edac2777a4d93b089b881`,
  frozen non-fallback Planner artifact `sha256:a1231b1d...e3ee2`, and fixed
  `thinking=false / thinking_budget=0 / multi_output=2048`.
- Serial (`Need concurrency=1`, endpoint limit 1):
  `/tmp/ns-stage2m-repair-v32-serial-p002-20260809-v1`; log alongside as `.run.log`.
  Manifest SHA-256 `f0755c567e8dd5fc95f5434693cda4de054230c99fb8b44f19db0f522b3e215f`;
  flow SHA-256 `e02c94cf5ce4a548bcc681ad6fa72708778ac3f1428b65294d6d50d0ab0cd52e`.
- Safe concurrent (`Need concurrency=2`, endpoint limit 1):
  `/tmp/ns-stage2m-repair-v32-concurrent-safe-p002-20260809-v1`; log alongside as `.run.log`.
  Manifest SHA-256 `245d6193c86c2484fc7e95aa41ca57a49cb75880b05d8e38e2e1609b9899f88e`;
  flow SHA-256 `e6e9fcaba8143866b9668471cf047ddc7c9c8113285559e0fe347eb0438758db`.
- Both funnels are identical: 5 proposal requests, 3 valid multi proposals, 2 verified multi
  claims, zero proposal/verifier transport failures, zero whole-verifier rejection, zero
  insufficient Need, and zero unclosed facet. The frozen paired artifacts
  `sha256:f364daff...7c02` (serial) and `sha256:7afe7fdb...54aa` (safe concurrent) each retain
  28 receipts and the same two model-produced verified chains; their producer field is explicitly
  `trusted_claim_support_producer.v32.synthesized`.
- Exact parity holds for the complete 14 scheduling descriptors, all 7 proposal/verifier call
  identities and hashes, all 18 pre-model audits, 31 selected units, Writer 1,061 / Ledger 11,981
  tokens, 107 semantically identical Ledger entries, and the complete five-segment evaluation.
  Serial scheduler acquired/released `14/14` with no wait; safe concurrent acquired/released
  `14/14` with 78.474s legitimate endpoint-capacity waiting. Both finish with inflight request/KV
  `0/0`, timeout 0, unsatisfiable 0, no OOM/context reduction/lease leak.
- Status: `RETURN_TO_CODEX_REVIEW`. These artifacts replace the v31/old-fingerprint evidence only;
  previously accepted implementation and test evidence is unchanged. No commit or merge was made.

## 25. Stage 4 Planner Context Loop implementation (2026-08-10)

- The corrected branch topology is `0ec1eb4 -> 1b926f5 -> 21cc3c3 -> 27bd5cf -> a76d059`,
  followed by coverage and integration-test commits. `1b926f5` is the independently extracted
  shared Context contract. The Stage 4 branch does not contain `bab4451` and does not import the
  Stage 3 Writer, Editor, Candidate Observer, or Stage 3 evaluation implementation.
- `21cc3c3` extends the shared projector/runtime contract for the already-declared Planner
  consumer: bootstrap may have no commit/snapshot, accepted-basis Writer validation remains
  fail-closed, Planner-safe deltas are typed, compacted item identities cannot be re-expanded,
  and event append/checkpoint/recovery stay owned by the shared runtime.
- Stage 4 implements all seven modes including `CHAPTER_SET`, typed `PlanningInquiry` and
  `GoalProposal`, an independent `PlanReviewerAgent`, Planner-specific Need generation and
  `PlannerContextPackage`, the no-base bootstrap path, typed graph path receipts, conditional
  Anchor-to-Graph expansion, compact-to-expand behavior, and the candidate-only Planner loop.
  It does not write Canon, PlanRoot, or Stage 5 scheduler state.
- Final deterministic verification: Ruff, Ruff format, strict MyPy, schema contracts, and
  pre-commit all pass. Pytest reports `1678 passed, 9 deselected`, 24,028 statements / 6,776
  branches, and 100% statement/branch coverage. Real native infrastructure verification reports
  `5 passed, 1682 deselected` across PostgreSQL, MinIO, OpenSearch, full outbox projection, and
  Stage 2 freeze/reveal.
- The checked-in evaluation contract enforces one frozen case for every Planner mode and supports
  configured plus ablation arms. No formal real-model seven-mode manifest, model endpoint adapter,
  or blind-review artifact was supplied or executed in this implementation run; semantic Gate
  evidence therefore remains `CONDITIONAL_PASS`, while the engineering Gate is complete.

## 26. Stage 4 final-Gate hardening continuation (2026-08-10)

- The evaluation CLI now loads exactly the seven frozen case identities, accepts either an
  application-supplied configured runtime factory or deterministic fake results, runs selectable
  configured/ablation arms, writes immutable content-addressed evidence, and refuses to present a
  fake report as Gate-eligible. Formal manifests bind the pilot, corpus, configuration, rubric, and
  threshold fingerprints before evaluator access.
- A formal configured run now requires a post-freeze blind evaluator and eight declared semantic
  metric families: author-intent coverage, accepted Plan/Canon contradiction, continuity,
  alternative quality, reviewer issue recall, future leakage, provenance error, and unsupported
  factualization. Every configured and ablation result is exported to the evaluator under an
  opaque candidate identity; the arm mapping remains in the report, so semantic ablation is
  possible without revealing the treatment to the evaluator. Fake reports remain explicitly
  `deterministic_fake` and non-Gate-eligible.
- Planner, Reviewer, model routing/transport, and shared Context Runtime owner failures now settle
  to typed Planner terminals. Failure results retain any inquiry/checkpoint/context artifacts that
  were already produced. `OpenAIChatEndpointError` now implements the provider-neutral
  `ModelEndpointError` port boundary, allowing exhausted provider failures to become
  `MODEL_UNAVAILABLE` without catching unrelated programming errors.
- Focused verification passed: Stage 4 domain/service/schema tests (`24 passed`), Planner loop and
  evaluation tests (`13 passed`), OpenAI/ModelGateway regression (`72 passed`), Ruff, formatting,
  and strict MyPy. The final deterministic selector
  `NOVEL_AGENT_FORBID_MODEL_CALLS=true PYTHONPATH=src pytest -m "not model_required and not integration"`
  reports `1683 passed, 9 deselected`, 24,082 statements / 6,784 branches, and 100% statement and
  branch coverage. `pre-commit run --all-files` passed all hooks. Real native infrastructure
  verification reports `5 passed, 1687 deselected` across PostgreSQL, MinIO, OpenSearch, durable
  workflow/outbox projection, and Stage 2 freeze/reveal behavior.
- The first unrestricted `pytest` attempt was invalid as evidence: the isolated worktree lacked the
  ignored private benchmark mount, included live model/integration markers, and the sandbox denied
  loopback sockets. The private benchmark was mounted read-only for the deterministic rerun and
  removed afterwards; integration was rerun separately outside the socket sandbox. No private
  benchmark or runtime data was added to Git.
- Topology audit remains correct for the Stage boundary: `0ec1eb4` and shared Context contract
  `1b926f5` are ancestors of the Stage 4 branch, while Stage 3 implementation `bab4451` is not.
  `21cc3c3` is the isolated shared Planner-consumer extension before the Stage 4 implementation
  commits; the Stage 4 diff contains no Writer/Editor/Observer/evaluation implementation from
  Stage 3.
- The real seven-mode semantic Gate has not run. `http://127.0.0.1:8002/v1/models` is unreachable,
  the local GPU driver is unavailable, no alternative configured LLM endpoint exists in `.env`,
  and native model health reports no running model PID. Therefore no formal case manifest,
  configured runtime-factory invocation, or blind semantic report is claimed. Engineering evidence
  is green, but §11.5/§12.2 remains pending and cannot be replaced by the deterministic fake path.

## 27. Stage 4 formal semantic-Gate contract completion (2026-08-10)

- Completion audit found that the manifest fingerprints added in §26 were not yet sufficient:
  the runner did not load the frozen pilot/rubric/threshold artifacts, did not bind their exact
  content identities, and an aggregate evaluator mapping could not prove per-arm blind quality.
  This continuation closes those engineering gaps without inventing semantic results.
- New versioned `PlanningEvaluationCriterion`, `PlanningEvaluationRubric`, and
  `PlanningEvaluationThresholds` contracts require every declared semantic metric exactly once,
  enforce metric direction, and freeze explicit minima/maxima for author-intent coverage,
  Plan/Canon contradiction, obligation/arc/hook continuity, rolling hierarchy consistency,
  chapter feasibility, alternative quality, decision rationale, reviewer issue recall,
  HUMAN_REQUIRED rate, future leakage, provenance error, and unsupported factualization.
- A formal run now requires the pilot, rubric, and threshold files in addition to the manifest.
  `load_frozen_planning_evaluation_gate()` canonicalizes and persists them; the runner verifies
  their content-addressed refs against the manifest before exposing any candidate to the evaluator.
  Fake runs do not obtain a frozen Gate or a semantic verdict.
- The evaluator receives every configured/ablation result under opaque candidate identities and
  must return the exact rubric score set for every identity. The runner rejects missing/extra
  candidates, malformed score objects, metric drift, and non-numeric scores, then unblinds and
  aggregates scores per arm. The report now includes per-arm semantic metrics and a typed
  `semantic_gate_passed`; internal diagnostic leakage/provenance and the configured
  HUMAN_REQUIRED rate remain independent fail-closed checks even if blind scores are favorable.
- Each evaluation adapter result is now a typed `PlanningEvaluationObservation` bound to the
  manifest configuration and model fingerprints. Per-arm reports aggregate prompt/completion
  tokens, latency, model-call count, exposed/used evidence, channel failures, and degraded runs;
  evidence use that was not first exposed is invalid. This makes the required token/latency/model
  and exposed/used audit explicit instead of relying on unparsed lineage artifacts.
- Focused Stage 4 domain/service/schema verification reports `26 passed`; strict MyPy and Ruff pass.
  Final deterministic verification reports `1684 passed, 9 deselected`, 24,233 statements / 6,826
  branches, and 100% statement/branch coverage. Final real native infrastructure verification
  reports `5 passed, 1688 deselected` across PostgreSQL, MinIO, OpenSearch, durable workflow/outbox,
  and the Stage 2 integration surface.
- The branch basis remains the accepted Stage 2 commit `0ec1eb4`; shared Context commit `1b926f5`
  remains an ancestor and Stage 3 implementation `bab4451` remains excluded. A read-only three-way
  merge audit shows `21cc3c3` can be merged into the Stage 3 branch without code conflicts, but this
  run does not rewrite or merge the already accepted Stage 3 history.
- The external semantic evidence is still unavailable: the configured local LLM endpoint remains
  unreachable and no alternative credentialed endpoint is configured. Consequently the formal
  seven-case pilot/manifest, real configured/ablation outputs, and independent blind scores have
  not been generated, and no §12.2 semantic PASS is claimed.
