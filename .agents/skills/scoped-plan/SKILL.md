---
name: scoped-plan
description: "Let Codex turn the project's existing architecture, design, and execution documents into one substantial OpenCode assignment without creating a duplicate documentation system."
---

# Scoped Plan

Use this skill for `/plan`. Codex is the top-level designer and architect; this skill prepares the
implementation handoff and does not perform the implementation.

## Inherit the Existing Document System

1. Read `AGENTS.md`, then use `docs/README.md` to resolve the repository's current document
   hierarchy and lifecycle. Read `docs/project_status.md` plus the applicable architecture, design,
   active execution, diagnostic, code, test, and real-artifact sources.
2. Treat an architecture or design document already discussed and accepted by the user as the
   upper-level decision. Treat the applicable active execution document as the current stage's
   operational authority. Preserve their precedence; do not make `.agent/plan.md` a competing
   source of truth.
3. Reuse before writing. If the upper-level documents already explain the design and execution
   route, cite their exact sections and add only the current task's missing context. Do not copy
   their contents or create another design, remediation, or execution document.
4. If a material architecture decision is genuinely absent, Codex decides it with the user when
   user judgment is required, then updates the most appropriate existing upper-level document. A
   new upper-level document is justified only when no existing document has the right purpose or
   lifecycle for that decision.

## Make the Architectural Decision

Investigate enough code and evidence to identify the real root cause, responsible layer, and
effective mechanism. Distinguish retrieval, ranking, selection, assembly, provenance, evaluation,
and runtime failures when relevant. Explain why the chosen direction should work and reject
shortcuts that merely move a metric or satisfy a report.

Update `.agent/plan.md` as a task-local supplement containing only what OpenCode needs:

- links to the governing documents and sections;
- the current objective, starting evidence, and any task-specific architectural judgment;
- the mechanism or outcome to implement, including important invariants;
- observable acceptance signals from tests, real API runs, monitoring, and artifacts as applicable;
- conditions that require returning to Codex because the approved direction is no longer enough.

Give OpenCode one substantial unit of work: implementation, regression tests, real execution,
monitoring, artifact diagnosis, and in-direction repair should normally fit in the same assignment.
Do not split work by individual file, checkpoint, command, or tiny iteration merely to retain
control.

## Keep Ownership Simple

Codex alone creates or edits architecture, design, planning, ADR, active execution, current-status,
`.agent/task.md`, `.agent/plan.md`, and `.agent/review.md` files. OpenCode receives those files as
read-only authority and writes implementation evidence and summaries.

Do not invent a control plane, custom agent state machine, file-by-file permission map, arbitrary
iteration count, or repetitive governance checklist. Specify exact files or commands only when
they are technically necessary or safety-critical.
