# Stage 3 Writing Core 迁移与完善开发执行

> 文档生命周期：`ACTIVE`
>
> 工作流：B
>
> 执行状态：`NOT_STARTED / READY_FOR_ASSIGNMENT`
>
> 实现负责人：`UNASSIGNED`
>
> 日期：2026-07-31
>
> 上位设计：`docs/stage3_writer_core_overall_design.md`
>
> 职责边界：本文负责代码开发和开发者自检；独立验收按 Stage 3 总验收测试文档执行

## 1. 目标

以现有 legacy worktree 中的 Writing Core 为实现基线，将 Writer 三模式、候选
`DraftArtifact`、Prompt/Skill、typed failure 和 offline runner 迁入当前 Stage 3 代码结构。

本工作流的重点是迁移、减负和适配，不重新开发一套 Writer。

## 2. 开发起点

主要输入位于：

```text
/home/cuihengjia/agent/novel/NS-stage2b-writer
```

现有隔离实现已验证 `DRAFT`、`CONTINUE`、`MAJOR_REWRITE`、candidate-only artifact 和零
Canon 权限。B 应优先保留这些可用行为，只调整：

- `stage2b` 项目命名；
- 与当前主线冲突的公共枚举和导出；
- 对 `Stage1ContextPackage` 的直接耦合；
- 已被当前基础设施替代的重复代码；
- 重复、过细或只验证实现细节的测试。

Stage 2M 不需要等待 B。B 前期使用冻结 Writer Context fixture 或本地 handoff contract。

### 2.1 可直接复用的实现

| legacy 文件/资产 | 迁移判断 |
|---|---|
| `domain/generation.py` | 作为 Writer/Draft 合同主体，按当前主线依赖减负 |
| `agents/writer.py` | 保留三模式和可信 Prompt 分层 |
| `services/writer_generation.py` | 保留候选 materialization 与 typed failure |
| `services/writer_draft_integration.py` | 只作参考，正式 handoff 由 A 后置完成 |
| `prompts/writer_*.md` | 迁移并按当前 Context 表达复核 |
| Writer skills | 迁移，避免把 Editor/Curator 职责写入 Writer |
| schema exporter/shadow runner | 改为 Stage 3 namespace 并删去重复机制 |
| 已有 Writer 测试 | 迁移核心行为、删除重复和字段级过拟合 |

### 2.2 当前已知冲突

- legacy code 直接导入 `Stage1ContextPackage`；
- `AgentType`/`AgentMode` 修改会带动 Stage 2 公共 Schema 变化；
- legacy schema、脚本、fixture 和测试使用 `stage2b`；
- 部分 Basis/fingerprint 检查可能重复现有 Artifact/Agent 基础设施；
- worktree 变更未提交，不能作为其他工作流稳定依赖。

B 要逐项解决这些真实冲突，但不把它们扩展成新的平台工程。

## 3. 开发范围

需要实现：

- Stage 3 Writer/Draft 公共合同；
- `DRAFT`、`CONTINUE`、`MAJOR_REWRITE` 三种模式；
- candidate-only `DraftArtifact` 和父 Draft 关系；
- Writer Agent、生成 Service、Prompt/Skill；
- 模型输出到候选正文和 sidecar 的可信转换；
- typed failure 和安全重试边界；
- Stage 3 schema/export 与 fake/offline runner；
- 迁移并删重后的专项测试。

不实现：

- `WriterContextPackage` 正式 adapter；
- Editor、Curator 或变化对账；
- Memory 检索、Memory 写入、Commit 或 Canon；
- 正式模型质量实验；
- 新 Harness、Artifact Store、模型网关或配置平台；
- 为 Prompt、Skill 或测试结果增加人工 hash 门禁。

已有内容寻址和 receipt 能直接复用时继续复用，不围绕它们扩建新的验收体系。

### 3.1 三种模式的实现边界

| Mode | 必要输入 | 必须保持的行为 | 不做 |
|---|---|---|---|
| `DRAFT` | Writing Task + 冻结 Context | 产生新的 candidate Draft | 不接受 prior draft 冒充初稿 |
| `CONTINUE` | prior Draft + 续写边界 | 保留既有前缀并产生子 Draft | 不原地追加或覆盖父 Draft |
| `MAJOR_REWRITE` | prior Draft + rewrite directive | 产生新的结构性重写候选 | 不执行 Editor 的局部修补职责 |

三模式共享一个 Writer 主体和基础输出合同，通过 mode、Prompt/Skill 和输入校验区分，不复制三个
Generation Service。

### 3.2 Writer 可见与不可见信息

Writer 可以看到 Writing Task、Writer-safe Context、必要 prior Draft 和明确指令。Writer 不应
直接看到：

- 底层 Retrieval Tool；
- evaluator-only/Gold/future 内容；
- Canon 数据库连接；
- MemoryPatch 或 Commit Service；
- 要求模型生成的可信 Artifact ID、EvidenceRef 或数据库主键。

Writer 输出中的 hints、unresolved questions 和 self observations 都是弱信号。它们可以进入 C
的对账，但不能直接提升为正式记忆变更或审校结论。

## 4. 文件所有权

B 优先拥有：

