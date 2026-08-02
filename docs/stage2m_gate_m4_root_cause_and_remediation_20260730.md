# Stage 2M Gate M4 根因分析与修复方案

文档生命周期：`ACTIVE_REMEDIATION`
日期：2026-07-30
状态更新：2026-08-02；P0/P1 与正式 Gate contract 已实现，P2 已接受为诊断证据；C40
currentness 和 C60 模型超时已修，C60 support-group/claim synthesis 质量仍未通过；P3 未执行
范围：WP7；`visible_at_cutoff`（VAC）与 `author_plan_conditioned`（APC）各
P001-P005 / C20-C95；Arm A deterministic
当前决策：**Gate M4 HOLD；正式 WP8 冻结；P2 = ACCEPTED_AS_DIAGNOSIS；当前只修 C60
rank-complete evidence 到完整 claim 的损失，明确提升后再做同类 C95 canary；严格 P3/Gate 门
保留到最终发布**

本文的 2026-07-30 根因数据保留为修复前基线；从第 1.1 节起记录当前代码与诊断运行状态。
开发人员应按第 7 节“当前状态与剩余动作”继续，不得把已经实现的 P0/P1 重新当作未开发需求，
也不得把 2026-07-31 的受限 WP8 或 2026-08-01 的 P2 诊断运行解释为正式 P3 或正式 WP8。

## 1. 2026-07-30 修复前结论

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

### 1.1 2026-07-31 当前复核

当前复核结论不是“修复尚未编码”，而是“修复代码已形成，但真实质量和正式运行尚未验收”：

1. P0/P1 的核心实现已经进入当前工作区：
   - evaluator receipt host validation；
   - `gate_metric_formula.v1` 与 content-addressed Gold metric contract；
   - APC canonical alias registry；
   - public NeedFacet / NeedCompletionSpec；
   - deterministic max-min scheduler；
   - ClaimSupportGroup、support receipt、ClaimVariant 和 ContextAssemblySpec；
   - support-aware selection 与 deterministic Assembler validation/packing。
2. 正式 `MemoryBenchmarkReporter` 已冻结 `stage2m_wp7_arm_a.v1`：只接受 Arm A 的
   C20/P001、C40/P002、C60/P003、C80/P004、C95/P005，要求五点共享同一冻结 run
   identity，并校验 VAC/APC 固定分母。诊断模式不能输出 `gate_passed=true`。
3. 2026-07-31 受限 WP8 诊断产出了双 profile 十个 Arm A，但十个
   `stage2m_case_C*_A.json` 都是旧 case schema，缺少当前正式门必需的六个字段：
   `code_version`、`run_config_hash`、`benchmark_contract_hash`、`matcher_version`、
   `writer_token_budget`、`evidence_ledger_token_budget`。当前代码会 fail closed；禁止事后
   backfill 后把旧运行签为 P3。
4. 十个旧单点 `scenario_run.completed=false`，APC 根 report 只覆盖 C20，VAC 根目录没有
   五点 unified report，因此生命周期和统一报告也不满足 P3。
5. 从旧冻结 evaluator bundle 得到的只读诊断值为：

| Profile | Current-state | Operational/plan | Historical | UNTRACEABLE | Contradiction |
|---|---:|---:|---:|---:|---:|
| APC | 3.50% | 19.79% | 36.78% | 7 / 72 | 0 |
| VAC | 9.00% | 8.45% | 32.18% | 8 / 47 | 0 |

以上所有质量轴均未达到 95% / 95% / 90%，且两个 profile 都触发 trace hard veto。
APC C40 从 `EVIDENCE_INSUFFICIENT` 变为 READY、VAC C40 coverage 提升，只能证明局部行为
变化，不能证明 M4 通过。

### 1.2 当前工程质量门

- Stage 2M 聚焦整改测试已通过；
- freeze/reveal integration：1 项通过；
- Ruff lint、Ruff format、严格 MyPy：通过；
- 最新 `make quality`：1614 项测试通过、9 项 deselected，Ruff、格式、严格 MyPy 通过，
  statement/branch coverage 均为 100%；
