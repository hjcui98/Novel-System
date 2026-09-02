# Novel-System 长篇规划与渐进 Skill 改造：收敛版补丁执行设计 v2.2

> **生命周期**：`HISTORICAL_REVIEW_INPUT / FOLDED_INTO_STAGE4_STAGE5`  
> **文档版本**：`v2.2`（2026-09-02）  
> **基线仓库**：`hjcui98/Novel-System`  
> **基线提交**：`ee8849a45f10f2ed3300d52cbfb45346f34df172`  
> **基线状态**：截至 2026-09-02，该提交是 GitHub `main` 最新提交。  
> **用途**：修订上一版“大范围分层规划 + Progressive Skill”方案，收敛为与现有 Canon、Stage 4/5 固定拓扑、PlanRoot 合并语义兼容的补丁型执行设计。  
> **重要**：本文不能单独成为新的执行权威。正式实施前，接受的规范性内容必须并入现有 Stage 4 / Stage 5 设计与当前统一执行计划，并按 `docs/README.md` 的文档优先级更新索引和状态。  
> **工作树约束**：本方案只针对 `ee8849a` / `origin/main` 基线。不得在当前 `codex/stage2m-semantic-closure` 脏工作树直接实施；应新建或切换到从 `ee8849a` 起步的干净生产工作树。  
> **相对 v2 的变更**：机制、不变量和 Patch 顺序不变。Gate 只保留 **8 个 pytest 准入场景**。INV 仍是规范，不是测试清单。层内分支用 parametrize / 现有 unit 覆盖，不升格为新 Gate。  
> **相对 v2.1 的变更**：删除 R-25 / R-50 及执行步骤中的真实长跑准入。0–25 / 0–50 余烬九序实跑不在本方案 Gate 内，由操作方在全部 Patch 完成后统一测试。

---

# 0. 本次修订的核心结论

上一版方向正确，但实施范围过宽。本版采纳“先证明当前调度语义、先关闭真正导致长篇失控的最小闭环，再逐步接通 hierarchy / skills”的原则。

最终目标仍然是：

```text
STORY
  └── ARC_VOLUME
        └── CHAPTER_SET
              └── CHAPTER
                    └── SCENE（按需）
```

以及：

```text
Planner / Writer / Reviewer / Editor
→ 按角色和任务渐进披露 Skill
```

但**本轮不能把它们一次性全部改完**。

本版将工程工作收敛为四个串行 Patch，其中只有前三个属于当前建议实施范围：

```text
Gate 0：先固定当前生产语义 + 修 P0 硬阻塞
    ↓
Patch A：最小“禁止提前兑现”闭环
    ↓
Patch B：真正接通 STORY / ARC_VOLUME 层级
    ↓
Patch C：有限 Progressive Skill
    ↓
Future Admission：event-triggered multi-level replan / lookahead typed impact
```

其中最重要的一条是：

> **在 obligation 时间窗 fail-close、局部 Planner context 隔离、Writer 禁止提前兑现约束没有闭合之前，不应该先把 STORY / ARC_VOLUME 接进 production。**

否则只是把更多 Planner mode 接上，但根因依然存在。

---

# 0.1 Gate 目录规则

本文只承认下面这张表里的准入场景。§3 的 INV-1…INV-8 是规范，不是测试清单。各 Patch 正文只引用场景 ID，不再罗列 `test_*` 函数名。

规则：

```text
Gate = 准入场景。
同一故障在相邻层上的探针，写进同一个场景的多条 assert。
一个机制的内部步骤，用 parametrize，不拆成多个 Gate。
不同 owner / 不同失败模式，不合。
Patch A 与 Patch B 不合：它们是串行准入。
真实长跑（0–25 / 0–50 / 余烬九序）不进入本文 Gate。
```

禁止再作为独立 Gate 的东西：

```text
lookahead 冻结：写进 G0-1 的 policy/fixture 断言，不断言 “False 仍是 False”
schema parity：跑现有 contract/export 测试，不发明新 Gate 名
reviewer / materializer / writer / curator 对同一银铭案例的分测：
  作为 A-1 的层内 assert 或 parametrize("layer", ...)，不升格为四个 Gate
```

有一个不该为了“少测试”而合并的分界：

```text
A-1  有时间窗，仍提前兑现
A-2  该有窗，却没写
```

合在一起会把两种失败混成一个假绿。

---

## 0.1.1 Gate 0

文件：`tests/integration/test_production_planning_cadence.py`

| ID | 场景 | 一次要钉死的事 |
|---|---|---|
| G0-1 | `test_non_lookahead_run_consumes_full_horizon_then_plans_next` | `plan 1–5 → draft 1…5 → plan 6–10`；policy 保持 `enable_planner_lookahead=False` |
| G0-2 | `test_blocked_plan_replacement_supersedes_and_does_not_reuse_task_id` | BLOCKED 不算“不存在”；替换 = supersede + 新 id；同 id create 失败 |
| G0-3 | `test_final_draft_outside_length_policy_cannot_mutate_text_root` | parametrize：`local_repair` / `major_rewrite` × 低于 min / 超过 max；`TextRoot` 不变；reason 可观测 |

三条绿才能进入 Patch A。cadence、identity、length 分属不同 owner，保持三个场景。

---

## 0.1.2 Patch A

文件：`tests/integration/test_future_locked_obligation_enforcement.py`

| ID | 场景 | 一次要钉死的事 |
|---|---|---|
| A-1 | `test_yinming_cannot_payoff_at_chapter_24` | **唯一产品回归。** 固定 `chapter=24`，银铭 `not_before=85`，target `90–100`。同一条里断言：CHAPTER_SET 上下文无完整 brief；Writer 只用现有 `mandatory_constraints` / `forbidden_reveals`；outline 不含远期节点；plan review 与 materializer 拒 resolve；curator writeback 拒 `RESOLVED` |
| A-2 | `test_long_range_promise_without_not_before_is_rejected` | 字段合同：`PROMISE` / `FORESHADOWING` 缺窗 → Reviewer `REVISE` / `HUMAN_REQUIRED` 或 materializer 拒收 |

A-1 覆盖原先拆开的 reviewer / materializer / writer constraint / outline / curator / brief isolation。若需定位，用 A-1 内部 parametrize，不新增 Gate 名。

---

## 0.1.3 Patch B

文件：`tests/integration/test_plan_hierarchy_production.py`

| ID | 场景 | 一次要钉死的事 |
|---|---|---|
| B-1 | `test_stage4_mode_basis_rules` | parametrize 三种 mode：STORY@ch24 且 `horizon=None`；ARC_VOLUME 1–100@ch24；CHAPTER_SET 仍要求 horizon 且每章一个 ChapterGoal。对应 INV-2 |
| B-2 | `test_single_level_plan_commit_respects_parent_scope` | 一 task 一层；子范围不超出父；Genesis seed 被 STORY 替换而不是并列。对应 INV-1 / parent scope / bootstrap |
| B-3 | `test_replan_invalidates_future_descendants_and_keeps_committed_prefix` | descendant closure；已 commit 章节点不去掉；同范围旧 future ChapterGoal 清掉。对应 INV-3 / INV-4 |

STORY@24 与 VOLUME 1–100@24 是 B-1 的两个参数，不是两个 Gate。

---

## 0.1.4 Patch C

文件：`tests/unit/test_progressive_skill_disclosure.py`

| ID | 场景 | 一次要钉死的事 |
|---|---|---|
| C-1 | `test_writer_selects_from_metadata_and_turn_loads_only_selected_bodies` | work-plan 无全文；turn 只有 selected；Writer / Planner inventory 不同 |
| C-2 | `test_review_paths_use_admitted_lenses_only` | Editor = core + 至多三个 admitted lens；PlanReviewer temporal / parent-scope 不串到 Writer |

“用了 core + lens”和“lens 集合不超过 admitted 三个”是同一断言，写在 C-2 里。

合计：**8 个 pytest 场景**。实现文件里可以有更细的 unit；那些不是 Gate，本文不再点名。

真实长跑不在本目录。余烬九序 0–25 / 0–50 由操作方在 Gate 0 + Patch A/B/C 全部完成之后统一测试，不作为 Patch 之间的准入。

---

# 1. 对评审意见的采纳结论

本次评审提出的主要问题，除两个实现细节作调整外，基本全部采纳。

## 1.1 直接采纳

以下意见全部采纳，并写成实现不变量：

