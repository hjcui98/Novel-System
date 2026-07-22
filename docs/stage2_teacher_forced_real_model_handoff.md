# Stage 2 Teacher-Forced Benchmark 真实模型测试实施交接

更新时间：2026-07-21  
实施对象：接手本仓库的编码 Agent  
唯一生成模型：本机 `qwen36-27b-nvfp4`，API `http://127.0.0.1:8002/v1`

## 1. 用户最终确认的实验定义

Benchmark 不应预先伪造每个 checkpoint 的完整 WorldRoot、Curator Replay Gold 或连续 Manifest。需要人工模拟的只有“作者动笔前已经拥有的初始输入”。完成稿正文直接代替尚未开发的 Writer 输出，后续状态必须由系统逐章维护出来。

正确流程：

```text
模拟作者初始输入
-> Bootstrap Ingestion
-> Planner PROJECT_BOOTSTRAP
-> Curator BOOTSTRAP
-> Bootstrap Validator
-> 模拟作者批准
-> Genesis C0
-> 原序章和第 1 章作为 Writer 已完成正文
-> Curator REPLAY
-> Validator
-> Risk Classifier / Guardian（需要时）
-> Canon Commit + R1 Projection
-> 逐章继续
-> C20 冻结真实项目状态
-> Planner 从 Genesis PlanRoot 和当前状态制定 21-25 章计划
-> MemoryNeed 生成
-> Memory Controller 使用只读检索工具组装上下文
-> 冻结检索结果
-> 此后 Evaluator 才能读取 21-25 正文和 Gold
-> 评分后继续 teacher-forced 回放
-> C40 / C60 / C80 / C95 同样执行
```

必须保持的边界：

1. `cases/*/gold.yaml`、未来正文、Replay Gold 和后验总结只能由 Evaluator 在 Context Freeze 后读取。
2. 根据用户最新意见，`cases/*/input.yaml` 中人工写好的精确 `target_plan` 也不能作为 checkpoint 前额外注入的作者输入。Planner 必须从初始大纲、当前 PlanRoot 和已发生正文自行规划。
3. `rough_story_outline.md` 在 `AUTHOR_PLAN_CONDITIONED` 中可以进入 PlanRoot，但始终是 `PLAN/INTENT`，不能成为 World Fact。
4. `VISIBLE_AT_CUTOFF` 不得看见作者未来大纲；两种 profile 分开运行和报告。
5. 已到达章节的正文是 Writer 替身，不是 Gold；尚未到达的正文才是 evaluator-only。
6. 真实模型失败时禁止静默回退到 scripted 输出。

## 2. 输入与本地模型

Benchmark：`benchmarks/private/ztj_memory_pilot_v0.1`。

模拟初始输入位于：

```text
benchmarks/private/ztj_memory_pilot_v0.1/bootstrap/
  author_initial_brief.md
  baseline_setting.md
  rough_story_outline.md
  project_preferences.md
  bootstrap_manifest.yaml
```

这些文件都标明 `provenance: reconstructed_from_completed_novel` 和 `experiment_role: simulated_author_input`。最终报告必须说明它们是依据完成稿重建的模拟输入，不是真实创作历史资料。

模型参数：

```text
base_url:            http://127.0.0.1:8002/v1
model:               qwen36-27b-nvfp4
context:             131072
并发:                串行 1
enable_thinking:     false
默认 max_tokens:     8192
```

已经验证 vLLM 支持：

```json
{
  "chat_template_kwargs": {"enable_thinking": false},
  "response_format": {
    "type": "json_schema",
    "json_schema": {"name": "output", "schema": {}, "strict": true}
  }
}
```

不关闭 thinking 时，短请求也可能把 token 用在 reasoning 上并以 `content=null`、`finish_reason=length` 结束。不能删除该参数。Codex 沙箱访问 loopback 可能需要提权，但主机 API 已验证为 HTTP 200。

## 3. 已经完成的工作

已有入口：

- `scripts/run_stage2_teacher_forced_e2e.py`
- `src/novel_agent/services/teacher_forced_benchmark_e2e.py`
- Make target：`stage2-teacher-forced-e2e`

