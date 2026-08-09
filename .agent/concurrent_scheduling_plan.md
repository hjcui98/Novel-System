# Stage 2M 模型调用并发调度改造规划（最终决策版）

- 用途：优化 LLM 调用调度机制和服务利用率。与 Plan-Conditioned 语义改造正交——并发只改变调用时间，不改变语义。
- 日期：2026-08-06（最终版，替换原草案）→ 2026-08-07（C0-C3 实施完成，证据见 `.agent/implementation.md` §16）→ 2026-08-08（最小充分调度边界补充）→ 2026-08-09（当前 endpoint 负载准入验收）
- 依据：vLLM 8004 MTP 并发基准（`bench_concurrent.py`）+ 架构文档 O5 决议 + C1 真实 workload 基准
- 关联文档：`.agent/need_pipeline_audit_and_semantics.md`（O5、O17）

---

## 0. 实施状态

| 阶段 | 状态 | 证据 |
|---|---|---|
| C0 调用 DAG 审计 | ✅ 完成 | §16.1（Planner 1 次/run + 走廊 need 级独立 + 评估器 batch） |
| C1 workload 基准 | ✅ 完成 | §16.3（串行→8 并发 4.2x，KV 预算标定） |
| C2 Evaluator batch 并发 | ✅ 完成 | §16.2（max_concurrent_batches=4，gather） |
| C3 走廊 Need 并发 + KV 双限流 | ✅ 完成 | §16.4（max_concurrent_needs + max_inflight_kv_tokens，1576 passed 100%） |
| C4 多 Case 并行 | ⏳ 待启动 | — |
| C5 可选 async 化 | ⏳ 待评估 | — |

**C1 历史短负载推荐参数**（8002：max-num-seqs=8 + MTP=2 + prefix caching 已就绪；不作为当前
Claim Support multi 正式配置）：
```yaml
endpoint_request_limit: 8            # 8002 已配置
application_default_max_inflight: 4  # 保守（2.7x, KV 64%）；6 为激进（4.1x, KV 92%）
application_kv_token_budget: 200000  # ≈ 8×25K 极端 + 20% reserve 的保守值
kv_safety_reserve_ratio: 0.20
```

### 0.2 当前本地 Qwen 正式运行约束（2026-08-09）

最终 P002/C40 multi-slice workload 证明，旧短负载吞吐基准不能直接外推到当前约 27K-29K input-token
的结构化生成。endpoint request limit 2 下，相同 input hash `dd858...` 从 serial 的 243 output tokens
异常增长到 2048 并 length-truncate；limit 1 下 serial 与 safe-concurrent 均稳定产生合法 proposal 和
verified receipt。

因此本次 APC/TIO 正式 Phase 4 固定：

```yaml
support_max_concurrent_needs: 2
endpoint_request_limit: 1
evaluator_max_concurrent_batches: 1
checkpoint_workers: 1
support_multi_enable_thinking: false
support_multi_thinking_token_budget: 0
support_multi_max_output_tokens: 2048
```

Need concurrency 2 仍允许 deterministic preparation、排队和非 endpoint 工作重叠，但 endpoint-global
admission controller 必须保证实际同时生成数为 1。该约束是当前 endpoint/model/workload 的实测配置，
不撤销 request+KV 双限流、typed timeout、descriptor 或 single-flight，也不推广成其他端点永远只能
串行。C4 多 Case 并行继续 deferred；本次正式矩阵不需要为追求吞吐实现它。

### 0.1 batched-tokens 复测结论（2026-08-07，服务端维持 2048 不动）

针对"提升 `--max-num-batched-tokens` 释放长 prompt prefill"的假设做真实复测
（8002 重启后，10.5K 与 15K prompt 负载、8 并发）：

| 配置 | 负载 | 并发 wall | 聚合 decode | 加速比 | 结论 |
|---|---|---|---|---|---|
| batched 2048 / util 0.90 | 10.5K prompt | 45.8s | 139.8 tok/s | 6.4x | 基线 |
| batched 16384 / util 0.95 | 10.5K prompt | 46.4s | 138.0 tok/s | 6.6x | 与基线几乎一致 |
| batched 2048 | 15K prompt | 48.1s（串行 167s） | — | 3.5x | 基线 |
| batched 16384 | 15K prompt | **CUDA OOM** | — | — | 一次 prefill 16000 tokens，activation 峰值爆 32GB（Tried to allocate 188 MiB ... 138 MiB free） |

