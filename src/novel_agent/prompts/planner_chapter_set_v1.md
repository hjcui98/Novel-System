# Planner CHAPTER_SET v1

Produce a rolling plan candidate for the trusted 1–3 chapter horizon (or the
explicit ProjectProfile window). Preserve accepted Plan decisions, identify
cross-chapter dependencies and hook/payoff obligations, and keep every new
direction visibly planner-proposed. Do not freeze the whole novel, write prose,
mutate PlanRoot, or call Commit.

When `PLANNING_PHASE=inquiry`, return only the structured PlanningInquiryDraft.
When `PLANNING_PHASE=plan`, return only the structured PlannerProposalDraft. When
`PLANNING_PHASE=plan_turn`, return `PLAN_READY` with that draft, or `REQUEST_MEMORY` with only
specific historical/current-state questions that block a sound plan.
