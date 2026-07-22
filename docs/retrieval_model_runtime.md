# 锁定检索模型运行基线

日期：2026-07-21

本基线只服务于 Stage 1 正式 Benchmark 的 embedding/reranker 路径，不属于开发模型端点，
也不得回退到 `implementation_model`。

## 固定模型

| 角色 | 模型 | 固定 revision | 运行语义 |
|---|---|---|---|
| embedding | `BAAI/bge-m3` | `5617a9f61b028005a4858fdac845db406aefb181` | CPU float32、1024 维、L2 normalize、最大 8192 tokens |
| reranker | `BAAI/bge-reranker-v2-m3` | `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` | CPU float32、sigmoid relevance、最大 8192 tokens |

逐文件大小和 SHA-256 位于 `infra/retrieval-models.lock`。下载入口固定为 BAAI 的 Hugging Face
仓库完整 commit URL；下载器只接受 HTTPS 和锁文件列出的重定向主机。模型权重写入 Git ignored
的 `models/retrieval/`，不会进入工程版本库。

BGE-M3 的发布 commit 只提供 `pytorch_model.bin` 主权重。运行时固定使用现代 Transformers 的
`torch.load(..., weights_only=True)` 路径，并同时设置 `trust_remote_code=False`、
`local_files_only=True` 和离线环境变量。reranker 使用发布方提供的 `model.safetensors`。

## 生命周期

```bash
make models-bootstrap  # 安装锁定的 CPU-only extra，下载并校验全部文件
make models-up         # 两个普通用户进程，仅绑定 127.0.0.1
make models-health     # 同时核对 PID owner/start time/完整命令和版本化 health
make model-smoke       # 显式 model_required：真实向量与中文 rerank 功能断言
make models-down       # 只向 PID 记录精确匹配的本用户进程发信号
```

默认端口为 embedding `8081`、reranker `8082`。可通过 `.env` 中的
`NOVEL_AGENT_EMBEDDING_MODEL_PORT` 和 `NOVEL_AGENT_RERANKER_MODEL_PORT` 修改，但仍只能绑定
loopback。每个模型 profile 包含 model、revision、task、device、dtype、最大输入、normalize 和
dimension；其 SHA-256 runtime fingerprint 会进入 HTTP route profile 和 Benchmark 配置指纹。

## 正式 Oracle Benchmark 接线

在原生基础设施和模型服务健康、数据库 migration 已完成后运行：

```bash
make stage1-native-benchmark \
  BUNDLE=/absolute/private/path/bundle.json \
  CASE_ID=case-id \
  OUTPUT=/absolute/private/path/result.json
```

该入口会：

1. 严格导入并重新校验 `BenchmarkBundle`；
2. 把 verified WorldRoot 物化到 PostgreSQL R1；
3. 使用真实 BGE-M3 构建 OpenSearch Anchor/Grounded 双索引；
4. 以 PostgreSQL R1 + OpenSearch BM25/k-NN + BGE reranker 运行完整 16-profile Oracle 矩阵；
5. 为外部 verified basis 幂等注册父提交为空的只读工作区 RootManifest，不绕过 Project/Commit
   外键；
6. 每次 embedding/rerank 调用即时写入 `RunEventLog`，记录 run/task/trace、模型角色、用途、
   revision、runtime fingerprint、用量、零本地费用、延迟与失败类型；
7. 在结果中保存 snapshot、embedding、reranker、Need 和 Summary profile，并把 16 个 profile
   的指标、失败码及模型审计元数据写入 PostgreSQL append-only Evaluation Ledger。

服务缺失、PID 身份不匹配、模型版本不匹配、数据库 migration 不完整或 OpenSearch 不可用时，
命令必须失败。当前宿主机没有可用 GPU，因此本路径能提供功能和 CPU 延迟证据，但不得冒充 GPU
或生产容量数据。

## 已验证功能证据

2026-07-21 在当前 Linux CPU 主机上已完成：

```text
make model-smoke
  2 passed（真实 BGE-M3 embedding、真实 BGE reranker）

make model-benchmark-smoke
  1 passed（PostgreSQL R1 + OpenSearch 双索引 + 两个真实模型 + 16 profiles）
  RunEventLog 调用数与结果调用数一致
  Evaluation Ledger 记录数与 16 profiles 一致
```

该证据证明锁定制品、进程/API、生产检索接线和审计持久化可工作；输入仍是许可安全的合成
fixture，因此不证明真实小说质量，也不是生产容量数据。
