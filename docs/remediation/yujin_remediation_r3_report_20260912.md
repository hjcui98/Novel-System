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

## 2. R3 未完成项

| 项 | 内容 |
|---|---|
| semantic judge 正式接线 | 8003 IMPLEMENTATION endpoint 注册（R0 已完成 profile），仍需按 purpose 显式配置并让 request id 含 run/task |
| request-local route plan | 依据实际 snapshot attestation 生成、公平使用工具预算、传入 reranker、无证明不补写 EXACT |
| readiness 收口 | 父级范围、blocking issue、真实审批回执、Plan/context/goal 引用与 exact projection 的实测输入 |
| A07/A08 | 合法正文进包、非法证据（错 quote/snapshot/cutoff）阻断 Writer 的端到端断言 |

## 3. 回归与失败身份

`tests/unit tests/contract`：**76 failed / 2963 passed / 1 skipped**；
失败身份集合与整合前基线**逐项完全相同**（0 新增、0 修复）。
