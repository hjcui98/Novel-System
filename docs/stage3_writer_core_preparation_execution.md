# Stage 3 Writer Core 准备性开发执行设计

> 文档生命周期：`SUPERSEDED`
>
> 取代文档：`docs/stage3_writer_core_overall_design.md`
>
> 保留用途：legacy Writer 实现细节与准备阶段历史输入，不再作为 Stage 3 当前执行基线
>
> 状态：Historical Preparation Input
>
> 初版日期：2026-07-28
>
> 更新日期：2026-07-31
>
> 运行取证窗口：2026-07-28 10:46–11:00 +08:00
>
> 模块：Writer Agent Contract & Generation Kernel
>
> 当前允许状态：`IMPLEMENTED_IN_ISOLATION_UNMERGED`
>
> 当前禁止状态：`MEMORY_INTEGRATED`、`PRODUCTION_ENABLED`、`QUALITY_PROMOTED`
>
> 第二阶段模块拆分：`NOT_STARTED`；第 8、13 节仅保留为既有草案输入，待第一阶段确认后重构
>
> 旧阶段名：Stage 2B
>
> 设计依据：总体架构 v2.2、技术实施 v0.1、正式开发执行规划、Stage 0/1/2、当前实现和真实运行产物

> 实现事实：Writer Core 位于 legacy worktree
> `/home/cuihengjia/agent/novel/NS-stage2b-writer`，当前为未提交、未合并实现；旧 worktree、分支和
> 物理路径中的 `stage2b` 仅作为历史标识保留。

---

## 0. 执行结论

在当前记忆模块继续进行真实逐章回放与质量验收时，下一项适合并行开发的单独模块是：

> **Stage 3 Writer Core：Writer 合同、纯生成内核、DraftArtifact lineage 与
> fake/offline shadow harness。**

本次只开发 Writer，不同时开发 Editor，不接完整章节 TaskGraph，不接 Curator 写回，不形成
Candidate ChangeBundle，不调用 Commit Service，也不运行正式 Stage 3 质量实验。

本文保留的工作包和 Issue 清单用于说明已有设计深度，不代表本轮已经完成模块边界复核、接口冻结
或人员分配；这些工作属于用户确认第一阶段结果后的第二阶段。

并行阶段的首个可交付切片是 `DRAFT`。同时冻结 `CONTINUE` 与 `MAJOR_REWRITE` 的模式语义和
输入互斥规则，后两种模式在 `DRAFT` 合同稳定后实现。这样既不把临时单模式设计固化为长期
接口，也避免一开始把 Editor 修复、重规划和章节提交链一起拉入范围。

### 0.1 对“Stage 012”的解释

仓库没有名为 `Stage 012` 的正式阶段。本文按用户意图将其解释为“Stage 0、Stage 1、Stage 2
的当前总体进度”。精确编号 `DEV-012` 只是 Stage 0 演示和验收任务，已经随 Stage 0 PASS 完成。

### 0.2 为什么是 Conditional Go

正式执行规划把完整 Writer/Editor 生成闭环放在 Memory Kernel 正式冻结之后：

- `长篇小说Agent正式开发执行规划_v0.1.md:1370-1397`
- `docs/stage2_memory_agents_development.md:1595-1609`
- `docs/stage2_memory_agents_development.md:1752-1759`

因此，本文件不解除 Memory Gate，也不宣布 Stage 3 正式质量阶段已经通过。它只批准一个不消费在线
Memory Gateway、不写 Canon、只用冻结 fixture 和 fake model 的休眠实现切片。

如果后续把此切片接入真实 `ContextPackage`、真实模型、Editor/Curator 或章节主图，必须重新经过
本文第 11 节的 Integration Gate。

### 0.3 2026-07-31 当前事实修订

本文初版时的 C34 运行快照只保留为历史取证。当前状态以
`docs/project_status.md` 为准：

- Stage 2A 开发完成并取得 `CONDITIONAL_PASS`；
- deterministic real-hybrid Memory Gateway 已冻结；
- Stage 2M 已实现 `WriterContextPackage` 并进入 WP8 诊断运行；
- legacy Writer worktree 已实现三模式 Core 和 DRAFT deterministic handoff，但全部改动仍未提交、
  未合并；
- legacy handoff 仍消费 `Stage1ContextPackage`，必须迁移到 ADR-0004 接受的
  `WriterContextPackage` 后才能形成当前 Stage 3 Integration Gate 证据；
- Writer Semantic Gate 仍为 `NOT_RUN`，Production Gate 仍为 `BLOCKED`。

因此，旧验收文档中的 `PASS_DRAFT_DETERMINISTIC_ONLY` 只属于隔离 worktree 的历史工程证据，
不能直接作为当前主工作树的 Stage 3 Gate 结论。

---

## 1. 当前事实基线

### 1.1 设计文档快照与实时运行事实必须分层

`docs/current_progress_architecture_technical_report_20260727.md` 的正文基线是
`7c9c125`，记录 Canon 安全停在 C20。该报告自己规定：当前源码、代码门禁和真实运行产物的
事实优先级高于历史设计目标，见该文档 `:60-69`。

2026-07-28 的仓库和运行产物已经前进，而且在本文编写期间仍持续变化：