1. `ProductionStage4InvocationFactory` 不只是硬编码 `CHAPTER_SET`，还存在：
   - rolling horizon 必填；
   - `horizon_start > chapter_index`；
   - `TextRoot latest == chapter_index`；
   - `author_intent_artifacts` 必填；
   这些都必须按 mode 分开处理。

2. **一条 post-Genesis `PLAN_CANDIDATE` 只产生一个 planning level。**
   不允许 STORY task 同时产出 STORY node + ARC_VOLUME node。

3. PlanRoot replan 不能依赖“同 id 覆盖”自然完成树级失效，必须显式计算/声明 descendant invalidation。

4. STORY / ARC_VOLUME 不使用 `TaskRecord.horizon_start/horizon_end` 的 rolling chapter-goal 语义。

5. `not_before_chapter` 不能只是字段，必须形成 Reviewer → trusted validation → Writer constraint → observed writeback validation 的完整 fail-close 链。

6. 不新增 `WriterPlanProjection`。Writer 继续只走：
   - `WritingTaskContract`
   - `AuthorPlanningContext`
   - `WriterContextPackage`
   三个既有合同，其中计划约束只通过前两个既有路径投影。

7. 本轮 hierarchy 改造期间 **冻结 lookahead**：
   - `enable_planner_lookahead=False`
   - 不升级 `affects_future_plan`
   - 不引入 `PlanningImpact`
   后续单独评审。

8. Phase 0 不承诺把旧 1–23 run 升级到新 hierarchy schema 后继续恢复。

9. Editor 第一批只增加极少量 lens，而不是一次新建十几个 Skill。

10. schemas / domain / curator / proposal validation / benchmark fixture 必须同步，不允许“optional 字段所以不 bump contract”这种处理。

11. 不新增 `plan.inspect_parent` 一类伪 Tool。本轮 parent plan 由 host 预投影，不建设新的 tool-call loop。

12. Bootstrap 与后续 STORY 的关系必须明确，避免 Genesis plan 与正式 Story plan 双真源。

---

## 1.2 调整后采纳

### A. `SkillCard`

上一版新增 `domain/skills.py::SkillCard` 过重。

本版不新增新的 domain contract。

但也**不建议直接把 `summary/tags` 加到 `SkillContractRef`**，因为 `SkillContractRef` 是 content identity / contract ref，增加展示元数据会扩大 schema 与 fingerprint 语义。

第一版更小的做法：

```text
SkillTemplate
  + summary
  + tags
  + applicable_modes
```

这些字段只属于 registry/runtime metadata，不进入 Canon，不改变 `SkillContractRef` 身份。

即：

```text
SkillContractRef = immutable identity
SkillTemplate    = registry metadata + body location
```

---

### B. `PlanningLevel`

上一版提出单独 `PlanningLevel` 是合理的，但必须解决三套标签问题。

本版保留一个极小的结构级枚举，建议命名：

```python
PlanLevel
```

它只表达 Canon / runtime 的层级结构：

```text
STORY
ARC_VOLUME
CHAPTER_SET
CHAPTER
SCENE
```

明确规定：

```text
PlanLevel
= 结构层级，权威

AgentMode
= Agent 调用模式
  STORY/ARC_VOLUME/... 与 PlanLevel 1:1 映射
  REPLAN / PROJECT_BOOTSTRAP 不属于 PlanLevel

PlanNode.node_type
= 自由语义标签，例如 conflict / character_arc / reveal / phase
  不参与 runtime routing
```

冲突规则：

```text
PlanLevel 决定层级；
node_type 永远不能覆盖 PlanLevel；
AgentMode 由 runtime 根据 PlanLevel + purpose 映射；
REPLAN 是动作，不是层级。
```

---

# 2. 当前代码事实与本版必须保护的不变量

---

## 2.1 Stage 4 production 当前确实是 CHAPTER_SET-only

文件：

```text
src/novel_agent/adapters/runtime/stage4_planner.py
```

当前 `ProductionStage4InvocationFactory.__call__()` 有四个关键约束：

```python
if request.horizon_start is None or request.horizon_end is None:
    raise ValueError("production Stage 4 invocation requires a rolling horizon")
```

```python
if latest != request.chapter_index:
    raise ValueError(...)
```

```python
if request.horizon_start <= request.chapter_index:
    raise ValueError("Stage 4 horizon must begin after the committed chapter")
```

以及：

```python
planning_task = PlanningTask(
    ...
    mode=AgentMode.CHAPTER_SET,
)
```

并且 author intent 为空时会回退到整个 `ReferenceRootDocument`。

因此不能只改：

```text
mode=CHAPTER_SET
```

必须同时改：

```text
horizon semantics
text basis semantics
source semantics
context semantics
```

---

## 2.2 当前 Plan materializer 的 horizon 是 CHAPTER_SET 特有合同

文件：

```text
src/novel_agent/adapters/runtime/materializers.py
```

当前：

```python
if candidate.horizon_start is not None and candidate.horizon_end is not None:
    expected_chapters = tuple(range(...))
    actual_chapters = ...
    if actual_chapters != expected_chapters:
        raise CandidateMaterializationError(
            "Plan candidate must provide exactly one chapter goal for every "
            "chapter in its accepted horizon"
        )
```

这个约束必须保留给：

```text
CHAPTER_SET
```

不能扩展到：

```text
STORY
ARC_VOLUME
```

因此本版硬规定：

> **STORY / ARC_VOLUME candidate 的 `horizon_start` / `horizon_end` 必须为 `None`。**

它们的章节范围写在：

```text
PlanNode.chapter_start
PlanNode.chapter_end
```

而不是借用 runtime rolling horizon。

---

## 2.3 当前 PlanRoot 合并不是树级 supersede

当前逻辑：

```python
invalidated = {
    item_id
    for deviation in execution.deviations
    for item_id in deviation.affected_plan_item_ids
} - set(review.preserve_item_ids)
```

然后：

```text
旧 node：
  id 在 invalidated → 删除
  id 与 incoming 相同 → 覆盖
  其余 → 保留
```

没有：

```text
父节点重写
→ 自动移除所有 descendants
```

所以 hierarchy 接通之前必须先定义 descendant invalidation owner。

---

# 3. 新的硬不变量

以下不变量必须在代码里成立，并由 §0.1 的对应准入场景证明。INV 本身不是测试清单，不给每个 INV 单开 Gate 名。

---

## INV-1：一条 post-Genesis Plan candidate 只产出一个 PlanLevel

适用于：

```text
STORY
ARC_VOLUME
CHAPTER_SET
CHAPTER
SCENE
```

不适用于：

```text
PROJECT_BOOTSTRAP
```

因为 Genesis 是导入/初始化阶段，不作为正式 hierarchy candidate。

准入：Patch B 场景 `B-2`。

具体规则：

```python
proposal_level = trusted_task.plan_level

for incoming_node in candidate:
    assert incoming_node.plan_level == proposal_level
```

模型不允许在一个 STORY proposal 里直接创建 ARC_VOLUME node。

---

## INV-2：STORY / ARC_VOLUME 不使用 rolling horizon

准入：Patch B 场景 `B-1`。STORY / ARC_VOLUME / CHAPTER_SET 作为同一测试的参数，不拆成三个 Gate。

要求：

```text
STORY:
  horizon_start = None
  horizon_end   = None

ARC_VOLUME:
  horizon_start = None
  horizon_end   = None

CHAPTER_SET:
  horizon_start/end 必须有
  且继续要求每章一个 ChapterGoal

CHAPTER:
  如果以后接通 production：
  可以使用 chapter_index / single-chapter scope
  不复用 CHAPTER_SET 的 multi-chapter horizon validator
```

---

## INV-3：committed prefix 不允许被 replan 删除

定义：

```text
current_chapter = TextRoot 最后已 commit chapter
```

任何：

```text
CHAPTER / SCENE plan node
```

若其 scope 完全落在：

```text
<= current_chapter
```

则成为历史 planning record：

```text
不可被普通 REPLAN invalidate
不可因为 parent replan 自动删除
```

可以新增：

```text
superseding future node
deviation record
```

但不能重写已经发生过的历史。

准入：Patch B 场景 `B-3`。

---

## INV-4：replan 必须显式失效未执行 descendants

准入：Patch B 场景 `B-3`。与 INV-3、同范围旧 ChapterGoal 清理在同一场景中断言。

owner 选择：

