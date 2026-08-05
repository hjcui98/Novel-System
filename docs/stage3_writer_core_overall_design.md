# Stage 3 Writer Core 与生成质量总设计

> 文档生命周期：`ACTIVE`
>
> 设计状态：`DESIGN_BASELINE`
>
> 开发状态：`NOT_STARTED`
>
> 初始日期：2026-07-31
>
> 最近修订：2026-08-03
>
> 阶段：Stage 3 — Writer Core and Generation Quality
>
> 本文职责：定义 Stage 3 的目标、范围、总体接口、并行边界和完成判定
>
> 本文不承担：逐项任务清单、人员排期、字段级 Schema 设计和测试用例穷举

## 1. 设计结论

Stage 3 的目标不是继续扩展 Memory 基础设施，而是把已经形成的
`WriterContextPackage` 转化为可评估的正文候选，并建立最小、独立的生成质量闭环：

```text
WritingTask + WriterContextPackage
    → Writer 生成候选正文
    → Editor 独立审阅
    → 局部修复或重大重写
    → Curator 独立观察正文变化
    → Writer 声明与 Curator 观察结果对账
    → 生成质量评估
```

Stage 3 完成后，项目应能回答三个问题：

1. Writer 能否稳定地把任务、计划约束和记忆上下文写成候选正文；
2. 独立 Editor 能否发现主要问题，并把问题路由到局部修复或重大重写；
3. 在同一 Writer 模型和相近预算下，`WriterContextPackage` 是否比简单上下文更有助于连续性、
   计划遵循和整体写作质量。

本阶段的终点是“可集成、可评估的候选生成链”，不是生产化的完整章节系统。正文和变化结果都保持
候选状态，不在 Stage 3 内直接写入 Canon。

## 2. 当前起点

Stage 3 明确以现有 Writing Core 为实现基线，不重新建设另一套 Writer。legacy worktree
`/home/cuihengjia/agent/novel/NS-stage2b-writer` 已经包含 Writer 三模式、候选
`DraftArtifact`、typed failure、Prompt/Skill、shadow runner 和一组通过的隔离测试。

这些代码是 Stage 3 的主体起点。为了形成最终集成结果，迁移只需解决四件事；前两项由 Writing
Core 工作流先完成，后两项可以留到后置集成工作流：

- 把未提交实现整理成可评审的 Git 变更；
- 把项目面对外的 `stage2b` 命名迁移到 `stage3`；
- 在后置集成阶段把 `Stage1ContextPackage` 输入替换为 `WriterContextPackage`；
- 在后置集成完成后，让最小链通过一次正常的 current-main 集成验证。

除非现有行为与当前公共合同或安全边界冲突，Writer 领域模型、生成服务、Agent、Prompt/Skill 和
测试都优先直接迁移。开发精力用于删去不再需要的复杂度、补齐当前接口以及新增 Editor、对账和
生成质量评估；不重新实现已经正确的 Writer 主体，也不为了迁移复制一套新 Harness、Artifact
系统或运行账本。

Stage 2M 与 Stage 3 Writing Core 可以独立开发。Stage 3 前期使用冻结 fixture 或本地 handoff
contract 开发 Writer、Editor 和评价链；Stage 2M 稳定后，再由工作流 A 完成
`WriterContextPackage` adapter 和正式集成。接口名称、版本和字段差异在集成工作包中统一，
不作为两个阶段独立开发的阻塞项。

## 3. 范围和边界

### 3.1 Stage 3 交付范围

- `DRAFT`、`CONTINUE`、`MAJOR_REWRITE` 三种 Writer 模式；
- candidate-only `DraftArtifact` 及父子 lineage；
- `WriterContextPackage` 到 Writer 的单一输入边界；
- Writer 的弱变化提示、未决问题和自我观察；
- Editor `REVIEW` 与 `LOCAL_REPAIR`；
- `PASS`、`LOCAL_REPAIR`、`MAJOR_REWRITE` 三类审阅结论；
- Writer 声明变化与 Curator 独立观察变化的对账结果；
- 一条不写 Canon 的最小集成链；
- 一次有明确样本、基线和评价方法的真实模型生成实验。

### 3.2 不在 Stage 3 交付