| 项目 | 取证窗口内最新观察值 |
|---|---|
| Git HEAD | `97b59ce632b5d2f4746f0dcacbcd2d99b159bf92` |
| 最新恢复实验 | `c34-recovery-97b59ce-from-3606881-r27` |
| 真实回放进度 | C1–C34 已接受；C35 typed pause 后进入恢复迭代 |
| 最新接受 Commit | `sha256:36068815d5584641f6b991d03df7e53bb13e04b8e362853da41de9adf1d21a17` |
| Retrieval | `real_hybrid` |
| Controller | `deterministic_plus_agentic_delta` |
| Curator | Candidate ID v2 + pre-candidate support gate |
| 写回模式 | 非 dry-run |

运行证据：

- `reports/stage2a/teacher_forced_real/author_plan_conditioned_qwen36_c34_recovery_97b59ce_from_3606881_20260728_r27/experiment_manifest.json`
- 同目录 `progress_manifest.json`

取证窗口开始时，主线还是 `d98f4e2`、r26 和 C32；结束前已经变为 `97b59ce`、r27 和
C34。这一变化本身证明主线仍在主动修复和恢复。本文只把 C34 作为“取证窗口内已经达到的
下界”，不把它写成长期固定状态，也不把它解释为 C95 长程 Gate 已通过。开始实现 Writer 时必须
重新读取最新 HEAD、最新 `progress_manifest.json` 和 Gate 报告，不能直接复用本表章号。

### 1.2 Stage 0/1/2 当前判断

| 阶段 | 当前判断 | 对本模块的含义 |
|---|---|---|
| Stage 0 | PASS | Artifact、ModelGateway、RunEvent、内容寻址和测试底座可复用 |
| Stage 1 Engineering | PASS | `Stage1ContextPackage` 等合同已存在 |
| Stage 1 Formal Quality / Freeze | 未完成 | Writer 不得正式消费和宣传 Memory Kernel 质量 |
| Stage 2A Harness | 已实现 | Planner、Controller、Curator、Guardian 的运行模式可作为实现范式 |
| Stage 2R | 真实 paired 已运行，但未达原定质量目标 | 不修改检索和 Context Compiler |
| Stage 2W | 已从 C21 继续到至少 C34，真实回放仍在进行 | 不修改 Curator、写回、Commit、Projection、runner |
| Stage 3 | 准备性开发 | 只允许隔离 Writer Core 和受控 DRAFT 接线 |

Stage 2R 最新 C20 paired 结果中，Arm A 与 Arm C 的 Gold Evidence Recall 都为
`0.863636...`，Mandatory Coverage 都为 `0.9`，`accuracy_gain=false`；
Future Leakage 和 Safety Regression 为 0：

- `reports/stage2a/teacher_forced_real/author_plan_conditioned_qwen36_quality_repair_c20_paired_cdb3483_20260728_r15/e2e_paired_report.json:16-64`

这意味着安全底座可继续使用，但 Memory Kernel 的正式质量冻结和 Agentic Controller 晋升仍不能
视为完成。Writer 的后续真实接线必须能够在 deterministic Memory Gateway 上运行，不能把
BOUNDED_R2 晋升当作前置假设。

---

## 2. 模块选择依据

### 2.1 Writer 是下一条产品链上的第一个缺口

正式路线把 Stage 3 定义为：

```text
Writer
→ Editor
→ Writer-declared Change
→ Declared vs Observed Reconciliation
```

依据：`长篇小说Agent正式开发执行规划_v0.1.md:1370-1397`。

总体运行图中，Writer 位于 Context Compiler 之后，并同时成为 Editor Review、Memory Curator 和
Draft Deterministic Checks 的共同前驱：

```text
Plan / Scene Contract
          +
Frozen ContextPackage
          ↓
      Writer Core
          ↓
     DraftArtifact
      ├── Editor Review
      ├── Curator Extraction
      └── Draft Checks
```

依据：

- `长篇小说Agent总体架构设计_v2.2_完整合并版.md:2911-2940`
- 同文档 `:3953-3981`
- 同文档 `:4146-4175`

### 2.2 Writer 的职责边界已经足够稳定

Writer 的唯一主目标是把场景合同和上下文实现为正文。已经稳定的模式为：

```text
DRAFT
CONTINUE
MAJOR_REWRITE
```

标准输出包含正文、弱变化提示、未决问题和 Self Observation。Writer 不负责正式审校，不批准
自己的正文，不直接调用 Exact/BM25/Vector/Graph，也不写 Canon。

技术实现已经把 Writer 映射为：

```text
typed composition call
→ DraftArtifact
```

依据：

- `长篇小说Agent总体架构设计_v2.2_完整合并版.md:2821-2833`
- 同文档 `:2911-2940`
- `长篇小说Agent技术实施与选型设计_v0.1.md:320-350`

### 2.3 当前仓库已有足够可复用底座

| 能力 | 现有位置 |
|---|---|
| 严格 Pydantic 领域模型 | `src/novel_agent/domain/base.py` |
| 内容寻址 Artifact | `src/novel_agent/services/artifacts.py` |
| 模型端点抽象 | `src/novel_agent/ports/model_endpoint.py` |
| Model Gateway 与调用账本 | `src/novel_agent/services/model_gateway.py` |
| Prompt/Skill 内容哈希 | `src/novel_agent/prompts/registry.py`、`skills/registry.py` |
| Agent Spec 与执行 Receipt | `src/novel_agent/domain/stage2.py` |
| 审计化结构输出 Runner | `src/novel_agent/agents/runner.py` |
| Fake/Scripted Model | `src/novel_agent/adapters/model/fake.py`、`scripted.py` |
| 冻结 Context 合同 | `src/novel_agent/domain/memory.py:324-338` |
| Context Freeze Receipt | `src/novel_agent/domain/stage2.py:1627-1635` |