> **由 trusted host / materializer 根据当前 PlanRoot 的 `parent_id + scope` 计算 descendants。**

不依赖模型列全 `affected_plan_item_ids`。

模型的：

```text
execution.deviations[].affected_plan_item_ids
```

仍作为语义起点。

Host 做：

```text
explicit invalidated roots
      ↓
descendant closure
      ↓
filter committed prefix
      ↓
effective invalidation set
```

伪代码：

```python
roots = model_invalidated_ids - preserve_ids

descendants = descendant_closure(
    current_plan,
    roots=roots,
)

effective = {
    node_id
    for node_id in roots | descendants
    if not node_is_committed_prefix(node_id, current_chapter)
}
```

---

## INV-5：Plan node identity 与 task generation 分离

`plan_node_id`：

```text
表示逻辑计划槽位/对象
```

`TaskRecord` generation：

```text
表示某次重新计算
```

规则：

### 同一个逻辑节点的修订

例如：

```text
volume-1
chapter-set-21-30
chapter-24
```

若仍是相同逻辑槽位：

```text
plan_node_id 保持稳定
```

这样 materializer 才能安全覆盖。

### 原节点被废弃并换成新的剧情结构

则：

```text
旧 node 显式 invalidated
新 node 新 id
```

### Task id

重规划必须新 identity：

```text
plan.chapter-set.24-28.g1
plan.chapter-set.24-28.g2
```

或等价 deterministic generation identity。

不能复用同一个 task id。

准入：`G0-2` 覆盖 replacement identity。Patch B 接通 hierarchy 后，新 id 形态升级为 `plan.<level>.<scope>.g<N>`，仍由 `G0-2` / `B-3` 断言，不另开 Gate。

---

## INV-6：future-locked obligation 必须 fail-close

准入：Patch A 场景 `A-1`（有窗仍提前兑现）与 `A-2`（该有窗却没写）。二者不可合并。

字段存在不算完成。

必须经过：

```text
Plan authoring
→ Plan Review
→ trusted proposal validation
→ Writer task projection
→ Draft observation / Curator write validation
```

任何一层发现：

```text
current_chapter < not_before_chapter
AND operation = RESOLVE/PAYOFF
```

都不能继续普通 acceptance/commit。

---

## INV-7：Writer 不新增第四种/第三种重复 plan product

本轮禁止新增：

```text
WriterPlanProjection
```

Writer 继续使用：

```text
WritingTaskContract
AuthorPlanningContext
WriterContextPackage
```

其中：

```text
WritingTaskContract
= 当前章执行约束

AuthorPlanningContext
= 当前章允许看到的 parent-plan 视图

WriterContextPackage
= Memory evidence product
```

不再新增第四种 wrapper。

准入：写在 `A-1` 的 Writer 约束/outline 断言里，不单开 Gate。

---

## INV-8：hierarchy 改造期间 lookahead 冻结

配置：

```python
enable_planner_lookahead=False
```

本轮：

```text
不改 affects_future_plan
不新增 PlanningImpact
不改 LookaheadRevalidationReceipt
不把 STORY/VOLUME replan 接到 lookahead
```

准入：写在 `G0-1` 的 policy/fixture 断言里。不要单开 “lookahead remains false” Gate。

只有 hierarchy + non-lookahead 稳定后，另开 admission。

---

# 4. Gate 0：先固定 cadence，再碰 hierarchy

这是本版最高优先级。

---

## 4.1 为什么必须先做

当前生产报告中的描述与之前对代码的口头理解曾经发生过不一致。

但 `ee8849a` 当前 non-lookahead 代码明确是：

```python
if task.chapter_index >= task.horizon_end:
    planning = self._rolling_plan_task(...)
else:
    draft = self._draft_task(..., task.chapter_index + 1)
```

因此在：

```python
enable_planner_lookahead=False
planning_horizon=5
```

时，预期：

```text
Plan 24–28
→ Draft 24
→ Draft 25
→ Draft 26
→ Draft 27
→ Draft 28
→ Plan 29–33
```

而不是每写完一章都 Plan +1。

这必须通过真实 production assembly trace 固定，而不是继续依赖描述。准入场景是 `G0-1`。

---

## 4.2 准入：G0-1

文件：

```text
tests/integration/test_production_planning_cadence.py
```

场景：

```text
G0-1  test_non_lookahead_run_consumes_full_horizon_then_plans_next
```

同一条里断言：

```text
plan.1-5 commit
→ draft.1
→ draft.2
→ draft.3
→ draft.4
→ draft.5
→ plan.6-10

policy.enable_planner_lookahead is False
```

lookahead 冻结写进这条的 fixture/policy 断言。不要再单开“False 仍是 False”的 Gate。本轮也不测旧 lookahead 全行为。

---

## 4.3 Gate 0-A：修 BLOCKED task identity collision

文件：

```text
src/novel_agent/services/creative_runtime.py
```

当前 `_repair_post_draft_projection()` 查 existing 时，会排除 BLOCKED，导致：

```text
旧 plan.24-28 = BLOCKED
→ Runtime 认为没有 foreground plan
→ 重新 create 同 ID
→ RuntimeCommandConflictError("task identity collision")
```

第一步修改：

```python
existing = next(
    (
        task
        for task in reversed(tasks)
        if projection.task_id in task.dependency_task_ids
        and task.kind in {TaskKind.PLAN_CANDIDATE, TaskKind.PLAN_ACCEPTANCE}
        and task.purpose is not TaskPurpose.LOOKAHEAD
        and not task.superseded
        and task.status not in {
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    None,
)
```

即：

```text
BLOCKED
= durable existing work
≠ nonexistent
```

---

## 4.4 真正替换 BLOCKED task 的规则

不能：

```text
create same ID again
```

必须：

```text
1. explicit supersede old task
2. create replacement identity
```

本轮不必立刻给整个 TaskRecord 加通用 `planning_generation`。

Phase 0 可以先用最小 replacement identity：

```python
replacement_id = bounded_runtime_identity(
    f"{old.task_id.root}.replacement.{old.task_revision + 1}",
    f"replacement-plan.{run_id}.{basis_commit}.{start}-{end}",
)
```

但在 Patch B hierarchy 正式接入时，应升级成统一的：

```text
plan.<level>.<scope>.g<N>
```

---

## 4.5 准入：G0-2

这三件事是同一次 replacement 的断言，不是三个 Gate：

```text
BLOCKED 算已存在的 durable work
替换必须显式 supersede
新 task 必须使用新 identity
```

场景：

```text
G0-2  test_blocked_plan_replacement_supersedes_and_does_not_reuse_task_id
```

---

# 5. Gate 0-B：Canon 前长度门

当前：

```text
WriterCognition
WriterGenerationService
WriterContextLoop
Editor repair
Major rewrite
```

存在多条文本产生路径。

真实运行已经证明：

```text
目标 3000–5000+
实际极短章
仍可能到达最终 accepted draft
```

因此最终 boundary 必须再次检查。

---

## 5.1 修改位置

文件：

```text
src/novel_agent/adapters/runtime/materializers.py
```

当前 Draft materializer：

```python
text = self._artifacts.read_verified(candidate.artifact_ref).decode("utf-8")
if not text.strip():
    raise CandidateMaterializationError("accepted Draft text is blank")
```

增加：

```python
length = len(text)
policy = writing_task.length_policy

if length < policy.minimum_characters:
    raise CandidateMaterializationError(
        "accepted Draft is shorter than trusted WritingTask minimum"
    )

if length > policy.maximum_characters:
    raise CandidateMaterializationError(
        "accepted Draft exceeds trusted WritingTask maximum"
    )
```

---

## 5.2 失败语义

当前 `CreativeRuntimeService` 已经对：

```python
CandidateMaterializationError
```

统一映射：

```text
AttemptOutcome.FAILED
TaskStatus.BLOCKED
FailureClass.VALIDATION_REJECTED
CreativeRunTerminal.REVIEW_REQUIRED
reason = candidate_materialization_rejected
```

因此第一版**不需要新 FailureClass**。

但建议为了运营诊断，做一个窄的 typed reason：

```text
draft_length_contract_rejected
```

实现方式有两种：

### 最小版本

保留异常类，只在 materializer error 文本和 artifact 中能看见。

### 推荐版本

新增：

```python
class DraftLengthContractError(CandidateMaterializationError):
    pass
```

Stage 5 单独映射：

