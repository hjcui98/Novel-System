# Stage 1 验收记录

日期：2026-07-21

结论：**Stage 1 通用工程闭环通过；正式质量门禁未通过，Stage 2 仍保持阻断。**

当前仓库已经完成 Stage 1A/1B 的通用协议、运行器和合成闭环。合成 fixture 不承担文学质量、
真实检索效果或真实 Curator 精度证明。正式退出既需要完成剩余生产适配，也需要用户提供有
授权的真实 20→3 Bundle 和至少 50 章的 Replay Bundle。执行规划要求 Writer / Planner /
Editor 在该门禁前保持阻断。

## 已验证工程证据

| 工作包 | 状态 | 证据 |
|---|---|---|
| M1-01～04 导入、Plan、Gold、World | 通过 | 严格 `BenchmarkBundle`/Manifest；Chapter/Scene/Block；三类 read Gold；Replay Gold；内容哈希、Unicode codepoint span、QuoteHash、章节边界与未来泄漏检查 |
| M1-05 Anchor | 通过 | World/Text/Plan 构建版本化 Anchor 与 Grounded unit；证据、层级和 basis 可追踪 |
| M1-06 R1 | 通过 | PostgreSQL 17.10 实表物化；commit 版本过滤；Exact/Entity/Predicate/Temporal 与固定深度 recursive CTE；真实集成测试通过 |
| M1-07～09 Retrieval/Context | 功能通过、质量待证 | OpenSearch 独立 Anchor/Grounded 物理索引与原子 alias；真实 BM25/filtered k-NN；Exact bypass、Hierarchy、应用 RRF、Anchor→L0、mandatory closure、provenance 与预算；锁定 BGE-M3/BGE-reranker revision 与逐文件 SHA；CPU-only loopback 服务、严格 HTTP 合同、批处理与 `batch_test_model` 隔离；真实模型功能烟测通过。合成输入仍不证明真实小说质量 |
| M1-10 Benchmark Runner | 功能通过、质量待证 | Oracle/Generated Need 接口；B0～B4、K1～K4、A0～A6 共 16 个 profile；完整指标合同与配置指纹；B1 使用最近 3 章原文加更早章节的证据绑定摘要；真实 PostgreSQL R1 + OpenSearch + BGE 双模型合成端到端运行通过；每次调用进入 RunEventLog，16 profile 进入 Evaluation Ledger。正式 Bundle 仍待提供 |
| M1-11 Curator | 工程通过、质量待证 | 通用 ExtractionRule 确定性基线；审计化模型 Curator seam 只接收当前揭示章节与当前 WorldRoot；工程侧重新绑定 commit、EvidenceRef 与 support status，拒绝跨章/篡改证据 |
| M1-12 Overlay/Validation/Commit | 工程通过、质量待证 | Candidate WorldRoot；Schema/引用、重复写、EvidenceRef/QuoteHash、根哈希、Overlay 一致性和 False World-Fact Promotion fail-closed；版本化状态转移策略覆盖状态值迁移、义务生命周期、Event narrative order；模型辅助 Validator 只能增加 finding，不能压过确定性失败；Commit 幂等 |
| M1-12A Derived Propagation | 通过 | 0002/0003 migration；同事务 outbox；带 lease 的失败/崩溃恢复；完整 MinIO→R1→双 OpenSearch 投影；每 Commit 唯一 Snapshot；WAIT/DEGRADED/BLOCK/MANUAL freshness 语义 |
| M1-13 Continuous Replay | 工程通过、质量待证 | 21→22→23 逐章 Curator→Validate→Commit→Snapshot→Freshness；确定性与模型辅助 Curator/Validator 共享同一事务路径；按章 checkpoint 计算当前状态准确率、累计漂移、非法覆盖、孤儿证据、人工修复、首次污染与传播深度；50～200 章真实回放未运行 |
| M1-14 Failure/ADR/Gate | 部分通过 | F-STATE～F-EVAL 分类；ADR-0001；PASS/CONDITIONAL_PASS/FAIL/INCOMPLETE/NOT_ELIGIBLE 正式 Gate。合成 Bundle 被强制判为 NOT_ELIGIBLE，真实失败分析待正式 Bundle |

## 本轮命令结果

```text
make quality
  Ruff passed
  Ruff format passed
  Mypy passed
  236 passed, 8 deselected
  line and branch coverage 100%

make stage1-smoke
  148 passed, 0 skipped

make model-smoke
  locked BGE-M3 embedding + BGE-reranker-v2-m3
  2 passed, 0 skipped

make model-benchmark-smoke
  PostgreSQL R1 + OpenSearch BM25/k-NN + locked BGE models + 16 profiles
  RunEventLog + 16 Evaluation Ledger entries verified
  1 passed, 0 skipped

make integration
  PostgreSQL 17.10 migration 0003 + R1/outbox/snapshot passed
  MinIO outage/recovery passed
  OpenSearch outage/recovery、双索引与 filtered k-NN passed
  MinIO + PostgreSQL + OpenSearch 完整 outbox projection passed
  suite-exclusive ports/processes/data; OTel implicit shared metrics listener disabled
  4 passed, 240 deselected, 0 skipped
```

## 未满足的正式退出条件

1. DEV-110：真实 20→3 Bundle 的 Oracle 与 End-to-End Benchmark 尚未运行；
2. DEV-113：有 Gold 的连续 50～200 章真实 replay 尚未运行；
3. BGE-M3 / BGE-reranker 的锁定制品、用户态服务、真实功能与合成端到端运行已完成；正式
   Bundle 上的质量与可接受延迟仍未运行，当前 CPU 数据也不得冒充生产容量数据；
4. 高风险 State Delta F1、False World-Fact Promotion、Evidence Binding Accuracy 等真实指标
   尚无可接受的语料证据；
5. 因此不能冻结 Memory Kernel v0.1，也不能开始执行规划中标记为 `BLOCKED，需 Stage 1 Gate`
   的 Stage 2～6。

正式 Bundle 不应要求修改任何小说专用核心代码；Importer、Manifest、Evaluator、Gate 和失败分类
合同、生产 inference seam、模型服务、runner 接线和审计持久化已经就绪。剩余工作以真实
Bundle 的 DEV-110/DEV-113 和正式 Gate 为主。正式结果必须保留配置指纹与报告，不能使用
合成分数替代。逐项审计见
`docs/stage1_gap_audit.md`。
