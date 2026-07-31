# Stage 3 总验收测试执行

> 文档生命周期：`ACTIVE`
>
> 执行状态：`NOT_STARTED / WAITING_FOR_INTEGRATION_CANDIDATE`
>
> 验收负责人：`UNASSIGNED`
>
> 日期：2026-07-31
>
> 上位设计：`docs/stage3_writer_core_overall_design.md`
>
> 输入：A/B/C/D 四个开发工作流的 Git 提交和交接说明
>
> 职责边界：本文负责独立测试、审核和阶段结论，不负责替开发工作流实现功能

## 1. 目的

本文件是 Stage 3 唯一的总验收测试执行文档。它验证：

1. 四个开发工作流是否按总设计完成；
2. Writing Core、Editor、变化对账和 Context handoff 能否形成最小候选链；
3. 权限和 candidate-only 边界是否保持；
4. 工程质量是否满足当前仓库要求；
5. 真实生成实验是否给出可信、可解释的结果。

验收只形成一个阶段级结论，不为 A/B/C/D 分别建立多层 Gate、Acceptance Manifest 或重复报告。

## 2. 职能分离

验收负责人不得同时作为被验收功能的主要实现负责人。多人或多 agent 协作时：

- B/C/D 负责并行功能开发；
- A 负责后置集成；
- 验收负责人在独立 worktree/branch 中读取候选代码；
- 验收负责人可以新增黑盒测试和验收 fixture；
- 生产代码缺陷退回对应开发工作流修复；
- 验收负责人不在验收分支顺手重写 Writer、Editor、adapter 或评价 Service。

团队人数不足时，至少保证验收发生在代码交接之后，并由不同人员或不同 agent 在独立上下文中
执行。开发者的专项自检结果只能作为输入，不能代替验收人员复跑和判断。

## 3. 验收范围

### 3.1 包含

- Git 变更和文件所有权审核；
- Stage 3 公共合同和 namespace；
- Writing Core 三模式；
- Editor REVIEW、LOCAL_REPAIR 和 MAJOR_REWRITE 路由；
- Writer hints 与 Curator observation 对账；
- `WriterContextPackage` 正式 handoff；
- fake/offline 最小链；
- Stage 3 专项测试；
- 一次全仓 `make quality`；
- 一次正式真实模型生成实验；
- 一份总验收结果。

### 3.2 不包含

- Stage 2M 全部质量问题的重新验收；
- Advanced Agentic Retrieval；
- 完整章节/卷 TaskGraph；
- 生产 Commit 或 Canon 写入；
- 长期自主运行；
- 多套独立 Judge、复杂统计平台或大规模文学标注；
- 对每次小修复都重跑全套测试；
- 额外代码/Prompt/fixture hash 门禁。

Stage 3 验收只证明本阶段候选生成链和生成质量结果，不授权生产发布。

## 4. 验收前置条件

开始验收前应具备：

| 输入 | 最低要求 |
|---|---|
| B：Writing Core | 三模式代码、公开入口、fixture、自检结果 |
| C：Editor/对账 | Review/Repair/Rewrite/Reconciliation 代码和 fixture |
| D：评价工具 | runner、case、评价接口、报告输出、自检结果 |
| A：正式集成 | WriterContext adapter 和最小链 |
| Stage 2M | 本轮可用的 `WriterContextPackage` 合同和代表性 Context |
| Git | 一个可检出的集成候选 commit |
| 运行资源 | fake/offline 环境；正式实验时另有 Writer 和 evaluator 资源 |

Stage 2M 不需要先达到所有 M4/M5 指标。验收只要求被选入 Stage 3 实验的 Context 可用，并在结果
中如实区分 Context 问题和 Writer 问题。

## 5. Git 验收基线

### 5.1 独立分支

建议验收分支：

```text
codex/stage3-acceptance
```

从待验收的集成候选 commit 创建独立 worktree。验收 worktree 应干净，不混入其他阶段或用户尚未
提交的修改。

### 5.2 版本记录

总验收结果只记录 Git 原生信息：

- 被测 branch；
- 被测 commit；
- A/B/C/D 主要提交；
- 验收测试提交（如果验收人员新增了测试）；
- 修复后最终复测 commit。

不再为每个文件、Prompt、Skill、fixture 或报告维护人工 hash 表。运行代码中原有的内容寻址不受
影响，但它不是人工验收流程。

