# Codex acceptance review

- Outcome: `PASS`
- Reviewed: `2026-08-09 +08:00`
- Scope: current-v32 P002/C40 bounded serial/safe-concurrent evidence、2048 Claim Support
  transport policy、frozen Planner lineage，以及 `.agent/plan.md` §§6.1-6.2 的正式运行准入
- Review mode: read-only；未重跑测试、quality、pre-commit、benchmark、模型/API 或正式 Phase 4
- Accepted executable identity:
  - HEAD `420e16303c18354e72fa6486eb8968707842ef71`
  - `_code_source_fingerprint()`：
    `sha256:20daa522f815c88c5ab823d2b03ff896b6751264dd6edac2777a4d93b089b881`
  - producer `trusted_claim_support_producer.v32`
  - `git diff --check` 无输出

## Decision

本次实现与最终源码身份证据通过验收。上一轮唯一缺口——成功 bounded 工件仍属于 producer v31 / 旧
source fingerprint——已经关闭：serial 与 safe-concurrent manifest 均绑定当前 producer v32 和相同
源码指纹 `20daa...881`；运行后没有 executable source 变化，只追加了 implementation 证据。

`.agent/plan.md` §§5.1-5.5、§6.1 和 §6.2 的实现/准入工作现已接受。允许进入 §6.3，但本次
`PASS` 不是 Stage 2M、M4 或 Gate 0-3 PASS，也不代表当前 dirty diagnostic 工件可以直接升级为正式
矩阵结果。正式 Phase 4 仍须先形成 clean executable-source identity，再使用全新 DB、output root、
experiment ID 和 checkpoint-scoped report 运行 APC P001-P005 与 TIO ablation。

## Evidence accepted

1. 两侧 manifest 均为 producer v32、源码指纹 `20daa...881`、同一非 fallback Planner artifact
   `sha256:a1231b1d4bf4022295b06b44034d2ee8e953fdba70711327490f2db70c3e3ee2`，并固定
   `thinking=false / reasoning budget=0 / multi output=2048`。
2. serial 使用 Need/endpoint `1/1`；safe-concurrent 使用 `2/1`。两侧均有 14 个 scheduler
   descriptors。除每次运行按实际开始时间生成、预期不同的绝对 `scheduling_deadline` 外，request ID、
   Need、stage、dependency、endpoint、context hash、prompt/output/sequence reservation、safety、priority
   和 timeout 全部一致。
3. 去除真实时间、latency 和 sequence 后，33 个 support progress event 全量一致：23 个
   handle-audit、5 个 proposal、2 个 verification、funnel/terminal/freeze 各 1 个；其中所有 proposal
   与 verification 的 input/output hash、workset 和调用身份一致。
4. 两侧 scheduler 均 acquired/released `14/14`，peak request `1`、peak KV `31,106`，结束时
   request/KV `0/0`，scheduling timeout 和 unsatisfiable 均为 0。safe-concurrent 的 78.474 秒等待被
   正确记录为 endpoint capacity wait，没有 OOM、context reduction、duplicate equal-key verification
   或 lease leak。
5. 两份 paired artifact 均可按内容哈希解引用，各含 28 个 verified receipts；其中两个 receipt 的
   `producer` 为 `trusted_claim_support_producer.v32.synthesized`，同时保留 multi proposal 和 whole
   verifier call record，并分别闭合两个最终 Need。
6. 两侧 Writer 输出完全相同：31 个 selected units、Writer 1,061/4,000 tokens、Ledger
   11,981/12,000 tokens、107 个语义相同 Ledger entries、相同 rendered context 和相同 assembly spec。
   `e2e_paired_report.json` 字节级相同，five-segment 结构完全相同；future leakage、future-isolation
   failure 和 plan leakage 均为 0。
7. `support_terminal_state=completed_with_failures` 保持如实：两侧各有一个模型质量层 proposal
   rejection，但两个最终 Need 均形成 verified receipt，且无 transport/verifier failure、insufficient
   Need 或 unclosed facet。该状态不阻塞本次调度/链路准入，也不得改写成纯 `completed`。
8. 当前 handoff 声明的 `make quality`（1642 passed、9 deselected、100% branch coverage）、全量
   pre-commit、Ruff/MyPy 与 `git diff --check` 证据被接受；Codex 本轮没有代为重跑。

## Artifact attribution correction

`.agent/implementation.md` §24 将两份 paired artifact 的 serial/safe 归属写反。运行态
`scenario_run.json` 是权威证据，正确映射为：

- serial：`sha256:7afe7fdb60cd09f9bebb774050966eb7fb79662db681021c0f217948a5aa54aa`；
- safe-concurrent：`sha256:f364daffcad5436a2f9e17ede3f9f3d4c61788b119c30676deb82a73d9247c02`。

两者内容、长度和 receipt 结论均已独立核验，路径归属笔误不影响验收，不要求重跑。

## Accepted operational boundary

本地 `qwen36-27b-nvfp4` 的当前正式配置必须保持 endpoint request limit 1。endpoint limit 2 已证明
会使同一 multi prompt 从 serial 243 output tokens 异常增长到 2048 并截断；因此 Need concurrency 2
只用于准备、排队与非 endpoint 工作重叠，不得制造两路同时生成。这是当前 endpoint/model/workload 的
实测部署约束，不是把 endpoint-global scheduler 简化为普通信号量，也不是全局禁止未来其他端点并发。

不再增加动态调参、第二 scheduler、并发控制面或额外报告体系。若未来要提高本地 endpoint 生成并发，
必须以独立服务端根因修复和相同 prompt parity 实测重新取得准入，不能在正式 Phase 4 中边跑边调。

## Next permitted action

1. 保持当前 accepted executable tree 不变并形成 clean executable-source identity；
2. 按 `.agent/plan.md` §6.3 使用全新 DB/output/experiment identities 运行正式 APC P001-P005；
3. 使用同一固定语义预算运行 TASK_INTENT_ONLY ablation；
4. 每个 checkpoint 独立归档 report/manifest child ref，禁止覆盖式保存；
5. 由新矩阵报告 Gate 0-3 指标、leakage 和 held-out 结果，再交回 Codex 做 Gate 判断。

P004/P005 继续冻结，不得根据运行结果修改输入、Gold、prompt、预算或阈值。未运行 Agentic 时必须明确
报告“无 paired claim”，但不因此否定 Arm A。未经用户授权，本次 review 不 commit、不 merge，也不
启动正式 Phase 4。
