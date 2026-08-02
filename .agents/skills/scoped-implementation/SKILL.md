---
name: scoped-implementation
description: Implement an approved .agent/plan.md as the sole OpenCode writer in one worktree, run only declared checks, and record a reviewable handoff. Use when a human invokes /implement after approving a Codex plan, or once more to address the first Codex review failure.
---

# Scoped Implementation

Act as the OpenCode implementer, not the planner or reviewer. Invocation of `/implement` is the
human approval for the current `.agent/plan.md`.

## Procedure

1. Read `AGENTS.md`, `.agent/task.md`, `.agent/plan.md`, and, for a repair, `.agent/review.md`.
2. Verify the stage, base commit, allowed files, plan state, and revision count. Stop as `BLOCKED` if
   the plan is missing, stale, contradictory, outside the current worktree, or requires a new design
   decision.
3. Work as the only writer in this worktree. Do not invoke subagents, AO, another orchestrator, PR
   automation, or an independent planning model.
4. Modify only allowed files. Preserve unrelated dirty changes. Never add Gold/case/checkpoint-derived
   runtime logic, lower a Gate, weaken provenance, or cross the declared stage boundary.
5. Run the plan's targeted checks. Run a real endpoint/canary only when the plan explicitly permits
   it and its health preflight passes; endpoint failure makes the run non-comparable and stops retries.
6. Write `.agent/implementation.md` with changed files, behavioral explanation, exact commands and
   results, residual risks, and `Revision: 0/1` or `1/1`. Do not claim PASS.

## Revision limit

On the first `FAIL`, fix only the findings in `.agent/review.md` and set `Revision: 1/1`. After a
second `FAIL`, make no more changes and return control to the human.

Never commit, merge, reset, clean, delete a worktree, start P3, or broaden scope unless the approved
plan explicitly assigns that operation.
