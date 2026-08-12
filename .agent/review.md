# Codex acceptance review

- Outcome: `REPAIR`
- Reviewed: `2026-08-11 +08:00`
- Scope: Stage 5 A-layer repair for `runtime advance` and the real Stage 3 Writer isolated E2E
- Review mode: read-only acceptance; Codex did not rerun tests, quality, infrastructure,
  migrations, pre-commit, benchmarks, model endpoints, or real APIs

## Decision

The submitted verification evidence is accepted for the exercised final tree: the focused 61 tests,
`1968 passed, 9 deselected`, 100% branch coverage, strict MyPy, Ruff/format, pre-commit, Stage 5
integration, migration symmetry, and `git diff --check`. The known R1 count failure remains a
base-reproduced non-regression. The fixed declarations remain correct:
`real_stage4_adapter=false`, `creative_product_gate=NOT_RUN`, and
`production_activation=BLOCKED`.

The repair is not yet accepted as `ISOLATED_KERNEL_PASS`. Static inspection found one target-binding
bug in the new CLI path and one mismatch between the reported E2E chain and the assertions actually
present in the test. These are bounded repairs within the existing design.

## Required repair

1. Bind `runtime advance` to both explicit identities. `--run-id` is currently parsed but never
   used: `CreativeDispatcher` is constructed with only `project_id`, and
   `RuntimeTaskQueryRepository.next_ready()` filters only by project. With two ready runs in the
   same project, the command may advance the wrong run. Extend the existing query/dispatcher owner
   with an optional `run_id` filter and pass `RunId(args.run_id)` from the CLI. Do not add a second
   task-selection path. Add a regression containing two ready runs under one project; advancing one
   run must leave the other unchanged. Also cover a project/run mismatch. The current
   `test_runtime_advance_rejects_missing_identity` only proves argparse rejects omitted operational
   arguments; it does not prove identity binding.
2. Complete the real-writer E2E through the boundary claimed in the handoff. The new test correctly
   constructs `WriterContextLoopService`, wraps it in `Stage3WritingLeafAdapter`, and reaches
   `WAITING_DRAFT_ACCEPTANCE`. It then ends. Extend the same test to submit the persisted Draft
   candidate acceptance, advance the resulting Draft Commit task, advance Projection/Freshness,
   and assert exact freshness plus the expected terminal/current Commit and immutable lineage. This
   is necessary to demonstrate the requested real-Writer result → candidate acceptance → Commit →
   Projection/Freshness composition; the existing fake-writer chain cannot substitute for this
   adapter-level composition proof.

## Evidence anchors

- `src/novel_agent/cli.py`: `args.run_id` is not referenced inside the `advance` branch; dispatcher
  construction supplies only `project_id`.
- `src/novel_agent/runtime/creative_dispatcher.py`: task polling passes only the optional project
  filter.
- `src/novel_agent/adapters/postgres/runtime.py`: `next_ready()` has no run filter.
- `tests/unit/test_stage5_cli.py`: the new happy-path test uses one target ready run and has no
  same-project/wrong-run regression.
- `tests/integration/test_stage5_real_writer_e2e.py`: the final assertion is
  `WAITING_DRAFT_ACCEPTANCE`; no Draft acceptance/Commit/Freshness operation follows it.

## Retest boundary

After repair, run the affected dispatcher/query/CLI and real-writer E2E tests, then run the unified
A-layer acceptance sequence once. The evidence must show the same-project run-selection regression
and the completed real-adapter Draft acceptance/Commit/exact-Freshness chain. Do not rerun B-layer
product/model gates.

The untracked `objects/` and `benchmarks/` directories are runtime/local data and are outside the
accepted delivery; they must not enter a commit. No B-layer integration, product gate, production
activation, merge, or commit is accepted by this review.
