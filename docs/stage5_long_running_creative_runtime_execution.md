# Stage 5 Long-running Creative Runtime 详细执行文档

> 文档生命周期：`ACTIVE`
>
> 执行状态：`VERTICAL_ENGINEERING_READY / REAL_MODEL_GATES_PENDING`
>
> 更新日期：2026-08-13
>
> 阶段：Stage 5 — Long-running Creative Runtime
>
> 上位设计：`docs/stage5_long_running_creative_runtime_overall_design.md`
>
> 上位决定：ADR-0006、ADR-0007
>
> 关联技术基线：技术实施与选型设计 §28.7、正式执行规划 §6.5、Long-running Runtime 调研
> §23～26、InkOS 调研 §12/§14/§19～21、agentmemory 调研 §5/§7/§10
>
> 当前 caller：Stage 3 Writer candidate loop、Stage 4 Planner candidate port、现有 Commit/
> Projection/Freshness、operator long-run command
>
> responsible layer：Stage 5 application/runtime services；复用现有 leaf、Memory、Commit、Artifact、
> Event、Projection 和 Model Admission owners

## 1. 本文如何执行

本文把 Stage 5 拆成三个“准入层”，不是三个新的产品 Stage：

| 准入层 | 何时开发 | 本次是否可开始 | 完成含义 |
|---|---|---|---|
| A. Isolated Runtime Kernel | Stage 3 工程合同可用、Stage 4 窄 leaf contract 已可冻结 | 是 | 固定拓扑、Task/Attempt、接受/Commit、恢复和维护以真实 Stage 3 port + strict fake Planner 闭合 |
| B. Real Creative Loop Integration | Stage 3/4 工程候选与 Stage 5 A 层可审计，正式语义 Gate 可独立后补 | 是，形成非生产 integration candidate | 接真实 Planner/Writer，形成可执行 Plan→Write→Commit 闭环；Gate 未完成前保持 conditional |
| C. Evidence-triggered Operations/Evolution | multi-worker、外部 surface、长期样本等真实 caller 成立 | 否，按触发项启用 | lease/schedule/Hook/Skill evolution/Temporal 等逐项进入生产 Gate |

当前 A/B 层、Stage 3/4、可信 root materializer 和两路有界 dispatcher 已收敛成一个工程闭环
candidate；本轮只用确定性、离线和轻量集成测试证明组合机制，
不把缺失的 Stage 3/4 真实语义证据伪装成产品 PASS。若本地 `8002` 正被 Stage 2 benchmark 占用，
不得抢占、重启或改变其配置，真实 API 检查直接记为 deferred。C 每个触发包仍只有真实 caller 和
验收数据出现后才能授权。

Hierarchy migration on `ee8849a` is non-lookahead only: full-horizon cadence, BLOCKED plan
replacement with a new identity, Canon length gate, future-locked obligation fail-close, then
STORY/ARC_VOLUME PlanLevel. Lookahead, typed impact, and event-triggered multi-level replan stay
deferred.

## 2. 当前基线与分支规则

### 2.1 代码证据

截至 2026-08-10：

- main 的长期底座为 `RunEventLogRepository`、`RunCheckpointRepository`、`EffectReceipt`、
  `CommitService`、`ProjectionOutboxRepository`、`FreshnessGate` 和
  `ModelRequestAdmissionController`；
- Stage 3 在 `codex/stage3-writer-context-loop` / `bab4451` 完成工程实现，统一确定性证据为
  1893 passed、100% coverage、full pre-commit；真实模型语义和基础设施 Gate 仍为条件项；
- Stage 4 在 `codex/stage4-planner-context-loop` / `0dcf17a` 已形成 Planner Context Loop candidate
  并接入共享 Context Runtime；尚未形成正式 Stage 4 语义 PASS；
- 集成候选已接入 `Stage4PlanningLeafAdapter`、Stage 3 public Writer adapter、stable ready batch、
  `parallelism=1|2`、lookahead promotion/replan/supersede，以及非 fixture 的
  `PlanCandidateMaterializer`/`DraftCandidateMaterializer`；仅真实模型 Gate 待后续。

### 2.2 Isolated Kernel worktree

Stage 5 使用单独 clean worktree 和 `codex/stage5-long-running-runtime` 分支。起点必须是一个可审计的
clean integration base，至少绑定：

```text
accepted_stage2_base_commit
accepted_or_conditionally_accepted_stage3_commit
shared_context_runtime_commit
stage4_leaf_contract_version（schema/port only，不复制 Stage 4 implementation）
document_baseline_commit
```

如果 Stage 3 尚未正式并入 main，可以从其 clean engineering-complete commit 建 Stage 5 candidate，
但 manifest 必须标记 `stage3_gate=CONDITIONAL`，且该分支不能成为生产基线。不得从
`codex/stage4-planner-context-loop` 的活动 worktree 直接建 Stage 5，也不得把 Stage 4 的半成品通过
文件复制带入 Runtime。

### 2.3 文件所有权

Stage 5 不修改 Stage 3/4 的 Agent、Prompt、Skill、Context 和评价内部实现。建议文件边界：

```text
src/novel_agent/domain/runtime.py                 Task/Attempt/Fence/typed runtime payload
src/novel_agent/domain/creative_runtime.py        fixed topology request/result/acceptance/policy
src/novel_agent/ports/creative_runtime.py         Planner/Writer/materializer/reconciler leaf ports
src/novel_agent/services/runtime_commands.py      唯一 Task/Attempt/Effect command owner
src/novel_agent/services/creative_runtime.py      fixed topology application coordinator
src/novel_agent/services/runtime_recovery.py      checkpoint selection/effect reconciliation/recovery
src/novel_agent/services/runtime_acceptance.py    acceptance policy and trusted acceptance receipts
src/novel_agent/services/runtime_maintenance.py   maintenance command/pre-check/supervisor findings
src/novel_agent/adapters/postgres/runtime.py       PG projection/claim transaction adapter
src/novel_agent/runtime/creative_dispatcher.py    bounded dispatcher loop
src/novel_agent/api/                               仅已有 API surface 确有 caller 时加 command endpoints
src/novel_agent/cli/                               最小 run/status/pause/resume/reconcile 命令
schemas/stage5/                                    versioned public Stage 5 contracts
migrations/versions/                              runtime projection migration
scripts/export_stage5_schemas.py                   schema export
scripts/run_stage5_runtime_evaluation.py           final formal runner
```

如果实现发现某个建议文件只有一个很小 caller，应合并进相邻 owner，不能为了匹配文档机械创建空模块。
禁止新建 `scheduler_service` 微服务、通用 workflow package、第二 artifact/event/context store。

## 3. 最终调用链和 API

### 3.1 产品级入口

第一版只有一个顶层 application owner：

```python
class CreativeRuntimeService:
    def start(self, request: CreativeRunRequest) -> CreativeRunResult: ...
    def advance(self, command: CreativeRuntimeCommand) -> CreativeRunResult: ...
    def resume(self, command: ResumeCreativeRunCommand) -> CreativeRunResult: ...
```

`start()` 只创建 run 和第一批固定 Task，不在一个 HTTP 请求内无限写章。`advance()` 执行一个有界
Task/Attempt 或处理一个 operator/acceptance command；dispatcher 在外层有界调用。`resume()` 必须
经过 checkpoint/effect/basis 复验，不等同于继续 Python coroutine。

### 3.2 Leaf ports

```python
class PlanningLeafPort(Protocol):
    def run(self, request: PlanningLoopRequest) -> PlanningLoopResult: ...

class WritingLeafPort(Protocol):
    def run(self, request: WritingLoopRequest) -> WritingLoopResult: ...

class PlanCandidateMaterializer(Protocol):
    def materialize(
        self, accepted: AcceptedCandidateBinding
    ) -> tuple[CandidateChangeBundle, ValidationReport]: ...

class DraftCandidateMaterializer(Protocol):
    def materialize(
        self, accepted: AcceptedCandidateBinding
    ) -> tuple[CandidateChangeBundle, ValidationReport]: ...

class EffectStatusResolver(Protocol):
    def resolve(self, receipt: EffectReceipt) -> EffectResolution: ...
```

Stage 5 只依赖 Stage 3/4 public request/result/terminal/artifact lineage。adapter 不读取 agent 私有
messages、chain-of-thought、内部 reviewer state 或 mutable session。

### 3.3 Run result

每次 `advance()` 返回当前可理解的产品终态：

```text
PROGRESSED                 一个 durable task 已 settled，后继已计算
WAITING_PLAN_ACCEPTANCE    Plan candidate 完整，但未获接受
WAITING_DRAFT_ACCEPTANCE   Draft candidate 完整，但未获接受
WAITING_RETRY              已知安全重试条件/时间
RECOVERY_PENDING           effect/liveness 尚未对账
REVIEW_REQUIRED            需要作者或 operator 决策
BLOCKED                    basis/permission/validation 等硬阻断
COMPLETED                  当前 run 目标完成并 settled
CANCELLED                  显式取消 settled
```

内部 Task 状态不得直接冒充产品终态；result 必须附 run/task/attempt/basis/current commit、next legal
commands、artifact/receipt refs 和 typed reason。

## 4. A 层：Isolated Runtime Kernel 连续开发包

以下 A0～A9 是一个连续代码实现顺序，不是开发中测试门。OpenCode/开发者完成 A0～A9 全部代码和静态
自检后，统一进入 §12 测试。

### A0. 冻结 integration manifest 与 leaf contract

先新增 Stage 5 development manifest，记录：

```text
runtime_contract_version
stage2_base_commit / schema fingerprints
stage3_commit / stage3 contract and gate status
stage4_port/schema fingerprint / implementation status
commit/projection schema version
artifact/runtime/configuration/model fingerprints
feature admission flags
```

`feature admission flags` 第一版固定：

```text
real_stage4_adapter=false
multi_worker_lease=false
scheduled_fire=false
external_hook_ingress=false
skill_evolution=false
temporal_adapter=false
```

2026-08-13 contract note: `multi_worker_lease=false` in the old development manifest described A-layer
admission before a caller existed. The long-running production dispatcher now provides that caller and the
lease/heartbeat/suspicion/reclaim kernel is implemented. A production manifest update may set it true only
when migration `0009_stage5_attempt_leases` is applied and at least two processes exercise the same command
owner; the current offline evidence does not claim that deployment Gate.

生产代码不得通过环境变量悄悄打开未准入项；未知 flag 或 manifest mismatch fail closed。

### A1. Domain contract 与固定 topology reducer

扩展 `domain/runtime.py`：

