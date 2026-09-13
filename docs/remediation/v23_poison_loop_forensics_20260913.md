# v23 poison loop 的现场核对与处置记录（2026-09-13）

对应指导：`docs/yujin_jiuxu_next_stage_execution_guidance_20260913.md` 第 2.1 节（v20/v21 残留）
与第 8.2 节（清理前核对 PID、lease、attempt 与调用账本）。

## 1. 现场核对

用运行时入口读取（未直接 SQL 改状态）：

```
novel-agent runtime status   --run-id run.yujin-jiuxu.v23
novel-agent runtime classify --task-id run.yujin-jiuxu.v23.plan.arc-volume.g0
```

| 对象 | 核对结果 |
|---|---|
| run | `run.yujin-jiuxu.v23`，5 个任务 |
| 已完成 | `…plan` / `…plan.accept` / `…plan.accept.commit` / `…plan.accept.commit.projection` 均 `succeeded` |
| 未完成 | `run.yujin-jiuxu.v23.plan.arc-volume.g0`（`plan_candidate`，`plan_level=arc_volume`）核对时 `running`，revision 52；处置后 `blocked`，revision 53 |
| 当前 attempt | `attempt.96b5e2354797153e85b1d41ea1f005557eca445470f37f8e`（no=24） |
| lease | `10:49:40Z` 到期；核对时 `13:34:55Z`，**已过期约 2 小时 45 分** |
| 进程 | `ps` 无任何 `novel-agent` / `production.dispatcher` 进程 |
| effect ledger | **0 条**（无 `REQUESTED`、无 `UNCERTAIN`、无 `COMPLETED`） |
| 上一已结算 attempt | no=23，`outcome=suspended`，`failure_class=poison_loop`，结束于 `10:42:53Z` |

`current_attempt_id` 与最新 attempt 一致，说明不是"旧 attempt 残留"，而是**该 attempt 的
worker 已消失且 lease 未被回收**：运行停在 running，无人推进。

## 2. 分类与处置

`runtime classify` 给出：

```
action = repair_owning_module
failure_class = poison_loop
retry_owner = operator
safe_to_retry = false
unsettled_sends = []   outstanding_request_ids = []   completed_response_refs = []
```

按指导第 8.1 节：确定性失败（此处 `poison_loop`）进入负责模块的修复路径，
**重试同一输入不能改善结果**，因此不做盲重试。分类同时证明 effect ledger 干净，
不存在"结果不确定"的发送，所以处置不需要先对账。

用运行时恢复入口结算（保留终态原因，不直接写库）：

```
novel-agent runtime reconcile \
  --project-id project.yujin-jiuxu.v23 --run-id run.yujin-jiuxu.v23 \
  --task-id run.yujin-jiuxu.v23.plan.arc-volume.g0 \
  --observed-revision 52 \
  --command-id reconcile.stale-lease.attempt24 \
  --actor-id author.hjcui98 \
  --terminal-status blocked --failure-class poison_loop \
  --reason "attempt 24 lease expired with no live worker; the run stopped in a poison loop at the ARC_VOLUME stage"
```

结果：task revision **53**，`status=blocked`，`current_attempt_id=null`，
`failure_budget` 6 → 5，终态工件（2 个 planning-loop-event、2 个 checkpoint）已挂到任务上。
随后 `status` 复核：4 succeeded / 1 blocked，无 `running` 残留。

## 3. 根因：v23 的实际失败形态

终态事件 `terminal.review_revision_required` 携带的 `plan-review` 工件
（`sha256:efd59bbf2…`）是：

```
decision = "revise"
issues   = []
revision_instruction = "修正 vol-4 中 midpoint_reversal 与 volume_climax 的语义越界：
  将 vol-4.payload.midpoint_reversal.description 中"发现黑月坠世并非实验失控的异常线索"
  改为"发现九位圣座存亡的异常线索"；将 vol-4.payload.volume_climax.description 中
  "暗示黑月坠世并非实验失控"改为"暗示九位圣座存亡异常"。依据 lock.long-truth.vol4-hint，
  第四卷仅允许对九位圣座存亡进行暗示，黑月坠世真相的暗示须推迟至第五卷
  （lock.long-truth.vol5-advance）。"
```

即：**模型提出了具体的、可执行的修改意见，但没有任何结构化意见**。
宿主既不能核验也不能强制执行它；每次修订都重写整份计划，要求既不关闭也不被拒绝，
循环于是不断购买 attempt（checkpoint 记录 `plan_revisions_used=16`），
直到 attempt 24 的 worker 消失、lease 过期。

**这正好是本轮 N1/N3 修复的形态**：无结构化意见的 `REVISE` 现在不授予任何修订范围，
`compose_scoped_revision` 返回父候选原样，循环停在
`REVIEW_REVISION_REQUIRED` 而不是继续重写。

