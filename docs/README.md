# Project documentation index

> Lifecycle: `AUTHORITATIVE`
>
> Updated: 2026-08-18
>
> Current progress source: `docs/project_status.md`
>
> Stage naming authority: `docs/adr/0006-three-product-stage-topology.md`
>
> Target Runtime authority: `docs/adr/0010-temporal-outer-langgraph-leaf-target-runtime.md`

## 1. How to read this repository

Use the following precedence when documents disagree:

1. accepted ADRs and current immutable runtime evidence;
2. `docs/project_status.md`;
3. the formal execution plan and active stage execution documents;
4. the long-lived architecture and technical implementation documents;
5. dated progress reports, handoffs, incident analyses, and historical acceptance snapshots.

Passing tests proves the implemented contract, not automatically the product-quality objective.
Stage acceptance requires the stage-specific gate and evidence named in the current status page.

All current architecture, technical, execution, and task documents inherit the repository's
**minimum-sufficient engineering** principle: use the smallest mechanism that satisfies a proven
current requirement and all applicable invariants; reuse the existing owner and document hierarchy;
do not create a second truth source, parallel pipeline, speculative platform, or duplicate document
for possible future use. This constrains implementation complexity, not correctness evidence:
typing, validation, security, leakage controls, failure semantics, required observability, tests,
reproducibility, and active Gates remain mandatory.

## 2. Canonical stage names

| Stage | Canonical name | Current status |
|---|---|---|
| Stage 0 | Engineering Foundation | `COMPLETE / PASS` |
| Stage 1A | Memory Read Kernel | `ENGINEERING_COMPLETE` |
| Stage 1B | Memory Write Kernel | `ENGINEERING_COMPLETE` |
| Stage 2A | Memory Agent Harness and Real-Project Validation | `DEVELOPMENT_COMPLETE / CONDITIONAL_PASS` |
| Stage 2R | Stage 2A real hybrid retrieval workstream | `COMPLETE` |
| Stage 2W | Stage 2A memory write and repair workstream | `COMPLETE` |
| Stage 2M | Stage 2A writer-facing memory benchmark workstream | `ENGINEERING_CLOSED / SOURCE_COMMITTED_AT_0bc7757 / REAL_TESTED_USABLE_WITH_EXPLICIT_GAPS / DETACHED_REF_AND_INTEGRATION_PENDING` |
| Stage 3 | Writer Agent and Writing Context Loop | `ENGINEERING_INTEGRATED / TRUST_REPAIRS_AND_REAL_SEMANTIC_GATE_PENDING` |
| Stage 4 | Planner Agent and Planning Context Loop | `ENGINEERING_INTEGRATED / RETRIEVAL_MATURITY_AND_REAL_SEMANTIC_GATE_PENDING` |
| Stage 5 | Long-running Creative Runtime | `ENGINEERING_INTEGRATED / PRODUCTION_ASSEMBLY_AND_REAL_PILOT_PENDING` |

