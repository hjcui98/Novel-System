# Current production content-path assessment

## 2026-09-12 second repair: implemented and offline verified

The complete follow-up findings are implemented in the uncommitted working tree. Final focused
regression: **375 passed in 61.68 seconds**. Changed Python files pass Ruff; mypy passes for 50
changed source files. Verification uses fake endpoints with external model calls forbidden.
No formal coverage gate, real hybrid service, real novel generation, commit or push was run.

The [completion record](../docs/production_content_repair_completion_20260912.md) maps every
finding to its implementation and records the remaining real-output validation limits.
It supersedes the NOT_IMPLEMENTED status in the historical audit below. Ordinary Curator now
aggregates all source batches and continuation pages, rather than imposing four changes on a
whole chapter. Frozen grounded evidence reaches Writer; production Need planning uses the gateway.

Additional recovery regressions found and fixed admission-lease cancellation and stale Context
checkpoint restoration. Both local and major repair now complete under one provider call per slice.

## Historical: 2026-09-12 follow-up production and skill audit

Status: `STATIC_AUDIT / ADDITIONAL_GAPS_IDENTIFIED / NOT_IMPLEMENTED`.
After the repair batch below, the user requested a deeper review of the complete production
path, retained artifacts, and the invocation and contents of every runtime skill. The user
cannot currently provide the prior real novel outputs. This audit read the current source,
all 38 runtime skill Markdown files, and retained fake-endpoint production artifacts; it
did not run additional tests or call models.

The detailed findings, source locations, artifact evidence, complete skill inventory and
implementation priorities are in the
[production path and skill audit](../docs/production_path_and_skill_audit_20260912.md).
Key additional gaps are the default Writer skill restriction and conflicting protocols,
fixed Planner loading despite selected-skill output, stale nodes after replanning,
ancestor preconditions applied without phase semantics, the four-operation ordinary
Curator ceiling and missing planned-obligation identity binding, Editor evidence trust
classification, and production checkpoint restoration order. Repair history, budgets,
lookahead boundaries, retrieval focus and long-run context growth also need work.

These findings include gaps left in the first repair. The 246 passing offline regressions
below remain valid within their stated scope; they do not establish that these additional
issues are fixed or that real novel quality is acceptable. This follow-up changed audit
documentation only. The repair implementation and original baseline evidence are retained below.

## 2026-09-12 repair implementation and final verification

Status: implementation complete for this repair batch; offline verification recorded below.
User direction changed from read-only/no tests to implementation, then "tests and acceptance last".
All results in this section concern the uncommitted working tree based on
`1856153298856bdcb6eb7a8c6ad62f0727ac9683`. No real model calls, infrastructure startup,
benchmark or long-running novel generation was performed. Git was not committed or pushed.

### Implemented behavior

1. **Memory delivery and production wiring.** Grounded blocks/spans now pass through exact-cutoff
   evidence resolution into frozen Writer selections with facet support. Production injects the
   existing Need planner using production model purpose, records its result/fallback artifact,
   and allocates fair request-local routes from actual snapshot capabilities. The reranker is
   passed to the legacy retrieval caller as well. Need generation runs outside the async runtime
   loop with lease heartbeats, avoiding nested `asyncio.run`; shared generator lineage is locked.
2. **Planning context and contracts.** Inquiry receives bounded author/profile/accepted-plan
   context as a separate trusted artifact. Reviewer dereferences allowed comparison content.
   Requirements preserve preconditions, outcomes, prohibitions, dependencies and acceptance
   criteria through PlanNode/ChapterGoal materialization. New hierarchical plans require concrete
   outcomes and criteria. Old empty-field roots retain their content identity.
3. **Obligations and Writer scope.** Author/planner obligations are stored in PlanRoot separately
   from observed World facts. Effective obligations combine planned timing with observed status;
   temporal guards also check the accepted PlanRoot during writes. Current chapter selection
   includes all ancestors and excludes future siblings linked only by a shared obligation.
   Parent outcomes become due at their scope boundary; overdue obligations reach the chapter
   contract, along with preconditions, dependencies and prohibitions.