- `TaskKind`、`TaskStatus`、`AttemptOutcome`、`FailureClass`；
- `TaskRecord`、`TaskAttempt`、`AttemptFence`、`TaskEligibility`；
- typed `TaskClaimedPayload`、`TaskAttemptSettledPayload`、`TaskBlockedPayload`、
  `EffectRequestedPayload`、`EffectTerminalPayload`、`CheckpointCreatedPayload`、
  `ControlIntentPayload`；
- RunEventType 只增加当前 A 层实际调用的事件，不一次添加所有未来 Hook/Skill enum。

新增 `domain/creative_runtime.py`：

- `CreativeRunRequest`、`CreativeRunPolicy`、`AutomationMode`；
- `CreativeTaskSpec`、固定 topology cursor/reducer；
- `CandidateKind`、`AcceptedCandidateBinding`、`AcceptanceCommand/Receipt`；
- `CreativeRuntimeCommand` discriminated union；
- `CreativeRunTerminal`、`CreativeRunResult`；
- Plan/Draft candidate basis 和 expected commit validators。

Reducer 必须是纯函数：给定 settled events/task projection/current commit/policy，返回合法 next task 或
terminal，不做 I/O、不调模型、不写数据库。固定转移只允许：

```text
PLAN_CANDIDATE
→ WAIT_PLAN_ACCEPTANCE
→ PLAN_COMMIT
→ PROJECTION_FRESHNESS
→ DRAFT_CANDIDATE
→ WAIT_DRAFT_ACCEPTANCE
→ DRAFT_COMMIT
→ PROJECTION_FRESHNESS
→ next DRAFT or PLAN or COMPLETED
```

任何跳过 acceptance、validation、Commit、freshness 的转移在 model validation 阶段失败。

### A2. Typed payload registry、schema 与 upcast 边界

现有 `RunEvent.payload: JsonValue` 保持外层兼容，但 Stage 5 写入必须经过 event-type→payload-model registry。
实现：

- serializer 在 append 前验证 payload model 和 schema version；
- projector replay 时 unknown Stage 5 event/version fail closed；
- 大 input/output 只保存 ArtifactRef/hash，不内嵌正文或完整 prompt；
- claim token 只持久化 digest，原 token 只存在受控 worker capability；
- error payload 只保存 typed code、bounded sanitized message 和 error artifact ref；
- export 到 `schemas/stage5/`，不回写 stage0～stage4 合同语义。

不要创建通用 event framework。registry 只服务当前 Stage 5 payload 并复用 Pydantic/schema export 方式。

### A3. PostgreSQL projection 与事务内 event append

对 `RunEventLogRepository` 做最小改造：

```python
def append(self, event: RunEvent) -> RunEvent:
    with self._session_factory() as session, session.begin():
        return self._append_in_session(session, event)

def _append_in_session(self, session: Session, event: RunEvent) -> RunEvent:
    # 保留现有 advisory lock、identity conflict、sequence 和 row write 语义
```

新增 migration 和三张投影表：

```text
runtime_task_projection
  PK task_id
  run_id/project_id/kind/status/revision/current_attempt_id
  basis_commit/basis_snapshot/policy_hash/permission_hash
  priority/scheduled_for/task_json/updated_at
  indexes: project+status+priority+scheduled_for, run_id

runtime_task_attempt
  PK attempt_id
  unique task_id+attempt_no
  worker_id/claim_digest/fence_generation
  claimed/started/ended
  outcome/failure_class/attempt_json

runtime_effect_projection
  PK effect_identity
  unique request_identity where contract requires
  run/task/attempt/status/provider_request_id/result_ref/effect_json
```

Projection row 的每次 material change 必须与对应 RunEvent 同 transaction。实现可从 event 重建/审计
projection；不得让数据库 trigger 隐式创造领域状态。

第一版固定依赖存在 `task_json` 和 reducer，不建 `runtime_dependency`。如 SQL 查询需要 readiness cache，
它只能是可重算字段，claim 内仍调用同一个 eligibility 函数。

### A4. RuntimeCommandService

实现唯一命令写 owner：

```text
create_run_and_initial_task
claim
mark_started
record_effect_requested
record_effect_terminal
save_checkpoint
settle_attempt
wait_for_input
submit_acceptance
pause / resume / cancel / retry / unblock
mark_recovery_pending / reconcile_effect
```

`claim()` 精确顺序：

```text
BEGIN
  lock task projection
  compare observed revision
  load current project commit / permission / fixed dependencies
  eligibility = evaluate_task_eligibility(...)
  require READY and no current attempt
  create fresh attempt_id + claim token/fence generation
  CAS task to RUNNING/current attempt/revision+1
  insert attempt
  append task.claimed with basis and fence digest
COMMIT
```

worker 启动发生在 commit 之后；启动失败形成该 Attempt 的 typed failure，不能删除 claimed event。

`settle_attempt()` 同时验证：current Attempt/fence、terminal artifact、effect frontier、checkpoint 状态、
task kind 对应 terminal，以及是否允许 reducer 生成后继。旧 fence 只能写 reconciliation observation，
不能改 task/current project。

### A5. Acceptance、materialize、Commit、Projection/Freshness

实现 `RuntimeAcceptanceService`：

- 验证 command identity、actor/policy、candidate hash/ref、candidate basis、expected current commit；
- manual/semi/auto 产生同形 receipt；
- auto policy 必须 Profile-pinned，默认 false；
- reject/revoke 保留 candidate，不删除 artifact；
- 重复相同命令幂等，identity 重用不同 payload 冲突。

实现固定 Commit adapter：

```text
accepted binding
→ correct materializer port
→ trusted ValidationReport
→ CommitRequest with expected base
→ CommitService.commit
→ typed commit event/receipt
→ enqueue or reuse existing ProjectionOutbox
→ wait/process Projection
→ FreshnessGate exact READY
```

隔离 A 层使用 strict deterministic materializer fixture，真实调用现有 `CommitService`、outbox 和
FreshnessGate。fixture 必须输出合法五 Root ChangeBundle，不能绕过 validation 或直接改 ProjectRow。

Plan/Draft Commit 失败分类：

| 结果 | Task 处理 |
|---|---|
| validation rejected | `BLOCKED` 或 `REVIEW_REQUIRED`，不调用 Commit |
| base conflict | `BLOCKED_REBASE_OR_REPLAN`，不自动改 base |
| Commit accepted | 保存 commit receipt，进入 Projection task |
| Projection failed | 只 retry Projection task |
| Freshness waiting | `WAITING_RETRY`，不启动 Writer/next chapter |
| Freshness blocked | `BLOCKED`，不得降级读取后继续写 |

### A6. Stage 3 adapter、CreativeRuntimeService 与 dispatcher

先实现真实 `WritingLeafPort` adapter：

- 只调用 Stage 3 public `WritingLoopRequest/Result` 和 candidate terminal；
- 绑定 accepted Plan commit、exact snapshot、Profile、information scope 和 WritingTaskContract；
- 保留 Stage 3 candidate/review/reconciliation/checkpoint lineage；
- 不读取 Writer/Editor/Observer 内部 mutable state；
- Stage 3 conditional Gate 和 executable identity 写入 Stage 5 manifest/result；
- 无模型 endpoint 时只允许运行 Stage 3 的正式离线/确定性合同路径，不把 scripted semantic score
  包装成真实生成质量。

该 adapter 使 A 层具有当前真实 Writer caller；B 层只在最终共同 accepted identity 上收敛和复验，
不再另造第二个 Writer adapter。

然后实现薄 `CreativeRuntimeService`：

实现薄 `CreativeRuntimeService`：

- 根据 reducer 创建一个 next durable task；
- 调用 command service claim；
- 按 task kind 调对应 leaf/materializer/projection/maintenance port；
- 将 leaf terminal 映射为 Task settlement；
- 输出 current result 和 legal commands；
- 每次 advance 有明确 max task/effect/model budget，不在 service 内无限循环。

实现有界 `CreativeDispatcher`：

```text
poll one eligible task
→ claim
→ execute one attempt
→ settle or typed fail
→ emit metrics
→ return to poll loop
```

A 层默认单 dispatcher。`FOR UPDATE SKIP LOCKED` 可以用于 claim query，但仍要由 command service 在事务
内重算 eligibility。dispatcher 不实现 domain transition、Commit 或 retry policy。

strict Planner fixture 和故障注入 Writer fixture 必须模拟：

- Plan/Draft candidate ready；
- review required；
- suspended transient；
- blocked basis/permission；
- deterministic artifact/ref lineage；
- requested→completed 和 requested→uncertain effect。

fixture 只在测试/isolated runner 注入；A 层真实 Writer 合同测试使用 Stage 3 adapter。正式 production
assembly 若 Planner 或 Writer 仍是 fixture 必须拒绝启动。

### A7. Recovery、control command 和 project writer lane

实现 `RuntimeRecoveryService`：

1. 加载 task/current attempt/current project commit；
2. 选择 latest `RESUMABLE` checkpoint，而非 latest by time；
3. replay event 到 checkpoint，比较 projection/context/state hash；
4. 重验 artifact、schema、basis、snapshot、Profile、permission；
5. 扫描 checkpoint 后 effect frontier；
6. requested/no-terminal effect 逐项调用 resolver；
7. unresolved effect 进入 `RECOVERY_PENDING/BLOCKED`；
8. 全部 settled 后创建 fresh Attempt/fence 并 append resumed event。

实现 control command：

- `pause`：阻止新 claim；当前安全点 checkpoint 后停，不能伪造 provider cancel；
- `resume`：按上述恢复链创建新 Attempt；
- `cancel`：区分 requested、acknowledged、effect-still-running 和 settled；
- `retry`：只对 failure-owner matrix 允许的已知安全失败创建新 Attempt；
- `unblock`：必须带 block cause fingerprint、actor 和已变化前置证据；
- `operator_reconcile/complete`：与 worker completion 分离，并完整审计。

project single-writer lane 第一版可以用明确的 PostgreSQL CAS projection 实现。取得新 generation 失败时
旧 generation 继续有效；取得成功后旧 generation 的所有 Commit/settle callback 被 fence 拒绝。

### A8. Maintenance 与最小 Supervisor

实现 `MaintenanceCommand`，而不是先做 cron platform。首批 kind：

```text
RECONCILE_PROJECTION_FRESHNESS
AUDIT_RUNTIME_PROJECTION
RECONCILE_UNCERTAIN_EFFECTS
VERIFY_ARTIFACT_REFERENCES
REBUILD_CONTEXT_PROJECTION
RUN_DELAYED_EVALUATION
AUDIT_STUCK_OR_POISON_TASKS
```

