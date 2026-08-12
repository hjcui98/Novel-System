# Stage 2M Repair Handoff: Follow Upper Execution Document

- Lifecycle: `ACTIVE_TASK_SUPPLEMENT`
- State: `DEFER_TO_UPPER_EXECUTION_DOC`
- Updated: `2026-08-11 +08:00`
- Stage: `Stage 2M` only. Stage 3+ must not be modified.
- Architect/reviewer/merge owner: Codex
- Implementation owner: OpenCode default `build`

## 0. Correction

Codex's previous local `.agent/plan.md` handoff over-specified Round 2 and could be read as a
different execution plan from the upper-level repair document. That was wrong.

This file now deliberately stops being a parallel plan. OpenCode must execute directly from:

1. `docs/stage2_memory_architecture_repair_execution_20260811.md`
2. `docs/adr/0008-evidence-first-writer-context-product.md`
3. `.agent/current_memory_architecture_reality_audit_20260811.md`
4. `.agent/implementation.md` latest handoff section

When these sources disagree, the upper-level repair execution document wins.

## 1. Current Assignment

Continue Stage 2M repair according to
`docs/stage2_memory_architecture_repair_execution_20260811.md`.

Do not treat this `.agent/plan.md` as an independent technical design, phase split, or acceptance
matrix. It only records that Codex has handed execution back to OpenCode and that the upper document
is authoritative.

## 2. Boundaries

Follow the upper document's allowed and forbidden work exactly, including its round gates, ownership
boundaries, and stop conditions.

In particular:

- do not start KG/World rebuild unless the upper document's entry conditions authorize it;
- do not touch Face, Stage 3, Stage 4, or Stage 5;
- do not reintroduce Claim Support, whole verifier, semantic evaluator, claim matcher, or Gold scorer
  into the default Memory read path;
- do not mutate frozen DB/index/Commit/World/Text roots or benchmark Gold;
- do not commit, merge, or push.

## 3. Handoff Back To Codex

When the current upper-document repair unit is complete, append a new section to
`.agent/implementation.md` with:

- what changed;
- focused evidence run by OpenCode;
- artifact paths, if generated;
- commands intentionally deferred to Codex, if any;
- blockers that require Codex architectural judgment.

Then stop and return to Codex for review / unified testing / integration.
