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
| A15 | Memory/首稿/局修/大改/复审后重启 | `test_writing_loop_repair_frontier.py`（计数与前沿）+ 两个真实执行用例（大改前/大改后重启）见第 13 节 | ✅ 执行证据已补 |
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
| A05/A12 端到端 | 需真实 v7 运行补证（A15 的确定性执行证据已在第 13 节补齐） |
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
  （当时只做到"能读出"，卷首/卷中/卷尾尚无区别；第 12 节补齐窗口语义。）
- **仍未闭合**：义务 coverage 的分母仍来自候选自报声明。把它改成已接受 World 义务目录
  需要先确定"提案如何算满足一条既有义务"，本轮不做半成品改动，明确记入未完成项。

### 8.5 本轮回归结果

```text
tests/unit tests/contract  76 failed / 3038 passed / 1 skipped
                           失败身份集合 == R0 基线（逐条 diff，IDENTICAL）
```

本轮新增通过测试：作者锁 18 项、host review 义务契约 12 项、修稿恢复与阶段前沿 3 项、
中文门禁登记等，合计新增约 21 项确定性通过。未运行完整 `make quality`，未修改覆盖率阈值。

## 9. 真实运行打通记录（v12，2026-09-13）

### 9.1 阻塞真实运行的是一个身份口径 bug，已修复

v7 到 v11 每一次 `dispatch` 都在第一个任务之前失败：

```text
RUN_CONFIGURATION_CHANGED
```

根因是同一个配置指纹的 `profile_root_hash` 输入在两条路径上取了不同的值：

| 路径 | 取值 | 结果 |
|---|---|---|
| bootstrap commit | `manifest.project_profile_root.artifact_id` | 写进 descriptor |
| 运行装配 | 文档内部的 `profile.root_hash` 字段 | 装配时重新计算 |

`BootstrapRootBuilder` 存储根文档时**包含** `root_hash` 字段，因此制品的内容地址与该字段天然不同
（v11 实测：制品 `26382340…`，字段 `4a527b23…`）。只替换这一项就能精确复现冻结的 policy hash，
说明口径不一致就是全部原因。

修复后实测（v12，同一份冻结配置）：

```text
descriptor policy_hash = 与装配 attestation 一致
dispatch → status = progressed → run = waiting
```

`RUN_CONFIGURATION_CHANGED` 现在同时输出 `frozen=` 与 `observed=`，因为裸 code 读起来像"操作者改了配置"，
掩盖了究竟是哪个输入动了。回归：`test_root_document_identity_is_the_manifest_content_address` 固定
"两个身份确实不同"这一事实，防止口径再次分叉。

### 9.2 v12 首次在真实调用链上跑通 STORY

```text
run.yujin-jiuxu.v12.plan              plan_candidate   succeeded   level=story
run.yujin-jiuxu.v12.plan.accept       plan_acceptance  succeeded   level=story
run.yujin-jiuxu.v12.plan.accept.commit plan_commit     blocked     validation_rejected
```

真实模型调用 4 次（`logical_phase=development`，全部 `completed`）。STORY 提案 9 条、
coverage 0.95、2 条非阻断 unresolved，经 host review 后由作者接受并生成 commit 任务。

这是本项目第一次让"bootstrap → 装配 → dispatch → 规划 → 审校 → 接受"整条链在真实端点上完成，
而不是停在指纹校验。

### 9.3 仍未闭合的项（按优先级）

