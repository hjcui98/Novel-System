# Stage 3 Editor 与变化对账开发执行

> 文档生命周期：`ACTIVE`
>
> 工作流：C
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

在固定 `DraftArtifact` fixture 上独立开发 Editor `REVIEW`、一次局部修复、重大重写路由，以及
Writer 声明变化与 Curator 独立观察结果的对账。

C 与 B、D 可以并行，不等待正式 `WriterContextPackage` adapter。

## 2. 输出

C 提供三个清晰结果：

```text
EditorialReport
RepairedDraft（仅 LOCAL_REPAIR）
ReconciliationResult
```

Editor 结论只使用：

```text
PASS
LOCAL_REPAIR
MAJOR_REWRITE
```

`MAJOR_REWRITE` 只返回给 Writer 路由，不在 Editor 内自行重写整章。

### 2.1 Editor 输入

最小 REVIEW 输入包括：

- candidate Draft；
- Writing Task/Scene Contract；
- Writer-safe Context 摘要和 mandatory constraints；
- style/POV/disclosure 要求；
- prior repair history（如果存在）。

第一版不要求 Editor 直接检索。需要核实但当前输入不足时，Editor 返回明确 issue 或 unresolved
need，由后续阶段决定是否请求 Memory。

### 2.2 EditorialReport

报告需要足以支持路由和修复，但不穷举文学理论。建议包含：

- verdict；
- issue 类型、严重度、正文位置和说明；
- `LOCAL_REPAIR` 的 repair scope 与 preserve requirements；
- `MAJOR_REWRITE` 的结构性原因；
- 未解决的事实或 Context 问题。

正文位置优先使用现有 block/span 标识；如果当前 Draft 尚无稳定块标识，可以使用可信 Service
生成的局部范围，不要求模型计算 Unicode offset 或内容哈希。

## 3. 开发范围

需要实现：

- Editor REVIEW 输入输出合同；
- LOCAL_REPAIR 的明确范围和保留要求；
- MAJOR_REWRITE 路由；
- 修复后形成新的候选 Draft，不覆盖原 Draft；
- Writer hints 与 Curator observation 的最小对账；
- Editor Prompt/Skill 和模型调用封装；
- fixed fixture 下的 unit/contract 测试。

不实现：

- 多轮 Editor/Judge 讨论；
- 完整 Planner replan；
- Curator 抽取主链重写；
- MemoryPatch、Guardian、Commit 或 Canon；
- 正式质量判定；
- 流式 Span 增量抽取和性能优化；
- 为每个 issue 类型建立独立 Agent 或 Schema 文件。

## 4. 文件所有权

C 优先拥有：

```text
src/novel_agent/domain/editorial.py
src/novel_agent/agents/editor.py
src/novel_agent/services/editorial.py
src/novel_agent/services/writer_change_reconciliation.py
src/novel_agent/prompts/editor_*.md
src/novel_agent/skills/ 下 Editor skills
tests/ 下 Editor 和 reconciliation 专项 fixture、unit、contract
```

实际文件名可按现有包结构小幅调整。C 不直接修改 B 拥有的 Writer/Draft 合同；接口问题提交给 B
统一处理。C 不修改现有 Curator 写回主链，只通过明确的 observation 输入进行对账。

## 5. 实现步骤

1. 以固定 Draft、Writing Task 和 Context 摘要定义最小 REVIEW 合同；
2. 实现独立 REVIEW 和三类 verdict；
3. 实现一次 LOCAL_REPAIR，并保证原 Draft 保留；
4. 实现 MAJOR_REWRITE 路由，不在 Editor 内隐式降级；
5. 定义最小 Curator observation 输入；
6. 实现四类对账：一致、Writer 多报、Writer 漏报、类型/对象不一致；
7. 使用 fixture 串联 Review、Repair 和 Reconciliation；
8. 运行专项自检并交接。

第一版只需证明职责和路由正确，不追求覆盖所有文学问题类型。

### 5.1 REVIEW 执行

1. 验证 Draft、Writing Task 和 Context 属于同一候选任务；
2. 生成只读 EditorialReport；
3. 将问题归为可接受、局部可修或结构性失败；
4. `PASS` 不产生修改正文；
5. verdict 与 issues 不一致时拒绝输出，而不是猜测路由。

### 5.2 LOCAL_REPAIR 执行

