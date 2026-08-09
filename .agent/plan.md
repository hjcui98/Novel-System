# Stage 2M Plan-Conditioned 语义闭环与全局调度修复执行计划

- Lifecycle: `ACTIVE_TASK_SUPPLEMENT`
- State: `READY_FOR_IMPLEMENTATION`
- Updated: `2026-08-08 +08:00`
- Stage: `Stage 2M`
- Current gate: `Gate 0-3 implementation evidence invalidated by audit / M4 HOLD`
- Git baseline: `420e163` plus the current dirty Stage 2M worktree
- Working tree: preserve all current user changes, frozen inputs, implementation evidence, and
  Phase 4 artifacts; do not reset, discard, overwrite, or silently reclassify them
- Architect and final reviewer: Codex
- Implementation, tests, real API execution, monitoring, artifact analysis, and in-direction repair:
  OpenCode default `build`
- Commit and merge owner: Codex

## 0. Authority, purpose, and precedence

This file is the task-local implementation handoff. It does not replace the repository document
hierarchy or create a third architecture. OpenCode must read the following authorities before
implementation and preserve their precedence:

1. `AGENTS.md` and `docs/README.md` for ownership and document lifecycle;
2. `docs/project_status.md` for the current Stage 2M `HOLD` boundary;
3. `.agent/need_pipeline_audit_and_semantics.md`, especially:
   - §8.1 `AuthorPlanningContext` narrow channel;
   - §8.2 / D9 / O9 layered Plan policy;
   - Phase 1 Planner → Grounder → Validator and O8 lineage;
   - Phase 2 query-bundle/route intersection and O17;
   - Phase 3 five-segment semantics and `gold_need_spec`;
   - §9 Gate 0-3, §10 constraints, and O15 single source of truth;
4. `.agent/concurrent_scheduling_plan.md`, especially:
   - §1 scheduling-only semantics;
   - §4 context integrity;
   - §5 request-count + KV admission;
   - §8 shared-state ownership and in-flight deduplication;
   - §9 typed waiting/timeout semantics;
   - §10 endpoint-global budget;
   - §12 correctness acceptance;
5. `docs/stage2_memory_benchmark_task_closure_execution.md` for the public/private boundary,
   WriterContext ownership, applicable-profile evaluation, artifact identity, budgets, and Stage 2M
   Gate rules;
6. `.agent/implementation.md` §§16-18 for the current implementation claims and the partial
   four-checkpoint Phase 4 experiment.

The implementation audit found that several completed claims in `.agent/implementation.md` do not
match the governing semantics. This plan corrects the implementation; it does not amend the upper
design to fit the current code.

## 1. Objective and completion definition

Deliver one coherent repair of the Plan-Conditioned Need pipeline and the model-request scheduler so
that:

1. APC has one hash-bound planning truth source and obeys the fixed D9 policy;
2. Planner drafts retain their coherent multi-facet semantics through completion evaluation;
3. only executable per-channel queries are routed, with no broad exact retrieval from an ungrounded
   Need;
4. Planner lineage and health diagnostics are durable, replayable, and visible in Gate 1;
5. the five-segment report measures the five concepts named by the design, with per-profile and
   per-Gold attribution rather than global unions;
6. every model request sharing the endpoint is governed by one request-count + effective-KV budget,
   typed queue timeout, exception-safe release, and single-flight verification cache;
7. serial and concurrent execution have identical semantic request inputs and deterministic
   persisted ordering;
8. old, semantically invalid Phase 4 output remains immutable diagnostic evidence, while a fresh
   final-code experiment receives a new identity and formula version.
9. the repair uses the minimum sufficient mechanism at each demonstrated responsibility boundary and
   does not grow into a parallel platform or speculative Stage 3/4 capability.

Completion requires implementation, license-free regressions, full repository quality, focused real
API evidence, fresh final-code APC/TASK_INTENT_ONLY evidence when admitted, monitoring, artifact
analysis, and `.agent/implementation.md` reporting. Passing tests alone is not completion.

## 2. Starting evidence and root-cause ownership

The following are accepted starting facts for this task. Reproduce them with focused tests where
useful, but do not spend a new implementation cycle rediscovering them.

### 2.1 Evaluation implementation does not preserve metric meaning

- `MemoryBenchmarkEvaluator.evaluate_five_segments()` counts a goal from a Need's self-declared
  chapter, forms global unions of all Need scopes/entities/facets, and matches Evidence Recall against
  the full final Ledger.
- E2E computes `applicable_gold` but passes all `gold_items` and all `gold_need_specs` into the
  five-segment calculation.
- `GoldBlindness` is compiled but unused in profile denominators.
- absent `gold_need_spec` becomes Need Recall `1.0` instead of unavailable.
- the present unit test encodes the cross-Need global-union behavior.

Responsible layers: evaluator contracts, E2E evaluator binding, report schema, and adversarial tests.

### 2.2 Planner Gate 1 diagnostics are dropped

- `TaskPlanConditionedNeedGenerator.generate_with_lineage()` returns planner metadata, fallback, and
  grounding counts.
- `Stage2PairedPilotRunner.resolve_state_case()` retains only `generation.needs`.
- `PairedContextComparison` defaults then report no fallback and perfect grounding when the actual
  fields are absent.

Responsible layers: paired-run orchestration, frozen comparison contract, evaluator binding, and
artifact persistence.

### 2.3 Planner semantics are compressed after validation

- `required_claim_scopes` is not consumed after Grounder.
- `suggested_facets` selects the first matching `need_type`; completion facets are then rebuilt from
  that one type.
- an ambiguous-only entity draft is accepted although it emits no canonical entity ID.
- future-factualization validation does not use the supplied World/Plan and only rejects exact text
  equality.

Responsible layers: Planner draft contract, Validator, Need construction, completion contract, and
query eligibility.

### 2.4 Query-bundle/route intersection is documented but not executed

- `RetrievalQueryBundle` contains channel data but not an authoritative executable-channel result.
- `RetrievalOrchestrator` executes every route/pool channel even when exact filters or graph seeds are
  absent.