- Advanced Agentic Retrieval、复杂 R2 或 Writer 直接检索；
- 完整 Planner 重规划实现；
- 完整章节/卷 TaskGraph 和生产调度；
- Candidate ChangeBundle 的生产提交和 Canon 写入；
- 长期自主恢复、跨机器调度和大规模并发；
- 多候选搜索、常驻多 Judge 或自动 Skill 演化；
- 通用/外部 Hook 平台；
- consolidation 或长期记忆自动晋升；
- Operational/Derived retention 与自动遗忘；
- 通用 observation graph；
- Viewer 产品；
- learned fusion 或在线自适应检索策略；
- 为每次运行新增一套哈希门禁、证明文件或验收清单；
- 没有真实需求支撑的数据库 migration、平台层或通用框架。

这些能力分别属于 Stage 4–7，或应在真实 Stage 3 结果证明需要后再增加。其中 compact→exact
expand 属于 Stage 4 的高级读取接口；Hook 仅在 Stage 5 有真实外部 Runtime surface 时考虑；
Operational retention 属于 Stage 6；consolidation/晋升、独立 observation graph 与 learned
fusion 属于 Stage 7 的受控候选；Viewer 延后到 post-Stage 7 的可选运维表面。列入后续站位不
等于预先批准实施，仍须对应 Stage 的需求、消融和门禁。

### 3.3 必须保持的边界

只保留会影响系统正确性的四条硬边界：

1. Writer 输出始终是候选，不是 Canon；
2. Writer 不直接使用底层检索、Memory 写入、Commit 或 Canon 接口；
3. Editor 必须独立审阅，Writer 的自我评价不能代替正式审阅；
4. Curator 必须独立读取正文，Writer 的变化提示不能直接变成 MemoryPatch。

已有 Artifact 内容寻址可以继续作为运行实现细节，但 Stage 3 不再围绕内容哈希建立额外的人工
门禁。代码和文档版本统一由 Git 管理。

## 4. 最小产品链

### 4.1 输入

Stage 3 只定义一个生成入口，输入由三部分组成：

- `WritingTaskContract`：本次要写什么、必须满足什么、不能泄露什么；
- `WriterContextPackage`：Writer 实际可见的连续性、状态、关系、历史、计划和未决缺口；
- 可选的 prior draft 与明确指令：仅供 `CONTINUE` 或 `MAJOR_REWRITE` 使用。

Stage 2M 继续拥有 `WriterContextPackage`。Stage 3 只提供小型 handoff/adapter，不复制
Context Package，不把 `EvidenceLedger` 或原始检索轨迹默认塞入 Writer Prompt。

如果当前 `WriterContextPackage` 中仍有只服务 Benchmark 的字段，优先在边界适配，不立即设计
第二个 Writer Context 合同。只有实际生产调用无法表达时，才对公共合同做最小泛化。

### 4.2 Writer 输出

Writer 输出包含：

- 候选正文；
- 弱变化提示；
- 未决问题；
- 自我观察；
- 模式和父 Draft 关系。

可信 ID、证据定位、内容寻址和 Canon 身份由现有 Service 生成或校验，不要求模型猜测。总设计不
固定所有字段，具体字段由 Writer 执行文档和现有实现共同收敛。

### 4.3 Editor 与修复

Editor 先执行只读 `REVIEW`：

- `PASS`：正文可进入后续候选处理；
- `LOCAL_REPAIR`：Editor 按明确范围做一次局部修复；
- `MAJOR_REWRITE`：退回 Writer，保留旧 Draft，不做原地覆盖。

第一版不加入多轮 Judge 讨论。局部修复后只重新检查受影响内容；重大重写才重新执行完整审阅和
变化对账。

### 4.4 变化对账

Writer 的变化提示与 Curator 的独立观察形成一个 `ReconciliationResult`，至少区分：

- 双方一致；
- Writer 提示但正文没有充分支持；
- 正文发生变化但 Writer 未提示；
- 对变化类型或对象的判断不一致。

对账结果用于评价和后续 Curator 改进，不在本阶段自动发布 MemoryPatch 或 Canon 变更。

## 5. 并行开发划分

Stage 3 划分为四个开发工作流。四个工作流对应四份代码开发执行文档，阶段验收由独立的第五份
文档负责，不在本文展开到 Issue 或测试用例级别。