### 5.3 变更审核

验收负责人先检查：

```text
git log --oneline --decorate
git diff <baseline>...<candidate> --stat
git diff <baseline>...<candidate>
git diff --check
```

重点确认：

- 提交按 A/B/C/D 职责可理解；
- 没有混入无关 Stage 2M 或用户改动；
- 新公开路径使用 `stage3`，legacy 名称只作为历史来源；
- 共享文件修改有明确所有者；
- 没有引入生产 Canon/Commit 权限；
- 没有为了验收新增重复 Harness、Artifact Store 或通用平台。

发现提交边界混乱时，可以要求开发者整理提交或补充说明，但不要求为形式整洁反复改写已共享的
Git 历史。

## 6. 验收顺序

验收按由便宜到昂贵的顺序执行：

```text
Git/diff 审核
→ 专项工程测试
→ fake/offline 最小链
→ 全仓 make quality
→ 正式真实模型生成实验
→ 总验收结论
```

前一步出现阻断性缺陷时先退回修复，不继续消耗真实模型资源。

### 6.1 专项工程测试

验收负责人复跑四个工作流交接的专项命令，并补充少量黑盒场景。重点不是重复开发者所有 unit
test，而是从公共入口验证行为。

#### Writing Core

| 验证点 | 预期 |
|---|---|
| DRAFT | 产生新的 candidate Draft |
| CONTINUE | 保留父 Draft/冻结前缀并产生子 Draft |
| MAJOR_REWRITE | 产生新候选，不覆盖旧 Draft |
| 模型或存储失败 | typed failure，无伪成功 Draft |
| 权限 | 无 raw retrieval、Memory write、Commit、Canon |

#### Editor 与变化对账

| 验证点 | 预期 |
|---|---|
| PASS | 不修改正文 |
| LOCAL_REPAIR | 只改允许范围，产生新候选 |
| MAJOR_REWRITE | 返回 Writer 路由 |
| Report 不一致 | 明确拒绝 |
| 对账四分类 | matched、多报、漏报、不一致可区分 |

#### Context handoff

| 验证点 | 预期 |
|---|---|
| ready Context | 可进入 Writer |
| task/target mismatch | 模型调用前拒绝 |
| basis mismatch | 模型调用前拒绝 |
| blocking gap/status failure | 明确停止 |
| evidence ledger | 可追溯但不默认进入 Writer Prompt |

#### 评价工具

| 验证点 | 预期 |
|---|---|
| 三种 Context | 进入同一 runner 和统一结果 |
| 单模块失败 | 不被记为成功或零质量分 |
| evaluator-only 数据 | 不进入 Writer 输入 |
| 报告 | case 结果与汇总一致 |

### 6.2 Fake/offline 最小链

至少执行以下三条路径：

```text
ready Context → DRAFT → Editor PASS → observation → reconciliation

ready Context → DRAFT → LOCAL_REPAIR
→ repaired Draft → observation → reconciliation

ready Context → DRAFT → MAJOR_REWRITE route
```

另执行一个 Writer 或 Editor typed failure。确认失败不会继续形成伪成功链，也不会产生 Memory、
Commit 或 Canon 副作用。

### 6.3 全仓质量

专项和 offline 链通过后，在同一集成候选上运行一次：

```text
make quality
```

该命令覆盖 Ruff、format、严格 MyPy、确定性 Pytest 和仓库覆盖率要求。若失败：

1. 先定位到 A/B/C/D 或既有基线；
2. 只退回对应所有者修复；
3. 复跑失败的专项范围；
4. 候选重新稳定后，再运行一次最终 `make quality`。

不在每个开发提交、每个 review comment 或每个模型 case 后重复全仓测试。

## 7. 正式生成质量实验

### 7.1 执行责任

正式实验由验收负责人运行，D 只提供工具和 runbook。Writer 实现者、Editor 实现者和评价工具
实现者可以解释代码，但不能单独决定最终结论。

### 7.2 实验输入

使用 D 交付的代表性 case，并为每个 case 准备：

- 最近正文上下文；
- 简单检索上下文；
- deterministic `WriterContextPackage`；
- 相同 Writing Task；
- 独立 evaluator 或盲审人工所需的评分材料。

Writer 不得读取未来正文、Gold 或 evaluator-only 说明。

### 7.3 比较条件

三个方案使用：