- R1 exact retrieval can therefore become a broad latest-record query for an unanchored factual Need.

Responsible layers: query compiler, route orchestration, R1 defensive boundary, and retrieval trace.

### 2.5 APC single-source binding is incomplete

- Import validates context ref/hash/profile but not case/context task intent, target range, or
  context/PlanRoot content agreement.
- `derive_gate_subset()` narrows the case and PlanRoot but retains the original planning context.
- A focused diagnostic imported the derived bundle successfully while all five case/context ranges
  disagreed, for example case `(21, 22)` versus context `(21, 25)`.
- TASK_INTENT_ONLY currently relies on the caller passing no PlanRoot; the public boundary rejects a
  PlanRoot only for VISIBLE_AT_CUTOFF.

Responsible layers: compiler, derived-bundle compiler, importer, public task builder, and domain
validators.

### 2.6 Planner artifact references are not durable

- Needs store `content_id(PlannerArtifactMetadata)`, but the metadata and its prompt/world/raw/
  validated inputs are not written as a dereferenceable artifact.
- production reruns cannot load a frozen Planner artifact and verify its context/world basis.

Responsible layers: artifact contract, Planner service, generator orchestration, freeze receipt, and
rerun entry point.

### 2.7 Admission is neither strict nor endpoint-global

- oversized requests bypass the endpoint request limit; a focused diagnostic reached two in-flight
  requests with `endpoint_request_limit=1`.
- `kv_safety_reserve_ratio` is reported but does not reduce usable capacity.
- capacity waiting has no scheduling timeout and uses polling.
- the controller is conditionally constructed and only reaches Claim Support; Planner, replay
  agents, and Evaluator batches can bypass it while sharing the endpoint.

Responsible layers: model-call scheduling boundary, runner wiring, gateway, and CLI configuration.

### 2.8 Concurrent shared-state and failure semantics are incomplete

- whole-claim verification can return on missing raw output without releasing capacity.
- the verification cache lock protects only completed get/set, not in-flight duplicate work.
- concurrent workers write artifacts and shared gateway collections directly.
- checkpoint workers mutate builder/freezer shared state in completion order, so persisted receipt
  ordering is not guaranteed to match checkpoint order.

Responsible layers: admission lease, verification single-flight, worker result contracts, artifact
coordinator, model-call ledger, and scenario coordinator.

### 2.9 The 2026-08-08 four-checkpoint run is useful baseline evidence, not Gate evidence

The unmodified APC + `real_hybrid` run under
`/tmp/ns-stage2m-phase4-apc-20260807` completed deterministic evaluation at C20/C40/C60/C80. The
latest intended summaries, confirmed against the content-addressed case-summary objects, are:

| Case | Checkpoint | Gold evidence recall | Observed-use coverage | Operational coverage | Plan-obligation coverage | Traceability |
|---|---:|---:|---:|---:|---:|---:|
| P001 | C20 | 0.654 | 1.000 | 1.000 | 0.000 | 1.000 |
| P002 | C40 | 0.793 | 1.000 | 1.000 | 0.000 | 1.000 |
| P003 | C60 | 0.552 | 0.714 | 1.000 | 0.000 | 1.000 |
| P004 | C80 | 0.545 | 0.500 | 1.000 | 0.000 | 1.000 |

The zero leakage result is retained as a positive diagnostic safety signal. The decreasing
observed-use coverage at C60/C80 and the C60/C80 evidence-recall plateau are quality signals, but
must not be optimized directly until the evaluator binding is repaired. The same run exposes
additional evidence-contract defects:

- all four top-level results are `comparable=false` because Agentic was not run; nevertheless the
  report copies deterministic quality metrics into `agentic_metrics`, sets Agentic retrieval calls
  to zero, and derives `tool_call_reduction=true`. This is a false paired-comparison signal.
- the top-level `e2e_paired_report.json` and `flow_summary.json` are overwritten checkpoint by
  checkpoint. Six content-addressed paired summaries remain because C20 and C40 each have two
  attempts with different metrics; an unscoped object-store scan therefore sees conflicting results.
- the current C80 `flow_summary.json` reports `planner_agent_calls=0`, `validator_calls=0`,
  `scenario_run_completed=true`, and `run_complete=false`. These fields do not provide a coherent
  Gate 1 or experiment-completion statement.
- every `stage2m_case_C*_A.json` has Per-Gold weighted coverage and mandatory hit rate `0.0`, despite
  the moderate retrieval recall above. The provisional loss labels are dominated by
  `F-NEED_ROUTE_RETRIEVE` and `F-ASSEMBLY`; nine items are labelled `COMPLETE` while their final
  verdict remains `MISS`, so `COMPLETE` is not currently a trustworthy terminal-stage label.
- several observed Ledger IDs in P001/P002/P004 contain a `ZTJ-P005` source namespace. This may be
  benign content deduplication of shared cutoff-safe history, but the current lineage does not make
  that distinction self-evident. Case-local evidence must prove observed TextRoot ancestry and must
  never receive credit from another case's evaluator-only Gold/future sources.

Responsible layers: single-arm/paired report contracts, metric availability, run-attempt identity,
checkpoint artifact persistence, aggregation, lifecycle reporting, per-stage failure attribution,
and case/source provenance.

## 3. Task-specific architectural judgments

These judgments resolve implementation ambiguity without changing the governing design.

### 3.1 D9 is strict for every APC Memory Need

For `AUTHOR_PLAN_CONDITIONED`, the run-level policy is fixed:

```text
planner_may_read_plan = True
retrieval_may_return_plan = False
claim_may_cite_plan = False
allow_plan = legacy_allow_plan = False
```

No `need_type`, including `plan_obligation` or `plan_conditioned_history`, may widen retrieval or
claim authority. AuthorPlanningContext guides what historical questions the Planner asks; it is not
retrieval evidence and is not an observed factual claim source.

