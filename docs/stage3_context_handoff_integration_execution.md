# Stage 3 Context Handoff 与正式集成开发执行

> 文档生命周期：`SUPERSEDED`
>
> 后继：`docs/stage3_writer_context_loop_execution.md`
>
> 工作流：A
>
> 执行状态：`NOT_STARTED / WAITING_FOR_INTEGRATION_WINDOW`
>
> 实现负责人：`UNASSIGNED`
>
> 日期：2026-07-31
>
> 上位设计：`docs/stage3_writer_core_overall_design.md`
>
> 职责边界：本文负责代码开发和开发者自检；独立验收按 Stage 3 总验收测试文档执行

## 1. 目标

在 Stage 2M 和 Stage 3 两侧可以独立开发的前提下，最后用一个小型 adapter 收敛
`WriterContextPackage` 与 Writing Core 的 handoff，并接通不写 Canon 的最小生成链。

完成后的代码路径为：

```text
WriterContextPackage
→ Stage 3 handoff/adapter
→ Writer
→ Editor
→ Curator observation
→ ReconciliationResult
```

本文不是 B/C/D 的前置条件。工作流 A 在 Stage 2M 接口基本稳定、B/C 基本完成后启动。

## 2. 启动条件

启动 A 只要求：

- Stage 2M 已提供本轮准备集成的 `WriterContextPackage` 合同；
- B 已提供可调用的 Writer 入口和 `DraftArtifact`；
- C 已提供 Editor 与变化对账入口；
- D 已提供最小实验 runner 所需的调用接口；
- 各工作流的变更已经形成可评审的 Git 提交。

这里的“接口基本稳定”不等于 Stage 2M 所有质量指标已经通过，只表示当前集成周期没有已知的
破坏性合同变更。

### 2.1 集成时读取的实际接口

A 开始时应直接读取当时主线代码，不从本文推测字段。至少核对：

| 来源 | A 需要确认的内容 |
|---|---|
| `domain/writer_context.py` | Writer 实际可见正文、task、basis、gaps 和 budget status |
| `domain/generation.py` | Writer 调用所需输入、模式和候选 Draft 输出 |
| Writer Service | 调用方式、失败结果和 artifact 产生时机 |
| Editor/Reconciliation Service | Draft 输入、verdict、repair 和对账入口 |
| D 的 evaluation runner | 最小链需要返回的统一结果 |

核对结果只形成当前集成分支上的代码调整和简短 PR 说明，不另建接口审计数据库或长期同步清单。

## 3. 开发范围

需要实现：

- 从 `WriterContextPackage` 到 Writing Core 输入的单一 adapter；
- task、target range、basis 和 context readiness 的必要边界检查；
- legacy `Stage1ContextPackage` handoff 的替换或明确隔离；
- Writer、Editor、Curator observation 和对账服务的最小串联；
- fake/offline 集成入口；
- 少量跨模块合同与集成测试。

不实现：

- Stage 2M 检索、评价或 benchmark 修复；
- 完整章节 TaskGraph；
- Planner replan；
- MemoryPatch、Commit 或 Canon 写入；
- 新的通用集成框架、运行平台或数据库表；
- 正式真实模型实验和阶段验收。

## 4. 文件所有权

优先由 A 修改：

```text
src/novel_agent/services/writer_draft_integration.py
src/novel_agent/services/ 下新增的 Stage 3 handoff/integration 模块
scripts/ 下最小 offline integration 入口
tests/contract/ 下 Stage 3 handoff 合同测试
tests/integration/ 下 Stage 3 最小链测试
```

`domain/generation.py` 和 Writer 主体由 B 拥有，Editor/对账模块由 C 拥有，Stage 2M 合同由
Stage 2M 工作流拥有。A 发现接口问题时通过小型接口提交协调，不在本分支复制或重写对方模型。

## 5. 实现步骤

1. 对照两侧当前公开入口，列出实际需要转换的字段和状态，不预先设计 v2 合同；
2. 实现只做边界转换和必要校验的 adapter；
3. 将 B/C 的调用入口串成最小候选链；
4. 保证 failure 在当前节点结束，不产生伪成功 Draft 或对账结果；
5. 替换项目面对外的 legacy `stage2b`/`Stage1ContextPackage` 集成入口；
6. 使用冻结 fixture 完成一次 offline 串联；
7. 将接口限制和未覆盖路径写入 Git 交接说明。

adapter 不承担检索、压缩、证据重评或语义修复。需要这些能力时应返回已有的明确失败状态，而不是
继续扩大工作流 A。

### 5.1 Handoff 语义

adapter 至少完成以下语义映射：