4. **Editor and repair.** PASS requires explicit assessments of every outcome, acceptance
   criterion, mandatory constraint, prohibition and unresolved Memory gap. Positive outcomes need
   exact Draft quotes; claims that gaps are supported need supplied context quotes. Missing or
   invented evidence rejects the review. Length violations request major rewrite before acceptance.
   Local repairs can repeat on the latest candidate; local/major transitions preserve lineage and
   obey budgets. Major rewrite receives a new WorkPlan after the Editor directive.
5. **Runtime continuity.** Editor planning defects return to CHAPTER_SET for the same chapter,
   carrying explicit feedback subordinate to the accepted outline. A per-chapter budget stops
   repeated replans. Durable task topology checks the feedback artifact, scope, basis and generation.
   Draft task IDs preserve generation to avoid reuse; rolling windows are clipped to the parent
   volume. Reconciliation removes the currently matched change, avoiding shrinking-list indexes.
6. **Contracts and development workflow.** Affected versioned schemas were regenerated. Regression
   fixtures now provide explicit content assessments rather than bare PASS. The prior cleanup of
   obsolete OpenCode commands, handoff skills and unsuitable workflow constraints remains intact.

### Verification

- Final combined offline run: **246 passed, 1 warning in 60.38 seconds**. Environment:
  `NOVEL_AGENT_FORBID_MODEL_CALLS=true`; pytest used `--no-cov -q --tb=short`.
- Suites: `test_production_content_repairs`, `test_stage2_memory_gateway`,
  `test_stage2_paired_controller`, `test_editorial`, `test_production_stage2m_writer_context`,
  `test_stage4_planning_loop_and_evaluation`, `test_writer_context_loop`,
  `test_stage5_creative_runtime`, `test_plan_hierarchy_production`,
  `test_memory_write_validation_v2`, `test_u8b_runtime_commands`,
  `test_production_assembly_one_chapter`.
- The warning is a Pydantic serialization warning for a fixture root hash stored as `str` rather
  than `ArtifactId` in the production assembly smoke. It did not fail the run; it is not a clean
  warning-free result.
- Ruff passed for all 41 modified/new Python files; scoped mypy passed for all 34 changed source
  files. This is not a claim that repository-wide mypy is clean; the earlier broader check exposed
  existing errors in curator bootstrap overrides and temporal candidate decorators outside this patch.
- All `scripts/export*schemas.py` exporters completed; `git diff --check` passed.
- New focused regressions cover frozen historical prose delivery, ancestor/future-sibling scope,
  legacy root identity, planned-vs-observed obligations, invalid review evidence, length rewrite,
  multi-change reconciliation, repeated local repairs, and current-chapter replans with budgets
  of zero and two. The latter exercises actual durable task settlement and proves no Draft commit
  is produced while the Editor keeps requesting replan.

### Remaining limits

- Real retrieval recall/ranking, actual Need planner behavior, plan specificity and literary quality
  are not established by fake endpoint tests. `real_hybrid` remains an explicit deployment choice;
  the default in-memory smoke backend is not evidence of hybrid retrieval effectiveness.
- Exact quote checks establish source presence, not that a model's semantic judgment is correct.
  There is no separate persisted cross-chapter milestone fulfillment ledger yet. Runtime completion
  remains target-chapter based, with current due requirements checked through Writer/Editor.
- Major rewrite still requires a complete candidate and cannot request another Memory round;
  pre-draft reactive Memory remains available. Exhausted repair/replan budgets require attention.
- Existing weak accepted plans remain readable; adding fields does not retroactively enrich them.
  New planning/replanning must produce concrete requirements, and real output still needs review.
- Full-suite coverage and the formal 100% branch-coverage gate were not run or claimed. The threshold
  is unchanged. No production-quality or endurance acceptance is claimed.

The following assessment is retained as **historical baseline evidence**. Its source line numbers
refer to the baseline commit and its "no product code/tests changed" statement describes that
initial read-only assessment, not the repaired working tree above.

## Historical baseline: 2026-09-12 production content-path static assessment

- Scope: `feat/hierarchy-progressive-skill-patch`, source commit
  `1856153298856bdcb6eb7a8c6ad62f0727ac9683`.