```text
BLOCKED / REVIEW_REQUIRED
reason=draft_length_contract_rejected
```

不自动重写。

为什么不自动重写：

> Canon boundary 不能自己决定“再让 Writer 写一次”，否则 runtime 责任层混乱。

Operator / future bounded repair policy 再决定是否新建 rewrite task。

---

## 5.3 准入：G0-3

`local_repair` / `major_rewrite` × 低于 min / 超过 max，以及“不修改 TextRoot”，都是同一个 Canon 长度门的参数组合。

场景：

```text
G0-3  test_final_draft_outside_length_policy_cannot_mutate_text_root
```

实现用 parametrize。不要为每条路径单开 Gate。

---

# 6. Patch A：先关闭“提前兑现”最小闭环

这个 Patch 是当前最值得优先实现的产品修复。

它**暂时仍然允许 production 只跑 CHAPTER_SET**。

目标：

> 即使还没有正式 STORY / ARC_VOLUME production task，第 24 章也不能再把第 90 章以后才允许完成的 payoff 写掉。

---

# 7. Obligation 时间窗合同

---

## 7.1 修改 `PlanObligation`

文件：

```text
src/novel_agent/domain/memory.py
```

当前：

```python
due_chapter: int | None
```

增加：

```python
not_before_chapter: int | None = Field(default=None, ge=1)
target_chapter_start: int | None = Field(default=None, ge=1)
target_chapter_end: int | None = Field(default=None, ge=1)
```

保留：

```python
due_chapter
```

语义：

```text
not_before_chapter
= 最早允许 RESOLVE / PAYOFF

target_chapter_start/end
= 推荐兑现窗口

due_chapter
= 最晚应处理时间
```

---

## 7.2 validator

```python
@model_validator(mode="after")
def validate_timing(self):
    if (self.target_chapter_start is None) != (self.target_chapter_end is None):
        raise ValueError("target chapter window must be complete")

    if (
        self.target_chapter_start is not None
        and self.target_chapter_end is not None
        and self.target_chapter_end < self.target_chapter_start
    ):
        raise ValueError("target chapter window is reversed")

    if (
        self.not_before_chapter is not None
        and self.target_chapter_start is not None
        and self.target_chapter_start < self.not_before_chapter
    ):
        raise ValueError("target window starts before not-before boundary")

    if (
        self.due_chapter is not None
        and self.target_chapter_end is not None
        and self.due_chapter < self.target_chapter_end
    ):
        raise ValueError("due chapter precedes target window end")

    return self
```

---

# 8. Curator 合同必须同步

文件：

```text
src/novel_agent/domain/changes.py
```

当前：

```python
class CuratorObligationRecord:
    ...
    due_chapter: int | None
```

同步增加：

```python
not_before_chapter
target_chapter_start
target_chapter_end
due_chapter
```

所有下列构造/转换必须全仓审计：

```text
CuratorObligationRecord
→ CuratedOperationDraft
→ World change materialization
→ PlanObligation
→ WorldRoot
```

不能只改：

```text
memory.py
```

---

## 8.1 Schema 面

至少需要重新导出/更新：

```text
schemas/stage1/PlanObligation.schema.json
schemas/stage1/CuratorObligationRecord.schema.json
schemas/stage1/WorldRootDocument.schema.json
```

如果 Stage 2/3/4/5 contract transitively引用它们，也需要跑完整 schema parity。

不能“手改 JSON schema”。

统一从 Pydantic contract 重新生成。

---

# 9. 谁必须写 not_before：正式合同

这是本版最重要的新规范。

---

## 9.1 强制对象

以下 obligation kind：

```text
PROMISE
FORESHADOWING
```

以及明显属于长程 payoff 的：

```text
OBJECTIVE
```

只要它的语义是：

```text
未来卷节点
长期伏笔
终局真相
卷高潮奖励
关系阶段跃迁
关键能力/道具获得
```

必须有：

```text
not_before_chapter
```

若作者 brief 已给出卷范围，则还必须给：

```text
target_chapter_start
target_chapter_end
```

---

## 9.2 第一阶段谁生成

在 hierarchy 还没正式接入 production 前：

```text
PROJECT_BOOTSTRAP / bootstrap normalization
```

可根据明确作者章节/卷范围填入。

无法可靠推断时：

```text
不允许瞎造具体数字
→ unresolved / reviewer issue
```

Patch B 接通 hierarchy 后，正式 owner 变成：

```text
STORY 或 ARC_VOLUME
```

---

# 10. PlanReviewer fail-close

文件：

```text
src/novel_agent/agents/plan_reviewer.py
src/novel_agent/services/planning_context_loop.py
```

当前 Reviewer 已经是独立 acceptance gate。

需要把以下规则写入 core Plan Review Skill/Prompt 和 host validation：

```text
LONG_RANGE_PAYOFF_WITHOUT_TIME_WINDOW
EARLY_RESOLUTION_OF_FUTURE_LOCKED_OBLIGATION
TARGET_WINDOW_OUTSIDE_PARENT_SCOPE
```

决策：

```text
可自动补明确机械字段？
→ REVISE

需要作者决定具体在哪一卷/哪一阶段兑现？
→ HUMAN_REQUIRED

提前兑现违反已有 hard author plan？
→ REVISE / blocking
```

---

# 11. Trusted Plan validation 才是最终 fail-close

不能只靠 Reviewer Prompt。

在 Plan candidate 进入 `PlanRoot` 前增加 deterministic validation。

最合适的位置：

```text
PlanCandidateMaterializer._materialize()
```

在写新 Root 之前：

```python
validate_temporal_obligation_use(
    current_plan=current,
    incoming_nodes=incoming_nodes,
    incoming_goals=incoming_goals,
    world=...,
    target_chapters=...,
)
```

如果 `CHAPTER_SET 24–28` proposal：

```text
把 obligation O-silver 标成 resolved/payoff
而 O-silver.not_before_chapter = 85
```

直接：

```python
raise CandidateMaterializationError(
    "future-locked obligation cannot be resolved in this planning scope"
)
```

---

## 11.1 不要依赖 `proposal.validate_plan` Tool

当前 Planner 不是通用 tool-call loop。

因此本轮：

```text
不新增 tool
不指望模型主动 call validator
```

validator 是 trusted host boundary。

---

# 12. CHAPTER_SET Planner 只允许看到父级投影，不再默认吃全 brief

文件：

```text
src/novel_agent/adapters/runtime/stage4_planner.py
src/novel_agent/services/planner_context_assembler.py
```

当前：

```python
author_intent = request.input_artifact_refs
```

为空时：

```python
ReferenceRootDocument
→ all reference assets
```

而 assembler：

```python
for artifact in request.author_intent_artifacts:
    mandatory + protected
```

这是当前最直接的 global-intent leakage。

---

## 12.1 Patch A 的最小修法

暂时不新增：

```text
PlanProjection domain type
PlannerContextPolicy service
```

直接在现有 `PlannerContextAssembler` 内增加一个私有 selection helper：

```python
def _planning_source_items(
    self,
    request: PlanningLoopRequest,
    plan: PlanRootDocument,
) -> tuple[PlannerContextItem, ...]:
    ...
```

对于：

```text
CHAPTER_SET
```

规则改成：

```text
raw author intent：
  默认不进入 mandatory context

accepted plan：
  保留当前 horizon 相关 goals
  保留 parent nodes
  保留 active obligations
  保留 future-locked obligation 约束摘要

explicit_author_overrides：
  仍然 protected

ProjectProfile：
  仍然可见
```

---

## 12.2 原始 brief 什么时候还能读

只有两种情况：

```text
PROJECT_BOOTSTRAP
STORY
```

`ARC_VOLUME`：

```text
默认使用 STORY parent + 明确 author plan refs
```

不应该每轮完整读 800 章 brief。

---

## 12.3 Stage4 LeafAdapter 也要同步

当前：

```python
if not detailed.author_intent_artifacts:
    raise ValueError("Stage 4 CHAPTER_SET requires author-intent artifacts")
```

Patch A 后，CHAPTER_SET 允许：

```text
author_intent_artifacts = ()
```

只要：

```text
accepted_plan_ref
accepted_world_ref
accepted_text_ref
project_profile_ref
```

都存在。

所以这条检查必须改成 mode-aware。

---

# 13. Writer：复用现有合同，不新增 Plan wrapper

文件：

```text
src/novel_agent/adapters/runtime/stage3_writer.py
```