当前缺少 Writer、Editor、DraftArtifact 和 EditorialReport。Writer 因此是边界最干净、可通过新增
文件完成主体实现的下一模块。

### 2.4 候选比较

| 候选 | 产品顺序 | 与当前实测冲突 | 前置依赖 | 结论 |
|---|---:|---:|---:|---|
| Writer Contract + Fake Harness | 最高 | 低 | 已具备 | **选择** |
| Acceptance Manifest 聚合器 | 中 | 低 | 已具备 | 作为运行治理补充，不是下一业务模块 |
| Editor REVIEW | 次高 | 低 | 依赖 DraftArtifact | Writer 后开发 |
| Draft Deterministic Checks | 次高 | 低 | 依赖 DraftArtifact/Writing Contract | Writer 后开发 |
| Declared vs Observed Reconciliation | 高 | 高 | 依赖 Writer + 当前 Curator 热区 | 当前暂缓 |
| 完整 Stage 3 TaskGraph | 高 | 极高 | Writer、Editor、Curator、Commit | 当前禁止 |
| Advanced R2 / Maintenance / Skill 演化 | 低 | 高 | 后续 Gate 和运行数据 | 不选 |

---

## 3. 本次范围

### 3.1 必须交付

1. Writer 三模式领域合同；
2. 首个 `DRAFT` 生成实现；
3. 内容寻址 `DraftArtifact` 与完整 Artifact Basis；
4. `declared_memory_hints`、`unresolved_questions`、`self_observations` sidecar；
5. Writer AgentSpec、Prompt、Skill 和零底层检索 ToolPolicy；
6. fake/offline shadow harness；
7. JSON Schema 导出；
8. 单元、合同、golden、故障和权限测试；
9. 明确的 typed terminal；
10. Post-Memory-Gate 接线清单，但不执行接线。

### 3.2 明确不交付

- Editor REVIEW 或 REPAIR；
- Writer 与 Editor 合并调用；
- Writer 与 Curator 合并调用；
- `ObservedChangeSet`；
- `CandidateChangeBundle`；
- Declared vs Observed Reconciliation；
- MemoryWriteWorkflow、Commit、Projection 或 Freshness 接线；
- 在线 Reactive MemoryNeed / ContextDelta 恢复；
- 顶层章节 LangGraph；
- 多正文候选搜索；
- 正式真实模型 A/B；
- Stage 3 PASS、Writer 质量 PASS 或 Memory Kernel PASS 结论。

### 3.3 权威边界

```text
Writer model output
    = untrusted generation result

DraftArtifact
    = content-addressed candidate artifact
    ≠ TextRoot
    ≠ Narrative Canon

declared_memory_hints
    = weak advisory signal
    ≠ EvidenceRef
    ≠ ObservedChangeSet
    ≠ MemoryPatch
    ≠ Candidate ChangeBundle

只有未来独立 Curator + Validation + Commit 链
    才可能把已接受正文和变化发布到 Canon
```

Generation Result、Candidate ChangeBundle 和 Accepted Commit 必须分离，依据：
`长篇小说Agent总体架构设计_v2.2_完整合并版.md:1837-1850`。

---

## 4. 领域合同

领域模型建议新增在 `src/novel_agent/domain/generation.py`，不继续扩大已经很大的
`domain/stage2.py`。只在后者的公共 Agent 枚举中增加最小 Writer 类型与模式。

### 4.1 Writer Mode 集合

```text
DRAFT
CONTINUE
MAJOR_REWRITE
```

持久化与 Agent Registry 统一使用现有 `AgentMode` 扩展，不再建立第二套可独立演化的
`WriterMode` 枚举。`domain/generation.py` 可以提供 Writer mode 校验集合或类型别名，但不得让
它与 `AgentMode` 形成两套权威。

模式语义：

| Mode | 必需输入 | 禁止输入 | 输出语义 |
|---|---|---|---|
| DRAFT | Writing Contract + Frozen Context | prior draft、rewrite directive | 新候选正文 |
| CONTINUE | prior draft + continuation boundary | rewrite directive | 保留冻结前缀后续写 |
| MAJOR_REWRITE | prior draft + frozen rewrite directive | Editor local repair scope 冒充 major rewrite | 新候选，不覆盖旧 Artifact |

首个实现只把 `DRAFT` 注册为可执行 AgentSpec；三种模式的领域枚举、互斥验证和 Schema 一次冻结。

### 4.2 `WriterArtifactBasis`

所有 Writer 输入和输出必须绑定同一 Basis：

```yaml
writer_artifact_basis:
  project_id: ...
  base_commit: ...
  snapshot_id: ...
  context_id: ...
  context_artifact: ...
  context_fingerprint: ...
  writing_contract_artifact: ...
  plan_artifact: ...
  project_profile_artifact: ...
  configuration_fingerprint: ...
  future_isolation_attestation: ...
  source_artifacts: [...]
```

规则：

