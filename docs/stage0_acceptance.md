# Stage 0 验收记录

日期：2026-07-20

结论：**Stage 0 退出门禁通过，可以进入 Stage 1。** EADR-07 的 Linux 用户态原生基线已
实现并在本机实跑；Docker 不再是 Stage 0 依赖。`make integration` 真实执行 PostgreSQL、
MinIO、OpenSearch 三项故障恢复测试，3 passed、0 skip；`make stage0` 完成四服务启动、
loopback 安全检查、Alembic 迁移和暂停/恢复 demo。

## 已验证证据

| 门禁 | 状态 | 证据 |
|---|---|---|
| 项目专用 Conda 与锁定依赖 | 通过 | `.conda-env` 使用 Python 3.12.13；`environment.yml` 固定 Python/uv；`uv.lock` 已按 `pyproject.toml` 更新 |
| Git 忽略边界 | 通过 | `.env`、`.conda-env`、`tmp`、`benchmarks/private`、`择天记.txt` 均由 `.gitignore` 命中，且未被跟踪 |
| Domain 与 Schema contract | 通过 | 领域模型 strict/frozen/extra-forbid；导出 Schema 位于 `schemas/stage0`；contract tests 通过 |
| 确定性质量门禁 | 通过 | `make quality`：Ruff、format、Mypy 通过；80 tests passed；branch coverage 100% |
| FastAPI 最小运行契约 | 通过 | ASGI 测试验证 `/health`、服务版本和 OpenAPI；Stage 0 未暴露创作业务端点 |
| Artifact 完整性 | 通过 | 内容寻址、元数据核验、篡改检测与 Hypothesis 哈希性质测试通过 |
| Commit 原子性与幂等 | 通过 | 相同 idempotency key 只产生一个 receipt/accepted commit；PostgreSQL 双线程同请求实跑通过；真实外键检查发现并修复 Project/Commit flush 顺序 |
| RunEvent/Checkpoint/replay | 通过 | 单调序号、append-only、幂等重放、checkpoint high-watermark 与 PostgreSQL 双线程同事件实跑通过；真实外键检查发现并修复 Stream/Event flush 顺序 |
| LangGraph 暂停与恢复 | 通过（确定性 adapter） | 固定章节在 Process Chapter 后 interrupt；重建 workflow 后 resume；最终 14 个事件且无重复提交；恢复路径 Commit 与同进程继续路径 Commit 一致 |
| 关键运行事件 | 通过 | 成功主链实际落盘 run.created/resumed/completed、task.started/completed/suspended、artifact.produced、commit.requested/accepted、checkpoint.created；其余失败/模型/副作用事件由强类型枚举与专门调用路径承载 |
| OTel 贯通 | 通过（in-memory exporter） | 同一 run 的 14 个 RunEvent 共享真实 OTel trace_id，并逐事件保存 span_id；重启前后 trace 连续 |
| 模型角色隔离与审计 | 通过 | `make quality` 排除 `model_required` 且禁止外部调用；显式 smoke 仅调用 batch_test_model fake；完整 role/purpose/endpoint/version/cost/latency/trace/span 记录可由 RunEventLog 强类型持久化和重放 |
| Evaluation Harness | 通过 | 固定 run config、配置指纹、append-only ledger 与带配置元数据的 Parquet 导出测试通过 |
| Alembic migration 生成 | 通过（offline） | PostgreSQL dialect 成功生成 Stage 0 全部事务 DDL |
| 真实 PostgreSQL + durable checkpoint | 通过 | PostgreSQL 17.10 suite 独占 cluster 完成 migration、并发 Commit/Event 幂等、停机失败、同目录恢复和持久 checkpoint |
| 真实 MinIO | 通过 | 锁定版本原生 MinIO 完成 round-trip、停机失败和同目录原对象恢复 |
| 真实 OpenSearch | 通过 | OpenSearch 3.7.0 原生实例完成 index/search、停机失败和同目录索引恢复；恢复探针等待集群 yellow/green 且无 relocating shard |
| fresh-clone 单命令 | 通过 | `make stage0` 默认 `INFRA_BACKEND=native`，不调用 Docker 或 sudo；bootstrap、health、migration、demo 全链通过 |
| Native 安全边界 | 通过 | 普通用户运行、官方 HTTPS 版本锁与 checksum、loopback-only、SCRAM、`.env` 0600、精确 PID/owner/exe/start-time/data-dir 校验、测试与开发数据隔离 |

## 本轮命令结果

```text
make quality
  Ruff passed
  Ruff format passed
  Mypy passed
  80 passed, 4 deselected
  branch coverage 100%

make integration
  3 passed, 81 deselected, 0 skipped

make stage0
  native bootstrap/locked checksum verification passed
  PostgreSQL 17.10, OpenSearch 3.7.0, MinIO RELEASE.2025-06-13T11-33-47Z,
  OTel Collector 0.156.0 health and loopback guards passed
  Alembic upgrade passed
  demo completed: 14 events, 0 model calls
```

## 原生运行证据

锁定下载摘要记录在 `infra/native-services.lock`；本次服务版本、实际可执行文件 SHA-256、
配置指纹、UID、PID/start-time、数据目录和端口证据记录在忽略的
`tmp/native/run/dev/default/health-evidence.json`。OpenSearch 使用
`node.store.allow_mmap: false`，该结果只证明功能正确，不作为生产性能数据。Docker parity
run 可后补，但不影响本机 Stage 0 放行。
