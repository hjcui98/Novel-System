# Stage 3/4/5 Integration and Bounded Concurrency Plan

- Lifecycle: `ACTIVE_TASK_SUPPLEMENT`
- State: `IMPLEMENTED_INTEGRATION_CANDIDATE / PRODUCT_GATES_PENDING`
- Updated: `2026-08-12 +08:00`
- Scope: Stage 3 Writer + Stage 4 Planner + Stage 5 Runtime only
- Frozen dependency: Stage 2 main identity `408a46f`

## 1. Governing documents

The implementation follows, in precedence order:

1. `docs/adr/0006-three-product-stage-topology.md` and
   `docs/adr/0007-event-derived-agent-context-view.md`;
2. `docs/adr/0008-evidence-first-writer-context-product.md` for the frozen Stage 2 handoff;
3. `docs/stage3_writer_core_overall_design.md` and
   `docs/stage3_writer_context_loop_execution.md`, especially §2.5, §7, §10 and §11;
4. `docs/stage4_planner_core_overall_design.md` and
   `docs/stage4_planner_context_loop_execution.md`, especially §2.4, §5, §11 and §12;
5. `docs/stage5_long_running_creative_runtime_overall_design.md`, especially §4 and §8;
6. `docs/stage5_long_running_creative_runtime_execution.md`, especially B0–B5, §11 and §12.2.1;
7. overall architecture, technical implementation §28.8, and formal execution plan §6.2–§6.5.

This plan supplements those documents. It does not redefine their contracts.

## 2. Objective and starting evidence

Produce one isolated integration candidate from:

- Stage 2 frozen main: `408a46f`;
- Stage 3 Writer Context Loop: `bab4451`;
- Stage 4 Planner Context Loop: `0dcf17a`;
- the current uncommitted Stage 5 A-layer implementation in
  `.worktrees/stage5-long-running-runtime`.

Stage 3 reports 1893 deterministic tests/100% coverage; Stage 4 reports 1684 deterministic tests,
five native infrastructure tests and 100% coverage; Stage 5 A reports 1969 deterministic tests and
100% coverage. These are source evidence, not proof that the merged tree works.

The integrated result must execute this public path:

```text
real Stage 4 PlanningLoop
→ PLAN_CANDIDATE_READY
→ explicit/policy acceptance
→ trusted Plan materialize/validate/CAS Commit
→ Projection/Freshness exact
→ frozen Stage 2 Writer Memory handoff
→ real Stage 3 WritingLoop
→ DRAFT_CANDIDATE_READY
→ explicit/policy acceptance
→ trusted Draft/observed-change materialize/validate/CAS Commit
→ Projection/Freshness exact
→ WRITE_NEXT / REPLAN / WAIT / COMPLETE
```

## 3. Architectural judgments

### 3.1 One shared Context owner

Keep a single `AgentContextView`/`ContextDelta`/compaction implementation. Reconcile the Stage 4
consumer extension into the Stage 3 shared owner; do not retain duplicate projectors, enums, schema
models, stores, or exports. Full replay remains the correctness oracle.

### 3.2 Leaf adapters, not copied internals

Stage 5 invokes the real public Stage 3/4 services through narrow adapters. It does not regenerate
Writer/Planner MemoryNeed, inspect internal prompts, skip Reviewer/Editor/Observer, or translate
typed terminals into weaker dictionaries.

### 3.3 Bounded two-lane concurrency

Extend the existing in-process `CreativeDispatcher`; do not add a scheduler service or general DAG.
At most two dependency-independent tasks may execute concurrently:

- foreground: current Writer/Editor/repair or another critical candidate leaf;
- lookahead/background: basis-bound Planner lookahead, read-only/derived maintenance, prefetch, or
  non-blocking evaluation.

Reuse `PLAN_CANDIDATE` with a typed `LOOKAHEAD` purpose. It binds the exact pre-Writer basis, target
horizon and protected current chapter. It produces a candidate only. After the current Draft Commit
and exact Freshness, Stage 5 revalidates affected scope: promote unchanged candidates to the normal
acceptance stop; send affected candidates through Stage 4 revision/replan; supersede stale/unsafe
candidates while retaining lineage.

Candidate computation may overlap. Acceptance, materialization, Commit, Freshness, recovery and all
same-project Canon-changing work stay serialized. Same-book Writer N+1 cannot start before N exact
Freshness.

All model calls share the existing endpoint-global request-count + KV-token admission controller.
Runtime parallelism 2 does not imply endpoint concurrency 2; long requests may legally queue behind
capacity 1. Concurrency changes time only, never context, evidence, prompt, sampling, budget or
failure semantics.

### 3.4 Historical maintenance during Writer

Derived rebuild/cache/index work over the accepted pre-Writer basis may finish concurrently.
Semantic maintenance may only create a candidate; it waits for a foreground safe boundary and then
passes Guardian/validation/single-writer Commit. No background write may change the active Writer
basis.

## 4. Stage 2 protected boundary

Do not change the behavior or contracts owned by:

- `MemoryGateway` and deterministic default/fallback policy;
- evidence-first checkpoint runner, assembler, `WriterContextPackageV2` and `EvidenceLedgerV2`;
- Task/Plan-conditioned Writer Need generation and exact evidence selection/packing;
- accepted Stage 2 World/KG/R1/L1/L2 projection semantics and benchmark/Gate reporters;
- frozen Stage 2 schemas, fixtures, prompts, benchmark inputs or runtime artifacts.