**决策**：
- 提升 batched-tokens **无收益且有害**——2048 vs 16384 在 10.5K 负载下并发吞吐几乎一致
  （139.8 vs 138.0 tok/s），chunked prefill 分块并未造成可测调度损失；
  16384 在 15K 长 prompt 下直接 OOM。
- 服务端**维持 `--max-num-batched-tokens 2048` + util 0.90 + max-num-seqs=8** 不变
  （稳定配置，8 并发可用）。

---

## 1. 定位与基本原则

本改造只优化 **LLM 调用调度机制和服务利用率**，不修改 Need 生成、检索、证据、Claim Support、completion contract 或评测语义。

由于 Stage 2M 正在进行 Plan-Conditioned Need 架构改造，最终并发方案不再绑定当前旧管线固定的"24 Need / 79 次调用"拓扑。语义架构修改完成并稳定模型调用接口后，应重新绘制实际调用 DAG，再依据调用之间的数据依赖关系确定并发边界。

总体原则：

```text
语义架构
→ 定义模型调用 DAG
→ 判断调用是否独立
→ 确定可并发工作单元
→ 根据 KV 容量和服务端容量动态调度
```

并发调度只允许改变：请求何时提交、同时有多少独立请求执行、等待队列顺序、服务端 batch 形成方式。

并发调度不得改变：system/user prompt 内容、AuthorPlanningContext、Stage1MemoryNeed、RetrievalQueryBundle、workset、evidence slices、completion facets、单请求输入 token 预算、max_tokens、chunk 语义、claim 生成逻辑、Writer 4000 / Ledger 12000 等既有上下文预算。

核心约束：

> **Concurrency changes scheduling, not semantics or context.**

KV 不足时必须排队或降低同时运行请求数，不允许通过截断上下文、删除证据或降低 token 预算换取并发量。

### 1.1 最小充分调度边界

本任务只需要一个进程内、endpoint-global、request-count + KV-token 双限流的调度边界，并复用
现有 Model Gateway、调用记录和 Artifact/receipt 体系。优先对现有 admission controller 做
责任上移和语义补全，不另建调度微服务、分布式队列、通用 DAG Runtime、资源自治控制面、动态
优先级 DSL、自动扩缩容或全 Stage async 框架。

新增 scheduling descriptor、lease、typed timeout 和 single-flight 只因它们分别保护上下文身份、
容量释放、失败归因和重复调用不变量；不扩展为与当前 endpoint/Case 无关的通用平台。局部 async
足以满足正确性和吞吐时停止；只有实测证明局部方案无法满足当前 Gate，才返回 Codex 讨论更大
改造。最小充分不允许取消双预算、上下文 parity、稳定 artifact 顺序、异常释放、Telemetry 或真实
负载验收。

---

## 2. 服务端基准结论

当前 vLLM 8004：Qwen3.6-27B-NVFP4、TP=1、max-model-len=131072、FP8 KV、prefix cache enabled。

| MTP | 串行 8 请求 | 并发 8 请求 | 并发聚合 decode |
|---|---|---|---|
| MTP=2 | 282.8s | 38.7s | **165.3 tok/s（7.3x）** |
| MTP=3 | 268.9s | 68.3s | 93.7 tok/s |
| MTP=4 | 224.2s | 60.4s | 105.9 tok/s |

**决策**：
- MTP=2 保留为 Stage 2M 并发服务首选配置
- 服务端可配置 `max-num-seqs=8`，但不等于应用固定 8 并发
- `max-num-seqs=8` 是 vLLM 服务容量上限，最终同时运行多少请求由实际上下文长度和 KV token 预算决定
- 现有 benchmark 证明模型服务具有较高的批量并发收益，但不直接决定生产并发度

---

## 3. 语义架构修改后的调用 DAG 审计

Plan-Conditioned 改造完成后，预期主要链路为：

```text
AuthorPlanningContext
  → LLM Need Planner（单次整体规划，输入 PlanningContext + PlannerWorldSummary → PlannedNeedDraft[]）
  → Grounder（确定性代码，各 Draft 独立处理）
  → Validator（去重/合并/budget/priority 需全部 Draft 可见后统一完成）
  → Stage1MemoryNeed[]
  → Per-channel Query Compiler（Need → RetrievalQueryBundle，各 Need 独立）
  → Retrieval / Workset（不同 Need 的检索通常相互独立）
  → Claim Support Corridor（主要 LLM 并发收益点，跨 Need 独立可并发，同 Need 内串行）
  → ContextPackage → Evaluator
```

并发设计必须在上述接口稳定后重新审计，不硬编码当前旧管线的调用数量。

### 各阶段并发判断

