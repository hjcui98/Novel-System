# 盲修移植追踪表（R0 建立，R1–R5 更新）

来源：远端盲修提交 [`b79a75151587a8c4d615e056175f694283b6b973`](https://github.com/hjcui98/Novel-System/commit/b79a75151587a8c4d615e056175f694283b6b973)
获取方式：git 传输不可用（`github.com:443` 超时），改用 codeload 精确 commit 压缩包
SHA-256 `1a0df4e8a95f6d51ecb141fed02241bc6699fc1c3d41c508fc133c7a43f166f0`，解包后逐文件比对。
共同祖先：`1856153298856bdcb6eb7a8c6ad62f0727ac9683`。本地整合基线：`46dcf05`（生产 HEAD `934b3fb` + dirty 成果）。

## 1. 规模事实（决定不能整批取一侧）

相对于整合基线，盲修改动 = 169 modified + 18 added − 17 removed。源码 100 个文件：
本地 47,725 行 → 盲修 46,110 行（净 −1,615 行）。**盲修是并行分支，不是本地的超集**：

| 文件 | 本地 | 盲修 | 差异 |
|---|---|---|---|
| `adapters/runtime/materializers.py` | 1763 | 1019 | −744 |
| `services/task_conditioned_need_generation.py` | 3044 | 2667 | −377 |
| `runtime/production_bootstrap.py` | 1911 | 1542 | −369 |
| `runtime/production_novel_bootstrap.py` | 1515 | 1170 | −345 |
| `agents/plan_reviewer.py` | 658 | 386 | −272 |
| `services/writer_cognition.py` | 863 | 602 | −261 |
| `domain/editorial.py` | 632 | 696 | +64 |
| `services/model_gateway.py` | 1083 | 1122 | +39 |
| `services/creative_runtime.py` | 2512 | 2548 | +36 |

逐文件差量清单：工作区 `tmp/yujin-remediation-20260912/blindfix/delta-sizes.txt`。
本地 dirty 45 个变更文件中，26 个（源码 21 个）与盲修变更集重叠；按功能逐项移植。

## 2. 本地缺失、需适配移植的盲修模块

| 盲修新增文件 | 行数 | 本地状态 | 责任任务包 |
|---|---|---|---|
| `services/ordinary_curation.py` | 417 | 本地无此模块（现有 `model_curation.py` 承担单次抽取） | R4：分段/分页/工作集/整章聚合 |
| `services/planning_contracts.py` | 167 | 本地无此模块 | R1/R2：声明、有效义务、章节节点、milestone 口径 | 
| `services/planning_sources.py` | 94 | 本地无此模块 | R2：来源权限（受信章节 vs 工作计划/指令/候选） |
| `schemas/stage3/MemoryGapAssessment.schema.json` | — | 本地无 | R3：缺口评估契约 |
| `schemas/stage3/PlanRequirementAssessment.schema.json` | — | 本地无 | R2：计划要求评估契约 |

## 3. 机制级采纳决定与移植落点

| # | 方案第 4 节机制 | 盲修落点 | 决定 | 本地落点/状态 |
|---|---|---|---|---|
| 1 | grounded block/span 输出与 facet 映射 | `services/memory_gateway.py`（本地缺该出口） | 采纳，R3 | 待移植；保留本地 commit/snapshot/cutoff/quote/span 校验 |
| 2 | IMPLEMENTATION semantic judge、request-local route plan、公平路由与 reranker | `services/need_evidence_semantic_judgment.py`、`services/paired_controller.py` | 采纳接线，R3 | 待移植接线；8003 注册已在 R0 完成（`qwen38_27b_nvfp4_8003`） |
| 3 | 分段/分页/工作集与完整聚合 | `services/ordinary_curation.py`（新增） | 采纳并适配，R4 | 待移植；四条限制只作用于单次模型响应 |
| 4 | 剩余列表重新枚举 | `services/writer_change_reconciliation.py` | 采纳小修，R4 | 待移植 + 反序多条 exact match 回归 |
| 5 | frozen request 先恢复、completed raw response 重放 | `adapters/runtime/stage3_writer.py`、`services/writer_context_loop.py` | 采纳核心，R5 | 待移植；不得构造 `future_isolation_attestation.passed=true` |
| 6 | 局修/大改/复审前沿与共享调用预算持久化 | 同上 + `services/model_gateway.py` | 采纳并适配，R5 | 与本地累计预算/yield/错误分类合并 |
| 7 | NarrativeRequirements、阶段语义、Editor 正文证据 | `services/planning_contracts.py`、`domain/editorial.py`、`agents/editor.py` | 适配采纳，R2 | 与本地 beats/state_changes 归一为单一出口 |
| 8 | 来源权限与 Editor supported/avoided | `services/planning_sources.py`、`domain/editorial.py` | 采纳，R2/R3 | 工作计划/指令/候选 vs 真实历史分开 |
| 9 | scope supersession、祖先投影、范围/环/依赖检查 | `services/content_addressing.py`、`domain/planning.py` | 适配采纳，R2 | 合并本地单 wrapper、唯一 goal |
| 10 | PlanRoot 新义务集合、effective_obligations、自动 milestone | `services/planning_contracts.py` | **只采纳身份/观察原则**，R1/R4 | 沿用 `WorldRoot.obligations` 为唯一权威，不引入第二义务集合 |
| 11 | 方法卡、按 mode 核心、Editor lenses、完成证据 | `skills/*.md`（34 个技能文件被改） | 择取，R5 | 保留本地 DRAFT/CONTINUE/MAJOR_REWRITE 与中文门禁，不整批覆盖 |
| 12 | 来源核验、HUMAN_REQUIRED 保级、取消 lease 释放 | 多处 | 对照增量采纳，R1/R5 | 本地已有部分修复，只补缺失边界 |
| 13 | lookahead 根变化失效、有限 commit 缓存 | `services/creative_runtime.py` | 后置验证，R6 | canary 期间关闭 lookahead |
| 14 | DeepSeek harness、批量工作流/文档删除 | `docs/*`（新增 5 个 md，删除 17 个文件） | **本轮不实施** | 不构成修复依赖 |

## 4. 明确不采纳的部分及原因

| 盲修做法 | 不采纳原因 |
|---|---|
| `declared_obligations` 仍不读取 v6 `obligation_plan` | 未补上当前字段错位；采纳会保留 v6 阻塞根因 |
| 章节 prompt 要求声明 `payload.obligations`、无本地 `history_retrieval` 契约 | 与本地下层只引用正式义务 ID 的规则冲突 |
| 旧 checkpoint 兼容路径构造 `future_isolation_attestation.passed=true` | 违反“不得补造 passed 标志”的证据规则 |
| 旧测试允许同章多个 goal | 以本地唯一性要求为准 |
| 新增 PlanRoot 意图集合作为第二义务权威 | 会造成两个义务真值源；本轮沿用 WorldRoot |
| 整批覆盖 34 个 `skills/*.md` | 会丢失本地中文门禁与 mode 约束；按功能择取 |
| 删除 17 个文件（含仓库文档） | 与本轮修复无关，且会破坏历史可见性 |

## 5. 已验证的盲修基线

`tests/unit/test_production_content_repairs.py`、`tests/unit/test_production_memory_closure.py`、
`tests/unit/test_stage5_production_factories.py` 在盲修自身源码树下实测
**24 passed in 2.61s**（`PYTHONPATH=<blindfix>/src`，禁用模型调用，`--no-cov`），与方案记录一致。
注意 `tests/unit/test_stage5_production_factories.py` 在两棵树中都存在且内容不同，移植时以本地版本为基线。