每种 maintenance 先运行 deterministic pre-check，返回 `NO_WORK` 时不建模型请求、不制造空 artifact。
派生重建复用现有 builder/repository；Task audit 只报告或发 typed repair command，不直接改 row。

`RuntimeSupervisor` 第一版只输出 `SupervisorFinding` 和可选 `ControlCommandProposal`：

- stuck duration；
- current Attempt/lease liveness suspicion；expiry 先发 typed recovery command，effect reconciliation
  完成前不 reclaim；
- repeated failure fingerprint / poison loop；
- exhausted retry/model/token budget；
- pending effect / projection mismatch；
- author/operator action required。

Supervisor 不接受 candidate、不 Commit、不自动晋升 Skill。

### A9. CLI、report 和观测

在现有 CLI 架构中增加最小命令：

```text
runtime start
runtime status
runtime advance
runtime accept-plan / reject-plan
runtime accept-draft / reject-draft
runtime pause / resume / cancel / retry
runtime reconcile
runtime maintenance
runtime export-report
```

所有 mutate 命令要求显式 project/run/task/observed revision 或 expected commit；不得使用“最新项目”隐式
选择 destructive/Canon-changing target。输出 secret/redacted payload 规则与现有日志一致。

统一报告至少包含：

- manifest 和代码/合同/配置/model/Skill fingerprints；
- task/attempt timeline 和每次 fence/retry owner；
- Plan/Draft candidate、acceptance、validation、Commit、Projection/Freshness lineage；
- checkpoint/effect frontier/recovery decision；
- model request/KV admission、token、latency、cost；
- manual/semi/auto stop points；
- failure/poison/stuck/maintenance findings；
- active/deferred feature flags；
- zero direct Canon bypass assertion。

OTel/metrics 是 observability，不成为 Task truth。报告由 events/artifacts/projections 生成，不维护另一套
mutable run state。

## 5. B 层：Stage 4 通过后的真实闭环集成

B0～B5 必须在 Stage 4 形成 accepted clean identity 后连续完成，再进入 §13 统一产品测试。不得在 A
层用 fake 的成功结果宣称 B 已完成。

### B0. Rebase 和合同差异审计

- 选择同一 accepted Stage 2、Stage 3、Stage 4 identity 形成 clean integration base；
- 对比 Stage 5 `PlanningLeafPort` 与真实 `PlanningContextLoopService` request/result/schema；
- 只修改 adapter/translation，不把 Stage 4 内部 inquiry/Need/Context/review 搬进 Runtime；
- 删除只为过渡而存在的重复 fake production path；
- 更新 manifest，`real_stage4_adapter=true`，其他未触发 flag 仍 false。

如果 Stage 4 terminal、basis 或 review receipt 无法无损映射，先回责任 Stage 修合同；不得在 Stage 5
用弱类型字典或忽略字段消化冲突。

### B1. 真实 Planner adapter

实现并验证：

```text
Creative PLAN task
→ PlanningLoopRequest(author intent/current commit/snapshot/horizon/policy)
→ Stage 4 inquiry-conditioned Planner loop
→ PlanningLoopResult
→ Stage 5 candidate/basis/review terminal mapping
```

映射规则：

- `PLAN_CANDIDATE_READY` → `WAITING_INPUT`；
- `REVIEW_REQUIRED/HUMAN_REQUIRED` → product `REVIEW_REQUIRED`；
- `SUSPENDED` → 按 typed failure 映射 waiting/recovery/blocked；
- `BLOCKED` → Stage 5 `BLOCKED`，保留 Stage 4 reason/receipt；
- degraded/fallback candidate 不得伪装正式 ready，除非 acceptance policy 显式允许且报告标记。

### B2. Plan materializer 与 PlanRoot Commit

根据真实 Stage 4 Plan candidate 定义最小 `PlanCandidateMaterializer`：

- 验证 accepted candidate/ref/hash/review receipt；
- 验证 author-supplied 与 planner-proposed provenance；
- 只把被接受 scope 写入 PlanRoot，保留未涉及项目；
- 构造 observed changes、RootManifest 和 passed ValidationReport；
- expected parent 只有当前 base commit；
- candidate basis 过期则 fail closed/replan；
- Commit 后必须 exact Projection/Freshness 才能生成 Writer task。

不得为 Stage 5 创建第六 Root、ChapterMemo Canon 或平行 Plan store。

实施结果（2026-08-12）：`PlanCandidateMaterializer` 读取被接受的 `PlanProposal`、独立 ACCEPT review
和对应 `PlannerExecutionResult`，按 item/deviation scope 合并既有 PlanRoot，保留未涉及节点并只生成
五 Root `RootManifest`。过期 basis、未晋升 lookahead、缺 review/receipt 或未解决 proposal 均 fail closed。

### B3. 真实 Writer adapter 收敛与 Draft Commit

把 A 层 Stage 3 adapter rebase 到最终共同 accepted identity，审计 request/result/schema 差异并删除
任何过渡 translation；不得并行创建第二个 Writer adapter。随后实现并验证完整真实链：

```text
accepted PlanRoot commit + exact snapshot
→ WritingTaskContract / Stage 2M Writer Memory
→ Stage 3 WritingLoopRequest
→ Writer/Editor/Observer/reconciliation
→ DRAFT_CANDIDATE_READY
→ explicit acceptance
→ DraftCandidateMaterializer
→ ChangeBundle/validation/Commit/Projection/Freshness
```

Writer task 的 accepted plan binding、base commit、snapshot、Profile 和 information scope 必须与 Plan Commit
后的 exact basis 一致。Stage 5 不重新生成 MemoryNeed 或 Context；它只组装请求并验证 receipt lineage。

Draft materializer 只消费最终 accepted draft 和 observer/reconciliation output。它不得把 Writer declaration
直接当 World truth；仍通过现有 trusted observed-change/Curator/validation 语义形成 ChangeBundle。

实施结果（2026-08-12）：Stage 5 把完整 `WritingLoopResult` 纳入候选 lineage；
`DraftCandidateMaterializer` 核对 accepted Plan/Profile/basis、最终 PASS、Observer、reconciliation 和
future-Plan impact，再通过既有 `SequentialTextRootService` 追加连续章节。未能形成 canonical World
operation 的弱提示不会被升级为事实；它们只作为 observation evidence 保留，TextRoot exact projection
则继续由冻结 Stage 2 检索 owner 消费。

### B4. Rolling policy

固定 rolling 决策输入：

- accepted Plan obligations/current focus；
- last committed chapter/scene；
- Plan horizon 和 expiration；
- deviation/replan proposal；
- author command；
- current budget/failure/maintenance status。

输出只允许：

```text
WRITE_NEXT
REPLAN
WAIT_AUTHOR
RUN_MAINTENANCE
COMPLETE_RUN
BLOCK
```

第一版一章一 Commit。同书不并行写 N 和 N+1。rolling policy 是 deterministic/versioned service；模型只
能通过 Stage 4 proposal 提供候选，不能直接选择长期调度动作。

#### B4.1 Lookahead candidate 与晋升

当 accepted Plan horizon 低于 policy 阈值且当前 `DRAFT_CANDIDATE` 已固定其章节合同时，Runtime 可以
复用 `PLAN_CANDIDATE` task kind 创建一个 `purpose=LOOKAHEAD` 的候选任务。输入必须绑定当前 exact
base commit/snapshot、不可修改的当前 Writer scope、目标 horizon 和 revalidation policy；不得创建
第二种 PlanningLeafPort 或旁路 Stage 4 inquiry/review。

lookahead 与 Writer 可在同一进程内并发执行，但 lookahead 成功只持久化 candidate、review receipt 和
basis，不生成 acceptance/commit 后继。当前 Draft Commit 与 Freshness 完成后执行：

```text
candidate basis + C[N] exact snapshot
→ deterministic affected-scope/deviation check
→ unchanged: promote to normal PLAN_ACCEPTANCE stop
→ affected: Stage 4 bounded revision/replan
→ stale/unsafe: supersede candidate and retain lineage
```

晋升/废弃必须由 typed Runtime command 记录；不得原地修改 candidate，也不得让旧 basis candidate
迟到提交。

#### B4.2 历史维护并发

Writer 运行时只允许读取 C[N-1] 或更早 accepted state 的派生维护、历史分析和 candidate-only
semantic maintenance。Derived rebuild/cache/index 操作可以直接完成；任何改变 Canon/Accepted
Semantic Memory/Plan 的维护只生成候选，等 foreground safe boundary 后经 Guardian/validation 和
project single-writer lane 提交。当前 Writer 所依赖的 basis 不得在其背后变化。

### B5. 正式 runtime assembly 和 evaluation runner

新增 production assembly：

- 禁止 fixture leaf/materializer；
- 所有 Planner/Writer/Reviewer/Editor/Observer/evaluator 请求经过现有 endpoint admission；
- 固定 Profile/Prompt/Skill/ToolPolicy/Schema/model hashes；
- 使用全新 project/run/DB/output identity；
- 一次 formal run 不在途中调参数、改 policy 或更换代码；
- 每章/任务/attempt/checkpoint report 独立可寻址；
- stop/repair 后的新运行必须记录 executable boundary 和 continuation policy。

formal runner 覆盖 manual、semi、auto 三种停点，但不允许 auto 跳过 safety gate。

#### B5.0 2026-08-13 production closure

G4/G5/G7/G8 已按纵向 Pilot 文档实现：

- `ProductionStage4InvocationFactory` 生成 exact-basis `CHAPTER_SET` 请求；
- `ProductionWritingRequestFactory` 生成 WritingTask、Stage 2M v2 WCP/EvidenceLedger、RecentProse 和 Stage 3
  request；
- `DRAFT_COMMIT` 经 `AtomicChapterSettlementAdapter` 调用 Stage 2W
  `LocalMemoryWriteWorkflow(CHAPTER_REVEAL_ATOMIC)`，Stage 5 只在原 fence 下登记 accepted commit；
- `VerticalCreativeRunner` 与 `runtime advance` 共用显式 production composition factory，不再内建 fake
  Planner/Writer；versioned `Stage5VerticalRunReport` 只在目标 Draft freshness 成功后冻结。

生产 admission 会核对 runtime、adapter、G4/G5 factory、materializer 和 settlement 的实际对象身份。仓库
不提供使用 benchmark frozen inputs 的伪 production factory；部署/Pilot composition 负责注入现有 Stage 2M
backend 和模型 endpoint。focused offline evidence 见纵向 Pilot §4.1/§6。8002 未调用。

#### B5.0.1 固定拓扑的原子后继与 Chapter Settlement 恢复