Observed open promises/obligations remain legal through cutoff-safe World/history evidence and may
populate the Writer `plan_and_obligations` section. Author future goals do not enter the Evidence
Ledger. If direct author-plan delivery to Writer is required, that is a materially new product
channel and must return to Codex for an upper-level design/ADR decision; it must not be implemented
by restoring the current hidden exception.

Consequences:

- Planner output with `required_claim_scopes=("planned",)` or `suggested_facets=("PLAN_NODE",)` is
  not a legal historical Memory Need and must be rejected or converted by the Planner into an
  explicit history-answerable question before validation; host code must not silently rewrite a
  future goal into an observed fact.
- any Plan-labelled retrieval unit, plan provenance, or `plan_node_ids` in APC Memory evidence is a
  leakage event, regardless of Need type.
- TASK_INTENT_ONLY and VISIBLE_AT_CUTOFF both reject PlanRoot at the public boundary and set all three
  policy fields false.

### 3.2 One Planner draft remains one coherent semantic Need

The final Need may keep a primary `need_type` as a routing/classification hint, but that field is not
the completion truth source. All validated `required_claim_scopes` and `suggested_facets` from one
draft must be mapped to typed scopes/facets and retained in that Need's `NeedCompletionSpec`.

- Multi-facet drafts remain multi-facet.
- Required scopes must agree with the expected scope of their facets.
- Unknown or contradictory combinations fail validation with a typed reason.
- AMBIGUOUS/UNRESOLVED mentions do not count as anchors and cannot authorize exact/graph retrieval.
- every accepted non-plan historical Need has at least one grounded entity or grounded relation
  endpoint and a history-answerable semantic question.

Do not split or merge drafts merely to improve Need Recall. If the current private annotation cannot
be interpreted as one coherent Need signature for a Gold item, stop and return to Codex instead of
changing frozen P004/P005 annotations.

### 3.3 Five-segment binding is per applicable Gold

For each profile, the Evaluator builds an evaluator-only binding after freeze:

```text
applicable Gold
  -> applicable GoldNeedSpec
  -> best single coherent generated Need (stable deterministic match)
  -> Ledger entries attributed to that Need
  -> completion/claim verdict
```

Rules:

1. filter Gold by `GoldItem.applicable_profiles` first;
2. join GoldNeedSpec by `gold_id` and exclude `HINDSIGHT_ONLY` from planning-health denominators;
3. VISIBLE_AT_CUTOFF and TASK_INTENT_ONLY score `BLIND_RECOVERABLE`; APC additionally scores
   `PLAN_DEPENDENT` only when it still describes a historical question, never Plan evidence;
4. select the best single Need by required component coverage, then stable `need_id`; do not combine
   components from unrelated Needs;
5. component diagnostics may show partial coverage from that selected Need, but a full Need match
   requires all required scopes, entities, and facets;
6. Evidence Recall matches Gold evidence only within Ledger entries carrying the bound Need ID;
7. missing required annotations produce an explicit unavailable/not-evaluable segment, never 1.0;
8. Plan Goal Coverage counts only a validated Planner Need whose stored lineage binds to the exact
   canonical goal/chapter and whose question passed history-answerability/grounding validation;
9. Leakage is computed over every Ledger entry, with no Plan-channel exemption.

`Pxxx-PLAN-*`/plan-only annotations belong to Plan Goal Coverage. Under strict D9, their Plan refs
are not Evidence Recall inputs and they are not Writer Claim/Completion targets. If a plan-dependent
item also requires remembered historical facts, its history-answerable portion must have an explicit
GoldNeedSpec and observed accepted-evidence contract; the plan itself remains Planner guidance.
The legacy `plan_obligation_coverage` field must be deprecated or reported as unavailable with reason
`NOT_APPLICABLE_STRICT_D9`, not interpreted as a retrieval-quality zero and not restored through a
Plan evidence exception.

The report and formula version must change. Old and new five-segment scores are non-comparable unless
old frozen artifacts are explicitly rescored under the new formula.

### 3.4 Model admission belongs at the shared endpoint boundary

Every request to the same configured endpoint—including Planner, bootstrap/replay Curator,
Claim Support proposal/verification, and Evaluator—must pass through one shared scheduler. A
per-producer optional controller cannot be the authority.

Use a typed scheduling descriptor/lease and integrate it at `ModelGateway` or an equivalent single
endpoint wrapper so a new caller cannot bypass scheduling accidentally. Remove double admission
from Claim Support after gateway ownership is active.

Usable KV capacity is the configured budget after reserve:

```text
effective_kv_budget = floor(configured_kv_budget * (1 - kv_safety_reserve_ratio))
```

A request that cannot ever fit the effective application budget is a typed unsatisfiable scheduling
failure/configuration error; it must not bypass either budget or wait forever. A request beyond the
model single-sequence limit remains `CONTEXT_BUDGET_EXCEEDED`.

### 3.5 Deterministic-only evidence is single-arm evidence, not a failed pair

Gate 0-3 semantic evaluation does not require running experimental Agentic Arm B. A completed
deterministic Arm A result may be quality-eligible as a single-arm result. It has no A/B delta.

Every arm must carry an explicit execution status (`COMPLETED`, `SKIPPED`, or `FAILED`). Metrics may
exist only for a completed arm. If Agentic is skipped:

- `agentic_metrics`, delta/gain/reduction fields, and paired comparability are unavailable;
- the report may state `paired_comparison_status=NOT_RUN`, but must not synthesize an Agentic result;
- the deterministic Gate 0-3 result remains separately readable and cannot claim Agentic benefit;
- formal Agentic A/B/C promotion remains subject to the existing Stage 2M/M4 sequencing and is not
  pulled into this repair merely to clear `agentic_not_run_deterministic_gate`.

Lifecycle status is also layered: checkpoint scenario completion, single-arm evaluation completion,
paired-comparison completion, and whole experiment/matrix completion are separate typed fields. A
generic `run_complete` boolean must not collapse them.

### 3.6 Minimum-sufficient implementation is a hard task boundary

For this assignment, “do not overengineer” means:

1. repair the demonstrated owner first; prefer delete, merge, wire, narrow, configure, or extend over
   adding a parallel abstraction;
2. reuse `AuthorPlanningContext`, existing Need/query/completion contracts, Model Gateway, artifact
   store, runner, evaluator, and document hierarchy as their respective single owners;
3. add a type only when it expresses a named identity, permission, availability, failure, scheduling,
   or audit invariant that existing contracts cannot express safely;
4. add no second Planner/retrieval/evaluation pipeline, general workflow engine, dynamic rule/ontology
   DSL, plugin platform, scheduler service, new datastore, reporting warehouse, dashboard, or full-chain
   async migration for hypothetical reuse;
5. remove deprecated compatibility paths once their named migration is complete instead of retaining
   permanent old/new semantics;
6. stop expanding once the current regressions, Gate 0-3 semantics, scheduler invariants, and required
   real evidence pass. Performance or elegance work without a named acceptance signal is deferred.

This is not permission to omit strict typing, schemas/migrations, D9 and Gold isolation, hash/profile
binding, typed failures, exception-safe release, deterministic artifacts, observability required by
the acceptance evidence, regression tests, 100% coverage, reproducibility, or real runs. Those are
part of “sufficient.” If the smallest correct solution requires a new product channel, cross-stage
platform, or a second semantic owner, return to Codex rather than building it inside this task.

## 4. Implementation work

Implement the following as one substantial assignment. Finish each semantic dependency before
turning concurrency back on; do not split this into unrelated documentation or campaign files. At
each section, implement only the contracts and mechanisms needed by its named regressions and
acceptance signals; do not generalize beyond them.

### 4.0 Preserve and quarantine current evidence

Before code changes:

1. record HEAD, dirty status, current code fingerprint, relevant service configuration, and the
   latest state of `stage2m-phase4-apc-20260807` in `.agent/implementation.md`;
2. do not delete, rewrite, resume as formal, or reuse its output directory/database identity;
3. label its five-segment/Planner-health/global-concurrency conclusions `DIAGNOSTIC_ONLY_INVALIDATED`
   because formula binding and scheduler scope are incorrect;
4. retain raw prompts, responses, receipts, progress, and transport timing as diagnostic evidence;
5. inventory the six surviving content-addressed paired summaries, identify the four intended latest
   attempts without rewriting their payloads, and record that the original C20/C40/C60 top-level
   `e2e_paired_report.json` files were overwritten rather than claiming reconstructed originals;
6. retain the four Stage 2M case reports and their zero end-to-end coverage as a distinct downstream
   baseline; do not merge paired retrieval metrics with Per-Gold Writer metrics;
7. do not modify P004/P005 frozen input, Gold, GoldNeedSpec, or `frozen_inputs.json`.

Do not run old-formula P005, reconstruct overwritten reports by rerunning the old evaluator, or
launch another Phase 4 run until §§4.1-4.6 and the deterministic/quality gates pass.

### 4.1 Close APC truth and Plan policy boundaries

1. Make `AuthorPlanningContext` the authoritative normalized object. Its source/content identity must
   include profile, task intent, target range, visible outline, and chapter goals.
2. Treat manifest/task fields as derived cache fields only. Compiler and importer must prove:
   - case/context profile equality;
   - case/context target-range equality;
   - case/task/context task-intent equality where the profile permits intent;
   - context outline/goals and referenced author-visible PlanRoot agreement;
   - planning ref and hash appear as a valid pair.
3. `derive_gate_subset()` must derive a new narrowed context, source hash, content ref, goal set, and
   manifest binding together with its narrowed PlanRoot.
4. Duplicate planning contexts are rejected by full normalized identity; identical raw text with a
   different target/profile cannot collide.
5. `PublicCheckpointCase` and its builder reject any PlanRoot for TASK_INTENT_ONLY and
   VISIBLE_AT_CUTOFF. APC accepts only the verified author-visible PlanRoot/context binding.
6. Apply the strict D9 matrix from §3.1 to all Needs, retrieval filters, controller legal actions,
   Claim Support, evidence assembly, and leakage accounting.
7. Remove tests and comments that codify a privileged plan-to-plan evidence channel. Replace them
   with observed-only APC contract tests.

### 4.2 Preserve Planner semantics and make lineage durable

1. Validate Planner output values against typed scope/facet enums rather than accepting arbitrary
   strings through to a priority mapping.
2. Use all validated scopes/facets when constructing `NeedFacet` and `NeedCompletionSpec`; keep
   `need_type` only as a derived primary classifier/routing hint.
3. Require canonical trigger-goal binding:
   - every trigger chapter is in the task target range;
   - `trigger_plan_goal` equals the normalized canonical goal for that chapter;
   - semantic question is not the goal text and is structurally history-answerable;
   - planned scope/PLAN_NODE does not become a historical Need;
   - accepted non-plan drafts contain grounded entity/relation anchors.
4. Reject ambiguous-only/unresolved-only drafts with typed reasons. Preserve their diagnostic counts
   in the run artifact, even though they do not become Needs.
5. Fix `PlannerWorldSummaryBuilder` so filtering precedes caps (especially open obligations), and use
   deterministic task/plan relevance ordering before stable-ID tie-breaking. Record truncation counts
   in the summary/artifact; do not silently present an arbitrary source prefix as the relevant world.
6. Add a durable Planner invocation artifact containing or referencing, by content hash:
   - AuthorPlanningContext;
   - PlannerWorldSummary;
   - exact prompt and prompt version;
   - model/revision/temperature/seed support;
   - raw response;
   - parsed drafts;
   - grounded drafts and statuses;
   - validation acceptance/rejection/dedup/truncation reasons;
   - final validated Need-set hash;
   - fallback status/reason and token usage.
7. Persist the artifact once per Planner invocation. Each emitted Need stores its artifact ID and
   draft ID; the frozen comparison/receipt stores a dereferenceable `ArtifactRef`.