- 该工程质量门已关闭，但不替代 P2 哨兵或正式 P3 的真实运行证据；日常 canary 不把全仓
  `make quality` 作为迭代门，准备合并和正式 P3 前仍必须正常通过，不得静默添加 `--no-cov`
  或降低阈值。

因此当前开发入口是 C40/C60/C95 三点内容质量修复，不是继续扩展 P0/P1 治理、重复五点 P2
或运行 B/C。

### 1.3 2026-08-01 P2 真实哨兵执行结果（diagnostic/non-formal）

在当前修复版本的 clean execution copy 上，使用本地真实 Qwen3.6 API
(`http://127.0.0.1:8002/v1`，`qwen36-27b-nvfp4`) 完成了 M4 专项规定的五个单点。
每个单点均写出 `diagnostic_partial_report_A.json`，运行过程无 blocker，检索和语义后端
均 eligible，`Assembler` 为 `READY`，且 `scenario_run.completed=true`；这些产物均明确
`formal_contract_validated=false`、`gate_passed=false`，没有调用不完整矩阵聚合器。

| Profile / checkpoint | Experiment / 产物目录 | 运行与诊断结论 |
|---|---|---|
| APC P002/C40 | `stage2m-p2-remediation27-apc-c40-20260801` / `/tmp/ns-stage2m-formal-p2-remediation27-apc-c40-20260801` | `real_hybrid_completed`；weighted `0.0926`、mandatory `0.0909`；P002-G02 `MISS`、P002-G06 `UNTRACEABLE` |
| VAC P002/C40 | `stage2m-p2-remediation26-vac-c40-20260801` / `/tmp/ns-stage2m-formal-p2-remediation26-vac-c40-20260801` | `teacher_forced_real_hybrid_completed`；P002-G02 仍为 `MISS`，但对应 support group 已进入最终 Evidence Ledger |
| VAC P003/C60 | `stage2m-p2-remediation26-vac-c60-20260801` / `/tmp/ns-stage2m-formal-p2-remediation26-vac-c60-20260801` | `real_hybrid_completed`；最终 Gold verdict 全部为 `MISS` |
| VAC P004/C80 | `stage2m-p2-remediation27-vac-c80-20260801` / `/tmp/ns-stage2m-formal-p2-remediation27-vac-c80-20260801` | `real_hybrid_completed`；`trace_complete=false`，P004-G05 为 `UNTRACEABLE`，其余为 `MISS` |
| VAC P005/C95 | `stage2m-p2-remediation27-vac-c95-20260801` / `/tmp/ns-stage2m-formal-p2-remediation27-vac-c95-20260801` | `real_hybrid_completed`；`trace_complete=false`，P005-G09 为 `UNTRACEABLE`，其余为 `MISS` |

P2 结果暴露并关闭了一个真实的 `selected → assembled → ledger` 丢失点：support-aware
selector v3 现在会保留已选的 optional completion support group，VAC C40 的 P002-G02
对应 group 已真实落入最终 ledger。该项仍被 evaluator 判为 `MISS`，原因是模型结论只表达了
证据中的一个对话事实，未完整表达 Gold 要求的“挡在落落身前并持短剑挡敌”结论；这不是
candidate/selection/assembly/ledger 链再次静默丢失。另一方面，C80/C95 的 trace 不完整和
`UNTRACEABLE`，以及各点大量 semantic `MISS`，说明 P2 已完成诊断但尚未证明修复效果；这
只决定下一轮修复目标，不阻塞开发。P3 仍未启动，因为三点 canary 尚未显示整体接近正式门。

### 1.4 2026-08-01 直接修复与 C90–C95 续跑收口

本次执行已经完成被真实运行直接证明的修复，不再重复跑完整 C40–C95。保留的运行证据如下：

| 运行 | 直接证据 | 当前处置 |
|---|---|---|
| `remediation35` APC C40 | C45 暂停时定位到模型反复引用 `entity.tianhai-yaer` 却未先发出 CREATE；这是 Curator proposal repair contract 的缺口，不是放宽 dangling-reference 校验的理由 | 已补 V2 onboarding/repair contract，并保留该暂停产物作为根因证据 |
| `remediation36` APC C40 续跑 | 从 C44 恢复后成功跨过 C45，继续提交到 C89；随后才因本地 OpenSearch shard 上限停止 | C45 修复已由真实 Qwen3.6 运行验证；不重做已完成章节 |
| `remediation37` APC C89 续跑 | C90–C95 章节提交本身已完成；独立续跑的 formal lifecycle 未闭合，因此不冒充完整 P2/P3 report | 保留章节完成证据，不再为补齐旧 lifecycle 重跑 C40–C95 |