“当前 Task 成功”和“其唯一合法后继存在”属于同一个 Stage 5 reducer，不是两个可以分开提交的 service
调用。以下边界必须在现有 RuntimeCommand transaction 中原子完成：

```text
PLAN/DRAFT_CANDIDATE success  + acceptance Task
PLAN/DRAFT_ACCEPTANCE accept  + commit Task
PLAN_COMMIT accepted           + projection Task
PROJECTION_FRESHNESS success   + next foreground/background Task set
```

AUTO acceptance 仍然使用正常 acceptance command/receipt；区别只在 actor。Runner 恢复时若发现 pinned AUTO
policy 允许且 acceptance 仍为 `WAITING_INPUT`，使用 candidate identity 生成同一个稳定 command 继续，不增设
“auto accepted”旁路状态。

Stage 2W `CHAPTER_REVEAL_ATOMIC` 在 Stage 5 数据库事务之外提交五 Root，因此按现有 outer-effect 合同处理：

```text
claim DRAFT_COMMIT + writer fence
→ persist REQUESTED(effect identity scoped to the current Attempt)
→ call Stage 2W under stable idempotency key chapter-settlement.<acceptance>
→ persist terminal effect
→ atomically settle original Attempt + create Projection successor
```

Stage 5 effect identity 用于记录“哪一个 Attempt 执行过”，Stage 2W idempotency key 才表示跨 Attempt 不变的
业务请求；二者不得混为一行，否则一次确认未提交后的合法重试会撞上旧 Attempt 的 effect ownership。
若进程在 Stage 2W 返回前后崩溃，resolver 只查询相同 project/idempotency key 的 Commit receipt：accepted receipt
及其 manifest 必须证明 parent 正是 Task basis、当前 project commit 正是 resulting commit，才允许执行成功
reducer；无 receipt 或非权威状态保留 `RECOVERY_PENDING` 交给安全恢复，不猜测结果、不重发不同请求。Stage 5
只记录可恢复的 commit/receipt lineage，不伪造已经遗失的 Stage 2W validation/guardian 诊断 artifacts。

Draft Freshness 后的 lookahead 也不是 foreground 的硬依赖。可晋升/可 replan 的候选照常使用；仍在执行的
候选可以等待；死亡或缺失的 lookahead 必须回退到最新 exact snapshot 上的普通 rolling Planner task。

#### B5.1 Dependency-aware bounded dispatcher

扩展现有 `CreativeDispatcher`，不新增 scheduler service：

- 查询同一 run/project 最多两个 READY task，并保持稳定 priority/task-id 顺序；
- 只把 candidate leaf、read-only/derived maintenance 和非阻断评价作为并发 eligible；
- acceptance、materialize、Commit、Freshness、recovery/reconcile 和会改变 project writer generation 的
  task 保持串行；
- 在单 dispatcher 内先通过 command owner 独立 claim/fence，再用有界 async task group 执行；
- 一个 task 失败只结算自己的 Attempt，不取消已经独立 claim 的 sibling；
- 每个 leaf 的模型调用继续由共享 endpoint-global admission 决定实际请求并发度；
- 运行结束必须证明 task Attempt fence 和 model capacity lease 全部释放。

单项目 `runtime_parallelism=1|2` 保持不变：它表达 foreground Draft 与只读 lookahead 的依赖语义。全局
dispatcher admission 独立允许 `1/2/4/6/8`，只用于不同项目的 candidate leaf；同项目仍最多选择合法的
Draft+lookahead 两条 lane，Commit/Freshness/acceptance/recovery 仍串行。`endpoint_request_limit` 继续由共享
`ModelRequestAdmissionController` 的 request-count + KV-token capacity 决定，dispatcher 允许 8 个项目排队不
等于单卡会同时生成 8 个长请求。上线值从 2 起，4/6/8 只有真实 capacity evidence 后才配置，不在代码里
伪造吞吐结论。

## 6. C 层：按真实证据准入的扩展包

### C1. Multi-worker lease / heartbeat / reclaim

触发条件已经由 production long-running runner 和跨项目 dispatcher 成立。本轮实现：

- 以独立 migration/contract version 增加 `heartbeat_at`、`lease_expires_at` 和 liveness policy；
- claim 生成 lease，但 AttemptFence 永远必需；
- heartbeat 接受 matching attempt/claim token/fence；
- expiry 只进入 suspicion/recovery pending；
- liveness policy 区分 local process、remote job、provider request；
- reclaim 前 effect reconciliation；
- 旧 scanner/token 不能清理新 owner；
- project writer takeover 由新 Attempt claim/fence generation 完成；旧 callback/Commit 继续由现有 fence 拒绝。

具体流程固定为 `claim(lease) → mark_started → periodic heartbeat → safe phase/effect settlement`。Heartbeat
只续 matching current Attempt；scanner 看到 expiry 后先在同一事务把 task 置为 `RECOVERY_PENDING`。若当前
Attempt 存在 `REQUESTED/UNCERTAIN` effect，则只能交给 effect reconciler；effect frontier 已明确 settled 时，
reclaimer 才能结算旧 Attempt 为 `WAITING_RETRY`，随后普通 retry 创建新的 Attempt/token/fence。Lease expiry
不是任务失败证据，不扣 failure budget，也不直接重发 provider/Commit 请求。

不得只因表中已有 `lease_expires_at` 就宣称 multi-worker recovery 完成。

### C2. Scheduled fire

触发条件：确有周期 projection/freshness、delayed evaluation、backup/audit 或运行维护任务。新增最小
schedule projection，冻结：

- stable fire identity；
- same fire once；
- claim 与 next schedule advance 原子；
- overlap/missed fire/catch-up policy；
- deterministic no-work pre-check；
- long-running fire heartbeat/recovery。

不引入独立 scheduler 服务，除非现有 runtime dispatcher 无法满足并有规模证据。

### C3. External Hook / OperationalObservation

触发条件：真实外部 Agent、IDE、plugin/tool 不能直接写内部 RunEvent。实现窄 ingress、异步 observation
pipeline 和独立 operational index。请求路径只 identity/schema/allowlist/redaction/size/idempotent persist；
所有 LLM/embedding/index/graph/consolidation 异步。Hook failure 默认不阻塞主创作链，但必须计数、落 drop/
error receipt 并支持 replay。

内部组件永远不绕 HTTP Hook；OperationalObservation 不进入 Canon graph。

### C4. Controlled Experience / Skill evolution

触发条件：真实运行 corpus、重复质量目标、独立 held-out 数据和 promotion policy 已冻结。连续实现：

```text
sanitized run/evaluation observations
→ ExperienceCandidate
→ bounded SkillCandidate diff
→ held-out evaluator
→ regression/safety/cost comparison
→ explicit promotion receipt
→ immutable registry version + Profile pin
→ canary + rollback
```

LLM 可提议 Skill diff，可信代码决定合法范围；任何 active Skill 写入都只能由 promotion service 完成。
候选生成集和 held-out 集严格隔离。一次成功 run、访问热度、模型自评或无对照线上指标均不能晋升。

### C5. Temporal evaluation

只在上位设计所列五个 trigger 至少两个成立后：

1. 冻结同一 Plan→Write→Commit workload、failure injection 和成本指标；
2. 用当前 PostgreSQL Runtime 形成基线；
3. 只把 leaf I/O 包装为 stable-name activities；
4. 对比跨进程恢复、unknown effect、人工等待、运维复杂度和吞吐；
5. 独立 ADR 决定是否采用。

Temporal history 不替代 RunEvent，activity retry 不得与 Model/Task retry 重叠。

## 7. 失败分类与 retry owner

| Failure class | 唯一 retry owner | 默认 Task 结果 | 允许复用的 settled 层 |
|---|---|---|---|
| provider transport before effect | Model Gateway | leaf internal retry or suspended | input/context/checkpoint |
| model schema/semantic invalid | Stage 3/4 leaf bounded repair | review/blocked | earlier accepted plan/commit |
| context hard overflow/no safe cut | shared Context Runtime | blocked | raw events/artifacts |
| task worker startup | Stage 5 command service | waiting retry | claimed event/task input |
| uncertain external effect | Stage 5 reconciler/operator | recovery pending | all prior safe state |
| candidate rejected | acceptance owner | waiting/review/cancelled | candidate artifact |
| materializer/validation rejected | Stage 5 trusted service | blocked/review | accepted candidate |
| Commit CAS conflict | Stage 5 replan/rebase decision | blocked | candidate/review, not automatic commit |
| projection build failure | Projection owner | waiting retry | accepted Commit |
| freshness mismatch | Freshness owner/policy | waiting or blocked | accepted Commit |
| retry budget exhausted/poison | Supervisor/operator | failed/review required | full evidence trail |

新 FailureClass 必须在 exhaustive mapping 中选择 owner、budget、idempotency 和 terminal；不得默认 transient。

## 8. 权限与工具策略

| Actor | 可做 | 不可做 |
|---|---|---|
| Planner leaf | Plan candidate/review/memory action | PlanRoot/Commit/task row |
| Writer leaf | Draft candidate/editor/observer/memory action | TextRoot/WorldRoot/Commit/task row |
| Acceptance policy | 产生 versioned acceptance command | 修改 candidate/validation/Root |
| Runtime coordinator | 固定编排、typed command、调用 trusted ports | 自己检索、自己生成、直接 SQL 改 Root |
| Task command service | Task/Attempt/Effect projection + RunEvent 原子变化 | 候选内容判断、模型容量调度 |
| Materializer/validator | candidate→validated ChangeBundle | 绕过 acceptance/CAS |
| CommitService | 唯一 Canon CAS | 调 Planner/Writer、自动接受 |
| Supervisor | finding/command proposal/授权的 safe command | Commit、accept、Skill promotion |
| Skill promotion service | held-out 后 immutable version/pin | 原地改 active Skill、改 Canon |

ToolPolicy 中不存在 raw SQL、raw retrieval、Root mutation 或任意 scheduler mutation Tool。

## 9. 数据迁移与兼容

### 9.1 Migration

Migration 必须：

- 只新增 Stage 5 runtime projection 表/索引，不改历史 RunEvent/Commit 内容；
- 提供 upgrade/downgrade 的 schema 结构对称性；
- 不用默认值把历史 RunEvent 伪造成 Stage 5 task；
- 新表空启动，Stage 5 run 由 explicit create command 建 projection；
- foreign key/delete policy 不允许删除 Project 时留下跨项目 task/effect；
- claim/revision/status 约束和唯一键由 DB 辅助保护。

### 9.2 Event upcast

