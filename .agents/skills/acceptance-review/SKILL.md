---
name: acceptance-review
description: Review the current implementation diff against an approved .agent/plan.md and canonical stage invariants, then issue PASS, FAIL, BLOCKED, or STOP. Use when Codex must independently accept OpenCode work, authorize one repair pass, or make the final merge and stage-progression decision.
---

# Acceptance Review

Act as the Codex reviewer and decision owner. Review before making any integration change.

## Procedure

1. Read `AGENTS.md`, `.agent/task.md`, `.agent/plan.md`, `.agent/implementation.md`, Git status/diff,
   and the plan's canonical document references.
2. Verify stage boundary, base commit, allowed-file scope, implementation evidence, and actual test
   outputs. Rerun only the cheapest check needed to resolve contradictory or missing evidence.
3. Inspect mechanism quality, not just benchmark movement. Reject evaluator gaming, broad evidence
   binding without valid containment/provenance, hidden fixed limits, Gold-derived runtime behavior,
   weakened fail-closed rules, and non-comparable endpoint runs.
4. Write `.agent/review.md` with one verdict:
   - `PASS`: every criterion holds; list evidence and residual non-blocking risks;
   - `FAIL`: list concrete file/line findings and the exact required repair;
   - `BLOCKED`: evidence or environment prevents a valid decision;
   - `STOP`: a second review failed or a new architectural decision is required.
5. A first `FAIL` permits one OpenCode `/implement` repair. A second `FAIL` must be `STOP` and return
   to the human.
6. On `PASS`, integrate only when the plan says `Merge policy: CODEX_ON_PASS`; otherwise stop for
   explicit human merge approval. Never promote the next Stage or run P3 merely because code review
   passed.

The approved plan defines task criteria; canonical safety, stage, budget, cutoff, scope, and
provenance invariants always remain mandatory.
