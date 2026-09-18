# ADR-0011: 分层规划的层级语义、边界证据与正文完整性

> Lifecycle: `ACCEPTED`
>
> Date: 2026-09-18
>
> Baseline: `dda6938694efe815fdc084a884de20fac8533127`
>
> Source plan: `docs/Novel_System_Hierarchical_Planning_Remediation_20260918.md`

## 1. Context

固定基线暴露出七个相互关联的缺陷，它们共享同一个根因：**系统从位置和存在性推断语义**。

1. `PlanLevel` 已有 STORY / ARC_VOLUME / CHAPTER_SET / CHAPTER / SCENE，但章集只是“N 个章目标的
   批量提交”。章集没有自己的剧情内容，`_chapter_set_wrapper()` 只写 horizon。
2. `_successor_after_plan_projection()` 在 ARC_VOLUME 之后直接创建 Draft，运行时没有稳定的
   单章深化步骤；`_rolling_plan_task()` 又继承上一任务的 `plan_level`。
3. `_memory_gap_finding()` 把“有候选且必需 facet 未 SUPPORTED”直接判为
   `CANON_EXTRACTION_GAP`，于是“没检索到”被当成“Canon 漏提取”。
4. 该 finding 的 `no_progress_key` 含 attempt 身份，同一问题在同源重试下会重复派发维护。
5. `_volume_stage_constraints()` 把“章节位置已经过了卷首”渲染成“入口条件已经成立”。
6. `ProductionWritingRequestFactory` 只从章目标抽取字符串 beats，结构化细纲即使存在也会被丢弃。
7. 共享的 `candidate_surface_error()` 只检查长度与表面约束，恰好在 5,000 字处中断且未成句的
   正文可以成为可提交候选；`repeats_recent_prose()` 比较整章，看不到重复的章尾模板。

## 2. Decision

### 2.1 层级：四个正式层级 + 章内细纲，不新增运行阶段

保留 `PlanRootDocument.nodes + chapter_goals`。卷级新增 `chapter_set_roadmap` 作为粗路线图；
正式章集仍是滚动窗口，但升级为受限的 **1+N 复合提案**：一个 `chapter-set.v2` 语义父项 +
每章一个 `chapter.v2` 章纲子项。SCENE 不接成新的接纳节点；场景与数百字节拍保存在 CHAPTER
payload 内，由之后的 CHAPTER 深化任务生成。

父子分工由宿主校验：范围（父项 `chapter_assignments` 恰好覆盖 horizon）、任务（父级每个必需
转折有子章承接）、因果（依赖要么有已提交 Canon 证据，要么由本窗口更早章节建立）。
子章绑定 **父节点内容哈希**（`ParentPlanBinding`），父节点只引用子章稳定 ID，因此不会形成
父包含子哈希、子又包含父哈希的环。适用性检查比较当前父节点内容哈希，而不是整个 PlanRoot 哈希。

### 2.2 边界：两个维度 + 一个纯分类器 + 宿主准入

`PlanningQuestion` 新增宿主派生的 `question_purpose`（`verify_history` / `check_current_state` /
`design_future`）与 `dependency_expectation`（`must_establish_existing_fact` / `check_status` /
`no_historical_precondition`）。模型输出走独立的 `PlanningQuestionDraft`，**结构上不携带**这些
字段，因此模型无法把真实历史前提改标为未来设计。

`domain/planning_gap.py` 的 `classify_gap()` 是唯一分类点，输入只来自宿主核验的回执：

| 输入 | 结果 |
|---|---|
| `design_future` + 要求既存事实 | 直接拒绝（未来设计不能伪装历史前提） |
| 投影不 exact | `refresh_projection` |
| 有源证据 / 投影支持 | `supported_answer`（源有而投影缺且应当投影则为 `canon_extraction_gap`） |
| 有确切否定 | `supported_negation`；要求既存事实时 `precondition_conflict` |
| 正反证据同截止点 | `evidence_conflict` |
| 无证据 | `historical_unresolved`（**不是** PASS，也不是漏提取） |

`unknown` 永远不是否定证据。`design_future` 不生成历史 Need，但保留可审查的拒绝理由。
`PlanningLoopResult.failure_code` 与 detail 现在承载 `historical_dependency_unresolved` 与
`unsupported_plan_precondition`，规划闭环据此修订，而不是触发维护链。

### 2.3 去重：问题身份与尝试身份分离

