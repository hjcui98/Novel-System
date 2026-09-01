# Current task

- Task: consolidate the verified unified long-running novel-agent implementation onto `main`,
  create an auditable commit, and run a fresh production-path smoke from an initial author setting.
- Authority: the user's explicit 2026-09-01 instruction, together with
  `docs/stage2_to_stage5_unified_long_running_agent_integration_execution_20260818.md`,
  `docs/project_status.md`, and ADR-0003/0006/0007/0008/0009/0010.
- Source worktree: `/home/cuihengjia/agent/novel/NS/.worktrees/unified-agent-runtime-integration`
  on `codex/unified-agent-runtime-integration`.
- Integration target: `main`, using a separate clean worktree so the dirty root worktree and its
  independent user changes remain untouched.

## Current evidence

- The unified implementation includes Stage 2 Memory, Stage 3 Writer, Stage 4 Planner, Stage 5
  long-running Runtime, U6 endurance/fault/readout infrastructure, U7 Temporal candidates, and the
  production-disabled U8-C/U8-D/U8-E recovery/evolution mechanisms.
- The latest relation-gap repair preserves the explicit ordered subject/predicate/object triple and
  prevents unrelated anchors from falsely closing Planner Memory facets.
- Deterministic verification is green: `tests/unit + tests/contract` is `2841 passed` and
  property/golden/regression is `21 passed`; related U8/relation verification, strict MyPy, Ruff,
  schema contracts, and compileall pass.
- Local environment/model/private-benchmark symlinks are excluded from source control.

## Required result

- Commit the unified source, schemas, migrations, prompts/skills, tests, and governing documentation.
- Integrate that commit onto `main` without modifying or stashing the dirty root worktree.
- Re-run deterministic verification from the committed `main` tree before attempting real services.
- Check native services and model endpoints immediately before a fresh production smoke.
- Run the ordinary production composition path from an initial author brief with a fresh project/run,
  database/object/output identity. Keep RecoveryReasoner and production hot-swap disabled unless a
  separate admission result authorizes them.
- Record the exact terminal, artifacts, model usage, completed chapters, and any fail-closed reason.

## Stop conditions

- Do not commit local model weights, environments, private novel/benchmark content, runtime objects,
  databases, logs, or temporary outputs.
- Do not overwrite the root worktree's independent changes.
- Do not relabel a transport/resource failure as a semantic failure or a smoke result as a formal Gate.
- Do not activate U8 reasoner/evolution in production merely because the code is present.
