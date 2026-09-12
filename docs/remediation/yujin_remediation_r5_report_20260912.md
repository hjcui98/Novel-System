# R5 执行证据（可恢复修稿、模型重放与预算）

日期：2026-09-12（+08:00）
范围：方案第 6 节 R5、第 8 节 A15/A16
状态：进行中。模型重放契约已完成；修稿前沿持久化与恢复整合待续。

## 1. 已完成：完成态模型调用的重放（A16）

### 1.1 缺陷

`ModelGateway` 有完整的调用账本（`request_hash`、`raw_response_hash`、`raw_artifact_ref`、
终态），但**没有任何重放入口**：重启后要复用一次已完成调用，只能重新发一次请求。
方案第 5 节要求：“completed response 仅在 request hash、模型/配置、ledger 与原始响应一致时重放；
uncertain 或已发送未完成请求不能当成普通失败直接重发。”

### 1.2 实现

新增 `ModelGateway.replay_completed_structured(request, output_type, *, json_object_framing=False)`
与结果契约 `ModelReplayOutcome`（冻结 dataclass，`reason` 精确命名拒绝原因）：

| 检查顺序 | 拒绝原因 | 含义 |
|---|---|---|
| 1 | `endpoint_no_longer_registered` | 该 role 已无注册端点 |
| 2 | `no_ledger_entry` | 从未发过这个请求 |
| 3 | `uncertain_call_not_replayable` | **已发送未结算**：必须人工/程序对账，不能重放也不能当普通失败重发 |
| 4 | `request_identity_drift` | 绑定后的请求身份（含解析出的预算）不一致 |
| 5 | `terminal_status:*` | 终态不是 COMPLETED（如 VALIDATION_REJECTED） |
| 6 | `missing_raw_evidence` | 账本缺 call record 或原始响应工件 |
| 7 | `endpoint_identity_drift` / `model_identity_drift` | 提供方身份与当前注册不符 |
| 8 | `raw_evidence_unavailable` | 未配置原始响应仓库 |
| 9 | `raw_evidence_identity_drift` / `raw_response_hash_mismatch` | 原始响应与账本身份或内容哈希不符 |
| 10 | `raw_response_no_longer_parses` | 原始响应在**当前**严格契约下不再可解析 |

关键设计点：

- **绑定后再比对**：账本记录的是**完全绑定**（含解析预算）的请求身份，因此重放走同一条
  `_bind_budget` 路径。模型、序列上限或预算策略变化会表现为身份漂移，而不是静默复用旧响应。
- **不信任存储结论**：原始响应被重新读取并用当前严格契约重新解析，而不是直接采信账本里的
  “已完成”。
- **uncertain 优先**：未结算调用先于身份比较上报，避免被误判为普通漂移后重发。
- 重放不产生新调用，原调用的 usage 与完成时间保持不变。

### 1.3 证据

`tests/unit/test_model_replay.py`（10 项）：重放不触发第二次 provider 调用、无账本条目、
prompt 漂移、响应契约漂移、模型身份漂移、uncertain 调用、原始响应损坏（先保持哈希一致
以专门走到重新解析分支）、原始响应哈希不符、校验被拒调用、重放不消耗新预算。

## 2. 已完成：修稿前沿持久化（A15 预算部分）

### 2.1 缺陷

`writer_context_loop` 在恢复时无条件写：

```python
local_repair_attempt = 0
...
rewrite_attempt = 0
```

而大改分支的循环条件是 `while rewrite_attempt < request.budgets.max_major_rewrites`，
局修分支的无变化守卫也是 `local_repair_attempt < request.budgets.max_local_repairs`。
因此**每次重启都会重新发放整套修稿配额**——一个在两次修稿之间重启的进程可以无限次
支付同一份 pinned 预算。这既是预算失控，也违反方案“最多一次 Canon 提交 / 有限尝试”的要求。

### 2.2 实现

- `WritingLoopPhase` 新增 `REPAIR_PENDING` 作为显式耐久前沿（与盲修一致）；
- `WritingLoopCheckpoint` 新增 `repair_stage`（dispatch/local_review/rewrite_draft/
  rewrite_review）、`local_repairs_used`、`major_rewrites_used`、`repair_input`，
  并加校验：repair 前沿必须带输入与拒绝历史，`local_review` 必须有已结算的局修，
  `rewrite_review` 必须有已结算的大改；
- 两个计数改为从 checkpoint 恢复；两个 post-draft 前沿在写 checkpoint 时一并持久化
  当前计数与阶段。

### 2.3 证据

`tests/unit/test_writing_loop_repair_frontier.py`（6 项）：新 checkpoint 计数为零、
计数与阶段可序列化往返、负值与未知阶段被拒、`REPAIR_PENDING` 是已声明前沿且其规则存在、
以及一条源码检查断言恢复路径**确实读取**持久化计数（防止只写不读）。

## 3. R5 未完成项

| 项 | 内容 |
|---|---|
| `REPAIR_PENDING` 端到端 | 构造完整 post-Draft fixture 验证“局修中/大改中重启”的完整行为，属集成测试层 |
| 恢复入口统一 | 恢复请求与新建请求共用 readiness（R3 已完成部分）；恢复时的 frozen request 重建需接入 `replay_completed_structured` |
| 共享调用预算 | 单轮预算需覆盖 Writer/Editor/内部重试，并区分 slice yield、累计耗尽、provider 错误与契约错误；重放不占新配额 |
| 旧 checkpoint | 缺必需证明的旧 checkpoint 必须返回明确的“重建/新任务”要求，不得补造 passed 标志 |
| A15 | Memory/首稿/局修/大改/复审后重启的端到端证据 |

## 4. 回归与失败身份

`tests/unit tests/contract`：**76 failed / 3005 passed / 1 skipped**；
失败身份集合与整合前基线**逐项完全相同**（0 新增、0 修复）。R5 至今新增 16 项通过测试。

`tests/integration/test_writer_context_loop.py` 在整合前就有 11 项失败（已用 stash 对照确认与本次改动无关），
不在 R0 基线的命令范围内，将在 R6 前单独登记。
