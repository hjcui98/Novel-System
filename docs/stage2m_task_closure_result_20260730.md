# Stage 2M 任务闭环实施与验收结果（2026-07-30）

> 文档生命周期：`HISTORICAL`
>
> 本文记录 WP8 诊断运行前的 WP7 结果。2026-07-31 当前状态和新结果见
> `docs/project_status.md`，本文原始指标不改写。

## 结论

Stage 2M 的代码、契约、冻结、逐 Gold 评测、双 profile 历史 checkpoint
运行入口和统一报告已经实现。最终 deterministic A-arm 已在两个独立项目和数据库上
完成 C20/C40/C60/C80/C95，共十个真实 checkpoint 评测，future leakage 为 0，
Canonical 项目均保持 C95 / 96 commits。

本阶段不能宣告质量通过，也不能进入 WP8 的真实 ABC。原因不是数据库认证或运行
基础设施，而是 WP7 的质量准入失败：

- `visible_at_cutoff` 五点均 READY，但 macro weighted coverage 仅
  `0.008108`，mandatory hit rate 为 `0`；
- `author_plan_conditioned` 只有 3/5 READY，C40/C60 为
  `EVIDENCE_INSUFFICIENT`；macro weighted coverage 为 `0.019048`，
  mandatory hit rate 为 `0.053333`；
- 两个 profile 都远低于历史证据 recall `>= 90%`、operational/plan
  coverage `>= 95%` 的目标；
- 人工 Gold 仍以章节标签为主，未完成规划要求的精确 span、
  `target_components` 和 `accepted_evidence_sets` 双审标注，自动结果尚未达到
  可作为晋级依据的人机一致性门槛。

因此当前正式阶段结论是：

> **Stage 2M implementation closed / deterministic quality gate failed /
> WP8 agentic comparison not authorized**

deterministic Memory Gateway 继续冻结；ADR-0003 不变。

## 最终真实运行证据

两个最终 run 使用相同代码指纹：

`sha256:d749c94e6560ed32a052919aebb46decbc858e4f094b97f94fa436f7253a0ab6`

版本 attestation：

- experiment manifest schema `3`
- public task `memory_context_task.v1`
- task focus `task_focus.v3`
- Need Generator `task_plan_conditioned_need.v3`
- Retrieval Unit Normalizer `retrieval_unit_normalizer.v1`
- Writer Context contract `writer_context.v1`
- Writer Context Assembler `writer_context_assembler.v3`
- Gold matcher `gold_evidence_matcher.v2`
- evaluator `per_gold_v2`

最终产物：

- Visible:
  `reports/stage2m/writer_context_benchmark/visible_at_cutoff/qwen36_final_v7_a_20260730/`
- Author+Plan:
  `reports/stage2m/writer_context_benchmark/author_plan_conditioned/qwen36_final_v3_a_20260730/`
- Cross-profile:
  `reports/stage2m/writer_context_benchmark/cross_profile/qwen36_final_a_20260730/`

每个 profile 的顶层目录均包含：

- `flow_summary_C20...C95.json`
- `e2e_paired_report_C20...C95.json`
- `paired_case_C20...C95.json`
- `stage2m_case_C20...C95_A.json`
- `e2e_paired_report_all_checkpoints.json`
- `unified_report.json`

每个 checkpoint 另有独立 immutable manifest 和冻结目录
`checkpoints/C20...C95/`，避免历史 `resume_commit` 互相覆盖。

### Visible-at-cutoff

| Checkpoint | Assembly | Writer tokens | Weighted coverage | Mandatory hit | Untraceable |
|---|---:|---:|---:|---:|---:|
| C20 | READY | 554 | 0 | 0 | 0.5000 |
| C40 | READY | 2,020 | 0 | 0 | 0 |
| C60 | READY | 2,824 | 0 | 0 | 0 |
| C80 | READY | 3,770 | 0 | 0 | 0.6000 |
| C95 | READY | 3,787 | 0.040541 | 0 | 0.545455 |

聚合：5/5 READY，macro weighted coverage `0.008108`，
macro mandatory hit `0`，contradiction rate `0`，untraceable rate
`0.329091`。

### Author-plan-conditioned

| Checkpoint | Assembly | Writer tokens | Weighted coverage | Mandatory hit | Untraceable |
|---|---:|---:|---:|---:|---:|
| C20 | READY | 1,810 | 0 | 0 | 0.384615 |
| C40 | EVIDENCE_INSUFFICIENT | 3,375 | 0 | 0 | 0 |
| C60 | EVIDENCE_INSUFFICIENT | 3,700 | 0 | 0 | 0 |
| C80 | READY | 3,993 | 0 | 0 | 0.266667 |
| C95 | READY | 4,000 | 0.095238 | 0.266667 | 0.0625 |