scripted 契约冒烟曾完整跑通：Genesis 获批，序章加 1-95 章共 96 个 Commit，Curator REPLAY 调用 95 次，C20/C40/C60/C80/C95 五个 checkpoint 均冻结并评分，future isolation failure 和 future leakage 都是 0。该结果只证明编排、落库、Commit、Projection、Freeze 和 Evaluator 顺序，不证明语义质量；当时逐章 Curator 是空 delta，报告已标为 `semantic_quality_eligible=false`。

已新增或修改：

- `src/novel_agent/agents/curator_bootstrap.py`：Curator BOOTSTRAP Agent facade。
- `CuratorBootstrapDraft`：位于 `src/novel_agent/domain/stage2.py`。
- `src/novel_agent/adapters/model/scripted.py`：只用于 CI/契约测试。
- `src/novel_agent/adapters/model/openai_chat.py`：本地 OpenAI-compatible adapter 初版。
- `ModelRequest.response_schema`：已加入 `src/novel_agent/domain/model_calls.py`。
- `ModelGateway.generate_structured`：已开始向 Endpoint 传 Pydantic JSON Schema。
- `Stage2PairedPilotRunner.resolve_state_case` 与 `score_comparison`：已把 Freeze 前检索和 Freeze 后 Gold 评分拆开。
- `_plan_needs`：已改成从当前 PlanRoot chapter goals 生成，不再从 `plan_obligation_gold` 生成。
- `tests/contract/test_stage2_teacher_forced_e2e.py`：scripted 全流程合同测试，旧版本通过，约 21 秒。
- checkpoint Planner 已开始改成以当前 `self.plan` 为输入，不再直接载入 benchmark 的 `case.input_plan_root`。

CLI 已加入：

```text
--semantic-backend local_openai|scripted
--model-base-url http://127.0.0.1:8002/v1
--model qwen36-27b-nvfp4
--model-max-output-tokens 8192
```

## 4. 当前真实状态和风险

接手后不能直接声称真实 Stage 2 测试已完成：

1. 只有 scripted 旧版本全流程跑通过。
2. 本地模型的 models、JSON object、JSON Schema guided output 探测成功。
3. 本地模型尚未跑完真实 Genesis、单章 Curator 或 95 章回放。
4. Memory Controller 仍使用 `RouteBoundControllerPolicy`，尚未切为 `StructuredControllerPolicy` 和本地模型。
5. OpenAI adapter 缺 MockTransport 单元测试、异常响应测试和 client 生命周期处理。
6. 新增 `response_schema` 后需要重新导出 Stage 0/1/2 schemas，并重跑合同测试。
7. 真实 Curator 可能通过 JSON Schema 但无法通过 Evidence、Overlay 或 Validator；必须保留失败证据并有限重试，不能改成空 delta 继续。
8. 真实 Guardian 不能使用 scripted 自动批准。
9. 最近的 Planner 防泄漏重构尚未完整复跑。
10. 仓库全量 coverage 和 mypy 已有既存债务。功能验证可用 `--no-cov`，但不要误报全部门禁通过。

## 5. 实施工作包

### WP0：恢复稳定基线

1. 对最近改动执行 ruff 和目标 mypy。
2. 重新导出 schema：

```bash
.conda-env/bin/python scripts/export_schemas.py
.conda-env/bin/python scripts/export_stage1_schemas.py
.conda-env/bin/python scripts/export_stage2_schemas.py
```

3. 确认 `schemas/stage0/ModelRequest.schema.json` 包含 `response_schema`。
4. 用 `--semantic-backend scripted` 重跑 E2E，确认仍有 96 commits 和 5 checkpoints。

```bash
.conda-env/bin/python scripts/run_stage2_teacher_forced_e2e.py \
  --source benchmarks/private/ztj_memory_pilot_v0.1 \
  --output-directory /tmp/ztj_stage2_scripted_recheck \
  --information-profile author_plan_conditioned \
  --semantic-backend scripted
```

### WP1：完成本地模型 Adapter

完善 `src/novel_agent/adapters/model/openai_chat.py`：

1. 默认只允许 loopback。
2. 使用 `/chat/completions`，强制 `enable_thinking=false`。
3. 有 `response_schema` 时发送 strict `json_schema`，否则发送 `json_object`。
4. 校验 `finish_reason=stop` 和非空字符串 content。
5. 记录 prompt/completion tokens，费用保持本地 0。
6. HTTP、非 JSON、缺字段、null content、length 截断必须抛类型明确的错误。
7. 不打印潜在密钥。
8. 用 `httpx.MockTransport` 增加成功与失败单元测试。
9. 明确 AsyncClient 的关闭时机。
10. 允许有限重试时，每次 attempt 都要审计，最多 1-2 次；禁止 scripted fallback。