Shared infrastructure files such as `domain/runtime.py`, `services/commits.py`, event log, model
endpoint adapters and PostgreSQL models may receive Stage 3/4/5 additive extensions only. Existing
Stage 2 tests and serialized contracts must remain unchanged in behavior. Resolve integration
conflicts downstream through adapters before considering a shared contract edit.

## 5. Implementation sequence

1. Preserve the current Stage 5 A-layer source and exclude local `objects/`/`benchmarks/` data.
2. Integrate final Stage 2 identity, Stage 3 and Stage 4 in a controlled order; resolve shared
   Context/export/schema conflicts to one implementation and regenerate only downstream schemas.
3. Replace Stage 5 strict fake Planner production assembly with the real Stage 4 adapter while
   retaining fakes only for tests/fault injection.
4. Converge the real Stage 3 adapter and complete the trusted Plan/Draft materializer chain.
5. Add typed lookahead purpose/basis/revalidation outcome without a new task family or truth store.
6. Add stable ready-batch query and a single-process dispatcher with configurable parallelism 1/2.
   Claim/fence tasks independently before concurrent execution; sibling failure must not cancel an
   already independent Attempt.
7. Add runtime/report telemetry for task overlap, model-capacity waiting, revalidation and final
   released Attempt/model leases.
8. Run the focused acceptance set and repair only within this direction.

## 6. Acceptance evidence

Development uses one minimal smoke check before integration and one concentrated deterministic
acceptance run after integration. Do not use source/hash verification and do not repeatedly rerun
isolated suites after each small edit. The final concentrated run must prove the complete
composition:

1. contract/schema tests for shared Context, Planning/Writing leaf mappings and Stage 5 runtime;
2. unit tests for lookahead identity, affected-scope promotion/replan/supersede and project/run/basis
   mismatch;
3. dispatcher tests proving actual max in-flight >= 2 for independent fake leaves, serial behavior
   for Commit/Freshness tasks, no duplicate claim/Commit and no sibling cancellation;
4. serial/parallel parity over the same deterministic fixture and model-request descriptors;
5. one offline full-chain integration through real Stage 4 service, Plan acceptance/Commit/Freshness,
   real Stage 3 service, Draft acceptance/Commit/Freshness and terminal/next decision;
6. existing Stage 2 focused regressions for Memory Gateway, evidence-first assembler/checkpoint and
   model admission, with no expected-output update;
7. Ruff/format, strict MyPy for changed production files, schema golden checks and `git diff --check`.

If the final integration changes enough shared code that this evidence cannot establish safety, run
one deterministic repository selector before declaring the integration stable. Formal real-model
Stage 3 three-scheme, Stage 4 seven-mode and Stage 5 multi-chapter Gates remain separately pending.

## 7. Endpoint 8002 policy

Use only non-mutating process/health/usage inspection. If Stage 2 benchmark activity, an in-flight
request, GPU load ownership, or endpoint availability is ambiguous, do not send a request. Never
restart or reconfigure the server. Record real API verification as `DEFERRED_ENDPOINT_BUSY` and
finish deterministic/offline evidence. If clearly idle, allow at most one short smoke request; do
not run concurrency calibration in this task.

## 8. Return to design review

Stop implementation and return to the user before:

- modifying Stage 2 product semantics or benchmark inputs;
- permitting concurrent same-project Canon writes or Writer N/N+1 overlap;
- adding a new scheduler service, queue, DAG DSL, context store, Memory Gateway or Planner path;
- weakening accepted candidate/review/validation/CAS/freshness boundaries;
- introducing multi-worker lease/reclaim, Hook, Temporal or Skill evolution;
- silently translating an incompatible Stage 3/4 terminal or basis contract;
- using 8002 when ownership/capacity is not clearly available.

## 9. Reporting

Append final-tree evidence to `.agent/implementation.md`: integrated source identities, conflict
decisions, changed owners, tests/commands, serial/parallel descriptors, offline full-chain artifacts,
Stage 2 protected-boundary audit, endpoint decision and remaining formal Gates. Do not claim
`CREATIVE_RUNTIME_PRODUCT_PASS` from the focused tests alone.

## 10. 2026-08-12 implementation result

The integration candidate now contains the frozen Stage 2 identity, Stage 3 `bab4451`, Stage 4
`0dcf17a`, the Stage 5 kernel, `Stage4PlanningLeafAdapter`, bounded dispatcher parallelism 1/2 and
post-Draft lookahead promotion/replan/supersede. Stage 2 versioned schemas and product owners remain
frozen; only default-inactive shared Planner extensions were added.

Evidence followed the user's minimum-test direction: one 220-check concentrated run, then only the
9 exact failing selectors after contract-alignment repair; all 9 passed. Changed files pass Ruff and
11 changed production modules pass strict MyPy. No source-hash verification or repeated suite rerun
was used. 8002 remained unprobed because ownership could not be established.

The next implementation reference is this plan §10 plus Stage 5 execution B4/B5 and §12.2.1. The next
code owner is the existing trusted Commit/materialization boundary: finish real PlanRoot/TextRoot
materializers, then run one final real multi-chapter Stage 3/4/5 Gate. Do not expand concurrency or
add a scheduler platform first.