对应的最小工程修复已落盘并有回归覆盖：

- V2 Curator 在归一化后对同一 `(record_kind, target_id)` 做确定性合并；真正语义冲突在
  materialization 前 fail closed，避免重复 target 把后续章节拖入不可解释的 materialization failure；
- V2 proposal 与 repair prompt 明确要求新命名实体先以证据支持的精确 ID 发出 CREATE，之后才允许
  state/relation/event/obligation 引用；
- 本地单节点 OpenSearch projection index 明确设置 `number_of_replicas=0`，避免不可分配副本
  消耗 `LOCAL_ONLY` shard budget 阻断长回放；这只修复基础设施配置，不改变 Gate 语义。

误启动的 `remediation38` 全量运行已停止，精确前缀没有留下临时索引。当前结论保持：
`P2_DIAGNOSTIC_COMPLETE / ACCEPTED_AS_DIAGNOSIS`、Gate M4 `HOLD`、正式 P3 尚未执行；C90–C95 的章节完成
作为续跑诊断证据保留，不再以“补齐 formal lifecycle”为由重复运行。

### 1.5 2026-08-01 既有检查点报告提取

本次按已完成运行数据抽取检查点报告，不调用模型，也不重跑 C40–C95：

| Profile | 选定报告 | 来源检查点 | 用途 |
|---|---|---|---|
| VAC | `/tmp/ns-stage2m-selected-checkpoints-vac-20260801/selected_checkpoint_report_A.json` | C40/C60/C80/C95 | 既有 P2 基线与后续 canary 对照 |
| APC | `/tmp/ns-stage2m-selected-checkpoints-apc-20260801/selected_checkpoint_report_A.json` | C40 | 已有 APC 单点基线；没有伪造 APC C60/C80/C95 |

对应 `selected_checkpoint_manifest.json` 均记录 `source_identity_consistent=true`，但明确
`formal_contract_validated=false`、`gate_passed=false`，且选定子集不要求完整 scenario
lifecycle。五个既有 P2 哨兵共 53 个 Gold：45 `MISS`、3 `UNTRACEABLE`、4 `HIT`、1
`PARTIAL`；18 个证据链完整的 Gold 中仍有 13 个 `MISS`。当前修复只针对 C40 的 compound
claim 合成和 C60/C95 的 long-range candidate recall；C80 不单独开修复分支。

### 1.6 2026-08-01 聚焦修复 canary 结论

按本方案只运行了 APC C40、VAC C40、VAC C60、VAC C95 四个 checkpoint/profile canary，
没有重跑 C80、C90–C95 章节或 A/B/C。所有 canary 均完成 scenario lifecycle，
`Assembler=READY`，且 future leakage、future-isolation failure 和 Writer/Ledger budget
底线通过；它们仍是 `formal=false`、`gate=false` 的开发诊断产物。

| Canary | 结果 | 诊断解释 |
|---|---|---|
| APC C40 | `HIT 1→4`，weighted `0.0926→0.2407`，mandatory `0.0909→0.3636` | compound claim 合成有真实局部改善，但 `F-ASSEMBLY` 和 trace 缺口仍在 |
| VAC C40 | `HIT 3→2`，weighted `0.3636→0.2273`，新增 `CONTRADICTS` | 不能判定为改善，需要继续查语义结论与 stale/contrary context |
| VAC C60 | `MISS 9→MISS 8 + PARTIAL 1`，weighted `0→0.0192` | fallback 已调用且 candidate complete alternative 增加，但 selected/ledger 仍为零，`F-NEED_ROUTE_RETRIEVE` 仍主导 |
| VAC C95 | `MISS 10 + UNTRACEABLE 1` 基本不变，weighted 仍为 `0` | candidate complete alternative `1→2`，但 selected/ledger 仍为零，仍未形成可用 long-range alternative |