1. `base_commit == ContextPackage.base_commit`；
2. `snapshot_id == ContextPackage.snapshot_id`；
3. `context_id == ContextPackage.context_id`；
4. Context、Plan、Profile、Prompt、Skill、Tool 和模型配置均有内容哈希；
5. `future/evaluator/gold` taint 不得进入 Writer-safe input；
6. Basis 任一字段不一致时，在模型调用前 `CONTRACT_REJECTED`；
7. 不允许模型回写或覆盖任何 trusted Basis 字段；
8. 重试、恢复和幂等重放必须复用同一 Basis identity；
9. 不同 base commit、context、plan 或 profile 的 Draft 不得混合 lineage。

### 4.3 `WritingTaskContract`

建议字段：

```yaml
writing_task_contract:
  contract_id: ...
  target_chapter: ...
  target_scenes: [...]
  pov: ...
  narrative_person: ...
  chapter_goal: ...
  scene_goals: [...]
  required_beats: [...]
  active_plan_obligations: [...]
  mandatory_constraints: [...]
  forbidden_reveals: [...]
  preserve_requirements: [...]
  style_requirements: [...]
  length_policy:
    minimum_characters: ...
    target_characters: ...
    maximum_characters: ...
  blocking_gaps: [...]
```

该对象必须由可信编译器或测试 fixture 提供，不由 Writer 自己从自由文本中声明。`blocking_gaps`
非空时返回 `NEEDS_CONTEXT`，不得为了完成正文自行补造事实。

### 4.4 `WriterInvocation`

```yaml
writer_invocation:
  invocation_id: ...
  run_id: ...
  task_id: ...
  mode: DRAFT
  basis: ...
  writing_task: ...
  context_package: ...
  input_artifacts: [...]
  prior_draft: null
  continuation_boundary: null
  rewrite_directive: null
  budget:
    max_model_calls: 1
    timeout_seconds: ...
    input_token_limit: ...
    output_token_limit: ...
```

并行首切片不接受在线工具调用，`max_model_calls=1`，`max_tool_calls=0`。

### 4.5 不可信 `WriterDraftPayload`

模型只输出：

```yaml
writer_draft_payload:
  draft_text: ...
  declared_memory_hints:
    - subject_hint: ...
      change_kind: ADD | CHANGE | END | UNCERTAIN
      predicate_hint: ...
      value_hint: ...
      evidence_quote: ...
      confidence: ...
  unresolved_questions: [...]
  self_observations: [...]
```

约束：

- 模型不提供 `base_commit`、`snapshot_id`、`context_id` 或 Artifact hash；
- 模型不提供 Canonical record ID；
- 模型不提供 Unicode offset；
- 模型不生成 `EvidenceRef`；
- 模型不生成 `ObservedChangeSet` 或 `CandidateChangeBundle`；
- `evidence_quote` 只用于后续可信定位，找不到或重复时记录 advisory finding，不能成为 Canon 证据；
- `self_observations` 不是 EditorialReport；
- `unresolved_questions` 不是已批准的 MemoryNeed。

当前 Curator 实测已经证明，让模型直接计算 offset 是错误职责分配。Writer 的弱变化提示必须沿用
同一安全原则：模型表达语义，可信服务处理身份、位置、hash 和 lineage。

### 4.6 可信 `DraftArtifact`

```yaml
draft_artifact:
  draft_id: ...
  mode: ...
  basis: ...
  text_artifact: ...
  sidecar_artifact: ...
  raw_output_artifact: ...
  parent_draft_id: null
  writer_receipt: ...
  model_call_ids: [...]
  created_at: ...
  candidate_only: true
```

Artifact 规则：

1. 原始模型 JSON、正文 UTF-8 bytes 和可信 sidecar 分别内容寻址；
2. 不对模型文本做静默 Unicode 或换行改写；
3. 后续进入 TextRoot Candidate 前由独立可信 normalizer 分块、计算 offset 和 quote hash；
4. 对象存储失败不得生成成功 Receipt；
5. 相同 Basis + 相同模型响应必须得到相同正文 Artifact hash；
6. 同一 idempotency identity 的重复执行不得发布两个逻辑不同的成功结果；
7. 旧 Draft 永不原地覆盖；CONTINUE/MAJOR_REWRITE 通过 parent lineage 连接。

### 4.7 `WriterExecutionResult`

建议 typed terminal：

```text
COMPLETED
NEEDS_CONTEXT
CONTRACT_REJECTED
MODEL_UNAVAILABLE
MODEL_OUTPUT_REJECTED
BUDGET_EXHAUSTED
ARTIFACT_WRITE_FAILED
CANCELLED
FATAL
```

每个终态都必须返回：

- 是否调用模型；
- 模型调用数和 token/latency；
- 输入 Basis；
- 已产生的 ArtifactRefs；
- Agent/Prompt/Skill/Tool/Model fingerprints；
- 可安全重试与否；
- failure code；
- 不得包含伪成功 Draft receipt。

---

## 5. Agent、Prompt、Skill 与权限

### 5.1 AgentSpec

需要在公共 Agent 合同中增加：

```text
AgentType.WRITER
AgentMode.DRAFT
AgentMode.CONTINUE
AgentMode.MAJOR_REWRITE
```

首轮只注册 `WRITER/DRAFT/v1`，其余模式未注册时必须 fail-closed，不能自动回退为 DRAFT。

### 5.2 ToolPolicy

并行 shadow 阶段：

