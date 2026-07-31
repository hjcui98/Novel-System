# Stage 2M：面向小说写作的记忆 Benchmark 任务闭环与优化执行规划

> 文档生命周期：`ACTIVE`
>
> 文档状态：WP8 diagnostic execution / Gate M4 not passed / Gate M5 incomplete
> 编写日期：2026-07-29
> 最近更新：2026-07-31
> 代码基线：`ca9c78e` 及 2026-07-29 工作区中已完成的 C95 Retrieval Gate 相关实现
> 目标阶段：Stage 2M（Memory Benchmark Task Closure）
> 前置阶段：Stage 2W / Stage 2R 已形成 C95 连续记忆、真实检索索引和冻结后揭示 Gold 的基础设施闭环
> 后续阶段：Stage 3 Writer Core 的语义质量准入与生产 Writer Gateway 晋级

---

## 0. 执行摘要

当前系统已经完成了“真实长篇小说连续回放到 C95、构建真实检索投影、在公开上下文中运行检索、冻结结果后才向评测器揭示 Gold”的基础设施闭环。现阶段应继续保留 `deterministic` Memory Gateway；它具备进入 Writer 草稿实验的安全条件，但还不具备宣布“真实小说写作记忆 Benchmark 已通过”的质量证据。

本 Benchmark 只评测 Memory 模块：系统在只看到截止点历史正文和该 profile 合法作者规划时，能否为目标章节找回正确、相关、可追溯且不泄漏未来的信息。它不生成或评分续写正文，不测试文风，不设置预训练污染准入门禁。本文所称 `Writer-ready` 只表示 ContextPackage 可以作为写作模块的输入，不表示本阶段会调用 Writer 或用正文生成质量给 Memory 打分。

下一阶段不应继续扩大 replay、索引或 Curator 写入范围，也不应直接用现有 `Stage1ContextPackage` 启动昂贵的真实 A/B/C 模型评测。当前真正的主线是补齐以下任务闭环：

```text
公开的记忆检索任务 + 合法 profile 输入
  -> 任务/计划驱动的 Memory Need
  -> 有界检索与选择
  -> 冻结可供未来写作使用的 ContextPackage
  -> A/B/C 三个真实产物分别冻结
  -> Evaluator 揭示目标正文与 Gold
  -> 逐 Gold 判断支持、缺失、矛盾和不可追溯
  -> 评分后再继续逐章 teacher-forced 回放
  -> 两个 profile × 五个 checkpoint 的质量与成本报告
```

当前实现与该闭环之间有五个 P0 缺口：

1. 安全、通用的 benchmark task 没有成为稳定的公开运行契约；当前运行使用合成模板，而原始 `input.yaml` 又混有 evaluator-only 的精确 `target_plan` 和反向恢复提示，不能直接整体公开。
2. 默认 Need Generator 枚举整个 World State，并把每条状态都变成 mandatory need，检索规模随世界状态线性增长。
3. Context Compiler 把检索单元和展开证据同时塞入 mandatory 区，允许 mandatory 自身突破预算；C95 实跑中 4,000 token 预算生成了 36,069 / 42,309 token 的上下文。
4. 现有评分主要判断“Gold 是否碰到任意证据”，没有形成可逐项评测的 ContextPackage 结论层，也没有逐 Gold 语义支持、矛盾和可追溯性判断。
5. Arm C 只合并了 trace/selected unit id，没有重新组装 Writer 可见上下文；因此当前的 C 不是一个可被 Writer 消费和公平比较的真实实验臂。

本规划将上述问题拆分为九个按依赖顺序执行的工作包（WP0-WP8）。完成后，系统才可以进入真实模型 A/B/C，并把结果用于决定 agentic Memory Controller 是否有质量收益。

### 0.1 2026-07-31 WP8 诊断执行更新

WP0-WP7 的代码和两个 profile × 五个 checkpoint 的 deterministic Arm A 路径已实现。虽然
Gate M4 质量仍未通过，项目执行了一次受限 WP8 诊断运行，用于暴露 Agentic 路径问题；这次运行
不构成第 13 节所定义的正式晋级证据。

当前结果：

- 修复了 `controller_legal_actions.py` 中 `need_id + step_id` 组合导致的 `StableId` 长度溢出；
- `author_plan_conditioned` 与 `visible_at_cutoff` 各五个 checkpoint 均产生 Arm A；
- Arm B/C 只在部分早期 checkpoint 产生有效产物，C60 及以后为 Arm A-only；
- 两个 profile 的 contradiction 均为 0，但 coverage 和 mandatory hit 远低于 Gate M4；
- Agentic 的 silent failure 必须改成明确 typed skip/failure，不能计为完成；
- deterministic 继续是冻结默认路径，Agentic 不晋升。

当前汇总和阶段结论以 `docs/project_status.md` 为准。2026-07-30 的结果文档保留为 WP8 之前的
历史快照。

---

## 1. 本文档的证据范围与判断口径

### 1.1 已核对的设计与开发文档

本规划以以下文档的共同约束为准：

- `长篇小说Agent总体架构设计_v2.2_完整合并版.md`
- `长篇小说Agent技术实施与选型设计_v0.1.md`
- `长篇小说Agent正式开发执行规划_v0.1.md`
- `长篇小说Agent技术与执行评审建议_v0.1.md`
- `docs/stage1_acceptance.md`
- `docs/stage1_gap_audit.md`
- `docs/stage2_memory_agents_development.md`
- `docs/stage2_hybrid_retrieval_execution.md`
- `docs/stage2_memory_write_workflow_execution.md`
- `docs/stage2r_stage2w_controller_curator_quality_repair_execution.md`
- `docs/stage2_memory_gate_c95_acceptance.md`
- `docs/adr/0002-stage2-memory-controller-promotion.md`
- `docs/adr/0003-freeze-deterministic-memory-gateway.md`
- `docs/stage3_writer_core_preparation_execution.md`
- `benchmarks/private/ztj_memory_pilot_v0.1/README.md`
- `benchmarks/private/ztj_memory_pilot_v0.1/bootstrap/bootstrap_manifest.yaml`
- `benchmarks/private/ztj_memory_pilot_v0.1/bootstrap/rough_story_outline.md`
- `scripts/run_stage2_real_staged.sh`

同时核对了用户提供的开发与测试审计记录、私有 benchmark bundle、C95 真实运行报告和当前 Memory Controller / Benchmark / Context Compiler / Evaluator 实现。

### 1.2 证据优先级

当旧进度文档与当前产物不一致时，采用以下优先级：

1. 当前代码和不可变真实运行产物；
2. 2026-07-29 的 C95 Gate / ADR；
3. 当前测试；
4. 总体架构、技术设计和正式执行规划；
5. 较早的阶段进度快照。

因此，`docs/current_progress_architecture_technical_report_20260727.md` 仍可说明 C20 时的架构状态，但不能覆盖之后已经完成的 C95、真实索引和冻结协议证据。

### 1.3 当前回归基线

审计时抽跑了 Benchmark、Need、Context、Paired Pilot、Teacher-forced E2E 和 Retrieval Gate 相关测试：

- 123 个测试用例全部通过；
- 命令最终退出非零，是因为抽跑测试触发了全仓 `coverage fail-under=100`，而不是测试用例失败；
- 当前测试通过只能证明既有契约被满足，不能证明 Writer 任务闭环已经完成；
- `tests/unit/test_stage1_memory_pipeline.py` 中仍存在明确接受 `mandatory_tokens > token_budget` 的断言，这一行为与总体设计中的预算契约相反，必须在本阶段反转。

后续开发中，局部回归应使用 `--no-cov`；合并前再运行完整 `make quality`，避免把“未运行全仓导致覆盖率不足”和功能失败混为一谈。

---

## 2. 当前进度判断

### 2.1 已完成能力

| 能力 | 当前状态 | 可复用结论 |
|---|---|---|
| Genesis + C1-C95 连续回放 | 已完成 | 已形成 96 个提交（Genesis + 95 章）和 C95 最终状态 |
| 真实记忆写入链路 | 已完成基础闭环 | Controller、Curator、校验、修复、提交、派生投影均已实际运行 |
| R1 / Anchor / Grounded / Graph 投影 | 已完成 | 可作为任务化检索的底层事实与证据来源 |
| 真实 Hybrid Retrieval | 已完成 | BGE-M3、reranker 和真实物理索引可复用，不需要重新造检索后端 |
| checkpoint / snapshot 精确性 | 已完成安全门 | C20/C40/C60/C80/C95 Gate 已有通过证据 |
| 未来信息隔离 | 已完成基础门 | 公开输入先运行，结果冻结后才揭示 Gold；当前泄漏审计为 0 |
| deterministic gateway | 条件准入 | 可供 DRAFT + writer-safe 场景使用，不等于任务质量达标 |
| agentic gateway | 未准入 | 超时、成本和可比性仍未形成通过证据 |
| Benchmark ContextPackage | 未完成 | 当前输出偏内部检索/调试对象，不是稳定、可直接评测的 Memory 读侧产品契约 |
| 逐 Gold 任务质量评测 | 未完成 | 目前没有可审计的逐项支持、矛盾和证据判定 |
| 五 checkpoint 基础聚合 | 已有基础设施 | `paired_case_C20...C95` 与 `e2e_paired_report_all_checkpoints.json` 已有聚合入口 |
| 双 profile 新任务契约统一报告 | 未完成 | 尚未在新 Context/逐 Gold 契约下分别完成两套五 checkpoint 正式结果 |

### 2.2 真实运行暴露出的规模问题

C95 r35 诊断产物中的 P004/P005 两个代表案例显示（它们用于说明 Context 膨胀，不否定五 checkpoint 已完成的 Gate 证据）：

| 指标 | P004 / C80 | P005 / C95 |
|---|---:|---:|
| deterministic selected units | 206 | 251 |
| deterministic tool calls | 251 | 303 |
| mandatory context entries | 258 | 305 |
| mandatory tokens | 36,069 | 42,309 |
| Writer token budget | 4,000 | 4,000 |
| 当前 Gold recall | 0.8182 | 0.7442 |
| agentic 结果 | timeout / fallback | timeout / fallback |
| 当前 A/B 可比性 | false | false |

这组数据说明底层索引可以返回信息，但上层没有把“写作任务”转化为小而准确的需求集合，也没有把检索结果压缩成 Writer 可用的结论。继续增加 top-k、工具次数或模型超时，只会放大输入噪声和成本，不会自动补齐任务闭环。

