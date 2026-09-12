# R6 确定性验收矩阵与失败身份基线

日期：2026-09-12（+08:00）
范围：方案第 8 节 A01–A22、第 6 节 R6 第 1–2 项
状态：确定性矩阵已建立；真实 v7 运行（第 9 节 G0–G3）待执行。

## 1. 基线命令与结果（2026-09-12 复核）

| 命令 | 结果 |
|---|---|
| `pytest -q --no-cov tests/unit tests/contract` | **76 failed / 3008 passed / 1 skipped** |
| `pytest -q --no-cov tests/integration` | **19 failed / 123 passed** |
| `pytest -q --no-cov tests/integration --ignore=test_real_infrastructure.py` | **19 failed / 119 passed** |

integration 与 unit/contract 的失败身份集合在**两次独立运行之间逐项相同**，
且在“有真实基础设施”与“无基础设施”两种配置下**同为 19 项、集合完全一致**——
即真实服务只影响 `test_real_infrastructure.py` 的 4 项结果（现已全部通过），
不改变其余判定。

`test_real_infrastructure.py` 的 4 项（PostgreSQL 迁移与恢复、MinIO 往返、OpenSearch 索引与检索、
跨 MinIO/PostgreSQL/OpenSearch 的完整 outbox 投影）**全部 PASS**，
使用项目 Conda PostgreSQL + 本地缓存的 MinIO/OpenSearch/OTel 发行包，
在 `volumes/native/integration/...` 独立实例中运行。

### 1.1 R6 前必须修复的环境缺口（本轮已解决）

| 缺口 | 原因 | 处置 |
|---|---|---|
| `NativeInfraError: project Conda PostgreSQL binary is missing: initdb` | `native_infra.py` 以 `REPOSITORY_ROOT/.conda-env/bin` 解析二进制，而 `.conda-env` 是 gitignore 的、不随 worktree 创建 | 在整合工作树建立 `.conda-env` 符号链接（不进入版本库，`git status` 仍为 0 项） |
| `URLError: Temporary failure in name resolution` | `native_infra` 需联网下载 MinIO/OpenSearch/OTel 发行包，沙箱无 DNS | 从仓库既有 `tmp/native/dist` 与 `tmp/native/downloads` 缓存播种（二进制与归档，均不在版本库内） |

两者都是**环境**缺口而非代码缺陷；修复方式不改变被测代码。

## 2. 失败身份清单（整合前基线，全部为既有失败）

### 2.1 unit + contract（76 项）

完整清单：`tmp/yujin-remediation-20260912/R0/baseline-failures.txt`。
集中区域：`test_memory_benchmark_gate_formula` 8、`test_stage1_importer_negative_paths` 7、
`test_human_benchmark_compiler` 7、`contract/test_stage2_teacher_forced_e2e` 7、
`test_stage2_experiment_manifest` 6、`test_stage5_cli` 5。

### 2.2 integration（19 项）

| 文件 | 数量 | 主题 |
|---|---|---|
| `test_writer_context_loop.py` | 11 | MAJOR_REWRITE 路由、surface guard、recent-prose 重试、compaction 保留 |
| `test_production_planning_cadence.py` | 5 | 长度策略越界不得改 TextRoot、被阻塞计划替换 |
| `test_stage5_real_writer_e2e.py` | 1 | 真实 Writer adapter 组合 |
| `test_u6c_fault_matrix.py` | 1 | U6-C 故障矩阵 |
| `test_u6_continuous_replay.py` | 1 | U6-A 单次 basis 冻结 |

清单：`tmp/yujin-remediation-20260912/R6/integration-final-failures.txt`。

**结论**：本次 R0–R5 的全部改动**未新增任何失败身份**——unit/contract 与 integration
两侧的失败集合与整合前逐项一致（0 新增、0 修复）。

## 3. 确定性验收矩阵证据映射（A01–A22）

