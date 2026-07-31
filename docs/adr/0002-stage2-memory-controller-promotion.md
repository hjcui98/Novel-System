# ADR-0002: Defer bounded Memory Controller promotion

- Status: superseded by ADR-0003
- Date: 2026-07-21
- Decision owners: application architecture and independent evaluation
- Related design: `docs/stage2_memory_agents_development.md`
- Gate evidence: `reports/stage2a/current_gate_evidence.json`
- Gate report: `reports/stage2a/current_gate_report.json`
- Paired Pilot: `reports/stage2a/ztj_paired_pilot.json`

ADR-0003 supersedes this evidence-window decision after the current configuration completed the
real C1–C95 replay and the deterministic five-checkpoint gate entered the independent Evaluation
Ledger. This document remains the historical explanation for not promoting `BOUNDED_R2`.

## Context

Stage 2A permits the bounded R2 Memory Controller to become the default read path only after a same-basis, same-budget paired benchmark demonstrates stable gain on at least one predeclared held-out complex query class without safety regression. The Gateway must retain the deterministic Stage 1 path whenever that evidence is absent or non-comparable.

Five ztj checkpoints (C20, C40, C60, C80, C95) were compiled from the reviewed human-authored workspace and run against one immutable in-memory Oracle basis per case. All five deterministic-versus-bounded comparisons were comparable. Both arms achieved 1.0 Gold evidence recall and zero future leakage. The bounded arm used half as many retrieval calls in every case, and no mandatory-coverage safety regression was observed.

These cases exercise generated current-state needs. They are not a predeclared held-out complex-query subset, so the reduced call count is useful efficiency evidence but does not satisfy the accuracy-gain promotion condition. The same source workspace also lacks a 50+ chapter replay Gold partition and a continuous Curator receipt chain. Author-approved Genesis and Evaluation Ledger evidence are not yet present.

## Decision

Do not promote `BOUNDED_R2` to the default Memory Gateway path in this evidence window.

Keep the deterministic Stage 1 retrieval orchestrator as the safe runtime path. The high-level Memory Gateway may execute a bounded comparison only when explicitly requested and must select bounded output only when the comparison is comparable, safe, and sufficient; otherwise it falls back deterministically or blocks according to policy.

Do not freeze Memory Gateway v0.1 yet. The current Gate verdict is `INCOMPLETE`, promotion is `DEFER`, and `memory_gateway_frozen=false`.

## Consequences

- The five paired ztj results count as real comparability, leakage, safety, and efficiency evidence.
- They do not count as held-out complex-query accuracy gain.
- Writer, Planner, and Editor generation flows cannot assume bounded R2 is the default.
- A later evidence window may supersede this decision after the remaining Gate blockers are closed.
- Any attempt to set bounded R2 as default before a superseding Gate report is an architecture violation.

## Evidence required to supersede

1. Author-approved Genesis produced through Proposal, Validation, and Approval.
2. A predeclared held-out complex query subset with stable paired gain at equal basis and budget.
3. A continuous 50+ chapter Curator replay with Gold, rejection-pollution, and freshness evidence.
4. One continuous receipt chain for C20/C40/C60/C80/C95 with future-isolation attestations.
5. A complete independent Evaluation Ledger and a resulting PASS or CONDITIONAL PASS Gate report.