因此这轮只证明改动已进入真实执行链，尚未证明质量接近 Gate M4。当前继续修 C40 的
semantic contradiction/assembly 和 C60/C95 的 candidate→selected retrieval；不启动正式 P3，
不把 canary 目录拼接或提升为正式报告，也不设置人工签署门槛。

## 2. 判定依据与复核方法

### 2.1 权威输入

- 修复前 WP7 验收基线：
  `reports/stage2m/wp7_five_checkpoint_deterministic_acceptance_20260730.md`
- Gate M4 定义：
  `docs/stage2_memory_benchmark_task_closure_execution.md` 第 14.7 节
- 两个 WP7 run：
  - `reports/stage2m/writer_context_benchmark/visible_at_cutoff/qwen36_wp7_v1_20260730/`
  - `reports/stage2m/writer_context_benchmark/author_plan_conditioned/qwen36_wp7_v1_20260730/`
- 两个只读 C95 项目的 content-addressed frozen artifacts：
  - `.../visible_at_cutoff/qwen36_real_a_profile_v1_20260729/objects/`
  - `.../author_plan_conditioned/qwen36_real_a_profile_v1_20260729/objects/`
- 受限 WP8 诊断 run（仅失败/行为变化证据，不是正式准入输入）：
  - `reports/stage2m/writer_context_benchmark/visible_at_cutoff/qwen36_wp8_v1_20260731/`
  - `reports/stage2m/writer_context_benchmark/author_plan_conditioned/qwen36_wp8_v1_20260731/`
- 十点人类可读展示：
  `docs/stage2m_wp8_human_readable_outputs_20260731.md`
- 当前阶段权威状态：`docs/project_status.md`

根因基线以 `qwen36_wp7_v1_20260730` 为准；当前局部行为变化以
`qwen36_wp8_v1_20260731` 只读诊断产物为准。两者都不能替代下一次新 P3 的正式
per-Gold 和 lifecycle-closed aggregate。

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
| 冻结 Gate 公式、Gold descriptor/contract 和 hash | `src/novel_agent/services/memory_benchmark_metric_contracts.py:22-153` |
| 正式五点 Arm A 矩阵、run identity 与固定分母 | `src/novel_agent/services/memory_benchmark_reporting.py:54-302` |
| public `NeedFacet` / `NeedCompletionSpec` | `src/novel_agent/domain/memory.py:149-232`、`src/novel_agent/services/need_completion.py:50-126` |
| 实际 Need 的 deterministic max-min 调度 | `src/novel_agent/services/paired_controller.py:47-75`、`src/novel_agent/services/paired_controller.py:379-477` |
| APC canonical value 与版本化 alias registry | `src/novel_agent/services/canonical_alias_registry.py:18-107`、`src/novel_agent/services/retrieval_unit_normalizer.py` |
| `ClaimSupportGroup` / receipt-bound `ClaimVariant` | `src/novel_agent/domain/writer_context.py:176-217`、`src/novel_agent/services/claim_support.py:116-1403` |
| `ContextAssemblySpec` 与 deterministic Assembler | `src/novel_agent/domain/stage2.py:818-844`、`src/novel_agent/services/writer_context_assembler.py:61-569` |
| evaluator traceable-ID host validation | `src/novel_agent/services/memory_benchmark_evaluation.py:54-60`、`src/novel_agent/services/memory_benchmark_evaluation.py:262-301` |
| 正式 case 六个 identity/budget 字段及 runner 写入 | `src/novel_agent/domain/memory_benchmark.py:241-257`、`src/novel_agent/services/teacher_forced_benchmark_e2e.py:2668-2684` |
| StableId 长度修复与回归 | `src/novel_agent/services/controller_legal_actions.py:257-264`、`tests/regression/test_stage2_memory_benchmark_legacy_failures.py:57-67` |

第 4–5 节的代码叙述用于解释 2026-07-30 修复前行为，不得用其旧行号推断当前
实现仍然缺失。当前实现状态以本表、第 7 节和实际测试为准。

## 3. 当前已实现且必须保持的契约