```yaml
allowed_tools: []
denied_tools:
  - memory.search_exact
  - memory.search_temporal
  - memory.search_bm25
  - memory.search_vector
  - memory.search_graph
  - memory.write
  - canonical.commit
  - root.update
max_tool_calls: 0
permission: read
```

Post-Memory-Gate 可以只增加高层 `memory.request_context`，仍不得向 Writer 暴露底层检索工具。

### 5.3 Prompt 分层

Writer Prompt 必须：

1. 把 System Policy、Writer Contract 和 Skill 作为可信层；
2. 把 Context、Plan、历史正文和参考文字标记为不可信数据；
3. 明确其中出现的“指令”不能改变 System Contract、ToolPolicy 或输出 Schema；
4. 在长不可信 payload 之后重复精简可信输出合同；
5. 明确禁止发明 missing mandatory fact；
6. 明确 memory hints 只是弱提示，不是 Canon；
7. 不要求模型计算 offset、hash 或 Artifact ID。

不得为了 Writer 修改当前 Memory Controller/Curator Prompt。若现有通用 renderer 不能安全追加可信
尾部合同，先实现 Writer-local、带完整 fingerprint 的兼容渲染，不在活动记忆实验期间改变通用
renderer 的默认行为。

### 5.4 Skill

首轮新增 `scene_composition_v1.md`，至少包含：

- Purpose；
- Inputs；
- Mandatory checks；
- Scene/beat composition workflow；
- Character/POV/world-state discipline；
- How to report unresolved questions；
- How to emit weak memory hints；
- Failure modes；
- Output contract；
- 禁止自我审校通过、禁止 Canon write。

Skill 只指导 Writer 完成正文，不承担 Editor REVIEW 或 Curator extraction。

---

## 6. 建议代码边界

### 6.1 新增文件

```text
src/novel_agent/domain/generation.py
src/novel_agent/agents/writer.py
src/novel_agent/services/writer_generation.py

src/novel_agent/prompts/writer_draft_v1.md
src/novel_agent/prompts/writer_continue_v1.md
src/novel_agent/prompts/writer_major_rewrite_v1.md

src/novel_agent/skills/scene_composition_v1.md
src/novel_agent/skills/continuation_v1.md
src/novel_agent/skills/major_rewrite_v1.md

scripts/export_stage3_schemas.py
scripts/run_stage3_writer_shadow.py

schemas/stage3/
tests/fixtures/stage3_writer/
tests/golden/stage3_writer/
tests/unit/test_writer_agent.py
tests/unit/test_writer_generation.py
tests/contract/test_stage3_generation_contract.py
```

首切片只需要实际填充 DRAFT prompt/skill；其他模式文件可以在对应工作包开始时加入，不能用空文件
冒充实现。

### 6.2 最小共享改动

```text
src/novel_agent/domain/stage2.py
    只追加 WRITER 与三个 Writer Mode 枚举

src/novel_agent/agents/__init__.py
    只增加 Writer 公共导出
```

不得把 Writer 领域对象继续全部塞入 `domain/stage2.py`。

### 6.3 当前禁止修改的活动热区

```text
src/novel_agent/services/model_curation.py
src/novel_agent/services/evidence_support.py
src/novel_agent/services/memory_write_workflow.py
src/novel_agent/services/teacher_forced_benchmark_e2e.py
src/novel_agent/services/stage2_paired_pilot.py
src/novel_agent/runtime/memory_controller.py
src/novel_agent/adapters/memory_write/teacher_forced.py
scripts/run_stage2_teacher_forced_e2e.py
scripts/run_evidence_audit.py
reports/stage2a/**
benchmarks/private/**
```

不得新增数据库 migration。Writer Core 复用 ObjectStore、ArtifactRepository、ModelGateway 和现有
Receipt 合同；数据库持久化与顶层 RunEvent 接线留到 Integration Gate。

### 6.4 依赖方向

```text
writer shadow script / future runtime
               ↓
WriterAgent / WriterGenerationService
               ↓
domain.generation + existing ports
               ↓
ArtifactRepository / ModelGateway adapters
```

`domain/generation.py` 不得导入 LangChain、LangGraph、SQLAlchemy、OpenSearch 或供应商 SDK。

---

## 7. 并行开发隔离合同

### 7.1 Git 隔离

代码实现必须使用独立 worktree 和分支，建议：

```text
branch: codex/stage2b-writer-shadow
worktree: NS-stage2b-writer
```

不得在正在产生真实记忆实验提交的 `main` 工作树直接开发 Writer。实现开始时记录起始 commit，
合并前重新基于一个明确的安全 checkpoint 做 rebase 和全量回归。

本文作为设计文档可以进入主仓库，但 Writer 源码改动必须遵守上述 worktree 规则。

### 7.2 运行资源隔离

当前记忆实验使用：

```text
Qwen:       127.0.0.1:8002
Embedding:  127.0.0.1:8081
Reranker:   127.0.0.1:8082
PostgreSQL: 127.0.0.1:5432
OpenSearch: 127.0.0.1:9200
reports:    reports/stage2a/**
```

Writer shadow 阶段必须：

- 设置 `NOVEL_AGENT_FORBID_MODEL_CALLS=true`；
- 只用 FakeModelEndpoint 或 ScriptedModelEndpoint；
- 不访问 8002/8081/8082；
- 不访问 5432/9200；
- 使用测试临时目录中的 FilesystemObjectStore；
- 不写 `reports/stage2a/**`；
- 新运行的 shadow 输出只写 `reports/stage3/writer_shadow/<run_id>/` 或测试临时目录；
- 不把真实 benchmark 的 future/Gold 复制为 Writer fixture。

