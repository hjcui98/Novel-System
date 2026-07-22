# Stage 1 工作包证据与缺口审计

日期：2026-07-21

本审计把“合同存在”“确定性回归通过”“真实基础设施通过”和“真实质量已证明”分开记录。
只有最后一层可以支持 Stage 1 正式退出；合成 fixture 永远不能自行晋升为正式证据。

| 工作包 | 已有可复现证据 | 尚未完成 | 当前判定 |
|---|---|---|---|
| M1-01 | Chapter/Scene/Block、Unicode codepoint、TextRoot hash 与边界校验 | 真实 Bundle 导入 | 工程通过 |
| M1-02 | PlanRoot、ChapterGoal、义务引用与 hash 校验 | 真实 Plan 标注 | 工程通过 |
| M1-03 | BenchmarkBundle、三类 read Gold、Replay Gold、Evidence/QuoteHash、未来泄漏拒绝 | 用户授权的真实 Bundle | 工程通过，质量待证 |
| M1-04 | 最小 WorldRoot Schema、ID 唯一性和实体引用闭包 | 真实题材表达覆盖率 | 工程通过，质量待证 |
| M1-05 | Anchor/Grounded 构建、层级和精确 EvidenceRef | 真实 Anchor 质量 | 工程通过，质量待证 |
| M1-06 | PostgreSQL 17.10 R1 物化、版本/时间过滤和 recursive CTE 集成测试 | 正式数据规模与延迟 | 功能通过 |
| M1-07 | OpenSearch 双索引、严格 basis filter、BM25、filtered k-NN、原子 alias；锁定 BGE-M3 revision/逐文件 SHA；CPU float32/1024d/normalize loopback 服务；严格 count/index/dimension、批处理、PID 身份与 runtime fingerprint；真实功能和合成端到端烟测通过 | 正式 Bundle 上的质量/可接受延迟；生产容量基准 | 功能通过，质量待证 |
| M1-08 | 确定性 Need；经 ModelGateway 审计的 HorizonNeedSet 生成；无未来文本/Gold prompt；Runner 可注入冻结后的 Oracle/Generated Need 并记录 condition/profile | 在正式 Bundle 上分别运行两种 query condition | 工程通过，质量待证 |
| M1-08A | 单次应用 RRF、通道 rank、去重、配额；锁定 BGE-reranker-v2-m3 revision/SHA；CPU loopback 服务与严格响应校验；真实中文 rerank 和完整矩阵烟测通过 | 正式 Bundle 上的真实质量对照 | 功能通过，质量待证 |
| M1-09 | Anchor→L0、有界预算、mandatory closure、trace/provenance | 真实 token 效用 | 工程通过，质量待证 |
| M1-10 | 16 profile 矩阵、指标合同、配置指纹、正式 Gate；生产 Runner 实际跨 PostgreSQL R1、OpenSearch 和两个 BGE 服务；外部 basis 以合法 Project/Commit/RootManifest 幂等注册；模型调用进入 RunEventLog，16 profile 进入 append-only Evaluation Ledger；B1 证据绑定摘要 | 正式 Bundle 的质量报告与 Gate | 功能通过，质量待证 |
| M1-11 | 规则 Curator 基线；审计化模型 Curator seam；prompt 只含当前揭示章节和当前 WorldRoot；工程侧绑定 canonical commit、EvidenceRef、support status，拒绝跨章/篡改/重复目标 | 正式 Bundle 上的真实抽取精度 | 工程通过，质量待证 |
| M1-12 | Overlay、Schema/引用、Evidence、冲突、root hash、truth promotion、事务幂等；版本化状态转移和义务生命周期策略；模型 Validator 只能追加 finding，确定性失败时零模型调用 | 正式 Bundle 上的状态策略覆盖率和模型 Validator 质量 | 工程通过，质量待证 |
| M1-12A | 带 lease 的 outbox 恢复、完整三服务投影、snapshot/alias freshness | 正式长回放压力与恢复测试 | 功能通过 |
| M1-13 | 3 章合成逐章闭环；确定性/模型辅助 Curator 与 Validator 共享提交路径；内容敏感 Gold evaluator；逐章 state checkpoint、分类错误、非法覆盖、孤儿证据、人工修复、首次污染与传播深度账本 | 50～200 章真实 replay | 工程通过，质量待证 |
| M1-14 | 失败分类、ADR、五态正式 Gate；synthetic 强制 NOT_ELIGIBLE | 真实失败账本和最终选型结论 | 部分通过 |

## 审计中修复的高风险问题

1. R1 从接口 smoke 提升为 PostgreSQL 真实物化与查询。
2. Dense 从内存 smoke 提升为 OpenSearch 双索引 filtered k-NN；测试 hash embedding 明确不作为
   BGE-M3 质量证据。
3. Derived Snapshot 从 metadata-only 提升为 MinIO artifact 读取、R1 物化、Anchor/Grounded 建索引
   与 alias 发布的跨服务闭环。
4. Outbox 增加 worker ownership 与 lease，能够接管崩溃后遗留的 processing 任务。
5. Replay Gold 不再只比较目标键；`expected_record` 标注字段错误时不得获得命中分。
6. Gate 显式拒绝 synthetic/non-gate Bundle，且缺 profile、缺长回放或缺双轨结果时返回
   INCOMPLETE/NOT_ELIGIBLE，而不是误报 PASS。
7. B1 不再用历史原文冒充摘要；历史摘要必须绑定来源 TextRoot、章节和 EvidenceRef。
8. 模型 Curator/Validator 接入仍服从确定性校验、角色隔离和完整模型调用审计，模型不能自行决定
   canonical basis 或覆盖确定性失败。
9. Replay 增加版本化状态 checkpoint 与漂移传播账本，错误内容不能仅靠命中目标键获得通过。
10. 检索模型制品固定到 BAAI commit 与逐文件 SHA，下载只允许锁文件列出的官方 HTTPS
    重定向链；服务只监听 loopback，并核验 PID owner/start time/完整命令。
11. 生产 Benchmark 不再直接向 R1 写入不存在的项目：外部 verified basis 先注册只读
    RootManifest，保留 source commit 且不绕过外键。
12. 空检索结果的 future leakage/error-rate 分母语义修正为 0，避免 `_ratio(0, 0)` 将“无证据”
    误报成 100% 未来泄漏；真实模型烟测覆盖该回归。
13. OTel 原生 integration 显式关闭 Collector 自身的默认 8888 metrics reader，避免 suite 独占
    实例与 dev 实例共享隐式端口；OTLP traces/metrics/logs 接收管线保持启用。

## 下一实现顺序

1. 导入用户提供且可授权使用的真实 Bundle；
2. 以已锁定的 `batch_test_model` BGE 服务运行 DEV-110、DEV-113 和正式
   Stage1GateEvaluator，保存配置指纹、RunEventLog、Evaluation Ledger 和失败账本；
3. 仅在真实门禁通过后冻结 Memory Kernel v0.1，并解除 Stage 2 阻断。
