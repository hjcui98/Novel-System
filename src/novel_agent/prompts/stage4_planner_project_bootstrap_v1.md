# Stage 4 Planner PROJECT_BOOTSTRAP v1

Normalize author-supplied project intent and routed design candidates. Honor `PLANNING_PHASE`: inquiry returns only a PlanningInquiryDraft and plan returns only a PlannerProposalDraft. Bootstrap has no base commit, does not call project Memory, and never writes PlanRoot or Commit.
For `plan_turn`, return `PLAN_READY`; bootstrap must not request project Memory.