### 7.3 测试隔离

活动记忆实验期间只运行：

```text
Writer 专项 unit
Writer 专项 contract
Writer schema export
针对新增文件的 Ruff/Mypy
git diff --check
```

当前阶段不运行：

```text
真实模型测试
native integration
完整 teacher-forced
stage2r gate/backfill
会停止或重启共享基础设施的 Make target
```

全仓 `make quality` 在合并前的固定窗口运行，不与正式模型回放争抢 CPU、文件和工作树状态。

---

## 8. 工作包

### W0：边界冻结与有限例外记录

任务：

1. 确认本文为 Writer shadow 的唯一执行合同；
2. 记录“允许隔离实现、不允许真实接线”的有限阶段例外；
3. 冻结禁止修改路径和共享端口；
4. 创建独立 worktree/branch；
5. 记录起始 commit 和当前 Memory Gate 状态。

交付：

- 本文；
- 分支基线记录；
- Writer scope/ownership 清单。

退出条件：

- 任何人都能明确区分 shadow engineering Gate 与正式 Stage 3 Gate。

### W1：领域合同与 Schema

任务：

1. 实现第 4 节领域对象；
2. 实现三模式互斥验证；
3. 实现 Basis 一致性验证；
4. 实现 future/evaluator taint 拒绝；
5. 实现 typed terminal；
6. 创建独立 Stage 3 schema exporter；
7. 生成 deterministic golden schema。

退出条件：

- unknown field、错误 mode、basis mismatch、非法 input combination 全部 fail-closed；
- Stage 0/1/2 既有 Schema 不发生无关变化。

### W2：DRAFT AgentSpec、Prompt、Skill

任务：

1. 注册 `WRITER/DRAFT/v1`；
2. 固定 input/output schema refs；
3. 固定 prompt/skill hash；
4. ToolPolicy 为零工具；
5. 实现长 payload 后可信输出契约；
6. 写 prompt injection 与 source-data 指令隔离测试。

退出条件：

- prompt、skill 或 policy hash 不符时在模型调用前失败；
- raw retrieval 和 Canon write 没有可达入口。

### W3：Writer DRAFT Core

任务：

1. 复用 `StructuredAgentRunner.prepare/execute/receipt`；
2. 在调用前验证 Artifact binding 和 Basis；
3. 通过 fake model 生成 `WriterDraftPayload`；
4. 原样保存 raw response 与正文 Artifact；
5. 可信构造 sidecar 和 `DraftArtifact`；
6. 生成包含所有 input/output ArtifactRef 的 receipt；
7. 实现 idempotency；
8. 对模型、Schema、Artifact 失败返回 typed terminal。

退出条件：

- 相同输入/响应产生相同内容 hash；
- 失败不产生伪成功 Artifact/Receipt；
- 整条路径没有 Canon、DB、Search 调用。

### W4：CONTINUE 与 MAJOR_REWRITE

任务：

1. 实现 prior draft lineage；
2. CONTINUE 绑定冻结前缀和 continuation boundary；
3. 验证续写结果未静默覆盖冻结前缀；
4. MAJOR_REWRITE 绑定 frozen rewrite directive；
5. 保留旧 Draft，不做原地覆盖；
6. 禁止把 Editor LOCAL_REPAIR 当作 Writer MAJOR_REWRITE；
7. 为两模式增加独立 AgentSpec/Prompt/Skill。

退出条件：

- 三模式正向、负向和重放测试完整；
- 每个结果可沿 parent lineage 回到输入 Draft。

### W5：Fake/Offline Shadow Harness

任务：

1. 读取 frozen `Stage1ContextPackage` fixture；
2. 读取 WritingTaskContract fixture；
3. 使用 fake/scripted output；
4. 输出 Draft、sidecar、receipt 和 manifest；
5. 校验所有输入输出 hash；
6. 支持故障注入；
7. 明确报告标签为 `engineering_only=true`。

退出条件：

- 无网络、无数据库、无 OpenSearch 即可完全重放；
- 不读取 future/Gold；
- shadow manifest 不能被正式 Gate 聚合器误认为质量证据。

### W6：验证与交付冻结

任务：

1. Writer 专项单元/合同测试；
2. branch coverage 100%；
3. Ruff、format、Mypy；
4. Schema export 二次运行无 diff；
5. Artifact tamper 测试；
6. timeout/cancel/retry/idempotency 测试；
7. 全仓质量门禁；
8. 生成 `Writer Core Engineering Acceptance`。

退出条件：

```text
Writer Engineering Gate = PASS
Memory Integration Gate = PENDING
Writer Semantic Gate = NOT_RUN
Production Gate = BLOCKED
```

### W7：Post-Memory-Gate Integration

本工作包当前只定义，不执行。前置条件和任务见第 11 节。

---

## 9. 测试矩阵

### 9.1 Domain / Contract

| Case | 预期 |
|---|---|
| unknown field | schema reject |
| 缺 base commit/snapshot/context | schema reject |
| Context base commit 不一致 | 模型调用前 reject |
| Context snapshot 不一致 | 模型调用前 reject |
| future/evaluator/gold taint | information-boundary reject |
| DRAFT 携带 prior draft | mode-contract reject |
| CONTINUE 无 prior draft | mode-contract reject |
| MAJOR_REWRITE 无 directive | mode-contract reject |
| 未注册 Mode | registry reject，无 fallback |
| blocking gap 非空 | `NEEDS_CONTEXT`，零模型调用 |