| 项 | 现状与下一步 |
|---|---|
| STORY commit 被拒 | `plan_commit` 任务 `validation_rejected`，事件只带 failure_class，没有细节。需要先让该拒绝输出具体失败项，再判断是数据还是校验问题 |
| 八卷与首批五章 | STORY 提案只有 3 条卷结构（第一卷、第三卷、第四卷），没有完整八卷，也没有章节目标。G0 未通过 |
| 审校输入缺口 | `planning_context_loop` 调用审校器时未传 World 根与作者约束根；目录读取器只认专用媒体类型，真实 World 根是 `application/json`；`trusted_window` 生产未传 |
| Genesis 未来事件分类 | bootstrap 把第四卷"唐钧打造沉曜"写成 `valid_time.start_ordinal = 0` 的当前事实，而 Writer 会把参与人物状态注入 `Canon current state`，与时间锁直接冲突 |
| 卷阶段时间语义 | 阶段槽投影未按条目章节窗口筛选，卷首/卷中/卷末收到同一组约束 |
| 修稿恢复执行证据 | 已闭合，见第 13 节（`REPAIR_PENDING` 真实写入 + 两处中断恢复执行证据） |
| 大改路径夹具 | 8 个相关单测文件实测 **1 失败**：`test_chapter_set_prompt_binds_each_horizon_chapter_to_a_goal`（断言英文串，prompt 是中文，R0 基线既有） |

八卷与章节目标未产生之前不进入两章 smoke。

## 10. 审校入口缺口的两处修复（2026-09-13）

### 10.1 义务声明缺少可读 kind（真实复现 → 已修）

用 v12 真实接受绑定直接调用 `PlanCandidateMaterializer`，复现出 commit 被拒的确切原因：

```text
CandidateMaterializationError: obligation declaration has an unknown kind
```

来源是 STORY 提案里的 `story.obligation.reveal_lock`：`kind="obligation"`，
payload 里只有 `title` 与 `constraints`，**没有 `obligation_kind`**。
host review 当时返回 ACCEPT，于是又走了一遍"审校通过、commit 才拒绝"。

修复：host review 现在复刻物化器的直接声明面——item 自身 kind 是义务类
（含字面量 `"obligation"`）或带嵌套 `obligation` 对象时，必须给出可读的
`obligation_kind`；给出未知值同样 REVISE。旧版 `obligation_plan` 责任表与
`obligation_declarations` 列表各有自己的检查，不被当作直接声明，因此卷级计划不受影响。

回归：`test_a_declaration_without_an_obligation_kind_is_revise`、
`test_an_unknown_obligation_kind_is_revise`、
`test_a_legacy_responsibility_table_is_not_treated_as_a_direct_declaration`。

### 10.2 目录读取没有沿可信引用继续（已修）

两个目录读取器此前只在自己收到的制品列表里按专用媒体类型找，而规划循环传的是
**Planner context package**，真实 World 根的媒体类型又是 `application/json`。
结果是真实路径上目录返回空：未声明义务的 action 与作者约束分母同时失效。

修复：审校现在读取 host 组装的 `PlannerContextPackage`，并沿它的
`profile_ref` 与 `author_constraint_root_ref` 继续取用；没有 World 根引用时，
义务目录从可信 profile 携带的声明按 host 自己的标识约定（
`obligation.<item_id>.<ordinal>.<kind>`）推导。目录确实读不到时仍返回 `None`
而不是空集合，让检查报告"缺失"而不是放行。

### 10.3 仍未闭合

| 项 | 现状 |
|---|---|
| Genesis 未来事件分类 | v12 把第四卷"唐钧打造沉曜"写成 `truth_class=accepted_world_fact`、`start_ordinal=0`。`world_graph` 与 R1 只把 `ACCEPTED_WORLD_FACT` 当 Canon，因此这类条目会被当作开篇事实；正确落点是把作者声明的未来意图标成 `PREDICTION`（或等价非 Canon 类），而不是当前事实。**尚未修** |
| 卷阶段时间语义 | 阶段槽投影未按条目章节窗口筛选，且已属 `relevant_nodes` 的卷节点被排除在阶段投影之外，卷首/卷中/卷末收到同一组约束。**尚未修** |
| STORY 八卷与章节目标 | STORY 提案只有 3 条卷结构、无章节目标。G0 未通过 |
| 修稿恢复执行证据 | 已闭合，见第 13 节 |

## 11. 审校与物化统一到同一份义务声明契约（2026-09-13）

### 11.1 两份实现互相矛盾，两个方向都错

用 v12 真实候选逐项对照，发现审校与物化对同一 payload 的判断不一致：