| Writer Context 内容 | Stage 3 使用方式 |
|---|---|
| task/target 信息 | 校验本次 Writing Task 与 Context 面向同一目标 |
| basis commit/snapshot | 绑定本次候选生成的读取基线 |
| `rendered_context` 和结构化 sections | 形成 Writer 可见上下文，不重新检索 |
| gaps | 区分可以显式带入正文创作的不确定性和必须停止的缺口 |
| budget/final status | 判断 Context 是否可以进入 Writer |
| evidence ledger ref | 保留追溯引用，但默认不展开进 Writer Prompt |

不要求 Stage 2M 为 A 改成新的 v2 Schema。字段名称不同但语义相同的情况在 adapter 内转换；语义
确实缺失时才回到所属工作流讨论公共合同。

### 5.2 最小链执行语义

一次集成调用按以下顺序执行：

1. 接收 Writing Task、Writer Context 和运行参数；
2. 验证 target、basis 和 Context 状态；
3. 组装 Writer 本地调用并生成 candidate Draft；
4. Draft 成功后调用 Editor 与 Curator observation，第一版不要求优化并发；
5. Editor 若为 `LOCAL_REPAIR`，产生新候选并重新执行受影响的 observation/对账；
6. Editor 若为 `MAJOR_REWRITE`，返回明确路由结果，不在 A 内自动无限重试；
7. 汇总 Draft、EditorialReport 和 ReconciliationResult；
8. 任一关键节点失败时返回该节点失败，不调用后续生产提交路径。

A 只接通业务顺序，不新建通用 DAG、状态机或调度平台。若现有简单 Service 调用足够，就不引入
LangGraph。

### 5.3 失败处理

| 情况 | 预期处理 |
|---|---|
| task 与 Context target 不一致 | Writer 调用前拒绝 |
| basis commit/snapshot 不一致 | Writer 调用前拒绝 |
| Context budget/status 不允许生成 | 返回 Context 侧失败 |
| blocking gap 无法满足任务 | 不调用 Writer |
| Writer 失败 | 不调用 Editor/对账，不产生伪成功 Draft |
| Editor 失败 | 保留候选 Draft，集成结果标记未完成 |
| Reconciliation 失败 | 不影响 Draft 存在性，但不得宣称最小链完成 |
| 意外触达 Memory write/Commit/Canon | 立即视为实现缺陷并停止交接 |

失败类型优先复用 B/C 已有结果，不为每种集成异常建立一套新的 terminal hierarchy。

## 6. Git 执行方式

建议分支：

```text
codex/stage3-context-integration
```

执行要求：

- 从包含 B/C/D 可集成提交的共同基线创建独立 worktree；
- 不在 Stage 2M 或 Writing Core 的开发 worktree 中直接开发；
- 提交按“adapter”“minimal flow”“integration tests”保持少量、单一目的；
- 不 squash 或改写其他工作流的历史；
- 合并前做一次必要 rebase，解决后的冲突必须由对应文件所有者复核；
- 不建立额外 commit hash 清单，Git 历史本身就是版本依据。

## 7. 开发者自检

开发者负责最低限度的工程自检：

- handoff 正向和关键拒绝路径的专项测试；
- fake/offline 最小链一次；
- 修改路径的 Ruff 和 MyPy 检查；
- `git diff --check`；
- 确认运行未触发 Memory 写入、Commit 或 Canon 调用。

开发者不运行正式生成实验，不签发 Stage 3 PASS，也不以专项测试替代独立验收。

建议自检场景：

| 场景 | 最低预期 |
|---|---|
| ready Context + DRAFT | 返回 Draft、EditorialReport、ReconciliationResult |
| target mismatch | 零模型调用 |
| basis mismatch | 零模型调用 |
| blocking gap | 明确停止 |
| Writer typed failure | 不继续 Editor |
| Editor LOCAL_REPAIR | 新 Draft 保留父 Draft |
| Editor MAJOR_REWRITE | 返回路由，不自动循环 |
| spy Commit/Canon port | 调用数为 0 |

场景可集中在少量参数化测试中，不要求一行预期对应一个测试文件。

## 8. 交接

向独立验收负责人交付：

- Git 分支和待验收提交；
- 变更文件及公共接口摘要；
- 已运行的命令和结果；
- 尚未覆盖的限制；
- 与 B/C/D 或 Stage 2M 仍存在的已知差异。

交接使用一段简短的 PR/任务说明即可，不新增 handoff manifest、文件哈希表或重复验收文档。

## 9. 开发完成条件

- 正式 handoff 已使用 `WriterContextPackage`；
- 最小候选链可由 fake/offline 输入运行；
- legacy handoff 不再是 Stage 3 默认入口；
- 没有新增 Canon、Memory 写入或生产 Commit 权限；
- 代码和最小自检结果已形成可独立审核的 Git 提交。

达到这些条件只表示工作流 A 已完成开发交接，不表示 Stage 3 已验收。
