# R6 确定性验收矩阵与失败身份基线

日期：2026-09-12（+08:00）
范围：方案第 8 节 A01–A22、第 6 节 R6 第 1–2 项
状态：确定性矩阵已建立；真实 v7 运行（第 9 节 G0–G3）待执行。

## 1. 基线命令与结果（2026-09-12 复核）

| 命令 | 结果 |
|---|---|
| `pytest -q --no-cov tests/unit tests/contract` | **76 failed / 3008 passed / 1 skipped** |
| `pytest -q --no-cov tests/integration` | **19 failed / 123 passed** |
| `pytest -q --no-cov tests/integration --ignore=test_real_infrastructure.py` | **19 failed / 119 passed** |

integration 与 unit/contract 的失败身份集合在**两次独立运行之间逐项相同**，
且在“有真实基础设施”与“无基础设施”两种配置下**同为 19 项、集合完全一致**——
即真实服务只影响 `test_real_infrastructure.py` 的 4 项结果（现已全部通过），
不改变其余判定。

`test_real_infrastructure.py` 的 4 项（PostgreSQL 迁移与恢复、MinIO 往返、OpenSearch 索引与检索、
跨 MinIO/PostgreSQL/OpenSearch 的完整 outbox 投影）**全部 PASS**，
使用项目 Conda PostgreSQL + 本地缓存的 MinIO/OpenSearch/OTel 发行包，
在 `volumes/native/integration/...` 独立实例中运行。

### 1.1 R6 前必须修复的环境缺口（本轮已解决）

| 缺口 | 原因 | 处置 |
|---|---|---|
| `NativeInfraError: project Conda PostgreSQL binary is missing: initdb` | `native_infra.py` 以 `REPOSITORY_ROOT/.conda-env/bin` 解析二进制，而 `.conda-env` 是 gitignore 的、不随 worktree 创建 | 在整合工作树建立 `.conda-env` 符号链接（不进入版本库，`git status` 仍为 0 项） |
| `URLError: Temporary failure in name resolution` | `native_infra` 需联网下载 MinIO/OpenSearch/OTel 发行包，沙箱无 DNS | 从仓库既有 `tmp/native/dist` 与 `tmp/native/downloads` 缓存播种（二进制与归档，均不在版本库内） |

两者都是**环境**缺口而非代码缺陷；修复方式不改变被测代码。

## 2. 失败身份清单（整合前基线，全部为既有失败）

### 2.1 unit + contract（76 项）

完整清单：`tmp/yujin-remediation-20260912/R0/baseline-failures.txt`。
集中区域：`test_memory_benchmark_gate_formula` 8、`test_stage1_importer_negative_paths` 7、
`test_human_benchmark_compiler` 7、`contract/test_stage2_teacher_forced_e2e` 7、
`test_stage2_experiment_manifest` 6、`test_stage5_cli` 5。

### 2.2 integration（19 项）

| 文件 | 数量 | 主题 |
|---|---|---|
| `test_writer_context_loop.py` | 11 | MAJOR_REWRITE 路由、surface guard、recent-prose 重试、compaction 保留 |
| `test_production_planning_cadence.py` | 5 | 长度策略越界不得改 TextRoot、被阻塞计划替换 |
| `test_stage5_real_writer_e2e.py` | 1 | 真实 Writer adapter 组合 |
| `test_u6c_fault_matrix.py` | 1 | U6-C 故障矩阵 |
| `test_u6_continuous_replay.py` | 1 | U6-A 单次 basis 冻结 |

清单：`tmp/yujin-remediation-20260912/R6/integration-final-failures.txt`。

**结论**：本次 R0–R5 的全部改动**未新增任何失败身份**——unit/contract 与 integration
两侧的失败集合与整合前逐项一致（0 新增、0 修复）。

## 3. 确定性验收矩阵证据映射（A01–A22）