| 阶段 | LLM 调用 | 可并发性 | 备注 |
|---|---|---|---|
| LLM Need Planner | 1次/run | 单次无内部并发 | Benchmark 若存在冻结 Planner artifact 则跳过 |
| Grounder | 无 LLM | — | 确定性代码，可 per-Draft 并行但非 GPU 优化重点 |
| Validator | 无 LLM | 全局去重需串行 | — |
| Query Compiler | 无 LLM | 各 Need 独立 | 确定性代码 |
| Retrieval | 无 LLM | 各 Need 独立 | 与 LLM 服务调度分别限流 |
| Claim Support Corridor | **79次（旧管线）** | **跨 Need 独立可并发** | 主要收益点；同 Need 内 proposal→verify→chunk 保持串行 |
| Evaluator | 2-3次（batch_size=2） | batch 间可并发 | batch_size 调整另做实验 |

---

## 4. 上下文完整性硬约束

并发模式下，每个请求必须与对应非并发模式具有相同的语义输入。至少保证 context_hash、prompt tokens、max output tokens、evidence set、evidence ordering、completion contract、model parameters 不会因并发发生变化。

禁止：为提高并发量降低 context window、缩减 workset、丢弃 evidence slice、改变 chunk 大小、降低 max output token、静默截断 prompt、因 KV 不足切换到简化 prompt、因排队时间过长跳过某个 mandatory Need。

请求如果本身超过模型单序列 context limit，应明确失败 `CONTEXT_BUDGET_EXCEEDED`，不得由 Scheduler 自动裁剪。

---

## 5. KV-aware Admission Control

应用层不能只使用 `Semaphore(max_inflight=8)`，因为不同请求的 KV 占用差异可能非常大。

每个准备提交给模型的请求应生成调度描述：

```python
@dataclass(frozen=True)
class ModelRequestSchedulingInfo:
    request_id: str
    need_id: str | None
    stage: str
    estimated_prompt_tokens: int
    reserved_output_tokens: int
    reserved_sequence_tokens: int  # = estimated_prompt + reserved_output + safety_margin
    dependency_ids: tuple[str, ...]
    context_hash: str
    priority: int
```

Scheduler 同时满足两个条件才允许提交：

```text
inflight_request_count ≤ endpoint_request_limit
sum(inflight_reserved_sequence_tokens) ≤ application_kv_token_budget
```

因此实际情况可能是：8×8K 请求可同时执行，4×30K 请求已接近预算，8×30K 请求不允许全部同时提交。

> **并发度是动态结果，不是固定常数。**

应用层初始建议（真实 workload 基准后确定）：

```yaml
endpoint_request_limit: 8
application_default_max_inflight: 4
application_kv_token_budget: <实测后确定>
kv_safety_reserve_ratio: 0.20
```

---

## 6. Prefix Cache 原则

Prefix cache 只视为性能优化，不纳入安全容量保证。原因：不同 Need 的 workset 不同、evidence slices 不同、proposal / verify prompt 不同、Planner 与 Corridor 的 prefix 结构也不同。

Admission Control 必须使用保守 token 估计，即使当前 benchmark 的 warm prefix cache 命中很好，也不得因此允许超额提交长请求。

---

## 7. 调度方式决策

在 Plan-Conditioned 语义改造完成后，基于新的模型调用点重新核查。首选结构：

```text
外部接口保持现有调用方式
  → 局部 async scheduler
  → 多个独立 Need pipeline 并发
  → 单 Need pipeline 内保持串行
```

即优先采用"局部 async island"，而不是直接全链路 async 化。

若现有 ModelGateway / HTTP client 的 event-loop 生命周期使局部 async 难以落地，再评估 `ThreadPoolExecutor` + 每 worker 独立 async lifecycle。不优先直接实施全 Stage 2 async 化。

---

## 8. 共享状态处理

并发 worker 不直接无序修改全局 mutable state。

- **Funnel**：每个 Need 返回 `SupportFunnelDelta`，由 coordinator 确定性汇总
- **Receipts / Attestations / Workset Reports**：worker 局部收集后按 need_index → local_sequence 排序归并
- **Progress Events**：runtime progress 允许按真实完成顺序产生；persisted audit artifact 按稳定 key 排序后保存
- **Verification Cache**：必须支持并发安全访问 + 相同 key 的 in-flight deduplication，防止两个独立 Need 同时请求相同验证而重复调用模型
- **Artifact**：worker 不直接并发写同一 artifact；统一为 worker 返回结果 → coordinator 汇总 → 单点持久化