### 2.3 当前阶段的正式结论

当前里程碑应命名为：

> **C95 Memory Infrastructure Closed / Memory Benchmark Task Open**

允许的陈述：

- 已经能安全地在 C95 真实状态和真实索引上运行记忆检索；
- 已经能证明评测前没有把 Gold 或未来文本暴露给 Controller；
- deterministic gateway 可以作为下一阶段 Writer Context 改造的底座。
- 当前 `CONDITIONAL_PASS` 和 deterministic 冻结决定保持有效；
- 已有五 checkpoint Gate 证据和聚合脚本可被下一阶段复用。

暂不允许的陈述：

- 记忆模块已经通过真实小说写作 benchmark；
- 当前 ContextPackage 可以直接用于 Writer 质量评测；
- agentic arm 优于 deterministic arm；
- Arm C 已证明增量信息对 Writer 有收益；
- 当前 recall 数字代表完整、精确的 Gold 任务完成率。
- `visible_at_cutoff` 与 `author_plan_conditioned` 已经在同一正式 run 中得到可直接比较的完整结果。

---

## 3. 目标、非目标与完成定义

### 3.1 本阶段唯一主目标

在不接触隐藏 Gold、目标正文或反向恢复精确目标计划的前提下，让 Memory 模块根据安全的目标范围任务和该 profile 合法输入主动检索相关历史记忆，并输出一个可供未来写作使用、严格受预算约束、每条结论都有证据链、能够在 Gold 揭示后逐项评测的 ContextPackage。

### 3.2 Benchmark 的任务定义

对 checkpoint `Ck` 的公开历史和当前世界状态，给定一个安全的记忆检索任务；只有 `author_plan_conditioned` profile 额外获得 Bootstrap 中模拟作者初始粗纲形成的 Plan/Intent：

> 为目标章节范围准备必要的历史 ContextPackage，恢复相关的当前状态、关系与情绪、因果历史、知识边界、未决义务，以及该 profile 合法可见的作者规划；不要续写，不泄漏 `Ck` 之后的正文或 evaluator-only 信息。

系统输出不是固定数量的“记忆条目”，而是一个满足 token、工具调用、延迟和证据约束的 Memory Context。它以后可以交给 Writer，但本 Benchmark 不运行或评分 Writer。

原 benchmark 文本中的“最多 18/20/22 条”等措辞必须从公开任务要求中移除。这些数字把资源约束错误地变成了答案形状约束。资源限制应由独立的 RunConfig 管理，不能作为模型猜测 Gold 数量的暗示。`cases/*/input.yaml` 中由完成稿反向恢复的精确 `target_plan`、详细目标提示和 preparation 材料仍属于 evaluator-only；不能因为补齐 task contract 而被复制到公开任务。

### 3.3 Definition of Done

本阶段完成必须同时满足：

1. 五个案例均从统一安全模板生成显式 `task_contract`，包含 case/target range/profile 和输出要求，不再使用不可审计的运行时临时模板，也不包含反向恢复目标提示；
2. 公开类型和 evaluator-only 类型物理分离，冻结前无法访问 Gold、Gold 权重、`target_plan`、preparation、未来正文、未来证据或 forbidden facts；
3. 默认 Need 由 Task + 当前可查询状态，以及仅在 `author_plan_conditioned` 中合法可见的 Plan 驱动，不再扫描并 mandatory 化整个 World；
4. Writer Context 的结论层严格不超过配置预算；超预算时返回类型化失败，不能伪装成 `SUFFICIENT`；
5. Writer Context 有独立的关系/情绪、知识边界、因果事件和计划义务区；
6. 检索结论与原始证据分层，原始大段 evidence 不直接重复塞入 Writer prompt；
7. Gold 的 `type`、`why_needed`、`mandatory`、`weight` 和可接受证据集合完整保留到 evaluator；
8. 每个 Gold 都输出 `HIT / PARTIAL / MISS / CONTRADICTS / UNTRACEABLE` 及原因和证据；
9. A、B、C 各自有真实冻结的 Writer Context；C 必须从合并后的单位重新执行同一 Context Assembler；
10. `visible_at_cutoff` 和 `author_plan_conditioned` 分别在同一代码、各自固定配置和评测版本下完成 P001-P005，并生成 profile-separated 统一报告；
11. 在 deterministic 门通过后才启动真实 agentic A/B/C；agentic 超时不得伪造成有效 B 输出；
12. 完整 `make quality` 和新增 contract/integration tests 通过。

### 3.4 本阶段非目标

- 不为已具备有效 profile attestation 的项目重复 replay C1-C95；缺失的 `visible_at_cutoff` 独立轨仍需在隔离 namespace 中补跑；
- 不重新设计 R1、Anchor、Grounded、Graph 底层存储；
- 不立即晋级 agentic Memory Gateway；
- 不生成续写正文，不测试文风，不把 Writer 质量与 Memory Context 质量混为一个分数；
- 不设置预训练记忆污染门禁；任何不能回指本 case 合法历史证据的内容仍记为 `UNTRACEABLE`；
- 不允许评测器反向参与 Need 生成或检索；
- 不要求 Context 输出固定条数；
- 不在本阶段让 Memory 模块直接写入 canonical state；
- 不以扩大上下文窗口替代信息选择和压缩。

---

## 4. 当前实现的根因分析

### 4.1 P0：公开任务和 evaluator 精确计划没有形成安全的契约切面

当前 `BenchmarkCaseManifest` 没有保存显式 public task，`PublicCheckpointCase` 只保留 case/project/range 等标识。`Stage2PairedPilotRunner` 最终为 `MemoryResolutionRequest.task_contract` 构造了运行时模板。另一方面，`cases/*/input.yaml` 同时含有可用于人工编译诊断的 `task/visible_outline/target_plan`，其中精确 `target_plan` 是从完成稿反向恢复的 evaluator-only 信息，正式 teacher-forced 运行不能直接把整个 input manifest 或由它编译的 Oracle PlanRoot 交给 Planner。

影响：

- 公开任务没有稳定 hash、版本和输出契约；
- 检索只能退化为全状态扫描；
- 若简单修复为“透传 input.yaml”，又会把精确目标计划和目标主题反向泄漏给被测系统；
- 报告无法展示 `Task -> Need -> Context -> Gold` 的端到端因果链。

正确修复不是原样恢复 `input.yaml.task/target_plan`，而是构造安全的 public task：只声明目标章节范围、profile、ContextPackage 输出契约和“不要续写/不要把计划当事实/必须给历史证据”等通用约束。具体未来剧情、精确目标计划和后验准备材料继续留在 evaluator。

### 4.2 P0：Need Generator 把世界规模当成任务规模

当前 `Stage1NeedGenerator` 遍历 `world.states`，把每条当前状态都生成成 mandatory need，并追加大量事件和计划需要。P004/P005 最终分别选择 206/251 个单元。

影响：

- 小说越长，Need、工具调用和上下文越大；
- 无关人物、地点和状态挤占资源；
- mandatory 失去“不可缺少”的语义；
- `max_calls = len(needs) * 2` 进一步让资源预算随错误 Need 数量膨胀；
- 无法测试“系统是否理解了当前写作任务”。

### 4.3 P0：Context Compiler 不是 Writer Assembler

当前编译器对 mandatory need 同时加入 selected unit 和 expanded evidence；同一事实会在结构化记录、检索单位和原始文本中多次出现。mandatory 不参与预算裁剪，只有 optional 会被丢弃。

影响：

- 4,000 token 预算实际产生 3.6 万/4.2 万 token；
- 当前 `SUFFICIENT` 只意味着找到了记录，不意味着 Writer 可消费；
- 原始证据淹没当前结论；
- Writer 需要自己在几十万字节的调试对象中重新做一次记忆归纳；
- 测试把过预算固化成预期行为。

### 4.4 P0：评分只测“碰到证据”，没有测“交付正确结论”

当前 `_gold_coverage` 对 historical Gold 主要判断是否有任意匹配 evidence，对 plan Gold 判断是否命中引用。部分 evidence 匹配在同一对象的 span 相交时即可计分，而当前人工 Gold 的引用粒度可能是整章 block。

影响：

- 同一章里任意不相关证据可能让 Gold 被认为 covered；
- 没有判断 Context 是否明确表达了 Gold 事实；
- 没有区分过时事实、相反事实和无证据猜测；
- `GoldItem.weight`、`type`、`why_needed` 没有完整进入计算；
- recall 无法说明冻结的 ContextPackage 实际表达了什么。

### 4.5 P0：Arm C 没有形成新的 Writer 可见上下文

当前 Arm C 把 B 相对 A 新增的 unit/trace 合并到 deterministic 结果的 trace 和 selected id 中，但没有重新编译 mandatory/current/plan/event/truth 等 Writer 可见分区，也没有重新计算真实预算。

影响：

- C 的“新增命中”可能只存在于调试 trace；
- Writer 实际看到的仍近似 A；
- C 的 recall 和成本不能代表真实混合方案；
- 无法判断 agentic 检索的新信息是否值得占用 Writer token。

### 4.6 P0：B timeout fallback 破坏实验可比性

当 B 超时，当前 paired runner 会复制 A 作为 B 的 context，同时把 B 标记为 tool failure / budget exhausted。安全上这是合理降级，但实验上不能把复制结果当作 agentic 输出。

正确处理方式：

- 持久化 B 的失败状态、已完成步骤、调用成本和可能的 partial artifact；
- `comparable=false`；
- 不计算 B 相对 A 的质量收益；
- C 若只能退化为 A，应明确标记 `C_FALLBACK_TO_A`，不能记为混合臂胜利。

### 4.7 P1：Writer 需要的语义分区不完整

当前 ContextPackage 有 mandatory、current state、plan、events、truth、raw evidence、optional 和 gaps，但实跑 P004/P005 的 event/truth 为 0，且没有独立的：

- relationship and emotion；
- knowledge / disclosure boundary；
- causal chain；
- unresolved obligation；
- superseded / stale warning。

这会直接漏掉长篇写作中最容易出错的关系变化、秘密知情边界、因果承接和伏笔兑现。

---

## 5. 目标架构

### 5.1 端到端数据流

