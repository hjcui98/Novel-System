# ADR-0010: Temporal 外层与 LangGraph leaf 的长期目标 Runtime

- Status: accepted
- Date: 2026-08-18
- Decision owners: project architecture and delivery governance
- Supersedes: ADR-0006 中“Stage 5 尚未预先批准 Temporal”的技术选择部分
- Preserves: ADR-0006 的 Stage 3 Writer、Stage 4 Planner、Stage 5 Runtime 产品拓扑与全部 Stage 边界
- Input: Runtime v2 迁移设计、2026-08-18 统一计划审核 `REPAIR`

## Context

当前 PostgreSQL Runtime 已经拥有 Task/Attempt/fence、RunEvent、EffectReceipt、checkpoint、恢复、
两路调度和原子 Chapter Settlement，是现实生产基线。外部 Runtime v2 设计随后明确提出长期目标：
Temporal 负责跨进程、跨天和跨 Worker 的 durable lifecycle，LangGraph 负责 Planner/Writer/Curator
等 Agent 决策循环，现有确定性 Python service 继续负责 Retrieval、Context、Validation、Commit、
Projection 和 Artifact。

统一执行计划一度把 Temporal/LangGraph 又写成 U7 可永久放弃的候选。这会让 U1～U6 的自研 Runtime
成熟化与 U7 的迁移方向摇摆，也把 Temporal 本身的适用性错误绑定到
`temporalio.contrib.langgraph` experimental plugin 的成熟度。

## Decision

长期目标架构固定为：

```text
Temporal
  -> long-lived run/chapter lifecycle, durable wait, timer, worker recovery,
     Activity retry, Signal/Update and Continue-As-New

LangGraph leaf executors
  -> Planner / Writer / Curator decision loops where graph migration proves
     control-flow, checkpoint or maintenance value

Existing deterministic Python services
  -> Retrieval / Context / Validation / Acceptance / Commit / Projection /
     Artifact / Effect reconciliation
```

PostgreSQL Runtime 是迁移完成前的唯一生产调度基线，不是与目标架构永久平级的最终选择。U7 决定
Temporal 的接入形态、LangGraph 迁移范围和 cutover 时机，不重新决定是否采用目标架构。

POC 或正式对照不通过时，合法动作是：

```text
keep PostgreSQL as the current production runtime
-> record the failed invariant or unsupported deployment condition
-> keep the migration task open
-> repair, narrow or defer cutover
```

不能把一次 POC 失败改写成“目标架构已取消”；取消或替换本决定需要新的 ADR。

## Integration forms

Temporal 必须与 experimental LangGraph plugin 解耦验证：

1. **Activity-wrapped leaf**：Temporal Workflow 调用粗粒度 Planning/Writing/Settlement/Projection
   Activities；Activity 内复用当前 Python loop 或独立 LangGraph executor。恢复粒度默认是整个 leaf
   Activity，节点内已结算调用仍依赖现有 checkpoint、ModelCallLedger 和 EffectReceipt 防重。
2. **Plugin-integrated leaf**：使用官方 Temporal–LangGraph integration，把有恢复价值的 graph node
   映射到 Workflow/Activity。恢复粒度可以到 node，但必须证明 history、payload、retry 和 effect
   identity 不产生第二真源。

Plugin 失败只淘汰或延期形态 2，不否定 Temporal 外层。若形态 1 只是包住全部 PG scheduler、没有
退休任何通用 orchestration owner，也不能视为成功 cutover。

## Delivery sequence

- U1～U3 先完成 ref、production assembly、raw/ledger/receipt 等两种 Runtime 都需要的基础能力；
- U3.5 做隔离、零生产接管的 Temporal 可行性 Spike，同时验证 Activity-wrapped 与 plugin-integrated
  两种形态；
- U4～U6 继续由当前 PG Runtime 取得真实 leaf、五章、连续 benchmark、20/50 章和故障证据；
- U7 用同一冻结负载做正式对照，冻结 cutover Gate，选择接入形态、迁移范围和时间；
- cutover 采用 shadow/differential → isolated canary → 20 章 → 50 章，单个 run 始终只有一个权威
  orchestrator。

## Ownership invariants

- Canon、五 Root、CommitService、RunEvent、EffectReceipt、Artifact 和 Evidence Ledger 保持 NS owner；
- Temporal History 只负责 Workflow replay，不替代业务审计或小说事实；
- LangGraph state/checkpoint 只保存小型状态和 ArtifactRef，不成为第二 Canon；
- model/database/file/retrieval/commit 等 I/O 在 Temporal Activity 或现有 service 中执行；
- 同一 retry、wait、approval、cancel 或 effect 只能有一个 owner；
- 私有正文、模型 raw response、benchmark Gold/future text 不进入 Workflow history；
- 同一正式 run 不允许 PG scheduler 与 Temporal 双调度、双写或竞争副作用。

## Acceptance evidence

U3.5 至少证明 payload 可序列化、Workflow/Activity 边界、worker-kill recovery、history 私有数据边界、
RunEvent/Temporal History 分责和窄 adapter 隔离。U7 还必须证明：

- replay、duplicate effect、stale fence、half settlement、private-history leakage 均通过 hard Gate；
- old-history 能在新 Worker/build 下 replay，一次 in-flight upgrade 能继续；
- approve/reject/pause/resume/cancel 的重复、迟到、basis/fence 变化有幂等结果；
- Continue-As-New 只发生在 accepted Commit、Projection/Freshness 完成且无 pending acceptance/effect/
  repair/command 的 safe point；
- Temporal 确实能退休至少一组现有通用 scheduler/recovery 责任，而不是只增加一层包装。

## Consequences

- PG Runtime 的 U4～U6 成熟化仍然必要，因为它提供生产基线、迁移 workload 和 cutover fallback；
- 不建立全局 `RuntimeBackend`、长期 feature flag 或双写平台；只允许当前 Python、LangGraph
  differential 和 Temporal Activity 三个现行 caller 所需的窄 leaf/orchestrator seam；
- U7 的合法结果不再包含“永久关闭 Temporal 迁移并以 PG 为最终目标”；可以延期 cutover，但必须
  保留失败证据、未闭合条件和下一次准入点。