### 9.2 Agent / Prompt / Skill

| Case | 预期 |
|---|---|
| prompt hash mismatch | 模型调用前 reject |
| skill hash mismatch | 模型调用前 reject |
| ToolPolicy hash mismatch | 模型调用前 reject |
| source text 内含伪 system instruction | 仍遵守 trusted output contract |
| 模型篡改 trusted IDs | 忽略模型值或 schema reject |
| 模型输出 EvidenceRef/Commit 字段 | extra-forbid reject |
| raw retrieval 请求 | policy reject |

### 9.3 Artifact / Lineage

| Case | 预期 |
|---|---|
| 相同正文 bytes | 相同 text Artifact hash |
| 正文一字变化 | 不同 hash |
| Artifact 被篡改 | read verification failure |
| sidecar write 失败 | 不产生 COMPLETED |
| raw output write 失败 | 不产生 COMPLETED |
| 重复 idempotency key + 同输入 | 返回相同逻辑结果 |
| 重复 key + 不同输入 | conflict reject |
| CONTINUE/REWRITE | parent draft lineage 完整 |

### 9.4 Model / Failure

| Case | 预期 |
|---|---|
| timeout | typed model terminal |
| transport failure | typed suspension/failure，无 silent fallback |
| invalid JSON | structured output rejected |
| empty draft | output rejected |
| 预算耗尽 | `BUDGET_EXHAUSTED` |
| cancel | `CANCELLED`，无伪成功 |
| retry | 每次调用都有 ledger evidence |

### 9.5 权威与副作用

必须用 spy/fake 证明一次 Writer run 的副作用集合严格等于：

```text
ModelCallLedger
+ candidate Artifact writes
+ Writer receipt / shadow manifest
```

必须严格为 0：

```text
Canonical Root writes
Commit requests
MemoryWriteWorkflow requests
OpenSearch writes/reads
PostgreSQL canonical writes
Projection/alias changes
ObservedChangeSet
CandidateChangeBundle
```

---

## 10. Writer Engineering Gate

只有同时满足下列条件，Writer Core 才可标记为
`IMPLEMENTED_IN_ISOLATION`：

1. 新增领域对象 strict、frozen、extra-forbid；
2. 三模式互斥规则全部覆盖；
3. Basis 和 future isolation fail-closed；
4. fake model shadow run 可重复；
5. 所有正文和 sidecar 内容寻址；
6. prompt/skill/tool/model/config fingerprints 完整；
7. 模型不能提供可信 offset、EvidenceRef 或 Canon ID；
8. hints 不能进入 MemoryPatch/ChangeBundle；
9. Writer 没有 raw retrieval、DB、Search、Commit 权限；
10. typed failure 不产生伪成功 receipt；
11. 专项测试和全仓质量门禁通过；
12. 既有 Stage 0/1/2 Schema 和回归无非预期变化；
13. 当前记忆实验目录、端口、数据库和索引未被访问；
14. Acceptance 明确写 `semantic_quality_not_evaluated=true`；
15. 未把 shadow 输出加入正式 Evaluation Ledger。

该 Gate 通过仍不允许合并到生产章节主图。

---

## 11. Post-Memory-Gate Integration 与正式质量实验

### 11.1 Integration Gate 前置条件

至少满足：

1. Memory Kernel 有正式 PASS 或 CONDITIONAL PASS；
2. deterministic Memory Gateway v0.1 可冻结并保留为安全默认；
3. C20/C40/C60/C80/C95 连续 receipt chain 完整，或正式 Gate 明确接受替代范围；
4. Future Isolation、Canon pollution、Freshness 没有未关闭 P0；
5. 当前真实回放停止在安全 checkpoint；
6. Writer 分支基于该 checkpoint rebase；
7. 当前 Writer 输入所需 Context/Plan/Profile adapter 形成正式合同；
8. 独立评测方案和阈值预注册。

Agentic Controller 可以继续 DEFER；Writer Integration 不得要求它成为默认路径。

### 11.2 接线顺序

```text
Frozen Plan/Scene Contract
        +
Deterministic Memory Gateway
        ↓
Context Compiler / Freeze
        ↓
Writer Core
        ↓
DraftArtifact
        ├── Draft Deterministic Checks
        ├── Editor REVIEW
        └── Curator Independent Extraction
                ↓
Declared vs Observed Reconciliation
                ↓
现有 Validation / MemoryWrite / Commit 链
```

先只接 DRAFT。CONTINUE 与 MAJOR_REWRITE 在 Editor verdict 和 rewrite contract 稳定后接入。

### 11.3 Stage 3 正式三臂实验

固定：

- 同一 Writer 模型；
- 同一模型版本；
- 同一采样参数；
- 同一 WritingTaskContract；
- 同一输出长度和调用预算；
- 同一 evaluator；
- 同一公开输入边界。

只替换上下文：

```text
Arm A: 最近章节
Arm B: Naive RAG
Arm C: Memory Kernel ContextPackage
```

评测不要求复现原小说未来正文，重点比较：