8. Add frozen-artifact replay. It skips the model only when context, world-summary, prompt/schema,
   model policy, and validated set hashes match the current request. Any mismatch fails closed and
   creates a new run identity; it must not silently reuse or mutate the artifact.
9. Persist fallback invocations as artifacts too. Do not treat template fallback as missing lineage.
10. Propagate artifact ref, fallback status, and full grounding counts through every
    `PairedContextComparison` construction/copy path, flow summary, and five-segment report. Planner
    and Validator call counts/statuses must reconcile with the durable artifact ledger; zero means
    an explicitly proven no-call/frozen-replay path rather than a missing counter.

### 4.3 Enforce executable query routing

1. Make query availability explicit and deterministic. The effective channels are exactly:

   ```text
   ROUTES[query_intent]
   intersect allowed_candidate_pools
   intersect channels with a valid compiled query
   ```

2. Channel eligibility at minimum:
   - BM25 requires non-empty lexical queries;
   - Dense requires a non-empty semantic question;
   - R1 exact/temporal requires a grounded entity ID and/or an exact predicate appropriate to the
     intent;
   - typed graph requires grounded seeds and any required relation constraint;
   - hierarchy requires its declared parent/arc basis;
   - excluded information labels remain enforced independently of channel availability.
3. If no legal channel remains, return a typed no-executable-query trace/failure attributed to that
   Need. Do not open all channels and do not manufacture a broad query.
4. Add an R1 repository defense so factual exact retrieval without entity/predicate filters cannot
   execute even if a future caller bypasses the orchestrator.
5. Preserve direct retrieval versus corridor expansion lineage and record the compiled query bundle
   plus effective-channel decision in the trace.

### 4.4 Rebuild five-segment evaluation on frozen bindings

1. Introduce a typed evaluator-only `GoldNeedBinding` (or equivalent internal artifact) recording:
   profile, Gold ID, spec ID/hash, selected Need ID, component hits/misses, eligible Ledger IDs,
   blindness classification, and deterministic tie-break evidence.
2. Implement the per-applicable-Gold rules in §3.3.
3. Change missing-denominator semantics to explicit unavailable. Extend the report schema with typed
   availability/denominator fields or nullable metrics; never encode absence as perfect success.
4. Plan Goal Coverage must consume accepted Planner lineage, not raw Need chapter tags alone.
5. Evidence Recall must use the Need-bound Ledger subset. Evidence retrieved under another/broad Need
   is not credit for this Gold.
6. Completion/Claim Accuracy continues to use the frozen Per-Gold evaluator and Gold matcher; do not
   replace it with another LLM or fold Leakage into accuracy.
7. Leakage remains independent and zero-tolerance. Count every Plan-labelled citation/ledger entry
   under strict D9 and every future/profile contamination event.
8. Route plan-only annotations to Plan Goal Coverage as defined in §3.3. Exclude Plan refs from
   Evidence Recall and Claim/Completion denominators; expose legacy `plan_obligation_coverage` as
   deprecated/unavailable under the new formula.
9. Represent arm execution status and metric availability explicitly. A skipped/failed arm has no
   metrics, delta, gain, or tool-call-reduction result. Keep deterministic single-arm Gate status
   separate from paired comparability.
10. Make failure-stage terms terminally consistent. `COMPLETE` may not coexist with a final `MISS`;
    a post-retrieval/assembly/claim/evaluator loss must name its actual typed stage.
11. Audit evidence ancestry by canonical content/source identity. Shared observed-history
    deduplication is legal only when its TextRoot and cutoff basis are proven; foreign case Gold,
    future, and evaluator-only sources are never eligible.
12. Bump evaluator/formula/schema versions and include formula hash, binding artifact refs, Planner
   artifact refs, code/config fingerprints, and availability in case/unified reports.
13. Persist checkpoint reports under immutable experiment/attempt/profile/case/checkpoint/arm/formula
    identities before advancing to the next checkpoint. Write the latest convenience view only as a
    pointer/index, never as the sole evidence.
14. Aggregate only the child report refs named by one frozen experiment manifest. Fail closed on a
    missing, duplicate, conflicting, or foreign-attempt child; do not discover active results by
    recursively treating every historical object-store payload as current.
15. Report checkpoint scenario, single-arm evaluation, paired comparison, and complete matrix
    lifecycle independently and reconcile each state with its receipts.
16. Old five-segment reports remain immutable and are labelled legacy/invalid for the new formula.

### 4.5 Implement one endpoint-global scheduler

1. Introduce the scheduling descriptor required by the concurrency design: request ID, endpoint ID,
   Need/stage, prompt estimate, reserved output, safety allowance, total reserved sequence tokens,
   dependencies, context hash, priority, and scheduling deadline.
2. Use a condition-variable/queue scheduler, not polling sleeps. Admission requires both request and
   effective-KV capacity. Stable queue ordering must be observable and deterministic for equal
   priorities.
3. Return an idempotent lease/context manager. Release in `finally` for success, validation error,
   raw-response absence, artifact-retention error, model timeout, cancellation, and unexpected
   exceptions. Reject double release and counter underflow.
4. Implement typed states/outcomes at least for:
   - `WAITING_FOR_CAPACITY`;
   - `SCHEDULING_TIMEOUT`;
   - `SCHEDULING_BUDGET_UNSATISFIABLE`;
   - `CONTEXT_BUDGET_EXCEEDED`;
   - existing model/validation/retrieval failures.
5. Apply the reserve ratio to usable capacity. Remove the oversized-request bypass. The snapshot must
   expose configured/effective budgets, queue depth, in-flight reservations, wait time, timeouts, and
   stage/endpoint attribution.
6. Construct one scheduler for every real run regardless of `max_concurrent_needs`; concurrency=1
   still uses and verifies the same admission path.
7. Route Planner, replay/bootstrap agents, Claim Support, and Evaluator calls through the scheduler
   at the shared gateway/endpoint boundary. If multiple physical endpoints exist, budgets are keyed by
   endpoint identity rather than model role labels.