| 项目 | 当前状态 | P2/P3 要求 |
|---|---|---|
| public/private 与 freeze/reveal 边界 | 已实现，聚焦测试通过 | 保持 taint/leakage = 0 |
| `gate_metric_formula.v1` 与 content-addressed Gold contract | 已实现，公式/hash/固定分母有测试 | 禁止在正式 run 中改公式或分母 |
| `stage2m_wp7_arm_a.v1` 正式矩阵 | 已实现且 fail closed | 必须由新五点 run 实际通过 |
| Need completion / 公平调度 / support closure / Assembly | 已实现，聚焦测试通过 | 必须在 P2 真实哨兵点证明质量改善 |
| APC alias 和 evaluator host validation | 已实现，回归通过 | 真冲突与非法 receipt 仍必须 fail closed |
| Writer/ledger 预算、future leakage、profile 隔离 | 旧诊断产物未见超预算/泄漏/串线 | 新 P2/P3 需要重新产生独立 attestation |
| 旧十点 Arm A | 仅诊断；旧 schema、lifecycle 未完成 | 不得作为 P3 或 Gate M4 输入 |

以上实现项应保持为回归不变量，但“代码存在”不等于“真实运行已验收”。

## 4. 2026-07-30 修复前失败事实（历史基线）

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

## 7. 当前状态与剩余动作

2026-08-02 最新接手基线：冻结 VAC C60 已使用 Need generator v21、task-weighted scheduler
v6 和本地 `http://127.0.0.1:8002/v1` 完成真实单点 Arm A。22 个 Need 是自然生成结果，未触发
当前 32 的兼容上限；不得以新的固定 Need 小整数继续收紧。11 个两-Need proposal 批和 3 个
verification 批全部成功，`0 timeout / 0 endpoint error`，所以 endpoint/120 秒取消不再是当前
主阻塞。产物目录为 `/tmp/ns-stage2m-v21-v6-qwen-c60-20260802`。

质量仍未提升：C60 `weighted=0`、`mandatory=0`、9 个 Gold 全部 MISS；candidate/rank/stage1/
ledger 的 complete alternative 数为 `3/2/0/0`，matched refs 为 `13/10/3/0`。G06/G09 在
rank 已分别完整匹配 `2/2`、`3/3`，但没有形成 Writer 可用的完整 claim。完整 grounded 统一
rerank及 query-aware compact/path-diversity 装箱均经真实 C60 证明无效并已撤销。下一步只查
Controller support-aware selection 与 compound claim synthesis，不扩建治理契约，不启动 P3。

2026-08-02 的最新证据覆盖旧 canary 结论：VAC C40 已通过 producer v23 的 currentness
语义修复把 P002-G01 从 `CONTRADICTS` 改为 `HIT`，checkpoint contradiction 回到 0；
C60 scheduler v5 使 candidate/rank 的 matched refs 和 partial alternatives 小幅增加，但没有
增加 complete alternative。producer v24 另将 proposer completion ceiling 从 1024 调到
2048，verifier 仍为 1024，以减少真实 Qwen 调用在结构化 JSON 闭合前耗尽输出上限；这不改
Writer/Ledger 预算。C95 未重跑，P3 仍未启动。

AO `session/ns-4@abf0731` 随后完成 VAC C40/C60/C95 canary40：C40 weighted 达到
`0.4545` 且 contradiction=0，C60/C95 分别仍为 `0.0192/0`，因此 Gate 继续 HOLD。
该运行使用 v19 Need generator 与旧 ContextCompiler；当前工作区已额外合并同 Need 完整反证
扫描、scope lattice 和 Make ROOT，并保留 v20 Knowledge query 与 Context fair packing。

2026-08-02 接手收口：上述现代 Stage 2M 修复及后续 v21/v6 基线已合并到 `main`，生产代码
基线为 `90f05b1`。`make quality` 在该基线上通过 1389 个测试、严格 MyPy 与 100% statement/
branch coverage。失败的 `ns-6` evidence-block binding 实验未合并；Stage 2M/AO 临时 worktree
和分支已清理；Stage 3 实现未合并并继续保留在独立 worktree。下一轮仍只执行下表 P1-2 的
G06/G09 frozen-trace 切片，不回到 P0/P1 治理重建，也不提前启动 P3。

