# Stage 2/3/4/5 模型预算与 Thinking/Output 运行时策略

> Lifecycle: `PROPOSED`
>
> Date: 2026-08-18
>
> Scope: Stage 2A/2M Memory Planner、Controller、Semantic Judge 的模型调用预算解析；Stage 3 Writer、Stage 4 Planner、Stage 5 Runtime 接入控制。
>
> Related code: `src/novel_agent/domain/model_calls.py`、`src/novel_agent/services/model_gateway.py`、`src/novel_agent/adapters/model/openai_chat.py`、`src/novel_agent/services/evidence_first_checkpoint_runner.py`、`src/novel_agent/runtime/memory_controller.py`。

---

## 1. 问题陈述

当前模型预算存在多个来源，且彼此不一致：

| 位置 | 当前行为 | 风险 |
|---|---|---|
| `adapters/model/openai_chat.py:271` | `"max_tokens": request.max_output_tokens or self.max_output_tokens` | `request.max_output_tokens=None` 时静默回落到 endpoint 默认值（当前为 8192），调用方可能完全不知道 |
| `services/model_gateway.py:374` | `output_tokens = request.max_output_tokens or 4096` | 调度器按 4096 预留，但真实 API 请求可能是 8192；KV admission 和调用记录不一致 |
| `services/writer_cognition.py:151` | `request.budgets.reserved_output_tokens or 4096` | 同样存在 `None/0` 的静默 4096 回落 |
| `services/plan_conditioned_need_planner.py:433` | Planner 输出硬上限 `max_output_tokens <= 32768` | 与 `ModelRequest` 的 131072 上界不一致，且“无输出上限 canary”无法真正到达 model max |
| `services/teacher_forced_benchmark_e2e.py:1942-1945` | Controller 请求写死 `enable_thinking=False`，输出跟随 endpoint 默认 | 开思考或给 Controller 独立输出预算都必须改代码 |
| `runtime/memory_controller.py:898` | 单工具 timeout 写死 `min(..., 30_000)` | 工具实际 timeout 与声明的 120s wall-clock 不一致 |
| `services/evidence_first_checkpoint_runner.py:309-353` | `max_rounds=2`、`max_query_rewrites_per_need=0`、`wall_clock_budget_ms=120_000` 写死 | 外层 Stage 3/4/5 无法按任务传入不同检索预算 |
| `services/paired_controller.py:145` + `tools/retrieval.py:67` | Agentic 工具 adapter 默认 `max_limit=20`，未接 request 的 `max_candidates` | `--max-candidates > 20` 被静默夹回 20 |
| `agents/controller.py:237,268` | `_bind_draft` 写死单批最多 8 个 action | `max_agentic_actions=32` 的配置实际不生效 |

当前 Stage 2 主链路多数请求显式设置了预算，所以最近的 8192/4096/2048 不是全部静默回落。但只要后续某个 Agent 或外层 Stage 传入 `None`，就可能同时出现“实际请求 8192、调度预留 4096、日志记录又不一致”的问题。**这个修复必须在开启 thinking 和扩大输出上限之前完成。**

---

## 2. 目标与非目标

### 2.1 目标

1. 预算、thinking、output 上限成为**运行时参数**，不再在 runner/adapter 调用点写死业务默认值。
2. 一个 `effective_output_tokens` 同时驱动：
   - OpenAI-compatible API 的 `max_tokens`；
   - `ModelGateway` 调度预留；
   - Writer/Planner/Controller 的请求构造；
   - 日志、manifest、model call ledger。
3. `None` 不再表示“静默回落”。它必须被解析为明确的 `budget_source`。
4. 支持 `auto/model_max` 模式：根据 provider context limit、估算输入和安全余量动态计算输出上限。
5. thinking/reasoning 预算显式计入总输出预留；`thinking_token_budget=None` 只表示“不传该参数、由服务端决定”，但仍要有可审计的估算预留。
6. Stage 3/4/5 外层运行时可以通过 `PlanningBudgets`、`ContextWindowPolicy`、`ProviderValidityReceipt` 等既有结构传入控制，不另建平行系统。

### 2.2 非目标