Stage 5 event payload version从 v1 开始。相同 version 不原地改变含义；兼容变化新增 version/upcaster，
未知未来 version 的 mutating resume fail closed。只读 audit 可以显示 unknown metadata，但不能据此推进 Task。

### 9.3 Stage 3/4 schema

Stage 5 通过 adapter 消费 stage3/stage4 schema，不重导出或改写它们。若 Stage 4 active branch 在 B 层前
改变 request/result，Stage 5 只更新 `schemas/stage5` 中的 binding/receipt，不覆盖 Stage 4 source schema。

## 10. 实现完成前的静态自检

A0～A9 或 B0～B5 的代码全部写完后、正式运行测试前，开发者先做一次不执行测试的完整代码阅读：

- 搜索所有 Project/Root/Commit 写入口，确认仍只有 `CommitService`；
- 搜索所有 task/attempt row update，确认只在 command repository/service；
- 搜索所有 production fixture/fake 注册，确认 production assembly fail closed；
- 检查所有 Stage 5 RunEvent 使用 typed payload registry；
- 检查所有 worker mutation 是否要求 AttemptFence；
- 检查 retry owner 是否唯一且预算有界；
- 检查大正文/prompt/output 是否只用 ArtifactRef；
- 检查 manual/semi/auto 是否只是停点策略；
- 检查 deferred feature flag 默认 false 且无法被未知环境值绕开；
- 检查没有新增第二 event/context/artifact/memory store。

发现问题直接继续改代码；这不是测试 Gate，也不暂停任务等待新的开发许可。

## 11. A 层全部开发完成后的统一测试

只有 A0～A9 全部实现并完成 §10 静态自检后，才一次性开始以下测试。测试发现同方向缺陷时在同一
工作包内修复，修复后重跑受影响层和最终全量；不要返回“某个小单测已过，等待下一阶段”。

### 11.1 Domain/unit

- Task/Attempt/AttemptFence identity、status transition、terminal contract；
- topology reducer 每个合法/非法转移；
- manual/semi/auto stop point parity；
- candidate basis/hash/acceptance identity validation；
- exhaustive FailureClass→retry owner/budget mapping；
- eligibility 的 dependency/basis/permission/schedule/cancel/poison 组合；
- old fence 对 effect/checkpoint/complete/commit 全拒绝；C1 启用后 heartbeat 同样拒绝；
- latest safe checkpoint selector；
- Supervisor finding 不产生越权 mutation；
- maintenance no-work 不发 model request。

### 11.2 Property/model

- 任意合法 event sequence：incremental Task projection = full replay；
- 任意 crash boundary：不存在 task row 已变但 event 未写或相反；
- retry 总创建 fresh Attempt/token/fence，旧 owner 不可复活；
- acceptance command 幂等，identity collision fail closed；
- topology 不可能从 candidate ready 直接跳过 acceptance/validation/Commit/freshness；
- fixed dependency derivation 对相同 input/events deterministic；
- report/task/result artifact lineage 可回溯且无跨项目 ref。

### 11.3 PostgreSQL contract/integration

- 双连接 claim race 只有一个 Task/Attempt/Event 原子组成功；
- ready 发现后 basis/dependency flip，claim 事务拒绝；
- SQL exception 注入在 projection/event 任一写点均全事务回滚；
- stale revision/claim digest/fence generation CAS 失败；
- old attempt late complete 不覆盖 new attempt；
- effect requested/terminal projection 与 event 原子；
- checkpoint high-watermark 现有保证保持；
- project writer takeover CAS 失败保留旧 owner，成功后旧 Commit 失败；
- migration upgrade/downgrade 和 empty-start。

### 11.4 Crash/recovery

在以下边界逐点 kill/restart，并只从 DB/Artifact/Event 恢复：

```text
task projection before/after event append
claim committed before worker start
effect requested before provider call
provider success before terminal receipt
candidate artifact written before task settlement
acceptance receipt before materialize
Commit accepted before Runtime event
Commit accepted before Projection
Projection built before publish
checkpoint artifact written before checkpoint event/save
pause/cancel delivered during provider call
writer takeover between CAS and old worker callback
```

每个 case 证明无 duplicate Canon/effect、无跳层、无丢 receipt，且恢复选择明确。

### 11.5 Isolated end-to-end

使用 strict fake Planner、真实 Stage 3 Writer adapter（离线/确定性合同路径）、故障注入 Writer fake，
以及 real Artifact/Event/Task/Commit/Projection/Freshness infrastructure：

1. Plan candidate → manual accept → Plan Commit → exact freshness；
2. Draft candidate → Editor/Observer fixture receipt → accept → Draft Commit → freshness；
3. semi 在两个 acceptance 停点等待；
4. auto 仅在 pinned policy 允许时生成 acceptance command；
5. Planner blocked、Writer suspended、validation rejected、Commit conflict、Projection retry、uncertain
   effect 各自局部恢复；
6. 连续 3 章固定 topology，不同 chapter 的 commit chain/basis/snapshot 一致；
7. 同书双 dispatcher 不产生双 Commit；
8. 跨项目 task 可排队，但模型容量仍由现有 admission owner 约束。

### 11.6 全仓质量

```text
Ruff lint + format
strict MyPy
deterministic Pytest with 100% branch coverage
schema export/golden/contract verification
Alembic migration checks
integration tests against isolated real PostgreSQL/Object Store/Search where required
pre-commit --all-files
git diff --check
```

A 层最终报告必须明确写 `ISOLATED_KERNEL_PASS` 或失败原因，同时写：

```text
real_stage4_adapter=false
creative_product_gate=NOT_RUN
production_activation=BLOCKED
```

## 12. B 层全部开发完成后的统一 Stage 5 产品测试

B0～B5 全部写完后才执行。A 层已通过的确定性回归仍全量保留，但不在 B 开发中逐包阻断。

### 12.1 真实 full chain

```text
Author intent
→ real Stage 4 Planner inquiry/memory/context/review
→ Plan candidate
→ explicit acceptance
→ real PlanRoot materialize/validate/Commit/Projection/Freshness
→ real Stage 2M Writer Memory
→ real Stage 3 Writer/Editor/Observer/reconciliation
→ Draft candidate
→ explicit acceptance
→ real ChangeBundle/Commit/Projection/Freshness
→ next chapter or rolling replan
```

至少覆盖 Project bootstrap 后的 story/arc/chapter-set/chapter/replan 组合，以及连续章节中上一章 committed
state 被下一章 Planner/Writer 正确读取。

### 12.2 模式 parity

- manual、semi、auto 使用同一 input/basis/Profile/Prompt/Skill/model；
- 差异仅为停止点/acceptance actor；
- 进入同一 Commit 的 candidate/materialization/validation/Root manifest 语义一致；
- auto 不能降低 Reviewer/Editor/Observer/validation/freshness Gate；
- endpoint safe-serial 与允许的 orchestration concurrency 语义一致。

### 12.2.1 两路并发与串行等价

开发中只做一次最小冒烟；整合完成后统一执行一次覆盖本节的确定性验收。不得为每个小改动反复跑隔离
套件，也不使用源码或上下文哈希作为正确性证明；正确性以显式版本、内容、依赖、事件和最终行为等价为准。

- 在相同 fixture/seed/config 下，serial 与 parallel 的 task graph、candidate refs、acceptance、Commit
  chain、projection/freshness 和 terminal 语义一致；
- 并发只改变事件完成时间，不改变每次模型请求的上下文内容与版本、prompt、evidence、sampling、token
  reserve、dependency ids 或 retry policy；
- overlap fixture 证明 Writer N 与 Planner lookahead、Writer 与历史维护至少有一次同时 in-flight；
- lookahead 在 C[N] 无影响时可晋升，有影响时 revision/replan，错误 project/run/basis 必须 fail closed；
- `max_parallelism=2` 下零 duplicate claim、零 duplicate Commit、零 sibling cancellation、零未释放
  Attempt/model lease；
- endpoint capacity=1 时两个 leaf 可并发准备并合法排队，结果仍与串行等价；capacity=2 仅在真实
  workload 标定无 OOM/truncation/timeout 后晋升。

### 12.3 局部恢复

- Planner transport/schema/review failure 不重跑 accepted Draft/Commit；
- Plan acceptance 等待跨进程恢复；
- Plan Commit conflict 进入 replan/review，不盲 rebase；
- Plan Projection 失败不重跑 Planner；
- Writer memory/context/model/editor/observer failure 只在 Stage 3 leaf 恢复；
- Draft acceptance 等待跨进程恢复；
- Draft Commit/Projection 失败不重新生成正文；
- rolling replan 保留 accepted history 和未履行 obligation。

### 12.4 长时运行

- 至少一次跨天或等价 kill/restart 演练；
- 多章连续 commit chain、snapshot/freshness、Context basis 和 RunEvent 一致；
- pause/resume/cancel/operator intervention 可审计；
- poison loop/failure budget 能停下，不无限消耗模型；
- maintenance 可独立修复 derived projection；
- 运行报告可按 run/task/attempt/chapter/commit 寻址。

### 12.5 质量与安全 Gate

- Stage 3/4 各自语义 Gate 仍成立，Stage 5 不用端到端平均分掩盖 leaf 退化；
- 零 future/private/跨项目 leakage；
- 零 direct Canon/PlanRoot bypass；
- 零 audit/review failed 仍 Commit；
- 零 uncertain effect blind replay；
- 零 same-project concurrent Canon writer；
- 所有 accepted content 有 candidate→acceptance→validation→Commit→freshness lineage；
- full repository quality、integration、real-model evaluation 和 pre-commit 全通过。

只有这些证据完成，Stage 5 才能从 `ISOLATED_KERNEL` 进入 `CREATIVE_RUNTIME_PRODUCT_PASS`。

## 13. C 层专项验收

### 13.1 Lease/reclaim

- dual worker race、heartbeat false positive、live provider job、dead worker、old scanner token；
- expiry 不自动重试；effect reconciliation 先于 reclaim；
- old writer late callback/Commit 全部 fence；
- multi-worker 与 single-worker 输出语义 parity。

### 13.2 Scheduled maintenance

- same fire once；claim/advance atomic；overlap/missed fire policy；
- no-work zero LLM；长任务 heartbeat/recovery；
- maintenance 不改 Canon/active Skill。

### 13.3 Hook/Observation

- seeded-secret redaction、跨项目身份、payload limit、idempotency、timeout/drop；
- 请求路径不调用 LLM/embedding/search/graph/Context/Commit；
- async worker crash/replay/rebuild；
- Operational index 与 Canon graph 完全隔离。

### 13.4 Skill evolution

