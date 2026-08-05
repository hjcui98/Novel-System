---
name: scoped-implementation
description: "Let the OpenCode default build agent execute a Codex-approved design end to end, including implementation, tests, real API runs, monitoring, artifact analysis, in-direction repair, and evidence reporting. Use when the human invokes /implement."
---

# Scoped Implementation

Use this skill for `/implement`. Run the normal OpenCode `build` agent; `/implement` is a command
entry point, not a new restricted agent or a separate orchestration platform.

## Read the Authorities

1. Read `AGENTS.md` and use `docs/README.md` to resolve document precedence.
2. Read the applicable upper-level architecture, design, status, and active execution documents
   cited by `.agent/plan.md` before treating the task supplement as instructions.
3. Read `.agent/task.md`, `.agent/plan.md`, and `.agent/review.md` when the latest review contains a
   repair direction. The plan and review supplement the upper-level documents; they do not replace
   them.
4. If these sources conflict in a way that changes architecture, Stage, safety, or acceptance,
   report the conflict to Codex instead of silently choosing a new direction.

## Execute End to End

Follow Codex's root-cause judgment, design direction, invariants, acceptance signals, and stop
conditions. Within that direction, use the normal implementation freedom of the `build` agent:

- inspect and modify all relevant production code, tests, scripts, configuration, and fixtures in
  the responsible subsystem;
- add regression coverage and run the focused and broader checks needed to establish confidence;
- when the task calls for it, use the designated real API, monitor long-running work and model
  calls, preserve artifacts, and diagnose the earliest real loss rather than relying only on final
  scores;
- repair implementation and runtime defects that remain inside the approved design, then rerun the
  affected checks without waiting for a new micro-plan;
- do not repeat an unchanged failed run: use its evidence to change the code, test, runtime
  condition, or diagnosis first.

Continue until the design is demonstrated, evidence shows the design direction is wrong, a new
architectural decision is needed, or an external dependency is genuinely unavailable.

## Report, Do Not Redesign the Documentation

Keep `.agent/implementation.md` current with the implementation, exact commands, test and real-run
results, artifact paths, diagnosis, repairs, and remaining risks. Write another execution result or
summary document only when Codex's plan explicitly names it.

OpenCode must not create or edit architecture, design, planning, ADR, active execution,
current-status, `.agent/task.md`, `.agent/plan.md`, or `.agent/review.md` files. It reports evidence
to Codex; it does not resolve architecture, promote a Stage, declare a formal Gate, or create a new
workflow system.
