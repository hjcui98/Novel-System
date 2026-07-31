# ADR-0004: Stage 2M Writer Context is the Memory read-side product

- Status: Accepted
- Date: 2026-07-29
- Depends on: ADR-0003 deterministic Memory Gateway freeze

## Decision

Formal Stage 2 memory benchmarks use `memory_benchmark.v0.2`,
`task_plan_conditioned_v1`, `writer_context.v1`, and `per_gold_v1`.

The benchmarked read-side product is `WriterContextPackage`, not
`Stage1ContextPackage` or a retrieval trace. A valid package:

- is generated from a hash-bound public task and a profile-legal Plan view;
- contains writer-facing claims in typed semantic sections;
- stores source material in a separate `EvidenceLedger`;
- has `ContextAssemblyStatus.READY`;
- never exceeds the configured Writer token budget;
- is frozen before evaluator-only Gold or future text is revealed.

`Stage1ContextPackage` remains readable for synthetic and historical artifacts,
but is marked `benchmark_quality_eligible=false`.

Arm C is assembled from the normalized union of legal A/B retrieval units under
the same budget. A B timeout is an ineligible failure artifact; it is not scored
as an Agentic result. A resulting C fallback is diagnostic only.

`visible_at_cutoff` and `author_plan_conditioned` use separate experiment/state
namespaces and separate unified reports. Cross-profile output contains deltas
only and never pools the two denominators.

## Consequences

- The production Gateway remains deterministic.
- Agentic or hybrid promotion still requires a later ADR backed by comparable
  real-model results.
- Legacy oversized contexts and coarse evidence-overlap scores cannot be used
  as Stage 2M quality evidence.