`MemoryRepairFinding.no_progress_key` 改为跨 attempt 稳定的问题身份（项目、源事实版本、
cutoff、规范化问题、facet、受信来源绑定），不含 run / attempt / 随机 question id；
`attempt_problem_key` 保留尝试审计身份。`runtime_commands._maintenance_task_id()` 以稳定问题
键为主键，因此同源同问题的重试复用同一个维护任务；来源证据 digest 变化时产生新的机会。
混合 facet 的问题按 owner 拆成共享同一父 problem key 的受控子 finding，关系 facet 只交给
Graph Curator。

### 2.4 调度：由当前根派生的唯一下一步决策

`domain/creation_step.py` 的 `select_next_creation_step()` 由 **当前已接纳 PlanRoot + 已提交
正文 cursor** 派生下一步，输入 `TrustedPlanReadiness`。`_creation_step_successor()`：

- 非滚动的 STORY / ARC_VOLUME 任务以任务自身层级为权威（不因根暂时不可读而退化）；
- 章集之后，若目标章没有可用的 execution，下一步是 CHAPTER，不是 Draft；
- CHAPTER 任务之后询问它自己正在处理的章（`from_chapter`），不会跳过该章；
- 同一个 CHAPTER 深化任务若没有产生 execution，不再生成相同身份的第二个深化任务；
- 是否仍在已接纳章集内由当前 PlanRoot 计算，而不是读上一任务的 horizon。

`ProductionStage4InvocationFactory._mode()` 让 V2 REPLAN 按受信层级选择模式，而不是一律落到
通用 REPLAN。

### 2.5 Writer 保真与正文完整性

`WritingTaskContract` 新增 `scene_blueprints` / `planned_participants` /
`planned_introductions`，由 `_compile_chapter_execution()` 从被接纳的 `chapter.v2` payload
确定性编译，保留 scene / beat 稳定 ID。计划参与者是计划身份，**不进入**
`participating_entity_ids`，也不会被预写成 World 关系；World 只在正文写出后由 Curator 依证据接入。

`validate_work_plan_execution()` 是第二次就绪检查：WorkPlan 必须覆盖全部必需执行 beat，
且 `expected_total_characters` 等于各 beat 预算之和并落在长度政策内。`outline` 不能直接进入
正文生成。

`text_integrity_verdict()` 区分两类结果：确定的传输/生成不完整（provider 长度终止、未闭合引号、
停在连接符）使候选不可提交；语义不完整嫌疑进入既有 Editor 复审。结尾有句号**不**证明情节完整，
省略号、破折号与合法悬念不被机械误杀。`tail_repeats_recent_prose()` 补上章尾局部比较，
只复用既有近文机制，不新建重复检测器。

### 2.6 位置不再是事实证据

`_volume_stage_constraints()` 删除了“阶段已过去 → 入口已成立”的推断。已过期的入口 slot 渲染为
**“按已接纳计划本项应已建立，属于计划要求而非既成事实”**，并要求 Writer 只使用已核实部分；
未到期的卷出口继续作为禁止提前兑现的约束。

## 3. Consequences

- 新增两个叶子 domain 模块（`plan_detail.py`、`planning_gap.py`、`creation_step.py`），
  全部无 IO、无模型调用、无第二存储。
- V1 提案与历史 artifact 仍可读取与回放；声明 `chapter-set.v2` / `chapter.v2` 的 payload
  必须满足 V2 校验，不会静默降级为 V1。
- 一个只写了 outline 的章节不再直接进入 Writer：必须先完成并接纳单章 execution。
  这会增加一次规划接纳往返，属于设计要求而非故障。
- `MemoryRepairFinding` 与 `Stage1MemoryNeed` / `RetrievalTrace` 新增可选字段；
  schema 已同步，历史记录可读。
- 维护任务身份随稳定问题键变化，因此“同一问题重试”会复用同一任务；这是本次修复的目标。

## 4. Alternatives rejected

- **把 SCENE 接成新的顶层运行阶段**：需要为同章多场景设计 scene-order 范围，会触碰同层范围
  不重叠不变量，且用户要的是更细的内容而不是更多接纳节点。
- **用数据库迁移给旧 wrapper 填入“看起来合理”的剧情**：等于伪造计划内容。
- **把 `noop` 统一当作“事实不存在”**：会把语义判断失败写成否定证据。
- **取消 REQUIRED / 放宽判定通过 G3**：违反证据边界，明确禁止。
- **在 Writer 侧增加旁路开关绕过边界**：修复点在规划问题生成与闭环，不在 Writer。