- Status: `STATIC_ASSESSMENT / PRODUCT_GAPS_IDENTIFIED / NOT_A_FORMAL_GATE`.
- User direction: read code carefully; do not run tests. Reported real-run symptoms are sparse
  Memory retrieval, vague plans with weak constraints, deviation from the outline, and low quality.
- Evidence: current source and existing documents only. No tests, benchmarks, or model calls were
  executed for this assessment. No product code was changed. The previous run's exact commit,
  retrieval profile, model requests, and prose artifacts are not present in this clone, so the
  contribution of each finding to that run is not yet established.
- Legacy acceptance entries were removed from this active file during the 2026-09-12 workflow
  cleanup; their original evidence remains in Git history at the source commit above.
  This assessment does not promote, revoke, or replace a formal Stage Gate.

### Product assessment

The repository has a substantial durable execution foundation and a real production assembly.
The latest commits wire STORY → ARC_VOLUME → CHAPTER_SET into scheduling and add bounded temporal
and length guards. The remaining content problem is visible in the production data path:
requirements and evidence are weakened or lost between owners, while later stages can continue
with incomplete inputs. A correctly scheduled and committed chapter is not yet evidence that the
author's intended narrative has advanced.

The README and much of `docs/project_status.md` describe older integration status. The September 2
production handoff records 23 committed chapters and concrete quality failures, but its runtime
artifacts are excluded from Git. Its pre-patch behavior must not be presented as a live measurement
of this September 5 commit.

### 1. Grounded historical evidence is discarded during Writer delivery

`src/novel_agent/services/memory_gateway.py:508-585` builds live evidence selections. At 530-535,
selected `GROUNDED_BLOCK` and `GROUNDED_SPAN` units are skipped before evidence resolution. These
units can carry exact historical prose references; their construction is in
`src/novel_agent/services/memory_pipeline.py:288`.

Production Writer delivery uses `ProductionStage2MWriterContext` → `MemoryGateway.resolve` → frozen
selections → `EvidenceFirstWriterContextAssembler`. The final assembler consumes those selections,
so a successful grounded retrieval can produce no delivered evidence for that Need. This affects
both the in-memory and real-hybrid backends. It is a delivery defect, not proof that the original
chapters were never stored.

The same selection builder also omits `supported_facet_ids` when constructing `SliceSelectionTrace`
at 564-574. The assembler interprets the default empty set as no mapped facet support
(`evidence_first_writer_context_assembler.py:319`). The checkpoint runner has a separate path that
does populate this mapping (`evidence_first_checkpoint_runner.py:642`). Evidence selection and
facet attribution need to converge on the actual production caller.

### 2. Production does not use several implemented Memory capabilities

- `src/novel_agent/runtime/production_bootstrap.py:1312-1315` constructs
  `TaskPlanConditionedNeedGenerator` without `planner_gateway`. Its constructor consequently sets
  `_planner=None` (`task_conditioned_need_generation.py:174-202`), and generation falls through to
  static `TaskFocusExtractor` logic at 284-297. The model-driven plan-conditioned Need generator
  used by the checkpoint runner is not wired into this Writer production path.
- `production_bootstrap.py:1182-1192` creates the shared controller without `route_plans`.
  `paired_controller.py:289-300` therefore selects legacy sequential retrieval. Its 355-370 loop
  can exhaust the shared budget on earlier Needs and leave later Needs unexecuted. The legacy
  `RetrievalOrchestrator` construction at 374-379 omits the available reranker as well.
- CLI defaults to `--retrieval-backend-profile memory` (`cli.py:75-79`). This path reads real
  Canonical Roots, but uses `AnchorBuilder` plus `InMemoryRetrievalBackend`
  (`production_bootstrap.py:761-846`); its dense score is token-set overlap rather than embedding
  search (`retrieval.py:691`). Only explicit `real_hybrid` selects the real retrieval assembly.
  The old run's profile is unknown; the first two omissions are not fixed merely by changing it.

### 3. Missing Memory can remain mechanically READY and enter Writer