```text
src/novel_agent/domain/generation.py
src/novel_agent/agents/writer.py
src/novel_agent/services/writer_generation.py
src/novel_agent/prompts/writer_*.md
src/novel_agent/skills/ 下 Writer skills
schemas/stage3/
scripts/export_stage3_schemas.py
scripts/run_stage3_writer_shadow.py
tests/ 下 Writer 专项 fixture、unit 和 contract
```

共享枚举或公共导出确需修改时由 B 单点完成。B 不修改 Stage 2M evaluator、Memory Controller、
Curator 写回主链或 Editor 模块。

## 5. 实现步骤

1. 将 legacy Writing Core 整理为少量可阅读的 Git 提交；
2. 迁移代码并解决与当前主线的真实冲突；
3. 把公开 `stage2b` namespace 改为 `stage3`，历史分支和 worktree 名不强制改写；
4. 用冻结 fixture/local handoff 隔离对旧 Context 类型的直接依赖；
5. 保留三模式、candidate-only、权限和失败语义；
6. 删除迁移过程中出现的重复 adapter、重复 schema 和重复测试；
7. 生成必要的 Stage 3 公共 Schema；
8. 运行专项自检并形成开发交接。

不因为旧实现已有较多字段，就要求所有字段继续成为 Stage 3 公共合同。只保留当前执行真正使用或
安全边界真正需要的内容。

### 5.1 迁移批次

建议分三个可独立评审的批次：

#### 批次一：合同和 namespace

- 迁移 `generation.py`；
- 确认三模式和候选 Draft 的最小公共字段；
- 将 schema/export/test namespace 改为 `stage3`；
- 处理共享枚举变化，确认没有无关 Stage 2 Schema diff。

#### 批次二：Agent 和生成 Service

- 迁移 Writer Agent、Prompt/Skill 注册和生成 Service；
- 将 Context 输入换成 local handoff abstraction/fixture；
- 复用现有 ModelGateway、ArtifactRepository 和 runner；
- 保留 typed failure，但删除重复封装和未使用指纹检查；
- 确认失败不会返回成功 Draft。

#### 批次三：三模式、runner 和测试

- 完成 DRAFT、CONTINUE、MAJOR_REWRITE；
- 迁移 offline shadow runner；
- 迁移并压缩已有测试；
- 为 A/C/D 提供稳定 fixture 与调用示例。

### 5.2 失败与重试

Writer 至少区分：

- 输入合同不合法；
- Context 不足或不可用；
- 模型不可用/超时；
- 模型输出不符合 Writer 输出合同；
- 候选 Artifact 写入失败；
- 调用取消。

重试只处理明确可重试的模型或存储错误。合同错误、mode 错误和 basis 冲突不自动重试；也不为
重试开发新的持久化调度系统。

## 6. Git 执行方式

建议分支：

```text
codex/stage3-writing-core
```

执行要求：

- legacy worktree 中的未提交实现先形成可审阅提交，再迁移到 Stage 3 分支；
- 不把 Stage 2M 未完成改动混入 B 的提交；
- 提交建议按“contracts”“writer runtime”“namespace/tests”划分；
- 使用 Conventional Commit，提交内容保持单一目的；
- 合并前只做一次必要 rebase，不持续追逐 Stage 2M；
- 不 force-push 已供其他工作流使用的共享提交；
- Git 提交记录版本，不另建人工指纹和文件哈希清单。

## 7. 开发者自检

B 的最低自检范围：

- 三种 Writer 模式的核心正向路径；
- 非法模式输入、空正文、模型失败和候选写入失败；
- continuation/rewrite 不覆盖父 Draft；
- Writer 无底层检索、Memory 写入、Commit 和 Canon 入口；
- schema exporter 一次；
- 修改路径的 Ruff、MyPy 和 `git diff --check`。

测试优先迁移已有用例并删重。开发者不为每个字段创建独立测试文件，也不运行正式真实模型实验。
全仓质量和阶段结论由独立验收负责人执行。

建议将测试集中为以下行为组：

| 行为组 | 代表场景 |
|---|---|
| Domain | mode 输入互斥、父 Draft 关系、candidate-only |
| Writer Agent | Prompt/Skill 选择、无工具策略、结构化输出 |
| Generation | 正常 materialization、空正文、模型失败、存储失败 |
| Continue/Rewrite | 前缀保留、父子关系、旧 Draft 不覆盖 |
| Permission | raw retrieval、Memory write、Commit、Canon 均不可达 |
| Migration | Stage 3 schema/export 正常，无新公开 `stage2b` |

每组可以由参数化用例覆盖多个相近输入，不要求复制 unit、contract、golden 和 regression 四份
同义测试。

## 8. 交接

交付内容：

- Git 分支和提交；
- legacy 文件到 Stage 3 文件的简短映射；
- Writer/Draft 公开入口；
- 专项自检命令与结果；
- 有意删除或暂缓的 legacy 行为；
- A/C/D 使用 fixture 或公共合同的方法。

不生成单独的 Writer Engineering Acceptance 文档。验收证据集中进入 Stage 3 总验收结果。

## 9. 开发完成条件

- 当前主线候选中存在 Stage 3 Writing Core；
- 三模式和 candidate-only 边界保持；
- legacy `stage2b` 不再作为新的公开 namespace；
- B 可在不等待 Stage 2M 的情况下使用 fixture 完成 offline 运行；
- 代码、自检和已知限制已通过 Git 交接。

达到这些条件只表示 B 已完成开发，不表示 Writing Core 已被独立验收。