当前已经有：

```text
WritingTaskContract
AuthorPlanningContext
WriterContextPackage
```

本版不新增 `WriterPlanProjection`。

---

## 13.1 WritingTaskContract 直接接 temporal lock

已有：

```python
mandatory_constraints
forbidden_reveals
active_plan_obligations
```

直接投影：

### future-locked payoff

例如：

```text
银铭获取 not_before=85
当前 chapter=24
```

写入：

```python
mandatory_constraints += (
    "银铭相关长期目标当前只能 SETUP/PROGRESS，不得 RESOLVE/PAYOFF；最早第85章。",
)
```

必要时：

```python
forbidden_reveals += (
    "不得在本章完成银铭最终获得或宣布该长期目标已解决。",
)
```

不增加新的字段。

---

## 13.2 裁剪 `AuthorPlanningContext.visible_outline_nodes`

当前 Stage3 adapter 的 planning context 会从 PlanRoot 投影 outline。

第一版改为只包括：

```text
当前 CHAPTER_SET node
它的直接 parent ARC_VOLUME node（如果存在）
当前章相关 node
```

不再：

```text
全 PlanRoot.nodes
```

---

## 13.3 Writer 只知道：

```text
what matters now
what may progress now
what must not resolve yet
```

不需要知道：

```text
未来全部卷终章细节
```

---

# 14. Draft writeback 也必须检查 future lock

仅仅 Writer prompt 禁止不够。

Chapter Settlement / Curator 最终会把正文观察写入 WorldRoot。

如果 Writer 还是写出了：

```text
银铭到手
```

Observer / Curator 可能产生：

```text
obligation.status = resolved
```

因此写路径必须 fail-close。

---

## 14.1 Curator / Memory-write validation

当：

```text
record_kind = OBLIGATION
operation = REPLACE
new.status = resolved
```

必须读取旧 obligation：

```python
if (
    current_chapter < old.not_before_chapter
    and new.status == RESOLVED
):
    reject
```

对应：

```text
ValidationFinding
code = OBLIGATION_RESOLVED_BEFORE_NOT_BEFORE
blocking = true
```

---

## 14.2 Draft 本身是否直接拒绝

第一版不必做复杂 NLP validator 来判断正文有没有“事实上提前兑现”。

已有：

```text
CandidateObserver
Curator
chapter settlement
```

只要它可靠提取到 canonical state change，就在 canonical write gate 拒绝。

如果 Observer 发现但 Curator 没写出 obligation operation：

```text
不能当作 NONE / normal success
```

应进入：

```text
review required / unresolved observation
```

具体可在后续基于真实 false-negative 数据强化。

---

# 15. Patch A 验收

只承认两个准入场景，见 §0.1.2。

### A-1 银铭产品回归（唯一纵向场景）

固定输入：

```text
current_chapter = 24
obligation = 银铭最终获得
not_before = 85
target = 90–100
```

同一条测试里必须全部成立：

```text
CHAPTER_SET 上下文不含完整 raw brief
Writer 只通过现有 mandatory_constraints / forbidden_reveals 看到 lock
Writer outline 不含远期卷节点
Plan 24–28：可以 setup / progress，不可以 resolve
Writer 24：可以铺垫，不可以最终获得
plan review 与 materializer 拒收提前兑现
curator / settlement：RESOLVED operation fail-close
```

不要把 reviewer、materializer、writer、curator、brief isolation 再拆成六个 Gate。需要定位时，在 A-1 内对 `layer` parametrize。

### A-2 缺窗拒收

```text
长程 PROMISE / FORESHADOWING 没有 not_before
→ Reviewer REVISE / HUMAN_REQUIRED，或 materializer 拒收
```

A-1 与 A-2 不可合并。两条 pytest 都绿，才允许进入 Patch B。A-2 是 authoring 合同，与“有窗仍提前兑现”不是同一失败。不要求先跑真实 0–25。

---

# 16. Patch B：真正接通 STORY / ARC_VOLUME

完成 Patch A 后，再做 hierarchy。

---

# 17. `PlanLevel` 最小合同

建议新增到：

```text
src/novel_agent/domain/world.py
```

或一个已有的无 Stage2 依赖的 plan-domain 文件。

不建议为了一个 enum 单独新建大 domain 模块。

```python
class PlanLevel(StrEnum):
    STORY = "story"
    ARC_VOLUME = "arc_volume"
    CHAPTER_SET = "chapter_set"
    CHAPTER = "chapter"
    SCENE = "scene"
```

`PlanNode`：

```python
class PlanNode(DomainModel):
    ...
    plan_level: PlanLevel | None = None
    chapter_start: int | None = Field(default=None, ge=1)
    chapter_end: int | None = Field(default=None, ge=1)
```

迁移策略：

```text
旧 node：plan_level=None
新 production node：plan_level 必填
```

---

# 18. level / node_type / AgentMode 的唯一语义

正式写进 Stage4 design：

```text
PlanNode.plan_level
= 层级真值

PlanNode.node_type
= 该层内部的文学/业务类型
  例：
  story_conflict
  character_arc
  reveal_anchor
  volume_phase
  chapter_turn
```

Stage 4 adapter：

```python
MODE_BY_LEVEL = {
    PlanLevel.STORY: AgentMode.STORY,
    PlanLevel.ARC_VOLUME: AgentMode.ARC_VOLUME,
    PlanLevel.CHAPTER_SET: AgentMode.CHAPTER_SET,
    PlanLevel.CHAPTER: AgentMode.CHAPTER,
    PlanLevel.SCENE: AgentMode.SCENE,
}
```

REPLAN：

```python
mode = AgentMode.REPLAN
target_level = task.plan_level
```

---

# 19. `TaskRecord` 只增加真正必要字段

Patch B 才增加：

```python
plan_level: PlanLevel | None = None
planning_generation: int = Field(default=0, ge=0)
```

不增加：

```text
PlanningImpact
PlanProjection ref
Skill selection ref
...
```

---

## 19.1 plan task validation

新 production task：

```python
if kind in {PLAN_CANDIDATE, PLAN_ACCEPTANCE, PLAN_COMMIT}:
    assert plan_level is not None
```

旧 persisted task：

迁移期允许 `None`，但：

```text
不得作为 hierarchy 新 run 的 task
```

---

# 20. Stage4 factory 按 mode 拆语义，而不是只拆 mode 值

文件：

```text
src/novel_agent/adapters/runtime/stage4_planner.py
```

建议重构：

```python
def __call__(...):
    mode = self._mode(request)
    self._validate_basis(request, mode)
    sources = self._source_artifacts(request, mode, manifest)
    ...
```

---

## 20.1 STORY

允许：

```text
chapter_index = 当前已写到哪里
horizon_start/end = None
TextRoot latest == chapter_index 仍可要求
```

注意：

> `latest == chapter_index` 不是 CHAPTER_SET 专属，它表示 Planner 基于当前 canonical text cutoff。

所以这一条可以保留给 STORY / VOLUME。

真正要删除的是：

```text
horizon_start > current chapter
```

---

## 20.2 ARC_VOLUME

允许：

```text
horizon=None
```

其实际 scope 来自：

```text
trusted target parent / plan node range
```

例如：

```text
volume1 = 1–100
```

即使：

```text
current chapter = 24
```

也合法。

---

## 20.3 CHAPTER_SET

继续：

```text
horizon_start > chapter_index
horizon_end >= horizon_start
```

并继续保留完整 chapter-goal coverage gate。

---

## 20.4 source 规则

### STORY

```text
允许 raw author intent
ProjectProfile
current Plan seed
current Canon summary
```

### ARC_VOLUME

```text
parent STORY node
relevant explicit author plan source
current state
```

### CHAPTER_SET

```text
parent ARC_VOLUME node
current active plan
current state
future locks
不默认 raw full brief
```

---

# 21. Single-level candidate 强制点

最稳妥方式：

> runtime task 给出 trusted `plan_level`，proposal item 本身不需要被模型自由声明 level。

Materializer 构建 incoming node 时：

```python
PlanNode(
    ...
    plan_level=accepted/candidate trusted level
)
```

但由于 INV-1 已规定“一 task 一 level”，不会出现上一版的 mixed-level 覆盖问题。

---

## 21.1 STORY 不生成 Volume node

STORY 输出：

```text
premise
global conflict
reader promise
ending anchor
global character arcs
global reveal/payoff obligations
```