| 编号 | 场景 | 证据位置 | 状态 |
|---|---|---|---|
| A01 | v6 原六条字符串 action → host review REVISE | `test_obligation_contract.py`、`test_plan_review_obligation_contract.py` | ✅ |
| A02 | 八卷 16 条旧责任表逐条可追溯 | `scripts/audit_v6_responsibility_binding.py`（真实产物 16→16）+ `test_obligation_declaration_binding.py` | ✅ |
| A03 | 合法链路落地、偷建/未知 ID/非法动作被拒 | 同上 + `test_volume_stage_grid.py` | ✅ |
| A04 | 伪 waiver 拒绝、合法首章豁免通过 | `test_history_waiver_authenticity.py`、`test_writer_history_retrieval_gate.py` | ✅ |
| A05 | 未来章规划时无正文、执行时按当前历史检索 | 部分（`test_plan_unresolved_scope.py` 覆盖规划侧） | ⚠️ 待真实运行 |
| A06 | future-locked PROGRESS 与 101/201/350/401 边界 | `test_writer_history_retrieval_gate.py::test_future_locked_obligation_stays_in_scope_before_its_boundary` | ✅ |
| A07 | grounded block/span 与错 quote/snapshot/cutoff | `test_writer_history_retrieval_gate.py::test_grounded_canonical_prose_is_not_skipped_by_live_l0` | ✅ |
| A08 | REQUIRED 零 Need/零命中、缺 facet/ref/审批/exact | `test_writer_history_retrieval_gate.py`（A1–A4）、`test_evidence_first_writer_context.py` | ✅ |
| A09 | 重复 goal、图/顶层不一致、悬空依赖、环 | `test_plan_graph_scope_guards.py`、`test_writer_history_retrieval_gate.py::test_a9_*` | ✅ |
| A10 | 部分重叠窗口、父级替换、已写前缀与未来尾部 | `test_plan_graph_scope_guards.py` | ✅ |
| A11 | 卷入口/持续/出口与章节目标不同 | `test_volume_stage_grid.py` | ✅ 契约层 |
| A12 | 工作计划冒充历史、Editor avoided 强制缺口 | 宿主机理已核实（Editor 只能引用 Draft 内精确引文），端到端待真实运行 | ⚠️ 部分 |
| A13 | 六条以上变化、满四条末页、跨段依赖、无进展 | `test_ordinary_curation.py`（11 项） | ✅ |
| A14 | 义务正文观察、提前 payoff、到期未完成 | `test_obligation_observation_writeback.py`（5 项） | ✅ |
| A15 | Memory/首稿/局修/大改/复审后重启 | `test_writing_loop_repair_frontier.py`（计数与前沿）+ 恢复路径断言 | ⚠️ 端到端待补 |
| A16 | 错请求 hash、损坏原始响应、uncertain 调用 | `test_model_replay.py`（13 项） | ✅ |
| A17 | 8003 正确/错误模型、timeout/config 漂移 | R0 实时预检证据 + `test_production_bootstrap_endpoints.py`、`test_endpoint_preflight.py` | ✅ 身份层 |
| A18 | 真实旧 v4/v6 roots 与新 schema | `test_obligation_declaration_binding.py` + R0 的 v6 roots 只读加载 | ✅ 部分 |
| A19 | 多条反序匹配、lease 取消、跨 gateway 切片 | `test_writer_change_reconciliation_indices.py`（3 项） | ✅ 反序部分 |
| A20 | canonical 游标与 100→101 卷边界 | `tests/integration/test_plan_hierarchy_production.py::test_volume_boundary_retriggers_arc_volume_instead_of_next_chapter_set`（7 项文件全绿）、`tests/unit/test_stage5_vertical_runner.py::test_stale_zero_cursor_is_normalized_from_committed_projections` | ✅ |
| A21 | 单个英文违规词与白名单代号 | 三个入口同一判定器：`draft_surface_error`（`services/writer_cognition.py`），调用点为 Writer 首稿（`writer_cognition.py:614`）、局修/重试后（`writer_cognition.py:658`）、最终 Draft 物化（`adapters/runtime/materializers.py:1591`）；三处均以 `mandatory_constraints` 的 `正文语言：` 取目标语言、`language_allowlist_tokens` 取白名单 | ✅ 已登记 |
| A22 | 作者约束/证据超预算、四类 coverage | `test_planning_coverage.py`（8 项） | ✅ |

图例：✅ 已有确定性证据；⚠️ 部分或需真实运行补证；❌ 尚未覆盖。

### 3.1 A21 登记（中文门禁三入口一致性）

| 入口 | 位置 | 目标语言来源 | 白名单来源 | 失败语义 |
|---|---|---|---|---|
| Writer 首稿 | `services/writer_cognition.py:614` | `mandatory_constraints` 中 `正文语言：` | `language_allowlist_tokens(mandatory_constraints)` | `WriterCognitionError` |
| 局修 / 复读重试后 | `services/writer_cognition.py:658` | 同上 | 同上 | `WriterCognitionError` |
| 最终 Draft 物化 | `adapters/runtime/materializers.py:1591` | 同上 | 同上 | `CandidateMaterializationError` |