- 不在本次把候选数量、query rewrite、L0 装配等检索执行权交给 Agent。
- 不把“无限输出”定义为字面不发送 `max_tokens`；`unbounded/auto` 仍是内部算出一个明确上限。
- 不把 action/tool/wall-clock/KV 并发预算合并进 token budget。
- 不改变 `ToolPolicy` / `RetrievalBudget` 的 content-addressed 审计语义。

---

## 3. 统一预算模型

### 3.1 定义

```text
C = 服务或模型的上下文/序列上限
I = 当前序列化请求的估算输入 token
B = 正文/结构化输出预算
R = thinking/reasoning 预算
S = 安全余量
O = B + R（总输出预算）
```

必须满足：

```text
I + O + S <= C

有效输出上限 O_effective =
    explicit request budget
    或 stage/invocation budget
    或 registered endpoint/model default
    或 auto/model_max 动态计算结果

可用输入预算 = C - O_effective - S
调度预留   = I + O_effective + S
```

### 3.2 与现有实现的对应关系

- `ProviderValidityReceipt.available_input_tokens` 已经实现 `sequence_limit - reserved_output_tokens - safety_allowance_tokens`。
  - 见 `src/novel_agent/domain/agent_context.py:135-157`。
- `PlanningBudgets.model_token_budget` 已经是 invocation 级累计 soft slice。
  - 见 `src/novel_agent/domain/planning.py:228-243`。
- `planning_context_loop.record_model_call()` 已经累计 `input + output + reasoning`。
  - 见 `src/novel_agent/services/planning_context_loop.py:213-235`。

当前缺少的是：**把这些既有字段接到“API 请求构造”和“ModelGateway 调度预留”的同一个解析器上**，而不是让各调用点各自 `or 4096/or 8192`。

### 3.3 应新增的解析记录

每次模型请求都应在 ledger/manifest 中记录：

```json
{
  "context_limit": 131072,
  "estimated_input_tokens": 12340,
  "body_output_budget": 8192,
  "thinking_budget": 2048,
  "total_output_budget": 10240,
  "safety_allowance_tokens": 4096,
  "reserved_sequence_tokens": 26676,
  "remaining_input_budget": 114396,
  "budget_source": "invocation_budget | endpoint_default | model_max_auto",
  "compaction_route": "none | truncate_tool_results | compact"
}
```

其中 `budget_source` 是强制字段，禁止出现无来源的预算。

---

## 4. `EffectiveBudgetResolver` 解析规则

### 4.1 解析优先级

```text
1. explicit request override（ModelRequest.max_output_tokens 显式传入）
2. stage/invocation policy（PlanningBudgets.reserved_output_tokens 等外层控制）
3. endpoint/model registered default（provider profile 中明确登记）
4. auto/model_max：
   O = min(
         C - I_estimated - S,
         provider_model_max_output,
         global_output_cap
       )
5. 没有任何来源：
   - canary profile：显式进入 auto/model_max，记录 budget_source = model_max_auto
   - production profile：直接 ValidationError，不发送请求
```

### 4.2 `auto/model_max` 的计算

```text
O_auto =
  min(
    provider.sequence_limit - estimated_input_tokens - safety_allowance_tokens,
    provider.output_limit,
    global_output_cap
  )
```

- `estimated_input_tokens` 当前用 UTF-8 字节近似，后续可替换为 provider tokenizer 的真实计数；解析器只依赖接口，不依赖具体 tokenizer。
- `provider.output_limit` 必须来自 provider/model profile，不能是代码里的 8192。
- `global_output_cap` 是部署级安全上限（例如 32768 或 131072），来自配置，不在 adapter 内写死。

### 4.3 thinking/reasoning 预算

- `enable_thinking=false`：`R = 0`。
- `enable_thinking=true` 且 `thinking_token_budget` 显式：`R = thinking_token_budget`，并在 API payload 中传 `thinking_token_budget`。
- `enable_thinking=true` 且 `thinking_token_budget=None`：
  - API payload 不传该字段，由服务端决定；
  - 调度仍使用 `R_estimated = policy.estimated_reasoning_reserve`（默认不得为 0），避免 KV/序列预留低估。
