# R3 执行证据（真实 Memory 证据出口与完整 readiness）

日期：2026-09-12（+08:00）
范围：方案第 6 节 R3、第 8 节 A07/A08
状态：进行中。已完成 grounded 证据出口；semantic judge 接线、request-local route plan 与 readiness 收口待续。

## 1. 已完成：grounded 正文证据出口

### 1.1 缺陷

`MemoryGateway._selection_for_trace` 在遍历已选候选时显式跳过
`GROUNDED_BLOCK` / `GROUNDED_SPAN`：

```python
if candidate.unit.unit_kind in {GROUNDED_BLOCK, GROUNDED_SPAN}:
    continue
```

后果：真实 hybrid 检索命中 canonical 正文、候选被选中，但 grounded 单元永远不进入
冻结 selections，Writer 实际消费不到正文证据——即方案记录的下游缺口
“本地 Gateway 跳过 grounded 正文单元”。

### 1.2 修复（移植自盲修 b79a751）

- 移除该跳过分支；grounded 单元与其它单元一样经过
  `resolve_live_evidence` 的 commit / snapshot / cutoff / block / span / object-hash /
  quote-hash 全套校验，证据不合法仍然被过滤（不是“信任 grounded”）；
- 切片记录 `supported_facet_ids=FacetSupportEvaluator.supporting_facet_ids(need, unit)`。

保留的语义边界：`FacetSupportEvaluator._facets_for_unit` 对 grounded 单元**故意返回空**
（原始正文没有可识别 facet 语义的谓词，按检索相关度认领语义支持会造成假闭合）。
因此 grounded 证据进入冻结选择与 Writer 包，但 facet 闭合仍由 semantic judge 决定。

### 1.3 证据

`tests/unit/test_writer_history_retrieval_gate.py::test_grounded_canonical_prose_is_not_skipped_by_live_l0`
用真实证据（block/span/quote hash 与 basis 对齐）直接驱动 `_selection_for_trace`：
修复前返回空切片，修复后返回切片与 evidence refs；并断言 grounded 切片
`supported_facet_ids == ()`，防止把检索相关度当成语义支持。

## 2. 已完成：semantic judge 接线

### 2.1 缺陷

生产 `bootstrap` 只在注册了 `ModelRole.BATCH_TEST` 端点时才构造 semantic judge：

```python
semantic_judge = NeedEvidenceSemanticJudge(...) if batch_endpoint is not None else None
```

8003 profile 只注册 `IMPLEMENTATION`，于是 judge 恒为 `None`——语义检查**静默消失**，
且没有任何报错或降级标记。这正是方案第 7.1 节要求的“不能因为没有 BATCH_TEST 端点而无语义检查”。

### 2.2 修复

- `NeedEvidenceSemanticJudge` 显式接收 `model_role` / `purpose`，并拒绝
  “batch/evaluation purpose + 非 batch_test role”的组合（gateway 侧同规则）；
- bootstrap 改为回退到已注册的 `IMPLEMENTATION` 端点，purpose 记为 `DEVELOPMENT`；
  只有**完全没有注册端点**时 judge 才为 `None`；
- judge 的 request id 现在包含 run 与 task，单批调用可归因。

证据：`tests/unit/test_need_evidence_semantic_judgment.py`
（实现端点端到端判定、batch purpose 拒绝、request id 含 run/task）。

## 3. 关于 `PLAN_BLOCKING_UNRESOLVED`

该 reason code 在仓库中**从未被赋值**（死代码）。核查后决定不新增重复机制：
`WritingTaskContract.blocking_gaps` 已在 `writer_draft_integration`（BLOCKING_GAP）与
`writer_generation`（WRITING_TASK_BLOCKING_GAPS）两处阻断，且提交边界由
`PlanCandidateMaterializer._accepted_review` 强制“ACCEPT 且无 blocking issue、
receipt 成功、绑定同一 proposal”。已提交的 PlanRoot 结构上不含 unresolved，因此
阻塞项只可能来自未被接受的计划——那条路径已在更早的边界 fail closed。

## 4. R3 未完成项

| 项 | 内容 |
|---|---|
| request-local route plan | 依据实际 snapshot attestation 生成、公平使用工具预算、传入 reranker、无证明不补写 EXACT |
| readiness 余项 | A5—A8 的端到端断言（合法正文进包；错 quote/snapshot/cutoff/零 Need 在模型调用前阻断）已有部分覆盖（A1—A4/A9），需在真实运行时补齐证据 |
| 死代码清理 | `PLAN_BLOCKING_UNRESOLVED` 是否删除或改为真实信号，留待 R6 前的契约清理 |

## 5. 回归与失败身份

`tests/unit tests/contract`：**76 failed / 2965 passed / 1 skipped**；
失败身份集合与整合前基线**逐项完全相同**（0 新增、0 修复）。
