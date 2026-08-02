# Current development task

- Task: Stage 2M G06/G09 support-group and compound-claim quality
- Stage: Stage 2M
- Planner/reviewer: Codex
- Implementer: OpenCode default `build` agent via `/implement`
- Workflow state: `WAITING_FOR_HUMAN_APPROVAL`
- Repair allowance: `0/1`
- Merge policy: `CODEX_ON_PASS`

## User decisions

- Preserve the existing Stage 2M document workflow; optimize only the execution interface.
- Do not add an arbitrary fixed small Need limit.
- Do not treat evidence-receipt matching alone as retrieval-quality improvement.
- Avoid endpoint timeout/error loops; a failed endpoint run is non-comparable and must stop retries.
- Use the active Qwen3.6 service at `http://127.0.0.1:8002/v1` only for the single bounded
  post-offline synthetic smoke authorized by `.agent/plan.md`; read the current model ID from
  `/models` and record the result in `.agent/implementation.md`.
- Do not run C80, P3, the five-point matrix, or Stage 3 work in this task.

## Canonical authority

- `docs/stage2_memory_benchmark_task_closure_execution.md`, sections 0.1.7 and 0.2.
- `docs/stage2m_gate_m4_root_cause_and_remediation_20260730.md`, sections 7 and 8.3.