`evidence_first_writer_context_assembler.py:846-857` separates assembly success from semantic
completeness. `NO_SELECTED_EVIDENCE` and mandatory facet gaps can coexist with READY. This separation
is an intentional product contract, but its consumer currently provides little enforcement:

- `production_components.py:498-519` expands budgets for budget problems, not for every unresolved
  critical narrative prerequisite.
- `adapters/runtime/stage3_writer.py:237-243` rejects falsely labelling known gaps as COMPLETE, but
  permits an honestly incomplete package.
- `services/loop_round_progress.py:57-68` checks only assembly/budget READY before writing.

Writer can request additional Memory, and RecentProse still provides local continuity. The precise
problem is that a thin historical package is allowed into generation without an explicit decision
about which missing facts make the current chapter unsafe or incoherent. A repair should distinguish
critical chapter prerequisites from tolerable gaps, rather than redefine all gaps as assembly errors.

### 4. Local Planner asks its first Memory questions before seeing the parent plan

`adapters/runtime/stage4_planner.py:151-155` intentionally removes raw author-intent artifacts from
ARC_VOLUME/CHAPTER_SET. However, `planning_context_loop.py:607-625` then prepares initial source text
from those artifacts plus World entity labels. At 735-744 it asks for the inquiry; at 872-910 it
generates Needs and retrieves Memory. Accepted-plan/profile context is assembled only afterward at
1010-1016.

Thus the first inquiry is not grounded in the actual parent plan and current narrative obligations.
Later Planner turns can request more Memory, but the initial questions are already vulnerable to
generic entity-centric requests. Keeping raw long-range prose out of local context needs a useful
bounded parent-plan projection at inquiry time.

### 5. Plans are persisted mostly as summaries, with weak binding to chapter execution

`adapters/runtime/materializers.py:395-441` projects proposed plan items into `PlanNode` and
`ChapterGoal`: title/summary, IDs, level, parent and chapter range. Other structured payload fields
are not retained in these Canon objects. `domain/world.py:69-78` and `domain/benchmark.py:130-134`
contain no mandatory narrative start/end state, success evidence, or milestone dependency contract.
Such detail can survive as prose in a summary, but has no separately enforced execution meaning.

`adapters/runtime/stage3_writer.py:149-194` selects nodes by goal ID or shared obligation IDs and
uses their summaries as both scene goals and required beats. The selection does not check node
level or chapter range. A future climax sharing an obligation can therefore become a current
required beat, while a parent volume without shared IDs can be omitted.
`writer_context_loop.py:1311-1336` similarly projects accepted plans by nearby goals/shared
obligations rather than following the complete ancestor chain.

Parent binding is also incomplete: `materializers.py:555-556` allows a CHAPTER_SET node without a
parent, while 571-589 checks only that some accepted volume covers the overall window. The window
check does not itself establish that each chapter belongs to the correct parent volume.

### 6. Plan Reviewer receives source hashes rather than the comparison content

`planning_context_loop.py:1715-1727` sends the proposal JSON to the reviewer and passes author/plan
context as `trusted_source_artifacts`. `agents/plan_reviewer.py:203-216` inserts only REVIEW_TARGET
and source hashes into the prepared prompt. `agents/runner.py:160-170,192-200` retains artifact refs
for receipts but does not dereference them. `prompts/registry.py:70-78` renders the hashes literally.

The prompt asks the reviewer to compare original intent, current state and parent scope, but those
source contents are not delivered through this call. Host constraints catch some explicit temporal
and parent-field errors; they do not reconstruct the missing comparison basis. This is a concrete
input defect in the nominally independent review, not evidence that the reviewer model ignored a
fully supplied outline.

### 7. Author-planned obligations do not automatically become executable future locks

`runtime/production_novel_bootstrap.py:503-547` constructs Genesis World from entities and states;
World obligations default to empty. Its initial `_plan_root` at 454-500 keeps title/summary and
opening goals without obligation windows. Later Plan materialization only updates PlanRoot
(`materializers.py:285-289`); `_node` does not preserve `not_before/target/due` fields as typed
obligations.

