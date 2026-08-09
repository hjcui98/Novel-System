# Repository Guidelines

## Project Structure & Module Organization

Production code lives in `src/novel_agent/`. Keep business models in `domain/`, orchestration in
`services/` and `runtime/`, external integrations in `adapters/`, and interfaces in `ports/`.
API, CLI, agents, prompts, skills, and tools have matching subpackages. Tests mirror behavior under
`tests/unit/`, `tests/contract/`, `tests/integration/`, `tests/model/`, `tests/property/`,
`tests/golden/`, and `tests/regression/`. Database changes belong in `migrations/versions/`;
versioned JSON contracts are in `schemas/stage0` through `schemas/stage2`. Use `scripts/` for
operational entry points, `docs/adr/` for architecture decisions, and `benchmarks/` for benchmark
inputs. Do not commit runtime data from `tmp/`, `volumes/`, or the local `.conda-env/`.

## Build, Test, and Development Commands

- `make bootstrap` creates the Python 3.12 environment, installs locked development dependencies,
  and initializes local configuration.
- `make quality` runs Ruff linting and format checks, strict MyPy, and deterministic Pytest tests.
- `.conda-env/bin/pytest tests/unit/test_api.py` runs a focused test; add `--no-cov` only for quick
  local iteration.
- `make integration` runs tests against isolated real infrastructure; use
  `make INFRA_BACKEND=docker integration` for Docker parity.
- `make stage0` starts services, applies Alembic migrations, and runs the replayable demo.
- `make stage1-smoke` exercises the deterministic Stage 1 memory kernel.

## Coding Style & Naming Conventions

Use four-space indentation, Python 3.12 typing, double quotes, and a 100-character line limit.
Ruff enforces imports and common correctness rules; MyPy runs in strict mode. Name modules and
functions `snake_case`, classes and Pydantic models `PascalCase`, and constants `UPPER_SNAKE_CASE`.
Keep domain code independent of infrastructure and inject adapters through typed ports. Run
`.conda-env/bin/pre-commit run --all-files` before submitting.

## Testing Guidelines

Pytest, Hypothesis, and pytest-cov are configured in `pyproject.toml`. Name files `test_*.py` and
place fixtures in `tests/fixtures/`. The default suite requires 100% branch coverage. Mark
infrastructure tests `integration` and endpoint-dependent tests `model_required`; deterministic
tests must not call model endpoints. Add regression coverage for every bug fix and contract tests
when changing schemas or public boundaries.

## Minimum-Sufficient Engineering

Do not overengineer. Implement the smallest mechanism that closes a demonstrated requirement or
failure while preserving the repository's existing invariants. Prefer removing, merging, reusing,
configuring, or extending the current owner before adding another abstraction, service, state
machine, storage system, queue, configuration language, control plane, report family, or document.

Every new component or contract must name its current caller, responsible layer, protected
invariant, and acceptance evidence. “It may be useful later” is not sufficient; speculative
generality stays deferred until a real use case or benchmark proves it necessary. Keep one source of
truth, one owner per responsibility, and one implementation path for the same semantics.

Minimum-sufficient engineering applies to mechanism size, not evidence strength. It never permits
skipping strict typing, validation, permission and leakage boundaries, failure semantics,
observability required for diagnosis, migrations, regression tests, reproducibility, or the active
Stage gate. A smaller change that weakens those properties is incomplete, not simpler.

## Commit & Pull Request Guidelines

History follows concise Conventional Commit subjects, commonly `fix(curator): ...`,
`fix(controller): ...`, and `feat: ...`. Keep each commit focused and describe behavior in the
imperative mood. Pull requests should explain the problem and solution, list verification commands,
link relevant issues or ADRs, and call out migrations, schema changes, configuration changes, or
benchmark evidence. Include screenshots only for visible API or reporting changes; never include
secrets from `.env` or private benchmark data.

## Codex–OpenCode Development Workflow

Canonical Stage documents remain the source of truth. `.agent/plan.md` carries Codex's technical
direction for the current substantial task.

- Codex is the top-level designer and architect. It determines root cause, effective design,
  responsible layer, allowed direction, success signals, stop conditions, final acceptance, and
  merge decisions.
- Human approval is the act of starting OpenCode in this repository and invoking `/implement`.
- OpenCode default `build` is the execution owner and sole code writer. It implements, tests, uses
  and monitors real APIs, analyzes artifacts, repairs within Codex's direction, and reports in
  `.agent/implementation.md` without returning after every small step. It never commits or merges.
- Codex alone creates or edits architecture, design, planning, active execution, current-status,
  ADR, `.agent/task.md`, `.agent/plan.md`, and `.agent/review.md` files. OpenCode reads them as
  authority; it writes another result or summary document only when the plan explicitly names it.
- Codex reviews the completed implementation and evidence. If repair is needed, Codex gives the
  technical direction in the existing review/plan and OpenCode continues; a new design document is
  created only for a materially new decision, and there is no arbitrary repair-count limit.
- Use one shared worktree for the serial flow. Create separate worktrees only for genuinely
  independent tasks with distinct owners and file boundaries.
- Stage boundaries are fail-closed: a Stage 2M task must not modify or merge Stage 3 implementation.
- The implementation must follow the minimum-sufficient engineering rule above. If satisfying the
  plan appears to require a parallel framework, speculative platform, or unrelated cross-stage
  expansion, OpenCode returns evidence to Codex instead of building it opportunistically.

The normal interaction is deliberately short:

1. Discuss the design with Codex; Codex updates an existing upper-level document when needed and
   prepares or refreshes `.agent/plan.md` for the substantial implementation task.
2. Invoke `/implement` in OpenCode. The default `build` agent completes implementation, tests, real
   API work, monitoring, evidence-driven repair, and `.agent/implementation.md` reporting.
3. Ask Codex to `review`. Codex writes `.agent/review.md` and either accepts the work or gives the
   next technical direction.
4. For `REPAIR`, invoke `/implement` again in the same worktree. For `PASS`, Codex integrates the
   accepted evidence into the existing project documents and performs the authorized merge.
