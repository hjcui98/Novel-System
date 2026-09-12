# 《余烬九序》统一修复 R0 执行报告

日期：2026-09-12（+08:00）
方案：`docs/yujin_jiuxu_unified_remediation_execution_plan_20260912.md` 第 6 节 R0
状态：R0 完成；R1 起为后续实施任务。

## 1. 生产基线与冻结对象

| 对象 | 值 |
|---|---|
| 生产工作树 | `.worktrees/main-production`（分支 `feat/hierarchy-progressive-skill-patch`） |
| 生产 HEAD | `934b3fb7292ab81322d0a2cfe66029e8c7fe64b5` |
| tracked 修改 | 38 个文件，`2226 +/184 -` |
| tracked patch SHA-256 | `fc46ebb18175d81f4d0d3f60d7082427938ce1fce057b70a016722c9bc07fef8` |
| untracked 文件 | 7 个，逐个 sha256 记录并逐一校验 `OK` |
| 整合分支 | `codex/yujin-unified-remediation` |
| 整合基线提交 | `46dcf05` `chore(r0): freeze pre-remediation baseline from production worktree` |
| R0 端点/预检提交 | `25450b5` `feat(r0): register the 8003 Qwen 3.8 profile and add fail-closed endpoint preflight` |
| 工作区证据目录 | `tmp/yujin-remediation-20260912/`（patch、untracked 副本与 sha256、v6 状态副本、失败基线日志） |

生产工作树及其 dirty 状态保持原样，未 reset、未丢弃；仓库根工作树的无关改动未触碰。
整合工作树在两次提交后 `git status --short` 为 0。

## 2. 残留循环处置（R0 第 3 步）

### 2.1 v6（本项目）

| 对象 | 处置前 | 处置结果 |
|---|---|---|
| auto-accept 循环 | PID `3159738`，日志持续增长（18:31 仍在写） | `SIGTERM` 后进程消失；`auto-accept.log` 20 秒内 395430 → 395430 字节（停止增长） |
| dispatch watch | PID `2313397`，日志停在 16:17 | 处置时已不存在 |
| retry-pump | PID `2313421`，日志停在 16:15；曾把 `plan.arc-volume.g0` 自动重试至 `task_revision=2683` | 处置时已不存在 |

模型服务、PostgreSQL、OpenSearch、MinIO、embedding/reranker 一律未停止。

### 2.2 v4（用户 2026-09-12 授权的额外止血）

v4 `continuation4` supervisor 自 2026-09-09 起崩溃-重启循环，`dispatch-continuation4.log`
达 556,515,417 字节仍在追加。按用户指示：

- 停止：tmux `1643598` + supervisor `1643599`（日志以 `Terminated` 结束）、retry pump `1665285`；
- 保留：`author_accept_continuation4_20260909.py`（PID `2956392`，
  `NOVEL_RUN_ID=run.yujin-jiuxu.v4.continue4`，`NOVEL_ACCEPTANCE_POLL_SECONDS=5`），
  已 orphan 到 PID 1，人工接受通道仍在；
- 未触碰 v4 其它历史进程与产物。

## 3. 8003 端点核实与注册（R0 第 4 步）

| 事实 | 证据 |
|---|---|
| 8003 实际服务 | `vllm serve /data1/users/cuihengjia/qwen3.8-modelopt --served-model-name qwen38-27b-nvfp4 --port 8003 --max-model-len 131072`，PID `2946550`，启动于 2026-09-08 15:56 |
| `/v1/models` | `qwen38-27b-nvfp4` |
| 8006 | 同一 checkpoint 的独立实例（PID `2946551`），保留为后续比较资源，不自动回退 |
| 8081 / 8082 / 5432 / 9200 | 均在监听 |

新增 profile `qwen38_27b_nvfp4_8003`（`qwen38-27b-nvfp4@8003`，
`http://127.0.0.1:8003/v1`，`sequence_limit=131072`、`output_limit=12000`、
`safety_allowance_tokens=1000`、`estimated_reasoning_reserve=2048`、`default_thinking=false`）。
旧 `qwen36_27b_nvfp4_8003` 保留为独立身份，不被静默重定向到实际服务的 checkpoint。

实时预检（`novel-agent preflight-endpoint --endpoint-profile …`）：

```text
qwen38_27b_nvfp4_8003 -> identity_matches=true  issues=[]  exit=0
qwen36_27b_nvfp4_8003 -> identity_matches=false exit=2
  issues=["declared model 'qwen36-27b-nvfp4' is not served by http://127.0.0.1:8003/v1;
           live models: ['qwen38-27b-nvfp4']"]
```

这同时构成验收矩阵 A17（正确/错误模型漂移被拒绝而非热改绕过）的首个真实证据。

### 3.1 timeout 双来源消除

`environment.sh` 声明 `NOVEL_SCHEDULING_TIMEOUT_SECONDS=300`，而
`commands/12_commit_genesis.sh` 与 `commands/30_watch_800_chapters.sh` 硬编码 `120`。
两者已改为读取 `"${NOVEL_SCHEDULING_TIMEOUT_SECONDS}"`；端点与 timeout 现在只有一个配置来源。
脚本不在版本库中（工作区运行配置），本报告记录该修改及其差异。

