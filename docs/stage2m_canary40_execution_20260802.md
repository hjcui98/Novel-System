# Stage 2M canary40 执行记录（2026-08-02）

> 生命周期：`CURRENT_RESULT / DIAGNOSTIC`
>
> 状态：Gate M4 `HOLD`；正式 P3 `NOT_STARTED`
>
> 执行基线：`session/ns-3@e4fb89a`；真实模型 `qwen36-27b-nvfp4`

## 1. 执行边界

本轮只运行 VAC C40/C60/C95、Arm A、`real_hybrid + local_openai`；未运行 C80、
C90–C95 章节、五点矩阵或 A/B/C，也未聚合不完整矩阵。执行时文档端点 8002 不可达，
AO worktree 发现同模型监听于 `http://127.0.0.1:8003/v1`，因此使用 8003；偏差已原生写入
各 `experiment_manifest.json`。当前主工作区的 8002 后续已重新验证健康，不能反向改写本次
历史 manifest。

## 2. 结果

| Canary | weighted 前→后 | mandatory 前→后 | contradiction | untraceable |
|---|---:|---:|---:|---:|
| VAC C40 | `0.2273→0.4545` | `0.3333→0.6667` | `0.1111→0` | `0` |
| VAC C60 | `0.0192→0.0192` | `0→0` | `0` | `0→0.2222` |
| VAC C95 | `0→0` | `0→0` | `0` | `0.0909` |

- C40：P002-G01 `CONTRADICTS→HIT`，P002-G03 `MISS→HIT`；目标层有实质改善；
- C60：G06/G08 的变化是 provenance 不完整导致的 `UNTRACEABLE` 重分类，不是候选召回改善；
- C95：逐 Gold verdict 与 loss 类别不变；
- 三点均满足 future leakage=0、future-isolation failure=0、预算未超限，
  `scenario_run.completed=true`、Assembler READY；作为诊断产物，
  `formal_contract_validated=false`、`gate_passed=false`。

## 3. 版本解释与决策

本次 canary 使用 `task_plan_conditioned_need.v19` 和修复前的 ContextCompiler，早于当前工作区的
v20 Knowledge query enrichment 与 evidence-group fair packing。因此它是旧路径失败和 C40
改善的有效真实证据，但不是当前 C60 两项修复的运行验证。

三点远低于 Gate M4 阈值，故 Gate M4 保持 `HOLD`，正式 P3 不启动，WP8 继续冻结。