8. Keep same-Need proposal → verify → next chunk dependencies serial; only independent Need pipelines
   and evaluator batches may overlap.
9. Implement verification-cache single-flight:
   - the first caller owns the request;
   - concurrent equal keys await the same Future/result;
   - failures are propagated consistently and do not poison later retries;
   - completed cache access remains thread-safe;
   - cache identity includes every semantic input and model-policy field.
10. Workers return immutable outcome plus artifact payload intents. The coordinator persists artifacts,
    receipts, attestations, workset reports, and audit events in stable `(checkpoint, need_index,
    local_sequence)` order. Workers do not write a shared artifact or mutate a global builder directly.
11. Make ModelGateway call records, raw-response retention, structured-validation attempts, and call
    ledger concurrency-safe without making completion order the persisted semantic order.
12. Refactor checkpoint corridors to return local freeze/score/reveal outcomes. Coordinator commits
    lifecycle receipts in checkpoint order; per-case `_FrozenState` remains immutable and cutoff-bound.

### 4.6 Preserve semantic parity while optimizing

For every request that exists in both serial and concurrent executions, persist and compare:

- Planner/context/world hashes;
- prompt bytes/hash and token estimate;
- max output/thinking/model parameters;
- Need/completion contract hash;
- workset/evidence membership and ordering;
- retrieval query bundle and effective channels;
- Writer 4000 / Ledger 12000 budgets;
- model policy and retry settings.

Concurrency may change submission/completion time only. Do not use scheduling pressure to change
prompt, evidence, chunk size, max output, retry policy, model settings, or mandatory Need execution.

The broad teacher-forced transport change (`12288`, `600s`, thinking disabled for every agent) must
be audited as a separate semantic/runtime configuration version. Keep it only with per-agent evidence
and a new configuration fingerprint; do not describe it as a concurrency-only change.

## 5. Required regression and contract matrix

All tests are license-free and may not embed private Gold text, accepted evidence, case-specific
roles, or chapter-specific production exceptions.

### 5.1 Evaluation adversarial tests

Add regressions proving:

1. scope from Need A, entity from Need B, and facet from Need C cannot fully match one GoldNeedSpec;
2. evidence under the wrong Need does not increase Evidence Recall;
3. Plan Goal Coverage rejects an unvalidated/self-declared chapter tag;
4. profile-inapplicable Gold and specs are absent from denominators;
5. blindness classification produces the defined VAC/TIO/APC denominators;
6. missing specs/empty required segments are unavailable, not `1.0`;
7. Plan citation under any Need is leakage under APC;
8. a correctly bound coherent Need and evidence set receives credit;
9. strict-D9 APC can receive Plan Goal Coverage through validated Planner lineage while emitting no
   Plan evidence/claim and treating legacy plan-obligation coverage as unavailable;
10. skipped Agentic produces no metrics/deltas/gain/reduction while deterministic single-arm Gate
    metrics remain readable;
11. final `MISS` cannot retain `primary_failure=COMPLETE`;
12. shared observed-history content deduplication retains canonical ancestry, while another case's
    evaluator-only source cannot receive credit;
13. every checkpoint/attempt report remains independently addressable; aggregation rejects missing,
    duplicated, conflicting, or unmanifested child reports;
14. scenario completion does not imply paired or matrix completion;
15. formula/hash/version changes invalidate comparison with the old report.

Replace any existing assertion that intentionally expects the global-union behavior.

### 5.2 Planner and APC contract tests

Add regressions proving:

1. fallback and GROUNDED/AMBIGUOUS/UNRESOLVED counts survive E2E comparison and report serialization;
2. multi-facet/multi-scope drafts retain all legal completion requirements;
3. contradictory/unknown/PLAN_NODE/planned-scope historical drafts fail with typed reasons;
4. ambiguous-only and unresolved-only drafts never reach exact/graph retrieval;
5. trigger goal/chapter mismatch and future factualization are rejected;
6. open obligations are filtered before capping and summary ordering is deterministic/relevance-bound;
7. Planner artifact write/read/replay is content-addressed and hash mismatch fails closed;
8. compiler/importer reject task, profile, target, outline, goal, ref/hash, and PlanRoot mismatches;
9. derived gate subsets contain newly derived matching contexts;
10. APC all-Need policy is `(True, False, False)` and all evidence is observed-only;
11. TIO/VAC public cases reject PlanRoot at construction and validation boundaries.

### 5.3 Query-routing tests

Add regressions proving:

1. exact routes with no entity/predicate do not call R1;
2. graph routes with no seeds do not execute;
3. BM25/Dense use their respective compiled queries;
4. effective channels equal the three-way intersection;
5. no-executable-query is typed and attributed;
6. R1 rejects a direct unfiltered factual exact call;
7. direct versus expanded units remain distinguishable.

### 5.4 Scheduler and concurrent-state tests

Add deterministic threaded/async regressions proving:

1. multiple oversized/unsatisfiable requests cannot exceed request or KV limits;
2. reserve ratio changes effective capacity;
3. capacity waits, then admits after release;
4. scheduling timeout is distinct from model timeout;
5. every exception/early-return path returns the lease and counters end at zero;
6. double release/underflow fails visibly;
7. Planner, replay agent, Claim Support, and Evaluator requests share one endpoint maximum;
8. scheduler is active even at semantic concurrency=1;
9. equal verification keys produce exactly one model call while in flight;
10. single-flight failure unblocks waiters and permits a later clean retry;
11. worker artifacts are persisted once and in stable order;
12. checkpoint completion out of order still produces ordered lifecycle receipts;
13. serial and concurrent request descriptors/context hashes/evidence order are identical;
14. failure attribution remains local to its Need/batch and independent work continues;
15. queue and scheduler telemetry agree with call-ledger receipts.

### 5.5 Full quality

Run on the final tree:

```bash
make quality
.conda-env/bin/pre-commit run --all-files
```

The final suite must retain 100% statement/branch coverage and strict MyPy/Ruff cleanliness. Record
exact commands, counts, durations, HEAD, dirty status, and code fingerprint in
`.agent/implementation.md`.