ADR-0006 supersedes ADR-0005 only for Stage 3 and later: Writer and Planner are independent Stage 3/4
products over the shared Stage 2 Memory foundation, and Stage 5 integrates them into long-running
operation. Historical Stage 4–7 names remain valid provenance only. `Stage 2R/2W/2M` remain Stage 2A
workstream codes. ADR-0010 supersedes only ADR-0006's prior non-adoption stance for the Stage 5
technical Runtime: Temporal outer + LangGraph leaf is the long-term target, while the current
PostgreSQL Runtime remains the migration production baseline.

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
| `docs/stage2_memory_benchmark_task_closure_execution.md` | `HISTORICAL_EXECUTION_BASELINE` | Legacy benchmark task closure; current evidence-first product semantics are ADR-0008/0009 |
| `docs/stage2_memory_architecture_repair_execution_20260811.md` | `ACCEPTED` | Accepted mechanical architecture-repair baseline and historical §29 evidence; current semantic repair direction is the 2026-08-13 document |
| `docs/stage2_memory_semantic_repair_execution_20260813.md` | `ACCEPTED_REPAIR_BASELINE` | 2026-08-13 semantic repair analysis; implemented latest Stage 2 direction is ADR-0009 and no longer the next action |
| `docs/stage3_writer_core_overall_design.md` | `ACTIVE` | Writer product, plan-conditioned Memory, dynamic Context View, Skills and candidate Gate |
| `docs/stage3_writer_context_loop_execution.md` | `ACTIVE` | Implemented Stage 3 Writer/Editor/Observer/Context loop; current accepted-identity convergence and real infrastructure/model Gates remain |
| `docs/stage4_planner_core_overall_design.md` | `ACTIVE` | Planner product, inquiry-conditioned Memory, independent Plan Review and candidate Gate |
| `docs/stage4_planner_context_loop_execution.md` | `ACTIVE` | Active Stage 4 implementation path: inquiry-conditioned Memory, Planner Context, Reviewer, conditional Graph adoption, then final unified testing |
| `docs/stage5_long_running_creative_runtime_overall_design.md` | `ACTIVE` | Fixed Plan/Write/Accept/Commit topology, Task/Attempt, recovery, maintenance, controlled evolution and admission boundaries |
| `docs/stage5_long_running_creative_runtime_execution.md` | `ACTIVE` | Detailed Stage 5 implementation path: isolated Runtime Kernel now, real Stage 4 integration after its Gate, evidence-triggered operations/evolution later |
| `docs/stage2_to_stage5_unified_long_running_agent_integration_execution_20260818.md` | `ACTIVE` | Current cross-Stage authority: branch/worktree convergence, production assembly, U4-L0 model-budget/progressive-context closure, V0.5 real-Writer evidence, Stage 3/4/5 leaf Gates, continuous/real long runs, early Temporal feasibility, target-Runtime migration/cutover and bounded self-correction |
| `docs/stage2_model_budget_runtime_policy.md` | `PROPOSED` | U4-L0 input: unique effective budget, Controller C1+C2, Memory Planner P1; not yet a production default. The worktree copy is retired by this root path. |
| `docs/novelmem_v0.5_plan_write_extension_design.md` | `ACTIVE_CANARY_EXECUTION` | V0.5 Track B wiring → C-ROLL → D-SHORT canary; defines Track B two-layer product, Track C/D contracts, four-condition attribution, Oracle/Gap protocol and U4-L0 variable isolation |
| `docs/novelmem_ztj_v0.5_benchmark_development_plan.md` | `ACTIVE_DEVELOPMENT_PLAN` | Concrete build plan for the ZTJ V0.5 benchmark: WP0 → WP-BASIS → WP1 → WP3 → WP2 → WP4 → WP7, with production basis, Track C run/score, unified four-condition Writer input contract, and acceptance red lines |
| `docs/stage2_to_stage5_real_novel_vertical_pilot_execution.md` | `ACTIVE_SUBPROTOCOL` | U5 real-novel C20→25 Pilot request, assembly, report and product acceptance procedure |
| `docs/Novel-System_分层规划与渐进Skill_收敛版补丁执行设计_v2_ee8849a.md` | `HISTORICAL_REVIEW_INPUT` | Folded into Stage 4/5 designs: Gate 0 cadence/BLOCKED/length, Patch A future-lock, Patch B PlanLevel hierarchy, Patch C limited progressive Skill |

## 5. Accepted decisions, baselines, and gate evidence

| Document | Lifecycle | Result |
|---|---|---|
| `docs/stage0_acceptance.md` | `ACCEPTED` | Stage 0 PASS |
| `docs/adr/0001-stage1-memory-kernel-baseline.md` | `ACCEPTED` | Stage 1 deterministic kernel baseline |
| `docs/adr/0003-freeze-deterministic-memory-gateway.md` | `ACCEPTED` | Stage 2A conditional pass; deterministic gateway frozen |
| `docs/adr/0004-stage2m-writer-context-product.md` | `ACCEPTED` | WriterContextPackage is the Memory read-side product |
| `docs/adr/0005-stage-numbering-and-document-lifecycle.md` | `ACCEPTED` | Canonical numbering and document lifecycle |
| `docs/adr/0006-three-product-stage-topology.md` | `ACCEPTED` | Stage 3 Writer, Stage 4 Planner and Stage 5 long-running topology; supersedes ADR-0005 later-stage mapping |
| `docs/adr/0007-event-derived-agent-context-view.md` | `ACCEPTED` | Shared event-derived Context View, ContextDelta and safe compaction contract |
| `docs/adr/0008-evidence-first-writer-context-product.md` | `ACCEPTED` | Evidence-first Stage 2M Writer package; Claim/Evaluator removed from the Agent default path |
| `docs/adr/0009-need-evidence-semantic-closure.md` | `ACCEPTED` | Need–evidence semantic judgement; separates assembly, semantic completeness and usable-with-gaps without restoring Claim/Gold |
| `docs/adr/0010-temporal-outer-langgraph-leaf-target-runtime.md` | `ACCEPTED` | Temporal outer + LangGraph leaf is the target Runtime; PostgreSQL remains the migration production baseline and plugin maturity does not decide Temporal adoption |
| `docs/retrieval_model_runtime.md` | `ACCEPTED_RUNTIME_BASELINE` | Locked Stage 1 retrieval-model revisions and runtime semantics |
| `docs/stage2_memory_gate_c95_acceptance.md` | `ACCEPTED` | C95 infrastructure and safety gate |

