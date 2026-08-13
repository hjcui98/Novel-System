# Stage 3 生成质量评估工具开发执行

> 文档生命周期：`SUPERSEDED`
>
> 后继：`docs/stage3_writer_context_loop_execution.md`
>
> 工作流：D
>
> 执行状态：`NOT_STARTED / READY_FOR_ASSIGNMENT`
>
> 实现负责人：`UNASSIGNED`
>
> 日期：2026-07-31
>
> 上位设计：`docs/stage3_writer_core_overall_design.md`
>
> 职责边界：本文只开发评价工具；正式运行、复核和阶段结论由独立验收人员负责

## 1. 目标

开发一套足够小、可以被独立验收人员直接运行的 Stage 3 生成质量评价工具。它比较不同 Context
方案下的正文结果，生成可阅读报告，但不替开发者自己签发 Stage 3 结论。

D 与 B/C 并行开发，前期使用冻结 task、context、draft 和 editorial fixture。

## 2. 评价对象

工具支持总设计中的三类输入：

```text
最近正文上下文
简单检索上下文
deterministic WriterContextPackage
```

工具只要求各方案使用相同 Writer 模型、相同主要生成参数和可比较预算。具体模型、样本量和数值
阈值由正式验收运行确定，不在开发文档中写死。

评价维度保持有限：

- mandatory constraints 与 plan obligations；
- 连续性和事实冲突；
- 计划外泄露和事实编造；
- Editor verdict 与修复/重写情况；
- Writer hints 与 Curator observation 对账；
- 文学质量是否明显退化；
- 基本调用、token、延迟和失败信息。

### 2.1 Case 组织

每个 case 只保留正式比较需要的内容：

- case ID 和 Writing Task；
- 三种 Context 输入或其生成入口；
- 对 Writer 可见的公共约束；
- evaluator/human-only 的评分说明；
- 运行后产生的 Draft、EditorialReport 和 ReconciliationResult。

未来正文和 evaluator-only 信息必须与 Writer 输入分离。D 可以复用 Stage 2M 已有的信息边界
设施，但不复制其完整 benchmark 合同。

### 2.2 评价职责

| 评价内容 | 首选实现 |
|---|---|
| 明确约束、禁止泄露、输出存在性 | deterministic rule |
| 人物/时间/物品/世界规则冲突 | rule + 独立 evaluator |
| Editor verdict/repair 次数 | 直接读取运行结果 |
| Writer/Curator 对账 | 直接读取 ReconciliationResult |
| 文学质量 | 独立 evaluator 或盲审人工 |
| token/延迟/失败 | 复用 ModelGateway/运行结果 |

不让被测 Writer 成为自己的唯一评价者，也不要求所有维度都再调用一个模型。

## 3. 开发范围

需要实现：

- 小型公开 case/fixture 合同；
- 三类 Context 输入的统一调用入口；
- Writer、Editor、对账结果的采集；
- 独立 evaluator 或人工评分导入接口；
- 单 case 和汇总报告；
- fake/scripted 数据下的确定性测试；
- 正式运行所需的简短 runbook。

不实现：

- 新的常驻 Judge Agent；
- 多套重复评分框架；
- 自动调参或 Prompt 搜索；
- 为显著性分析预建复杂统计平台；
- Stage 2M benchmark 重跑器；
- 自动宣布 Stage 3 PASS；
- 正式真实模型运行。

## 4. 文件所有权

D 优先拥有：

```text
src/novel_agent/domain/ 下 Stage 3 evaluation 公共结果
src/novel_agent/services/ 下 Stage 3 evaluation service
scripts/ 下 Stage 3 generation evaluation runner
tests/fixtures/stage3_evaluation/
tests/unit/ 和 tests/contract/ 下评价工具专项测试
reports/stage3/ 的输出约定
```

D 只消费 B/C/A 的公开入口，不修改 Writer、Editor、Context adapter 或 Stage 2M evaluator。
发现接口缺口时交给对应所有者处理。

## 5. 实现步骤

1. 定义最小 case 输入和统一结果结构；
2. 接入三个 Context 方案，但允许前期全部由 fixture 提供；
3. 采集 Writer、Editor 和 ReconciliationResult；
4. 实现规则型检查与一个独立评价接口；
5. 输出单 case 结果和一份汇总报告；
6. 提供人工评分的简单导入方式；
7. 用 fake/scripted fixtures 验证成功、失败和缺失评价；
8. 写明独立验收人员如何运行工具，不执行正式结论。