| 编号 | 场景 | 证据位置 | 状态 |
|---|---|---|---|
| A01 | v6 原六条字符串 action → host review REVISE | `test_obligation_contract.py`、`test_plan_review_obligation_contract.py` | ✅ |
| A02 | 八卷 16 条旧责任表逐条可追溯 | `scripts/audit_v6_responsibility_binding.py`（真实产物 16→16）+ `test_obligation_declaration_binding.py` | ✅ |
| A03 | 合法链路落地、偷建/未知 ID/非法动作被拒 | 同上 + `test_volume_stage_grid.py` | ✅ |
| A04 | 伪 waiver 拒绝、合法首章豁免通过 | `test_history_waiver_authenticity.py`、`test_writer_history_retrieval_gate.py` | ✅ |
| A05 | 未来章规划时无正文、执行时按当前历史检索 | 部分（`test_plan_unresolved_scope.py` 覆盖规划侧） | ⚠️ 待真实运行 |
| A06 | future-locked PROGRESS 与 101/201/350/401 边界 | `test_writer_history_retrieval_gate.py::test_future_locked_obligation_stays_in_scope_before_its_boundary` | ✅ |
| A07 | grounded block/span 与错 quote/snapshot/cutoff | `test_writer_history_retrieval_gate.py::test_grounded_canonical_prose_is_not_skipped_by_live_l0` | ✅ |
| A08 | REQUIRED 零 Need/零命中、缺 facet/ref/审批/exact | `test_writer_history_retrieval_gate.py`（A1–A4）、`test_evidence_first_writer_context.py` | ✅ |
| A09 | 重复 goal、图/顶层不一致、悬空依赖、环 | `test_plan_graph_scope_guards.py`、`test_writer_history_retrieval_gate.py::test_a9_*` | ✅ |
| A10 | 部分重叠窗口、父级替换、已写前缀与未来尾部 | `test_plan_graph_scope_guards.py` | ✅ |
| A11 | 卷入口/持续/出口与章节目标不同 | `test_volume_stage_grid.py` | ✅ 契约层 |
| A12 | 工作计划冒充历史、Editor avoided 强制缺口 | 宿主机理已核实（Editor 只能引用 Draft 内精确引文），端到端待真实运行 | ⚠️ 部分 |
| A13 | 六条以上变化、满四条末页、跨段依赖、无进展 | `test_ordinary_curation.py`（11 项） | ✅ |
| A14 | 义务正文观察、提前 payoff、到期未完成 | `test_obligation_observation_writeback.py`（5 项） | ✅ |
| A15 | Memory/首稿/局修/大改/复审后重启 | `test_writing_loop_repair_frontier.py`（计数与前沿）+ 恢复路径断言 | ⚠️ 端到端待补 |
| A16 | 错请求 hash、损坏原始响应、uncertain 调用 | `test_model_replay.py`（13 项） | ✅ |
| A17 | 8003 正确/错误模型、timeout/config 漂移 | R0 实时预检证据 + `test_production_bootstrap_endpoints.py`、`test_endpoint_preflight.py` | ✅ 身份层 |
| A18 | 真实旧 v4/v6 roots 与新 schema | `test_obligation_declaration_binding.py` + R0 的 v6 roots 只读加载 | ✅ 部分 |
| A19 | 多条反序匹配、lease 取消、跨 gateway 切片 | `test_writer_change_reconciliation_indices.py`（3 项） | ✅ 反序部分 |
| A20 | canonical 游标与 100→101 卷边界 | 未覆盖 | ❌ 待补 |
| A21 | 单个英文违规词与白名单代号 | 既有中文 token 门禁测试（未在本次改动范围） | ⚠️ 待登记 |
| A22 | 作者约束/证据超预算、四类 coverage | `test_planning_coverage.py`（8 项） | ✅ |

图例：✅ 已有确定性证据；⚠️ 部分或需真实运行补证；❌ 尚未覆盖。

## 4. R6 未完成项

| 项 | 内容 |
|---|---|
| A20 | canonical 游标与 100→101 卷边界（需隔离生产工厂/调度 fixture） |
| A21 | 中文门禁的成稿/修稿/最终物化入口统一性登记 |
| A05/A12/A15 端到端 | 需真实 v7 运行补证 |
| 8003 预检补齐 | 结构化输出、中文长输出、usage/取消、embedding+reranker 真实索引（R0 只做了身份层） |
| 真实 v7 运行 | 第 9 节 G0 配置与规划 → G1 两章 → G2 五章 → G3 20 章 |

## 5. 回归纪律

本轮之后的每个增量都必须保持：

```text
tests/unit tests/contract  失败身份集合 == R0 基线（76 项）
tests/integration          失败身份集合 == 本节 2.2 清单（19 项）
```

比较对象是**具体测试身份**，不是失败数量。