```text
PublicBenchmarkCase
  ├─ task_contract
  ├─ checkpoint / target range
  ├─ information_profile
  ├─ author-visible PlanRoot (author_plan_conditioned only)
  └─ public current-state/retrieval handles
          |
          v
TaskFocusExtractor
  -> FocusSet(entity/action/relation/secret/location/obligation)
          |
          v
TaskPlanConditionedNeedGenerator
  -> NeedSet
  -> NeedValidator / Deduplicator / BudgetPlanner
          |
          v
Arm A deterministic retrieval
Arm B bounded agentic retrieval
          |
          v
RetrievalUnitNormalizer
  -> current/superseded resolution
  -> identity collapse
  -> evidence deduplication
          |
          v
WriterContextAssembler
  ├─ WriterContextPackage (strict prompt budget)
  └─ EvidenceLedger (separate audit budget)
          |
          v
Freeze A / Freeze B / Reassemble+Freeze C
          |
          v
GoldRevealReceipt
          |
          v
PerGoldEvaluator
  -> case report
  -> reveal receipt is sealed
  -> continue teacher-forced chapter replay
  -> per-profile five-checkpoint unified report
```

### 5.2 公开域与评测域必须物理分离

公开运行进程只允许反序列化 `PublicBenchmarkCase`。Gold 揭示后，由独立 evaluator 读取 `EvaluatorCase` 和冻结 artifact。

```text
PublicBenchmarkCase
  safe task + checkpoint + profile-legal plan (optional) + public handles

EvaluatorCase
  public_case_hash
  + gold_items
  + accepted_evidence_sets
  + weights
  + forbidden_future_facts
  + future_text_private
  + reconstructed target_plan
  + preparation references
```

约束：

- 两个类型均设置 `extra="forbid"`；
- public serializer 对 `gold*`、`future*`、`forbidden*`、`target_plan*`、`preparation*` 字段进行 taint 拒绝；
- freeze receipt 记录 public input hash、代码版本、配置版本和三个 arm artifact hash；
- evaluator 只接受 freeze receipt 已存在且 hash 验证通过的产物；
- 任何在 Gold reveal 后重新运行 retrieval 的行为都使该 case 失效。

### 5.3 Writer Context 是产品，不是 retrieval dump

Writer Prompt 内只放“写作结论层”：

1. 当前必须遵守的连续性约束；
2. 当前世界状态；
3. 关系与情绪状态；
4. 相关因果历史；
5. 角色知识与披露边界；
6. 作者可见计划和未决义务；
7. 长程伏笔/回收提醒；
8. 明确缺口与不确定性。

原始证据放入独立 `EvidenceLedger`。Writer Context 只携带简短 citation id，避免把同一段原文重复塞入 prompt。

### 5.4 两个 information profile 是两个独立实验

| Profile | 截止点前可见内容 | 明确不可见 |
|---|---|---|
| `visible_at_cutoff` | Bootstrap 中合法的开篇事实/偏好、截至 Ck 的正文与系统自己建立的当前记忆 | `rough_story_outline` 的未来 Plan/Intent、case target plan、未来正文、Gold、preparation |
| `author_plan_conditioned` | `visible_at_cutoff` 的全部内容，加 Bootstrap `rough_story_outline.md` 中合法的粗粒度 Plan/Intent | case target plan、未来正文、Gold、preparation；粗纲不得提升为 observed/current fact |

运行约束：

- 两个 profile 使用独立 `experiment-id`、project namespace、Evaluation Ledger scope 和输出目录；
- 不能在同一 project replay 到一半后切换 profile；
- 不能让 `visible_at_cutoff` 复用已经加载未来粗纲的 PlanRoot；
- 可以复用相同历史原文和模型服务，但不能复用带 profile 状态的缓存、PlanRoot、Commit 或索引 namespace；
- 两个 profile 分别形成五 checkpoint 报告；跨 profile 只做并列分析，不把十个结果混成一个分数；
- Gold 需声明 `applicable_profiles`。仅由作者未来规划产生的 obligation 不得反向惩罚 `visible_at_cutoff`；
- `author_plan_conditioned` 的 Plan 只能标为 `PLAN_OBLIGATION/INTENT`，绝不能作为 `OBSERVED_FACT` 或角色知识。

正式 teacher-forced 生命周期：

```text
Ck public state
  -> 在对应 profile 下生成 Need/Context
  -> 冻结全部被测 arm
  -> Evaluator 解封目标窗口与 Gold 并评分
  -> 关闭该 checkpoint 的 evaluation receipt
  -> 下一真实章节才按正常 teacher-forced 流程揭示给 Curator
  -> Commit / Projection
  -> 到达下一个 checkpoint 后重复
```

Evaluator 为评分读取目标窗口，不等于该窗口提前进入 Planner、Memory Controller、检索索引、模型缓存或 canonical state。

---

## 6. 新增和修改的领域契约

### 6.1 `BenchmarkTaskContract`

建议新增至 `src/novel_agent/domain/memory_benchmark.py`：

```python
class BenchmarkTaskContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    task_text: str
    task_kind: Literal["memory_context_for_target_range"]
    checkpoint_chapter: int
    target_chapter_start: int
    target_chapter_end: int
    information_profile: str
    task_template_version: str
    output_contract_version: str
```

说明：

- `task_text` 由版本化安全模板生成，只描述目标范围和 ContextPackage 输出要求；
- 不包含答案数量上限；
- 不包含原始 `input.yaml.target_plan`、后验主题提示或 preparation 摘要；
- PlanRoot 仍是 profile 合法作者规划的唯一事实源，不把 evaluator plan answer 复制进任务；
- `information_profile` 明确 author planning 和 character POV 的边界。

### 6.2 `PublicCheckpointCase`

在 `src/novel_agent/domain/stage2.py` 中增加：

```python
task_contract: BenchmarkTaskContract
plan_root_ref: PlanRootRef | None
public_input_hash: str
```

`plan_root_ref` 在 `visible_at_cutoff` 下必须为空或指向经过验证、不含未来粗纲节点的 cutoff-only Plan view；在 `author_plan_conditioned` 下只能指向 Bootstrap 粗纲形成的 author-visible PlanRoot，不能指向 Human Benchmark Compiler 从 case `target_plan` 构造的 Oracle PlanRoot。

并增加禁止字段测试，确保以下内容不能进入 public JSON：

- `gold_items`
- `gold_weight`
- `accepted_evidence_sets`
- `future_evidence_refs`
- `forbidden_future_facts`
- `target_plan`
- `preparation_refs`

### 6.3 `GoldItem`

修改 `src/novel_agent/domain/benchmark.py` 和 Stage1 schema，完整保留私有 benchmark 原始字段：

```python
class GoldItem(BaseModel):
    gold_id: str
    gold_type: GoldType
    fact: str
    why_needed: str
    mandatory: bool
    weight: float = Field(gt=0)
    applicable_profiles: tuple[BenchmarkInformationProfile, ...]
    accepted_evidence_sets: tuple[EvidenceSet, ...]
    target_components: tuple[str, ...] = ()
```

`GoldType` 至少包括：

- `CURRENT_STATE`
- `RELATIONSHIP_EMOTION`
- `CAUSAL_HISTORY`
- `KNOWLEDGE_BOUNDARY`
- `PLAN_OBLIGATION`
- `LONG_RANGE_CALLBACK`
- `OBJECT_CONTINUITY`

对于复合 Gold，`target_components` 用于判断 partial，而不是只看任意一个证据。`applicable_profiles` 用于避免用 evaluator-only 精确规划要求惩罚 `visible_at_cutoff`；所有基于已发生历史且未来写作确实需要的 observed/current Gold 通常适用于两个 profile。

### 6.4 `WriterContextItem`

```python
class WriterContextItem(BaseModel):
    context_item_id: str
    section: WriterContextSection
    claim: str
    validity: Literal["current", "historical", "planned", "uncertain"]
    mandatory: bool
    confidence: float
    need_ids: tuple[str, ...]
    retrieval_unit_ids: tuple[str, ...]
    evidence_ledger_ids: tuple[str, ...]
    supersedes_item_ids: tuple[str, ...] = ()
```

关键原则：

- `claim` 是 Writer 可直接理解的结论；
- 一个结论可以引用多条证据，但不能把完整证据正文复制到 claim；
- 相同 canonical identity 的旧状态必须通过 `supersedes` 或当前性决策折叠；
- 没有可接受证据的 claim 必须显式标为 uncertain，不能进入 mandatory fact。

### 6.5 `WriterContextPackage`

```python
class WriterContextPackage(BaseModel):
    contract_version: str
    task_contract: BenchmarkTaskContract
    basis_commit_id: str
    basis_snapshot_id: str
    arm: Literal["A", "B", "C"]

    continuity_constraints: tuple[WriterContextItem, ...]
    current_world_state: tuple[WriterContextItem, ...]
    relationship_and_emotion: tuple[WriterContextItem, ...]
    causal_history: tuple[WriterContextItem, ...]
    knowledge_and_disclosure: tuple[WriterContextItem, ...]
    plan_and_obligations: tuple[WriterContextItem, ...]
    long_range_callbacks: tuple[WriterContextItem, ...]
    gaps: tuple[ContextGap, ...]

    budget_report: WriterContextBudgetReport
    evidence_ledger_ref: ArtifactRef
    lineage: ContextLineage
```

### 6.6 预算结果必须是显式状态机

```python
class ContextAssemblyStatus(StrEnum):
    READY = "READY"
    NEEDS_REDUCTION = "NEEDS_REDUCTION"
    CONTEXT_BUDGET_INSUFFICIENT = "CONTEXT_BUDGET_INSUFFICIENT"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    POLICY_BLOCKED = "POLICY_BLOCKED"
```

只有 `READY` 可以作为有效 Writer Context 进入冻结和质量评分。其他状态仍需持久化诊断 artifact，但不能伪装成成功。

`WriterContextBudgetReport` 至少记录：

- tokenizer/model/version；
- configured writer token budget；
- actual rendered writer tokens；
- evidence ledger tokens；
- mandatory conclusion tokens；
- optional conclusion tokens；
- header/citation/gap tokens；
- deduplicated item count；
- superseded item count；
- dropped optional ids + reason；
- reduction rounds；
- final status。

### 6.7 `PerGoldComparison`

```python
class GoldMatchStatus(StrEnum):
    HIT = "HIT"
    PARTIAL = "PARTIAL"
    MISS = "MISS"
    CONTRADICTS = "CONTRADICTS"
    UNTRACEABLE = "UNTRACEABLE"

class PerGoldComparison(BaseModel):
    gold_id: str
    status: GoldMatchStatus
    weight: float
    mandatory: bool
    matched_context_item_ids: tuple[str, ...]
    matched_evidence_ledger_ids: tuple[str, ...]
    supported_components: tuple[str, ...]
    missing_components: tuple[str, ...]
    explanation: str
    verifier_receipt_ref: ArtifactRef | None
```