模型那条意见本身是合理的（它确实指名了字段），但它没有按契约写成
`affected_item_ids` + `field_path` + `quote` + `unmet_condition`。同一诉求写成结构化意见后，
宿主能精确派生范围为 `vol-4.volume_climax`——这正是修复后的 prompt 所要求的形状。

## 4. 回归验证

新增 `tests/integration/test_yujin_v23_poison_loop_regression.py`（6 项，确定性），
**直接读取该真实工件**作为输入，而不是重建：

| 用例 | 断言 |
|---|---|
| 该审校确为 `revise` + `issues=[]` | 前提来自工件本身；自由文本确实指名了两个字段与两个锁 |
| 无结构化意见 → 不授予范围 | `scope.targets/additions/removals` 全空；`blocking_issue_identity` 为空 |
| 无范围 REVISE 不能改写冻结计划 | 任意整份重写经合成后**逐字等于父候选** |
| 首次不构成进展、重复也不构成 | 空前沿无比较基准；同一（空）身份不算新进展 |
| 同一诉求写成结构化意见后可用 | 范围精确为 `vol-4` 的 `volume_climax`，身份含所引约束 |
| 宿主覆盖在 Planner 之前拒绝 | 传真实作者目录后，无范围 REVISE settle 为 `ACCEPT`、`revision_instruction=None` |

## 5. 对 G0 的含义

- v23 这条运行**不能再作为 G0 载体**：其 basis 上的 ARC_VOLUME 任务已 `blocked`，
  且指导第 2.1 节要求"不能把它当作实时状态"。它的价值是上述证据。
- 修复后同一失败形态会**立即停止并保存候选与诊断**，而不是空转 24 次；
  真实收敛能力仍需在新的冻结身份上验证。
- 仍未处置：v20/v21 的历史残留（指导第 2.1 节记载的旧 `RUNNING` 与 `REQUESTED`）。
  本轮只核对了 v23；v20/v21 需按同样流程逐个核对，且它们不属于当前推进线。

## 6. 限制

- 本记录只处置了 v23 的 stale lease 与分类核对；未取消、未删除、未重发任何调用。
- `failure_budget` 由 6 减为 5 是 reconcile 的既有语义（该失败消耗任务预算），
  未手工调整。
- 未清理 v20/v21；未对 v23 的 blocked 任务做任何后续推进。

## 7. v20 / v21 残留处置（同轮完成）

按同一流程以运行时入口核对与结算，未直接写库：

| run | task | 核对时状态 | 未结算 attempt | lease 到期 | failure_class | 处置 |
|---|---|---|---|---|---|---|
| v20 | `…v20.plan.arc-volume.g0` | `running` rev 34 | no=15 | 08:33:04Z（已过期约 5 小时） | **无**（最后已结算 attempt 未记录分类） | `blocked` / `worker_lease_expired`，rev → 35，budget 1 |
| v21 | `…v21.plan.arc-volume.g0` | `running` rev 41 | no=19 | 09:10:23Z（已过期约 4.5 小时） | `poison_loop` | `blocked` / `poison_loop`，rev → 42，budget 5 |

两者的 effect ledger 均为 **0 条**（无 `REQUESTED`、无 `UNCERTAIN`、无 `COMPLETED`），
因此不需要先对账，也不存在"结果不确定的发送"。核对时 `ps` 无任何 `novel-agent` 进程。

v20 的处理依据值得单列：它的最后已结算 attempt **没有** `failure_class`，
所以 `runtime classify` 如实报告 `undetermined` 而不是猜一个确定性类别。
它被结算为 `worker_lease_expired`，因为那正是可核验的原因——worker 消失、lease 过期——
而不是把未知分类伪装成已知分类。

处置后全库复核：

```
unsettled attempts (ended_at IS NULL): 0
tasks with status = 'running': 0
```

即指导第 2.1 节记载的"残留 `RUNNING`"已全部处置完毕，
每一条都保留了终态与原因，没有删除任务、没有改写数据库状态、没有盲重发调用。

## 8. 处置后的总体状态（截至本轮）

| run | 阶段 | 状态 | 说明 |
|---|---|---|---|
| v20 | ARC_VOLUME | `blocked` / `worker_lease_expired` | 未分类失败 + 放弃的 lease |
| v21 | ARC_VOLUME | `blocked` / `poison_loop` | ARC_VOLUME 阶段收敛失败 |
| v22 | ARC_VOLUME | `ready`（无 attempt） | 未开始，保留 |
| v23 | ARC_VOLUME | `blocked` / `poison_loop` | STORY 已提交；ARC_VOLUME 收敛失败 |

这四条都停在**同一个阶段**：ARC_VOLUME。它们的共同失败形态是无结构化意见的
`REVISE` 驱动整份重写（见第 3 节），正是本轮 N1/N3 修复的对象。
因此下一阶段的 G0 必须在新代码与新冻结身份上重新验证收敛，而不能复用这些运行。