| 输入 | 物化器 | 旧审校 |
|---|---|---|
| 线上坏条目：`kind="obligation"`、只有 `title`+`constraints` | 拒绝（unknown kind） | **ACCEPT** |
| 合法：直接 `obligation_kind=objective` + summary | 接受 | **拒绝**（OBLIGATION_KIND_MISSING） |
| 合法：嵌套 `obligation.kind=objective` | 接受 | **拒绝** |
| 只补 kind、没有描述 | 拒绝（requires a description） | 通过 |

也就是说"审校通过、commit 才拒绝"这条故障模式仍然活着，只是这次以另一种形式出现。

### 11.2 统一到 `domain/obligation_contract.py`

契约模块现在是唯一owner：`parse_obligation_declarations` 按绑定顺序读取全部声明面
（item 自身 kind、`obligation_kind`/`obligation_type`、嵌套 `obligation`、
`obligations`/`obligation_declarations`/`key_obligations`/`obligation_declaration`、
旧版 `obligation_plan`），逐条报告不可读原因；`claims_direct_obligation` 判定"断言了一条义务"；
`declared_obligation_identities` 把 host 的标识约定暴露一次。审校与物化都调用它们。

配套修正：`not_before_chapter` 现在随声明一起传递（此前在转换中丢失，导致长程
`promise`/`foreshadowing` 报"requires not_before_chapter"）。

回归：`tests/unit/test_obligation_declaration_binding.py`、`test_obligation_contract.py`、
`test_plan_review_obligation_contract.py` 全绿；`tests/unit tests/contract` 与 R0 基线
逐条一致（76 项）。

### 11.3 目录改回 World 权威来源

`_accepted_obligation_ids` 不再读 `Profile.capability_profile.obligation_declarations`
（真实 v12 Profile 没有该字段），也不再复制 ID 算法。目录来自装配绑定的已接受 World 根
（`manifest.world_root`），由 `PlanReviewerAgent` 在构造时持有；没有 World 根时返回
`None` 表示"无法核验"，与"目录为空"区分开。

### 11.4 v12 候选的正确处置：修订，不是补 kind

用统一契约重跑 v12 的接受绑定，仍然拒绝，且原因更清楚：

```text
obligation declaration is not readable:
item.story.obligation.reveal_lock[0] declares no obligation kind;
name one of foreshadowing, promise, objective, unresolved_conflict
```

该条目本质是作者锁汇总，属于约束上下文而非新增义务，**不应**通过补一个占位 kind 让它过关；
旧候选必须修订并重审。已把契约写进 `planner_story_v1.md`：声明需要 kind 与非空描述、
作者锁不是本章新增义务、占位 kind 不是通关手段。

### 11.5 仍未闭合

| 项 | 现状 |
|---|---|
| Writer 忽略 `truth_class` | `stage3_writer` 把参与人物的**任何**状态注入 `Canon current state`，所以即使 Genesis 把未来条目标成 `PREDICTION`，它仍可能以"当前事实"进入正文。需要在注入侧按 `truth_class` 过滤（**尚未修**） |
| Genesis 未来事件分类本身 | 关键词 + 作者锁文本匹配只能捕捉字面含"第N卷/最终/后续/成长路线"的条目；像"唐钧在钧炉城锻打沉曜"这种**不含卷号**的整段人物描述仍判为当前事实。需要拆分描述（当前身份 vs 未来经历）或由 Curator 显式给出时间意图（**尚未完成**） |
| 卷阶段时间语义 | 已修，见第 12 节（条目窗口优先，自由文本按槽位语义绑定阶段） |
| `REPAIR_PENDING` | 已修，见第 13 节（写在大改/局修派发之前，恢复只补未结算的那一步） |
| G0 八卷与首批五章 | 未进入 ARC_VOLUME；STORY 候选需按新契约重新生成 |

## 12. 卷阶段真正区分卷首/卷中/卷尾（2026-09-13）

### 12.1 缺陷