## 6. Real execution and artifact evidence

Real execution is staged. Do not jump directly to another 95-chapter run.

### 6.1 Focused Planner/artifact run

Using the configured real local endpoint and a development case (P001 or P002):

1. execute one APC Planner invocation under final code;
2. persist the complete Planner artifact;
3. replay from that artifact with the model disabled;
4. prove identical validated Need set, completion contracts, query bundles, and Planner diagnostics;
5. prove a changed context/world/prompt hash refuses reuse;
6. show no planned/PLAN_NODE Need entered observed retrieval.

P003 may be used once as validation after P001/P002 behavior is stable; it must not be used to tune a
case-specific prompt. P004/P005 remain frozen test cases.

### 6.2 Serial/concurrent parity and scheduler load run

Run the same bounded APC development checkpoint twice from the same canonical commit and frozen
Planner artifact:

- serial scheduling (`need concurrency=1`, evaluator concurrency=1);
- recommended concurrent scheduling under the same endpoint-global budget.

Required evidence:

- identical semantic request descriptor/context hashes, prompt bytes, model parameters, completion
  contracts, worksets, evidence ordering, and budgets;
- observed in-flight request and KV usage never exceed configured/effective limits;
- capacity waiting and release are visible;
- no scheduling/model failure is misclassified;
- no duplicate equal-key verification call;
- stable persisted artifact/receipt ordering;
- no KV OOM and no context reduction;
- wall time and throughput reported as performance evidence, not a correctness substitute.

Model output may vary; semantic input parity must not.

### 6.3 Fresh Phase 4 identity

Only after §§5.1-5.5 and §§6.1-6.2 pass:

1. compile/import the unchanged frozen source inputs with the corrected derived contexts;
2. create a fresh experiment/database/output identity and final code/config/formula hashes;
3. run the APC main profile through P001-P005;
4. run TASK_INTENT_ONLY as the defined ablation under the same fixed semantic budgets and final
   formula;
5. persist and verify each checkpoint-scoped report and manifest child ref before starting the next
   checkpoint; never reuse an attempt directory after a code/config/formula change;
6. retain the old history/VAC artifacts as a labelled legacy baseline; do not silently aggregate
   old-formula scores with the new report;
7. monitor endpoint health, scheduler telemetry, checkpoint progress, projection freshness, artifact
   retention, and leakage throughout;
8. if a corridor or replay fails, diagnose and repair only within this plan, then use a new run
   identity when code/config/formula changes.

The fresh run reports Gate 0-3 measurements but does not choose new thresholds. P001/P002 calibration
evidence is returned to Codex for threshold freezing; P004/P005 are held-out evidence and must not
drive code, prompt, or threshold edits. Agentic need not run for this single-arm semantic matrix;
if it is not run, the report must state that no paired claim exists without invalidating Arm A.

## 7. Acceptance signals

The next Codex review requires all of the following.

### 7.1 Contract and semantic closure

- all importer/APC mismatch regressions fail closed;
- derived subset case/context/Plan bindings agree;
- every APC Need has strict D9 policy and every Memory evidence entry is observed-only;
- no planned-scope/PLAN_NODE historical Need survives validation;
- multi-facet completion contracts preserve the accepted draft semantics;
- ambiguous/unresolved drafts cannot trigger broad exact/graph retrieval;
- effective retrieval channels match compiled query availability.

### 7.2 Planner and metric integrity

- every Planner run, including fallback, has a dereferenceable artifact;
- frozen Planner replay is basis/hash checked;
- fallback/grounding diagnostics in the report equal the artifact;
- every scored Gold has an applicable-profile/spec/Need/Ledger binding artifact;
- adversarial cross-Need and wrong-Need evidence tests receive no false credit;
- missing metric annotations are unavailable rather than perfect;
- plan-only annotations score Planner goal coverage rather than impossible Plan evidence/Writer
  claims under D9;
- skipped/failed arms have no synthetic metrics or derived improvement flags;
- stage-loss labels, final verdicts, and lifecycle statuses are mutually consistent;
- checkpoint/attempt reports are immutable, manifest-addressed, and aggregation ignores no ambiguity;
- old/new formula versions are visibly non-comparable;
- Plan/future leakage is zero, with no privileged Plan channel.

### 7.3 Scheduler correctness

- one endpoint-global scheduler covers every model call stage;
- request and effective-KV ceilings are never exceeded;
- no capacity leak, counter underflow, polling loop, or indefinite wait remains;
- scheduling timeout is typed separately;
- verification cache has true in-flight deduplication;
- workers do not persist shared artifacts or mutate lifecycle builders out of order;
- serial/concurrent semantic inputs are identical;
- focused real load evidence shows stable execution without KV OOM or context reduction.

### 7.4 Repository and real evidence

- `make quality` and pre-commit pass on the final code fingerprint;
- focused Planner replay and scheduler parity artifacts are complete;
- fresh APC/TIO Phase 4 runs use new identities and final hashes, if admitted;
- every P001-P005 checkpoint has a distinct report ref and the final matrix references exactly those
  children; no checkpoint is represented only by an overwritten convenience path;
- Writer ≤ 4000, Ledger ≤ 12000, future leakage = 0, Plan evidence leakage = 0;
- `.agent/implementation.md` contains exact commands, fingerprints, test results, real run recipes,
  scheduler telemetry, artifact refs, per-segment denominators/bindings, failures/repairs, and a stable
  final handoff.

## 8. Safety, frozen boundaries, and non-goals

Do not:

- edit P004/P005 source input, Gold, GoldNeedSpec, or frozen hashes based on results;
- introduce any Gold ID, Gold text, accepted evidence, case-specific role, entity, or chapter special
  case into production code/prompts/tests;
