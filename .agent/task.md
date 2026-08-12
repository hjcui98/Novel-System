# Current task

- Task: integrate the existing Stage 3 Writer, Stage 4 Planner, and Stage 5 Runtime candidates into
  one executable, candidate-only-to-trusted-commit creative loop with bounded two-lane concurrency.
- Owner: Codex implements, tests, diagnoses, and reports in this repository.
- Authorized: 2026-08-12 by the user.
- Stage 2 boundary: frozen product/executable semantics at `408a46f`; do not edit Stage 2 Memory,
  evidence-first Writer Context, retrieval/selection, benchmark, or accepted Gate behavior.

## User requirements

- Record the integration and concurrency design in the existing architecture/design/execution
  documents before changing code.
- Integrate Stage 3, Stage 4, and Stage 5; do not build a parallel framework.
- Implement a complete Plan candidate → acceptance → Plan Commit/Freshness → Writer candidate →
  acceptance → Draft Commit/Freshness → next/replan loop.
- Add natural concurrency for independent work, especially Writer N with future Planner lookahead or
  historical maintenance, while keeping same-project Canon writes serial.
- Run focused/simple deterministic and offline tests after integration.
- The local Qwen endpoint at `8002` may be occupied by the Stage 2 benchmark. Do not preempt,
  restart, reconfigure, cancel, or load-test it. If exclusive capacity cannot be established by a
  non-mutating health/usage check, skip real API tests and report them as deferred.
- Preserve existing dirty work and never include `objects/`, local `benchmarks/`, `tmp/`, or private
  runtime data in delivery.

## Current result

- Code state: `STAGE345_ENGINEERING_CLOSED_LOOP_READY` on main.
- Implemented: real Stage 4 adapter, shared Stage 3/4 Context owner, two-task dispatcher, Writer +
  Planner-lookahead overlap, post-Draft promote/replan/supersede, trusted PlanRoot/TextRoot
  materializers, immutable acceptance binding, and serial acceptance/Commit/Freshness.
- Evidence: 220 checks passed in the concentrated run; 9 merge-alignment failures were repaired and
  their exact selectors passed; changed code Ruff and strict MyPy pass.
- Additional focused evidence: real Stage 4 adapter → PlanRoot → real Stage 3 Writer/Editor/Observer
  → TextRoot closure 1 passed; lookahead 1 passed; acceptance guards 2 passed; Ruff/strict MyPy pass.
- Deferred: 8002 real API because endpoint ownership could not be established; Stage 3/4 semantic
  Gates and the Stage 5 real multi-chapter Gate.