`docs/adr/0002-stage2-memory-controller-promotion.md` is `SUPERSEDED` by ADR-0003.
`docs/adr/0004-stage2m-writer-context-product.md` remains the historical claim-first baseline and is
`SUPERSEDED` by ADR-0008 for the active Stage 2M payload semantics.

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
| `docs/stage3_writer_core_preparation_assessment.md` | `HISTORICAL_ASSESSMENT` | Pre-convergence readiness snapshot; current status is in project_status and the Stage 3 design |
| `docs/stage3_writing_core_migration_execution.md` | `SUPERSEDED` | Legacy Stage 3 workstream B; replaced by the single Writer Context Loop execution design |
| `docs/stage3_editor_reconciliation_execution.md` | `SUPERSEDED` | Legacy Stage 3 workstream C; accepted concepts folded into the Writer Context Loop |
| `docs/stage3_generation_evaluation_development_execution.md` | `SUPERSEDED` | Legacy workstream D; evaluation requirements folded into the current Stage 3 design |
| `docs/stage3_context_handoff_integration_execution.md` | `SUPERSEDED` | Legacy workstream A; replaced by current WCP/Context View convergence |
| `docs/stage3_acceptance_test_execution.md` | `SUPERSEDED` | Legacy A/B/C/D acceptance plan; current Gate lives in the Stage 3 design/execution pair |
| `长篇小说Agent技术与执行评审建议_v0.1.md` | `HISTORICAL_REVIEW` | Initial review whose accepted decisions were incorporated into the execution plan |
| `docs/current_implementation_architecture_review_20260816.md` | `CURRENT_REVIEW_INPUT / NON_AUTHORITATIVE` | Code-based review at `6a195e0`; adopted/current findings are resolved in the 2026-08-18 unified execution plan |

## 8. Technical reference inputs

| Document | Lifecycle | Intended use |
|---|---|---|
| `docs/inkos_longform_agent_technical_reference_20260809.md` | `TECHNICAL_REFERENCE / NON_AUTHORITATIVE` | Fixed-commit InkOS evidence for rolling planning, protected/compressible context, bounded revision, chapter transaction and recovery |
| `docs/agentmemory_reference_and_memory_maturity_roadmap_20260801.md` | `TECHNICAL_REFERENCE / NON_AUTHORITATIVE` | agentmemory source/test comparison for conditional BM25+Dense+Graph, compact→expand, provenance and external Hook ingress |
| `docs/long_running_agent_runtime_source_reference_20260810.md` | `TECHNICAL_REFERENCE / NON_AUTHORITATIVE` | Hermes/OpenClaw/OpenHands/PydanticAI source audit for Context View, compaction, Task/Attempt, fencing and long-running Runtime |
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

Document ownership in the automated Codex–DSH loop (the loop contract lives in `AGENTS.md`):

- Codex creates or edits architecture, design, planning, active execution, current-status, ADR,
  `.agent/task.md`, `.agent/plan.md`, `.agent/reviews/`, and `.agent/handover.md` files.
- `.agent/plan.md` supplements the applicable upper-level documents for one implementation task; it
  does not replace or duplicate them, and every update bumps its version header.
- The DSH execution engine reads those documents and writes code, tests, runtime artifacts,
  `.agent/implementation.md`, and `.agent/loop.md`. It writes a separate result/summary document
  only when the Codex plan explicitly names one.
- After review, Codex integrates accepted evidence into existing project documents. A new design,
  remediation, or execution document is created only for a materially new decision that the current
  hierarchy cannot represent.
- Do not create a separate “simplicity”, “governance”, or “platform” document for a rule that fits an
  existing authority. Add the minimum local clarification and link upward instead of copying a new
  documentation system.

When a gate or work package changes:

1. update `docs/project_status.md`;
2. append evidence to the relevant active execution plan;
3. create or supersede an ADR if a stage boundary, default path, or invariant changes;
4. create a dated result document for immutable metrics;
5. update this index only if document lifecycle or navigation changes.

## 11. Development workflow reference

| Document | Lifecycle | Notes |
|---|---|---|
| `docs/codex_dsh_automated_loop_reference.md` | `AUTHORITATIVE` | Full overview of the automated Codex–DSH loop: planes, document library, Codex session contract, verdicts, gates, and operating manual; the operative contract lives in `AGENTS.md` |