三处共用同一个纯函数 `draft_surface_error`，判定顺序、白名单语义与"目标语言为英文时跳过"条件一致，
不存在只在一侧生效的中文门禁。目标语言为空时三处都退化为不判定（`language is None`），
因为 `_split_composite_brief` 只从作者 brief 与风格指南编译 `正文语言：` 约束。


## 4. R6 未完成项

| 项 | 内容 |
|---|---|
| ~~A21~~ | **已完成**，见 3.1 节三入口登记 |
| A05/A12/A15 端到端 | 需真实 v7 运行补证 |
| ~~8003 预检补齐~~ | **已完成**，见第 5 节 |
| ~~G0 作者锁通道~~ | **已完成**，见第 7 节 |
| ~~修稿恢复崩溃~~ | **已修复**，见第 8 节（复现 + 修复 + 回归） |
| ~~accept 通过 / commit 才拒绝~~ | **已补齐两处 host review 漏口**，见第 8 节 |
| ~~作者锁语义~~ | **已修正**：不再替作者补 deadline；401 独立成机器边界；Writer 覆盖全部通道，见第 8 节 |
| ~~阶段网格与 coverage 口径~~ | **已接线**：作者约束作为 coverage 分母；卷阶段网格投影到 Writer，见第 8 节 |
| 义务 coverage 分母 | **未闭合**：分母仍来自候选自报声明；改为已接受 World 义务目录需要先决定"提案如何满足既有义务" |
| 真实 v7 运行 | 第 9 节 G0 配置与规划 → G1 两章 → G2 五章 → G3 20 章；G0 需以冻结代码重新冻结运行身份 |


## 5. 8003 预检实跑证据（2026-09-12）

命令：

```text
novel-agent preflight-endpoint --endpoint-profile qwen38_27b_nvfp4_8003 \
  --live-generation \
  --embedding-url http://127.0.0.1:8081/v1/embeddings \
  --reranker-url http://127.0.0.1:8082/rerank --timeout-seconds 300
```

结果（真实调用，exit=0，耗时约 5 秒）：

```json
{
  "declared_model": "qwen38-27b-nvfp4",
  "identity_matches": true,
  "generation_ran": true,
  "issues": [],
  "retrieval": {
    "embedding_model": "BAAI/bge-m3",
    "embedding_dimensions": 1024,
    "reranker_model": "BAAI/bge-reranker-v2-m3@953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
    "relevant_score": 0.9694971442222595,
    "irrelevant_score": 1.6124457033583894e-05,
    "discriminative": true,
    "issues": []
  }
}
```

覆盖项：模型身份、schema 约束输出、**中文长输出**（要求 ≥300 汉字并据此判定）、
usage 上报、模型版本、embedding 身份与维度、reranker 身份与区分度。

预检本身暴露并修复了两个真实缺陷：

1. **探针绕过预算绑定**：原先直接调用 `endpoint.adapter.generate`，适配器以
   `OpenAI adapter requires a gateway-bound EffectiveBudgetResult` 拒绝。现经最小
   `ModelGateway` 走真实调用契约，探针调用同时进入调用账本，不再隐藏一次真实模型调用。
2. **局部变量遮蔽参数**：`embedding_model: str | None = None` 遮蔽了同名参数，
   导致每次探测都发送 `model: null`，服务返回 422。静态检查无法发现，只有实跑暴露。

## 6. 回归纪律

本轮之后的每个增量都必须保持：

```text
tests/unit tests/contract  失败身份集合 == R0 基线（76 项）
tests/integration          失败身份集合 == 本节 2.2 清单（19 项）
```

比较对象是**具体测试身份**，不是失败数量。

## 7. G0 作者锁通道与 bootstrap 预算（2026-09-12 真实运行）

### 7.1 作者锁从"模型转述"改为"作者冻结通道"

**发现的缺陷**：v6 的 `ProjectProfileRoot` 中 `planning_constraints` 为空对象，
`author-constraint-root` 只编译出 1 条约束（`author-constraint.language.0`）。
brief 第 212–215 行明确写有第一卷内府禁止、第一卷末铜铭、断星六号不得完成核心回收、
第三碎片/ER-07 最早第三卷、长程真相最早第四卷后段，但这些**只以自然语言存在于 brief**，
由模型抽取，实际被全部丢弃。结果是 Planner 与 Writer 都拿不到作者声明的边界。