- train/held-out 隔离、candidate diff bounded、权限不扩大；
- held-out failure active version unchanged；
- promotion receipt/Profile pin/canary/rollback；
- same run 不能生成候选又评价自己；
- 没有 Hook/Observation/访问热度直达 active Skill。

### 13.5 Temporal

- 与 PG Runtime 同一 frozen workload/failure matrix；
- retry/effect/heartbeat/cancellation 责任不重复；
- RunEvent/Commit 仍可独立审计和恢复；
- 只有量化收益覆盖迁移/运维成本才通过 ADR。

## 14. 验收报告状态词

只使用以下状态，避免“代码写完”等于产品 PASS：

| 状态 | 含义 |
|---|---|
| `ISOLATED_KERNEL_IMPLEMENTED` | A 层代码完成，统一测试尚未完成 |
| `ISOLATED_KERNEL_PASS` | A 层 deterministic/PG/crash/isolated-E2E Gate 通过 |
| `REAL_INTEGRATION_IMPLEMENTED` | B 层真实 adapter/full chain 代码完成，正式 Gate 尚未完成 |
| `CREATIVE_RUNTIME_PRODUCT_PASS` | B 层真实 Plan→Write→Commit/长期恢复/质量安全 Gate 通过 |
| `OPERATIONS_CAPABILITY_PASS:<name>` | 某个 C 层触发能力独立通过 |
| `BLOCKED:<reason>` | 外部基础、合同或证据阻断，说明 safe next action |

不得使用 `Stage 5 PASS` 表示只有 fake leaf、单章 smoke 或无故障运行通过。

## 15. 停止条件

开发中出现以下情况必须停止扩张并交回架构决策：

- 需要第二套 Event/Task/Context/Artifact/Commit truth；
- 固定 topology 无法表达已经出现的第二个真实业务流程；
- Stage 4 public contract 不能无损映射，只能读其内部 mutable state；
- 需要 Planner/Writer 直接修改 Canon 才能继续；
- effect adapter 无 stable identity 且无法查询/对账，却要求自动重试；
- 需要无真实 caller 的 queue/microservice/Temporal/HA；
- 自动 Skill promotion 无独立 held-out 数据；
- 为通过 Gate 需要放宽 information、review、validation、CAS、freshness 或 coverage；
- 同一语义将由两个 retry/scheduler/commit owner 同时负责。

普通实现缺陷、类型错误、测试失败、migration 问题或同方向局部修复不构成停止条件；在当前工作包内
持续修复并完成最终统一验证。

## 16. 完成交付清单

### 16.1 A 层

- Stage 5 manifest、domain/schema/migration；
- typed payload registry；
- Task/Attempt/Effect command/projection；
- fixed topology coordinator/dispatcher；
- acceptance + real Commit/Projection/Freshness adapter；
- settled recovery/control/project writer lane；
- maintenance/Supervisor findings；
- strict fake Planner/fault-injection Writer/materializer，以及真实 Stage 3 Writer adapter；
- CLI/report/formal isolated runner；
- unified unit/property/contract/PG/crash/E2E/full-quality evidence；
- `ISOLATED_KERNEL_PASS` report，明确真实 Stage 4/生产仍未启用。

### 16.2 B 层

- accepted Stage 3/4/Stage 2 clean integration identity；
- real Planner adapter，以及 accepted identity 上的 Writer adapter 收敛；
- real Plan/Draft materializers（已完成工程闭环）；
- rolling policy；
- production assembly；
- real model/multi-chapter/cross-process/failure matrix；
- complete candidate→acceptance→Commit→freshness lineage；
- `CREATIVE_RUNTIME_PRODUCT_PASS` report。

### 16.3 C 层

只交付被真实 caller 触发且单独通过验收的能力。未触发项在 manifest 中保持 false，不创建空壳 service。

## 17. 当前推荐的实际顺序

真实小说 Stage 2～5 纵向闭环的新增输入合同、八项已确认断点、修复顺序和
`20→5 / 60→5 / 100→10` Pilot 由
`docs/stage2_to_stage5_real_novel_vertical_pilot_execution.md` 统一负责。本文 B 层实现必须满足该文档，
不能再以 fake Planner、手工 WCP 或 TextRoot-only Draft commit 作为真实 full-chain 完成证据。

```text
现在（2026-08-12 用户授权）：
  冻结 Stage 2 executable/product semantics，不修改 Stage 2 owners
  以 Stage 3 bab4451、Stage 4 0dcf17a 和 Stage 5 A 层工作树形成隔离 integration candidate
  收敛唯一 shared Context Runtime 和真实 Planner/Writer leaf adapter
  完成可信 PlanRoot/TextRoot materializer 与 candidate-binding acceptance guard
  实现 B4 lookahead/background 两路有界并发与 basis revalidation
  完成 G4/G5 production factory、G7 atomic settlement、G8 vertical runner
  已运行 focused deterministic/offline closure tests；8002 忙时不抢占，真实 API deferred

下一步（纵向 Pilot 文档为唯一执行参考）：
  配置真实 ProductionRuntimeAssembly composition callable
  先运行 C20→25 AUTO Pilot，再按结果决定 C60→65 和有界并发对照

真实 Pilot 后：
  分别补 Stage 3 三方案、Stage 4 七模式和 Stage 5 多章真实 Gate
  Gate 全部完成后才形成 production clean identity

真实长期运行后：
  用两个真实 worker 验证已实现的 lease/heartbeat/reclaim 部署 Gate
  用实际 KV/latency/OOM 证据决定跨项目 dispatcher 是 2、4、6 还是 8
  scheduled fire / Hook / Skill evolution / Temporal 仍按各自 trigger 单独准入
```

这样先证明真实代码组合、固定事务拓扑和有界并发，而不把当前工程候选误报成语义/生产 PASS，也不
提前制造 Hook、分布式调度和自动 Skill 平台。

## 18. GPU6 修复后 Writer 采样、seed 与 thinking A/B 操作指南

### 18.1 目的、边界和本次结论

本节是 2026-08-11 起 GPU6 长篇小说复测的唯一操作入口。它回答四个问题：

1. 严谨、结构化的 agent 与创造性 Writer 应怎样分开采样；
2. 单卡 thinking / non-thinking 怎样做公平 A/B；
3. 三卡候选怎样避免因为相同 seed 生成完全相同的小说；
4. 怎样用当前已经本地构建的 InkOS CLI 初始化约 300 章目标、再串行生成并观察前 30 章。

本节不授权安装、升级或重启任何环境。GPU6 已由人工确认修复，本轮只使用现有服务
`http://127.0.0.1:8005/v1` 和仓库内已经构建的 InkOS 1.7.2。预检失败时暂停当前 attempt、记录并归因，不得用
`pip`、`npm`、`pnpm install`、容器拉取或换模型掩盖问题。

本节也不把 InkOS 直接变成本项目的生产架构。它仍是隔离的参考实现和模型压力测试工具；输出只能作为
candidate/evidence，不能直接写入本项目 Canon。

本轮用户提供的《余烬九序》完整大纲与初始设定已按内容不变、换行统一为 UTF-8/LF 的方式保存为私有 benchmark 输入：
`benchmarks/private/yujin_jiuxu_longform_v0.1/author_brief.md`。该目录受 `.gitignore` 保护，不进入版本库；
后续命令统一读取这个仓库内稳定路径，不再依赖 Codex attachment 临时路径。原设定写的是 800 章目标，
本轮 `target-chapters=300` 是为了缩短长跑复现范围，不代表修改作者原始设定；结果报告必须保留这一差异。

### 18.2 参数所有权：确定性角色低温，Writer 高温

采样参数必须属于“具体 role + 具体 attempt”，不能成为所有 agent 共用的一组全局默认值。

| 角色/阶段 | 基线模式 | temperature | top_p | top_k | min_p | presence penalty | repetition penalty | seed 规则 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Architect/Planner | non-thinking | 0.2 | 0.80 | 20 | 0.0 | 0.0 | 1.0 | 同一输入固定 |
| Reviewer/Validator/Observer/Settler | non-thinking | 0.1～0.2 | 0.80 | 20 | 0.0 | 0.0 | 1.0 | 同一输入固定 |
| Reviser/Editor | non-thinking | 0.3 | 0.85 | 20 | 0.0 | 0.0 | 1.0 | 同一修订任务固定 |
| **Writer 正文** | A/B 指定 | **0.8** | **0.95** | 20 | 0.0 | 0.0 | 1.0 | 每个 candidate lane 不同 |