- provider profile 必须声明 `reasoning_included_in_completion_tokens`，决定 `O = B + R` 还是 `O = max(B, R)` 的记账口径。

### 4.4 禁止的写法

```python
# 禁止
max_output_tokens = request.max_output_tokens or self.max_output_tokens
output_tokens = request.max_output_tokens or 4096
```

统一替换为：

```python
effective = self._budget_resolver.resolve(request, policy, provider_profile)
payload["max_tokens"] = effective.total_output_budget
scheduling.reserved_output_tokens = effective.total_output_budget
```

API payload、ModelGateway scheduling、writer_cognition、日志/manifest 必须使用同一个 `effective`。

---

## 5. 阶段性缺省策略

这是本次文档的核心策略：**“现在不传就放大；Stage 3/4/5 接入后再控制”。**

### 5.1 Stage 2 canary / 当前阶段

允许以下 profile：

```json
{
  "default_output_mode": "model_max_auto",
  "allow_missing_explicit_budget": true,
  "budget_source": "model_max_auto",
  "thinking": {
    "enable": true,
    "budget": null
  }
}
```

含义：

- 调用方未显式传 output 时，**不要静默回落 8192**，而是通过 `auto/model_max` 动态计算并发送明确的大上限。
- `thinking_token_budget=null` 表示“服务端自行决定”，但调度仍用 `estimated_reasoning_reserve` 做安全预留。
- 每次请求必须记录 `budget_source=model_max_auto`，便于后续复盘和回退。
- 该 profile 只用于当前 1~2 个 case 的 canary 实验，不用于正式 Gate。

### 5.2 Stage 3/4/5 生产接入

Stage 3/4/5 已经具备外层控制接口，应作为**最终预算来源**：

- Stage 3 Writer：`PlanningBudgets.model_token_budget` + `ContextWindowPolicy`。
- Stage 4 Planner：`PlanningBudgets` + `PlannerContextRuntime` + `ContextCompactor`。
- Stage 5 Runtime：Task/Attempt 级 `PlanningBudgets`、checkpoint 累计计数、admission controller。

此时规则变为：

```text
budget_source =
    invocation_budget（必填，来自 PlanningBudgets）
  | endpoint_default（provider profile 已登记）
  | error
```

- `allow_missing_explicit_budget=false`。
- 没有显式 invocation 预算且没有登记 endpoint default 时，`ModelGateway` 直接抛 `ModelBudgetResolutionError`。
- Stage 3/4/5 的 context pressure、compaction、invocation soft slice 仍按既有 `AgentContextRuntime` / `ContextCompactor` 执行，不把模型输出预算和上下文压缩混成一个数字。

### 5.3 转换条件

Stage 2 canary 切换到 strict production profile 的条件：

1. `auto/model_max` canary 完整记录 `budget_source` 和真实 usage；
2. 没有出现 `finish_reason=length`、scheduling 预留偏差或 KV admission 低估；
3. Stage 3/4/5 外层调用路径已能显式传入 `PlanningBudgets`；
4. provider profile 已登记 `sequence_limit`、`output_limit`、reasoning 记账口径。

---

## 6. 上下文渐进式展开策略（Planner / Controller）

### 6.1 原则

1. WorldRoot / 确定性摘要不是上下文终点。WorldRoot 的 `Event / StateRecord / RelationRecord / PlanObligation` 都携带 `evidence_refs`，可以回源到原始文本；见 `src/novel_agent/domain/world.py:80-98`。
2. 模型始终从有界摘要开始，认为不足时再向可信代码请求下一层原文；**代码负责 cutoff、access scope、snapshot、预算和截断**，模型不能任意读原文。
3. Controller 不能只收到“执行完/没执行完”。它至少需要看到任务、Need 契约、候选内容和必要的精确切片，才能判断是否继续检索；但最终证据充分性仍由 Semantic Judge 裁决。
4. 每一层展开都必须通过同一个 `EffectiveBudgetResolver`：先确定 `O`，再计算 `可用输入 = C - O - S`，上下文组装器只在这个额度内打包。
5. 装不下时按确定性顺序 truncate/drop/compact，并记录 `compaction_route`；不得静默丢掉 mandatory Need 或合法动作。

