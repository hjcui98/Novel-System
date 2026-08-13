# Long-running Agent Runtime 上游源码参考研究

> 状态：`TECHNICAL_REFERENCE / NON_AUTHORITATIVE`
> 日期：2026-08-10
> 适用范围：NS Stage 3 之后的 Writer Runtime、TaskGraph、恢复、上下文压缩与长期自治设计
> 约束：本文不改变当前 Stage gate，不授权实现，不替代总体架构、ADR、执行计划或
> `docs/project_status.md`。所有判断固定到下列上游 commit，后续上游变化需另行复核。
>
> 阅读导航：第 1–15 节是架构裁决与采用边界；第 16–26 节是函数调用链、
> transaction/write-set、迟到回调、crash point、上游测试和 NS 逐文件落点的实现级审计。
>
> 2026-08-10 正式吸收映射：Context View/compaction 是 Stage 3 Writer 与 Stage 4 Planner 都立即需要的
> 共享窗口能力，不再单列旧 Stage 4；Task/Attempt、lease、Supervisor、长期恢复和受控演化统一归入
> Stage 5。上游源码证据和机制裁决不变，阶段映射以 ADR-0006/0007 为准。

## 1. 结论先行

这轮源码研究支持一个很明确的判断：NS 不需要迁移任何一个上游项目的完整 Runtime，
也不应该再增加一套与现有 `RunEventLog` 平行的会话存储、StepStore 或工作流历史。
最有价值的路线是按责任边界组合六类已经被上游验证的机制：

1. 从 **Hermes Agent** 迁移 durable task 的业务语义：任务、尝试、依赖、claim、lease、
   heartbeat、reclaim、block/unblock、定时任务 pre-check；不要迁移其巨型 SQLite Runtime。
2. 从 **OpenClaw** 参考单写者 lane、输入意图队列、生命周期 fencing、compaction hook 和
   supervisor 控制面；不要迁移 Gateway、渠道队列或完整 TaskFlow 存储。
3. 从 **OpenHands Software Agent SDK** 迁移不可变事件、可重建 View、Condensation 事件和
   工具调用批次原子性；不要替换 NS 已经更强的 PostgreSQL `RunEventLog`。
4. 从 **PydanticAI durable execution** 参考未来 Temporal Activity 边界、稳定注册名、
   可序列化依赖、异常分类和取消传播；只把它视为叶子 Agent Loop 候选。
5. 从 **PydanticAI Harness** 参考 settled step、effect started/completed/failed、
   complete/interrupted snapshot、确定性 receipt 和分层 compaction；不要把 alpha 状态的
   StepPersistence 或 Planning 当作 NS 的 TaskGraph。
6. 从 **OpenCode** 参考 model-aware context reserve、durable compaction checkpoint 和“完成压缩后重建
   同一逻辑 provider turn”；不要迁移其 IDE/session 产品或把 agent step limit 当成小说 run 终态。

综合优先级不是“选择一个框架”，而是：

| 优先级 | 源码 | 对 NS 的直接收益 | 建议用法 |
|---|---|---|---|
| P0 | Hermes Agent | durable task 语义最完整 | 迁移数据语义与事务不变量 |
| P0 | OpenHands SDK | 事件投影和压缩结构最干净 | 迁移事件/View 不变量 |
| P0 | OpenClaw | 单写者、并发 lane、steer/interrupt 最成熟 | 参考控制面与排队语义 |
| P1 | PydanticAI | 叶子 Agent I/O durable 化成熟 | Temporal 触发后条件式迁移 |
| P1 | PydanticAI Harness | step/effect/compaction 形状清晰 | 仅作实现参考，暂不依赖 |
| P0 | OpenCode | provider 前置压缩与同 turn 续接边界清晰 | 参考 context reserve/checkpoint/单次 overflow recovery |

如果后续只保留三套源码作为日常架构对照，优先顺序是 **Hermes、OpenHands SDK、
OpenClaw**；PydanticAI 两仓库用于 Temporal 和叶子运行循环专项研究。

## 2. 研究范围与可复现基线

本报告直接审查本地浅克隆源码。`depth=1` 只丢失旧提交历史，不丢失当前 commit 的目录、
生产代码、测试和文档；因此足以研究当前机制。克隆使用 partial/sparse 方式，没有下载
与架构研究无关的大型媒体或数据文件。