报告优先展示可解释结果，不为每个内部中间值创建独立 manifest 或报告文件。

### 5.1 Runner 流程

1. 读取一个 case；
2. 为三个 Context 方案构造可比较的 Writer 调用；
3. 调用同一 Writing Core；
4. 收集 Editor 和对账结果；
5. 执行 deterministic checks；
6. 可选调用独立 evaluator，或导出人工评分包；
7. 生成每个 Context 方案的结果；
8. 汇总跨 case 差异并输出报告。

一个方案失败时保留 typed failure，不用其他方案的结果填补，也不把缺失结果记成零质量分。

### 5.2 失败归因

报告至少区分：

| 类别 | 示例 |
|---|---|
| `INPUT_NOT_READY` | Context 缺失、task 不一致 |
| `WRITER_FAILED` | 模型或 Writer 输出失败 |
| `EDITOR_FAILED` | 无法形成 EditorialReport |
| `RECONCILIATION_FAILED` | hints/observation 无法对账 |
| `EVALUATION_FAILED` | evaluator 或人工评分缺失 |
| `COMPLETED` | 结果完整，可进入比较 |

这样可以判断问题属于 Context、Writer、Editor 还是评价工具，避免对同一 case 无目标地反复重跑。

### 5.3 报告结构

正式输出保持两层即可：

1. 机器可读的统一结果，供聚合和复核；
2. 一份面向人的 Markdown/JSON 汇总，展示各方案结果、失败和限制。

报告记录 Git 被测 commit、主要运行配置和实际命令。Git commit 用于版本管理，不再为代码、
Prompt、fixture 和文档分别建立人工哈希门禁。

## 6. Git 执行方式

建议分支：

```text
codex/stage3-evaluation
```

执行要求：

- 从包含冻结评价 fixture 的共同基线创建独立 worktree；
- D 的提交不夹带 Writer、Editor 或 Stage 2M 修复；
- 提交建议按“evaluation contracts”“runner/report”“focused tests”划分；
- 对 B/C/A 接口的更新通过普通 Git 提交同步；
- 合并前做一次必要 rebase；
- Git commit 和正式验收时记录的被测 commit 足够标识版本，不新增人工 hash 体系。

## 7. 开发者自检

D 的最低自检范围：

- 三类 Context 输入可进入同一 runner；
- fake/scripted case 能稳定生成单 case 和汇总结果；
- 缺失 Writer/Editor/evaluator 结果被明确报告，不伪装为零分或 PASS；
- 人工评分导入拒绝未知 case；
- 报告能追溯到 case 和输入方案；
- 修改路径的 Ruff、MyPy 和 `git diff --check`。

D 不用真实模型为自己的工具生成“正式成功”数据，也不对文学质量作最终裁定。

建议自检行为组：

| 行为组 | 代表场景 |
|---|---|
| Case loading | 完整 case、缺失公开输入、evaluator-only 隔离 |
| Context execution | 三种 Context 都进入相同调用入口 |
| Collection | Draft、Editor、Reconciliation 正常收集 |
| Failure typing | Writer/Editor/Evaluator 缺失不被伪装成成功 |
| Rules | 明确 constraint、泄露和调用数据可计算 |
| Human import | 已知 case 接受、未知 case 拒绝 |
| Reporting | 单 case 与汇总数字一致 |

工具测试使用少量代表性 fixture，不复制 Stage 2M 的完整 benchmark 数据集。

## 8. 交接

向独立验收负责人交付：

- Git 分支和提交；
- runner 命令与必要配置；
- case/fixture 说明；
- 输出报告的位置和阅读方式；
- 开发者自检结果；
- 当前评价能力的限制。

正式验收人员可以在独立分支增加黑盒验收测试，但功能缺陷应退回 D 修复，不由验收人员直接改写
评价生产代码。

## 9. 开发完成条件

- 评价 runner 可以在 fixture 下完整运行；
- 三类 Context 方案使用统一输出结构；
- 报告覆盖总设计要求的有限评价维度；
- 工具不自动签发阶段结论；
- 代码、自检和 runbook 已通过 Git 交接。

达到这些条件只表示评价工具可供验收使用，不表示真实生成质量已通过。