**修复**：`bootstrap-prepare --planning-locks <file>` 引入作者冻结通道。
文件在**绑定任何 endpoint 之前**校验并内容寻址；编译出的分道写入
`capability_profile.planning_constraints`，也就是 `compile_author_constraint_root`
与 Stage 3 Writer 锁投影已经在读的同一处。指向其它项目的锁文件 fail-closed。

`input/planning-locks.json` 声明 5 条锁：

| lock_id | category | 边界 | 来源 |
|---|---|---|---|
| `lock.inner-court.vol2` | timeline | not_before 101 / latest 200 | brief 212 |
| `lock.copper-token.vol1-end` | equipment | earliest 90 / latest 100 | brief 213 |
| `lock.duanxing-core.vol1-forbidden` | progression | not_before 101 / latest 200 | brief 213 |
| `lock.third-shard-er07.vol3` | reveal | not_before 201 / latest 300 | brief 214 |
| `lock.long-truth.vol4-late` | reveal | 无显式下界（作者只给"最早第四卷后段"） | brief 215 |

`timeline` 类强制要求同时给出 `not_before_chapter` 与 `chapter_latest`，
因为只有下界的"锁"可以被一个短规划视野静默满足；`reveal` 类允许不写下界，
作者没有声明 deadline 时不替作者发明一个。

证据：`tests/unit/test_author_planning_locks.py`（10 项，含推导根哈希、
陈旧哈希拒绝、作者通道覆盖模型自报、编译后进入 author-constraint root）、
`tests/unit/test_production_novel_bootstrap.py`（14 项，含 CLI 透传与跨项目 fail-closed）。

### 7.2 bootstrap 输出预算与请求超时是硬上限

v7 真实 G0 连续失败三次，全部是预算而非模型错误：

| 次 | 配置 | 结果 |
|---|---|---|
| 1 | 默认 12 000 输出 token / 300 s | `OpenAIChatOutputLengthError`（finish_reason=length） |
| 2 | 48 000 / 300 s | `TimeoutError`：300 s 时仍在生成 |
| 3 | 48 000 / 900 s（探针） | 375.4 s 后 `OpenAIChatOutputLengthError`，`output_tokens=48000`、`input_tokens=13019`、原始内容 81 761 字符 |

即：这份 800 章 brief 的 bootstrap Planner 响应**超过 48 000 token**，
v6 的同类响应当初在 12 000 以内完成。结论与处置：

- `BOOTSTRAP_REQUEST_TIMEOUT_SECONDS` 默认从 300 s 提到 900 s（`ModelRequest.timeout_seconds` 的域上限）；
- `--max-output-tokens` 与 `--bootstrap-timeout-seconds` 在 `bootstrap-prepare` 上暴露并记录，
  长程运行必须显式申报所请求的预算，而不是被一个 Stage 0 默认值静默截断。

这仍是 G0 的**未闭合约束**：在 900 s × 900 s 上限内能否产出完整响应，
取决于该模型对 800 章 brief 的真实输出长度，属于运行配置问题而非既有缺陷的回归。
G0 未产出 `state/genesis-prepared.json` 前，G1/G2/G3 都不启动。


## 8. 正文启动前的缺陷修复（2026-09-13）

按"先补齐规划契约、作者锁与恢复链路，再推进正文验收"的顺序，复现并修复了以下缺陷。
每条都给出复现方式、修复位置与回归证据。

### 8.1 修稿恢复崩溃（已复现 → 已修复）

复现：既有确定性 fixture（`tests/integration/test_writer_context_loop.py`）在较小
`max_post_draft_model_calls` 下写检查点时，实际抛出

```text
UnboundLocalError: cannot access local variable 'major_verification'
```

根因：`_persist_workflow_checkpoint` 读取的 `major_verification` / `local_repair_attempt` /
`rewrite_attempt` 只在 `LOCAL_REPAIR` 与 `MAJOR_REWRITE` 分支里初始化；正常 PASS 让出时这些
名字从未绑定。修复后这三项在任何 verdict 分支之前统一落定，`RepairStage` 成为
`domain/writing_loop.py` 中的单一类型别名，rewrite 分支进入复审时写入
`rewrite_review` 而不是停在 `rewrite_draft`。

新增回归：
`test_ordinary_pass_reaches_candidate_ready_with_a_dispatch_repair_frontier`、
`test_every_declared_repair_stage_round_trips_through_a_real_checkpoint`、
`test_local_repair_slice_records_the_local_review_stage`。