### 6.2 Planner：从 WorldRoot 摘要渐进回原文

当前 Planner 只有 WorldRoot 的有界摘要，且 `TaskPlanConditionedNeedGenerator` 没有接收 `TextRootDocument`；见 `services/task_conditioned_need_generation.py:222` 和 `services/evidence_first_checkpoint_runner.py:256`。

建议增加三层：

```text
P0 现有世界摘要：48 实体 / 64 状态 / 12 事件 / 48 关系 / 32 义务
P1 记录证据片段：对 P0 命中的高优先级 Event/State/Relation/Obligation，
    用 evidence_refs 在冻结 TextRoot 上解析精确 L0 片段；受条数和字符预算约束
P2 章节窗口：Planner 认为某章证据不足时，通过只读工具请求某一章/场景/段落的
    有界窗口；仍受 checkpoint_chapter 和 access scope 约束
```

实施分两步：

- 先做**确定性 P1**：`PlannerSourceExpander` 在 Planner 调用前为摘要中的高优先级记录附加精确片段；模型不需要新工具，风险最低。
- 再做**工具化 P2**：给 Planner 增加 `text.read_chapter_window` 一类只读工具，`ToolPolicy` 独立限制 `max_rounds/max_tool_calls` 和 `planner_text_token_budget`。

安全边界：

- `chapter > checkpoint_chapter` 的正文不可读；
- APC 允许读 Plan，但不等于允许读未来正文或 Gold；
- 所有 `evidence_refs` 解析必须校验 source commit 和 snapshot，失败时标记 access/freshness failure。

### 6.3 Controller：从摘要升级到“可判断证据”的观察上下文

当前 Controller compact prompt 只发送 `candidate_count / success / gain`，见 `agents/controller.py:124-163`；原始 `ToolResult.payload.hits` 里有完整 `RetrievalUnit.text`，但没有进入模型上下文。同时，精确 L0 切片目前发生在 Controller 循环结束之后的 evidence-first selection 阶段，所以 Controller 现在不可能基于切片判断完成度。

建议把 Controller 观察上下文定义为四层：

```text
C0 当前摘要：task id、Need id/intent/requirement/resolved、action outcomes
C1 Need 契约：query_text、semantic_question、实体、required facet ids、
    当前 unresolved/closed 状态
C2 候选预览：每轮工具的 Top-K hit，
    包含 unit_id、channel、rank、unit_kind、chapter、predicate、truth_class、
    以及按字符预算截断的 unit.text
C3 精确 L0 预览：对未解决 mandatory Need 的 Top-K 候选，用
    EvidenceSliceResolver 在观察组装阶段做 bounded preview slicing
C4 按需展开：后续 Arm C 允许 Controller 通过合法动作请求某个候选/章节的更多细节
```

实施顺序：

- 第一步先落 **C1 + C2**：不需要改变检索和切片管线，只新增 `ControllerObservationAssembler`，从已有 `ToolResult` 中确定性打包候选预览。
- 第二步把 **C3** 做成有界 preview：每条 unresolved mandatory Need 最多解析 `preview_slice_limit` 条、总 token 受 `可用输入` 约束；它不替代最终 Writing Package 的精确切片，只服务 Controller 的停止决策。
- 第三步再考虑 **C4** 的 Agent 主动展开，这属于 Arm C 权限扩展，必须单独 feature flag。

### 6.4 上下文组装与预算的接口

`ControllerObservationAssembler` 和 `PlannerSourceExpander` 应统一使用：

```text
available_input_tokens = C - O_effective - S
```

组装顺序固定为：

```text
1. protected：合法动作、预算、身份字段
2. mandatory Need 契约
3. unresolved mandatory Need 的候选/切片预览
4. optional Need 摘要
5. 历史动作结果（旧结果优先压缩成一行）
```

超预算时的处理顺序：

```text
1. 截断候选文本到最小可读片段
2. 丢弃 optional Need 摘要
3. 压缩最旧的历史动作结果
4. 才允许 compact mandatory 候选预览（记录 dropped/compact 明细）
```

每次请求记录：