`volume_stage_grid_defects` 只要求结构化条目申报窗口，投影侧并不使用这些窗口：
`_stage_slot_texts` 把 `chapter_start/chapter_end` 当成一句说明文字拼进约束，于是**任何**
条目都会到达该卷的每一章；`covering_volume_nodes` 又把"已经是目标父节点"的卷排除在外。
结果是"十个非空字段"在正文侧仍然是同一组约束，卷首与卷尾收到的要求完全一样，
卷出口结果可以在第一章就被兑现。

### 12.2 实现（`adapters/runtime/stage3_writer.py`）

| 机制 | 语义 |
|---|---|
| `VolumeStageSlot` | 阶段条目连同它**自己声明的**窗口；`declares_window` / `covers(chapter)` / `window_label` |
| `_volume_stage_positions` | 章在自己范围内的位置：首章为卷首、末章为卷尾、中间连续三等分，`(opening)`/`(middle)`/`(closing)` |
| `VOLUME_STAGE_SLOT_SCOPE` | 自由文本槽的阶段归属：`entry_conditions`→卷首、`exit_conditions`→卷尾、`reveal_window`/两个 ceiling→整卷 |
| `_volume_stage_constraints` | 结构化条目**只在其窗口内**绑定；自由文本按槽位归属绑定；窗口外不再作为本章要求 |

两处补齐的语义：

- **不得提前兑现卷出口**：卷首/卷中看到 `exit_conditions` 时，投影为
  `当前卷阶段[卷中·出口未到期:exit_conditions]：…（本章不得提前兑现或解决本卷出口结果）`，
  与既有 `EARLY_RESOLUTION` 判定同向；
- **入口条件不是新任务**：卷中/卷尾看到 `entry_conditions` 时，投影为"本卷入口条件已经成立，
  本章不得与之矛盾"，而不是要求本章重新达成入口状态。

覆盖范围同时放开：只要节点**自己的**章节范围覆盖本章就参与投影，不再因为它是目标父节点而被跳过。

### 12.3 证据

```text
tests/unit/test_author_planning_locks.py::test_volume_stage_grid_binds_free_text_slots_to_their_own_stage
tests/unit/test_author_planning_locks.py::test_a_declared_stage_window_outranks_the_slot_scope
tests/unit/test_author_planning_locks.py::test_volume_stage_slots_reach_the_writer_in_every_declared_shape
tests/unit/test_author_planning_locks.py  20 passed
```

用例对同一卷的第 1、5、9 章分别断言三组**不同**约束，并断言窗口 `4-6` 的条目在第 2 章与第 9 章
都不出现、只在第 5 章出现。

回归（`tests/unit tests/contract`，冻结基线对比）：

```text
76 failed / 3048 passed / 1 skipped
new: []   fixed: []   identical: True
```

未运行完整 `make quality`，未修改覆盖率阈值。

## 13. 修稿前沿真实写入与中断恢复（2026-09-13）

### 13.1 缺陷一：`REPAIR_PENDING` 从未被写入

`WritingLoopPhase.REPAIR_PENDING` 与 `repair_stage` / 两个计数器在 R5 就已进入契约，
但**没有任何代码写它**：循环只在 EDITOR_PENDING / OBSERVER_PENDING /
RECONCILIATION_PENDING / REACTIVE_MEMORY_PENDING 四个前沿落盘。于是"修稿中重启"落到
最近的 EDITOR_PENDING 前沿，恢复入口会重新跑一次初始 Editor 复审（多花一次调用），
而修稿配额只能等到下一次切片让出时才被持久化。

### 13.2 缺陷二：大改路径在生产上必然失败

`WriterCognitionService.take_turn` 对 `MAJOR_REWRITE` 要求 work plan **选定**
`skill.major-rewrite`（模式能力边界），但循环拿到的是 DRAFT 模式的 work plan，
从不按模式重新规划：

```text
WriterWorkPlan is missing the required mode Skill: skill.major-rewrite
```