1. 只接受已经冻结的 `LOCAL_REPAIR` report；
2. 输入原 Draft、repair scope、preserve requirements 和修复指令；
3. 产生新的候选 Draft；
4. 检查 preserve 范围未被越界修改；
5. 将新 Draft 标记为原 Draft 的后继；
6. 只把新旧差异交给后续 observation/对账。

第一版只允许一次自动局部修复。修复后仍失败时返回未通过，不在 C 内加入无限循环。

### 5.3 MAJOR_REWRITE 路由

结构性问题返回 Writer rewrite directive。directive 应说明必须改变的目标和需要保留的内容，但不
替 Writer 生成新正文。需要 Planner replan 的情况可以标记出来，本阶段不接 Planner。

### 5.4 变化对账语义

对账以 Writer hints 和 Curator observation 为两个独立输入：

| 分类 | 含义 | 本阶段处理 |
|---|---|---|
| `MATCHED` | 双方指向同一变化 | 记录一致 |
| `DECLARED_ONLY` | Writer 声明但正文观察不支持 | 记录 Writer 多报 |
| `OBSERVED_ONLY` | 正文明显变化但 Writer 未声明 | 记录 Writer 漏报 |
| `MISMATCHED` | 对象、类型或结果不一致 | 保留双方差异 |

对账不裁定 Canon 真值，也不自动修复 MemoryPatch。匹配算法第一版可以使用结构化 key 和简单规则，
不需要语义搜索平台。

### 5.5 修复后的失效范围

LOCAL_REPAIR 产生新 Draft 后：

- 原 EditorialReport 仍作为旧 Draft 的历史输入保留；
- 受影响范围的 Curator observation 和 ReconciliationResult 需要重算；
- 未受影响范围可以复用，但第一版若局部复用过于复杂，可以安全地重跑整个小型对账；
- 任何结果都不得错误绑定到旧 Draft。

## 6. Git 执行方式

建议分支：

```text
codex/stage3-editor-reconciliation
```

执行要求：

- 从总设计和最小 Draft fixture 所在共同基线创建独立 worktree；
- C 的生产代码、Prompt/Skill 和专项测试保持在本分支；
- 需要 Writer 合同调整时，由 B 提供单独提交，C 通过 rebase/cherry-pick 消费；
- 提交建议按“editor contracts/runtime”“reconciliation”“focused tests”划分；
- 不把 A 的正式 Context adapter 或 D 的评价实现混入 C；
- Git 管理版本，不新增文件锁、接口 hash 清单或重复 handoff manifest。

## 7. 开发者自检

C 的最低自检范围：

- PASS 不修改正文；
- LOCAL_REPAIR 只产生新候选并保留原 Draft；
- MAJOR_REWRITE 正确返回 Writer 路由；
- Editor 失败不产生伪成功 Repair；
- 四类 ReconciliationResult 正确区分；
- Editor 和对账路径不调用 Commit、Canon 或 Memory 写入；
- 修改路径的 Ruff、MyPy 和 `git diff --check`。

开发者可以使用 fake/scripted model 验证结构化输出，不运行正式文学质量实验，也不为自己的
Editor 签发验收结论。

建议自检行为组：

| 行为组 | 代表场景 |
|---|---|
| REVIEW | PASS、LOCAL_REPAIR、MAJOR_REWRITE |
| Report consistency | verdict/issues/scope 组合合法 |
| Repair | 修改限定范围、保留区不变、产生新 Draft |
| Rewrite routing | directive 返回 Writer，不由 Editor 执行 |
| Reconciliation | MATCHED、DECLARED_ONLY、OBSERVED_ONLY、MISMATCHED |
| Failure | 模型失败、非法 report、repair 越界、对账输入不完整 |
| Permission | 无 Commit、Canon、Memory write |

这些是行为覆盖范围，不是要求建立七套测试文件。

## 8. 交接

交付内容：

- Git 分支和提交；
- Editor、Repair 和 Reconciliation 公共入口；
- B/A 调用所需的最小示例；
- 专项自检命令与结果；
- 已知不支持的问题类型或路由；
- 需要独立验收重点观察的行为。

不单独生成 Editor Acceptance、Repair Acceptance 和 Reconciliation Acceptance 三份报告。

## 9. 开发完成条件

- Editor 可独立 REVIEW；
- LOCAL_REPAIR 和 MAJOR_REWRITE 职责分开；
- 原 Draft 不被覆盖；
- Writer hints 和 Curator observation 可形成对账结果；
- 代码、自检与限制已经通过 Git 交接。

达到这些条件只表示 C 已完成开发，不表示 Editor 或对账质量已被接受。
