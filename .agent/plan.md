# Stage 2M Semantic Memory Repair Plan

- State: `READY_FOR_IMPLEMENTATION`
- Scope: Stage 2M only
- Authority: `docs/stage2_memory_semantic_repair_execution_20260813.md`

## Assignment

OpenCode must implement the complete repair round defined by the authority document, following its
A -> E dependency order, named code owners, product boundaries, acceptance criteria, clean-Genesis
rerun requirement, and stop conditions.

Treat the authority document as the complete technical direction. Do not create a parallel retrieval,
packing, graph, projection, or readiness path. Preserve the behaviors that the document marks as
already correct, keep Stage boundaries fail-closed, and apply minimum-sufficient engineering.

Implementation priority is the working product path: valid Planner Needs survive admission; unresolved
facets drive retrieval until exact L0 evidence or exhaustion; exact slices are packed fairly into the
Writer-visible package; durable State/Event/Obligation/Relation memory is written and retrievable. Keep
report/schema work limited to what this path needs to run and explain failures.

## Completion

OpenCode owns implementation, focused regression and contract tests, the required real smoke, artifact
analysis, and repairs that remain within the documented direction. When the implementation is stable,
record `READY_FOR_IDENTITY_FREEZE` in `.agent/implementation.md`; after Codex/human fixes the Stage 2M
source identity, continue with the required clean-Genesis real run. Record changed owners, commands,
results, product-level evidence for P001-P005, unresolved failures, and stop-condition decisions there.

Do not claim completion from mechanical gates alone. A typed gap may coexist with a mechanically READY
package, but completion requires every mandatory Need facet to be closed by exact L0 evidence.