- 当前状态一致性；
- 人物、时间、位置、物品、关系和世界规则冲突；
- Plan Obligation Coverage；
- Mandatory Constraint Violation；
- POV/Disclosure/Future Leakage；
- unresolved question 与事实编造；
- Writer declared hint 的误报、漏报和 truth-type mismatch；
- Editor verdict；
- repair / rewrite 轮次；
- token、模型调用、延迟和成本；
- 文学质量非劣性。

### 11.4 正式晋升规则

在真实实验前预注册具体样本量和阈值。最低语义规则：

1. Future Leakage 必须为 0；
2. Canon pollution 必须为 0；
3. Arm C 的 mandatory constraint 不能低于 Arm A；
4. Arm C 的状态一致性和计划遵循至少预注册非劣于 A/B；
5. 增益不能来自不同模型、更大预算或未来数据；
6. 文学质量不能为了硬一致性出现不可接受退化；
7. Writer hints 不得作为独立 Curator 的替代；
8. 失败必须能回退到未提交 Draft，不得污染 Canon。

只有独立 Evaluation Ledger 给出正式结论后，才能改变：

```text
Writer Semantic Gate
Production Gate
Stage 3 状态
```

---

## 12. 风险、停止条件与回退

| 风险 | 触发 | 处理 |
|---|---|---|
| 当前主线继续快速提交 | Writer 分支基线漂移 | 不追逐每次提交；在安全 checkpoint 集中 rebase |
| 共享枚举引起 Schema diff | Stage 2 Schema 非预期变化 | 独立 Stage 3 exporter；只接受预期 enum 增量 |
| Prompt 长上下文指令衰减 | 输出合同被忽略 | 不可信 payload 后重复可信最小合同 |
| Writer hints 被误作 Canon | 出现 EvidenceRef/ChangeBundle 直通 | hard fail，阻断合并 |
| Writer 真实调用争抢 8002 | endpoint receipt 出现 8002 | 立即停止 shadow run，作隔离事故处理 |
| 误读 future/Gold | taint 或 source id 命中 | hard fail，隔离并封存事故证据，不进入正式评测或候选链 |
| Artifact 部分写入 | sidecar/text 不完整 | 非 COMPLETED；按幂等 key 安全重试 |
| 过早接主图 | 出现 Curator/Commit import | 阻断 PR，退回独立模块 |
| Memory Gate 最终失败 | Context 合同需要重构 | 保留 Writer 领域/Artifact层，重做 adapter，不晋升 |

必须停止并重新评审的条件：

- Writer Core 需要修改当前 Curator/MemoryWrite 主链才能完成；
- Writer 需要直接访问底层 Retrieval Tool；
- Writer 必须生成 Canonical EvidenceRef 才能工作；
- 隔离测试无法避免使用当前 8002/5432/9200；
- Memory Gate 发现 `Stage1ContextPackage` 或信息边界需破坏性改版；
- 需要改变五 Root、Commit 或 Freshness 核心不变量。

回退方式：

1. 不注册 Writer AgentSpec；
2. 不导入 Writer runtime；
3. 删除/禁用 Stage 3 feature flag；
4. 保留不可变 shadow artifacts 作为失败证据；
5. 不需要回滚任何 Canon，因为本模块无 Canon write。

---

## 13. 建议 Issue 拆分

```text
S3-W00  Freeze Writer shadow boundary and isolation contract
S3-W01  Add Writer generation domain contracts and schemas
S3-W02  Add WRITER/DRAFT AgentSpec, ToolPolicy, prompt and skill
S3-W03  Implement trusted Writer ArtifactBasis validation
S3-W04  Implement DRAFT generation and DraftArtifact materialization
S3-W05  Implement typed Writer failures and idempotency
S3-W06  Add fake/offline shadow runner and manifests
S3-W07  Add Writer unit/contract/golden/fault tests
S3-W08  Implement CONTINUE lineage and frozen-prefix checks
S3-W09  Implement MAJOR_REWRITE directive and lineage
S3-W10  Produce Writer Core Engineering Acceptance
S3-W11  Post-Memory-Gate Context adapter
S3-W12  Post-Memory-Gate three-arm generation benchmark
```

当前并行范围为 `S3-W00` 至 `S3-W10`；`S3-W11` 和 `S3-W12` 保持 BLOCKED。

---

## 14. 最终 Definition of Done

本轮完成时，项目应能够准确声明：

> Writer 的合同、三模式边界、内容寻址 DraftArtifact、权限、失败语义和 fake/offline
> 执行链已经完成工程验证；它没有接入在线 Memory Gateway、Editor、Curator 或 Canon，
> 没有占用真实记忆实验资源，也没有产生任何 Writer/Memory 质量晋升结论。

不得声明：

> Writer 已可生产使用；Stage 3 已通过；Memory ContextPackage 已提升生成质量；Writer 的
> memory hints 已能正确写回；完整章节闭环已完成。

下一次状态迁移只能是：

```text
IMPLEMENTED_IN_ISOLATION
    -- Memory Integration Gate PASS -->
MEMORY_INTEGRATED_EXPERIMENTAL
    -- Stage 3 Semantic + Safety Gate PASS -->
QUALITY_PROMOTED
    -- Production Gate PASS -->
PRODUCTION_ENABLED
```

这条状态机保证并行开发真正缩短后续路径，同时不把正在验证的 Memory Kernel、真实运行证据和
Canonical 安全边界混入尚未成熟的生成模块。
