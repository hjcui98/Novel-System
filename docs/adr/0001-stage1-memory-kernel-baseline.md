# ADR-0001: Stage 1 Memory Kernel baseline

- Status: accepted engineering baseline
- Date: 2026-07-21
- Scope: Stage 1A read side and Stage 1B write side

## Decision

Stage 1 uses intent-routed retrieval over typed, logically separate Anchor and Grounded pools.
Exact/current-state requests use R1 without RRF; semantic history uses Anchor BM25/Dense first and
expands only selected anchors to L0 evidence; exact quote and continuous style requests may address
Grounded units directly. Multi-channel ranks are fused once in application code with complete rank,
candidate-count and reason traces.

Canonical commits and derived search state are separated. Every accepted commit writes one
`projection_outbox` row in the same PostgreSQL transaction. An idempotent worker publishes one exact
`DerivedSnapshotLite` per source commit. A replay may proceed only when Canonical, R1 basis, alias and
snapshot source agree, or when the result is explicitly waiting, degraded, blocked, or covered by a
recorded manual approval.

Chapter write-back is Candidate-first: Curator creates evidence-bound `ObservedChangeSet` values,
WorldOverlay materializes a non-canonical candidate, Validator runs deterministic fail-closed checks,
and only a passed bundle reaches `CommitService`. Assertion, rumor, dream, prediction and hypothetical
records are not promoted to accepted world facts without being detected.

## Consequences

- Indexes are replaceable derived products, never Canonical truth.
- Retrieval documents carry source commit, snapshot and retrieval-unit type.
- Mandatory context closure cannot be silently trimmed by an optional token budget.
- Private future text is evaluator-only and is excluded before context construction freezes.
- The rule-driven Curator is a deterministic baseline and adapter seam, not a claim that production
  extraction quality is solved.
- The checked-in synthetic 20→3 and 21→22→23 fixtures are engineering tests only.

## Promotion condition

This baseline can be frozen as Memory Kernel v0.1 only after a user-supplied, authorized real
`BenchmarkBundle` passes both read-side targets and at least 50 chapters of Gold-labelled continuous
replay. Until then Stage 2 remains blocked by the formal execution plan.