| 顺序 | 当前状态 | 剩余动作 | 验收信号 |
|---|---|---|---|
| P0-1 receipt host validation | `IMPLEMENTED_TESTED` | 保持当前 fail-closed 测试 | 空/非法 traceable IDs 不可声明 SUPPORTS |
| P0-2 Gate 公式与聚合器 | `IMPLEMENTED_TESTED` | 在 P2/P3 验证实际产物能通过严格入口 | 公式/hash/固定分母与五点矩阵全部通过 |
| P0-3 APC alias | `IMPLEMENTED_TESTED` | APC C40 真实哨兵复验 | alias 冲突消失，真冲突仍 fail closed |
| P0-4 public completion contract | `IMPLEMENTED_TESTED` | 在真实 trace 中检查 facet closure 与 taint | runtime 不读 Gold，closure 可审计 |
| P1-1 task-weighted scheduler v6 | `IMPLEMENTED_TESTED / C60_NO_QUALITY_GAIN` | 保留首轮不饿死；不要再加固定 Need 小上限 | 所有合法 Need 先获一次调用；剩余预算连续分配 |
| P1-2 support closure | `IMPLEMENTED_TESTED / CURRENT_BLOCKER` | 逐项对照 G06/G09 rank-complete refs 与 claim variant，修原子 support group 合成 | G06/G09 的完整 evidence 形成一条 Writer 可读且 verifier 通过的 claim |
| P1-3 support-aware selection | `IMPLEMENTED_TESTED / C40_CURRENTNESS_VERIFIED / CONTEXT_FAIR_PACKING_FIXED` | 用后续受影响 canary 验证 C60/C95 正确候选是否进入 Stage1；不重跑已完成章节 | mandatory Need 只锁定首个/显式 mandatory evidence group；其余按 Need 公平原子装箱，不重复展开 L0 |
| P1-4 deterministic Assembler | `IMPLEMENTED_TESTED` | 检查 selected→assembled→ledger 损失 | spec/hash/receipt 校验与原子 packing 均成立 |
| P0-5 formal run provenance/lifecycle | `IMPLEMENTED_TESTED / P2_DIAGNOSTIC_COMPLETE` | 五个 P2 单点已在 clean execution copy + real infrastructure 完成；仍待正式 P3 五点 lifecycle 验收 | `code_source_dirty=false`；五点共享 identity；`scenario_run.completed=true` |
| P001/P003 自动化契约复核 | `AUTOMATED_CHECKS_ONLY / NON_BLOCKING` | 保留 schema、identity、evidence/cutoff 和 COMPLETE+MISS 的可读诊断；不设置人工签署门槛 | 自动化 contract/evidence 输出可复核；不以人工签名阻塞开发 |
| P2 真实哨兵 | `ACCEPTED_AS_DIAGNOSIS / C40_TARGET_FIXED / C60_TIMEOUT_FIXED` | 只修 G06/G09 support selection/synthesis；C80 暂不重跑 | C60 模型批次 0 timeout；rank-complete evidence 进入完整 Writer claim；尚未达到 P3 触发条件 |
| C45 新实体 onboarding | `IMPLEMENTED_TESTED / REAL_C45_CROSSED` | 保持精确 entity-ID 与 dangling-reference fail-closed；无需重跑已完成章节 | 真实 C45 续跑不再进入 poison loop |
| V2 normalized target collision | `IMPLEMENTED_TESTED / FAIL_CLOSED` | 保持 materialization 前冲突拒绝与证据合并上限 | 同 target 同语义只合并证据，异语义拒绝 |
| 单节点 projection replicas | `IMPLEMENTED_TESTED / CONFIGURED` | 仅影响新建 projection index 的本地资源配置 | `number_of_replicas=0`，不改变 Gate 评分 |
| C90–C95 章节续跑 | `CHAPTERS_COMPLETE / FORMAL_LIFECYCLE_NOT_CLAIMED` | 保留 `remediation37` 章节提交证据；本任务不再补跑 C40–C95 | C90–C95 章节已完成，独立续跑 lifecycle 缺口明确记录 |
| P3 双 profile 五点 WP7 | `NOT_STARTED / FINAL_GATE_ONLY` | 仅在三点 canary 接近正式门、底线无回归且最终 P3 前置条件满足后全新运行 | 达到第 9 节全部准入条件 |

