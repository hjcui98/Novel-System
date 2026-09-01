# ADR-0009: Stage 2M Need–证据语义闭合

- Status: Accepted
- Date: 2026-08-18
- Amended: 2026-08-18 — Plan v17 capacity-driven batching and paging
- Refines: ADR-0008 的默认 evidence-first read-side 路径
- Preserves: ADR-0003 的确定性安全边界与 ADR-0008 的无 Claim、无 Gold 产品边界

## Context

真实模型驱动的 P001-P005 v5 运行证明，Stage 2M 已能从冻结 WorldRoot/TextRoot 检索并装配
可解引用的精确 L0 原文，Writer 可以把这些材料作为候选上下文使用。人工阅读同时暴露两个
read-side 质量缺口：

1. 结构化 `FacetSupportEvaluator` 只允许 predicate-bound State/Event/Relation/Obligation anchor
   闭合语义 facet。原文已经直接回答问题但没有对应结构化 anchor 时，会产生假阴性的
   `no_selected_evidence`；相反，某个结构化 anchor 命中时，所装配的原文仍可能没有直接回答
   Need。
2. `assembly_status=READY` 只证明包、Ledger、预算和安全边界机械成立，但人类可读包没有在顶部
   同等醒目地表达语义完整性和未闭合 Need/facet。

Planner 已有目标章节覆盖、规范实体 grounding 和目标实体覆盖，但没有对章节目标所依赖的关键
人物、关键道具、当前伤势/状态及知识边界做一次整体遗漏审计。Gold 是离线评测参考，不能成为
生产 Planner 的输入或标准答案。

## Decision

1. ADR-0008 的 evidence-first 产品不变：默认输出仍是按 public Need 组织的原文材料及其精确
   `EvidenceLedger`。不得恢复 Claim proposal、multi-slice synthesis、whole-claim verifier 或
   逐 Gold evaluator。
2. 在精确 L0 slice 选择完成、package assembly 开始之前，增加一个窄的
   **Need–证据语义裁决**。它只判断给定原文是否直接回答 public Need 的 mandatory facet，
   不生成事实 Claim，不读取 Gold，不改写原文，也不写 Canon。
3. 裁决必须覆盖每个 Need 已选择的全部去重精确 L0 slices，不设 slice 数量或物理调用次数的产品
   上限。按模型输入 token 容量把有限输入确定性装入多个 batch；一个 Need/facet 尽量保持在同一
   batch，超出单请求容量时才按 slice 边界分块。模型返回 `SUPPORTED`、`PARTIAL` 或
   `UNSUPPORTED`；任何未覆盖、请求、传输、预算或结构化结果失败记录为 `UNRESOLVED`。
4. 每个裁决必须绑定 `need_id`、`need_facet_id` 和输入集合内的 `slice_id`。`SUPPORTED` 至少绑定
   一条 slice；任何未知、重复、越界或遗漏的绑定均失败关闭。被引用 slice 继续由现有 resolver、
   cutoff、scope、taint 和 dereference 不变量保护。
5. 结构化 facet receipt 保留为独立诊断真相，不被覆盖。语义 receipt 可以把“结构化未闭合但原文
   直接回答”的 facet 升为有效支持，也可以把“结构化命中但原文未回答”的 facet 降为部分或不
   支持。Writer-visible gap 和 `semantic_status` 读取语义 receipt；结构化状态继续写入 case record
   和 manifest。
6. 人类可读包顶部必须分别显示 `assembly_status`、`semantic_status`、`usable_with_gaps`，并列出
   所有未闭合 mandatory Need/facet 及其裁决原因。`READY` 永远只表示机械装配成功。
7. Planner generation 和遗漏审计各是一项逻辑阶段，不是固定一次物理调用。目标章节 goals 按
   序列化 token 容量确定性分页；每页使用同一 public task、该页 goals、截止点 World 摘要和已
   接受 drafts。审计类别固定为关键人物、关键道具、当前状态/伤势和知识边界；不得读取 Gold 或
   未来正文。
8. 遗漏审计只产生 typed finding。若某页存在 finding，与该页现有缺章、实体覆盖和标签合同
   finding 合并，复用一次完整替代 drafts 的 repair；repair 后只做 host-side closure 校验，每页
   不形成循环。不得用固定 24 drafts、32 Needs 或 case 级 48 tool calls 作为产品语义上限；物理
   调用数由有限输入和 token 容量推导并完整留痕。
9. v5 产物和 `53f7f5c` 保持只读基线。任何上述代码、prompt、schema 或调用策略变化都使用新提交、
   新 experiment id 和新输出根；冻结 C20/C40/C60/C80/C95 输入可继续只读复用，无需重建 Canon。

## Consequences

- Stage 2M 可以诚实地区分“包可用”“语义完整”和“带缺口仍可供 Writer 使用”，不再用机械
  `READY` 暗示语义完成。
- 原文相关性由窄 receipt 解释，不恢复 Memory 先替 Writer 生成标准答案的 claim-first 失败域。
- 真实运行允许存在 `PARTIAL/UNSUPPORTED/UNRESOLVED`；修复验收要求状态完整、引用可信和表达
  诚实，不要求所有 GAP 清零，也不要求与 Gold 完全一致。
- 新裁决的物理调用数随实际证据增长；通过单请求 token 容量、有限输入、显式 wall-clock/token
  预算、零隐藏重试和完整 batch receipt 保持资源边界，不用固定条数换取表面成功。
