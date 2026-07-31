# Stage 2 Memory Gate C95 验收

> 文档生命周期：`ACCEPTED`
>
> 日期：2026-07-29 +08:00
>
> 结论：`CONDITIONAL_PASS`
>
> 默认路径：冻结 deterministic real-hybrid Memory Gateway
>
> Agentic Controller：不晋升

## 证据结论

```text
C1–C95 chapter commits = 95
Genesis + chapter commits = 96
final_commit =
  sha256:d4920c29bcdcbc07b64de4b0ffac4772d4aae0fefb900d67d343b01f1ec29ba9
future_leakage = 0
future_isolation_failures = 0
checkpoint_chain_consistent = true
projection_outbox = 97 completed / 0 incomplete
derived_snapshots = 97 exact
deterministic checkpoints = C20/C40/C60/C80/C95 passed
independent evaluation entries = 10
```

独立 Evaluation Ledger 的 10 项由五项既有 paired checkpoint evidence 和五项本次
deterministic physical-index gate evidence 组成。

## 正式 Gate

现有 `Stage2GateEvaluator` 对当前证据给出：

```text
verdict = conditional_pass
controller_promotion = freeze_deterministic_gateway
memory_gateway_frozen = true
blockers = [controller.held_out_gain]
```

其余 21 项检查全部通过。`controller.held_out_gain` 是保留的晋升阻塞，不阻塞 deterministic
Gateway 冻结。

## 验收修复

历史 checkpoint 验收曾使用 OpenSearch alias 获取文档数。Alias 会随同一实验的后续 snapshot
前移，因此可能把 C60/C80 attestation 与更晚的索引计数比较。

`scripts/run_stage2_retrieval_gate.py` 已改为查询 attestation 固定的 `physical_name`，与
`build_real_hybrid_backend` 的运行时行为和 checkpoint retention policy 一致。修复后五个
checkpoint 的 R1、Anchor、Grounded、snapshot 和 source commit 均通过。

## Writer 解锁范围

本 Gate 只解锁：

```text
Writer DRAFT
+ frozen deterministic Memory Gateway
+ writer_safe ContextPackage
+ candidate DraftArtifact
```

继续禁止：

```text
CONTINUE / MAJOR_REWRITE 正式接线
raw retrieval tools
Editor / Curator / MemoryWrite / Commit / Canon 接线
Writer Semantic PASS
Production enablement
```