开发人员不应为了“完成表格”重写 P0/P1；只有 P2 真实 trace 证明实现错误时，才回到
对应责任层做最小修复。当前串行主线（本任务不再启动新的真实全量运行）是：

```text
既有 P2 诊断归档（接受为诊断证据）
  -> C40 currentness 已验证；C60 v21/v6 与 8002 timeout 修复已验证
  -> 用 G06/G09 frozen trace 修 Controller support group / compound claim synthesis
  -> 只在机制有明确离线/单测证据后重跑 C60；C60 提升后才考虑同类 C95 canary
  -> 继续目标层修复；接近正式门时才冻结 clean code/config/contract，执行 P3 双 profile 五点 Arm A
  -> Gate M4
```

Stage 3 coverage 修复可由对应 owner 并行，但在 P3 正式启动前必须让 `make quality`
正常通过或得到明确、版本化的项目级处置。不得降低门槛、跳过 coverage 或改动
Gate 公式、Gold contract、matcher 和分母 fixture。

## 8. 测试与诊断要求

### 8.1 单元/契约测试

下列 1–18 是 P0/P1 契约清单，当前 130 项聚焦测试已通过；后续修复必须保持，
不得删除断言来让 P2 通过。19、20 和 22 已有明确回归；21、23、24 是当前
formal-run provenance/lifecycle 审计缺口，需在 P3 前补齐。

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
    复算相同结果；
19. formal reporter 只接受 Arm A 的 P001/C20、P002/C40、P003/C60、
    P004/C80、P005/C95 精确五点，缺点、多点或错配必须拒绝；
20. 五点的六个 identity/budget 字段或固定分母任一漂移时 fail closed；
21. 旧 case 缺少六个正式字段时必须在 schema/reporter 边界被拒绝，不得有自动
    backfill 或 legacy promotion 路径；
22. `need_id + step_id` 超长时 action StableId 仍稳定、唯一且不超过 128 字符；
23. formal runner 在 `code_source_dirty=true` 时必须在启动/冻结前 fail closed；
24. 五点中任一 scenario 未 completed、freeze/reveal 顺序不合法或根 aggregate 缺点时，
    formal lifecycle 验收必须拒绝。

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

1. C40：确认一个 Need 下的多证据 compound claim 能在 support producer 生成完整结论，
   且没有预算、scope、cutoff 或 evidence silent fallback；
2. C60：确认 long-range Need 的扩展 grounded fallback 能把复合历史 alternative 留在
   candidate 层；
3. C95：用同一 long-range fallback 检查后段召回和 trace 底线；
4. C80 暂不单独修复，只作为三点 canary 的旁观对照；
5. reviewer 如需并行，只检查 13 个 `COMPLETE + MISS` 样本并记录“claim 未表达/评测误判”，
   不构成开发准入或人工签署门槛；
6. 只有三点结果接近正式门时，才冻结最终 P3 的 clean source、schema、lifecycle 和严格聚合。

P2 不是 Gate M4 运行，可以只跑指定哨兵点；但必须使用新 experiment id、当前 schema
和冻结身份字段，并以 diagnostic/non-formal 形式发布。诊断 reporter 即使质量数值达标，
也不允许输出 `gate_passed=true`。

当前 `make stage2-memory-benchmark` 包装入口用于 P3 正式五点；它的末端聚合器会严格要求五点，
不应用它发布单点 P2。P2 先用 `scripts/resolve_stage2_checkpoint_commits.py` 获取目标章的
`<checkpoint-commit>`，再按下列模板对每个哨兵点单独运行：