这里的低温不是 `temperature=0`。结构化角色依靠 schema 校验、事实约束和确定性 retry 获得严谨性；
完全 greedy 曾经出现过重复退化，且不利于验证 seed/采样链路是否真的生效。Writer 的 `0.8/0.95` 是本项目
创造性基线，不是声称照抄 Qwen 官方通用基线。Qwen3.6 官方当前给出的参考是 thinking 通用任务
`1.0/0.95`、精确任务 `0.6/0.95`、non-thinking `0.7/0.8`；本项目按角色收窄参数，并通过自己的长篇门禁
验收，而不是把厂商参数直接当作质量结论。官方依据见
[Qwen3.6-27B model card](https://huggingface.co/Qwen/Qwen3.6-27B)。

基线阶段固定 `presence_penalty=0.0` 和 `repetition_penalty=1.0`，避免在 thinking A/B 中同时引入额外变量。
若仍有短语复读，只能在完成本基线后另开单变量实验测试 `repetition_penalty=1.05`，不得回写污染本轮结果。

### 18.3 seed：可复现不等于所有卡使用同一个值

同一项目、章节、prompt 和配置先得到稳定 `base_seed`：

```text
base_seed = sha256(project_id + chapter_no + prompt_hash + config_hash)[0:8] & 0x7fffffff
lane_0_seed = base_seed
lane_1_seed = (base_seed + 1009) & 0x7fffffff
lane_2_seed = (base_seed + 2017) & 0x7fffffff
```

执行时必须同时满足：

- 同一 lane 内的 non-thinking / thinking A/B 使用同一个 seed；
- 同一 attempt 的 retry 复用原 seed，不因 HTTP 重试而换 seed；
- 三张卡/三个 candidate lane 分别使用上述三个不同 seed；
- seed、prompt hash、context hash、模型 revision、采样参数和 endpoint identity 一并落证据；
- 三个 lane 只并行产生候选，不能并发提交同一本书的 canonical chapter；
- 三个正文 SHA-256 完全相同，直接判定 diversity gate 失败，而不是任选一个继续。

示例只用于人工试跑：若本轮固定 `base_seed=24081106`，则三个 lane 可使用 `24081106`、`24082115`、
`24083123`。正式 runner 必须按上式计算并持久化，不能长期硬编码这三个示例值。

### 18.4 thinking A/B 的唯一变量和预算合同

Qwen3.6 不使用 `/think`、`/nothink` 文本软开关。non-thinking 必须显式传：

```json
{"chat_template_kwargs":{"enable_thinking":false}}
```

thinking 必须显式传：

```json
{
  "chat_template_kwargs":{"enable_thinking":true},
  "thinking_token_budget":1024,
  "include_reasoning":true
}
```

当前 vLLM 文档明确说明：未传 `thinking_token_budget` 时，reasoning 除总输出上限外没有独立上限；达到预算
后 vLLM 会强制生成 reasoning 结束边界。因此“只把 `max_tokens` 调小”不算受控 thinking。依据见
[vLLM Reasoning Outputs / Thinking Budget Control](https://docs.vllm.ai/en/stable/features/reasoning_outputs/)。

公平性合同如下：

| 阶段 | non-thinking 总输出上限 | thinking reasoning 预算 | thinking 总输出上限 | 目的 |
|---|---:|---:|---:|---|
| 直接 API smoke 初始值 | 4096 | 512 | 4608 | 用完整约 3000 字正文确认开关、闭合和正文返回 |
| 单章正式 A/B | 8192 | 1024 | 9216 | 为两组保留相同的 8192 正文空间 |

两组必须保持 model、prompt、context、seed、Writer 采样参数和正文目标完全一致；thinking 组只增加 thinking
开关、独立预算以及相等的额外总 token 空间。响应兼容读取：

输出上限是 admission/安全边界，不是期望模型必须用满的长度。若完整正文目标变化，先按正文目标估算
`body_output_budget`，再令 thinking 总上限等于 `body_output_budget + thinking_token_budget`。发生可归因的硬截断
时允许提高 `body_output_budget`；为保持公平，A/B 两侧必须使用新的相同正文空间重新配对，旧响应保留为前一
attempt 的诊断证据。

```text
reasoning = message.reasoning ?? message.reasoning_content ?? ""
content   = message.content ?? ""
```

本地 vLLM 构建可能返回 `message.reasoning`，而部分 OpenAI-compatible 实现返回
`message.reasoning_content`。原始 reasoning 不进入 Git、不进入长期小说记忆，也不成为架构事实；证据默认只保存
reasoning token 数、SHA-256、是否正常闭合、`finish_reason` 和最终正文。仅在隔离诊断且目录被忽略时短暂保留
raw response。

### 18.5 当前 InkOS 1.7.2 的代码事实与限制

开始操作前必须知道当前参考代码并没有完整的 role-scoped 采样控制：

- `packages/core/src/agents/writer.ts` 的正文温度是
  `input.temperatureOverride ?? 0.7`，正文 `maxTokens` 按目标字数计算并限制在 `4096～16384`；
- 同文件 Observer 固定 `temperature=0.5, maxTokens=4096`，Settler 固定
  `temperature=0.3, maxTokens=8192`；
- `packages/core/src/agents/planner.ts` 当前 Planner 调用温度为 `0.7`；
- CLI 的 `write next`/`auto` 没有 temperature 选项，因正文调用显式传 `0.7`，仅设置
  `INKOS_LLM_TEMPERATURE=0.8` **不会**把 Writer 正文改为 0.8；
- `packages/core/src/utils/effective-llm-config.ts` 会把 `INKOS_LLM_EXTRA_*` 解析后放进 OpenAI 请求
  `extra`，数字、布尔和 JSON 对象都可解析；
- `INKOS_LLM_THINKING_BUDGET` 只写入 InkOS 配置并打开内部 `piModel.reasoning` 标记，当前 custom OpenAI
  transport 不会自动把它映射成 vLLM 的 `thinking_token_budget`；
- `packages/core/src/llm/provider.ts` 的 non-stream parser 能读取 `message.content` 和
  `message.reasoning_content`，但不能读取当前本地 vLLM 可能返回的 `message.reasoning`。受限 thinking 正常闭合
  后正文应在 `content`；若只有 `reasoning` 没有 `content`，InkOS 会按空响应失败，这是正确的 fail-closed 行为。

因此本轮分成两种证据等级：

1. **直接 API A/B**：可以严格执行 Writer `0.8/0.95` 和同 seed 单变量比较；
2. **当前 InkOS CLI 长跑诊断**：可以验证全链路是否还复读、能否连续生成，但 Writer 实际仍是代码中的
   `0.7`，thinking extra 也会作用于该进程的所有模型调用。它不得标记为“role-scoped Writer A/B PASS”。

要取得正式 role-scoped PASS，后续实现必须让 Stage 3 Writer policy 成为温度、top-p、seed、thinking mode 和
thinking budget 的 owner，由具体调用显式传入；CLI/env 只负责测试覆盖，不再用一组全局 extra 控制整条管线。
这是对现有 owner 的最小扩展，不新建采样服务。

### 18.6 零安装预检

所有命令从仓库根目录执行。先定义路径，不修改全局配置：

```bash
cd /home/cuihengjia/agent/novel/NS
set -euo pipefail
INKOS_SRC=/home/cuihengjia/agent/novel/NS/inkos_lab/ref/inkos
INKOS_CLI=/home/cuihengjia/agent/novel/NS/inkos_lab/ref/inkos/packages/cli/dist/index.js
GPU6_BASE=http://127.0.0.1:8005/v1
BRIEF=/home/cuihengjia/agent/novel/NS/benchmarks/private/yujin_jiuxu_longform_v0.1/author_brief.md

test -f "$INKOS_CLI"
test -f "$BRIEF"
node "$INKOS_CLI" --version
curl --fail --silent --show-error "$GPU6_BASE/models"
```

预期 InkOS 版本为 `1.7.2`，`/models` 返回 HTTP 200。把 `/models` 返回的精确 `id` 复制为
`GPU6_MODEL`，不要猜测 service 暴露名：

```bash
GPU6_MODEL='把 /v1/models 返回的精确 id 填在这里'
test -n "$GPU6_MODEL"
```

同时从已启动服务清单/日志确认本实例是 GPU6、`max_model_len=131072`、启用了 Qwen reasoning parser，且
MTP/KV 配置与本轮 manifest 一致。这里只检查，不重启。任一身份暂时无法确认时先标记
`NEEDS_REPAIR:ENDPOINT_IDENTITY_UNPROVEN`，继续使用安全的只读进程、参数和日志检查；只有可用只读证据已耗尽
仍无法证明身份时才升级为 `BLOCKED`。

上下文 admission 必须满足：

```text
estimated_prompt_tokens + requested_total_output_tokens + 4096 <= 131072
```

不要因为“128K 拉满”就让输入占满 128K；输出和强制 closing 也属于同一上下文窗口。

### 18.7 直接 API smoke：先证实模型，再碰 InkOS 状态

先创建全新的证据目录。若目录已存在就更换 `RUN_ID`，不得覆盖旧证据：

```bash
RUN_ID=gpu6-qwen36-ab-20260811-01
RUN_ROOT=/home/cuihengjia/agent/novel/NS/inkos_lab/runs/$RUN_ID
test ! -e "$RUN_ROOT" || { echo "RUN_ROOT already exists; choose a new RUN_ID"; exit 1; }
mkdir -p "$RUN_ROOT"/api-smoke "$RUN_ROOT"/inkos-no-thinking "$RUN_ROOT"/inkos-thinking
```

两个请求使用同一段真实章节 prompt 和同一个 `seed=24081106`。下面只给参数骨架；执行者把同一份 system/user
内容分别写进两个请求，不得为了某一组“优化提示词”：

```json
{
  "model": "<GPU6_MODEL>",
  "messages": [
    {"role": "system", "content": "你是长篇小说正文 Writer。严格保持给定事实、视角和章纲，只输出本章正文。"},
    {"role": "user", "content": "<同一份《余烬九序》章纲、已知事实和本章目标>"}
  ],
  "stream": false,
  "temperature": 0.8,
  "top_p": 0.95,
  "top_k": 20,
  "min_p": 0.0,
  "presence_penalty": 0.0,
  "repetition_penalty": 1.0,
  "seed": 24081106,
  "max_tokens": 4096,
  "chat_template_kwargs": {"enable_thinking": false}
}
```

thinking 请求只改以下字段：

```json
{
  "max_tokens": 4608,
  "thinking_token_budget": 512,
  "include_reasoning": true,
  "chat_template_kwargs": {"enable_thinking": true}
}
```

请求发送到 `$GPU6_BASE/chat/completions`，原始 JSON 分别保存为
`api-smoke/no-thinking.json` 和 `api-smoke/thinking.json`。smoke 通过条件：

- 两个 HTTP 状态都是 200；
- 两个 `message.content` 都非空，正文无单句/单段机械循环；
- non-thinking 的 reasoning 为空；
- thinking 的 reasoning 非空，预算计数不超过 `512 + 16` 个 closing 容差 token；
- 两组都不是“reasoning 用完后正文为空”；
- `finish_reason=length` 且句子被硬截断时，本 attempt 不通过，但先分类为
  `RETRYABLE:OUTPUT_BUDGET_MISMATCH`，不得直接把模型或整个实验标记为永久 BLOCKED；
- thinking 字段返回 400/422 时，本 attempt 不通过；应先核对 request schema、服务能力和字段透传，不能简单
  删除预算字段后把无上限 thinking 当作成功。

#### 18.7.1 受控重试和解决问题的规则

门禁约束的是“坏输出不得进入下一阶段或污染状态”，不是禁止执行者解决问题。任何失败先暂停当前 attempt，
保留原始证据并归因，然后按下面顺序自主处理：

1. **瞬时传输失败**：429、502、503、连接重置和暂时超时，可以在 prompt、seed、采样参数和 request body
   完全不变的前提下退避重试；记录 `transport_retry_index`，不创建伪造的新内容样本。
2. **输出预算不匹配**：`finish_reason=length` 且正文仍在正常推进、没有复读或语义崩坏时，创建新
   `attempt_id`，提高两侧相同的正文预算并重新跑完整 A/B 对。推荐阶梯为 `4096 → 8192 → 16384`，但只走到
   完整闭合所需的最小一档，并始终满足 128K admission。
3. **thinking 预算不匹配**：reasoning 正常但正文空间被挤压时，保持独立 thinking 预算不变，只同步增加两侧
   `body_output_budget`；reasoning 经常撞上上限且明显未闭合时，才另建 config version 调整 thinking budget。
4. **提示合同问题**：模型明显忽略目标长度、输出格式或停止条件时，可以修正共同 prompt；修正后 prompt hash
   改变，必须把 A/B 两侧和三个 seed 全部作为新 experiment version 重跑，不能与旧版本横向拼接。
5. **采样/内容质量问题**：复读、事实冲突或风格退化允许做单变量修复实验，例如只改 repetition penalty 或
   Writer prompt。每次只改一个可解释变量，保留前后证据；修复有效后用统一配置重跑完整矩阵。
6. **InkOS 状态问题**：失败输出不得 settle/commit；若工具已推进 chapter number 或 truth files，从最近的
   accepted checkpoint 恢复到新 run/attempt，再继续验证，而不是停止整个研究任务。

每次重试都必须记录 `parent_attempt_id`、failure class、假设、唯一改动、预期信号和结果。允许持续修复，只要
仍有明确、可验证且不会污染 Canon 的安全动作；不得为了“过门”同时盲调多个参数或覆盖旧证据。

本轮已产生的 `gpu6-qwen36-ab-20260811-01/api-smoke` 应按以下决定继续：

- attempt-01（2048/2560）原样保留，重分类为
  `INCONCLUSIVE:NON_THINKING_OUTPUT_BUDGET_MISMATCH`，它证明 transport、seed、thinking 开关和 512 预算基本
  可用，但不能用于正文质量 A/B 结论；
- 建立 attempt-02，复用同一 `prompt.md`、`seed=24081106` 和所有采样参数，将 non-thinking 改为 4096、
  thinking 改为 4608/512，**两侧都重跑**；
- attempt-02 两侧完整闭合即可进入 §18.8；若仍有一侧只因 length 截断，按本节升级到 8192/8704 后配对重跑；
- 不需要重启 GPU6，也不需要安装任何依赖。

### 18.8 三 seed 配对矩阵

API smoke 通过后再跑 3 个 seed × 2 个 mode，共 6 次调用：

| lane | endpoint | seed | A | B |
|---|---|---:|---|---|
| 0 | GPU6 本轮先单卡串行 | base | non-thinking | thinking 1024 |
| 1 | GPU6 本轮先单卡串行 | base+1009 | non-thinking | thinking 1024 |
| 2 | GPU6 本轮先单卡串行 | base+2017 | non-thinking | thinking 1024 |

本轮先在已修复 GPU6 串行完成矩阵，排除跨服务配置差异。之后若改为三卡并行，每个 endpoint 都要有独立
identity manifest，并保持模型 revision、上下文长度、MTP、量化/KV 和服务参数相同；只让 endpoint 与 lane
seed 不同。

每个 A/B 对使用正式上限 `8192` 与 `1024+8192=9216`，记录：请求/响应 SHA、首 token 延迟、总耗时、
prompt/completion/reasoning token、finish reason、正文字符数、段落重复、12-gram 最大重复次数、事实违约、
章纲完成度。通过门槛：

- 6/6 正文非空，无 Unicode replacement character；
- 任一正文不得出现完全相同的非对白段落重复，12-gram 重复次数不得大于 3；
- 三个 seed 的正文 SHA 必须互不相同，任意两篇 5-gram Jaccard 相似度应小于 0.85；
- 两种 mode 都必须保留相同核心事实、人物状态、POV 和本章结果；
- 目标长度落在目标值的 70%～130%，且没有输出截断；
- thinking 只有在质量门槛不下降且能解释额外延迟/token 成本时才可进入下一阶段候选。

### 18.9 InkOS CLI 两条隔离长跑

直接 API 矩阵通过后才运行 InkOS。两种 mode 必须是两个独立 project root，不能在同一本书中途切换配置；
否则前序摘要、伏笔、状态和失败重试会污染 A/B。

初始化两个 project root：

```bash
cd "$RUN_ROOT/inkos-no-thinking"
node "$INKOS_CLI" init --lang zh

cd "$RUN_ROOT/inkos-thinking"
node "$INKOS_CLI" init --lang zh
```

non-thinking 进程配置：

```bash
export INKOS_LLM_PROVIDER=custom
export INKOS_LLM_BASE_URL="$GPU6_BASE"
export INKOS_LLM_API_KEY=EMPTY
export INKOS_LLM_MODEL="$GPU6_MODEL"
export INKOS_LLM_API_FORMAT=chat
export INKOS_LLM_STREAM=false
export INKOS_LLM_THINKING_BUDGET=0
export INKOS_LLM_EXTRA_top_p=0.95
export INKOS_LLM_EXTRA_top_k=20
export INKOS_LLM_EXTRA_min_p=0
export INKOS_LLM_EXTRA_presence_penalty=0
export INKOS_LLM_EXTRA_repetition_penalty=1
export INKOS_LLM_EXTRA_seed=24081106
export INKOS_LLM_EXTRA_chat_template_kwargs='{"enable_thinking":false}'
unset INKOS_LLM_EXTRA_thinking_token_budget
unset INKOS_LLM_EXTRA_include_reasoning
```

thinking 进程在相同基线上只替换：

```bash
export INKOS_LLM_THINKING_BUDGET=1024
export INKOS_LLM_EXTRA_thinking_token_budget=1024
export INKOS_LLM_EXTRA_include_reasoning=true
export INKOS_LLM_EXTRA_chat_template_kwargs='{"enable_thinking":true}'
```

注意：此处没有设置 `INKOS_LLM_TEMPERATURE=0.8` 来冒充 Writer 参数已经生效。当前 InkOS agent 会显式传各自
温度并覆盖 client default；本轮 CLI 结果必须在报告中写明 Writer 实际基线为 0.7。`top_p=0.95` 和 seed 是
transport extra，当前会透传给全管线。

每个 project root 分别执行建书；`book-id` 以 JSON 返回值为准，不要假定书名就是 id：

```bash
node "$INKOS_CLI" book create \
  --title "余烬九序-GPU6-A或B" \
  --genre xuanhuan \
  --platform tomato \
  --target-chapters 300 \
  --chapter-words 3000 \
  --brief "$BRIEF" \
  --lang zh \
  --json
```

`target-chapters=300` 只表示全书目标，不允许一次模型调用生成 300 份详细章纲。初始化验收只要求：全局命题、
世界规则、核心人物、卷级结构/主要 arc 以及近期约 30 章可执行规划。若 Architect 单次响应开始机械枚举几百章、
上下文逼近 admission 上限、响应被截断或基础文件出现重复，先暂停进入正文，保留当前 attempt 并修正输出预算、
分层规划 prompt 或滚动规划范围；新 attempt 的基础文件通过后即可继续。后续远期规划应滚动展开。

先显式观察一章，不直接 `--count 30`：

```bash
node "$INKOS_CLI" plan chapter <book-id> --json
node "$INKOS_CLI" compose chapter <book-id> --json
node "$INKOS_CLI" write next <book-id> --words 3000 --count 1 --json
node "$INKOS_CLI" eval <book-id> --json
```

首章通过后，以 5 章为一个人工可审计批次，串行跑到第 30 章：

```bash
node "$INKOS_CLI" write next <book-id> --words 3000 --count 5 --json
node "$INKOS_CLI" status <book-id> --json
node "$INKOS_CLI" eval <book-id> --json
```

重复上面三条批次命令直到累计 30 章。GPU6 当前服务按单序列串行观察，不用后台并发堆积请求。每 5 章保存
status/eval、章节 SHA、上下文估算、token/延迟、失败次数和服务健康快照；每章结束后验证章节文件、summary、
人物状态、伏笔和当前 chapter number 共同前进，不能只相信 CLI 打印“成功”。

### 18.10 长跑暂停、修复、恢复与最终阻断

出现下列情况时先暂停当前 attempt，不让坏结果进入下一章或状态结算，然后执行 §18.7.1 的归因和受控修复：

- HTTP 400/422 表示 thinking 参数、模板参数或 request schema 当前不匹配；
- 502/503/超时持续到当前请求失败；传输重试复用 seed 和请求语义；
- response/content 为空、只有 reasoning、`finish_reason=length` 或正文硬截断；
- 同一句/同段机械重复，连续三段语义几乎相同，或 12-gram 重复超过门槛；
- 章节质量审计失败但 InkOS chapter number、summary 或 truth files 已前进；
- 人物生死/位置/能力、时间线、POV、章纲结果出现硬冲突；
- prompt + output reserve 超过 128K admission；
- 三个 seed 输出完全相同，表明 seed 未透传或服务端采样退化；
- reasoning 超过预算容差，或 thinking 成本持续增长而质量无收益。

恢复时先记录失败 request identity 和现有状态文件 SHA，从已验证 checkpoint 或无状态 API attempt 继续。可以
创建新的 attempt/config version、提高合理预算、修正共同 prompt、修复明确代码缺陷并重跑；不得删除失败目录、
覆盖原 response、就地手改 truth files 后假装同一 run 连续成功，也不得在 A 项目里切到 B 配置续写。

只有满足以下任一条件才把整个测试标记为 `BLOCKED` 并交回决策：

- endpoint/model identity 无法证明，或服务能力与必需合同根本不兼容；
- 已确认的上下文窗口无法容纳最小可用 prompt、正文和 reasoning reserve；
- 同一明确故障在多次因果修复后仍无改善，且当前没有新的可检验假设；
- 继续操作必须安装/升级/重启、破坏已有证据、越过权限边界或污染 Canon，而尚未获得授权；
- 状态已不可逆损坏且没有 accepted checkpoint 或可重建来源。

“一次输出被截断”“一次 HTTP 失败”“一个参数不兼容”本身都不是整个研究任务的永久阻断条件。

最终状态只允许：

| 状态 | 含义 |
|---|---|
| `RETRYING:<failure_class>` | 当前 attempt 已保留，正在进行同语义传输重试或因果明确的新 attempt |
| `NEEDS_REPAIR:<reason>` | 当前结果不能推进状态，但存在安全、可验证的修复方向 |
| `API_AB_PASS` | GPU6 直接 API 的 3 seed × 2 mode 严格矩阵通过 |
| `INKOS_30CH_DIAGNOSTIC_PASS` | 当前 InkOS 1.7.2 两条隔离长跑均完成 30 章，无重复/状态推进错误 |
| `ROLE_SCOPED_WRITER_AB_PASS` | role-scoped 采样实现后，Writer 0.8/0.95 的正式 A/B 通过 |
| `BLOCKED:<reason>` | 安全修复路径已耗尽，或继续需要新的权限/外部状态变化 |

报告必须明确区分上述三种 PASS。只完成单章、只完成 non-thinking、只看 HTTP 200、或当前 InkOS 用默认 Writer
0.7 跑完 30 章，都不能写成 `ROLE_SCOPED_WRITER_AB_PASS`。