---

## 7. Task + Plan 驱动的 Need 生成

### 7.1 替换默认策略

新增 `TaskPlanConditionedNeedGenerator`，并把当前 `Stage1NeedGenerator` 降级为：

- Stage1 synthetic/oracle 测试兼容实现；
- 不允许被真实 Stage2 Benchmark E2E 作为默认生成器；
- runner 若检测到真实 case + legacy full-world generator，应直接返回 contract error。

`ModelMemoryNeedGenerator` 可以保留为可选实验臂，但必须接收版本化的安全 public task，且不能把整个 WorldRoot 序列化进 prompt。

### 7.2 Need 生成输入

只允许使用：

- `BenchmarkTaskContract`
- 截至 checkpoint 已发生的正文和由其建立的 canonical/derived memory；
- `author_plan_conditioned` 下 Bootstrap 粗纲形成的 author-visible `PlanRoot`；
- checkpoint/basis
- entity alias resolver
- 当前状态和关系的受控查询接口
- public retrieval schema / tool manifest

不允许使用：

- Gold fact / type / weight
- target chapter 正文
- future evidence
- evaluator explanation
- `cases/*/input.yaml.target_plan`
- `preparation/` 后验总结和人工准备稿
- `visible_at_cutoff` 下的未来粗纲节点
- 由 Gold 构造的 Oracle WorldRoot

`visible_at_cutoff` 如果没有合法未来 Plan，则 Task Focus 只能从通用 task、目标范围、当前开放状态/义务和截止点历史中形成；不得使用 evaluator 目标主题来弥补。`author_plan_conditioned` 可以用粗纲扩大 focus，但必须保留 `source=plan_intent`，后续 Context 不得把它渲染成已发生事实。

### 7.3 两阶段算法

#### 阶段一：Task Focus Extraction

从 task 和可见 plan 提取：

- 显式人物、物件、地点；
- 目标动作和事件；
- 目标关系；
- 秘密、知情者和披露动作；
- 当前目标与开放义务；
- plan 中即将回收的伏笔；
- 与目标实体相邻的一跳关系实体。

安全 public task 通常不会包含具体未来剧情，因此还必须从 cutoff frontier 提取：

- 最近已揭示章节中的活跃人物、地点、物件和冲突；
- 当前仍开放的 obligation、promise、threat、injury 和 deadline；
- 最近 Commit 改变但尚未稳定的 state/relation；
- 当前场景离开时未闭合的因果边；
- `author_plan_conditioned` 粗纲中与当前阶段相交的宽粒度 intent。

`cutoff frontier` 只能由截至 Ck 的正文、canonical state 和合法粗纲产生，不能用未来目标窗口或 case target plan 反向选主题。这样 `visible_at_cutoff` 即使没有未来粗纲，也能根据“刚刚发生什么、哪些问题仍未解决”形成 Memory Need，而不是退回全 World 扫描。

输出 `FocusSet`，每个 focus 都保留来源：

```text
focus_id
focus_type
canonical_entity_id / relation_id / plan_node_id
source = task | cutoff_frontier | plan_intent | alias_expansion | one_hop_relation
reason
```

#### 阶段二：Need Construction

针对 focus 生成有目的的 need：

| Need 类别 | 生成条件 | 目标分区 |
|---|---|---|
| current state | 任务涉及人物/物件/地点 | current_world_state |
| relation/emotion | 任务涉及交互或关系节点 | relationship_and_emotion |
| causal history | 目标动作依赖历史原因 | causal_history |
| knowledge boundary | 任务涉及秘密、误解、披露 | knowledge_and_disclosure |
| plan obligation | 可见计划存在目标/约束/伏笔 | plan_and_obligations |
| object continuity | 关键物件位置、持有人、状态相关 | continuity_constraints |
| long-range callback | plan 明示回收或 task 明示承接 | long_range_callbacks |

每个 need 必须含：

```text
need_id
purpose
expected_section
focus_ids
mandatory
priority
horizon
information_scope
query_hints
completion_criteria
```

### 7.4 mandatory 的定义

只有满足以下任一条件才能标 mandatory：

1. task 明确要求；
2. author-visible plan 明确约束下一段；
3. 缺失会直接造成连续性矛盾；
4. 缺失会导致角色知道不该知道的信息；
5. 当前状态查询表明它是目标实体的开放义务或未解决约束。

“它存在于 WorldRoot”不能成为 mandatory 理由。

### 7.5 去重与规模约束

Need Validator 必须：

- 按 canonical entity/relation/obligation identity 合并重复 need；
- 将同一实体的 state + recent cause 组织为一个 need plan，而非多次独立检索；
- 拒绝没有 purpose 或 focus 来源的 need；
- 对一跳关系扩展设置资源边界，禁止递归遍历整图；
- 在资源不足时输出 `NEED_BUDGET_EXHAUSTED` 和未展开 focus，不静默丢弃 mandatory need。

初始实验配置建议：

```yaml
need_generation_profile: task_plan_conditioned_v1
max_total_needs: 32
max_alias_expansions_per_focus: 4
max_relation_hops: 1
max_reduction_rounds: 2
```

这些是可校准的计算资源上限，不是 Context 输出条数，更不是 Gold 数量提示。

### 7.6 必须新增的反规模测试

构造两个 World：

- W1：只有任务相关的 10 个状态；
- W2：在 W1 上增加 1,000 个与 task/plan 无关的状态。

验收要求：

- 两者生成的 mandatory need 集合完全相同；
- 总 Need 数差异不超过由显式一跳关系引起的合理扩展；
- 工具调用预算相同；
- Writer Context token 不随 1,000 个无关状态线性增长。

这是本阶段最关键的性质测试之一。

---

## 8. 有界检索、选择与当前性解析

### 8.1 资源预算与 Need 数解耦

移除或停止使用：

```python
max_calls = len(needs) * 2
```

改为 RunConfig 显式声明：

```yaml
retrieval_budget:
  arm_a_max_tool_calls: 48
  arm_b_max_tool_calls: 64
  arm_b_max_agentic_steps: 8
  per_need_candidate_limit: 20
  per_need_selected_limit: 4
  wall_clock_timeout_seconds: 180
```

初始数值用于 P001/P003 校准。任何调整必须产生新 `run_config_hash`，不得在同一 profile 的五案例之间动态改变。

### 8.2 路由原则

- current state 优先 R1 / Grounded current projection；
- relation/emotion 优先 relation projection + supporting evidence；
- causal history 优先 event/graph/anchor；
- knowledge boundary 优先 information-boundary records；
- plan obligation 只查询 author-visible PlanRoot；
- long-range callback 可以使用 lexical + semantic + anchor，但仍受 cutoff 和 scope 约束；
- 不因为一个 need 未命中就自动打开所有 channel；
- agentic B 可以决定二次查询，但不能扩大信息域。

### 8.3 Retrieval Unit Normalizer

在 Context Assembler 前新增标准化步骤：

1. canonical identity collapse；
2. freshness/supersession resolution；
3. object hash + span 的 evidence 去重；
4. 同一事实的结构化记录和 evidence 绑定；
5. 过时记录转为 history 或 stale warning，不能与 current state 并列；
6. 冲突记录进入 `ContextGap/Conflict`，不能由排序分数直接覆盖。

### 8.4 选择策略

每个 need 优先选择：

1. 一个当前结论记录；
2. 一个最小充分证据集合；
3. 必要时一个因果前驱或关系补充；
4. 只有在前述内容不能支持 claim 时才扩展原文。

禁止：

- 把整章 block 当作默认 evidence；
- 同一事实重复进入 mandatory、raw evidence 和 optional；
- 仅因为 retrieval score 较高就让无 task purpose 的记录进入 Writer Context；
- 将 future evidence 用作检索候选或 claim 支持。

---

## 9. Writer Context Assembler

### 9.1 组装顺序

`WriterContextAssembler` 使用确定性步骤：

1. 依据 Task/Need 把 normalized units 转为候选 claim；
2. 合并等价 claim；
3. 应用 freshness/supersession；
4. 检查每个 claim 的 evidence；
5. 将 claim 放入目标 Writer section；
6. 先计算真实 tokenizer 下的 mandatory conclusion tokens；
7. 若超预算，执行最多两轮结构化归并/缩写；
8. mandatory 仍超预算则返回 `CONTEXT_BUDGET_INSUFFICIENT`；
9. mandatory 可容纳后，再按边际任务价值加入 optional；
10. 渲染最终 Writer Context，重新计数并冻结。

### 9.2 结论层与证据层

Writer Context：

```text
[当前必须遵守]
- 唐三十六目前……

[关系与情绪]
- A 对 B 的态度已从……转为…… [E17,E18]

[知识边界]
- 角色 X 尚不知道……；作者计划允许在本章…… [E31,P4]

[未决义务]
- 必须兑现……，但具体结果未知 [P7]

[缺口]
- 未找到物件 Y 在 C73 后的可靠持有人记录。
```

Evidence Ledger：

```text
E17:
  evidence_ref
  exact quote / structured record
  basis commit
  information scope
  need ids
  retrieval trace ids
```

Writer prompt 预算只统计实际送给 Writer 的渲染文本。Evidence Ledger 设独立审计预算，不得通过“外置”方式向 Writer 偷渡无限文本。

### 9.3 初始预算配置

```yaml
context_assembler_profile: writer_context_v1
writer_token_budget: 4000
evidence_ledger_token_budget: 12000
tokenizer: writer_model_exact
allow_mandatory_overflow: false
allow_raw_chapter_in_writer_prompt: false
max_reduction_rounds: 2
```

4,000 token 延续当前实验预算，便于前后对比；P001/P003 手工审阅后可以调整，但每个 profile 的五案例正式运行必须固定同一配置。

### 9.4 预算失败语义

以下行为必须废止：

- mandatory 超预算仍返回正常 package；
- deterministic 结果超预算仍标记 `SUFFICIENT`；
- 用 optional drop 数量掩盖 mandatory overflow；
- 让 Writer 自己在大段 raw evidence 中压缩。

正确语义：

```text
mandatory 超预算
  -> NEEDS_REDUCTION
  -> 收紧 Need / 合并 claim / 选择最小证据
  -> 重新组装
  -> 仍超预算
  -> CONTEXT_BUDGET_INSUFFICIENT
  -> case 不得进入质量通过统计
```