---

## 9. 失败与排队语义

并发调度不得改变原有失败边界。

- 单个请求失败：只影响所属 Need/chunk，保留原来的 retry/failure policy，不取消无依赖的其他 Need
- KV 容量不足：`WAITING_FOR_CAPACITY`，不是失败
- 等待超过调度 timeout：`SCHEDULING_TIMEOUT`，必须与 `MODEL_TIMEOUT` / `RETRIEVAL_FAILURE` / `VALIDATION_FAILURE` 语义区分

---

## 10. 多 Case 全局预算

若同时运行多个 Case，不允许 `3 cases × 每 case 8 requests = 24 requests` 同时压向 `max-num-seqs=8` endpoint。

应满足：
```text
sum(all_cases_inflight_requests) ≤ endpoint_request_limit
sum(all_cases_reserved_sequence_tokens) ≤ application_kv_token_budget
```

短期 P001-P005 运行可采用简单额度分配（如 `case_parallelism: 2, per_case_max_inflight: 4, endpoint_request_limit: 8`）。后续若需要动态共享资源，再实现全局 Scheduler。

---

## 11. 实施阶段

并发改造在 Plan-Conditioned 语义架构确定最终调用接口和调用 DAG 后启动。

| 阶段 | 内容 | 前提 |
|---|---|---|
| C0 | 真实调用 DAG 审计（模型调用点、调用数、dependency、prompt/output token 分布、跨 Need 共享状态） | 语义 Phase 2 接口稳定 |
| C1 | 真实 workload 服务基准（MTP=2, max-num-seqs=8, concurrency=1/2/4/6/8）确定 KV token budget 和首选并发策略 | C0 完成 |
| C2 | Evaluator 独立 batch 并发（batch_size 保持 2，只并发独立 batch） | 随时可开始 |
| C3 | Claim Support 独立 Need 并发（跨 Need 并发 + 同 Need 内串行 + request-count + KV-token 双限流） | C1 完成 |
| C4 | 多 Case 并行（2-3 case，全局预算） | C3 稳定 |
| C5 | 可选进一步 async 化 | 仅当 C3 仍为明显瓶颈 |

---

## 12. 验收标准

并发改造验收的目标是**执行机制正确 + 上下文完全保持 + 吞吐提高**，不是修改或提升语义质量。

必检验收：
- 所有 dependency 顺序正确；proposal 先于对应 verify；同 Need chunk early-stop 仍正确
- 每个请求 context_hash 与非并发调度对应输入一致；prompt token 内容不因调度缩减；evidence 不因 KV 压力被删除
- 无 KV OOM；preemption/recomputation 控制在合理范围；容量不足请求进入等待队列
- receipts / attestations / funnel 完整；artifact 最终顺序稳定；failure attribution 正确

性能目标由真实 workload benchmark 决定，不提前硬编码"必须 4x"。旧 40-50min → 6-10min 只保留为实验预期，不作为正确性 Gate。

---

## 13. 与 Plan-Conditioned 语义改造的关系

两项工作相互正交：

| Plan-Conditioned 改造 | Concurrency Scheduler |
|---|---|
| 决定调用什么、输入什么、依赖什么 | 决定这些已确定调用何时执行 |
| 修改 semantic question、Need、Query Bundle | 不触碰以上任何内容 |

语义代码和调度代码独立提交。语义 Phase 2 完成、最终调用 DAG 足够稳定后，即可开始 C0/C1；不需要等全部 P001-P005 质量评测结束才开始并发开发。

---

## 14. 最终决策汇总

1. **MTP=2** 保留为 Stage 2M 并发服务首选配置
2. 服务端可配置 `max-num-seqs=8`，但不等于应用固定 8 并发
3. 应用层使用 **request-count + KV-token budget 双重 admission control**
4. KV 不足时等待或降低并发，**绝不缩减单请求上下文**
5. 并发边界由 Plan-Conditioned 改造后的真实调用 DAG 决定
6. 独立 Need pipeline 是预期主要并发单位；同 Need 内具有因果/early-stop 依赖的调用继续串行
7. Evaluator batch 间可以并发，但 batch size 调整另做实验
8. 多 Case 并发纳入后续阶段，并共享 endpoint 全局预算
9. 优先采用局部 async scheduler；是否使用线程池由最终调用栈审计决定
10. 并发改造的验收目标是**调度正确、上下文完整和性能提升**，不改变模型任务语义
11. 不建设独立调度平台、分布式队列、动态 DSL 或全链 async；当前局部 scheduler 达到合同后即停止扩张
