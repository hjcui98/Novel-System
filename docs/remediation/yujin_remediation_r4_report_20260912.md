# R4 执行证据（完整记忆写回与义务正文观察）

日期：2026-09-12（+08:00）
范围：方案第 6 节 R4、第 8 节 A13/A14
状态：进行中。R4.1（有界整章抽取）已移植并测试；生产接线与义务观察结算待续。

## 1. 已完成：有界整章抽取（R4.1）

### 1.1 缺陷

模型单次响应最多四条操作，这是**响应上限**。v6 结算路径把它当成**整章容量**，因此一章
若有四条以上 durable 变化，多出的部分永远进不了 Canon——方案记为
“普通 Curator 整章四条上限”。

### 1.2 移植内容（来源 b79a751）

**域模型**（`domain/changes.py`）：

| 模型 | 作用 |
|---|---|
| `PlannedObligationObservation` | 同一 accepted obligation 的正文观察：`obligation_id` + 状态（not_observed/progressed/resolved/abandoned）+ rationale + 对应原文引文；非 `not_observed` 必须有引文 |
| `OrdinaryCurationPageReceipt` | 单页回执：来源单元、来源 hash、模型请求 id、操作数、`has_more`、`covered`、lookup 词 |
| `CuratorV2EvidenceDraft` 扩展 | `has_more`、`world_lookup_terms`、`plan_observations` |

**服务**（`services/ordinary_curation.py`，425 行）：
- `source_batches`：按 2400/5000 字符切片，跨切片保留 256 字符上下文；
- `world_working_view`：按来源文本与 lookup 词筛出有界工作集，
  精确 lookup 命中永不丢弃（上限 128），并报告 `omitted_record_counts`；
  明确声明“这是工作集不是完整 World，最终校验仍用完整 World”；
- `extract_source_batches`：分批驱动续页，直到每个来源切片耗尽；
  逐页产出回执；整章聚合后只给一个 coverage 结论。

**fail-closed 语义**（逐条测试）：

| 情形 | 结果 |
|---|---|
| 续页无新操作、无 lookup、无观察 | `no progress` 拒绝 |
| `has_more=false` 但 `coverage != 1` | `incomplete coverage` 拒绝 |
| 缺少 `<CURATOR_INPUT>` 包封 | `source envelope` 拒绝 |
| 输出长度截断 | 一次压缩重试（`json_object_framing`，最多两条操作），再失败则拒绝 |
| 单批超过 16 页 | 拒绝 |
| 提前兑现时间锁定义务 | 拒绝 |
| 到期 milestone 缺完成证据 | 拒绝 |

### 1.3 证据

`tests/unit/test_ordinary_curation.py`（10 项）：
切片预算、空章节、有界工作集与精确 lookup 保留、跨页聚合出 7 条操作（>4）、
停滞续页、覆盖不足、缺包封、页面回执含来源单元与请求 id、页请求载荷含
`EVIDENCE_CANDIDATES`/`PLANNED_OBLIGATIONS`/续页契约。

## 2. 未完成：生产接线（下一轮整体实施）

不能只做半接线，原因已核实：

1. `extract_source_batches` **自行构建** `WORLD`/`CHAPTER`/`EVIDENCE_CANDIDATES`/`PLANNED_OBLIGATIONS`
   并整段替换 `<CURATOR_INPUT trusted="false">…</CURATOR_INPUT>` 之间内容；
   而本地 `model_curation.extract_reported_v2` 当前把 WORLD 与候选
   **内联**在同一段 prompt 文本里，需要先把 prompt 重构为“前后缀 + 包封”结构，
   否则会被整段替换掉。
2. 本地 `_bind_cumulative_budget`（含 elastic tier 与 `budget_source` 记账）是本地独有改进，
   盲修把它删掉了；接线必须保留该路径，逐页传递
   `cumulative_token_budget` / `cumulative_token_budgets` / `cumulative_tokens_used`。
3. 返回值从“单 call”变为“多 call + 多页”，`ModelCurator` 的
   `last_*` 状态、调用账本与下游统计需要同步；
4. 本地输出长度重试块与 `extract_source_batches` 内部重试重复，接线时应删除其中之一
   （保留本地 `_bind_cumulative_budget` 记账、删除重复块）。
5. 需同步更新断言 prompt 文本的既有测试（`test_curator_evidence_contract_v2.py` 等）。

## 2.5 已完成：剩余列表索引错配（R4.6，A19）

`WriterChangeReconciliationService.reconcile` 先把观察列表 `enumerate` 成
`(原索引, 元素)` 再按**位置**`pop`：第一次删除后，剩余元素保存的原索引全部失效，
多条声明时会消费错误的观察，甚至直接崩溃。

**实测确认**（临时还原旧代码）：`test_partial_match_after_an_exact_pop_keeps_a_distinct_observation`
报 `IndexError: pop index out of range`——不是理论风险。

修复即盲修 b79a751 的做法：只保存元素，匹配时现算索引，`pop` 命中元素。
证据：`tests/unit/test_writer_change_reconciliation_indices.py`（3 项）：
反序 exact match 各自绑定、partial mismatch 消费自己的 subject、pop 后未匹配观察仍如实上报。

## 3. R4 其余未完成项

| 项 | 内容 |
|---|---|
| R4.2 自报覆盖核验 | 模型 `coverage` 只能作为待核验结果，结合源段覆盖、未解决变化、无进展检测与真实样例审计 |
| R4.3 完整 World 校验 | 工作集不得替代最终完整校验；跨段状态、先实体后关系、同章新实体依赖 |
| R4.4 同章原子提交 | 全章操作与同 ID obligation observations 汇总后统一验证、原子提交；分页失败/预算耗尽不得部分写入 |
| R4.5 到期责任 | 到期强制责任缺正文完成证据、提前兑现、身份错配阻止提交（`extract_source_batches` 已实现里程碑部分，需接到结算路径） |
| R4.6 剩余列表索引 | `writer_change_reconciliation` 反序多条 exact match 回归 |

## 4. 回归与失败身份

`tests/unit tests/contract`：**76 failed / 2978 passed / 1 skipped**；
失败身份集合与整合前基线**逐项完全相同**（0 新增、0 修复）。本轮新增 15 项通过测试。
