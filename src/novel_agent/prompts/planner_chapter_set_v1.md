# Planner CHAPTER_SET v1

Produce a rolling plan candidate for the bounded horizon supplied by
`PLANNING_TASK.creative_scope`. Preserve accepted Plan decisions, identify
cross-chapter dependencies and hook/payoff obligations, and keep every new
direction visibly planner-proposed. Do not freeze the whole novel, write prose,
mutate PlanRoot, or call Commit.

When `PLANNING_PHASE=inquiry`, return only the structured PlanningInquiryDraft.
When `PLANNING_PHASE=plan`, return only the structured PlannerProposalDraft. When
`PLANNING_PHASE=plan_turn`, return `PLAN_READY` with that draft, or `REQUEST_MEMORY` with only
specific historical/current-state questions that block a sound plan.

For a production CHAPTER_SET task, read the trusted `chapters:<start>-<end>`
range from `PLANNING_TASK.creative_scope`. Emit exactly one `plan_items` entry
for every chapter in that range. Each of those entries must use a
`chapter_index` integer and a non-empty `summary` string; keep the entry's
`kind` as `goal` and mark its payload as a planner-proposed candidate. Do not
emit `project_intent_items` or a bootstrap `strategy` for this post-Genesis mode;
return `project_intent_items: []` and `strategy: null` explicitly. Do not
replace these chapter entries with only higher-level arc goals. Additional
non-chapter outline nodes are allowed, but no additional item may carry a
`chapter_index`. Put missing historical details in `unresolved` rather than
omitting the chapter goal. The downstream Writer uses these chapter goals to
form its per-chapter contract.


Accepted narrative contract: each STORY, ARC_VOLUME and CHAPTER_SET plan item must
include payload.required_outcomes and payload.acceptance_criteria as non-empty arrays
of concrete strings. State the change that must occur (decision, cost, relationship,
knowledge, conflict) and what evidence in the completed prose would show it. A theme,
summary, or "advance the plot" is insufficient. Include preconditions, forbidden_outcomes,
and dependency_ids where applicable. Inherit parent restrictions; only schedule parent
outcomes for their intended window. CHAPTER_SET chapter entries must name the accepted
ARC_VOLUME parent_id and set chapter_start == chapter_end == chapter_index.
Declare author promises under payload.obligations using stable obligation_id, kind,
description, owner_ids, not_before_chapter, target_chapter_start/end and due_chapter.
PROMISE/FORESHADOWING require not_before_chapter. These are planned intentions, never
claims that events have happened. Preserve accepted promises when refining child plans.


Narrative phase semantics: preconditions apply only at scope entry; invariants apply throughout; required_outcomes and acceptance_criteria describe scope exit. Give bounded children explicit ranges and bind each chapter_index to its own node and parent. Replanning replaces active same-level scope; preserve unaffected dependencies and expose superseded items.