The effective locks are read from `world.obligations` by
`materializers.py:602-605` and `stage3_writer.py:325-341`. Therefore an author or Planner declaration
of a timed promise does not, through this path alone, establish the World obligation that Writer
and the hard guard consume. Existing World obligations supplied elsewhere or subsequently extracted
by Curator can work; the brief → accepted plan → executable lock path is not closed.

Even with an existing lock, `services/validation.py:184-218,265-295` checks structured obligation
operations marked resolved. A payoff present in prose but not extracted as that operation depends
on Editor's semantic judgment. A ban on early resolution also does not define the positive progress
that intermediate chapters must achieve.

### 8. Product acceptance and repair do not close the long-range quality loop

The Editor is a real content gate, but its guarantees should be stated precisely:

- `services/editorial.py:78-84` only adds the plan-adherence lens for explicit obligations or
  forbidden reveals, and the pacing/repetition lens for a prior report or more than two beats.
  Generic alignment instructions remain, but sparse ordinary chapter contracts receive less
  specialised review.
- `writer_context_loop.py:1096-1105` explicitly treats Writer/Observer reconciliation as advisory;
  Editor's final PASS is the hard content verdict. `creative_runtime.py:1492-1527` then auto-accepts
  according to policy without an additional milestone-completion check.
- `creative_runtime.py:973-1018` advances by chapter count/horizon and reports target completion by
  chapter count. There is no persisted upper-plan milestone → chapter outcome → volume/end-of-book
  fulfilment-evidence loop in this path.
- WorkPlan Skill receipts are PLANNED and are copied into the candidate receipt
  (`writer_cognition.py:297`, `writer_candidate.py:135`). Loading a Skill is not evidence that the
  resulting prose met its checkpoints.
- Local-repair and major-rewrite paths do not smoothly switch strategies after re-review
  (`writer_context_loop.py:623-725,803-814,899`). `planner_replan_required` is recorded but is not
  consumed by the runtime to create the required planning repair. A faulty plan can therefore trap
  prose repair within the same weak instructions.

The deterministic length guard is real (`materializers.py:782,844-852`), but runs during accepted
chapter materialization. In production settlement, its failure becomes BLOCKED/REVIEW_REQUIRED via
the generic catch at `creative_runtime.py:725-744`, without a Writer length-repair task. The current
patch protects Canon length more strongly than it protects autonomous recovery from short drafts.

### Additional statically identified continuity defects

- Cross-volume windows: runtime selects CHAPTER_SET if the next chapter lies in a volume
  (`creative_runtime.py:1754-1762`), but calculates the full window without clipping to that volume
  (`1635-1645`, `1798-1802`). An accepted volume 1-12 and horizon 5 can generate window 11-15 after
  chapter 10; materialization rejects it at `materializers.py:583-589`. This conditional failure is
  derived from code, not a reproduced run.
- Reconciliation indexing: `writer_change_reconciliation.py:38-50,65-84` stores original enumerate
  indexes and later uses them as indexes into a shrinking list. Two matching observations in order
  can cause the second `pop(1)` to exceed the one-item remaining list. The Writer catch at 1077 does
  not catch IndexError. No test was run to demonstrate this trace.

### Recommended next repair direction

1. Restore end-to-end input delivery in existing owners: grounded evidence and facet mapping,
   production Need/routing/reranker wiring, bounded parent context before inquiry, and actual
   comparison content for the reviewer. Preserve exact-source and information-boundary checks.
2. Strengthen the existing Plan/Obligation contracts with only the fields their current consumers
   need: required narrative outcome, start/end conditions, scope, dependency, forbidden outcomes,
   author-source lineage, and chapter/volume fulfilment evidence. Ensure materialization and Writer
   projection preserve them. Resolve the plan-to-executable-obligation ownership gap explicitly.
3. Make Writer admission and Editor decisions respond to critical missing evidence and unmet
   chapter outcomes; route length, prose, Memory and planning defects back to their existing owners
   with bounded budgets. Verify Skill use through content evidence where it matters.
4. Use the existing run's saved artifacts to attribute losses before any later authorised real-run
   validation. Future acceptance should separately assess causal continuity, character motivation
   and voice, narrative progress, pacing, payoff timing and readability, alongside durable runtime
   correctness. No new benchmark or test run is authorised by this assessment itself.