都属于：

```text
PlanLevel.STORY
```

Volume anchor：

另一次：

```text
ARC_VOLUME task
```

即便只生成 coarse volume anchor，也必须由 ARC_VOLUME task 产生。

---

# 22. STORY / ARC_VOLUME 的章节范围放 PlanNode，不放 horizon

例如：

```python
PlanNode(
    plan_level=PlanLevel.ARC_VOLUME,
    chapter_start=1,
    chapter_end=100,
)
```

而 task：

```text
horizon_start=None
horizon_end=None
```

Materializer：

```text
只有 CHAPTER_SET candidate
才执行“每章一个 ChapterGoal”的 horizon validator
```

---

# 23. Parent 范围 validator

PlanRoot 新写入时：

```python
child.chapter_start >= parent.chapter_start
child.chapter_end <= parent.chapter_end
```

允许 STORY range 为：

```text
None
```

代表 global。

ARC_VOLUME / CHAPTER_SET：

```text
必须 bounded
```

CHAPTER：

```text
start == end
```

---

# 24. Replan descendant invalidation

建议在：

```text
PlanCandidateMaterializer
```

新增私有函数：

```python
def _effective_invalidated_ids(
    current: PlanRootDocument,
    execution: PlannerExecutionResult,
    review: PlanReview,
    *,
    current_chapter: int,
) -> set[StableId]:
    ...
```

步骤：

```text
1. explicit roots
2. remove preserve ids
3. descendant closure
4. remove committed-prefix nodes
5. include old ChapterGoals in same future scope
```

---

## 24.1 ChapterGoal 也必须清

否则：

```text
旧 CHAPTER_SET 24–28 goals
+
新 CHAPTER_SET 24–28 goals
```

会并存。

建议 future CHAPTER_SET replacement：

```text
按 accepted target chapter range
移除旧未执行 ChapterGoal
```

这比依赖 goal_id 稳定更安全。

即：

```python
if plan_level is CHAPTER_SET:
    goals = (
        old goals outside target range
        + incoming goals
    )
```

前提：

```text
target range > committed current_chapter
```

---

## 24.2 已 commit chapter goal

保留为历史 planning record还是从 PlanRoot 清？

为了最小改造，建议：

```text
PlanRoot 继续保留
```

但 Writer selection 永远只取：

```text
target current/future chapter
```

不让过去 goal 干扰。

---

# 25. Genesis 与正式 hierarchy 的关系

当前：

```text
ProductionNovelBootstrap._plan_root()
```

会把 PROJECT_BOOTSTRAP proposal 写入：

```text
PlanRoot.nodes
ChapterGoal 1–5
```

这些内容不能与后续 STORY 再形成第二套全书计划。

---

## 25.1 本版定义

Genesis PlanRoot：

```text
= bootstrap planning seed
≠ 正式 hierarchy 完成态
```

在新 hierarchy run 中：

```text
STORY task
```

第一职责之一是：

```text
吸收 / refine bootstrap seed
```

并在 accepted STORY commit 时：

```text
invalidate 被正式 Story 替代的 unscoped bootstrap PlanNodes
```

---

## 25.2 Bootstrap ChapterGoal 1–5

第一版不必立刻删除 bootstrap 生成逻辑。

首个正式：

```text
CHAPTER_SET 1–5
```

commit 时：

```text
按 chapter range 替换未来 ChapterGoal
```

而不是按 `goal_id` 叠加。

这样不会出现双份 opening plan。

---

## 25.3 后续可以再简化 bootstrap

如果 hierarchy 跑稳定，再考虑：

```text
PROJECT_BOOTSTRAP 不生成 final opening ChapterGoal
```

但不属于本轮必须项。

---

# 26. hierarchy production 启动顺序

新 run：

```text
Genesis
 ↓
STORY candidate
 ↓
Story review
 ↓
Story commit
 ↓
ARC_VOLUME current-volume candidate
 ↓
Volume review
 ↓
Volume commit
 ↓
CHAPTER_SET opening candidate
 ↓
Chapter-set review
 ↓
Chapter-set commit
 ↓
Writer
```

不需要一次生成八个详细卷。

可以：

```text
STORY
= 全书全局

ARC_VOLUME
= 当前卷 detailed

未来卷
= 暂不作为正式 detailed ARC_VOLUME node
```

如果需要 coarse future-volume anchors：

```text
必须单独 ARC_VOLUME candidate
```

不能混进 STORY candidate。

---

# 27. hierarchy 后 cadence 仍保持固定

本轮 hierarchy 接通后，仍然：

```text
CHAPTER_SET 24–28
→ Writer 24
→ Writer 25
→ ...
→ Writer 28
→ next CHAPTER_SET
```

不同时引入：

```text
event-triggered multi-level replan
```

只有明确错误触发：

```text
TaskPurpose.REPLAN
```

才进入 replan。

这是为了控制变量。

---

# 28. Lookahead 明确冻结

Stage5 设计中 lookahead 已是正式扩展路径。

本次修订必须在执行文档明确：

```text
Hierarchy Migration Admission:
enable_planner_lookahead=False
```

所有新准入场景：

```text
non-lookahead only
lookahead 冻结写在 G0-1，不单开 Gate
```

不要现在改：

```text
LookaheadRevalidationReceipt
affects_future_plan
promotion rules
```

未来如果要启用：

```text
PlanLevel + REPLAN scope
```

必须与 lookahead 一起重新设计状态机。

---

# 29. Patch C：有限 Progressive Skill

Patch A 的 `A-1`/`A-2` 与 Patch B 的 `B-1`/`B-2`/`B-3` 绿了之后再做。不等待真实小说长跑。

---

# 30. 先拆 assembly Skill ownership

当前：

```text
production_assembly_spec.json
```

只有：

```json
"expected_skill_ids": ["skill.scene-composition"]
```

同时：

```python
WritingRequestPolicy.allowed_skills = spec.expected_skill_ids
Stage4InvocationPolicy.allowed_skill_ids = spec.expected_skill_ids
```

这是必须改的。

---

## 30.1 最小 spec

不要一上来设计复杂 `ProductionSkillPolicy` tree。

第一版只拆：

```python
writer_skill_ids: tuple[StableId, ...]
planner_skill_ids: tuple[StableId, ...]
editor_skill_ids: tuple[StableId, ...]
plan_reviewer_skill_ids: tuple[StableId, ...]
```

或 JSON：

```json
{
  "writer_skill_ids": [...],
  "planner_skill_ids": [...],
  "editor_skill_ids": [...],
  "plan_reviewer_skill_ids": [...]
}
```

保留：

```text
expected_skill_ids
```

仅作为 migration fallback 一版，然后删除。

---

# 31. Writer Progressive Disclosure 第一批

Writer 当前已经有：

```text
create_work_plan()
→ selected_skill_ids
→ take_turn()
```

真正的问题是：

```text
create_work_plan()
```

在选择前已经把所有 full Skill body 塞进去。

---

## 31.1 不新增 SkillCard domain

修改：

```text
src/novel_agent/skills/registry.py
```

`SkillTemplate` 增加 registry metadata：

```python
summary: str = ""
tags: tuple[str, ...] = ()
applicable_modes: tuple[str, ...] = ()
```

新增：

```python
def describe(skill_id, version) -> str:
    ...
```

输出小卡片：

```text
ID
summary
tags
```

---

## 31.2 Writer work-plan

从：

```python
text, resolved = self._skills.resolve(...)
skill_payload.append(full body)
```

改为：

```python
skill_payload.append(self._skills.describe(...))
```

然后：

```text
WriterWorkPlan.selected_skill_ids
```

选出 1–3 个。

`take_turn()` 继续使用当前已有：

```python
self._skills.resolve()
```

加载完整 body。

这是最低风险的 progressive disclosure。

---

# 32. Planner Skill

Planner 已有：

```text
PlanningTurnDraft.selected_skill_ids
PlanningTurnOutput.selected_skill_ids
PlanningLoopRequest.allowed_skill_ids
```

但本轮不建设新的 `PlannerSkillResolver` service。

直接在：

```text
planner.py / production stage4 policy
```

用 mode-specific static allowlist。

例如：

```python
PLANNER_SKILLS_BY_MODE = {
    AgentMode.STORY: (...),
    AgentMode.ARC_VOLUME: (...),
    AgentMode.CHAPTER_SET: (...),
    AgentMode.CHAPTER: (...),
    AgentMode.REPLAN: (...),
}
```

