---
name: acceptance-review
description: "Let Codex perform a read-only architectural review of OpenCode's completed, stable handoff against the upper-level design and existing evidence, then accept it or provide the next repair direction without rerunning implementation, tests, benchmarks, or real APIs."
---

# Acceptance Review

Use this skill for `/review`. Codex is the architect and final reviewer; OpenCode's report is
evidence, not the acceptance decision.

## Require a Stable Handoff

Start acceptance only after OpenCode has explicitly completed the assignment and handed back a
stable code, report, and artifact set. If OpenCode is still writing files, running tests, calling a
real API, producing benchmark artifacts, or marking the implementation as continuing:

- stop the review and report that implementation has not been handed over;
- do not wait for, poll, monitor, resume, or complete the active execution;
- do not write an acceptance result against a moving snapshot.

## Review Against the Existing Hierarchy

1. Read `AGENTS.md` and resolve the authoritative document hierarchy through `docs/README.md`.
2. Read the applicable upper-level architecture, design, status, and active execution sections
   before `.agent/plan.md` and the latest `.agent/review.md` repair addendum.
3. Inspect `.agent/implementation.md`, the actual diff, relevant tests, exact real-run artifacts,
   and runtime evidence. Do not accept a prose claim that is not supported by the repository or
   artifacts.
4. Decide whether the implementation solved the diagnosed problem at the correct architectural
   layer. Metric movement, reference matching, report completeness, or one successful run is not a
   substitute for a sound mechanism.
5. Verify the applicable safety, cutoff, scope, provenance, budget, Stage, and other project
   invariants from the stable handoff evidence.

Keep acceptance read-only. Use repository and artifact inspection commands such as diff, search,
log reading, and existing-result parsing. Do not run or rerun project tests, quality suites, model
calls, real APIs, benchmarks, replay jobs, or long-running processes, and do not monitor an active
OpenCode run. Independent review means interpreting the evidence independently, not reproducing
OpenCode's execution.

When evidence is missing, stale, internally inconsistent, or produced by a different code version,
return `REPAIR` and specify the exact evidence OpenCode must produce. Do not generate the missing
evidence on Codex's behalf.

## Give One Clear Decision

Write `.agent/review.md` with exactly one outcome:

- `PASS`: the approved design is implemented and demonstrated by sufficient evidence.
- `REPAIR`: identify the failed mechanism or unsupported claim, explain the next architectural
  direction, and state the evidence that would demonstrate the repair.
- `BLOCKED`: a genuine user/architecture decision or unavailable external dependency prevents a
  valid judgment or implementation.

On `REPAIR`, keep the same task and let OpenCode run `/implement` again when the correction remains
inside the existing upper-level design. Put the concrete diagnosis and direction in
`.agent/review.md`; do not create a new plan or remediation document merely because the first
implementation failed. Update `.agent/plan.md` only when its task-level direction materially
changes. If the upper-level architecture itself must change, update the appropriate existing
design or active execution document first, and create a new document only when the current
hierarchy cannot represent the decision.

On `PASS`, integrate accepted facts into the existing `docs/project_status.md` and applicable active
execution document when repository status needs updating. Add or index a dated result document
only when immutable evidence needs a durable record. Acceptance does not automatically promote a
Stage or declare a formal Gate PASS.

Do not impose arbitrary repair counts or procedural failures. Judge whether the engineering design
worked; if it did not, give OpenCode the next useful technical direction.