### 9.5 与现有 `Stage1ContextPackage` 的兼容

- 保留旧类型供 Stage1 synthetic/oracle 测试和历史 artifact 读取；
- 新 benchmark 默认使用 `WriterContextPackage v1`；
- 可以提供单向 legacy adapter，但 adapter 结果不得标记为 benchmark-quality eligible；
- 不对历史 CAS artifact 做破坏性迁移；
- 新报告同时记录 contract version，禁止混合不同版本计算总分。

---

## 10. Gold 标注与逐项评测

### 10.1 先修正 Gold 编译完整性

`HumanBenchmarkCompiler` 必须完整保留：

- `type`
- `fact`
- `why_needed`
- `mandatory`
- `weight`
- accepted evidence alternatives
- plan reference alternatives
- applicable information profiles
- forbidden future facts（case 级 evaluator-only）

原始 Gold 若只引用整章，需要在 evaluator-only annotation 中补充精确 sentence/scene/span 或结构化 record。不能继续把“与同一整章有任意 span 相交”作为充分命中条件。

### 10.2 P001/P003 先行标注

先选择：

- P001：短程、较易人工核验；
- P003：包含长程回忆、物件连续性和关系/义务，适合验证跨章任务。

对每个 Gold 人工确认：

1. fact 是否原子化；
2. 是否有多个可接受表述；
3. accepted evidence 是否精确；
4. 是否允许 plan-only 支持；
5. mandatory 和 weight 是否合理；
6. target components 是否足以判断 partial；
7. future / forbidden 边界是否明确。

P001/P003 评测器通过人工对照后，再补齐 P002/P004/P005。

### 10.3 逐 Gold 判断规则

评测采用两层判断。

第一层：确定性 provenance/evidence 检查

- 先根据 `applicable_profiles` 确定本 profile 的 Gold 分母；
- citation 必须能解析到冻结的 Evidence Ledger；
- evidence 的 basis 不得超过 cutoff；
- information scope 必须适用于 Writer；
- 精确 evidence id 命中优先；
- span 匹配必须在同一 text object/block 内，并达到配置的覆盖阈值；
- plan Gold 必须命中相同的 author-visible plan node 或其合法版本；
- full-chapter overlap 本身不能判定 HIT。

第二层：Gold reveal 后的语义支持检查

- 比较冻结 claim 是否支持 Gold fact；
- 判断是否表达相反或过时结论；
- 对复合 Gold 判断支持了哪些 component；
- 语义 verifier 只能读取冻结 Context、Evidence Ledger 和 evaluator case；
- verifier 的模型、prompt、temperature、schema 和 receipt 必须版本化。

不增加“模型是否在预训练中见过《择天记》”门禁。系统即使凭预训练记忆给出正确答案，只要不能引用当前 case 在 cutoff 前允许读取的证据，仍判为 `UNTRACEABLE`；这已经覆盖本 Benchmark 所需的可追溯性要求。

### 10.4 五类结果

| 状态 | 定义 |
|---|---|
| HIT | claim 语义支持 Gold，且至少一个合法证据/计划引用可追溯 |
| PARTIAL | 复合 Gold 只支持部分 component，或结论基本正确但缺关键限定 |
| MISS | 没有相关 claim，或检索到的内容不足以形成目标结论 |
| CONTRADICTS | Context 明确给出与 Gold 相反、过时或越权的结论 |
| UNTRACEABLE | claim 看似正确，但没有合法 evidence/plan 支持 |

`CONTRADICTS` 与 `UNTRACEABLE` 必须单独报告，不能并入普通 MISS。

### 10.5 评分

逐项基础分：

```text
HIT         = 1.0
PARTIAL     = 0.5
MISS        = 0.0
CONTRADICTS = 0.0
UNTRACEABLE = 0.0
```

加权覆盖率：

```text
weighted_coverage =
  sum(gold.weight * status_score) / sum(gold.weight)
```

同时独立报告：

- mandatory hit rate；
- mandatory miss ids；
- contradiction rate；
- untraceable rate；
- evidence groundedness；
- current-state accuracy；
- operational/plan coverage；
- long-range callback coverage；
- leakage count；
- Writer token utilization；
- selected-unit-to-context compression ratio；
- tool/model calls、latency 和成本。

准入时 mandatory 只接受 `HIT`；`PARTIAL` 仍视为 mandatory 未完全满足。

### 10.6 不自动伪造 Context Precision

未匹配 Gold 的 Context item 不一定无关，因为 Gold 不可能枚举所有写作有用信息。因此：

- 自动报告 `gold coverage`，不直接把未匹配 item 全算 false positive；
- P001/P003 增加人工 relevance audit；
- 抽样标注 `RELEVANT / REDUNDANT / IRRELEVANT / UNSAFE`；
- 待人工一致性足够后，再引入可自动化的 context precision 指标。

---

## 11. A/B/C 实验的正确实现

### 11.1 三个实验臂

| Arm | 定义 | 必须产出的 artifact |
|---|---|---|
| A | Task-conditioned deterministic Need + fixed retrieval routing | A Writer Context + Evidence Ledger + trace + budget |
| B | 同一公开输入和信息域下的 bounded agentic retrieval | B Writer Context 或明确失败 artifact |
| C | A 与 B 的合法 retrieval units 合并、去重、重新组装 | 独立 C Writer Context + Evidence Ledger + budget |

三者共享：

- task contract；
- cutoff/basis；
- information profile；
- Writer token budget；
- evaluator；
- Gold reveal 时间；
- Context Assembler 版本。

### 11.2 Arm C 必须重新组装

实现流程：

```text
units_c = normalize(dedupe(units_a + legal_delta_units_b))
context_c = WriterContextAssembler.assemble(units_c, same_task, same_budget)
freeze(context_c)
```

禁止只修改：

- retrieval trace；
- selected unit id；
- metric 中的 covered ids。

如果 C 因新增 mandatory claim 超预算，必须应用和 A/B 完全相同的 reduction policy。这样才能真实测量“agentic 新增信息是否挤掉了更重要的上下文”。

### 11.3 B 超时/失败处理

B 失败时持久化：

- failure category；
- completed agentic steps；
- tool/model receipts；
- partial selected units；
- partial context（若已生成）；
- budget/timeout；
- `quality_eligible=false`；
- `comparable=false`。

此时：

- 不把 A clone 计为 B 的质量输出；
- 可以生成 C fallback 诊断，但标为 `C_FALLBACK_TO_A`；
- 统一报告中单独计 agentic completion rate；
- 不计算 agentic quality lift。

### 11.4 成本口径

分别报告：

- A 独立运行成本；
- B 独立运行成本；
- B 相对 A 的增量成本；
- C 的本地归并/组装成本；
- 实验总成本；
- 若未来生产采用 hybrid policy，预估的 production incremental cost。

不能把 A+B+C 全部调用简单相加后称为某一个生产臂的成本。

---

## 12. Artifact 与报告布局

建议新增：

```text
reports/stage2m/writer_context_benchmark/<profile>/<run_id>/
  run_manifest.json
  run_config.json
  source_state_manifest.json
  public_contract_audit.json
  freeze_manifest.json
  progress_manifest.json
  flow_summary.json
  e2e_paired_report.json
  e2e_paired_report_C20.json
  e2e_paired_report_C40.json
  e2e_paired_report_C60.json
  e2e_paired_report_C80.json
  e2e_paired_report_C95.json
  paired_case_C20.json
  paired_case_C40.json
  paired_case_C60.json
  paired_case_C80.json
  paired_case_C95.json
  e2e_paired_report_all_checkpoints.json
  cases/
    P001_C20/
      public_input.json
      need_set.json
      arm_a/
        result.json
        writer_context.json
        evidence_ledger.json
        retrieval_trace.json
      arm_b/
        result.json
        writer_context.json
        evidence_ledger.json
        retrieval_trace.json
      arm_c/
        result.json
        writer_context.json
        evidence_ledger.json
        retrieval_trace.json
      evaluator/
        gold_reveal_receipt.json
        per_gold_comparison.json
        case_report.json
    ...
  unified_case_ledger.jsonl
  unified_report.json
  unified_report.md
  cost_report.json
  leakage_and_taint_audit.json
```

这些名称延续 `run_stage2_real_staged.sh` 和 `aggregate_stage2_checkpoint_reports.py` 的正式接口；Stage 2M 可以扩展内容，但不应另造一套替代现有 `progress_manifest/flow_summary/paired_case/all_checkpoints` 的主产物命名。大 artifact 仍写 `objects/sha256/` CAS，目录中保存 `ArtifactRef` 和 hash；模型调用、候选、验证、Commit、Freeze、评分证据均须可追溯，不得只在内存中保留 Arm C 或 per-Gold 结果。正式评分继续写入独立 PostgreSQL Evaluation Ledger。

### 12.1 Unified Report 的最小字段

每个 case/arm：

- task text；
- checkpoint、basis commit/snapshot/index refs；
- Need 数及类别；
- tool/model calls；
- Writer token / evidence token；
- assembly status；
- selected units；
- per-Gold status；
- weighted coverage；
- mandatory hit rate；
- contradictions / untraceable；
- leakage；
- failure/blocker；
- comparable；
- artifact refs。

总报告：

- 当前 profile 的五案例 macro / weighted aggregate；
- 各 GoldType 分项；
- A/B/C 完成率与可比案例数；
- 仅在 comparable cases 上计算的 quality lift；
- 成本、时延、token；
- promotion recommendation；
- 未解决 blocker。

两个 profile 的总报告分别发布，并另生成只含并列差异的 cross-profile report。该报告比较粗纲对召回、Plan Obligation、成本和噪声的影响，不把两个 profile 合并为单一总体分数。

建议 cross-profile 产物位于：

```text
reports/stage2m/writer_context_benchmark/cross_profile/<comparison_id>/
  comparison_manifest.json
  visible_at_cutoff_report_ref.json
  author_plan_conditioned_report_ref.json
  profile_delta_report.json
  profile_delta_report.md
```

### 12.2 Bundle 预检与现有运行入口

正文、Bundle 或 annotation 修改后，先重新物化并验证：