| 项目 | 固定 commit | 本地源码 |
|---|---|---|
| Hermes Agent | [`326bdfb`](https://github.com/NousResearch/hermes-agent/tree/326bdfb7a27e292a25aa1a8a073e6fac43460a98) | `/home/cuihengjia/agent-source-research/hermes-agent` |
| OpenClaw | [`8fdf757`](https://github.com/openclaw/openclaw/tree/8fdf7570a17ffbbafe825bd379bab858f263b8ca) | `/home/cuihengjia/agent-source-research/openclaw` |
| OpenHands SDK | [`be6cd3b`](https://github.com/OpenHands/software-agent-sdk/tree/be6cd3b80b706bb14c91e604581a8de75cad61cc) | `/home/cuihengjia/agent-source-research/software-agent-sdk` |
| PydanticAI | [`fc6a3ac`](https://github.com/pydantic/pydantic-ai/tree/fc6a3ac506513150e2016ee5ba9785d792795150) | `/home/cuihengjia/agent-source-research/pydantic-ai` |
| PydanticAI Harness | [`5e18085`](https://github.com/pydantic/pydantic-ai-harness/tree/5e180850511dec469cc50aa9853675a8031d1f19) | `/home/cuihengjia/agent-source-research/pydantic-ai-harness` |
| OpenCode | [`d92d1e6`](https://github.com/anomalyco/opencode/tree/d92d1e654bd1aa8ccb972b3059825314c1633eb8) | `/home/cuihengjia/agent-source-research/opencode` |

研究方法不是按 README 比功能，而是沿以下问题从生产代码追到测试：

- 谁拥有任务状态，谁拥有执行尝试？
- claim、heartbeat、过期、回收和副作用不确定性如何闭环？
- 会话单写者和全局资源并发是否被错误地混成一把锁？
- 压缩是删除历史、修改历史，还是对不可变历史建立投影？
- pause、resume、fork、interrupt 的精确安全边界在哪里？
- 工作流框架历史、项目事实、Canon 和运行事件是否被混为一个事实源？
- 哪些机制已经有失败路径测试，而不只是类型或接口？

证据分三级：生产函数和 SQL 是机制证据，上游测试是边界规格证据，NS 现有类/
仓储是迁移落点证据。本轮对上游是固定 commit 的静态源码与测试审读；并未在本机搭建五个
项目的完整依赖并重跑其测试，因此引用测试表示“上游用它锁定了该语义”，不表示
“本轮已重复证明该 commit 的完整 suite 通过”。

## 3. NS 当前事实与真正缺口

### 3.1 已经存在的正确基础

NS 当前并非没有 Runtime。现有实现已经确立了几项必须保留的责任：

- [`src/novel_agent/domain/runtime.py#L21-L145`](../src/novel_agent/domain/runtime.py#L21-L145)
  已定义 `RunEvent`、`RunCheckpoint`、`EffectReceipt`，事件带
  `run_id/task_id/sequence_no/idempotency_identity/payload_schema_version`；checkpoint 绑定
  event position 与 completed effects，effect 已能表达 `UNCERTAIN`。
- [`src/novel_agent/services/event_log.py#L26-L159`](../src/novel_agent/services/event_log.py#L26-L159)
  以 PostgreSQL transaction 和 advisory lock 保证
  每个 run 的单调序列、幂等键冲突检测、stream row 锁和 checkpoint high-watermark。
- [`src/novel_agent/services/model_request_admission.py#L117-L155`](../src/novel_agent/services/model_request_admission.py#L117-L155)
  已负责 endpoint 级 request/KV 容量，
  它是模型资源 admission，不是 project/session 单写者，也不是 durable task queue。
- 五个 Canon root 仍是小说事实的唯一权威；`RunEventLog` 是运行事实源，不是第六个 Canon。
- Artifact Store 应保存大对象，event/checkpoint 只保存稳定引用、hash、版本和恢复所需边界。

因此，上游机制必须嵌入这些 owner，而不是覆盖这些 owner。

### 3.2 尚未冻结的缺口

当前真正缺少的是 Stage 5 长期运行工作包才会需要的任务投影和 durable 调度语义：

- `TaskRecord / TaskAttempt / TaskDependency` 的责任分离；
- dependency-ready、timer-wait、human-block、policy-block 的不同含义；
- claim/lease/heartbeat/reclaim 的事务和 fencing；
- 外部副作用在 crash 后为 `unknown` 时的 reconciliation；
- project/session mutation lane 与 endpoint-global admission 的正交关系；
- context compaction 成为可审计事件，而不是修改或删除运行历史；
- resume 时对 pinned Commit/Snapshot/Profile、权限和 scoped readiness 的重新验证；
- supervisor、scheduled task、cross-day/cross-machine 的启用阈值。

### 3.3 当前 Stage 的硬边界

[`docs/stage3_writer_core_overall_design.md#L68-L103`](stage3_writer_core_overall_design.md#L68-L103)
明确：Stage 3 Writer Core 只需要固定拓扑的 `DRAFT / CONTINUE / MAJOR_REWRITE`、候选态
`DraftArtifact` lineage、`WriterContextPackage`、Writer→Editor→repair→Curator 对账；
它明确不需要完整 chapter/volume TaskGraph、长期恢复、跨机调度或通用 hooks。

所以本报告的建议分成两类：

- **现在应兼容的 invariant**：事件身份、artifact lineage、settled step、安全 effect 边界；
- **未来才应实现的 mechanism**：durable task 表、lease、scheduler、supervisor、Temporal。

不能为了未来便利提前在 Stage 3 建第二套 Runtime。

## 4. 跨项目迁移决策矩阵

| 机制 | 最强参考 | 对 NS 的动作 | 放置位置 | 不应复制的部分 |
|---|---|---|---|---|
| Task/Attempt 分离 | Hermes | 迁移语义 | domain + PG projection | CLI/workspace/PID 耦合 |
| dependency readiness | Hermes | 迁移事务不变量 | Task projection service | 把所有等待都叫 blocked |
| lease/reclaim | Hermes | 迁移并加强 effect reconciliation | TaskAttempt owner | 仅凭超时重复执行 |
| 定时 pre-check | Hermes | 迁移纯函数/脚本 gate | scheduler adapter | 先启动 LLM 再判断 |
| session 单写者 | OpenClaw | 参考 keyed lane + generation fence | runtime admission | 与模型全局容量合并 |
| steer/followup/collect/interrupt | OpenClaw | 迁移输入意图 taxonomy | session control plane | 映射成 Task status |
| immutable event/view | OpenHands | 迁移投影模式 | RunEventLog projection | 文件 EventStore |
| compaction event | OpenHands + OpenClaw | 迁移结构和生命周期 | context compiler | 删除原始 RunEvent |
| tool batch atomicity | OpenHands + Harness | 迁移强 invariant | context view tests | 任意 message slice |
| step persistence | Harness | 参考 settled/effect 边界 | existing event/checkpoint | 新 StepStore |
| Temporal activities | PydanticAI | 触发条件满足后条件式迁移 | leaf Agent adapter | 用 workflow history 代替项目事件 |
| Planning/Dynamic Workflow | Harness | 拒绝作为 TaskGraph | 无 | 临时 UI plan/sandbox script |

## 5. Hermes Agent：迁移任务语义，不迁移整套 Runtime

### 5.1 为什么直接收益最高

Hermes 把长期任务运行中最容易被低估的失败路径集中到了一个可运行实现里：父任务在
claim 前再次变化、worker 活着但 lease 暂时过期、run 泄漏、相同阻塞原因反复出现、
定时任务重复 fire、压缩进程崩溃、SQLite WAL 恢复等。其结构过于集中，但业务语义很有
价值。

### 5.2 Task 与 TaskRun 的分离

[`hermes_cli/kanban_db.py#L905-L995`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L905-L995)
中的 `Task` 同时保存 status、priority、claim lease、idempotency、连续失败、当前 run、
技能、模型、最大重试、session、block kind 和 recurrence。数据库 schema 在
[`#L1184-L1330`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L1184-L1330)
进一步分出：

- `tasks`：用户/调度层看到的持久任务；
- `task_links`：依赖关系；
- `task_events`：状态变化审计；
- `task_runs`：每次 attempt 的 claim、lease、heartbeat、outcome、summary、error。

对 NS 最关键的不是字段照搬，而是 **Task 是意图和当前投影，TaskAttempt 是一次具有
独立生命期的执行**。一次失败或 reclaim 不应该抹掉任务身份，也不能复用旧 attempt 的
effect 状态。

NS 后续宜把 `task_id` 与 `attempt_id` 都带入 `RunEvent`；TaskRecord 只保存 current
attempt 指针和聚合状态，attempt 保存 lease owner/fence、started/heartbeat/ended、
outcome 和 effect reconciliation 结果。

### 5.3 readiness 必须在 claim 事务里重算

[`recompute_ready`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L4135-L4224)
把父任务终态作为 ready 的派生条件，并保留 operator block 和 failure-limit gate。
更重要的是
[`claim_task`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L4226-L4345)
不会相信早先算出的 `ready`：它在 transaction 内再次检查父任务，错误 ready 会降级，
泄漏 run 会先关闭，然后以 CAS 方式从 ready/no-lock 转为 running，同时新建 task run
并追加 claimed event。

应迁移的 invariant：

```text
claim = lock candidate
      + revalidate dependencies and pinned basis
      + compare expected revision/status
      + allocate fresh attempt/fence
      + append task.claimed event
      + commit atomically
```

仅用后台扫描把 `pending` 更新成 `ready` 不够；它只能做索引优化，不能成为 claim 的正确
性依据。对 NS，还要把 Canon commit、snapshot/profile 和 permission scope 加入 revalidate。

### 5.4 lease 过期只是怀疑，不是重复执行许可

[`heartbeat_claim`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L4423-L4451)
更新 claim 活性；
[`release_stale_claims`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L4454-L4600)
没有看到过期就盲目重排：若 worker 仍然活着会延长 lease；heartbeat 是第二重证据；
若进程无法确认终止则拒绝释放；最终更新仍用 CAS。

NS 需要保留这个原则，并比 Hermes 多一步：

1. lease 超时，任务进入 `recovery_pending` 或等价内部态；
2. 检查 owner/fence 和最新 heartbeat；
3. 对所有 `effect.started` 且无终态 receipt 的 effect 做查询或对账；
4. 只有确认旧 owner 不可继续、外部 effect 可安全重试后，才创建新 attempt；
5. 新 attempt 使用新 fence，旧 attempt 的迟到写入必须被拒绝。

lease 保证的是所有权时效，不提供 exactly-once。副作用安全来自 stable effect identity、
idempotency key、receipt 和 recovery reconciliation。

### 5.5 Block 不是“现在不能运行”的统称

[`block_task`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L5618-L5827)
把 dependency wait 送回 `todo`，只有 needs-input、capability、transient 等才进入 blocked；
相同 block cause 连续发生会升级 triage。
[`unblock_task`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L5901-L5964)
关闭陈旧 run，重新检查父任务，再落到 todo 或 ready，而不是无条件执行。

这个区分值得迁移，但 NS 命名可更明确：

- `pending`：依赖尚未满足；
- `waiting`：timer、外部回调或可预期条件；
- `blocked`：需要人、权限、策略决定或缺失 capability；
- `ready`：当前具备 claim 资格；
- `running`：有有效 attempt owner；
- `succeeded/failed/cancelled/superseded`：终态。

这里是未来设计候选，不在本文冻结 exact enum。应先用 Stage 5 固定拓扑用例和 failure
benchmark 验证状态是否最小充分。

### 5.6 Cron 的关键不是表达式，而是 fire 语义

Hermes 的 job claim、heartbeat、next-run 推进和 fire claim 分别在
[`cron/jobs.py#L2273-L2635`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/cron/jobs.py#L2273-L2635)。
Scheduler 的两个机制尤其值得保留：

- [`cron/scheduler.py#L3188-L3293`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/cron/scheduler.py#L3188-L3293)
  允许 `no_agent` 纯脚本 pre-check，false/empty 直接静默跳过；
- [`#L4826-L4994`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/cron/scheduler.py#L4826-L4994)
  使用跨进程 tick lock、pause gate、in-flight 去重，并在 dispatch 前推进 recurring schedule，
  选择“错过一次优于恢复后突发重复”。

NS 的 heartbeat、continuity scan、定期评估应先跑确定性 pre-check，例如“是否存在到期
候选”“工作区 revision 是否变化”“是否已有等价 task”。只有 gate 通过才占用模型。
是否采用 missed-run、catch-up-one 或 catch-up-all 必须由 job policy 显式决定。

### 5.7 Compression 与 recovery 中值得保留的点

[`agent/context_compressor.py`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/agent/context_compressor.py)
实现了旧 tool result pruning、受保护 head、token tail、turn/tool pairing、迭代摘要、
provenance 和 anti-thrashing。
[`agent/conversation_compression.py`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/agent/conversation_compression.py)
增加压缩 lease/fence、snapshot/rollback、child session publish 和压缩边界通知。

这些实现说明压缩本身也是需要防并发、回滚和审计的写操作。但 NS 不应迁移其 session
数据库。适合迁移的是：安全 cut、tool pairing、provenance、anti-thrashing、publish fence。

[`hermes_cli/session_recovery.py`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/session_recovery.py)
先复制 DB/WAL/SHM/journal 到新目标，再 salvage canonical tables、重建 derived state，
不在原损坏源上修复。它强化了 NS 现有原则：恢复只能从权威记录重建派生投影，不能把
损坏 projection 反写为权威。

### 5.8 Hermes 最终裁决

**迁移**：Task/Attempt/Dependency 语义、transactional claim、lease/fence、block cause、
相同失败熔断、纯 pre-check、schedule fire policy。

**参考**：compression lease、recovery salvage。

**拒绝**：巨型 `kanban_db.py`、SQLite 作为 NS 运行事实源、PID/workspace/CLI 逻辑、
整套 session runtime。

## 6. OpenClaw：参考 Session、Queue、Compaction 与 Supervisor

### 6.1 两层并发控制必须正交

[`src/acp/control-plane/session-actor-queue.ts#L1-L33`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/src/acp/control-plane/session-actor-queue.ts#L1-L33)
为每个 session key 建串行 actor queue，并对 enqueue/dequeue 对称计数。
[`lane-controller.ts#L29-L253`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/src/agents/embedded-agent-runner/run/lane-controller.ts#L29-L253)
先取得 session lane，再等待 global lane；等待不是错误。等待后它重新检查 lifecycle
generation、claim session writer、做 placement admission，避免已经失效的 run 在排队后复活。

对 NS 的直接映射：

```text
project/session mutation lane  ── 保证同一 Canon/working-state 只有一个有效写者
model endpoint admission       ── 限制 request 与 KV/cache 等全局模型资源
task claim/lease               ── 确认哪一个 attempt 拥有某个 durable task
```

三者不能合并。现有 `ModelRequestAdmission` 继续只拥有 endpoint 容量；未来增加的 project
mutation lane 也不拥有 durable task 状态。否则一个长模型请求会不必要地冻结整个项目，
或者全局容量释放会被误认为任务所有权释放。

[`src/gateway/server-lanes.ts#L18-L130`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/src/gateway/server-lanes.ts#L18-L130)
还将 main/subagent/cron/nested 配额分开，并用 group budget 避免 hook 与 cron 的容量简单
相加。这适合 Stage 5 supervisor 的资源模型，但 Stage 3/4 不需要提前实现。

### 6.2 steer/followup/collect/interrupt 是输入意图，不是任务状态

OpenClaw 的 queue 文档明确区分
[`steer/followup/collect/interrupt`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/docs/concepts/queue.md)：

- `steer`：尽快注入当前活动 run；
- `followup`：当前 run 完成后作为新 turn；
- `collect`：合并等待输入，降低 run 数；
- `interrupt`：取消当前 run，再处理新输入。

[`agent-steering-queue.ts#L1-L285`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/src/agents/agent-steering-queue.ts#L1-L285)
为 subagent completion 使用五分钟 stale lease、确定性排序、总 prompt/单项预算、
lease/ack/release，并明确把 runtime data 当数据而不是指令。

NS 后续应将这些作为 `InputIntent` 或 `RunControlCommand`，不要塞进 Task status：
同一个 `running` task 可以收到 steer，也可以收到 cancel；task 状态描述执行事实，input
intent 描述控制者希望 runtime 如何接纳新消息。

[`chat-queued-turns.ts`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/src/gateway/chat-queued-turns.ts)
保留 queued turn 的 abort identity，并用 identity guard 防止旧 callback 删除新的同 key
entry。这是通用的异步 fencing 模式：所有完成回调都必须核对自己仍是当前 owner。

### 6.3 Append-only session tree 与 compaction boundary

[`packages/agent-core/src/harness/session/session.ts#L15-L138`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/packages/agent-core/src/harness/session/session.ts#L15-L138)
把 session 当 append-only tree 投影；读取时从最新 compaction/reset boundary 起，用摘要、
保留 tail 和后续事件重建上下文，并剥离不能跨 prefix 重放的 checkpoint。

[`compaction.ts#L674-L805`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/packages/agent-core/src/harness/compaction/compaction.ts#L674-L805)
寻找不会从 `toolResult` 开始的安全 cut，准备被摘要范围和 kept tail；随后持久化
summary、`firstKeptEntryId` 与 `tokensBefore`。详尽生命周期在
[`session-management-compaction.md#L175-L297`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/docs/reference/session-management-compaction.md#L175-L297)：
包含 persistent compaction event、overflow recovery、post-turn threshold 和每周期一次的
pre-compaction memory flush。

[`compaction-hooks.ts`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/src/agents/embedded-agent-runner/compaction-hooks.ts)
把 before/after hook 设计为 best-effort，并记录失败。NS 应更细分：

- 防止丢失恢复必要状态的 checkpoint/state flush：必须 fail closed；
- 写可选 memory note 或统计：可以 best-effort；
- summary 失败：保留原 Context View，不能破坏 RunEventLog；
- compaction publish：必须带 basis revision/fence，避免旧摘要覆盖新上下文。

### 6.4 TaskFlow 的参考边界

[`docs/automation/taskflow.md#L10-L64`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/docs/automation/taskflow.md#L10-L64)
描述了 owner/controller、state JSON、wait JSON、task link 和 revision/CAS；取消是 sticky，
取消后不再创建 child，父任务只在 child settle 后 finalise。

这些是好 invariant，但其文档也强调 flow 协调 task 而不替代 task。NS 可参考 revision/CAS、
sticky cancellation、parent/child settle；不应复制 OpenClaw 的渠道、Gateway 和 TaskFlow
存储，因为 NS 已有 Canon、RunEventLog 和未来 Task projection 的明确 owner。

### 6.5 OpenClaw 最终裁决

**迁移/实现同等语义**：keyed single-writer、generation fence、回调 identity guard、输入意图
taxonomy、sticky cancel。

**参考**：分组 lane budget、pre/post compact lifecycle、TaskFlow revision/CAS、heartbeat
supervisor。

**拒绝**：完整 Gateway、渠道消息队列、插件系统、SQLite session store、把 queue mode
直接当 NS task 状态机。

## 7. OpenHands SDK：以不可变事件建立可恢复 View

### 7.1 Event 是事实，View 是可重建投影

[`event/base.py#L20-L40`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/event/base.py#L20-L40)
中的 `Event` 是 frozen、extra-forbid 的 typed model，包含 id、timestamp、source 和
`parent_id`，天然支持分支 ancestry。

[`conversation/state.py#L295-L395`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/conversation/state.py#L295-L395)
将 append 收口为单一 chokepoint：补 parent、推进 HEAD，并增量更新 View；恢复或切分支时
从事件全量重建。
[`context/view/view.py#L22-L160`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/context/view/view.py#L22-L160)
将可发给 LLM 的上下文封装为 property-safe projection，非 LLM 事件不会混进消息列表。

这正适合 NS：`RunEventLog` 继续是 operational source of record，`RunState`、TaskGraph
状态、Context View 都是按 event position 可重建的投影。投影可缓存，但缓存损坏不能
反向污染事件和 Canon。

OpenHands 的
[`event_store.py#L30-L40`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/conversation/event_store.py#L30-L40)
自己警告文件锁在 NFS 上不可靠；虽然它在 append 时检查 duplicate id/parent 并支持
path-to-root，NS 不应迁移文件 EventStore。NS 当前 PostgreSQL 锁、幂等和单调 sequence
更适合生产恢复。

### 7.2 Condensation 本身必须成为 Event

[`event/condenser.py#L11-L96`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/event/condenser.py#L11-L96)
把 `Condensation` 建模为事件，记录 forgotten event IDs、summary、offset、LLM response
identity；摘要事件 ID 可确定性生成。View 遇到它时改变投影，但原事件仍存在。

这比“在 messages 数组上原地替换”更符合 NS：

```text
raw RunEventLog:  永不因上下文压缩删除
summary artifact: 可版本化、可 hash、可回溯 provenance
ContextCompacted: 指向 covered event range + summary artifact + kept boundary
Context View:     由事件和最新有效 compaction 投影生成
```

压缩结果只改变下一次模型看到的上下文，不改变小说 Canon，也不改变曾经发生过的 tool、
effect、evaluation 或 commit 事实。

### 7.3 工具批次原子性是 provider-validity invariant

[`test_view_condensation_batch_atomicity.py`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/tests/sdk/context/view/test_view_condensation_batch_atomicity.py)
验证一个多工具 LLM response 中，只要任一 observation 被遗忘，就必须同时处理整个 action
batch 及对应 results；不能留下孤立 tool result 或有 call 无 result 的结构。

NS 的 Context Compiler/Compactor 应把以下内容当一个结构组，而不是独立 token 片段：

```text
assistant response(tool_calls=[a,b])
  + tool_call(a) + tool_result(a)
  + tool_call(b) + tool_result(b)
```

cut、prune、summary 和 resume 都不得破坏这个组。这个 invariant 应进入 property/regression
tests，而不是依赖 prompt provider 报错后再修。

### 7.4 Pause、interrupt、fork 的精确边界

[`local_conversation.py`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py)
区分：pause 在 step 之间生效，已经同步发出的 LLM 调用可以完成；interrupt 才取消异步
任务，并为 orphan tool call 补合成错误。fork 深拷贝 agent/state/events，可从指定 event
建立 branch；navigate 只改变 HEAD，不删除事件。

对 NS 的启示不是复制对话分支，而是冻结术语：

- `pause`：不领取下一安全 step，不假装取消正在发生的外部 effect；
- `interrupt`：请求取消当前 attempt，最终状态取决于 cancellation receipt；
- `resume`：从 checkpoint/effect reconciliation 后继续同一 lineage；
- `fork`：创建新的 run lineage 和 candidate artifacts，不修改原 run；
- Canon branch/commit DAG 与 operational run lineage 必须保持不同 namespace。

### 7.5 OpenHands 最终裁决

**迁移**：frozen typed event、append chokepoint、event→View、Condensation event、tool batch
atomicity、pause/interrupt/fork 的边界。

**参考**：parent ancestry、全量/增量投影一致性测试。

**拒绝**：文件 EventStore、用 conversation HEAD 替代 Canon Commit、把运行分支与小说
版本分支混为一体。

## 8. PydanticAI：未来叶子 Agent Loop 的 durable adapter

### 8.1 它解决的是 I/O durable 化，不是项目 Runtime

PydanticAI 的
[`docs/durable_execution/temporal.md#L5-L29`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/docs/durable_execution/temporal.md#L5-L29)
把 deterministic workflow 与外部 I/O activity 分开。文档在
[`#L69-L74`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/docs/durable_execution/temporal.md#L69-L74)
明确：仅给 Agent 挂 durability capability 不会自动 durable，Agent 必须在 Temporal
workflow 内运行。

它适合将模型请求、tool call、stream/cancel 变成 activities；不负责小说项目 TaskGraph、
Canon commit、candidate artifact、Evaluation Ledger 或 project mutation lane。

### 8.2 稳定名称和可序列化边界

[`temporal/_durability.py#L301-L403`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_durability.py#L301-L403)
注册 model/stream/cancel/tool activities，并为它们生成稳定名称；
[`#L453-L571`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_durability.py#L453-L571)
在 workflow 内禁用线程式行为、使用 durable sleep，并把 model request 代理到 activity；
workflow 外仍可透明运行。

文档的关键约束包括：

- Agent/toolset ID 必须稳定；payload contract 必须可序列化和版本化；
- activity 中对 `RunContext` 的修改不会自动回到 workflow；
- 大 payload 应放外部 artifact/object store，只在 history 传引用；
- streaming 实际需要 buffer/replay，不等于每个 token exactly-once；
- provider suspended turn 要分割成可 checkpoint 的 activities；
- framework、workflow、activity、provider 的 retry 不应层层相乘。

这些约束与 NS 的 Artifact Store、schema version、RunEvent 和 EffectReceipt 完全兼容。

### 8.3 Fail-closed context 与异常分类

[`temporal/_run_context.py#L51-L155`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_run_context.py#L51-L155)
只序列化 activity 真正可用的字段，对 unavailable 字段 fail closed，并阻止 activity
内错误地执行 enqueue。

[`temporal/_toolset.py#L54-L95`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_toolset.py#L54-L95)
发送 activity heartbeat；
[`#L156-L309`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_toolset.py#L156-L309)
区分 non-retryable UserError/Unexpected/PayloadSize，并对错误配置 fail closed，避免 workflow
task 无限 retry。

[`temporal/_activity_execution.py#L1-L53`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_activity_execution.py#L1-L53)
用 cancellation-safe shield 只转发一次 cancel，避免 AnyIO/Temporal cancellation livelock。

NS 即使暂不采用 Temporal，也应提前统一 error taxonomy：user/policy/config/schema 错误默认
non-retryable；provider/transient/timeout 可按层重试；payload-too-large 应转 artifact reference，
而不是盲目重试。

### 8.4 Temporal 的启用门槛

保留现有设计的门槛：只有以下至少两项成为真实需求，才做 Temporal benchmark/ADR：

- 单次运行跨日；
- 长时间 human/external wait；
- 跨进程或跨机器恢复；
- 复杂且不可本地封装的外部 effect；
- 多项目并发使自建 scheduler/recovery 成本显著；
- PostgreSQL queue + RunEventLog 的运维复杂度被实测证明过高。

即使采用 Temporal：

- Temporal history 只负责 workflow replay；
- NS `RunEventLog` 仍负责领域可审计运行事实；
- Canon 和 Artifact Store 仍负责小说事实和大对象；
- `EffectReceipt` 仍负责跨系统副作用身份；
- PydanticAI 运行在 leaf activity/child workflow，不成为整个项目控制面。

### 8.5 PydanticAI 最终裁决

**条件式迁移**：未来的 typed Agent loop、model/tool activity adapter。

**现在参考**：稳定 ID、版本 payload、artifact reference、exception taxonomy、heartbeat、
cancellation bridge、retry ownership。

**拒绝**：现在引入 Temporal、用 workflow history 替代 RunEventLog、让叶子 Agent 管理 Canon
或 project TaskGraph。

## 9. PydanticAI Harness：边界形状有价值，Runtime 不成熟

### 9.1 StepPersistence 自己明确不是完整 checkpoint

Harness 的
[`pyproject.toml#L18`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pyproject.toml#L18)
当前标记为 `Development Status :: 3 - Alpha`。其
[`step_persistence/README.md#L11-L22`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/step_persistence/README.md#L11-L22)
明确区分“记录步骤边界”和“能够安全恢复完整 graph”：capability state、workspace、node 内
恢复等均不在范围内。

[`step_persistence/_types.py#L11-L143`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/step_persistence/_types.py#L11-L143)
定义 StepEvent、ContinuableSnapshot、ToolEffectRecord 和 RunRecord，区分
conversation/run/step identity、parent run、effect status、complete/interrupted snapshot。

[`_capability.py#L238-L374`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/step_persistence/_capability.py#L238-L374)
为一次 run 分配单次 identity，注册运行并在成功/失败时写 final snapshot；失败依据 settled
程度分类 complete/interrupted。
[`#L411-L555`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/step_persistence/_capability.py#L411-L555)
记录 tool effect started/completed/failed，只在 settled `CallToolsNode` 保存健康 continuation。

最值得 NS 迁移的是两个问题：

1. checkpoint 是否位于 provider-valid、tool-settled 的边界？
2. crash 时 effect 是 `not_started`、`started_unknown`、`completed` 还是 `failed`？

最不应该迁移的是
[`_store.py#L132-L178`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/step_persistence/_store.py#L132-L178)
定义的独立 StepStore。NS 已有 RunEventLog/Checkpoint；再加 Store 会制造第二事实源和双写。

### 9.2 Continuation 必须保留工具配对

[`_helpers.py#L21-L89`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/step_persistence/_helpers.py#L21-L89)
在 continue/fork 时保持 provider-valid tool pairing，默认只选最新 complete snapshot；选择
interrupted snapshot 必须显式 opt-in，并先完成 effect reconciliation。

这应成为 NS `RunCheckpoint.resumability` 的实义，而不只是布尔标签：

- `safe`: settled，所需 state artifact 完整，所有 started effects 已知；
- `reconcile_required`: 结构可恢复，但存在未知 effect；
- `restart_step`: 不可在当前节点继续，只能从前一 safe checkpoint 重启；
- `non_resumable`: basis/artifact/schema/permission 无法重建。

具体 enum 仍需 Stage 5 ADR 冻结，但 checkpoint API 必须能够表达这些差异。

### 9.3 Compaction receipt 与分层策略

[`compaction/_shared.py#L274-L466`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/compaction/_shared.py#L274-L466)
仅在消息实际变化时产生 compaction span/receipt，并选择保持 tool call/return 配对的安全
cutoff。
[`_receipts.py#L1-L91`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/compaction/_receipts.py#L1-L91)
生成确定性 receipt，标记 secondhand/dropped，可指向 persisted run handle，刻意不加入
会破坏确定性的 timestamp。

[`_tiered_compaction.py#L30-L181`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/compaction/_tiered_compaction.py#L30-L181)
按 cheap→expensive 逐层执行，每层后重新计量并尽早停止；
[`_summarizing_compaction.py#L227-L437`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/compaction/_summarizing_compaction.py#L227-L437)
对昂贵摘要计入 usage、复用 previous summary、保留 first user/pinned content，并可附 receipt。

但 Harness 的消息 compaction 会编辑待发送 history，且持久 snapshot 可能已经只有 compacted
history；
[`compaction/README.md#L406-L434`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/compaction/README.md#L406-L434)
也明确 transcript handle 指向 persisted run，不承诺 pristine transcript 永久存在。NS 必须
采用更强约束：
原始 `RunEventLog` 不可因 retention/compaction 消失；compacted messages 只是可重建 View。

### 9.4 Planning 和 Dynamic Workflow 不适合作为 NS TaskGraph

[`planning/_types.py#L11-L67`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/planning/_types.py#L11-L67)
和
[`planning/_toolset.py#L130-L184`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/planning/_toolset.py#L130-L184)
主要是 Agent 可操作的 plan UI；其 cancelled prerequisite 可被视为解除阻塞，不具备 NS
所需 attempt/lease/effect/commit 语义。

Dynamic Workflow 是单次 tool call 内运行的 sandbox script；中间值留在 sandbox，只限制
agent calls，不提供 durable task graph。它可以降低 token 往返，但不能承担长期调度或恢复。

### 9.5 Harness 最终裁决

**迁移形状**：settled boundary、complete/interrupted、effect started/terminal、deterministic
receipt、tiered compaction。

**参考**：continuation/fork helpers、usage accounting。

**拒绝**：StepStore、Planning 作为 TaskGraph、Dynamic Workflow 作为 Runtime、依赖 alpha
API、把 compacted snapshot 当完整历史。

## 10. 建议的 NS 最小充分 Runtime 结构

以下是由源码证据推出的后续设计方向，不是当前实现任务。

### 10.1 单一事实源与投影关系

```text
Canonical five roots                         小说领域事实
        │
        ├── candidate artifacts / commits    候选与提交 lineage
        │
RunEventLog                                  唯一运行事实源
        ├── RunState projection              当前运行视图
        ├── Task/Attempt/Dependency projection  durable 调度视图
        ├── Context View                     发给模型的结构化投影
        └── Evaluation Ledger refs           评估证据引用

Artifact Store                               大 state/summary/output/receipt payload
RunCheckpoint                                event position + state artifact + effects
EffectReceipt                                外部副作用身份与结果
```

`TaskRecord` 可以有 PostgreSQL 物化表用于 claim，但表的状态变化必须和 RunEvent append
同事务或可靠 outbox 落地，并可从事件/审计证据校验。不能出现 Task DB、StepStore、Temporal
history 三者都声称是最终事实的情况。

### 10.2 最小任务合同候选

```text
TaskRecord
  task_id, project_id, kind, desired_state, projected_status
  dependency_revision, basis_refs, priority, not_before
  current_attempt_id, block_kind, block_fingerprint
  retry_policy_ref, created_event_id, revision

TaskAttempt
  attempt_id, task_id, run_id, ordinal
  owner_id, lease_expires_at, heartbeat_at, fence
  started_event_id, ended_event_id, outcome
  checkpoint_id, effect_reconciliation_status

TaskDependency
  task_id, prerequisite_task_id, condition, revision
```

状态与依赖 readiness 应分开：`ready` 是当前派生资格，不是用户意图；`blocked` 只保留给
需要外部决定的条件。`not_before`/timer wait 不应伪装成失败。

### 10.3 Claim/recovery 的强不变量

1. claim 必须在 transaction 内重查依赖、task revision、basis、permission 和取消状态；
2. 每次 claim 都新建 attempt 和 fence，不复活旧 attempt；
3. heartbeat 只延长当前 fence；迟到 worker 的 event/effect/commit 一律拒绝；
4. lease 到期只触发调查，不自动授权第二次 external effect；
5. `effect.started` 在 retry 前必须通过 idempotency 查询、provider receipt 或人工对账终结；
6. resume 重查 pinned Canon Commit/Snapshot/Profile/scoped readiness；basis 改变则 fork/replan，
   不能提交旧 candidate；
7. Task projection 与 claimed event 必须原子提交或有可证明的 outbox 修复路径。

### 10.4 控制命令与状态分离

建议未来明确 `RunControlCommand`：

| 命令 | 预期语义 | 安全边界 |
|---|---|---|
| steer | 注入仍兼容当前 step 的高优先输入 | 只在 agent-declared steer point |
| followup | 当前 attempt settle 后创建后续 turn/task | 不改变当前输入 |
| collect | 合并兼容的等待输入 | 保留来源、顺序和权限 |
| pause | 不开始下一 step | 不伪造当前 effect 已取消 |
| interrupt | 请求取消当前 attempt | 等待 cancellation/effect receipt |
| cancel | 使 task desired state sticky-cancelled | child 创建和迟到写 fail closed |

控制命令也要有 command identity、expected revision 和 receipt，避免重复点击或迟到 callback
破坏新 run。

### 10.5 Compaction 的建议事件链

```text
context.compaction.requested
  → state/checkpoint preflush（恢复必要部分 fail closed）
  → summary artifact persisted（含 covered range/provenance/hash/model usage）
  → context.compacted（含 kept boundary、basis revision、receipt）
  → optional RunCheckpoint
```

Context Compiler 读取事件后生成 View；遇到有效 compaction event 时用 summary artifact +
kept tail 投影。以下变化必须 full recompile，而不是应用旧 ContextDelta：basis/POV/access
scope、budget policy、task replanning、Canon commit、compaction revision 变化。

### 10.6 每个候选组件的最小充分证明

| 候选 | 当前 caller | responsible layer | protected invariant | 最低验收证据 |
|---|---|---|---|---|
| Task projection | Stage 5 fixed chapter loop | runtime/service | 单一 task owner、依赖可重算 | claim race + dependency flip tests |
| TaskAttempt lease | Stage 5 multi-worker runtime | runtime/service | 旧 owner 不可迟到提交 | expiry/live-worker/effect-unknown tests |
| Project mutation lane | Writer/Curator commit path | runtime admission | 同一 basis 单有效写者 | queued generation invalidation test |
| Control commands | user/supervisor input | runtime control | 意图不污染 task facts | duplicate/late command tests |
| Context projection | Context Compiler | service/domain view | 原事件不变、provider-valid | full vs incremental property tests |
| Compaction event | context budget gate | context service | 可追溯、可重建、工具配对 | batch atomicity + crash-point tests |
| Temporal adapter | future leaf Agent | adapter | I/O replay 不重复 effect | cross-process recovery benchmark |

没有明确 caller 或验收证据的组件继续 deferred。

## 11. 分 Stage 采用建议

### Stage 3：Writer Loop + Context View，不建长期调度器

- 保留 Writer→Editor→repair/rewrite→Curator/reconciliation 候选链；
- 在给定章节规划的 Stage 2M Writer Context 上增加 reactive MemoryNeed、ContextDelta 和安全 compaction；
- 用现有 RunEventLog 构造可重建 Writer Context View，验证 full/incremental 等价；
- checkpoint 只出现在 tool/effect settled 的 provider-valid boundary；
- 不新增 task/scheduler 表、Temporal 或通用外部 Hook/control plane。

### Stage 4：Planner Loop 复用 Context Runtime，但使用独立 Memory 目标

- Planner 先产生 PlanningInquiry/GoalProposal，再生成 Planner-specific MemoryNeed；
- 建 PlannerContextPackage 和同一个 AgentContextView/compaction port，不复用 WriterContextPackage；
- Context compaction event 只指向 Artifact Store，不删除 event；
- Planner/Plan Reviewer 的 inquiry、Memory、revision 全部保留 candidate lineage；
- 高级读取只成熟化 Planner 真正需要的 graph path、conditional expand 和 compact→expand。

### Stage 5：固定拓扑、长期任务和 Supervisor

- 由真实 Planner→Writer→Commit chapter/volume loop 引入最小 `Task/Attempt/Dependency` projection；
- PostgreSQL claim 在事务内重查 dependency、basis、permission 和 cancel；
- 加 project mutation lane，但继续复用现有 ModelRequestAdmission；
- 再按证据加入 heartbeat、lease/reclaim、scheduled task、Supervisor、control intent 和长期恢复；
- 外部 Hook ingress 只接 Runtime 外 surface，异步派生，不默认 Context 注入；
- Experience/Skill 演化只生成 candidate 并走 held-out Gate；
- 达到至少两个 Temporal trigger 后才做 PG Runtime 与 Temporal 对照 benchmark。

## 12. 明确拒绝的架构方向

1. **第二运行事实源**：不新增独立 StepStore、conversation DB 或 workflow history 作为权威。
2. **把 Temporal 当业务模型**：Temporal 只可成为执行 adapter，不拥有 Canon/Task semantics。
3. **把压缩当历史删除**：Context View 可丢细节，RunEventLog 和 artifact provenance 不可丢。
4. **lease 到期即重试**：必须先 fencing owner 并 reconciliation unknown effects。
5. **一把锁解决全部并发**：project writer、task claim、model endpoint admission 必须分责。
6. **把 queue mode 当 task state**：steer/followup/collect/interrupt 是输入策略。
7. **直接复用 Harness Planning**：它没有 durable attempt/effect/commit 语义。
8. **在 Stage 3/4 预建 Stage 5 长期平台**：当前 caller 和 gate 不成立，违反最小充分工程。
9. **照搬 Hermes 单文件 Runtime**：其耦合和存储边界不适合 NS 的分层与 PostgreSQL 基础。
10. **运行分支等同 Canon 分支**：run lineage、candidate lineage、Canon commit DAG 必须分离。

## 13. 后续源码阅读索引

若架构/实现负责人只读最关键代码，建议按以下顺序：

### Durable task 与恢复

1. Hermes `kanban_db.py`：Task/schema → `recompute_ready` → `claim_task` → heartbeat/reclaim →
   block/unblock。
2. Hermes `cron/jobs.py` 与 `cron/scheduler.py`：claim fire、advance schedule、pre-check、tick lock。
3. Harness StepPersistence `_types.py` → `_capability.py` → `_helpers.py`：effect 与 safe boundary。

### Session 并发与控制

1. OpenClaw `session-actor-queue.ts`。
2. OpenClaw `lane-controller.ts` 和 lifecycle/writer-claim tests。
3. OpenClaw `agent-steering-queue.ts`、`chat-queued-turns.ts`。
4. OpenClaw `queue.md`、`queue-steering.md`、`taskflow.md`。

### Context 与 compaction

1. OpenHands `event/condenser.py`、`context/view/view.py`。
2. OpenHands `test_view_condensation_batch_atomicity.py`。
3. OpenClaw `session.ts`、`compaction.ts`、compaction lifecycle 文档。
4. Harness compaction `_shared.py`、`_receipts.py`、`_tiered_compaction.py`。
5. Hermes `context_compressor.py` 和 `conversation_compression.py`。

### Temporal 适配

1. PydanticAI `docs/durable_execution/temporal.md`。
2. `_durability.py` → `_run_context.py` → `_toolset.py` → `_activity_execution.py`。
3. 优先看错误、取消、payload 和 retry 章节，不要先从 demo 推断生产语义。

## 14. 建议的验证实验

后续进入相应 Stage 时，先用实验裁决设计，不按上游知名度裁决：

1. **双 worker claim race**：同 task 只有一个 attempt 成功，失败者不产生 effect。
2. **dependency flip race**：ready 后、claim 前 prerequisite 被 supersede，claim 必须失败。
3. **lease false positive**：worker 活着但 heartbeat 延迟，不能启动第二 writer。
4. **unknown effect crash**：provider 已执行但 receipt 未写，恢复必须先查询而非重放。
5. **late callback**：旧 generation 完成后不能删除或覆盖新 run/current attempt。
6. **tool batch compaction**：任意预算 cut 后消息仍满足 provider pairing。
7. **projection equivalence**：增量 RunState/Context View 与全量 replay 完全一致。
8. **compaction crash matrix**：preflush 前、summary 后、publish 前后崩溃均可确定恢复。
9. **basis change resume**：Canon commit 改变后旧 candidate 不得直接进入 Curator commit。
10. **retry multiplication**：provider/activity/workflow/task 各层组合仍满足总尝试上限。
11. **scheduler restart**：missed-run policy 确定，不出现恢复后的 LLM 请求风暴。
12. **Temporal 对照**：只有 PG 实现跨日恢复成本明显失控时，Temporal 才通过引入门槛。

## 15. 最终架构判断

对 NS 最强的上游参考不是某个“全能 Agent 框架”，而是五个项目各自守住的边界：

- Hermes 证明任务正确性必须落在 attempt、transactional claim 和 recovery failure paths；
- OpenClaw 证明 session writer、全局容量和输入意图是三种不同控制问题；
- OpenHands 证明压缩和分支应通过不可变事件的 View 表达；
- PydanticAI 证明 durable workflow 的价值在 I/O activity 边界和 replay contract；
- Harness 证明 settled step/effect receipt 有用，也反向证明“有 snapshot”不等于完整恢复。

因此，NS 后续最稳妥的方向是继续以现有 `RunEventLog + RunCheckpoint + EffectReceipt +
Artifact Store + Canon` 为骨架，在真实 Stage caller 出现时逐步补：

```text
Task/Attempt semantics (Hermes)
  + project single-writer and control intent (OpenClaw)
  + event-derived Context View (OpenHands)
  + settled effect boundary (Harness)
  + optional leaf durable adapter after trigger (PydanticAI)
```

这条路线既吸收成熟项目最难得的失败语义，又不引入平行状态机、平行存储和提前平台化，
与 NS 的单一事实源、Stage fail-closed 和 minimum-sufficient engineering 保持一致。

---

## 16. 实现级源码审计：先固定身份和所有权

前 15 节回答“应参考什么”。从本节开始回答“一条命令如何实际落库、哪个
crash window 会留下什么状态、NS 的哪个 owner 应该吸收它”。后续设计先使用
下列五层 identity，不能用一个 `run_id` 代替全部含义：

| Identity | 稳定性 | 所有内容 | 不得兼任 |
|---|---|---|---|
| `project_id` | 长期 | Canon/working state 归属 | task attempt 所有权 |
| `task_id` | 跨重试稳定 | 用户/调度器意图、依赖、终态 | 一次 worker 执行 |
| `attempt_id` | 一次执行 | owner、lease、heartbeat、outcome | 任务跨重试历史 |
| `run_id` | 一条运行 lineage | 有序 RunEvent stream、checkpoint | Canon commit 或 task identity |
| `effect_identity` | 一个外部意图 | 幂等、provider receipt、reconciliation | attempt 的生命期 |

其中 `task_id` 和 `effect_identity` 必须可跨 attempt 对账；`attempt_id` 不得复用；
`run_id` 是事件流 identity，可与 attempt 一对一起步，但不应在领域合同中暗示两者
永远相同。未来 Temporal child workflow、provider continuation 或 fork 都可能使它们分开。

### 16.1 AttemptFence 不能是可选参数

五个上游项目的迟到回调问题最终都可归约为：“完成这次写入的人，还是否是现在的
owner？” NS 需要的最小 fencing 不是单一 lease token，而是：

```text
AttemptFence = {
  task_id,
  attempt_id,
  claim_token,          # 不可猜测的所有权 token
  task_revision,        # claim 时看到的任务投影版本
  writer_generation,    # 涉及 project/session mutation 时必需
}
```

所有 worker-originated `heartbeat / checkpoint / effect terminal / complete / block / fail`
命令都必须携带完整 fence。不能为 CLI 或测试便利设计“不传就跳过检查”的
生产 API；人工管理操作应使用另一个显式 `operator_reclaim/operator_complete` 命令，并写入
actor/reason/audit event。

### 16.2 三类“等待”不能共用一个 lease

```text
Task claim lease       -> 谁可以改变 task/attempt 运行状态
Project writer claim   -> 谁可以提交 working state / candidate / Canon mutation
Model capacity lease   -> 谁现在可以占用 endpoint request/KV 容量
```

这三类 lease 的作用域、TTL 和释放条件不同。特别是现有
[`ModelRequestLease`](../src/novel_agent/services/model_request_admission.py#L86-L115)
只是进程内 endpoint reservation；它不能被 checkpoint 持久化，也不能证明 task 或
project writer 所有权。

## 17. Hermes Agent 函数级审计

### 17.1 `claim_task` 的真实事务链

[`claim_task`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L4226-L4345)
不是单条 `UPDATE ready -> running`，而是在 `BEGIN IMMEDIATE` 内完成下列顺序：

1. 读取 candidate task 和 parents，不信任事务前的 ready projection。
2. 如有 parent 不是 `done/archived`，用 CAS 将错误 ready 降回 todo，并记
   `claim_rejected` event。
3. 如 `current_run_id` 还指向一个未结束 run，先把泄漏 run 关闭为 reclaimed。
4. 以 `status=ready AND claim_lock IS NULL` 作为 CAS 条件，写 running、claim token、
   expiry、worker PID/host、heartbeat。
5. 插入一个全新 `task_runs` row，把 `tasks.current_run_id` 指向它。
6. 追加带 `run_id` 的 `claimed` event，提交后才执行 lifecycle hook。

事务 helper
[`write_txn`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L2801-L2838)
对 busy begin/commit 有限重试，rollback 失败不覆盖原异常。这个代码链证明了一个不可
拆分的原子单元：

```text
task owner transition
  + fresh attempt creation
  + current_attempt pointer
  + claimed domain event
```

NS 不应按 Hermes 的 SQLite 写法照搬，但在 PostgreSQL 中也必须使上述四项共享
一个 transaction。现有
[`RunEventLogRepository.append`](../src/novel_agent/services/event_log.py#L30-L92)
每次自己开 transaction，因此未来不能由 Task service 先改 task row、再调这个方法。
所需的是可在同一 SQLAlchemy `Session` 中追加事件的 transaction-scoped 内部接口，
对外仍保留一个命令 chokepoint。

### 17.2 Hermes 状态转换的前置、写集和迟到写防护

| 命令 | 必要前置 | 事务内写集 | 旧 worker 防护 | 事务后 |
|---|---|---|---|---|
| claim | ready、parents terminal、no owner | task + run + event | claim CAS | lifecycle hook |
| heartbeat | running、matching claim | task lease + run heartbeat | claim token | 无 |
| complete | running/current run | task terminal + end run + event | `expected_run_id` 可选 | artifact cleanup |
| dependency block | running/current run | task→todo + end run + event | `expected_run_id` 可选 | failure budget reset |
| human/capability block | running/current run | task→blocked + recurrence + end run + event | `expected_run_id` 可选 | 无 |
| unblock | blocked | 重查 parent、todo/ready + close leaked run + event | task status CAS | failure budget reset |
| stale reclaim | expired + liveness verdict | clear owner + end run + diagnostic event | claim snapshot CAS | 重排/报警 |

Hermes 的
[`complete_task`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L4906-L4995)、
[`block_task`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L5618-L5827)
和
[`heartbeat_worker`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L7140-L7188)
只在调用者传 `expected_run_id` 时才做 attempt fencing；heartbeat SQL 也没有把已过期作为拒绝
条件。这是值得明确记录的上游弱点：**NS 应迁移 invariant，不应复制它的 API
宽松性**。

### 17.3 reclaim 不是一个 timeout callback

[`release_stale_claims`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L4454-L4597)
的决策树是：

```text
lease expired?
  no  -> ignore
  yes -> same-host PID alive?
           yes + heartbeat not stale -> extend lease, emit diagnostic
           yes + heartbeat > 1h      -> request termination
           unknown/alive after kill  -> do not reclaim; extend/defer
           dead                      -> CAS(task, old claim token), close attempt, reclaim
```

这里 PID 只对单主机有意义，所以 NS 不能迁移 PID 规则，但应迁移“多信号、最终
CAS”。对 NS，信号强度建议为：

```text
durable worker cancellation/termination receipt
  > provider job status / idempotency lookup
  > worker control-plane heartbeat
  > attempt heartbeat
  > lease wall-clock expiry alone
```

即使已确认 worker 死亡，也只能证明可以剥夺“未来写入权”，不能证明外部
effect 没有发生。所以 NS reclaim 必须先把该 attempt 下所有
`EFFECT_REQUESTED` 但无 terminal receipt 的 effect 投影为 `UNCERTAIN`，对账后才能决定
retry/compensate/human review。

### 17.4 失败预算要按原因分帐

[`detect_crashed_workers`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L7612-L7880)
区分 provider rate-limit、真 crash 和“worker clean exit 但 task 仍 running”的 protocol violation。
[`_record_task_failure`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L7882-L8010)
对 protocol violation 使用独立 streak，per-task max retries 覆盖 dispatcher default。

NS 建议从一开始就使用 typed `FailureClass`，而不是一个 `attempt_count`：

| FailureClass | 是否消耗 task retry | 默认处理 |
|---|---:|---|
| rate limit / admission timeout | 否 | backoff，继续 ready/waiting |
| invalid config/schema/permission | 否，但不自动重试 | policy blocked / operator action |
| provider transient | 是，由唯一 retry owner 计数 | bounded retry |
| worker crash before effect | 是 | new attempt |
| protocol violation | 独立预算 | circuit break / triage |
| uncertain external effect | 不得直接计为失败 | reconcile first |
| deterministic domain rejection | 不重试相同输入 | supersede/replan |

### 17.5 Hermes 测试给出的可迁移验收证据

生产代码之外，下列测试锁定了真正应迁移的边界：

- [`test_kanban_core_functionality.py#L405-L575`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/tests/hermes_cli/test_kanban_core_functionality.py#L405-L575)：
  旧 run 不能 heartbeat/block 新 attempt；claim 会先关闭泄漏 run；unblock 关闭 dangling run。
- [`test_kanban_reclaim_claim_lock_guard.py#L46-L113`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/tests/hermes_cli/test_kanban_reclaim_claim_lock_guard.py#L46-L113)：
  拿旧 claim token 做 crash reset 不得覆盖已接管的新 worker。
- [`test_kanban_db.py#L183-L397`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/tests/hermes_cli/test_kanban_db.py#L183-L397)：
  scheduled task 不可提前 claim；reclaim event 必须带 lease/heartbeat/PID/host 证据；
  rate limit 不消耗失败预算；readiness 与 dispatcher 使用同一 failure-limit 解析。
- [`test_kanban_blocked_sticky.py#L56-L132`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/tests/hermes_cli/test_kanban_blocked_sticky.py#L56-L132)：
  human block 经反复 readiness recompute 仍 sticky，防止无限 protocol loop。
- [`test_kanban_block_kinds.py#L66-L112`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/tests/hermes_cli/test_kanban_block_kinds.py#L66-L112)：
  同 cause 重复 block 会触发 loop detection；dependency 满足后才 promote。
- [`test_claim_job_for_fire.py#L21-L60`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/tests/cron/test_claim_job_for_fire.py#L21-L60)：
  同一 fire 只能一个 scheduler 获得，claim 与 next schedule 推进一起发生。
- [`test_script_claim_heartbeat.py#L18-L138`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/tests/cron/test_script_claim_heartbeat.py#L18-L138)：
  阻塞脚本期间 heartbeat 防止第二 scheduler 重复 fire；旧 owner 不得刷新替换 claim。

这些测试应被改写为 NS 的 property/integration/regression 验收，不需要复制其 fixture
或 SQLite 细节。

### 17.6 需要主动修正的 Hermes 矛盾

源码审计发现一个不应被迁移的语义不一致：
[`recompute_ready`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L4135-L4219)
和 claim 将 `done/archived` 都当作 parent terminal；但
[`unblock_task`](https://github.com/NousResearch/hermes-agent/blob/326bdfb7a27e292a25aa1a8a073e6fac43460a98/hermes_cli/kanban_db.py#L5935-L5941)
的 SQL 仅认 `done`，会把 archived parent 当作未完成。

所以 NS 不能让 `recompute_ready / claim / unblock / resume` 各自写一份前置逻辑。必须有一个
纯函数式 `evaluate_task_eligibility(snapshot, now, policy)`，上述四个入口共用；claim 在锁内
重算，背景 recompute 只可用它生成索引投影。

## 18. OpenClaw 函数级审计

### 18.1 lane admission 的完整顺序

[`lane-controller.ts#L29-L253`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/src/agents/embedded-agent-runner/run/lane-controller.ts#L29-L253)
展示的不是“加两把锁”，而是一段 admission protocol：

```text
capture priority/provenance/generation
  -> enqueue keyed session lane
  -> enqueue global lane
  -> after wait: abort check
  -> after wait: lifecycle generation check
  -> foreground-only explicit rebind OR reject background/stale descendant
  -> durable session-writer claim
  -> update expected lifecycle revision + writer run id
  -> placement/resource admission
  -> immediately before execution: abort/generation recheck
  -> mark queued context admitted; bind run context
  -> execute worker
  -> finally release/abandon queued context
```

重点是“排队前的权限和生命周期检查不会穿越 await 保鲜”。任何在 queue 中等待过的
run，都必须在获得 lane 后重新验证。对 NS，重新验证的不只是 generation，还包括：

- `AttemptFence` 是否仍 current；
- pinned `base_commit/snapshot/profile` 是否还可用；
- permission/information boundary 是否变化；
- task 是否被 cancel/supersede/block；
- candidate 是否仍属于当前 working-state generation。

OpenClaw 对排队期间的 lifecycle rotation 做了有条件的处理：旧 foreground user turn 可重绑，
background/subagent 默认拒绝。
[`lane-controller.lifecycle.test.ts#L110-L211`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/src/agents/embedded-agent-runner/run/lane-controller.lifecycle.test.ts#L110-L211)
锁定了 foreground rebind、background reject、aborted no-claim、stale descendant reject 和
不覆盖 newer same-id owner。NS 可以迁移这种“按来源定 rebind policy”的思路，但不应
默认让定时/背景 task 穿越 runtime restart 复活。

### 18.2 writer takeover 的安全顺序是先换权，后取消

[`lane-controller.writer-claim.test.ts#L53-L187`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/src/agents/embedded-agent-runner/run/lane-controller.writer-claim.test.ts#L53-L187)
验证了一个很关键的 takeover 顺序：

```text
transactionally change durable writer row from run-A to run-B
  -> run-B obtains expected writer revision
  -> cancellation signal is sent to run-A
  -> any late append from run-A carries old expected writer and is rejected
```

如果 run-B 的 durable claim 写失败，测试要求 run-A 继续活着，不能先 cancel 老 owner 再期待
新 owner 接管。这直接给出 NS `interrupt-and-replace` 的实现合同：

1. 新 attempt 或新 writer 用 CAS 提交所有权；
2. 追加 `writer.superseded` / `attempt.replaced` 运行事件；
3. 提交后再发 cancel 给旧 owner；
4. 旧 owner 的所有 artifact publish、effect terminal、checkpoint 和 task terminal 写入均使用
   旧 fence，必须被拒绝；
5. cancel 失败时新 owner 仍不得重放 uncertain effect，而是进 recovery/reconciliation。

这个顺序同时适用于 project writer generation 和 task attempt，但两者应有各自的 CAS row/owner，
不能共用一个简化 `current_run_id`。

### 18.3 queued input 是一个带 ACK 的小协议

[`chat-queued-turns.ts#L35-L240`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/src/gateway/chat-queued-turns.ts#L35-L240)
和其
[`tests`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/src/gateway/chat-queued-turns.test.ts#L61-L257)
锁定了这些不显眼的约束：

- protocol `runId` 使用精确字节，不能 trim 后相等；
- 相同 run/controller 重复注册幂等，相同 run/不同 controller 必须拒绝；
- abort listener 删除时必须检查 map 中仍是同一 entry object，不能删掉同 key 替换者；
- `collect` 转移 cancellation 时保留 entry identity，聚合完成才释放；
- session mismatch 时不得 abort 他人的 queued turn。

[`agent-steering-queue.ts#L1-L285`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/src/agents/agent-steering-queue.ts#L1-L285)
另外提供 pending→leased→acked/released，确定性排序，单项/总 prompt 字节预算，overflow
保留 pending，以及“这是运行数据，不是指令”的 prompt-injection 包装。其测试覆盖
注入失败后 release、stale lease reclaim、metadata sanitization 和确定性合并。

但两个实现都主要是进程内 map，steering 源码也明确称 lease 为 process-local coordination
hint。因此 NS 可以迁移 `InputIntent` 的 identity/lease/ack/release/budget/sanitization 协议，
但必须由现有 `RunEventLog` 和未来 control projection 持久化，不能把 OpenClaw map 称为
durable queue。

### 18.4 compaction 是重写 prefix 的投影事务

OpenClaw 的 compaction 代码链可分成四步：

1. [`isCutPointMessage/findCutPoint`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/packages/agent-core/src/harness/compaction/compaction.ts#L303-L430)：
   构建可切点列表，不从 tool result 开始，识别 split turn。
2. [`prepareCompaction`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/packages/agent-core/src/harness/compaction/compaction.ts#L674-L805)：
   找最新 compact/reset boundary，恢复 previous summary，对 provider usage 与估算 token 归一，
   分出 history prefix 与 split-turn prefix。
3. [`compact`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/packages/agent-core/src/harness/compaction/compaction.ts#L826-L905)：
   先摘要旧 history，再串行摘要 split-turn prefix，附加 file-operation provenance，产生
   summary/first-kept/tokens-before。
4. [`session.ts#L15-L138`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/packages/agent-core/src/harness/session/session.ts#L15-L138)：
   从最新 boundary 投影 summary + kept tail + later entries，并剔除 prefix 被改写后已不可重放的
   provider-specific checkpoint/cache marker。

[`compaction.test.ts#L353-L506`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/packages/agent-core/src/harness/compaction/compaction.test.ts#L353-L506)
验证 no-op skip、provider usage 归一、reset 后 orphan tool result 移除、只保留 occurrence-paired
result 和 reset prelude 对 cut 的影响；
[`#L648-L699`](https://github.com/openclaw/openclaw/blob/8fdf7570a17ffbbafe825bd379bab858f263b8ca/packages/agent-core/src/harness/compaction/compaction.test.ts#L648-L699)
明确要求两次摘要串行，防止 split prefix 失去前一摘要的语境。

NS 因此需要把“provider replay cache/checkpoint 失效”加入 compaction contract。仅保存
summary artifact 和 kept sequence 还不够；如果 provider/model adapter 缓存了旧 prefix，则 compaction
publish 必须使旧 prefix cache key 失效，下一次 model request 使用新 context hash。

### 18.5 OpenClaw 参考边界

OpenClaw 在 control-plane 顺序、迟到 callback 和 provider context 结构上很强，但当前这些对象
中不少仍是 process-local coordination。NS 应迁移：

- after-wait revalidation；
- durable writer CAS 先于 cancel；
- stale callback identity guard；
- queued input ACK 协议与 budget/sanitization；
- prefix rewrite 后 provider replay state 失效。

不应把 `chat-queued-turns` 或 `agent-steering-queue` 直接作为彼此跨进程的 durable 保证，
也不应把 session lane 当成 task lease。

## 19. OpenHands SDK 函数级审计

### 19.1 append 和 View rebuild 的责任边界

[`EventLog.append`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/conversation/event_store.py#L184-L227)
在锁中重新从磁盘 sync，检查 duplicate ID 和 parent 是否存在，然后写 event/index。
[`path_to_root`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/conversation/event_store.py#L106-L126)
对 parent 链做缺失和 cycle 检查。

[`ConversationState.append_event`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/conversation/state.py#L304-L334)
是上层唯一 append chokepoint：普通 event 自动指向 active leaf，新 root 显式指向 ROOT，
且只有会话语义 event 推进 HEAD。
[`ConversationState.view`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/conversation/state.py#L337-L395)
只在 cached leaf 是 new leaf 祖先时做增量 append；分叉、缓存不一致或增量失败时从 root
全量 rebuild，且 rebuild 失败不覆盖旧 cache。

这对 NS 的直接要求是：

```text
incremental projection is an optimization
full replay is the oracle
failed rebuild must not publish a partial projection
projection cache never repairs or rewrites source events
```

现有 `RunCheckpoint` 已绑定 `event_position`；未来 Context View/Task projection 都应携带
`basis_event_position + projection_schema_version + input_artifact_hashes`，不能只存一个不知来源的
JSON snapshot。

### 19.2 Context View 的 safe cut 是属性交集

[`View.manipulation_indices`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/context/view/view.py#L39-L50)
取所有 property 返回的可操作索引交集；一个 cut 对 tool pairing 安全但对 thinking
loop 不安全，仍然不可切。
[`enforce_properties`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/context/view/view.py#L74-L109)
遇到第一个违反就应用修正，然后从第一个 property 重新扫描，直到 fixed point。

上游已实现的结构属性包括：

- [`BatchAtomicity`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/context/view/properties/batch_atomicity.py#L10-L88)：
  同一 LLM response 的全部 action 是原子组；
- [`ToolCallMatching`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/context/view/properties/tool_call_matching.py#L15-L100)：
  action 和 observation 一对一，不得 orphan/duplicate，存在 pending call 时不可在内部操作；
- [`ToolLoopAtomicity`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/context/view/properties/tool_loop_atomicity.py#L14-L126)：
  provider thinking-block 与其 tool loop 不可拆开。

[`test_view_condensation_batch_atomicity.py#L29-L206`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/tests/sdk/context/view/test_view_condensation_batch_atomicity.py#L29-L206)
展示了 fixed-point 修复的级联效果：遗忘一个 observation 后，匹配 action 被移除，然后整个
action batch 被移除，最后另一个已变 orphan 的 observation 也被移除；不相关 batch
保留。所以“找一个不是 tool_result 的位置就切”远远不够。

NS 在这些 provider-validity 属性之外，还必须增加领域属性：

- information-scope/no-leak；
- base commit/snapshot/profile 一致性；
- mandatory continuity constraint 不可被可选摘要覆盖；
- evidence citation 与 claim group 不可拆开；
- compaction summary 不可伪装为新 user instruction；
- pending/uncertain effect 不可在 resume View 中被表达为已完成。

### 19.3 Condensation 只改投影，不修改因果历史

OpenHands `View.append_event`
[`#L111-L140`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/context/view/view.py#L111-L140)
遇到 `Condensation` 时对当前 View 应用它，普通事件只有 LLM-convertible 的才进上下文。
[`RollingCondenser`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/context/condenser/base.py#L95-L232)
在 soft trigger 无法安全压缩时返回原 View，hard trigger 才尝试 hard reset 并传播失败。

NS 应将这个区分落为明确 policy：

```text
SOFT_COMPACTION: cannot prove structural + information safety -> no-op, continue original view
HARD_CONTEXT_LIMIT: cannot produce safe view -> task.suspended/policy_blocked, never send malformed view
```

这能避免“模型上下文快满了”变成绕过 evidence/no-leak invariant 的紧急开关。

### 19.4 pause/interrupt/fork 的代码语义

[`LocalConversation.run`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py#L1850-L1980)
用 state FIFO lock 串行 step，pause 只在 step 之间检查。
[`pause`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py#L2552-L2575)
只允许 IDLE/RUNNING，幂等，当前 LLM/tool 可继续到边界。
[`interrupt`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py#L2577-L2635)
先设 cancellation token，再 cancel async task；`CancelledError` 在应用层被消化为 PAUSED/InterruptEvent，
并为未匹配 action 生成合成错误，保持 provider-valid history。

[`fork`](https://github.com/OpenHands/software-agent-sdk/blob/be6cd3b80b706bb14c91e604581a8de75cad61cc/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py#L712-L847)
在锁内先验证 branch，再通过序列化 roundtrip 深拷贝 agent/state/event，重置 metrics 并 rebuild
View。但它共享 workspace，这对 NS 的 candidate generation 不够强：NS fork 必须引用不可变
workspace snapshot 或独立 working overlay，不能让两个 run 无 fence 修改同一工作目录。

### 19.5 OpenHands 的一个实现假设

`View.from_events` 做全量 property enforcement，但增量 `View.append_event` 本身不在每次 append 后
重跑全部 fixed-point 验证；它假定 event producer 和 condenser 只在 safe manipulation index 操作。
NS 不应隐式依赖这个假设：在真正发 model request 的 dispatch boundary，必须对最终
Context View 做一次完整 structural + information-boundary validation；增量缓存只是提速。

## 20. PydanticAI Temporal 函数级审计

### 20.1 workflow 内部只组装确定性状态，I/O 全部出 activity

[`_register_activities`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_durability.py#L301-L403)
为 model request、stream、cancel 和 toolset 注册 activities，名称前缀来自稳定 agent name。Agent 实例在
workflow 外构建，使 worker 可预先注册 activity。
[`wrap_run`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_durability.py#L453-L464)
在 workflow 外保持透明，在 workflow 内禁用不可 replay 的线程路径，将 sleep 换成 durable
timer。
[`wrap_model_request`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_durability.py#L493-L571)
序列化 context，调度 request/stream/cancel activity，并在 workflow 侧捕获 history payload 超限。

这条代码链对 NS 的 leaf agent 边界可归纳为：

| 内容 | 应在 workflow/run state | 应在 activity/adapter |
|---|---|---|
| 步骤号、策略分支、已完成 receipt refs | 是 | 只读输入 |
| provider/model I/O | 否 | 是 |
| tool/external system I/O | 否 | 是 |
| retry/backoff 时间 | durable timer intent | activity attempt 执行 |
| 大 prompt/result | artifact ref + hash | 通过 adapter 取回 |
| live client、tracer、connection、lock | 否 | worker-local dependency |
| Canon mutation decision | NS domain service | 不在 generic activity 直接做 |

如未来采用 Temporal，activity 名称、input/output schema 和 non-retryable error list 都是跨部署
replay contract，需要与 `payload_schema_version` 一样版本化，不能将 Python 函数重命名当普通
refactor。

### 20.2 Durable RunContext 是最小 capability projection，不是对象序列化

[`_run_context.py#L51-L155`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_run_context.py#L51-L155)
只序列化 activity 需要的 stable fields，排除 live agent/model/tracer/messages/prompt/model settings/
validation context；访问被省略字段会抛 `UserError`，不是返回误导性 default。它还在
activity 内挂 `EnqueueGuard`，因为 enqueue 对 workflow state 的修改不会自动带回。

测试要求 run ID/metadata 保留，agent 被排除，序列化字段 exhaustive，被省略字段
fail closed，activity 内 enqueue 直接拒绝：
[`tests/test_temporal.py#L4236-L4515`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/tests/test_temporal.py#L4236-L4515)。

对 NS，这意味着未来 `ModelActivityInput` 不应接受整个 `WriterContextPackage`、
SQLAlchemy session 或 agent registry。它应仅包含：

```text
run/task/attempt/effect identities
attempt fence or immutable ownership proof
prompt/context artifact refs + hashes
model endpoint/config version
permission and information-scope digest
retry/timeout budget owned by this activity
trace linkage
```

所有必须影响 workflow/domain state 的变化都必须通过明确 activity result 或 NS RunEvent 返回；
不得依赖对 deps/context 对象的就地 mutation。

### 20.3 cancellation bridge 的核心是“只取消一次，然后等结果”

[`_activity_execution.py#L1-L53`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_activity_execution.py#L1-L53)
先 `start_activity`，再 shield await。外层收到 cancellation 时，它只对 handle 发一次 cancel，在
AnyIO shield 中等 activity 完成 cancellation protocol，然后重抛原 cancel。

这不是代码风格问题；
[`tests/test_temporal.py#L508-L710`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/tests/test_temporal.py#L508-L710)
测了 AnyIO cancellation 不 wedge workflow task、`asyncio.wait_for` 产生干净 timeout，甚至 activity
吞掉 cancel 时 workflow 仍能 cancel 并 replay history。如果没有 shield/once-only forwarding，容易在
调用层和 workflow 层之间形成 cancellation livelock。

NS 的 `interrupt` 也应区分三个事实：

1. `interrupt.requested`：控制者已发出意图；
2. `activity.cancel_requested`：runtime 已向当前 I/O owner 发一次取消；
3. `attempt.interrupted` 或 `effect.uncertain/completed`：执行结果已被确认。

第 1 步不能直接把 task 标成 cancelled，第 2 步也不证明 provider 没有完成 effect。

### 20.4 heartbeat 不是统一固定频率的保活线程

[`heartbeating`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_toolset.py#L54-L95)
使用 heartbeat timeout 的一半，无配置时每五秒，并保证：activity body 已失败时以
body error 为主；body 成功后 heartbeat 已崩溃则不得吞掉 heartbeat error。

Temporal tests 还展示一个重要反例：
[`tests/test_temporal.py#L10921-L11220`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/tests/test_temporal.py#L10921-L11220)
要求所有注册 activity 的 heartbeat supervisor 启停正确，但 tool activity 没有默认 heartbeat
timeout，因为 CPU-bound tool 可阻塞 event loop，造成假死判断。

所以 NS 必须按执行类型指定 liveness mechanism：远程 provider job 用 provider status/polling，
I/O activity 可用 heartbeat，CPU-bound 任务要用独立 process supervisor。“一次没 heartbeat 就重排”
在这三种情况中都不成立。

### 20.5 retry owner 矩阵

[`with_non_retryable_errors`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_toolset.py#L156-L170)
将 user/config/unexpected behavior/payload size 等标为 non-retryable；
[`activity config validation`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/pydantic_ai_slim/pydantic_ai/durable_exec/temporal/_toolset.py#L253-L309)
为未注册/无法恢复配置 fail closed，避免 workflow task 无限 replay。超大 model/tool result
也在 workflow 侧转成 actionable non-retryable error，因为 activity 自己不知道 server history 的最终
payload 大小。

NS 的重试不可同时由 provider SDK、model gateway、Temporal activity、leaf agent loop 和 task
scheduler 各做 N 次。每个 failure class 只能有一个 owner：

| 重试类型 | 建议 owner | 上层看到的记录 |
|---|---|---|
| HTTP transport/429 短重试 | model/provider adapter | one logical model attempt + provider subattempts |
| Temporal activity transport | Temporal retry policy | same effect identity/activity attempt metadata |
| 输出 schema repair | leaf agent/service | new model request identity, bounded repair index |
| task 业务重试 | task runtime | fresh attempt/fence |
| uncertain effect | reconciliation service | 先查询，不叫 retry |

每层的实际 subattempt 数必须进 audit，这样才能验证总预算不会乘法膨胀。

### 20.6 provider continuation 应是同一 effect 的分段

Temporal continuation tests
[`tests/test_temporal.py#L10367-L10920`](https://github.com/pydantic/pydantic-ai/blob/fc6a3ac506513150e2016ee5ba9785d792795150/tests/test_temporal.py#L10367-L10920)
验证 suspended provider turn 可每段作为 activity，段间用 durable timer，可从 history resume；中间失败
会取消 provider job，continuation 轮数有硬上限，streaming 合并时 usage 只算一次。

在 NS 中，这不应每段创建一个 Project Task。较合理的是：一个 logical model call/effect
包含多个 provider segment receipts，同一 task attempt 等待 continuation。只有业务语义已转成
独立可调度意图时，才创建 child task。

## 21. PydanticAI Harness 函数级审计

### 21.1 StepPersistence hook 的实际写入顺序

Harness 的一次 run 按下列 hook 写入：

```text
for_run                  derive run_id + parent_run_id
before_run               register RunRecord; append run_started
before_model_request     append model_request_started
after/on_error model     append model_request_completed/failed
before_tool_execute      record effect=started; append tool_call_started
after/on_error tool      read prior metadata; record completed/failed; append terminal event
after_node_run           at settled CallToolsNode, save provider-valid complete snapshot
after_run                fallback final snapshot if boundary snapshot is older; append completed
on_run_error             save newest history as complete/interrupted; append failed
```

`before_run` 对显式 `run_id` 重用 fail closed，避免 provider 重用 deterministic tool-call ID 时与上一次
effect ledger 碰撞：
[`_capability.py#L238-L299`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/step_persistence/_capability.py#L238-L299)。

`after_node_run`
[`#L506-L555`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/step_persistence/_capability.py#L506-L555)
在 settled `CallToolsNode` 将 `result.request` 折入 candidate history，通过 provider-valid gate 后立即
保存。这是硬 kill 也能保留的健康边界；`on_run_error`
[`#L343-L374`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/step_persistence/_capability.py#L343-L374)
只能救援能正常 unwind 经过 hook 的失败。

### 21.2 StepStore 的三路独立写入产生了明确 crash window

[`StepStore`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/step_persistence/_store.py#L132-L178)
将 `append_event`、`save_snapshot`、`record_tool_effect` 定义为三个独立方法。在
`before_tool_execute`
[`#L411-L436`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/step_persistence/_capability.py#L411-L436)
中，先写 effect started，再写 started event；终态路径也是先更新 effect，再写 terminal event。
在不提供 multi-record transaction 的 store 上，存在：

| crash 位置 | Store 可见状态 | 可否自动推断 |
|---|---|---|
| effect started 前 | 无 effect、无 event | 只能说 harness 未记录，不证明 tool 未被另一路调用 |
| effect started 后、started event 前 | unresolved effect，无对应 event | 必须当 uncertain |
| 外部 tool 完成后、terminal effect 前 | effect 仍 started | 必须当 uncertain，不得重放 |
| terminal effect 后、terminal event 前 | effect terminal，event trail 缺口 | 可以以 effect 为证据修复 projection，但需 audit |
| snapshot 后、run event 前 | resume point 存在，lifecycle event 滞后 | 需从多存储对账 |

这正是 NS 不应迁移 StepStore 的核心理由。现有 `EffectReceipt` 已可表达 `UNCERTAIN`，
`RunEventLog` 已有顺序和幂等；未来应由一个运行 transaction coordinator 原子写：

```text
effect request/terminal projection row
  + corresponding RunEvent
  + checkpoint high-watermark/current attempt mutation when applicable
```

如外部 effect 本身夹在两个 DB transaction 之间，这个不可消除的 window 就由 stable effect identity、
provider idempotency key 和 reconciliation 解决，而不是伪装成 exactly-once。

### 21.3 complete/interrupted 是读路径 gate，不只是状态标签

[`ContinuableSnapshot`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/step_persistence/_types.py#L86-L124)
将 complete 定义为所有 tool call 已匹配，interrupted 定义为存在未 settle 工作。默认
`latest_snapshot` 隐藏 interrupted，只有显式 opt-in 才能取到。

Tests
[`test_step_persistence.py#L1181-L1207`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/tests/step_persistence/test_step_persistence.py#L1181-L1207)
要求 dangling tool call 只能救成 interrupted，默认读返回 `None`；
[`#L1697-L1788`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/tests/step_persistence/test_step_persistence.py#L1697-L1788)
要求即使 interrupted 比 complete 新，默认也返回旧 complete。这是一个很重要的
read-path fail-closed 模式。

NS 应继续使用现有 `RunCheckpoint.resumability_status`，但必须让查询 API 把它当 gate：

- normal resume 只选 provider-valid + all-required-effects-known 的 checkpoint；
- recovery UI 可查看 blocked/interrupted checkpoint，但必须携带 reason 和 unresolved effects；
- operator 不能用一个 `include_interrupted=True` 就绕过 reconciliation；
- resume 前仍要重验 basis/artifact/schema/permission，结构 settled 不代表领域仍有效。

### 21.4 失败救援依赖一个易碎的 live-list invariant

[`_stash_live_history`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/pydantic_ai_harness/step_persistence/_capability.py#L301-L322)
将 pydantic-ai 内部 message list 的引用放入 ContextVar，假定 core 只 rebind 一次，之后全部
就地 append/slice mutation。注释明确说：如 core 中途再 rebind，失败快照会静默滞后。

这是 Harness 与一个具体 agent graph 实现紧耦合的证据。NS 不应从 leaf framework 的 mutable
message list 猜测 durable state。每个 settled boundary 应由 leaf adapter 主动返回版本化
`StepSettlement`，包含 history artifact ref/hash、provider-validity proof、usage delta、unresolved tool/effect IDs。

### 21.5 Harness 测试证据与局限

值得直接改写成 NS 验收的测试包括：

- [`test_step_persistence.py#L125-L180`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/tests/step_persistence/test_step_persistence.py#L125-L180)：
  unmatched call、orphan/duplicate/out-of-order result 都 provider-invalid；
- [`#L686-L817`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/tests/step_persistence/test_step_persistence.py#L686-L817)：
  interrupted run 从已完成 tool boundary resume，lineage/snapshot 可回放；
- [`#L1041-L1235`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/tests/step_persistence/test_step_persistence.py#L1041-L1235)：
  visible trail 不得伪造 continuation，model/output validation 失败可救最新有效 history，
  dangling tool 只能 interrupted；
- [`#L1333-L1393`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/tests/step_persistence/test_step_persistence.py#L1333-L1393)：
  after-node/after-run 必须经 provider-valid gate，无健康边界时才使用 final fallback；
- [`#L1487-L1532`](https://github.com/pydantic/pydantic-ai-harness/blob/5e180850511dec469cc50aa9853675a8031d1f19/tests/step_persistence/test_step_persistence.py#L1487-L1532)：
  terminal effect 不得丢失 started 阶段写入的 idempotency key/effect summary。

但 StepPersistence 不持久完整 agent capability state、workspace、节点内部 state，也没有 task claim/
lease/fence。它证明了 settled boundary 和 effect ledger 的必要性，不证明一个 agent graph 已经
可在任意中断点进行跨机器精确恢复。

## 22. 跨项目 crash-point 对照矩阵

下表是后续 Runtime ADR 最应直接使用的输入。它不按“正常流程”分类，而按最后一个
durable fact 分类。

| 中断点 | 崩溃后可信事实 | 不可做的推断 | 恢复动作 | 主要源码证据 |
|---|---|---|---|---|
| claim 前 | task 仍 ready/pending | worker 已拥有任务 | 重新计算 eligibility | Hermes claim |
| task owner 已改、attempt/event 未写 | 如允许分步，状态已不可判定 | “后续会补写” | 设计上禁止；三者同事务 | Hermes transaction |
| claim 已提交、worker 未启动 | running attempt 无 progress | effect 已发生 | lease/liveness 后 reclaim | Hermes reclaim |
| 旧 writer 被 cancel、新 claim 未提交 | 老 writer 可能已停，新 writer 无权 | 必有一个 owner | 禁止这个顺序 | OpenClaw writer claim |
| 新 writer claim 已提交、旧 writer 未停 | 新 owner durable，旧 owner 可运行 | 旧 callback 会自然停止 | 旧 fence 全部拒绝，继续 cancel | OpenClaw takeover tests |
| effect requested/started 前 | 无 NS 外部意图记录 | 外界一定未发生任何事 | 只能在 adapter 调用确认尚未发出时重试 | Harness window |
| effect requested 后、I/O 调用前 | durable intent 存在 | provider 已执行 | 安全发出相同 idempotency key | NS EffectReceipt pattern |
| provider 成功、receipt 未写 | effect 仍 requested/uncertain | 重试无害、provider 没做 | query/reconcile，不直接 replay | Harness + Temporal |
| terminal effect 已写、tool event 未写 | receipt 是结果证据，event trail 缺口 | task 状态已自动改变 | 同事务杜绝，或审计修复 projection | Harness split stores |
| tool batch 部分完成 | 存在 partial batch + effect receipts | 上下文仍 provider-valid | reconcile all calls，从上一 safe boundary 重建 | OpenHands properties |
| settled checkpoint 后、run complete 前 | safe resume point 存在，run lifecycle 未 terminal | 原 run 一定失败/成功 | supervisor 根据 attempt owner/liveness 判定 | Harness snapshots |
| interrupted checkpoint 后 | 不完整 frontier 可供调查 | 可直接 normal resume | 默认读路径隐藏，先 reconcile | Harness read gate |
| compaction summary 生成、publish 前 | summary artifact 可存在 | 新 View 已生效 | 丢弃或以 basis fence 重试 publish | Hermes/OpenClaw |
| compaction publish 后、provider cache 未失效 | Context View 已改 prefix | 旧 provider checkpoint 可重放 | 禁止分步；publish 包含 cache-generation bump | OpenClaw session projection |
| pause requested during I/O | 只有停在下一 step 的意图 | 当前 I/O 已取消 | 允许到 settled boundary，再 paused | OpenHands pause |
| interrupt requested during I/O | cancel intent 已发 | effect 一定没发生 | await cancellation receipt；不明则 uncertain | PydanticAI cancel |
| queue wait 期间 generation 变化 | 排队时的授权已过时 | 原 admission 仍有效 | 获得 lane 后重新验证/rebind-or-reject | OpenClaw lane |

这张表反映了一个统一原则：**恢复不是“从最新一行继续”，而是“从最新可证明的
safe boundary 继续，对边界之后的 effect frontier 先对账”**。

## 23. 直接映射到 NS 现有代码的设计裁决

### 23.1 现有 owner 应如何扩展

| NS 现有所有者 | 上游参考应进入的语义 | 不应进入的语义 |
|---|---|---|
| [`domain/runtime.py`](../src/novel_agent/domain/runtime.py#L21-L145) | typed runtime event、attempt/effect/checkpoint 合同 | SQL lease 操作、scheduler loop |
| [`services/event_log.py`](../src/novel_agent/services/event_log.py#L26-L159) | 同一 transaction 中的 event append primitive、replay oracle | Task eligibility、Context rendering |
| [`adapters/postgres/models.py`](../src/novel_agent/adapters/postgres/models.py#L45-L84) | Task/Attempt/Effect/Context projection rows（触发 Stage 后） | domain state transition rules |
| [`domain/writer_context.py`](../src/novel_agent/domain/writer_context.py#L278-L353) | Context View 的 basis/budget/evidence/information invariants | 对话 event store、task status |
| [`services/projection.py`](../src/novel_agent/services/projection.py#L49-L171) | 复用“build fully then publish”的投影模式 | 在 Canon projection service 中混入 runtime task queue |
| [`agents/runner.py`](../src/novel_agent/agents/runner.py#L45-L110) | leaf StepSettlement/typed adapter boundary | durable scheduler、project writer claim |
| [`services/model_gateway.py`](../src/novel_agent/services/model_gateway.py#L75-L223) | model effect identity、provider subattempt/usage receipt | task retry ownership、project mutation ownership |
| [`services/model_request_admission.py`](../src/novel_agent/services/model_request_admission.py#L117-L230) | endpoint request/KV capacity | task claim、session/project writer lease |

上表的核心是“扩展当前 owner”，不是为上游每个名词新建一个 service。特别是现有
[`WriterContextPackage`](../src/novel_agent/domain/writer_context.py#L321-L353)
已包含 Writer Seed 所需的 basis commit/snapshot、budget、evidence ledger 和 lineage。Stage 3 在其上
建立动态 Context View；Stage 4 建 consumer-specific PlannerContextPackage，但共享同一个 View/
compaction port。二者都不新建泛化 conversation context 或第二事件存储。

### 23.2 Stage 3 Writer Loop 的最小 Runtime 单元

当前 Stage 3 没有 durable Task Runtime caller，因此不新增 task/attempt 表或 supervisor。它需要的
是 Writer 自身多步生成、Memory 请求和 Context 压缩的可恢复边界：

- `RunEvent.payload` 中的 step/effect identity 必须稳定且版本化；
- candidate/artifact 必须携带 run/task/base commit lineage；
- `StructuredAgentRunner` 不应将 mutable internal message history 暴露成恢复合同；
- model/tool 的 external effect 不得仅靠 Python exception 表达结果；
- plan-conditioned WCP、ContextDelta 和 Writer output 必须保持同一 basis；
- context 预算削减不得破坏 mandatory/evidence/no-leak invariants；
- 不为未来 Temporal 引入第二套 history/store。

这些都可在不引入长期 scheduler 的前提下成立。

### 23.3 Stage 4 Planner Loop 与共享 Context projection/compaction

Stage 4 的 Planner 是第二个 Context consumer。它使用 inquiry-conditioned Need 和独立
PlannerContextPackage，但以下 Context Runtime 组件与 Stage 3 共享唯一实现：

| 组件/修改 | 当前 caller | 负责层 | 保护 invariant | 验收证据 |
|---|---|---|---|---|
| `WriterContextPackage`/`PlannerContextPackage` Seed lineage | Writer/Planner context compiler | domain | basis commit/snapshot/profile/consumer 可定位 | schema + unit/contract tests |
| `ContextProjection` service（名称候选） | next Writer/Planner model request builder | services | full replay = incremental；只发 LLM-safe events | property tests |
| `ContextProperty` 纯验证集 | projection + compactor | domain/services | tool batch、no-leak、evidence group 固定点 | upstream-derived regression corpus |
| `ContextCompacted` event payload schema | compaction publisher | domain/runtime | covered range/summary/kept boundary/basis 可审计 | replay + crash matrix |
| summary artifact | compactor | Artifact Store | 大内容不进 event，hash/provenance 稳定 | artifact verification |
| provider context generation bump | model adapter | services/adapters | prefix rewrite 后不重放旧 cache | integration regression |

Stage 4 **不需要** TaskRecord、TaskAttempt、cron 或 supervisor。Stage 3/4 共同把上下文从
“mutable messages”提升成“由现有事件和 artifact 可重建的安全投影”。

### 23.4 Stage 5 的最小实现单元：固定拓扑的 Task/Attempt

只当 Planner candidate acceptance→Writer→Editor/Curator→draft acceptance 的固定拓扑需要跨进程恢复
时，才增加：

| 组件/修改 | 当前 caller | 负责层 | 保护 invariant | 验收证据 |
|---|---|---|---|---|
| `TaskRecord/TaskAttempt/AttemptFence` | fixed topology runtime | domain | task 与 attempt 分离；worker 写强 fence | model/property tests |
| task/attempt PG rows | task command service | adapters/postgres | claim + attempt + event 同事务 | PG race integration |
| `evaluate_task_eligibility` | recompute/claim/resume/unblock | domain service 纯函数 | 同一 readiness 定义 | exhaustive transition table |
| task command service | runtime dispatcher/operator API | services | 唯一状态转换 chokepoint | contract + race tests |
| effect projection/reconciler | model/tool adapter + recovery | services | requested/terminal 与 event 同事务；uncertain 不重放 | crash-injection tests |
| checkpoint selector | resume command | services | 默认只选 safe，basis/permission revalidation | regression tests |

最小表集可以只有 `task_projection`、`task_attempt`、`effect_projection`；dependency 如固定拓扑能由
task payload 和 run event 确定推导，就不必立即建泛化 graph edge 表。只有第二个动态依赖 caller
出现后，才将 dependency 提升为独立合同。

### 23.5 Stage 5 在多 Worker/跨天 caller 成立后增加 lease/scheduler/supervisor

| 组件/修改 | 当前 caller | 负责层 | 保护 invariant | 验收证据 |
|---|---|---|---|---|
| durable claim lease + heartbeat | multi-worker dispatcher | services + PG adapter | expiry 仅触发 suspicion，fence 拒绝迟到写 | dual-worker/false-positive tests |
| recovery scanner | supervisor | services | liveness + effect reconciliation 先于 reclaim | kill/restart drills |
| scheduled fire projection | cron adapter | adapters/services | same fire once，advance policy atomic | cross-process race tests |
| deterministic pre-check | each scheduled job | domain/service pure gate | 没有工作时不启 LLM | no-model tests |
| control intent queue | UI/supervisor/subagent completion | runtime services | exact identity、lease/ack/release、sanitization | late callback tests |
| project writer admission | candidate/commit mutators | services + PG adapter | one current writer generation，takeover 先 CAS 后 cancel | takeover integration |

如 Stage 5 的实际运维数据显示 PostgreSQL queue/supervisor 已成为主要复杂度源，再对
Temporal 做 benchmark/ADR；不能倒过来因为 PydanticAI 有 integration 就提前引入。

### 23.6 现有 `RunEvent` 合同的缺口是 payload 类型，不是 event store

[`RunEventType`](../src/novel_agent/domain/runtime.py#L21-L71)
已有 task/model/tool/effect/checkpoint 基本类型，`RunEvent`
[`#L74-L110`](../src/novel_agent/domain/runtime.py#L74-L110)
已有 sequence/idempotency/schema/audit metadata。下一步不是换成 OpenHands Event 或 Harness StepEvent，
而是在真 caller 出现时为下列 event 建 typed payload：

```text
TaskClaimedPayload          attempt_id, claim_token digest, revision, lease, basis
TaskHeartbeatPayload        attempt_id, fence, observed progress, new expiry
TaskBlockedPayload          cause kind/fingerprint, recurrence, required actor/capability
AttemptReclaimedPayload     old fence, liveness evidence, unresolved effect ids, verdict
EffectRequestedPayload      effect identity, request identity, idempotency key, adapter version
EffectTerminalPayload       provider receipt/ref, status, reconciliation source
CheckpointCreatedPayload    structural validity, unresolved effects, basis hashes
ContextCompactedPayload     covered sequence range, summary ref/hash, kept boundary, view generation
ControlIntentPayload        exact intent id, source, mode, target generation, delivery lease
```

每个 payload 类型都应有自己的 schema version/upcaster 策略，不能只在 `payload: JsonValue` 里约定
一批无验证字典。但也不必一次把全部未来 payload 都写出；按 active Stage caller 逐个
增加，并保留 unknown-future-event 的 replay fail-closed 行为。

### 23.7 `RunEventLogRepository` 需要的不是大重写，而是事务内 primitive

现有 append 已经做了 PostgreSQL advisory lock、idempotency conflict、stream row `FOR UPDATE` 和单调
sequence。为了与 task/attempt/effect 原子更新，未来最小扩展形状应是：

```python
class RunEventLogRepository:
    def append(self, event: RunEvent) -> RunEvent:
        with session.begin():
            return self._append_in_session(session, event)

    def _append_in_session(self, session: Session, event: RunEvent) -> RunEvent:
        # same advisory lock, idempotency, sequence and row writes
        ...
```

`_append_in_session` 是 adapter/service 内部合作点，不应向 agent/tool 开放。Task command service 持有一个
Session，在同一 transaction 内做 state CAS、attempt/effect projection 和 event append。这是从 Hermes
事务不变量落到 NS 现有实现的最小改造，不是引入 unit-of-work framework 的理由。

## 24. 建议冻结的四个核心命令伪代码

这些伪代码不冻结 exact class/table 名，只冻结顺序和失败语义。

### 24.1 claim

```text
claim(task_id, worker_id, observed_revision, now):
  BEGIN
    task = SELECT task FOR UPDATE
    require task.revision == observed_revision
    eligibility = evaluate_task_eligibility(task, dependencies, basis, permissions, now)
    require eligibility == READY
    require task.current_attempt_id is null

    attempt = fresh Attempt(id, claim_token, lease_expiry, writer_generation?)
    CAS task READY/no-owner -> RUNNING/current_attempt=attempt.id/revision+1
    INSERT attempt
    APPEND task.claimed(event payload includes attempt fence and evaluated basis)
  COMMIT
  start worker; startup failure is a post-claim attempt failure, not transaction rollback
```

验收要点：双 worker 只有一个 commit；dependency/basis 在 candidate 发现后变化必须拒绝；
worker 启动失败不得删掉 claimed event。

### 24.2 effect execution

```text
execute_effect(fence, effect_request):
  BEGIN
    verify_current_fence(fence)
    upsert requested receipt by stable effect_identity + request_identity
    append effect.requested with same identity
  COMMIT

  provider_result = adapter.execute(idempotency_key=effect_identity, artifact_refs=...)

  BEGIN
    verify_current_fence_or_enter_reconciliation(fence, provider_result)
    write terminal receipt/result artifact ref
    append effect.completed|failed with provider receipt
  COMMIT

on process recovery with requested/no-terminal:
  mark/project uncertain
  query adapter by idempotency/provider request id
  append reconciled terminal OR block human; never blind replay
```

注意终态回写时 fence 可能已失效，但 provider 结果不应被丢失。实现上可将“记录一个旧
attempt 的 provider receipt”与“用该 receipt 推进当前 task”分开：前者可以写入 reconciliation
event，后者必须有 current fence。否则拒绝迟到写会反过来丢掉对账证据。

### 24.3 settled checkpoint / resume

```text
save_checkpoint(fence, settlement):
  verify current fence
  verify provider-structural fixed point
  verify information/no-leak properties
  verify state artifact hash and schema
  unresolved = effects requested without authoritative terminal receipt
  status = RESUMABLE if unresolved empty else BLOCKED
  save checkpoint at an existing event high-watermark

resume(task_id, checkpoint_id?):
  choose latest RESUMABLE by default; never latest-by-time alone
  replay events through checkpoint and compare projection hash
  verify base commit/snapshot/profile/artifacts/permissions again
  reconcile any effect frontier after checkpoint
  claim a fresh attempt/fence
  append run.resumed with source checkpoint and new attempt
```

验收要点：更新但 interrupted/blocked 的 checkpoint 不覆盖旧 safe checkpoint；projection replay 不一致
必须 fail closed；basis 已变时进 rebase/replan，不直接 resume。

### 24.4 compaction publish

```text
compact(run_id, view_generation, basis_event_position, budget):
  source = full replay or verified projection at basis position
  groups = build structural + domain atomic groups
  safe_indices = intersection(property.safe_indices for all properties)
  candidate = tiered compact at a safe index
  validate candidate to fixed point and final dispatch invariants
  if unchanged: return NO_OP

  write summary/details to Artifact Store, obtaining content hash
  BEGIN
    verify run high-watermark/view generation still matches basis policy
    append context.compacted(covered range, summary ref, kept boundary, hashes)
    bump context/provider-prefix generation atomically
  COMMIT
  return rebuilt Context View; never delete covered RunEvents
```

验收要点：同一 basis/input 产生同一 receipt/hash；旧 compactor 不覆盖新 View；summary model 失败
在 soft mode 下 no-op；任意 cut 都不破坏 provider/information properties。

## 25. 实施前的验收规格索引

未来的 `.agent/plan.md` 不应笼统写“参考 Hermes/OpenClaw”，而应从下表选当前 Stage
的具体规格。

### 25.1 Domain/model tests

- Task/Attempt identity 不可交换，attempt ID/token 不可复用。
- 所有 worker command 缺 AttemptFence 都在类型/API 边界失败。
- `evaluate_task_eligibility` 对每个 task/dependency/block/basis 组合给确定结果。
- FailureClass 到 retry owner/budget 的映射 exhaustive，新 enum 值不得默认 transient。
- Context safe index 是所有 properties 交集，fixed-point validation 幂等。
- compaction receipt 对同一 input deterministic，不包含 wall-clock 等非确定字段。

### 25.2 PostgreSQL contract/integration tests

- 并发 claim 只有一个 task/attempt/event 原子组提交。
- claim 事务内将 parent/basis 变化视为不可 claim，即使 ready projection 仍为 true。
- 旧 attempt heartbeat/complete/block/checkpoint 都无法改新 attempt。
- 旧 reclaim scanner 拿旧 token 无法清理新 owner。
- task/attempt/effect row 与 RunEvent 之间注入任意 SQL exception 后都不留半个事务。
- checkpoint position 不可超过 event high-watermark，这条现有保证必须保留。
- writer takeover 的 durable CAS 失败时老 writer 仍 current；CAS 成功后旧 append 失败。

### 25.3 Crash-injection tests

- 在第 22 节每一个中断点 kill process，从 DB/Artifact Store 重启而不读内存。
- provider 执行成功但 terminal receipt 丢失，必须 query/reconcile 而不是第二次调用。
- heartbeat 暂停但 worker/provider job 活着，supervisor 不启动第二 writer。
- cancel request 到达但 activity/provider 忽略，runtime 不 livelock，也不伪报 effect cancelled。
- compaction artifact 已写但 event 未 publish，原 View 仍可用；publish 后旧 provider prefix 失效。
- summary 生成与新 event append 竞争，旧 basis publish 必须 CAS 失败或按明确 append-safe policy 重基。

### 25.4 Projection/property tests

- 任意事件序列下，增量 Task/Run/Context projection 等于全量 replay。
- 分支/navigate/compaction 后重建失败不覆盖上一个健康 projection。
- 随机删除/摘要 message group，最终 View 无 orphan/duplicate tool result，无 partial batch/loop。
- 任意 compaction 不能将 hidden/future information 带进当前 scope，也不能丢 mandatory continuity
  constraints 而仍声称 READY。
- summary 和 raw events 可双向定位 covered range/provenance，但 summary 永远不修改 raw event。

### 25.5 Real-adapter tests

- 对每个 external adapter 证明 idempotency/query-status/cancel 的真能力，不从接口名推断。
- 测量 provider SDK 内建 retry，与 NS model/task retry 统一出具总 subattempt 和成本证据。
- 对大 prompt/result 走 Artifact Store 引用，验证不超 workflow/event/DB payload 限制。
- CPU-bound、I/O-bound、remote-job 三类 worker 分别测试 liveness policy，不共用一个假 heartbeat
  benchmark。

## 26. 深入研究后的迁移优先级修正

经过函数、事务、测试和 crash window 层的审计，最终顺序比“项目整体排名”更具体：

1. **先迁移 Hermes 的 claim/attempt/failure invariant，但修正 optional fencing、parent-terminal
   不一致和 PID-only 假设。**
2. **同时迁移 OpenHands 的 property-safe View 算法，并添加 NS 特有的 evidence/no-leak/
   basis properties。**
3. **使用 OpenClaw 定义 admission/takeover/control-intent 协议，但用 NS 的 PostgreSQL/
   RunEventLog 持久化，不复制其进程内 map。**
4. **从 Harness 只吸收 settled boundary、read-path gate 和 effect frontier；三套独立 store 语义
   反而作为 NS 应避免的 crash-window 教材。**
5. **PydanticAI/Temporal 保留为 leaf durable adapter；先冻结 activity payload、cancellation、
   heartbeat 和 retry ownership contract，达到引入门槛后再迁移代码。**

如果下一步是写架构 ADR，建议只写两个决策，不一次冻结整个平台：

- `Task/Attempt/Effect transaction and fencing contract`；
- `Event-derived Context View and compaction contract`。

前者使用第 17、18、20、21、22、24.1–24.3 节作证据；后者使用第 18.4、19、
21.3、22、24.4 节作证据。其他 scheduler/supervisor/Temporal 机制继续保持候选，直到
Stage caller 和失败数据证明需要。