- restore Plan retrieval/citation through a Need-type exception;
- change Writer 4000 or Ledger 12000 budgets;
- fold Leakage into accuracy or tune a formula/threshold to improve current scores;
- run old-formula P005 or use P004/P005 observations to tune code, prompts, formulas, or thresholds;
- populate an unexecuted arm with copied metrics or call the result a paired comparison;
- fabricate independent legacy report files from a convenience path that was already overwritten;
- replace deterministic Need/Evidence matching with another evaluator LLM;
- shrink prompts, worksets, evidence, output budgets, or mandatory execution for concurrency;
- increase vLLM `max-num-batched-tokens` from the accepted 2048 baseline in this task;
- start full-chain async migration unless the focused gateway/scheduler implementation proves the
  approved local island cannot meet correctness;
- modify Stage 3 implementation or claim Stage 3 progress;
- edit architecture, ADR, status, active execution, `.agent/task.md`, `.agent/plan.md`, or
  `.agent/review.md` from OpenCode;
- create a parallel remediation/campaign/reporting document. Append evidence only to
  `.agent/implementation.md` and normal runtime artifact directories;
- introduce a second semantic owner, generic platform, dynamic DSL, plugin layer, new datastore,
  scheduler service, reporting warehouse/dashboard, or speculative cross-stage abstraction when an
  existing narrow owner can satisfy the named invariant;
- keep deprecated and replacement paths as a permanent dual architecture after migration evidence
  permits deletion;
- commit or merge.

## 9. Stop and return conditions

Return to Codex with evidence when any of the following is true:

1. direct author-plan content is shown to be required in WriterContext, because that requires a new
   typed product channel and an upper-level design/ADR change;
2. a frozen GoldNeedSpec cannot be represented as one coherent Need signature without changing
   P004/P005 annotations;
3. Planner artifact replay cannot preserve final Need/completion/query identity under identical
   hashes;
4. serial/concurrent semantic input hashes differ;
5. any Plan/future/profile leakage is observed;
6. the scheduler cannot enforce both ceilings for a valid single request under the approved budget;
7. an existing safety/product boundary would need to be weakened;
8. the real endpoint/infrastructure remains unavailable after in-scope diagnosis;
9. closing the task would require a parallel framework, new platform/product channel, or unrelated
   cross-stage generalization rather than a narrow extension of the approved owners;
10. the implementation, focused real evidence, and fresh admitted runs are complete.

A model transport failure, malformed model output, failed unit test, or first implementation defect
is not by itself a return condition. Diagnose and repair in direction while the governing invariants
remain sufficient.

## 10. Reporting and handoff

`.agent/implementation.md` remains the single implementation evidence log. Append one new top-level
section for this task and include:

- the initial evidence quarantine and exact dirty fingerprint;
- the six-object 2026-08-08 paired-summary inventory, the four selected baseline attempts, the four
  Stage 2M case refs, and an explicit note that three top-level E2E reports were overwritten;
- architecture-to-code mapping for §§4.1-4.6;
- for every new contract/component, its current caller, unique owner, protected invariant, reason the
  existing narrow extension was insufficient, and the acceptance that justifies retaining it;
- schema/formula/version changes;
- every regression name and result;
- full quality and pre-commit evidence;
- Planner artifact examples and replay/mismatch receipts;
- serial/concurrent request-parity artifact and endpoint-global telemetry;
- capacity wait/timeout/single-flight/failure-release evidence;
- fresh Phase 4 experiment identities, recipes, monitoring, and final artifacts if admitted;
- the manifest-addressed per-checkpoint/per-attempt report index and typed single-arm/paired/matrix
  lifecycle states;
- per-profile metric availability, denominators, Gold→Need→Ledger bindings, and leakage;
- every evidence-driven in-direction repair;
- final state `COMPLETE` or `RETURN_TO_CODEX` with remaining architectural questions.

Do not report `COMPLETE` merely because tests pass or because a long run ended. `COMPLETE` means the
semantic contracts, metric attribution, artifact reproducibility, scheduler invariants, and required
real evidence above all hold on the same final code/config/formula identity.

## 11. 2026-08-09 Claim Support runtime repair authorization

The diagnostic input
`.agent/claim_support_runtime_bottleneck_analysis_20260809.md` is accepted as the starting evidence
for the next narrow implementation pass. Its P0-P6 ordering and §11 stop conditions govern this
pass. No formal APC/TIO P001-P005 or complete P002 parity run is authorized until the frozen
single-chunk calibration, failure telemetry, non-fallback Planner artifact, and bounded
serial/concurrent admission conditions pass in order.

For this pass only, the prohibitions in §4.6 and §8 against changing max output/thinking parameters
for concurrency are clarified as follows:

- completion contract, evidence/workset membership and order, prompt semantic content, model,
  input hashes, Writer/Ledger budgets, D9, leakage boundaries, and typed fail-closed validation
  remain semantic parity requirements and may not be weakened;
- Claim Support may run a small bounded calibration on one frozen, previously truncated multi-slice
  chunk with thinking disabled and a lower fixed total-output cap, because these are transport
  generation guards rather than permission to remove semantic input or typed result fields;
- the accepted setting must be the smallest fixed configuration with measured headroom that
  reliably returns a valid `MultiSliceProposalBatch` or typed insufficient result and closes at
  least one proposal -> verifier -> persisted-receipt chain; do not increase the current cap, use
  strict grammar/schema generation, or create a dynamic tuning/controller mechanism;
- record configuration and content identity so later serial/concurrent evidence uses exactly the
  same accepted semantic and transport configuration. Concurrency itself still may not change it.

Implementation must reuse the existing Claim Support, Planner, Validator, Model Gateway/ledger,
progress-event, manifest, and artifact owners. Add the minimum telemetry required to reconcile
proposal descriptors, progress terminals, scheduler descriptors, and model outcomes, including
invalid raw output retention and length/validation latency, finish reason, and available usage.
Obtain and freeze one successful non-fallback P002 Planner artifact with replay/lineage closure
before fan-out or bounded concurrency work. Only if the accepted compact output path remains too
slow may the existing Validator perform evidence-backed compatible-facet deduplication; any change
to irreducible completion facets, query corridor, or workset semantic budget returns to Codex.