```bash
.conda-env/bin/python \
  benchmarks/private/ztj_memory_pilot_v0.1/scripts/materialize_bundle.py \
  --bundle-root benchmarks/private/ztj_memory_pilot_v0.1 \
  --source 择天记.txt

.conda-env/bin/python \
  benchmarks/private/ztj_memory_pilot_v0.1/scripts/validate_bundle.py \
  --bundle-root benchmarks/private/ztj_memory_pilot_v0.1
```

如果正文和 Bundle 材料没有变化，正式 run 前仍需执行 `validate_bundle.py`。它只验证哈希、章节边界、物理隔离、Manifest 和 EvidenceRef，不调用模型。

canonical read-side 编译和单 case Stage1 运行只用于组件诊断：

```bash
.conda-env/bin/python scripts/compile_human_benchmark.py \
  --source benchmarks/private/ztj_memory_pilot_v0.1 \
  --output /tmp/ztj.canonical.json \
  --gate-output /tmp/ztj.gate.json

.conda-env/bin/python -m scripts.run_stage1_benchmark \
  /tmp/ztj.canonical.json \
  --case-id ZTJ-P003 \
  --track oracle_verified \
  --retrieval-backend in-memory \
  --output /tmp/ZTJ-P003.result.json
```

这条路径可以读取为 oracle/人工诊断准备的 case target plan，不能作为正式 teacher-forced profile 结果。正式实验继续以 `scripts/run_stage2_teacher_forced_e2e.py` 和 `scripts/run_stage2_real_staged.sh` 为主入口，按 Bootstrap -> Genesis -> 逐章揭示 -> Curator -> Commit/Projection -> checkpoint freeze/evaluate 的顺序运行。

无真实模型的 contract smoke：

```bash
.conda-env/bin/python scripts/run_stage2_teacher_forced_e2e.py \
  --source benchmarks/private/ztj_memory_pilot_v0.1 \
  --output-directory /tmp/ztj-stage2-smoke \
  --experiment-id ztj-stage2-smoke-001 \
  --information-profile author_plan_conditioned \
  --semantic-backend scripted \
  --retrieval-backend scripted_smoke \
  --max-chapter 20
```

WP1 完成后，还必须用独立 output/experiment id 补一条 `visible_at_cutoff` smoke。任何 smoke 都不进入正式质量分数。

---

## 13. 分工作包实施计划

### WP0：冻结当前基线与失败样本

目标：把当前 P004/P005 的过大 Context、template task、B timeout 和伪 Arm C 固化为 regression fixtures。

实施：

1. 从 r35 读取 P004/P005 已冻结 artifact；
2. 生成只含必要字段的小型 scrubbed fixture，不复制大体积原文；
3. 记录当前 36,069 / 42,309 mandatory token 行为；
4. 固化 `task_contract` 为模板的失败断言；
5. 固化 C trace 有新增、Writer section 不变的失败断言；
6. 建立 `legacy_context_quality_eligible=false` 兼容标记。

涉及文件：

- 新增 `tests/fixtures/stage2_memory_benchmark_baseline.py`
- 新增 `tests/regression/test_stage2_memory_benchmark_legacy_failures.py`

完成条件：

- fixtures 不含 future/Gold 文本；
- 测试能稳定复现五个 P0，而不是只比较整个 JSON snapshot。

预计：0.5-1 人日。

### WP1：公开/私有 Benchmark 契约闭环

目标：安全 public task 成为稳定运行契约，原始精确 target plan 和 Gold 完整留在 evaluator，公开域无法反序列化私有字段。

实施：

1. 新增 `memory_benchmark.py` 领域模型；
2. 扩展 `BenchmarkCaseManifest` 和 `PublicCheckpointCase`；
3. 修改 `HumanBenchmarkCompiler`；
4. 更新 Stage1/Stage2 JSON Schemas；
5. 从五个任务文本移除固定答案条数措辞，并将公开 task 规范化为不含目标主题答案的安全模板；
6. 把 `input.yaml.target_plan`、preparation 和 case 精确未来提示标记为 evaluator-only，禁止 formal E2E 使用编译出的 Oracle PlanRoot；
7. 为 `visible_at_cutoff` 和 `author_plan_conditioned` 构造不同 PublicCheckpointCase；
8. 增加 public/private serializer、profile 和 taint tests；
9. 生成 contract version `memory_benchmark.v0.2`；
10. 为旧 bundle 提供显式迁移/重编译命令，不做静默兼容。

主要文件：

- `src/novel_agent/domain/benchmark.py`
- `src/novel_agent/domain/stage2.py`
- 新增 `src/novel_agent/domain/memory_benchmark.py`
- `src/novel_agent/services/human_benchmark_compiler.py`
- `schemas/stage1/BenchmarkCaseManifest.schema.json`
- `schemas/stage1/GoldItem.schema.json`
- 新增 `schemas/stage2/PublicCheckpointCase.schema.json`
- 新增 `schemas/stage2/WriterContextPackage.schema.json`
- `benchmarks/private/ztj_memory_pilot_v0.1/`

测试：

- 修改 `tests/unit/test_human_benchmark_compiler.py`
- 修改 `tests/contract/test_stage2_human_benchmark.py`
- 新增 `tests/contract/test_memory_benchmark_public_private_contract.py`
- 新增 `tests/unit/test_memory_benchmark_taint_boundary.py`
- 新增 `tests/contract/test_memory_benchmark_information_profiles.py`

完成条件：

- P001-P005 的 public task 均由安全模板生成且 hash 可复现；
- Gold `type/why/mandatory/weight` round-trip 一致；
- public JSON 注入 Gold、target plan、preparation 或 future 字段均失败；
- `visible_at_cutoff` payload 不含未来粗纲，`author_plan_conditioned` 只含 Bootstrap 粗纲；
- freeze 前模型调用 payload 的 taint audit 为 0。

预计：1-2 人日。

### WP2：Task/Plan-conditioned Need Generator

目标：让 Need 数量由写作任务复杂度决定，而不是由 WorldRoot 大小决定。

实施：

1. 新增 `TaskFocusExtractor`；
2. 新增 `TaskPlanConditionedNeedGenerator`；
3. 增加 alias、一跳关系、开放义务查询；
4. 增加 Need Validator/Deduplicator；
5. 把真实 benchmark runner 从 legacy generator 切走；
6. 给可选 `ModelMemoryNeedGenerator` 增加 task 和有界 public view；
7. 移除 `len(needs) * 2` 资源推导；
8. 记录 `Task -> Focus -> Need` lineage。

主要文件：

- 新增 `src/novel_agent/services/task_focus.py`
- 新增 `src/novel_agent/services/task_conditioned_need_generation.py`
- `src/novel_agent/services/model_memory.py`
- `src/novel_agent/services/stage2_paired_pilot.py`
- `src/novel_agent/runtime/memory_controller.py`
- `src/novel_agent/services/retrieval_routing.py`

测试：

- 新增 `tests/unit/test_task_focus.py`
- 新增 `tests/unit/test_task_conditioned_need_generation.py`
- 新增 `tests/property/test_need_generation_world_scale.py`
- 修改 `tests/unit/test_model_memory.py`
- 修改 `tests/unit/test_stage2_paired_pilot.py`

完成条件：

- 1,000 个无关状态不会线性增加 Need/调用；
- 每个 Need 都有 purpose、focus 和目标 Writer section；
- P001/P003 不再生成全 World mandatory needs；
- 真实 runner 检测到 legacy generator 时 fail closed。

预计：2-3 人日。

### WP3：Writer Context + Evidence Ledger + 硬预算

目标：把 retrieval dump 变成 Writer 可读产品，并恢复总体设计中“超预算返回 reduction/failure”的契约。

实施：

1. 新增 Retrieval Unit Normalizer；
2. 新增 Writer Context Assembler；
3. 加入关系/情绪、知识边界、因果、义务和伏笔分区；
4. 结论层与 Evidence Ledger 分离；
5. 使用 Writer 模型真实 tokenizer；
6. 实现 mandatory reduction 状态机；
7. deterministic 和 agentic 共用同一 assembler；
8. 删除/反转 mandatory overflow 的旧测试预期；
9. 增加 legacy adapter。

主要文件：

- 新增 `src/novel_agent/services/retrieval_unit_normalizer.py`
- 新增 `src/novel_agent/services/writer_context_assembler.py`
- `src/novel_agent/services/memory_pipeline.py`
- `src/novel_agent/runtime/memory_controller.py`
- `src/novel_agent/domain/memory.py`
- `src/novel_agent/domain/memory_benchmark.py`

测试：

- 新增 `tests/unit/test_retrieval_unit_normalizer.py`
- 新增 `tests/unit/test_writer_context_assembler.py`
- 新增 `tests/golden/test_writer_context_rendering.py`
- 修改 `tests/unit/test_stage1_memory_pipeline.py`
- 修改 `tests/unit/test_stage2_paired_controller.py`

必须覆盖：

- mandatory 刚好等于预算；
- mandatory 超预算；
- exact tokenizer 与估算不同；
- 重复 evidence；
- current/superseded 冲突；
- relation/knowledge boundary 分区；
- no raw full chapter；
- gaps 保留；
- repeated assembly byte-identical。

完成条件：

- `READY` 的 actual writer tokens 永不超过 budget；
- P001/P003 人工可在不查看 retrieval trace 的情况下直接读懂 Context；
- 每个非 uncertain claim 都能解析到 Evidence Ledger；
- 相同输入、版本和配置生成字节级一致结果。

预计：2-3 人日。

### WP4：逐 Gold Evaluator

目标：从“任意 evidence coverage”升级为结论 + 证据的逐项判断。

实施：

1. 完成 P001/P003 Gold evidence 精标；
2. 实现 provenance matcher；
3. 实现 semantic support verifier；
4. 实现五类 Gold status；
5. 实现 weighted / mandatory / type metrics；
6. 输出 verifier receipt；
7. 建立人工双审小样本；
8. 通过后补齐 P002/P004/P005。

主要文件：

- 新增 `src/novel_agent/services/memory_benchmark_evaluation.py`
- 新增 `src/novel_agent/services/gold_evidence_matching.py`
- `src/novel_agent/services/stage2_evaluation.py`
- `src/novel_agent/services/teacher_forced_benchmark_e2e.py`

测试：

- 新增 `tests/unit/test_gold_evidence_matching.py`
- 新增 `tests/unit/test_memory_benchmark_evaluation.py`
- 新增 `tests/golden/test_per_gold_evaluation.py`
- 新增 `tests/contract/test_gold_reveal_after_freeze.py`

完成条件：

