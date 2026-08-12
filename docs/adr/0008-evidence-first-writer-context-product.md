# ADR-0008: Stage 2M 使用 evidence-first Writer Context 产品

- Status: Accepted
- Date: 2026-08-11
- Supersedes: ADR-0004 对 Stage 2M read-side payload 的 claim-first 要求
- Preserves: ADR-0003 的确定性 Gateway、安全边界和受控可见性

## Context

ADR-0004 将 `WriterContextPackage` 定义为 Writer-facing claim 集，并把原始材料放在独立
`EvidenceLedger`。后续 Stage 2M 因而增加了 Claim proposal、multi-slice synthesis、whole-claim
verifier、semantic receipt 和逐 Gold semantic evaluator。

冻结五检查点产物证明，这组机制已经偏离当前产品需要：45 条 observed/operational Gold 中 33 条
已有 accepted raw evidence 在 Ledger，但最终仍因 claim 未生成、表达不同或引用未对齐而得到
`MISS/UNTRACEABLE`。例如 P004 的模型语义判断已经认为 G02/G03 的 Context claim 支持 Gold，最终
仍因 claim-to-ledger identity 不一致而失败。这个失败不能说明 Memory 没有找回可供 Writer 使用的
材料。

当前产品调用者是 Writer。Writer 需要的是按写作任务组织的、cutoff-safe、可直接阅读并能回指原文
的记忆材料，不需要 Memory 先替 Writer 生成唯一标准结论。Gold 解封后的语义评分属于外部 benchmark
或人工评审，不是 Memory Agent 的生产职责。

## Decision

Stage 2M 默认 read-side 产品改为 evidence-first `WriterContextPackage` 与其绑定的
`EvidenceLedger`：

1. `WriterContextPackage` 按 public Need/facet/scope 组织材料，记录 `need_id`、写作用途、缺口、
   排序后的 evidence ids 和必要的有界原文 preview；“写作用途”解释为什么检索该材料，不断言
   evaluator-only Gold 结论。
2. `EvidenceLedger` 保存被 package 暴露的精确 L0 paragraph/contiguous-sentence slices，包括 source、
   chapter/scene、span、object/root identity、basis commit、cutoff、profile 和 taint attestation。
3. 每个 package evidence id 必须在同一冻结 Ledger 中可解引用；每个 exposed Ledger entry 必须绑定
   至少一个 public Need。相关性选择只能使用 public task/Plan/Need，不能读取 Gold 或未来正文。
4. 默认生产路径在 selection/packing 后结束：

   ```text
   public Task/Plan -> Need -> Retrieval/Rank/Exact L0 Expansion
   -> Evidence Selection/Packing -> WriterContextPackage + EvidenceLedger
   ```

5. Claim proposal、multi-slice synthesis、whole-claim verifier、semantic receipt 和逐 Gold semantic
   evaluator 不再是默认 Agent 路径、package READY 条件或 Stage 2M 产品 Gate。现有实现可暂时保留为
   legacy/diagnostic compatibility，但不得由默认 runner 调用，也不得阻塞 evidence-first package。
6. benchmark 可在 package 冻结后，由人工或独立强模型读取 Gold、package 和 Ledger 做语义评分；该
   评分过程与被测 Agent 隔离，不能反向修改 Need、检索、排序、package 或同一实验身份。
7. Agent 自身只对可机械验证的不变量负责：权限/cutoff/leakage、精确 provenance、引用可解引用、
   evidence/token 预算、typed gap、可复现 manifest 和无 source mutation。它不内置“标准答案裁判”。

## Consequences

- Stage 2M 不再为了生成中间 claim 承担额外模型延迟、截断、重复输出和 verifier 失败域。
- Writer 可以直接基于原始、可追溯材料形成符合当前写作意图的理解；不同但合理的归纳不会在 Memory
  层被误判为失败。
- 下一轮质量修复聚焦 Need 语义覆盖、检索、选择和 evidence packing。v3 的 Claim
  `MISS/UNTRACEABLE` 不再是产品修复依据。
- 历史 claim-first 报告保持不可变，仅作为诊断 provenance。新的 evidence-first 产物必须使用新
  contract/version/manifest identity，不能把历史分数重标为新 Gate 结果。
- Stage 3 Writer 的消费合同需要绑定新 package version；在该绑定完成前不得假定旧
  `WriterContextItem.claim` 是当前生产真相源。
