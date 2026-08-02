# Stage 2M Phase 4：C60/C95 逐 Gold 追踪（2026-08-02）

> 生命周期：`DIAGNOSTIC / HOLD`
>
> 追踪基线：`session/ns-4@abf0731`

## 1. 产物结论

基于 canary39/canary40 冻结 case、diagnostic report 和 `support_progress.json`：

- C60：8/9 Gold 在旧产物中 candidate/context/ledger 均未命中，P003-G09 为 `PARTIAL`；
  weighted `0.0192`、mandatory `0`，主失败层为 `F-NEED_ROUTE_RETRIEVE`；
- C95：7/11 Gold 零命中；G02/G03/G05 已有证据进入 ledger 但 claim 未表达目标结论；
  P005-G09 为 `UNTRACEABLE`；weighted `0`；
- proposal 阶段 C60 有 5/27、C95 有 2/27 批次发生 `OpenAIChatEndpointError` 后 fail-closed
  跳过。该现象不足以解释全部零覆盖，但必须作为 runtime/observability 债务保留。

## 2. 与当前代码的边界

AO 追踪正确证明 `session/ns-4` 基线仍未接近 Gate，但其“未发现 Need/query/fallback 缺陷”只对
当时版本成立。当前复核随后发现：

1. v19 Knowledge Boundary query 没有把公开 open obligation 和高信号关系状态放入历史查询
   前缀；当前 `task_plan_conditioned_need.v20` 已修复；
2. 旧 ContextCompiler 把 mandatory Need 的 top-20 全部强制展开，冻结 C60 trace 显示
   `mandatory_tokens=24523 > 4000`；当前已改为 evidence-group fair packing，离线复放使
   P003-G09 Stage1 覆盖 `0/3→1/3`。

因此 AO 产物不能作为这两项后续修复“无效”的证据，也不应触发重复旧 canary；当前状态仍是
需要在后续受影响 C60 canary 中验证，而不是启动 P3。

## 3. 决策

Gate M4 `HOLD`、P3 `NOT_STARTED`、WP8 冻结。C95 的 ledger→claim 损失继续归入 semantic
claim 责任层；proposal endpoint error 作为 bounded retry/typed failure observability 后续项，
不得静默转成成功。