## 4. 整合前失败身份基线

```text
PYTHONPATH=<worktree>/src NOVEL_AGENT_FORBID_MODEL_CALLS=true \
  .conda-env/bin/pytest -q -p no:cacheprovider --no-cov tests/unit tests/contract
```

结果：**76 failed, 2876 passed, 1 skipped in 77.32s**。

失败集中区域：`test_memory_benchmark_gate_formula` 8、`test_stage1_importer_negative_paths` 7、
`test_human_benchmark_compiler` 7、`contract/test_stage2_teacher_forced_e2e` 7、
`test_stage2_experiment_manifest` 6、`test_stage5_cli` 5，其余 1–4 项。
完整日志与失败身份清单见工作区 `tmp/yujin-remediation-20260912/R0/`。
R6 只比较具体失败身份，不用数量变化代替验收。

### 4.1 环境陷阱（R6 前需处理）

`.conda-env` 的 editable 安装 `_editable_impl_novel_agent.pth` 指向
`.worktrees/unified-agent-runtime-integration/src`。不显式设置 `PYTHONPATH` 时，
pytest/mypy 会静默检查**另一个工作树**的源码。本方案所有 Python 命令因此显式使用
`PYTHONPATH=<目标树>/src`（与 `yujin-jiuxu-v6/environment.sh` 既有做法一致）。

## 5. 冻结的 v6 blocked 事实（R1 输入）

`run.yujin-jiuxu.v6`：`status=blocked`、`current_chapter=0`、`outputs_frozen=false`、
`final_commit=sha256:72be31bb…`。STORY g1 与 ARC_VOLUME g0 的
candidate/accept/commit/projection 全部 succeeded；`plan.chapter-set.1-5.g0.accept.commit`
为 `blocked: validation_rejected`（failure_budget 3→2，无 terminal artifact）。
`yujin-jiuxu-v6/output/` 为空，v4 仅存旧机制 1—56 章正文。

## 6. 盲修来源核实（R0 交付项）

git 传输不可用（`github.com:443` 连接超时），改用 codeload 精确 commit 压缩包：

| 对象 | 值 |
|---|---|
| 压缩包 | SHA-256 `1a0df4e8a95f6d51ecb141fed02241bc6699fc1c3d41c508fc133c7a43f166f0` |
| 与共同祖先 `1856153` 比对 | modified 169、added 18、removed 17 |
| 方案引用的三个测试文件 | 均存在，实测 **24 passed in 2.61s**，与方案记录一致 |
| 与本地 dirty 成果重叠 | 45 个变更文件中 26 个（源码 21 个）落在盲修变更集内 |

规模事实（详见 `docs/remediation/yujin_blindfix_porting_trace_20260912.md`）：本地 47,725 行 vs
盲修 46,110 行，`materializers.py`、`task_conditioned_need_generation.py`、
`production_bootstrap.py`、`plan_reviewer.py`、`writer_cognition.py` 均是盲修更小。
**盲修是并行分支而非本地超集，必须按功能逐项移植。**

本地缺失、需适配移植的模块：`services/ordinary_curation.py`(417)、
`services/planning_contracts.py`(167)、`services/planning_sources.py`(94)、
`schemas/stage3/MemoryGapAssessment.schema.json`、`schemas/stage3/PlanRequirementAssessment.schema.json`。

## 7. R0 退出条件核对

| 退出条件 | 状态 |
|---|---|
| 整合树包含全部本地已执行成果 | 满足：38 tracked + 7 untracked 全部进入 `46dcf05` |
| 生产任务不会自动加载未验收中间源码 | 满足：v6 循环已停；`environment.sh` 仍指向冻结的生产工作树 |
| 可恢复源码基线 | 满足：patch + untracked 副本 + sha256 + 两个整合提交 |
| 端点配置差异登记 | 满足：新 8003 profile + 实时预检证据 + profile 回归测试 |
| 失败身份清单 | 满足：76 项身份清单 + 完整日志 |
| 残留循环处置 | 满足（v6 停机；v4 按用户指示止血并保留接受通道） |

## 8. 已知未完成项（转入后续任务包）

1. 预检的“结构化输出 / 中文长输出 / usage / 取消 / embedding+reranker 真实索引”全契约项
   （方案第 7.1 节）目前只覆盖模型身份与可选 bounded generation，应在 R6 正式预检时补齐并记录。
2. 盲修改动的 34 个 `skills/*.md` 与 Editor/prompt 语义（R2/R5 择取）尚未开始。
3. 本地缺失的 5 个盲修模块尚未移植（R3/R4 责任）。
4. 严格 MyPy 在当前 frozen 树上已有 26 项历史错误（`teacher_forced.py`、`curator_repair.py` 等），
   R0 未修复它们；R6 前需要区分“历史错误”与“本次新增错误”。