- 同一 Writer 模型和版本；
- 同一主要采样参数；
- 相近输入/输出预算；
- 同一 Editor 和 evaluator；
- 同一公开任务边界。

不要求在本文件写死精确模型名、Token 数或统计阈值。实际值由 D 的运行配置给出，并在正式结果中
记录。比较条件发生改变时，结果不得伪装成同一实验。

### 7.4 评价内容

每个 case 至少检查：

- mandatory constraints 和 plan obligations 是否实际满足；
- 人物、时间、地点、物品、关系和世界规则冲突；
- 计划外泄露和缺失事实编造；
- Editor verdict、局部修复和重大重写情况；
- Writer hints 与 Curator observation 的差异；
- 文学质量是否比简单基线明显退化；
- 模型调用、token、延迟和失败原因。

确定性规则负责明确约束和泄露；独立 evaluator/人工负责隐含矛盾和文学质量。不要用单一总分掩盖
硬约束失败。

### 7.5 运行次数

正式 case 集原则上完整运行一次：

- 瞬时 endpoint 失败只重跑失败 case；
- 明确代码缺陷修复后只重跑受影响 case，再做必要的汇总一致性检查；
- Prompt、模型、公共合同或评价方法实质变化时，相关结果失效并重新运行；
- 仅因分数波动或希望得到更好结论，不反复全量运行。

## 8. 安全与权限审核

验收负责人使用 spy/fake port、代码路径检查或现有权限测试确认：

```text
允许：
Model call
Candidate artifact write
Editorial/reconciliation/evaluation result

禁止：
Raw retrieval tool selected by Writer
MemoryWriteWorkflow
Candidate ChangeBundle production publish
Commit request
Canonical Root mutation
```

如果最小链需要生产 Canon 权限才能运行，Stage 3 验收直接退回开发，不通过增加审计记录来接受
该设计。

## 9. 缺陷返回与复测

验收发现问题时只记录足够修复的信息：

| 内容 | 要求 |
|---|---|
| 所属工作流 | A、B、C 或 D |
| 复现方式 | 命令、case 和必要输入 |
| 实际结果 | 可观察失败 |
| 预期结果 | 对应设计行为 |
| 是否阻断 | 是否影响继续验收或最终结论 |

对应开发负责人在自己的分支修复并提交，验收负责人拉取新候选后：

1. 复跑失败场景；
2. 复跑受影响的邻近专项测试；
3. 所有修复完成后运行最终全仓质量；
4. 只有影响正式实验的变更才重跑相应实验 case。

不为一个缺陷建立多份诊断、修复、复测和 Gate 文档。

## 10. 验收结论

### 10.1 `PASS`

同时满足：

- Git 变更范围清楚；
- 专项、offline 最小链和全仓质量通过；
- Writing Core、Editor、对账与 handoff 行为符合总设计；
- Writer/Editor/集成链没有 Canon 或 Memory 写入权限；
- 正式实验完成且结果可解释；
- 没有未来泄露或 Canon 污染；
- `WriterContextPackage` 方案未出现不可接受的连续性、计划遵循或文学质量退化。

### 10.2 `CONDITIONAL_PASS`

工程与安全验证通过，但正式实验因样本、模型资源或评价完整性不足而不能形成充分语义结论。

此结论只允许继续 Stage 3 实验和修复，不表示 Stage 3 完成，也不允许生产启用。

### 10.3 `FAIL`

出现以下任一情况：

- candidate-only、权限或 future isolation 边界被破坏；
- current-main 最小链无法运行；
- 全仓质量无法通过且问题属于 Stage 3；
- 正式实验结果不可比较或暴露严重生成质量退化；
- 报告把缺失、失败或其他方案结果伪装成成功。

## 11. 验收结果文件

只生成一份 Stage 3 总验收结果，包含：

- 日期和验收负责人；
- 被测 Git branch/commit；
- 验收测试 commit（如有）；
- 执行命令和简要结果；
- 正式实验报告引用；
- PASS/CONDITIONAL_PASS/FAIL；
- 仍存在的限制和下一步。

原始测试日志和机器可读实验结果保留在正常 CI/报告位置，不复制进多份 Markdown。

## 12. 验收完成条件

- 所有阻断问题已有结论；
- 必要复测完成；
- 最终 Git commit 明确；
- 一份总验收结果已经形成；
- `docs/project_status.md` 已按实际结果更新；
- 未通过时没有把候选功能标记为生产可用。