生产 allowlist 本身包含该 Skill（`production_assembly_spec.json` 的 `writer_skill_ids`），
所以这不是配置问题，而是循环缺少"按模式重规划"这一步：**任何 MAJOR_REWRITE 判定都会
以 WRITER_FAILED 结束**。integration 夹具注释写着"the Writer is re-planned per mode"，
实现里没有对应代码。

### 13.3 实现

| 位置 | 机制 |
|---|---|
| `_persist_repair_checkpoint` | 以 `phase=REPAIR_PENDING` 落盘，带 `repair_stage`、已用配额、触发修稿的复审输入与拒绝历史、以及已结算的候选（局修 / 大改 / 首稿） |
| 局修 | 派发**之前**写 `repair_stage="dispatch"`；局修结算后写 `"local_review"`（带 `repaired_draft`）；两处都写 |
| 大改 | 每次尝试派发前写 `"dispatch"`；大改写结算后写 `"rewrite_review"`（带 `rewritten_draft` 与它的 memory hints） |
| 恢复入口 | `phase=REPAIR_PENDING` 直接取 `reports[-1]` 与 `checkpoint.repair_input`，不再重跑初始复审；`repaired_draft` / `rewritten_draft` 已结算时跳过对应的模型调用，只补未结算的那一步 |
| `rewrite_awaiting_review` | 区分"已结算待复审"与"需要新一次尝试"：只有后者才递增计数并重新规划+派发，避免用同一次大改反复复审 |
| `_settle_work_plan` | 首稿与大改共用同一段"把 work plan 绑定进 view"的逻辑；大改前按 `MAJOR_REWRITE` 重新规划并替换 WORK_PLAN 条目，再重新取得 provider-validity receipt |

### 13.4 证据

```text
tests/integration/test_writer_context_loop.py
  test_a_restart_after_a_settled_repair_only_owes_the_independent_re_review
  test_a_restart_inside_a_dispatched_repair_returns_to_that_repair
  52 passed（本文件，修复前 11 failed / 39 passed）
tests/unit/test_writing_loop_repair_frontier.py  6 passed
```

两个执行用例的做法与断言：

- 第一个用例让局修结算、复审失败：失败的 `artifacts` 里同时存在
  `["dispatch", "local_review"]` 两个 `REPAIR_PENDING` 前沿；用后者恢复时 Editor 只被喂一个
  PASS 响应就必须到达 `DRAFT_CANDIDATE_READY`——若恢复重跑局修，请求类型不匹配会直接失败。
  事件账本同时断言 `EDITOR_REPAIR_SETTLED == 1`、`EDITOR_REVIEW_SETTLED == 2`。
- 第二个用例让局修**未结算**（Editor 返回非法修复载荷）：只剩 `"dispatch"` 前沿，
  `repaired_draft is None`、`local_repairs_used == 0`；恢复后按顺序消费"修复→复审"两个响应，
  最终候选不同于首稿，且 `EDITOR_REPAIR_SETTLED == 1`（同一次尝试，不是重新发放配额）。

### 13.5 integration 基线按原因复核（17 → 6）

`test_writer_context_loop.py` 的 11 项既有失败全部来自上述两个缺陷与三处过期断言，
逐条原因如下：

| 原因 | 处置 |
|---|---|
| 夹具从未调用已存在的 `_with_mode_skill`，大改请求缺模式 Skill | 7 个用例接入 `_with_mode_skill` |
| 循环不按模式重规划（生产缺陷，见 13.2） | 修复循环；夹具为每次大改尝试提供一份模式计划 |
| 夹具 `writer_turns` 只为多次大改提供一份计划响应 | 每次尝试"计划+回合"成对提供 |
| 断言英文 prompt 文案，实际 prompt 已中文化 | 3 处改为断言当前中文原文（`writer_work_plan_v1`、`writer_turn_v1`、`writer_major_rewrite_v1`） |
| 断言 `_META_RELATION_MARKER` 与"已知短语改写表" | 该表是**私有基准内容**写进生产代码，T2–T6 已撤销，不应回归；改为断言通用门禁（内部标记、章节标签、复读、目标语言）并要求项目专有短语**不再**被门禁拒绝 |
| 断言"allowlist 不再覆盖已结算计划"的具体文案 | allowlist 现在先于计划校验，改为断言更强的 `missing a required base Skill` |

