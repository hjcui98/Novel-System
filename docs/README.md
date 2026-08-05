# Project documentation index

> Lifecycle: `AUTHORITATIVE`
>
> Updated: 2026-08-03
>
> Current progress source: `docs/project_status.md`
>
> Stage naming authority: `docs/adr/0005-stage-numbering-and-document-lifecycle.md`

## 1. How to read this repository

Use the following precedence when documents disagree:

1. accepted ADRs and current immutable runtime evidence;
2. `docs/project_status.md`;
3. the formal execution plan and active stage execution documents;
4. the long-lived architecture and technical implementation documents;
5. dated progress reports, handoffs, incident analyses, and historical acceptance snapshots.

Passing tests proves the implemented contract, not automatically the product-quality objective.
Stage acceptance requires the stage-specific gate and evidence named in the current status page.

## 2. Canonical stage names

| Stage | Canonical name | Status on 2026-07-31 |
|---|---|---|
| Stage 0 | Engineering Foundation | `COMPLETE / PASS` |
| Stage 1A | Memory Read Kernel | `ENGINEERING_COMPLETE` |
| Stage 1B | Memory Write Kernel | `ENGINEERING_COMPLETE` |
| Stage 2A | Memory Agent Harness and Real-Project Validation | `DEVELOPMENT_COMPLETE / CONDITIONAL_PASS` |
| Stage 2R | Stage 2A real hybrid retrieval workstream | `COMPLETE` |
| Stage 2W | Stage 2A memory write and repair workstream | `COMPLETE` |
| Stage 2M | Stage 2A writer-facing memory benchmark workstream | `ACTIVE / M4_REPAIR` |
| Stage 3 | Writer Core and Generation Quality | `PREPARATION_ACTIVE` |
| Stage 4 | Advanced Agentic Retrieval and Risk Paths | `PLANNED` |
| Stage 5 | Full Chapter and Volume Creation Loop | `PLANNED` |
| Stage 6 | Long-Horizon Autonomous Operation | `PLANNED` |
| Stage 7 | Controlled Experience/Skill Evolution and Production Expansion | `PLANNED` |

Stage 3 is the canonical replacement for the former “Stage 2B”. The former Stage 3 through
Stage 6 are now Stage 4 through Stage 7. `Stage 2R/2W/2M` remain Stage 2A workstream codes.

## 3. Long-lived authoritative documents

| Document | Lifecycle | Responsibility |
|---|---|---|
| `长篇小说Agent总体架构设计_v2.2_完整合并版.md` | `AUTHORITATIVE` | Domain ownership, invariants, Agent/Service boundaries, controlled commit, Writer-oriented memory principles |
| `长篇小说Agent技术实施与选型设计_v0.1.md` | `AUTHORITATIVE` | Ports/adapters, runtime, storage, retrieval, model, testing, and replaceable technical choices |
| `长篇小说Agent正式开发执行规划_v0.1.md` | `AUTHORITATIVE` | Canonical project stages, gates, and delivery order; path retained for compatibility |
| `docs/README.md` | `AUTHORITATIVE` | Documentation taxonomy and navigation |
| `docs/project_status.md` | `AUTHORITATIVE` | Current progress, blockers, and next permitted transition |

The architecture document does not freeze models, thresholds, exact budgets, or deployment
topology. The technical document's `Phase` labels are conceptual implementation phases, not the
canonical project `Stage` numbering.

## 4. Active execution documents

| Document | Lifecycle | Current use |
|---|---|---|
| `docs/stage2_memory_benchmark_task_closure_execution.md` | `ACTIVE` | Stage 2M contract; current exact paragraph/window slicing, token-bounded semantic-input/Ledger packing, and on-demand claim-closure repair |
| `docs/stage2m_support_closure_campaign_20260802.md` | `ACTIVE` | 2026-08-02 support-closure campaign execution log: quality closure + real C60 ×3 (A0/A1/A2, producer v25→v27) + first-loss repairs; ended `CAMPAIGN_HOLD / C60_BUDGET_EXHAUSTED` (mechanism gain confirmed, G06/G09 complete claims not formed) |
| `docs/stage3_writer_core_overall_design.md` | `ACTIVE` | Stage 3 goals, scope, parallel boundaries, and completion criteria |
| `docs/stage3_writer_core_preparation_assessment.md` | `ACTIVE` | Current technical readiness and resource envelope for Stage 3 |
| `docs/stage3_writing_core_migration_execution.md` | `ACTIVE` | Workstream B Writing Core migration and implementation |
| `docs/stage3_editor_reconciliation_execution.md` | `ACTIVE` | Workstream C Editor, repair routing, and change reconciliation |
| `docs/stage3_generation_evaluation_development_execution.md` | `ACTIVE` | Workstream D generation evaluation tool development |
| `docs/stage3_context_handoff_integration_execution.md` | `ACTIVE` | Workstream A deferred WriterContext handoff and minimal integration |
| `docs/stage3_acceptance_test_execution.md` | `ACTIVE` | Independent Stage 3 engineering and semantic acceptance |

## 5. Accepted decisions, baselines, and gate evidence

