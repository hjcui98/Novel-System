---
description: Execute the Codex-designed task end to end and write evidence
agent: build
subtask: false
---

The human has reviewed and approved `.agent/plan.md` by invoking this command.

Read `AGENTS.md`, `.agents/skills/scoped-implementation/SKILL.md`, `.agent/task.md`, `.agent/plan.md`,
and the latest `.agent/review.md` if Codex requested repair. As the default `build` agent, execute
the technical task end to end: implement, test, use and monitor authorized real APIs, diagnose
artifacts, repair within Codex's direction, and report in `.agent/implementation.md`. Treat cited
architecture, design, status, planning, and active execution documents as read-only. Write another
result/summary document only when `.agent/plan.md` explicitly names it. Return when the design is
demonstrated or a genuine architecture/external blocker is reached.

Additional human arguments: $ARGUMENTS