```json
{
  "controller_context_level": "C2",
  "context_input_tokens": 9230,
  "available_input_tokens": 114396,
  "candidate_preview_count": 18,
  "candidate_text_truncated": true,
  "slice_preview_count": 6,
  "compaction_route": "truncate_tool_results_only"
}
```

### 6.5 与语义裁决的边界

Controller 看到候选和切片后，只负责判断“是否继续检索、检索哪个 Need、用哪个合法工具”；**“证据是否真正回答 Need”仍由 Semantic Judge 和 FacetSupportEvaluator 决定**。Controller 上下文增强不能变成让 Controller 替代 Judge。

---

## 7. 其他预算必须保持独立

不能把以下预算混入 `max_tokens`：

| 预算 | 现有归属 | 保持独立 |
|---|---|---|
| retrieval rounds / tool calls | `RetrievalBudget` / `ToolPolicy` | 不改 |
| Controller decision model calls | `QualityRepairFeatureFlags.max_controller_decision_model_calls` | 不改 |
| agentic action 数 | `QualityRepairFeatureFlags.max_agentic_actions` | 保持并修复 binder 8 截断 |
| wall-clock / provider timeout | `wall_clock_budget_ms`、`ModelRequest.timeout_seconds` | 开 thinking 前必须重估 |
| endpoint request / KV 并发 | `ModelRequestAdmissionController` | 使用同一个 `reserved_sequence_tokens` |
| invocation 累计 token | `PlanningBudgets.model_token_budget` | 继续累计 `input+output+reasoning` |

---

## 8. 修复清单

### 7.1 必须立即修复的静默回落

| 文件 | 修复 |
|---|---|
| `adapters/model/openai_chat.py:271` | 使用 `EffectiveBudgetResolver` 的 `total_output_budget`；删除 `or self.max_output_tokens` |
| `services/model_gateway.py:374` | scheduling 使用同一 `total_output_budget`；`None` 时按 resolver 结果，不再固定 4096 |
| `services/writer_cognition.py:151` | `reserved_output_tokens or 4096` 改为解析后的有效值或显式报错 |
| `services/plan_conditioned_need_planner.py:433` | 32768 上界从“业务常量”改为配置安全上限，并允许 canary profile 放宽到 model max |

### 7.2 参数化硬编码

| 文件 | 修复 |
|---|---|
| `services/evidence_first_checkpoint_runner.py:309-353` | `max_rounds`、`max_query_rewrites_per_need`、`wall_clock_budget_ms` 从 runtime config 传入 |
| `services/teacher_forced_benchmark_e2e.py:1942-1945` | Controller `enable_thinking` / `thinking_token_budget` / `max_output_tokens` 从 model policy 传入 |
| `runtime/memory_controller.py:898` | `30_000` 改为 `per_tool_timeout_ms`，仍受 wall-clock 和 provider timeout 约束 |
| `services/paired_controller.py:145` | `RetrievalToolAdapter(max_limit=...)` 接入 request 的 `max_candidates` 或统一的上界 |
| `agents/controller.py:237,268` | 8 截断改为 `max_agentic_actions`，保留安全上限 |

### 7.3 新配置对象建议

```json
{
  "model_budget_policy": {
    "default_output_mode": "model_max_auto",
    "global_output_cap": 32768,
    "safety_allowance_tokens": 4096,
    "estimated_reasoning_reserve_tokens": 4096,
    "strict_missing_budget": false
  },
  "planner_model": {
    "enable_thinking": true,
    "thinking_token_budget": null,
    "max_output_tokens": null,
    "max_input_tokens": 12000
  },
  "controller_model": {
    "enable_thinking": true,
    "thinking_token_budget": null,
    "max_output_tokens": null,
    "max_decision_model_calls": 8,
    "max_agentic_actions": 32
  },
  "semantic_judge_model": {
    "enable_thinking": true,
    "thinking_token_budget": null,
    "max_output_tokens": null,
    "max_input_tokens": 12000
  }
}
```

`max_output_tokens=null` 在此 schema 中明确表示“使用 model_budget_policy.default_output_mode”，不再表示“跟随某个代码常量”。

---

## 9. 实施顺序

1. **Phase 0：本文档 + 回归基线。**
   不改默认行为，只记录当前 8192/4096/2048 基线。