### WP2：增加分段运行和恢复能力

不要立即跑 95 章。为 CLI/Runner 增加：

```text
--stop-after-genesis
--max-chapter 1
--max-chapter 20
--resume
```

每章成功后原子写 progress manifest，恢复时从最后一个已接受 Canon Commit 开始。先后执行真实 Genesis、序章加第 1 章、C20 三个小门禁。

### WP3：真实 Genesis

验证：

1. Planner PROJECT_BOOTSTRAP 输出通过 `PlannerProposalDraft` 与 provenance 检查。
2. Curator BOOTSTRAP 只从 `baseline_setting.md` 中标为 `WORLD_FACT_AT_STORY_OPEN` 的内容写 WorldRoot。
3. `rough_story_outline.md` 只进 PlanRoot。
4. BootstrapCrossRootValidator 通过。
5. 模拟作者批准被 SQLite 持久化。
6. Genesis Commit 和五个 Root 可按 hash 读回。
7. ModelCallRecord、Agent receipt、输入与输出 artifact IDs 均落盘。

### WP4：真实逐章 Curator

每章严格执行：

```text
append TextRoot
-> CuratorReplayAgent（真实模型）
-> ModelCurator / Evidence 校验
-> WorldOverlay
-> Stage1Validator
-> PatchRiskClassifier
-> GuardianWriteGate
-> CommitService
-> DerivedProjectionService
-> FreshnessGate
```

Curator 只能看到当前已揭示正文和当前 WorldRoot；每个 operation 必须绑定已揭示正文中的有效 EvidenceRef；不允许用未来章节修正当前抽取。失败时保存输出、错误类型和父 Commit，不得以空 delta 代替真实结果。C20 对数据库、对象库、Commit 链和 R1 snapshot 做一致性审计。

### WP5：真实 checkpoint Planner

将 `_normalize_checkpoint_plan` 改名为 `_run_checkpoint_planner`。合法输入只有当前 PlanRoot、当前已揭示 TextRoot 或安全摘要、当前 WorldRoot、目标章节范围，以及该 profile 可见的初始 brief/rough outline。

禁止输入 `case.input_plan_root` 的精确 target plan、Gold、未来正文和 retrospective summary。输出应覆盖目标范围内每章的 `ChapterGoal`，保持 `PLANNER_PROPOSED` 或合法 `AUTHOR_SUPPLIED` provenance。计划永远是 intent，不能由 Curator 当作已经发生的事实。

### WP6：把 Memory Controller 切到真实 Agent

真实实验必须使用：

```text
StructuredControllerPolicy
+ Memory Controller AgentSpec(BOUNDED_R2)
+ system_policy_v1.md
+ memory_controller_v1.md
+ iterative_retrieval_v1.md
+ evidence_sufficiency_v1.md
+ context_reduction_v1.md
```

修改 `Stage2PairedPilotRunner.resolve_state_case`，允许注入 Controller policy factory。动态 ToolPolicy 是关键：AgentSpec 的 tool policy hash 必须与 `BoundedMemoryController` 的实际 policy 完全一致。可以在每个 case 冻结后创建对应的 sealed ToolPolicy、AgentSpec 和 StructuredAgentRunner，或使用一个统一、合理、固定的预算。

Controller 只能拥有现有只读检索工具，不能 Commit、写 Root 或读取 evaluator source。每轮 `ControllerPolicyDecision` 都必须来自本地模型并保留 decision receipt/model_call_id。deterministic 和 agentic 两臂必须共享同一 commit、snapshot、MemoryNeed、后端和预算。

现有 `Stage1NeedGenerator` 从当前 WorldRoot 和公开 target range 生成，不读 Gold，可以先用。Author-plan-conditioned needs 必须来自当前 PlanRoot。若改用 `ModelMemoryNeedGenerator`，也只能输入当前 WorldRoot、当前 PlanRoot 和公开目标范围，并单独统计 Need 生成调用。

### WP7：严格 Freeze 后评分

Freeze 前不要把包含 Gold 字段的完整 `BenchmarkCaseManifest` 传给 Agent 代码。提取最小公共 view，只保留 case id、project id、target range、公开 task contract 和 information profile。