```bash
.conda-env/bin/python scripts/run_stage2_teacher_forced_e2e.py \
  --source benchmarks/private/ztj_memory_pilot_v0.1 \
  --output-directory <p2-profile-checkpoint-output> \
  --resume-project <profile-isolated-project-directory> \
  --resume-commit <checkpoint-commit> \
  --resume-chapter <checkpoint> \
  --max-chapter <checkpoint> \
  --database-url <profile-isolated-loopback-postgresql-url> \
  --experiment-id <new-p2-profile-checkpoint-experiment-id> \
  --information-profile <visible_at_cutoff|author_plan_conditioned> \
  --arms A \
  --semantic-backend local_openai \
  --retrieval-backend real_hybrid \
  --allow-dirty-diagnostic \
  --model-base-url http://127.0.0.1:8002/v1 \
  --model qwen36-27b-nvfp4 \
  --model-max-output-tokens 8192 \
  --model-max-retries 1
```

该路径对非五点矩阵写出 `diagnostic_partial_report_A.json`，不写 formal unified PASS。
`--allow-dirty-diagnostic` 只允许当前开发树运行这种非正式 Arm A canary，并会在 manifest
中保留 `code_source_dirty=true`；最终 P3/Gate 仍必须移除该选项并使用 clean source。
P2 不调用 `scripts/aggregate_stage2_checkpoint_reports.py` 去聚合不完整矩阵。

### 8.4 P3 正式运行产物契约（仅最终 P3/Gate）

P3 必须是全新运行，不是对旧 WP7/WP8 JSON 的补字段操作。启动前必须同时满足：

- 代码源树 clean，manifest 记录 `code_source_dirty=false`；
- `make quality` 达到项目门，不使用 `--no-cov` 或降低覆盖率；
- C40/C60/C95 canary 已显示主要失败层得到改善，且四个 fail-closed 底线没有回归；
- P2 诊断结果、三点前后对照和自动化 contract/evidence 输出已归档；人工 reviewer/签署不作为
  本开发链准入条件；
- 两个 profile 使用隔离 namespace 和新 experiment id，但共享已冻结的代码、公式、
  benchmark contract、matcher 和预算身份。

每个 profile 必须产出精确五个 Arm A case、每点 freeze/reveal receipt、五点
`unified_report_A.json` 与 `unified_report.json`、`scenario_run.completed=true` 和完整
flow/progress summary。每个 case 必须原生写入六个 identity/budget 字段；两个 profile 均
`formal_contract_validated=true` 后才进入第 9 节 Gate 判定。

## 9. Gate M4 重新准入标准

### 9.1 工程与安全硬门

沿用既有 Gate M4，不降低标准：

- APC 和 VAC 均通过 `stage2m_wp7_arm_a.v1` 正式 contract validation；
- 每个 profile 只包含 Arm A 的 P001/C20、P002/C40、P003/C60、P004/C80、
  P005/C95 精确矩阵；
- 五点的 `code_version`、`run_config_hash`、`benchmark_contract_hash`、
  `matcher_version`、`writer_token_budget`、`evidence_ledger_token_budget` 完整且一致；
- 正式运行记录 `code_source_dirty=false`，不接受对旧产物事后 backfill；
- 每个 profile 的 `scenario_run.completed=true`，根 unified report 明确包含五点；
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
6. 任何预算或 matcher 规则变化都必须作为独立消融，不能混入正式 Gate run；
7. 2026-07-31 WP8 诊断产物全部排除在 P3/Gate 输入外，旧 B/C 不得与新 A 拼接；
8. P2 只使用 diagnostic reporter，不输出 formal PASS；P3 必须使用新 experiment id 和
   默认严格 formal reporter；
9. 正式运行前 `make quality` 必须通过或有项目级、版本化的明确处置，
   不得在 P3 命令中临时跳过 coverage；
10. P001/P003 的 schema、algorithm、Context、逐 Gold 和 Evidence Ledger 必须能由当前
    自动化 contract/evidence 检查复核；不沿用旧 artifact，也不把人工签署作为开发门。

满足以上条件并由正式 P3 的严格聚合器产出两个 profile 的 `gate_passed=true` 后，Gate M4
才可从 HOLD 转为 PASS。此前继续禁止启动 WP8。

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

当前正式状态：

> **P0/P1 代码与严格 Gate reporter 已实现；P2 diagnostic 已完成并接受为诊断证据，P3 未执行；
> Gate M4 HOLD；正式 WP8 未获授权。**