- 整章任意 overlap 不再自动 HIT；
- 权重完整影响 aggregate；
- mandatory PARTIAL 不算 mandatory pass；
- 矛盾和无证据猜测可稳定区分；
- evaluator 在 freeze receipt 缺失或 hash 不一致时 fail closed。

预计：2-3 人日，另需 Gold 人工标注时间。

### WP5：真实 A/B/C 产物、冻结与统一报告

目标：保证三个 arm 都是可消费、可审计、可公平评测的真实 Writer Context。

实施：

1. 重构 paired runner 的 ArmResult；
2. A/B 分别组装和冻结；
3. C 合并 normalized units 后重新组装；
4. B timeout 持久化 failure artifact；
5. 将三个 arm 的 Evidence Ledger、budget 和 cost 落盘；
6. evaluator 只读取冻结 artifact；
7. 扩展现有五 checkpoint aggregator，分别生成两个 profile 的 unified report；
8. 扩展 `run_stage2_real_staged.sh` 接收显式 information profile，并增加 `make stage2-memory-benchmark` 包装入口。

主要文件：

- `src/novel_agent/services/stage2_paired_pilot.py`
- `src/novel_agent/services/paired_controller.py`
- `src/novel_agent/services/teacher_forced_benchmark_e2e.py`
- 新增 `src/novel_agent/services/memory_benchmark_reporting.py`
- `scripts/run_stage2_teacher_forced_e2e.py`
- `scripts/run_stage2_real_staged.sh`
- `scripts/aggregate_stage2_checkpoint_reports.py`
- `Makefile`

测试：

- 修改 `tests/unit/test_stage2_paired_pilot.py`
- 修改 `tests/unit/test_stage2_paired_controller.py`
- 修改 `tests/unit/test_teacher_forced_e2e_edges.py`
- 修改 `tests/contract/test_stage2_teacher_forced_e2e.py`
- 新增 `tests/integration/test_stage2_memory_benchmark_freeze_and_reveal.py`

完成条件：

- C 新增有效 unit 时，Writer Context 和预算报告真实变化；
- B timeout 不再生成伪 B quality result；
- 三个 arm 的 hash 在 Gold reveal 前写入 freeze manifest；
- unified report 不丢弃 Arm C result。

预计：2-3 人日。

### WP6：P001/P003 deterministic 先行验收

目标：在不花费 agentic 模型预算前，分别验证两个 profile 的契约、Need、Context 和 Evaluator 是否真的工作。

执行顺序：

1. P001/C20 `visible_at_cutoff` deterministic；
2. P001/C20 `author_plan_conditioned` deterministic；
3. 人工审阅 Task/Need/Context/Gold，并确认两个 profile 的输入差异；
4. 修复明显 contract/assembly/evaluator 问题；
5. P003/C60 两个 profile deterministic；
6. 人工审阅长程记忆、物件、关系、知识边界和粗纲增益；
7. 锁定每个 profile 的 `v1` 配置；
8. 重跑 P001/P003，确保各 profile 内结果可重复。

准入：

- 无 fixed-count 任务提示；
- no Gold/future taint；
- `visible_at_cutoff` 无粗纲 taint，`author_plan_conditioned` 无精确 target-plan taint；
- Writer Context `READY` 且不超预算；
- 逐 Gold 可解释；
- mandatory miss 可定位到 Need/Routing/Retrieval/Assembly/Evaluation 中的具体层；
- 人工审阅认为 Context 可直接交给 Writer。

预计：1-2 人日。

### WP7：五 checkpoint deterministic 正式运行

目标：在同一版本下分别覆盖两个 profile 的 P001-P005。

实现原则：

- `author_plan_conditioned` 优先复用已经存在、profile attestation 一致的 immutable commits、snapshots 和物理索引，不无意义重放；
- `visible_at_cutoff` 只能复用从未加载未来粗纲的独立项目；若不存在有效 profile-isolated 项目，必须在新 experiment/project/index namespace 中单独 teacher-force replay 到 C95；
- 不能从 `author_plan_conditioned` 项目删除 PlanRoot 后冒充 `visible_at_cutoff`；
- 每个 profile 内五个 case 使用相同代码、task contract、Need profile、token budget 和 evaluator；
- 若同 profile 历史物理索引缺失而 canonical commits 有有效 attestation，可只补建所需 checkpoint 投影；
- 每个 profile 报告都必须同时包含 C20/C40/C60/C80/C95。

建议运行入口：

```bash
make stage2-memory-benchmark \
  SOURCE=benchmarks/private/ztj_memory_pilot_v0.1 \
  PROJECT_DIRECTORY=<profile-isolated-project-directory> \
  STAGE2R_EXPERIMENT_ID=<profile-isolated-experiment-id> \
  INFORMATION_PROFILE=<visible_at_cutoff|author_plan_conditioned> \
  ARMS=A \
  CHECKPOINTS=20,40,60,80,95 \
  OUTPUT=reports/stage2m/writer_context_benchmark/<profile>/<run-id>
```

完成条件：

- 两个 profile 各五个 case 均有有效 freeze + evaluator artifact；
- 所有 `READY` Context 均在预算内；
- future leakage = 0；
- profile cross-contamination = 0；
- 无 template task；
- 无 legacy full-world Need Generator；
- per-Gold 和 aggregate 可相互追溯。

预计：0.5-1 人日加运行时间。

### WP8：真实模型 A/B/C 与晋级决策

仅在 WP7 通过后启动。

运行要求：

- A、B、C 在两个 profile 各自的同一五案例上运行，profile 之间使用独立 namespace；
- B 使用固定 agentic tool/step/time budget；
- 每个 case 允许有限重试，但重试策略和 seed 必须写入 manifest；
- 只在 `comparable=true` 的 case 上计算 B/C 相对 A 的质量提升；
- 同时报告 agentic completion rate 和成本；
- 不用 C 的结果反向调 prompt 后再继续算作同一 run。

晋级判断：

- 若 B/C 没有稳定质量收益，继续冻结 deterministic；
- 若 B 经常 timeout，即使个别 Gold recall 高也不晋级；
- 若 C 有收益但 B 不稳定，可考虑未来“deterministic first + bounded diagnostic expansion”，不能直接开放 agentic production；
- 任何晋级都需新的 ADR，不修改 ADR-0003 的历史决定。

预计：1-2 人日加真实模型运行与复核时间。

---

## 14. 测试与质量门

### 14.1 本地快速回归

开发中按工作包运行：

```bash
NOVEL_AGENT_FORBID_MODEL_CALLS=true .conda-env/bin/pytest \
  tests/unit/test_task_conditioned_need_generation.py \
  tests/unit/test_writer_context_assembler.py \
  tests/unit/test_memory_benchmark_evaluation.py \
  tests/unit/test_stage2_paired_pilot.py \
  tests/contract/test_memory_benchmark_public_private_contract.py \
  tests/contract/test_gold_reveal_after_freeze.py \
  --no-cov
```

### 14.2 合并前

```bash
make quality
```

并补跑：

```bash
NOVEL_AGENT_FORBID_MODEL_CALLS=true .conda-env/bin/pytest \
  tests/integration/test_stage2_memory_benchmark_freeze_and_reveal.py \
  --no-cov
```

### 14.3 Gate M0：Contract

- safe task generation/hash 100%；
- Gold private fields round-trip 100%；
- public taint 0；
- target-plan/preparation taint 0；
- profile payload contract 100%；
- fixed-count 文案 0；
- freeze-before-reveal 100%。

### 14.4 Gate M1：Need Relevance and Scale

- 所有 Need 有 task/plan focus；
- legacy full-world generator 在 real benchmark 中不可用；
- 增加 1,000 个无关状态不导致线性膨胀；
- 无 reason 的 mandatory need = 0；
- tool budget 与 Need 数解耦。

### 14.5 Gate M2：Writer Context

- `READY` context budget violation = 0；
- raw full-chapter in Writer prompt = 0；
- grounded claim rate = 100%（uncertain/gap 除外）；
- duplicate canonical claim rate <= 5%；
- current/superseded unresolved conflict = 0；
- P001/P003 人工可读性审阅通过。

`duplicate <= 5%` 是首轮工程阈值，完成 P001/P003 后可根据人工结果收紧；任何变化必须版本化。

### 14.6 Gate M3：Per-Gold Evaluation

- 五类 status 均有测试；
- Gold weight 丢失 = 0；
- whole-chapter arbitrary overlap HIT = 0；
- mandatory Gold 有逐项 explanation = 100%；
- evaluator hash mismatch 必须 fail closed；
- P001/P003 自动结果与人工结果达到可接受一致性后，才扩展到五案例。

### 14.7 Gate M4：Five-checkpoint Deterministic

- 两个 profile 的 P001-P005 完成率分别 = 100%；
- future leakage = 0；
- profile cross-contamination = 0；
- basis/snapshot/index attestation = 100%；
- Writer Context `READY` 或明确 typed failure = 100%；
- 不允许 silent overflow / silent fallback；
- 逐 Gold -> ContextItem -> Evidence -> Commit 可追溯率 = 100%。

质量目标沿用总体设计方向：

- current-state accuracy >= 95%；
- operational/plan coverage >= 95%；
- key historical evidence recall >= 90%；
- trace completeness = 100%。

以上质量阈值必须基于新的逐 Gold 评测重新计算，不能沿用当前粗粒度 coverage 数字。

### 14.8 Gate M5：Agentic Comparison

- B completion rate 单独报告；
- 只有 comparable cases 进入 lift；
- C 是独立 reassembled Writer Context；
- B/C future leakage = 0；
- B/C profile cross-contamination = 0；
- B/C budget violation = 0；
- 质量收益必须同时给出成本和延迟；
- 未达到稳定收益时，gateway 保持 deterministic。

本 Gate 不包含预训练污染检查；无合法 cutoff 证据的答案由 `UNTRACEABLE` 指标处理。也不包含续写文风、原著相似度或正文生成分数。

---

## 15. 诊断分类与修复路径

每个 MISS/PARTIAL 必须只归入一个主责任层，并可附次责任层：