| 工作流 | 主要职责 | 主要输出 | 不负责 |
|---|---|---|---|
| A. Context handoff 与正式集成 | 在两侧基本稳定后收敛 WriterContext adapter 并接通最小链 | 当前主线可用的 Context handoff 与集成链 | 阻塞 B/C/D 前期开发、重做 Stage 2M |
| B. Writing Core 迁移与完善 | 基于现有实现迁移 Stage 3 合同、三模式 Writer、Prompt/Skill、DraftArtifact 和离线 runner | 当前主线可用的三模式 Writing Core | 另起一套 Writer、Editor、Curator、Canon |
| C. Editor 与变化对账 | Review、Local Repair、重大重写路由、声明/观察对账 | EditorialReport、修复 Draft、ReconciliationResult | 生产提交、Memory 写回 |
| D. 生成质量评估开发 | 实现样本、基线、统一 runner、评价接口和报告能力 | 可由独立验收人员运行的生成质量评估工具 | 为自己开发的工具签发阶段验收结论 |

### 5.1 依赖关系

```text
B：迁移 Writing Core
C：开发 Editor 与变化对账
D：开发样本、基线和评价工具
        并行进行

Stage 2M 合同稳定
+ B/C 基本完成
        ↓
A：收敛 WriterContext handoff 并接通最小链
        ↓
独立验收人员：使用 D 的工具执行正式生成实验
```

工作流 B 拥有 Writer 和 Draft 公共合同，工作流 A 拥有 Stage 2M 到 Stage 3 的 Context
handoff。C、D 使用冻结 fixture 和已发布合同开发；若发现接口问题，反馈给对应所有者，不各自
复制或扩展公共模型。

### 5.2 建议代码所有权

| 工作流 | 优先拥有的路径 |
|---|---|
| A | WriterContext adapter、Writing Core 集成 service 和最小链 |
| B | `domain/generation.py`、Stage 3 schema/export、Writer Agent/Service、prompts/skills、shadow runner |
| C | Editor/Editorial 新模块、reconciliation service 及其测试 |
| D | Stage 3 evaluation、fixtures、实验 runner 和报告生成 |

确实无法避免的 Writer 共享枚举或公共导出由 B 统一修改，Context handoff 改动由 A 统一修改。
其他工作流不同时编辑 `domain/stage2.py`、Stage 2M evaluator、Memory Controller 或 Curator
写回主链。

## 6. 多 Agent / 多分支协作方式

并行开发使用 Git 作为唯一版本和交接机制：

1. B 先把现有 Writing Core 整理成可评审的 Stage 3 实现基线；
2. B/C/D 使用独立 branch/worktree，并通过冻结 fixture 并行开发；
3. Stage 2M 在自己的工作流中继续演进，不等待 Stage 3；
4. Stage 2M 稳定且 B/C 基本完成后，A 建立集成分支并统一 handoff 差异；
5. 每个工作流保持少量、可评审的提交，合并前只做一次必要 rebase；
6. B/C/D 的开发成果先集成，A 随后接通最小链，正式实验由独立验收人员执行。

不额外建立人工文件锁、hash 清单、分支指纹表或重复的状态 manifest。文件所有权表、Git 提交和
正常代码评审足以处理并行冲突。

如果一个工作流需要大范围修改另一个工作流拥有的路径，应暂停该交叉修改并先调整接口，而不是让
两个 agent 在同一文件中并行堆叠实现。

## 7. 开发顺序

### 第一步：独立并行开发

- B 把现有 Writing Core 整理成可评审提交，完成 `stage2b` → `stage3` 命名迁移；
- B 保留已验证的三模式和 candidate-only 设计，使用冻结 Writer Context fixture；
- C 使用固定 Draft fixture 开发 Editor 和变化对账；
- D 开发小而有代表性的任务集、简单基线、评价工具与报告输出；
- Stage 2M 继续独立修复和稳定 `WriterContextPackage`。

### 第二步：后置 Context 集成

当 Stage 2M 合同稳定且 B/C 基本完成后，由 A：

- 统一接口名称、版本和必要字段差异；
- 实现 `WriterContextPackage` adapter；
- 把冻结 fixture handoff 替换为正式 handoff；
- 接通最小集成链。

集成链只包含：

```text
WriterContextPackage
→ Writer
→ Editor Review / Repair or Rewrite
→ Curator observation
→ ReconciliationResult
```

该链使用候选 Artifact 和测试/实验存储，不调用生产 Commit。

### 第三步：独立执行一次正式生成实验

在 Writer、Editor 和对账链稳定后，由未参与对应功能实现的验收人员使用 D 的工具执行正式
实验。实验使用同一 Writer 模型、相同生成参数和相近 token 预算，比较：