切片预算随之调整：大改每次尝试多一次"模式计划"调用，两个相关用例的
`max_post_draft_model_calls` 相应提高（生产策略 6 次仍覆盖"复审+模式计划+大改回合+复审+观察"）。

另外两项原因也已在同一轮复核，其中一项是生产缺陷：

| 用例 | 原因 | 处置 |
|---|---|---|
| `test_stage5_real_writer_e2e.py::test_real_writer_adapter_composes_through_draft_chain` | **生产缺陷**：计划的章节目标投影只在 `CHAPTER_SET` + 滚动窗口下按 id 失效旧目标；其他层级/无窗口时，同一 `chapter_index` 的旧目标会与新目标**同时留下**，`model_copy` 不跑校验，于是一份自己的读取端会拒绝的 PlanRoot 被写进提交，直到回读才炸 | `materializers` 的目标合并改为"同章号必然替换"（不依赖层级与窗口），并记入回归；不回退读取端校验 |
| `test_production_planning_cadence.py::test_blocked_plan_replacement_supersedes_and_does_not_reuse_task_id` | 夹具断言的是无 run 前缀的旧任务 ID 字面量；当前身份是 run 作用域（`run.replace.plan.chapter-set.1-5.g1`），同文件单测用 `endswith` 早已按新口径断言 | 夹具改为断言 run 作用域的完整身份，替换语义（新身份、g+1、原任务 superseded）不变 |

integration 全量结果：

```text
tests/integration（-m "not model_required and not integration"）
修复前基线 17 failed / 93 passed
现在        4 failed / 111 passed
fixed: 13   new: 0
```

剩余 4 项全部是同一处**语义冲突**，需要设计裁决而不是改夹具（见 13.6）。

### 13.6 待裁决：长度契约在提交端"重试"还是"直接要人看"

`test_production_planning_cadence.py::test_final_draft_outside_length_policy_cannot_mutate_text_root[4 组]`
要求：已接受但超出 `length_policy` 的正文在提交时判 `BLOCKED` / `REVIEW_REQUIRED`，
`reason=draft_length_contract_rejected`。设计文档（`docs/Novel-System_分层规划与渐进Skill_
收敛版补丁执行设计_v2_ee8849a.md` §"Stage 5 单独映射"）写的是同一件事，并明确"不自动重写"。

当前实现（`services/creative_runtime.py`，由 `b64d461 fix(runtime): recover continuation planning
and retry short drafts` 引入，且有单测 `test_advance_draft_commit_length_contract_error_waits_for_retry`
锁住）走的是另一条路：

```text
AttemptOutcome.SUSPENDED / WAITING_RETRY / LEAF_SCHEMA_REJECTED
reason = draft_length_contract_retry
```

两条都是"已经有测试锁住的现状"，不能靠改夹具掩盖。事实层面：提交任务重试时读到的是**同一个
已接受候选**（正文已冻结），所以重试不会产生更长/更短的正文，只会在 `failure_budget` 用尽后
回到同样的终点，并把可诊断的 `draft_length_contract_rejected` 藏进 `LEAF_SCHEMA_REJECTED`。

需要决定（本项未闭合）：

- **A（建议）**：提交端恢复设计文档语义——`BLOCKED` / `REVIEW_REQUIRED` /
  `draft_length_contract_rejected`，不自动重试；同步更新那条单测。
- **B**：保留重试语义，把设计文档与集成用例一起改成"允许有界重试"，并说明重试到底改变了什么
  （目前看不出有什么会变）。

### 13.7 回归（确定性）

```text
tests/unit tests/contract  76 failed / 3048 passed / 1 skipped
new: []   fixed: []   identical vs R0 baseline: True
```

未运行完整 `make quality`，未修改覆盖率阈值。
