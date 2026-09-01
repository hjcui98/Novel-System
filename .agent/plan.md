# Plan v7 — consolidate on main and run a production-path novel smoke

- State: `ACTIVE_MAIN_CONSOLIDATION_AND_PRODUCTION_SMOKE`
- Authority: user instruction on 2026-09-01 and the unified Stage 2→5 execution document.
- Change log:
  - v1–v6 — implemented and verified the unified runtime through U8-C/D/E while production defaults
    remained closed; moved U8-C real admission to fresh-evidence pending.
  - v7 (2026-09-01) — user explicitly authorized committing the current unified implementation to
    `main` and exercising the real production path from an initial author setting.

## Protected invariants

- Preserve the dirty root worktree and all unrelated user changes.
- Exclude local environments, model weights, private benchmark/novel data, runtime state, and secrets.
- Main receives only an auditable committed source tree; merge and verification happen in a new clean
  worktree.
- Production RecoveryReasoner and hot-swap remain disabled. A production smoke is not U8 admission.
- Use fresh project/run/database/object/output identities; never resume or overwrite sealed/frozen runs.

## Execution sequence

1. Audit and stage the unified worktree, excluding local-only symlinks and runtime/private data.
2. Run diff hygiene and deterministic checks, then create one consolidation commit on
   `codex/unified-agent-runtime-integration`.
3. Create a clean `main` worktree, merge the consolidation commit, resolve only evidence-backed
   conflicts, and commit the merge when required.
4. Re-run unit/contract, property/golden/regression, schema, Ruff, and strict MyPy checks on committed
   `main`.
5. Inspect the production CLI/composition contract and choose the smallest real scenario: an initial
   author brief plus one target chapter, using normal production assembly and disabled U8 features.
6. Recheck PostgreSQL/OpenSearch/MinIO/OpenTelemetry and model/embedding/reranker health, create fresh
   identities, apply migrations, run the scenario, and persist the report outside the repository.
7. Report the main commit, commands, terminal state, completed chapters, artifacts/model usage, and
   any remaining production blocker. Do not claim a formal Stage/U8 Gate from this smoke.