| 分类 | 判断 | 优先修复 |
|---|---|---|
| F-TASK | task 丢失、歧义或错误规范化 | contract/compiler |
| F-NEED | 没生成相关 need | focus/need generator |
| F-ROUTE | need 正确但 channel 错 | routing policy |
| F-RETRIEVE | channel 正确但未召回 | index/query/backend |
| F-RANK | 候选存在但未选择 | fusion/reranker |
| F-FRESHNESS | 选到旧状态或冲突未解析 | normalizer/freshness |
| F-ASSEMBLY | 候选正确但未进入 Writer Context | assembler/budget |
| F-EVIDENCE | claim 缺少合法证据 | evidence binding |
| F-EVAL | Context 正确但评测误判 | Gold annotation/evaluator |
| F-SCOPE | 信息域错误或泄漏 | information boundary |
| F-BUDGET | 正确 mandatory 无法在预算内表达 | need reduction/context design |

统一报告必须按分类聚合，避免看到 recall 下降后盲目增加 top-k。

---

## 16. 推荐提交顺序

为了降低大改风险，建议按以下独立提交推进：

1. `stage2m: freeze legacy benchmark failure fixtures`
2. `stage2m: preserve public task and private gold contracts`
3. `stage2m: add task-plan conditioned memory needs`
4. `stage2m: add writer context and evidence ledger`
5. `stage2m: enforce hard writer context budgets`
6. `stage2m: add per-gold evaluator`
7. `stage2m: persist and reassemble paired arm artifacts`
8. `stage2m: add five-checkpoint benchmark runner and report`
9. `stage2m: record deterministic gate evidence`
10. `stage2m: record real-model paired evaluation`

每个提交都应：

- schema、领域类型、服务和测试同提交；
- 不混入 C95 Retrieval Gate 的未提交修改；
- 不修改历史 report；
- 不在同一提交里同时改变 benchmark task、retrieval model 和评分器，否则无法定位收益来源。

---

## 17. 配置、版本与可复现性

Run manifest 至少固定：

```yaml
benchmark_bundle_hash: ...
memory_benchmark_contract_version: memory_benchmark.v0.2
information_profile: visible_at_cutoff | author_plan_conditioned
public_task_template_version: memory_context_task.v1
public_input_hash: ...
evaluator_private_manifest_hash: ...
pretraining_contamination_gate: not_applicable_by_project_decision
need_generation_profile: task_plan_conditioned_v1
retrieval_routing_profile: ...
retrieval_backend: real_hybrid
embedding_model: BGE-M3
reranker_model: ...
writer_context_profile: writer_context_v1
writer_tokenizer: ...
writer_token_budget: 4000
evidence_ledger_token_budget: 12000
evaluator_version: per_gold_v1
semantic_verifier_model: ...
semantic_verifier_prompt_hash: ...
code_commit: ...
source_state_hash: ...
checkpoint_index_refs: ...
random_seed: ...
```

配置变化规则：

- 任一字段变化都生成新 run id；
- 两个 profile 必须使用不同 run id、experiment id 和状态 namespace；
- 不把旧 evaluator 的分数与新 evaluator 混合求平均；
- 每个 profile 的五案例正式 run 中不得按 case 单独放宽预算；
- 失败重试必须保留原 receipt；
- 报告必须标识工作区是否 dirty，并保存 source state manifest。

---

## 18. 发布、兼容与回滚

### 18.1 Feature flags

建议增加：

```text
memory_benchmark_contract_version=memory_benchmark.v0.2
memory_need_profile=task_plan_conditioned_v1
writer_context_profile=writer_context_v1
memory_evaluator_profile=per_gold_v1
```

### 18.2 默认策略

- Benchmark runner：新版本必须使用 v0.2；
- 生产/Writer Gateway：继续保持 deterministic；
- agentic：只在 benchmark/evaluation 环境启用；
- legacy Stage1：继续可读，但不能获得新 Benchmark Gate 资格；
- Writer Core：可以继续 DRAFT 隔离开发，但语义晋级不得使用 legacy oversized Context 作为质量证据。

### 18.3 回滚

若新 Context Assembler 导致回归：

- 回退 feature flag 到 legacy reader；
- 保留 v0.2 artifact，不删除；
- gateway 仍保持 deterministic + DRAFT 限制；
- 不回滚 C95 commits、索引或冻结协议；
- 通过 F-NEED/F-ASSEMBLY/F-EVAL 分类定位，而不是重新 replay 小说。

---

## 19. 风险与控制

### 风险 1：Task-conditioned Need 过度收窄，漏掉隐含长程记忆

控制：

- plan obligations 和一跳关系作为 deterministic floor；
- P003 长程案例先行；
- agentic 只作为诊断扩展，不替代 deterministic floor；
- per-Gold MISS 归因到 F-NEED 后补规则。

### 风险 2：语义压缩产生无证据的新事实

控制：

- v1 先采用结构化/抽取式 claim；
- 每个 claim 绑定 evidence；
- 无证据内容只能进入 uncertain/gap；
- semantic compressor 若未来引入，必须有 entailment verifier 和独立版本。

### 风险 3：Gold 标注本身过粗

控制：

- P001/P003 双人或两轮人工复核；
- accepted evidence 使用 alternatives；
- whole-chapter overlap 禁止直接 HIT；
- annotation 版本写入 evaluator manifest。

### 风险 4：为了过 Gold 而把规则写死到案例

控制：

- Need 规则只引用 task/plan/focus 类型，不引用 P001-P005 id；
- 增加 synthetic unseen task；
- task 文本变化和 1,000 irrelevant states property tests；
- evaluator-only types 不得被运行模块导入。

### 风险 5：A/B/C 配置漂移

控制：

- shared run config hash；
- arm 只允许策略字段不同；
- freeze manifest 在 Gold reveal 前锁定；
- 只对 comparable case 计算 lift。

### 风险 6：Evidence Ledger 外置后实际仍然向 Writer 注入过量文本

控制：

- 记录真正发送给 Writer 的 prompt hash 和 token；
- citation lookup 不能在 Writer 生成中无限自动展开；
- Writer 需要更多证据时必须走显式、受预算的新 retrieval turn。

### 风险 7：旧阶段文档和新契约并存造成误用

控制：

- 在 Stage1ContextPackage 标注 legacy；
- CLI 拒绝 real benchmark + legacy contract；
- 新报告明确 contract/evaluator version；
- 完成后补一份 ADR，记录 Writer Context 成为记忆模块正式读侧产品。

### 风险 8：补齐 task 时误把反向恢复目标计划公开

控制：

- public task 只由安全模板生成；
- `target_plan` 和 preparation 使用 evaluator-only 类型；
- formal runner 禁止载入 Human Benchmark Compiler 的 Oracle PlanRoot；
- 模型 payload 和检索 query 做字段名、来源和内容 hash taint audit；
- P003 增加“精确短剑/怪字目标提示未提前出现”的负向测试。

### 风险 9：两个 profile 共享状态导致实验串线

控制：

- 独立 experiment/project/index/Evaluation Ledger namespace；
- Bootstrap receipt 明确是否加载 `rough_story_outline`；
- checkpoint attestation 记录 profile 和 PlanRoot hash；
- cross-profile 缓存命中视为 P0；
- 聚合器拒绝合并 profile 不一致的 case artifact。

---

## 20. 人力与顺序估算

单人净开发工作量约 12-20 人日，另加 Gold 标注、真实模型运行和人工复核时间。建议按以下节奏：

| 阶段 | 工作包 | 估算 |
|---|---|---:|
| 契约与基线 | WP0-WP1 | 1.5-3 人日 |
| Need 优化 | WP2 | 2-3 人日 |
| Writer Context | WP3 | 2-3 人日 |
| Evaluator | WP4 | 2-3 人日 + 标注 |
| A/B/C 与报告 | WP5 | 2-3 人日 |
| deterministic 准入 | WP6-WP7 | 1.5-3 人日 + 运行 |
| real-model 决策 | WP8 | 1-2 人日 + 运行 |

关键路径：

```text
WP1 Contract
  -> WP2 Need
  -> WP3 Writer Context
  -> WP4 Per-Gold Evaluator
  -> WP5 Freeze/A-B-C/Report
  -> WP6 P001/P003
  -> WP7 Five Checkpoints
  -> WP8 Real A/B/C
```

Gold evidence 精标可在 WP2/WP3 开发期间并行进行，但 evaluator 最终锁定必须晚于 Contract。

---

## 21. 下一步立即执行清单

按优先级直接开始：

1. 建立 `memory_benchmark.v0.2` contract 和 public/private taint test；
2. 定义安全 public task 模板，移除固定答案条数，禁止透传精确 target plan；
3. 为两个 profile 建立独立 PublicCheckpointCase、namespace 和 taint contract；
4. 为 P001/P003 补齐 Gold weight/type/why/evidence/applicable_profiles annotations；
5. 用 r35 小型 fixture 固化当前 overflow、template task 和伪 Arm C；
6. 实现 `TaskFocusExtractor` 和反世界规模测试；
7. 实现 `TaskPlanConditionedNeedGenerator`，禁止 real runner 使用 legacy generator；
8. 实现 `WriterContextPackage + EvidenceLedger`；
9. 将 mandatory overflow 从“合法测试”改为类型化失败；
10. 实现逐 Gold evaluator；
11. 修正 A/B/C 冻结与 C 重新组装；
12. 先跑 P001/P003 × 两个 profile deterministic 并人工验收；
13. 再跑两个 profile 各五 checkpoint deterministic；
14. 最后才启动真实模型 A/B/C。

---

## 22. 最终决策

当前最重要的不是继续证明“系统能检索到很多东西”，而是证明：

> 在只看到截止点历史和该 profile 合法作者规划时，系统能在严格信息边界和预算内，主动找回少量但足够的当前事实、关系、因果、知识边界和合法计划义务，把它们组装成可供后续写作使用的 ContextPackage，并能在冻结后逐项说明哪些 Gold 被正确支持、哪些缺失、哪些矛盾、哪些没有证据。

在上述闭环完成前：

- C95 基础设施 Gate 保持有效；
- deterministic gateway 保持冻结/条件准入；
- agentic gateway 不晋级；
- Benchmark 不调用或评分续写 Writer，不设置预训练污染门禁；
- `visible_at_cutoff` 与 `author_plan_conditioned` 必须独立运行和报告；
- 当前 oversized Context 和粗粒度 recall 只作为诊断基线，不作为 Benchmark 通过证据；
- Stage 3 Writer 可以继续 DRAFT 隔离开发，但不能据此宣称记忆质量已经达标。

完成 WP0-WP7 后，项目才真正具备“进入真实小说写作 Memory Benchmark”的条件；完成 WP8 并形成可比 A/B/C 证据后，才有资格讨论 agentic 或 hybrid Memory Gateway 的下一次晋级。