2. **Phase 1：统一 resolver。**
   - 新增 `EffectiveBudgetResolver`；
   - OpenAI adapter、ModelGateway、writer_cognition 全部切换；
   - 默认 profile 仍显式给出当前值，保证行为不变；
   - 补测试：`None` 不再静默回落。
3. **Phase 2：参数化现有 Stage 2 硬编码。**
   - Controller/Planner/Judge 的 thinking/output 改为传参；
   - 修复 retrieval adapter 20、binder 8、per-tool timeout 30s；
   - CLI/JSON 暴露配置并写入 experiment manifest。
4. **Phase 3：Controller 观察上下文 C1 + C2。**
   - 新增 `ControllerObservationAssembler`；
   - Controller prompt 从纯摘要升级为 Need 契约 + Top-K 候选预览；
   - 在 `可用输入 = C - O - S` 内打包，记录 `controller_context_level` 和 `compaction_route`；
   - 不改变检索、rerank、最终切片管线。
5. **Phase 4：canary profile。**
   - `default_output_mode=model_max_auto`、`strict_missing_budget=false`；
   - 1~2 个 case 跑 Controller-only thinking，观察 `length` / timeout / repair 率；
   - 若需分离变量，可与 Phase 3 各自单独出一次结果。
6. **Phase 5：Planner 渐进展开 P1。**
   - 增加 `PlannerSourceExpander`，把 WorldRoot 高优先级记录的 `evidence_refs` 解析为有界原文片段；
   - 先做确定性 P1，再评估是否需要 Planner 工具化 P2。
7. **Phase 6：Stage 3/4/5 接入 strict profile。**
   - 外层显式传 `PlanningBudgets`；
   - `strict_missing_budget=true`；
   - provider profile 补齐 sequence/output limit 和 reasoning 口径。
8. **Phase 7：C3 精确切片预览 + 动态压缩联动。**
   - 对 unresolved mandatory Need 的 Top-K 候选做 bounded L0 preview slicing；
   - 当 `I + O + S > C` 时，优先 compact history / truncate tool results / 等待下一次 checkpoint，而不是静默降低 O。
   - 最后再评估 Arm C 的 C4/P2 主动展开权限。

---

## 10. 验收标准

- `request.max_output_tokens=None` 时，行为只能来自 resolver 记录的 `budget_source`，不允许出现隐式 8192 或 4096。
- API `max_tokens`、ModelGateway scheduling、日志/manifest 三者一致。
- canary profile 下，未显式传 output 时能动态算出大上限并完整记录输入估算、输出预算、thinking reserve、剩余输入。
- strict profile 下，无显式预算且无 registered default 时直接报错，不发送模型请求。
- thinking 开启后，`reasoning_tokens` 进入 usage 和 invocation soft slice 累计。
- 开 thinking 前，Controller wall-clock / scheduling timeout / KV reserve 均有对应配置，不再依赖 120s 默认。
- Controller 观察上下文至少包含 C1 Need 契约和 C2 Top-K 候选预览，不再只含 success/count/gain 摘要；每次请求记录 `controller_context_level`、输入 token 和截断/丢弃明细。
- Planner P1 展开后的原文片段满足 checkpoint cutoff、access scope、source commit/snapshot 校验，并记录展开条数和 token。
- 上下文组装器在 `available_input = C - O - S` 内打包；超限时按确定性顺序截断/丢弃/压缩并记录 `compaction_route`，不得静默删除 mandatory 内容。
- 现有 Stage 2A 回归测试在 Phase 1 默认 profile 下全部通过。

---

## 11. 明确不做的“假 unbounded”

以下实现被视为不合规：

- 把 `max_tokens` 从 payload 中删掉，然后祈祷 provider 不截断；
- `request.max_output_tokens=None` 时继续悄悄使用 endpoint 构造时的默认值；
- thinking budget 未知时按 0 预留；
- 把 `auto` 算出来的大上限再次写死成 8192/32768/131072 中的某一个全局常量。

`unbounded` 在本仓库中的唯一合法语义是：**通过 `auto/model_max` 计算出一个明确的、可审计的、不会静默变化的输出上限。**
