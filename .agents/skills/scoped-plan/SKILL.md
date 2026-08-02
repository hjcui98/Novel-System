---
name: scoped-plan
description: Create an approval-ready execution slice from the repository's canonical stage documents and current code evidence. Use when Codex must make the technical direction, stage boundary, allowed-file scope, acceptance criteria, canary decision, or stop/go decision before OpenCode implementation.
---

# Scoped Plan

Act as the Codex planner. Preserve the full canonical document workflow; make only the human-to-agent
control surface small.

## Procedure

1. Read `AGENTS.md`, `.agent/task.md`, the cited canonical stage documents, current Git status, and
   the relevant implementation/tests. Treat current code and immutable run artifacts as stronger
   evidence than stale status prose.
2. Confirm one stage and one concrete problem. Do not mix Stage 3 work into a Stage 2M task.
3. Diagnose before prescribing. Distinguish retrieval, ranking, Context selection, claim synthesis,
   provenance, evaluator, and runtime failures. Do not describe metric plumbing as retrieval quality.
4. Write `.agent/plan.md` with all of the following:
   - task, stage, base commit, evidence, and canonical document references;
   - objective and explicit non-goals;
   - allowed files and forbidden files/stages;
   - implementation steps and invariants;
   - targeted tests and any conditional real canary;
   - acceptance signals, stop conditions, and merge policy.
5. Keep production behavior independent of private Gold, case IDs, accepted-reference fixtures, and
   checkpoint-specific branches. Never lower Gate formulas or fail-closed boundaries.
6. Do not invent a fixed small Need cap. Use evidence-driven scheduling, grouping, budgets, and
   backpressure when scale control is required.
7. Set the plan state to `WAITING_FOR_HUMAN_APPROVAL`. Do not edit production code, start OpenCode,
   launch a canary, or run P3.

## Plan quality bar

The plan must be executable by one OpenCode owner in the same worktree without asking it to make
architectural decisions. If a material choice is unresolved, stop and ask the human before writing
an implementation-ready plan.

`.agent/plan.md` is a task-local execution slice, not a replacement for the canonical Stage docs.
Update the canonical docs only when the user explicitly asks to change project policy or status.