聚合：3/5 READY，macro weighted coverage `0.019048`，
macro mandatory hit `0.053333`，contradiction rate `0`，untraceable rate
`0.142756`。

相对 Visible，Author+Plan weighted coverage 仅增加 `0.010940`，
mandatory hit 增加 `0.053333`；这不是可晋级的稳定质量收益。

## WP0–WP8 审计

| WP | 状态 | 证据或 blocker |
|---|---|---|
| WP0 | 完成 | 五个 legacy P0 已由 scrubbed regression fixtures 固化 |
| WP1 | 完成 | public/private 物理类型、schema、profile 和 taint tests 已闭合 |
| WP2 | 完成 | Task/Plan Need v3；world-scale property test 证明不随 1,000 个无关状态线性膨胀 |
| WP3 | 完成 | Writer Context/Evidence Ledger 分层、硬预算、冲突与 typed failure 已实现 |
| WP4 | 部分完成 | matcher/evaluator/freeze receipt 完成；人工 Gold 精确 span/components/双审未完成，真实质量未过 |
| WP5 | 完成代码 | A/B/C 都能独立重组和冻结；timeout/fallback 为 typed artifact；五点聚合修复 |
| WP6 | 未通过 | P001/P003 自动质量与人工可读性准入未形成通过证据 |
| WP7 | 已运行但失败 | 双 profile 五点 A 均有结果；Author C40/C60 非 READY，质量阈值显著失败 |
| WP8 | 未启动（正确行为） | 文档规定仅 WP7 通过后可启动；当前启动 ABC 会违反执行顺序 |

## Definition of Done 审计

1. 安全 task contract：通过。
2. public/evaluator-only 分离和 freeze-before-reveal：通过。
3. Task + 合法 Plan 驱动 Need：通过。
4. READY Writer Context 不超预算：通过；十个结果无 silent overflow。
5. Writer 专用分区：通过。
6. 结论与 Evidence Ledger 分层：通过。
7. Gold type/why/mandatory/weight/accepted evidence 契约保留：代码通过；
   私有 bundle 的 accepted evidence 精标内容仍不完整。
8. 五类逐 Gold 状态、解释、证据和 verifier receipt：通过。
9. A/B/C 真实 Context 重组：代码和测试通过；真实 ABC 因 WP7 失败未获准。
10. 双 profile 五点统一报告：deterministic A 通过；ABC 报告未运行。
11. deterministic 门后才允许 agentic：遵守，未越级。
12. `make quality` 和 integration：通过。

因此不能把 DoD 记为全部完成。

## 实施中额外修复

- 历史 checkpoint 解析不再假定“链距离等于章节号”，而是读取 commit 的
  TextRoot 章节数；兼容 Genesis/审批额外提交。
- C95 项目可只读评测历史五点，不重写章节、不移动 progress head。
- 每个 checkpoint 使用独立 immutable experiment manifest。
- derived snapshot attestation 存在但物理 OpenSearch index 已被清理时，
  自动重建可再生投影并发布新 attestation。
- 五点 aggregator 支持 checkpoint 子目录并拒绝缺 case。
- 非 READY A-arm 生成 typed-failure Stage 2M case report，逐 Gold 为 MISS，
  不再从 unified report 静默消失。
- manifest 版本字段从实现常量生成，消除硬编码版本漂移。
- PostgreSQL 凭据仅由根目录 `.env` launcher 加载到进程内存；报告保留
  无用户名密码的安全数据库描述符。

## 质量验证

- `make quality`: `1259 passed, 9 deselected`
- statement/branch coverage: `100%`
- ruff/format/mypy: 通过
- freeze/reveal integration: `1 passed`
- private bundle validator:
  `OK: ztj_memory_pilot_v0.1 cases=5`
- 两个 final run future leakage: `0`
- Canon 项目：两条轨道都保持 C95 / 96 commits

## 下一轮唯一合法主线

1. 对 P001/P003 先完成人工精确 Gold 标注：
   text object/block、精确 span、可接受证据组合、target components 和双审记录。
2. 用人审结果校准 `per_gold_v2`，先达到自动/人工一致性门。
3. 按 F-NEED/F-ROUTE/F-RETRIEVE/F-RANK/F-ASSEMBLY/F-EVAL 对 MISS 单一归因；
   优先修 Author C40/C60 的 `EVIDENCE_INSUFFICIENT`。
4. P001/P003 通过后扩展 P002/P004/P005 精标并重跑 WP7。
5. 只有 WP7 达到质量阈值后，才能用固定预算启动真实 ABC/WP8。