Freeze 后 Evaluator 才加载 Gold，计算 historical evidence recall、operational constraint coverage、plan obligation coverage、mandatory coverage、evidence traceability、future leakage、tool calls、tokens 和 latency。Evaluator 必须保持 `canonical_write_count=0`，销毁 evaluator context 后才继续回放。

### WP8：完整连续实验

C20 小门禁通过后再连续跑到 C95。由于只有一张卡，全部模型调用串行。分别运行 `visible_at_cutoff` 和 `author_plan_conditioned`，分目录、分数据库，不共享 mutable 状态。建议输出：

```text
reports/stage2a/teacher_forced_real/
  visible_at_cutoff/
  author_plan_conditioned/
```

主结果必须来自单项目连续回放；独立重建只用于幂等与顺序依赖审计。

## 6. 必补测试

1. OpenAI adapter：成功、HTTP 失败、非 JSON、length、null content、usage 缺失。
2. ModelGateway：Pydantic schema 确实进入 Endpoint request。
3. CuratorBootstrapAgent：成功与越权 source provenance。
4. 修改 Gold 后，Freeze 前 `_plan_needs` 结果不变。
5. checkpoint Planner 不访问 benchmark exact PlanRoot。
6. Freeze 前访问 evaluator-only source 必须失败。
7. StructuredControllerPolicy 与 ToolPolicy hash 一致和不一致。
8. scripted E2E 仍完成 96 commits。
9. 本地模型 live test 使用显式 `model_required` 标记，普通 CI 不自动运行。
10. C20 中断恢复后，最终 Commit 链与无中断运行一致。

功能测试：

```bash
.conda-env/bin/pytest -q tests/unit tests/contract --no-cov
```

目标静态检查：

```bash
.conda-env/bin/ruff check \
  src/novel_agent/adapters/model/openai_chat.py \
  src/novel_agent/services/teacher_forced_benchmark_e2e.py \
  src/novel_agent/services/stage2_paired_pilot.py \
  scripts/run_stage2_teacher_forced_e2e.py

.conda-env/bin/mypy \
  src/novel_agent/adapters/model/openai_chat.py \
  src/novel_agent/services/teacher_forced_benchmark_e2e.py \
  src/novel_agent/services/stage2_paired_pilot.py \
  scripts/run_stage2_teacher_forced_e2e.py
```

## 7. 最终验收标准

只有全部满足，才可称为真实 Stage 2 benchmark 已运行：

1. Planner、Curator BOOTSTRAP、95 次 Curator REPLAY、需要时的 Guardian、Memory Controller 都有本地模型 ModelCallRecord。
2. 96 个 Canon commits 构成单一连续父链，每个 commit 的 R1 snapshot fresh。
3. 五个 checkpoint 都在读取未来正文前完成 Context Freeze。
4. future isolation failure 和 future leakage 均为 0。
5. Controller tools、prompt、skills、AgentSpec 和 ToolPolicy 都有版本 pin 和 receipt。
6. checkpoint Planner 未读取 benchmark exact target plan 或未来正文。
7. Evaluator canonical write count 为 0。
8. 报告写明本地 model id；只有所有语义 Agent 都是真实模型时，`semantic_quality_eligible=true`。
9. 报告同时提供质量指标和运行指标，不能只说流程成功。
10. 报告明确初始大纲来自完成稿重建，因此属于 reconstruction-conditioned benchmark。

## 8. 优先检查文件

```text
src/novel_agent/services/teacher_forced_benchmark_e2e.py
src/novel_agent/services/teacher_forced_scenario.py
src/novel_agent/services/stage2_paired_pilot.py
src/novel_agent/services/paired_controller.py
src/novel_agent/runtime/memory_controller.py
src/novel_agent/agents/controller.py
src/novel_agent/agents/curator_bootstrap.py
src/novel_agent/agents/curator.py
src/novel_agent/agents/planner.py
src/novel_agent/adapters/model/openai_chat.py
src/novel_agent/services/model_gateway.py
src/novel_agent/domain/model_calls.py
scripts/run_stage2_teacher_forced_e2e.py
tests/contract/test_stage2_teacher_forced_e2e.py
```

总原则：先保证信息边界正确，再追求跑通率。真实输出失败要留下证据并修合同、schema 或 prompt，不能通过提前读取 Gold、灌入 benchmark target plan、自动空 delta 或 scripted fallback 来“修通”实验。