同时修掉测试夹具自身的缺陷：`_loop` 只注册 `skill.scene-composition`，导致 11 个既有
`MAJOR_REWRITE` 失败全部是"缺少 mode Skill"；并且 harness 按 DRAFT 模式构造 work plan，
而执行器会按当前模式重新规划。这一条与第 2.2 节 integration 基线清单相关，需要按"原因"
而不是"名称集合"复核。

### 8.2 accept 通过、commit 才拒绝（两处漏口已补齐）

复现（直接调用 host review，`decision=ACCEPT`、无 issue）：

| 输入 | 修复前 | 修复后 |
|---|---|---|
| 结构化 action 引用不存在的 obligation ID | ACCEPT | `OBLIGATION_ACTION_UNDECLARED` → REVISE |
| CHAPTER_SET 用 `obligation_declarations` 新建长期义务 | ACCEPT | `OBLIGATION_DECLARATION_FORBIDDEN` → REVISE |

`apply_host_plan_review_constraints` 新增 `accepted_obligation_ids`，由
`PlanReviewer.review` 从**可信 World root 制品**读出（`_accepted_obligation_ids`），
而不是从被审候选自报。没有目录时不判定"发明了 ID"，避免以猜测当证据。

同一轮补齐了生成侧契约：`planner_chapter_set_v1.md` 现在明确写出
`obligation_actions` 的 `{obligation_id, action, expected_delta}` 形状、`action` 的四个
枚举值、ID 必须取自可信上下文，以及本层级禁止任何形式的长期义务声明。至此
"生成说明 → 可信目录输入 → host 校验 → 最终物化"四处一致。

### 8.3 作者锁语义（已修正）

| 问题 | 处置 |
|---|---|
| 为"最早第二卷"自动补 `chapter_latest=200` | 改判为 `reveal` 锁，只保留作者声明的下界 101；不再替作者发明期限 |
| "第五卷起正式推进"只有自然语言 | 独立成 `lock.long-truth.vol5-advance`（progression，not_before 401）；350 仍为"开始暗示"的 reveal 锁 |
| 无任何边界的锁会静默编译成空 | 加载时 fail-closed：非 timeline 锁必须声明至少一个章节边界 |
| 铜铭只进了 profile，没进 Writer | Writer 锁投影改为遍历全部五个通道（含 `equipment_locks`、`location_preconditions`） |
| `chapter_latest` 被当成"锁到某章" | 上界现在报告为 deadline，与下界的"locked until"区分 |

v7 锁文件现为 6 条：101（内府）、90-100（铜铭）、101（断星六号核心禁令）、
201-300（第三碎片/ER-07）、350（长程真相暗示）、401（长程真相正式推进）。

### 8.4 阶段网格与 coverage 口径（已接线）

- **作者约束 coverage**：生产调用原先不传约束，实测 `author_constraint_coverage: 0/0`；
  现在 `PlanReviewer.review` 从可信制品读 `AuthorConstraintRoot` 作为分母，删除全部
  作者约束的提案不再得满分。回归：
  `test_author_constraint_coverage_uses_the_frozen_catalogue_as_denominator`、
  `test_author_constraint_coverage_credits_a_restated_constraint`。
- **章节分母**：`apply_host_plan_review_constraints` 接受 `trusted_window`，可由任务
  窗口提供，不再只能从候选自身推导。
- **卷阶段网格**：`entry_conditions` / `exit_conditions` / `reveal_window` /
  `capability_ceiling` / `equipment_ceiling` 现在从覆盖当前章的卷节点投影进
  `mandatory_constraints`，并且 text / list / 结构化表三种写法都能读出。
  回归：`test_volume_stage_slots_reach_the_writer_in_every_declared_shape`。
- **仍未闭合**：义务 coverage 的分母仍来自候选自报声明。把它改成已接受 World 义务目录
  需要先确定"提案如何算满足一条既有义务"，本轮不做半成品改动，明确记入未完成项。

### 8.5 本轮回归结果

```text
tests/unit tests/contract  76 failed / 3038 passed / 1 skipped
                           失败身份集合 == R0 基线（逐条 diff，IDENTICAL）
```

本轮新增通过测试：作者锁 18 项、host review 义务契约 12 项、修稿恢复与阶段前沿 3 项、
中文门禁登记等，合计新增约 21 项确定性通过。未运行完整 `make quality`，未修改覆盖率阈值。
