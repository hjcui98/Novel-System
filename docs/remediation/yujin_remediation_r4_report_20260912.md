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

## 2. 已完成：生产接线（R4.1 落地）

`extract_reported_v2` 已改为驱动 `extract_source_batches`，单次响应上限不再等于整章容量。
上一轮列出的五项阻塞全部处理：

1. **prompt 结构**：拆成“静态前缀 + 可替换 `<CURATOR_INPUT>` 包封 + 静态后缀”，
   续页只重写包封，输出契约不会在续页中丢失；
2. **保留本地预算改进**：`_bind_cumulative_budget`（elastic tier + `budget_source` 记账）
   原样保留，并把调用方的 `cumulative_token_budget` 透传给抽取器逐页 preflight；
3. **多调用状态**：新增 `last_ordinary_calls` / `last_ordinary_pages`，逐页记录模型调用与回执；
4. **删除重复重试**：本地输出长度重试块与其专用常量已删除（抽取器内部负责一次压缩重试）；
   provider 在压缩重试后仍截断时，保持 `ModelCurationOutputIncomplete` 这一 typed 失败；
5. **既有测试**：`test_curator_evidence_contract_v2.py` 全绿（含 prompt 文本断言）。

### 2.1 过程中发现并修复的两个真实缺陷

- **空页契约过严**：新增 `has_more`/`world_lookup_terms`/`plan_observations` 后，
  一页可以合法地“无操作但有后续”，但本地 `CuratorV2EvidenceDraft` 仍要求
  “空操作必须有 no-durable-delta 证明”。已按盲修规则放宽：只有**耗尽且无任何待办**的页
  才需要 no-op 证明，并补测试。
- **聚合覆盖了模型自报覆盖度**：`extract_source_batches` 原实现把聚合结果的
  `coverage` 硬写成 `1.0`，等于**掩盖**了模型自认“本章没抽完”的信号——而这正是
  `CURATOR_PROPOSAL_EMPTY_DELTA_UNVERIFIED` 支持门要拒绝的对象。现改为原样保留模型自报值，
  由支持门判定（方案 R4.2 要求的“自报只能作为待核验结果”）。

## 2.5 已完成：剩余列表索引错配（R4.6，A19）

`WriterChangeReconciliationService.reconcile` 先把观察列表 `enumerate` 成
`(原索引, 元素)` 再按**位置**`pop`：第一次删除后，剩余元素保存的原索引全部失效，
多条声明时会消费错误的观察，甚至直接崩溃。

**实测确认**（临时还原旧代码）：`test_partial_match_after_an_exact_pop_keeps_a_distinct_observation`
报 `IndexError: pop index out of range`——不是理论风险。

修复即盲修 b79a751 的做法：只保存元素，匹配时现算索引，`pop` 命中元素。
证据：`tests/unit/test_writer_change_reconciliation_indices.py`（3 项）：
反序 exact match 各自绑定、partial mismatch 消费自己的 subject、pop 后未匹配观察仍如实上报。

## 3. R4 义务正文观察（R4.5 证据）

`tests/unit/test_obligation_observation_writeback.py`（5 项）走真实 `extract_reported_v2` 路径：

| 场景 | 结果 |
|---|---|
| 已接受义务的 progressed 观察 | 生成**同 ID** 的 OBLIGATION 操作，带原文引文证据 |
| WorldRoot 尚无该记录 | 生成 CREATE（已存在则 REPLACE） |
| 观察未在本次请求目录内的义务 | fail closed |
| 对 `not_before=101` 的义务报 resolved | fail closed（断言 typed cause 含 "before its time lock"） |
| 到期 milestone 无完成证据 | fail closed |

## 4. R4 其余未完成项

| 项 | 内容 |
|---|---|
| R4.3 完整 World 校验 | 工作集不得替代最终完整校验；跨段状态、先实体后关系、同章新实体依赖 |
| R4.4 同章原子提交 | 全章操作与同 ID obligation observations 汇总后统一验证、原子提交；分页失败/预算耗尽不得部分写入 |

## 5. 回归与失败身份

`tests/unit tests/contract`：**76 failed / 2984 passed / 1 skipped**；
失败身份集合与整合前基线**逐项完全相同**（0 新增、0 修复）。R4 至今新增 21 项通过测试。