第一版：

```text
host 选择 core skills
模型 selected_skill_ids 只能选 optional subset
```

等真实 selection 数据稳定后，再决定是否抽 service。

---

# 33. Editor：是，需要 Skill，但第一批只三个 lens

当前 Editor 已经有 core：

```text
skill.editor-review
skill.editor-local-repair
```

继续保留。

新增第一批只建议三个：

```text
skill.editor.chapter-length
skill.editor.plan-adherence-hook-payoff
skill.editor.pacing-repetition
```

---

## 33.1 为什么只有三个

当前真实运行明确暴露：

```text
短章
计划提前兑现
节奏/重复/剧情拉回
```

其他：

```text
dialogue
POV
exposition
character voice
...
```

等真实 issue distribution 再 admission。

---

## 33.2 第一版不用新 Resolver class

直接在：

```text
src/novel_agent/services/editorial.py
```

写一个私有纯函数：

```python
def _selected_editor_lenses(
    review_input: EditorialReviewInput,
    *,
    prior_report: EditorialReport | None = None,
) -> tuple[StableId, ...]:
    ...
```

例如：

```text
length close to/outside boundary
→ chapter-length

active_plan_obligations / forbidden_reveals
→ plan-adherence-hook-payoff

multi-beat / previous repetition issue
→ pacing-repetition
```

如果逻辑未来扩张，再抽 `EditorSkillResolver`。

---

# 34. PlanReviewer Skill

同理，不先建多个 Reviewer Agent。

保留：

```text
skill.plan-review
```

只增加两个最必要的 review lens：

```text
skill.plan-review.temporal-obligation
skill.plan-review.parent-scope
```

这两个直接服务 Patch A/B。

---

# 35. 文档权威与仓库生命周期

上一版最大治理问题之一是“新文档自己宣布正式执行”。

本版明确：

> 本文件只能作为 review input / patch proposal。

若接受，应把规范内容合入：

```text
docs/stage4_planner_core_overall_design.md
docs/stage4_planner_context_loop_execution.md
docs/stage5_long_running_creative_runtime_overall_design.md
docs/stage5_long_running_creative_runtime_execution.md
docs/stage2_to_stage5_unified_long_running_agent_integration_execution_20260818.md
docs/project_status.md
docs/README.md
```

---

## 35.1 Stage 4 需要补的规范

至少更新：

```text
Planner modes：
  production 不再 CHAPTER_SET-only

Context：
  CHAPTER_SET 默认不消费完整 raw brief

Plan candidate：
  single PlanLevel invariant

STORY / ARC_VOLUME：
  no rolling horizon

Plan Review：
  temporal obligation window fail-close
```

---

## 35.2 Stage 5 需要补

至少更新：

```text
rolling policy：
  hierarchy migration 仍是 fixed CHAPTER_SET cadence

replan：
  purpose=REPLAN + target PlanLevel

lookahead：
  hierarchy migration 暂时 disabled

Task identity：
  supersede + generation identity

old run：
  hierarchy schema 只用于 fresh run
```

---

## 35.3 V0.5 不动

本轮不得修改：

```text
V0.5 four-condition contract
Stage 2M WriterContextPackage semantic contract
Benchmark task meaning
```

Writer 计划投影只调整 production Stage3 adapter 的 plan visibility，不新建 V0.5 input contract。

---

# 36. Schema / contract 必改面

实现者必须在 coding plan 中逐项检查。

至少：

```text
src/novel_agent/domain/memory.py
src/novel_agent/domain/changes.py
src/novel_agent/domain/world.py
src/novel_agent/domain/benchmark.py
src/novel_agent/domain/runtime.py
src/novel_agent/domain/creative_runtime.py
src/novel_agent/domain/planning.py
```

对应 schema：

```text
schemas/stage0/PlanNode.schema.json
schemas/stage1/PlanObligation.schema.json
schemas/stage1/CuratorObligationRecord.schema.json
schemas/stage1/PlanRootDocument.schema.json
schemas/stage2/PlanProposal.schema.json
schemas/stage2/PlannerExecutionResult.schema.json
schemas/stage3/WritingTaskContract.schema.json  # 仅如字段变化
schemas/stage4/PlanningLoopRequest.schema.json
schemas/stage5/TaskRecord.schema.json
schemas/stage5/PlanningLoopRequest.schema.json
schemas/stage5/CandidateBinding.schema.json
```

只更新真正受模型变化影响的 schema。

要求：

```text
schema parity test 必须绿
```

---

# 37. Benchmark / fixture 策略

旧 benchmark fixture 不要求自动升级成 hierarchy。

建议：

```text
legacy PlanNode.plan_level = None
```

只对：

```text
new production hierarchy test fixtures
```

要求 level。

这样不会为了一个生产修复，重写整个 Stage1/2 历史测试语义。

---

# 38. 不恢复旧《余烬九序》 1–23 到 hierarchy schema

Gate 0 修：

```text
collision
length gate
cadence trace
```

但 Patch B hierarchy 开始后：

```text
fresh project_id
fresh run_id
fresh Genesis
```

重新跑。

原因：

```text
旧 TaskRecord
旧 PlanRoot
旧 bootstrap seed
旧 lookahead/plan identity
```

与新 hierarchy semantics 不应混合迁移。

---

# 39. 推荐执行顺序

---

## Step 0 — 建干净生产工作树

基线：

```text
ee8849a
```

不得：

```text
在 codex/stage2m-semantic-closure 上直接修改
```

先确认：

```text
git status clean
HEAD == ee8849a（或经明确审核后的 fast-forward successor）
```

---

## Step 1 — 文档先收口

在写代码前：

```text
更新 Stage4 active design
更新 Stage5 active design
在统一执行计划增加该 patch 的 Gate
更新 docs/README 导航
```

不要创建第二份“正式执行权威”。

本文件可以保留为：

```text
HISTORICAL_REVIEW_INPUT
```

---

## Step 2 — Gate 0

只改：

```text
scheduler cadence trace
BLOCKED identity handling
final length gate
```

Gate：

```text
[ ] G0-1 绿
[ ] G0-2 绿
[ ] G0-3 绿
[ ] hierarchy schema 尚未引入
```

---

## Step 3 — Patch A：anti-premature-payoff

改：

```text
obligation timing fields
curator timing fields
schemas
PlanReviewer temporal check
trusted Plan materializer validation
CHAPTER_SET raw brief isolation
WritingTask existing constraints projection
AuthorPlanningContext clipping
Curator/settlement early-resolve validation
```

Gate：

```text
[ ] A-1 绿（银铭纵向链，含 brief isolation / writer 约束 / review / materializer / curator）
[ ] A-2 绿（缺窗拒收；不可与 A-1 合并）
[ ] current V0.5 contract unchanged
```

A-1 / A-2 绿即可进入下一 Step。不插入真实 0–25 长跑。

---

## Step 4 — Patch B：hierarchy production

再接：

```text
PlanLevel
single-level candidate
STORY
ARC_VOLUME
parent scope
descendant invalidation
CHAPTER_SET parent projection
```

Gate：

```text
[ ] B-1 绿（Stage4 三 mode 参数化）
[ ] B-2 绿（单层 + parent scope + Genesis 不双真源）
[ ] B-3 绿（future descendants 失效 + committed prefix 保留）
```

B-1 / B-2 / B-3 绿即可进入下一 Step。不插入真实 0–50 长跑。

---

## Step 5 — Patch C：Progressive Skill

依次：

```text
1. split role skill IDs
2. Writer card-first
3. Planner static mode allowlists
4. PlanReviewer two lenses
5. Editor three lenses
```

不要反过来。

Gate：

```text
[ ] C-1 绿
[ ] C-2 绿
```

---

# 40. 推荐提交拆分

```text
1. test(runtime): pin non-lookahead production planning cadence

2. fix(runtime): preserve blocked plan identity and explicit replacement

3. fix(writer): reject final drafts outside trusted length contract

4. feat(memory): add enforceable obligation timing windows

5. feat(planner): fail closed on future-locked payoff plans

6. fix(context): stop chapter-set planner from consuming full raw brief

7. fix(writer): project future locks through existing writing contracts

8. feat(plan): add single-level hierarchical plan scopes

9. feat(stage4): admit story and arc-volume production modes

10. fix(plan): invalidate only future descendants during replan

11. fix(skills): split production role skill inventories

12. feat(writer): select skills from metadata before loading bodies

13. feat(review): add minimal temporal/pacing editor and plan-review lenses
```