- 最近正文上下文；
- 简单检索上下文；
- deterministic `WriterContextPackage`。

这次实验同时评价约束遵循、连续性、计划使用、修复轮次和文学质量。若结果暴露明确实现缺陷，
修复后重跑受影响部分；不因为指标波动而无目标地反复全量测试。

## 8. 验证策略

### 8.1 开发自检

每个开发工作流只维护能证明自身行为的测试：

- 公共输入输出的合同测试；
- 核心正向路径；
- 会造成错误正文、越权或伪成功的关键失败路径；
- Writer、Editor、Curator/Commit 权限边界。

已有 legacy Writer 测试优先改名、迁移和删重，不复制成新的 unit/contract/golden/property/
regression 多套版本。内部字段和辅助函数不要求逐一建立测试文件。

开发者在交接前运行所属模块的专项测试和基本静态检查，不为自己的模块签发验收结论。全仓
`make quality` 和跨模块测试由独立验收人员在集成候选形成后运行。只有代码、公共合同、
Prompt/Skill、模型配置或评价样本发生实质变化，才需要重跑对应验证。

### 8.2 独立生成质量验证

正式实验只保留一套声明清楚的样本和评价流程，重点回答：

- mandatory constraints 和 plan obligations 是否被正文实际使用；
- 人物、时间、地点、物品、关系和世界规则是否出现冲突；
- 是否发生计划外泄露或对缺失事实的编造；
- Editor 的 PASS/Repair/Rewrite 分布和实际修复效果；
- Writer 提示与 Curator 观察是否一致；
- 文学质量相对简单基线是否出现明显退化。

评价可以由独立模型与盲审人工结合，但不建立多个功能重复的常驻 Judge。D 负责把样本、方法和
工具实现清楚，独立验收人员负责运行、复核和形成结论。

## 9. 完成判定

Stage 3 只有一个阶段级完成判定，不为四个工作流分别建立层层 Gate。完成时应同时具备：

1. current-main 中存在使用 `WriterContextPackage` 的 Stage 3 Writer；
2. Writer 三模式生成候选 Draft，且不具备检索、Memory 写入和 Canon 权限；
3. Editor 可独立 Review，并完成一次局部修复或重大重写路由；
4. Curator 独立观察与 Writer 声明能够形成对账结果；
5. 最小集成链通过独立的 fake/offline 和真实模型验证；
6. 独立验收执行的正式实验给出可解释结果，且没有未来泄露或 Canon 污染；
7. 结果、限制和未解决问题被写入项目状态，不用“测试通过”替代语义结论。

如果真实实验未显示预期收益，应根据失败归因继续改 Writer、Context 或评价样本；不要通过增加
门禁、报告、哈希校验或重复测试来制造进度感。

## 10. 后续执行文档

当前执行文档为：

| 文档 | 职责 | 启动顺序 |
|---|---|---|
| `docs/stage3_writing_core_migration_execution.md` | B：Writing Core 迁移与完善 | 可立即并行 |
| `docs/stage3_editor_reconciliation_execution.md` | C：Editor 与变化对账 | 可立即并行 |
| `docs/stage3_generation_evaluation_development_execution.md` | D：生成质量评估工具开发 | 可立即并行 |
| `docs/stage3_context_handoff_integration_execution.md` | A：Context handoff 与最小链集成 | B/C 和 Stage 2M 稳定后 |
| `docs/stage3_acceptance_test_execution.md` | 独立测试、审核和阶段结论 | 集成候选形成后 |

每份开发执行文档至少明确：

1. 目标和明确不做的事项；
2. 拥有的文件与共享接口；
3. 输入、输出和依赖；
4. 足够直接实施的代码步骤和失败处理；
5. Git branch/worktree、提交边界和合并方式；
6. 开发者必须完成的最小自检；
7. 向独立验收人员或下一工作流的交接条件。

足够详细不等于复制总体架构、穷举所有字段或建立重复门禁。只有执行中发生真实设计分歧时，才
回到本文或新增 ADR。

## 11. 下一步

总设计、四份开发执行文档和独立验收测试文档已形成。下一步分配 B/C/D 实现负责人和独立
worktree，让三者使用冻结 fixture 并行启动；A 的实现负责人可以预先指定，但在 Stage 2M
合同稳定且 B/C 基本完成后再开始正式集成。验收负责人应与对应实现负责人分离，并在集成候选
形成后开始工作。