| Document | Lifecycle | Result |
|---|---|---|
| `docs/stage0_acceptance.md` | `ACCEPTED` | Stage 0 PASS |
| `docs/adr/0001-stage1-memory-kernel-baseline.md` | `ACCEPTED` | Stage 1 deterministic kernel baseline |
| `docs/adr/0003-freeze-deterministic-memory-gateway.md` | `ACCEPTED` | Stage 2A conditional pass; deterministic gateway frozen |
| `docs/adr/0004-stage2m-writer-context-product.md` | `ACCEPTED` | WriterContextPackage is the Memory read-side product |
| `docs/adr/0005-stage-numbering-and-document-lifecycle.md` | `ACCEPTED` | Canonical numbering and document lifecycle |
| `docs/retrieval_model_runtime.md` | `ACCEPTED_RUNTIME_BASELINE` | Locked Stage 1 retrieval-model revisions and runtime semantics |
| `docs/stage2_memory_gate_c95_acceptance.md` | `ACCEPTED` | C95 infrastructure and safety gate |

`docs/adr/0002-stage2-memory-controller-promotion.md` is `SUPERSEDED` by ADR-0003.

## 6. Stage 2 implementation baselines retained for audit

These documents describe how the completed Stage 2A workstreams were built. They remain useful for
maintenance and regression analysis but do not define current progress:

| Document | Lifecycle |
|---|---|
| `docs/stage2_memory_agents_development.md` | `HISTORICAL_EXECUTION_BASELINE` |
| `docs/stage2_hybrid_retrieval_execution.md` | `HISTORICAL_EXECUTION_BASELINE` |
| `docs/stage2_memory_write_workflow_execution.md` | `HISTORICAL_EXECUTION_BASELINE` |
| `docs/stage2w_pre_candidate_repair_supplement.md` | `HISTORICAL_EXECUTION_BASELINE` |
| `docs/stage2r_stage2w_controller_curator_quality_repair_execution.md` | `HISTORICAL_EXECUTION_BASELINE` |
| `docs/stage2_teacher_forced_real_model_handoff.md` | `HISTORICAL_RUNBOOK` |

## 7. Historical reports and diagnostics

| Document | Lifecycle | Notes |
|---|---|---|
| `docs/current_progress_architecture_technical_report_20260727.md` | `HISTORICAL` | C20-era progress snapshot |
| `docs/stage1_acceptance.md` | `HISTORICAL_ACCEPTANCE_SNAPSHOT` | Superseded as current progress by Stage 2A evidence |
| `docs/stage1_gap_audit.md` | `HISTORICAL` | Stage 1 gap inventory |
| `docs/stage2m_task_closure_result_20260730.md` | `HISTORICAL` | WP7 result before the 2026-07-31 diagnostic WP8 run |
| `docs/stage2m_gate_m4_root_cause_and_remediation_20260730.md` | `HISTORICAL_DIAGNOSTIC` | M4 failure diagnosis; still useful for failure localization |
| `docs/stage3_writer_core_preparation_execution.md` | `SUPERSEDED` | Detailed legacy preparation design; replaced by the restrained Stage 3 overall design |
| `长篇小说Agent技术与执行评审建议_v0.1.md` | `HISTORICAL_REVIEW` | Initial review whose accepted decisions were incorporated into the execution plan |

## 8. Draft review inputs

| Document | Lifecycle | Intended use |
|---|---|---|
| `docs/agentmemory_reference_and_memory_maturity_roadmap_20260801.md` | `DRAFT` | Code-level and local protocol-v1 comparison with `rohitg00/agentmemory`; current deferral boundary, future Stage 4–7 placement, and candidate benchmark/test gates |
| `docs/stage2m_canary40_execution_20260802.md` | `CURRENT_RESULT / DIAGNOSTIC` | Real VAC C40/C60/C95 canary comparison and Gate HOLD evidence |
| `docs/stage2m_phase4_c60_c95_trace_20260802.md` | `DIAGNOSTIC / HOLD` | Artifact-backed C60/C95 per-Gold loss localization and version boundary |

Historical result numbers remain immutable. Corrections are added as a newer report or current
status entry, not by rewriting old evidence.

## 9. Naming and file rules

- New stage-facing files use `stage3`, `stage4`, and later canonical numbers.
- Do not create new `stage2b` paths.
- Existing immutable report paths, Git branch names, worktree names, and published artifact media
  types may retain `stage2b` as a legacy identifier.
- A legacy identifier must be labelled as such when cited in a current document.
- New execution plans include lifecycle, updated date, stage, current gate, and successor.
- Dated reports never serve as the current status page.
- Private benchmark content stays under ignored `benchmarks/private/` and is never copied into
  documentation.

## 10. Updating progress

Document ownership in the Codex–OpenCode implementation loop:

- Codex creates or edits architecture, design, planning, active execution, current-status, ADR,
  `.agent/task.md`, `.agent/plan.md`, and `.agent/review.md` files.
- `.agent/plan.md` supplements the applicable upper-level documents for one implementation task; it
  does not replace or duplicate them.
- OpenCode reads those documents and writes code, tests, runtime artifacts, and
  `.agent/implementation.md`. It writes a separate result/summary document only when the Codex plan
  explicitly names one.
- After review, Codex integrates accepted evidence into existing project documents. A new design,
  remediation, or execution document is created only for a materially new decision that the current
  hierarchy cannot represent.

When a gate or work package changes:

1. update `docs/project_status.md`;
2. append evidence to the relevant active execution plan;
3. create or supersede an ADR if a stage boundary, default path, or invariant changes;
4. create a dated result document for immutable metrics;
5. update this index only if document lifecycle or navigation changes.