不要一次一个巨 commit。

---

# 41. 准入场景目录（唯一清单）

完整表只在 **§0.1**。这里只复述 ID，禁止再增加 Gate 名。

```text
G0-1  cadence + lookahead 冻结写在同一条
G0-2  BLOCKED replacement（存在 / supersede / 新 id）
G0-3  Canon 长度门（路径 × 上下界 parametrize）

A-1   银铭第24章不得 payoff（纵向多层 assert）
A-2   长程义务缺 not_before 拒收

B-1   Stage4 三 mode 基础规则
B-2   单层 candidate + parent scope + Genesis 替换
B-3   replan：future descendants 失效，committed prefix 保留

C-1   Writer card-first + 角色 inventory 分离
C-2   Editor / PlanReviewer 只用 admitted lens
```

层内分支、schema parity、现有 contract 测试继续跑，但不进入本目录。

真实长跑不在本目录。操作方在 8 个场景全部绿了之后统一测余烬九序，不回写为本方案 Gate。

---

# 42. 真实长跑不在本方案准入内

0–25 / 0–50 / 余烬九序实跑由操作方在 Gate 0 + Patch A/B/C 全部完成之后统一测试。本文不规定其节奏、指标或通过条件，也不把它们插进 Patch 之间。

A-1 的银铭案例是 **pytest 纵向场景**，不是真实 24 章生产 run。

---

# 43. Progressive Skill 观察（C-1 / C-2）

准入场景只有 `C-1` 和 `C-2`。不要用“Prompt 里出现 Skill”当成功。

比较：

```text
card-first vs full-body-before-selection
```

实现期可观察：

```text
input token
selected skill count
Editor issue precision
repair success
Writer repetition/pacing
```

第一轮 Editor 只允许 admitted 三个 lens（写在 C-2 里，不另开 Gate）：

```text
chapter-length
plan-adherence/hook-payoff
pacing/repetition
```

如果后续真实 issue distribution 证明需要，再扩。那是统一测试之后的 admission，不是本方案 Gate。

---

# 46. 明确后置：event-triggered adaptive replanning

这个方向仍然正确，但不进入当前 Patch A/B/C。

后续 admission 前提：

```text
1. hierarchy PlanLevel 已稳定（B-1 / B-2 / B-3）
2. descendant invalidation 已验证（B-3）
3. old bool affects_future_plan 的语义已审计
4. lookahead 状态机重新统一设计
5. 操作方统一测试通过（不在本文 Gate 内）
```

届时再考虑：

```text
NONE
CHAPTER
CHAPTER_SET
ARC_VOLUME
STORY
```

typed impact。

但它必须和：

```text
lookahead promotion
lookahead supersede
REPLAN target level
```

一起设计，不能只替换一个 bool。

---

# 47. 明确后置：CHAPTER / SCENE production mode

本次 hierarchy 首先接：

```text
STORY
ARC_VOLUME
CHAPTER_SET
```

`CHAPTER`：

可以随后接，但不是解决当前提前兑现问题的必要条件。

`SCENE`：

保持已有合同能力，不要求 production 每章必跑。

理由：

```text
先解决 global→volume→local scope
再决定是否需要更细 planning leaf
```

---

# 48. 本轮明确不做

```text
× 新 HierarchicalPlannerAgent
× 新 Segment mode
× 每层一个 PlanRoot
× 每层一个 TaskKind
× WriterPlanProjection
× 第二个 PlannerContextPackage
× domain/skills.py SkillCard
× Planner 通用 tool-call loop
× plan.inspect_parent tool
× PlanningImpact
× 改 lookahead
× Temporal cutover
× Skill evolution / hot swap
× 一次新增 10+ Editor Skills
× hierarchy schema 迁移旧 1–23 run
× 修改 V0.5 four-condition semantics
```

---

# 49. 最终文件级修改清单

## Gate 0

```text
src/novel_agent/services/creative_runtime.py
src/novel_agent/adapters/runtime/materializers.py
tests/integration/test_production_planning_cadence.py   # G0-1, G0-2, G0-3
```

## Patch A

```text
src/novel_agent/domain/memory.py
src/novel_agent/domain/changes.py
src/novel_agent/adapters/runtime/materializers.py
src/novel_agent/adapters/runtime/stage4_planner.py
src/novel_agent/services/planner_context_assembler.py
src/novel_agent/agents/plan_reviewer.py
src/novel_agent/adapters/runtime/stage3_writer.py
Curator / memory-write validation owner
schemas/stage1/*
schemas/stage2/*
schemas/stage3/*
schemas/stage4/*
tests/integration/test_future_locked_obligation_enforcement.py   # A-1, A-2
```

## Patch B

```text
src/novel_agent/domain/world.py
src/novel_agent/domain/benchmark.py
src/novel_agent/domain/runtime.py
src/novel_agent/domain/creative_runtime.py
src/novel_agent/domain/planning.py
src/novel_agent/adapters/runtime/stage4_planner.py
src/novel_agent/adapters/runtime/materializers.py
src/novel_agent/services/creative_runtime.py
src/novel_agent/services/runtime_commands.py
src/novel_agent/runtime/production_novel_bootstrap.py
schemas/stage0/PlanNode.schema.json
schemas/stage1/PlanRootDocument.schema.json
schemas/stage2/PlanProposal.schema.json
schemas/stage4/PlanningLoopRequest.schema.json
schemas/stage5/TaskRecord.schema.json
schemas/stage5/PlanningLoopRequest.schema.json
tests/integration/test_plan_hierarchy_production.py   # B-1, B-2, B-3
```

## Patch C

```text
src/novel_agent/domain/production_assembly.py
src/novel_agent/runtime/production_assembly_spec.json
src/novel_agent/runtime/production_bootstrap.py
src/novel_agent/skills/registry.py
src/novel_agent/services/writer_cognition.py
src/novel_agent/agents/planner.py
src/novel_agent/agents/plan_reviewer.py
src/novel_agent/agents/editor.py
src/novel_agent/services/editorial.py
skills/ 新增少量 admitted skill 文件
tests/unit/test_progressive_skill_disclosure.py   # C-1, C-2
```

---

# 50. 文档更新清单

正式开始实现前必须修改：

```text
docs/stage4_planner_core_overall_design.md
docs/stage4_planner_context_loop_execution.md

docs/stage5_long_running_creative_runtime_overall_design.md
docs/stage5_long_running_creative_runtime_execution.md

docs/stage2_to_stage5_unified_long_running_agent_integration_execution_20260818.md
docs/project_status.md
docs/README.md
```

如果这些文档尚未更新：

```text
本方案只能作为 review input
不得称“权威执行方案”
```

---

# 51. 推荐的最终决策

本轮不要实施上一版“八个 Phase 全部串起来”的大改。

推荐按以下顺序：

```text
P0 / Gate 0：
G0-1 cadence
G0-2 collision replacement
G0-3 length gate

P1 / Patch A：
A-1 银铭纵向 fail-close
A-2 缺窗拒收

P2 / Patch B：
B-1 Stage4 三 mode
B-2 单层 + parent scope
B-3 replan descendants

P3 / Patch C：
C-1 Writer card-first
C-2 admitted review lenses

完成后由操作方统一测试真实长跑，不插入上述 Patch 之间。

Later：
event-triggered multi-level replan
lookahead integration
CHAPTER/SCENE deeper production
```

这个顺序直接对应当前真实故障：

```text
“剧情推进过快”
不是因为没有足够多 Planner Agent，
而是因为局部 Planner 获得了错误的全局信息权限，
并且长期 obligation 没有机械的时间边界。
```

因此第一目标不是增加 hierarchy 的“形”，而是先建立：

```text
长期目标有时间窗
局部 Planner 只看到当前父级约束
Writer 收到硬的不可提前兑现约束
Canon writeback 真的会拒绝违规
```

当这条链闭合后，再接 STORY / ARC_VOLUME，hierarchy 才真正有意义。

---

# 52. 一句话版本

> **先把“未来节点为什么不能现在发生”变成可执行合同，再把全书→卷→段层级接进 production；Skill 只在这条叙事控制链稳定后按角色做有限渐进披露。**

这是相对于 `ee8849a` 最小、最稳、最符合现有 Canon / Stage4 / Stage5 不变量的下一步。
