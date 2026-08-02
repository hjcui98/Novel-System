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

## Commit & Pull Request Guidelines

History follows concise Conventional Commit subjects, commonly `fix(curator): ...`,
`fix(controller): ...`, and `feat: ...`. Keep each commit focused and describe behavior in the
imperative mood. Pull requests should explain the problem and solution, list verification commands,
link relevant issues or ADRs, and call out migrations, schema changes, configuration changes, or
benchmark evidence. Include screenshots only for visible API or reporting changes; never include
secrets from `.env` or private benchmark data.

## Codex–OpenCode Development Workflow

Canonical Stage documents remain the source of truth. `.agent/plan.md` is only the approved slice
for one implementation cycle.

- Codex owns planning, architecture, stage boundaries, acceptance, and merge decisions. Invoke
  `$scoped-plan` to prepare `.agent/plan.md`; it must stop at human approval.
- Human approval is the act of starting OpenCode in this repository and invoking `/implement`.
- OpenCode is the sole writer for that cycle. It follows `.agent/plan.md`, writes
  `.agent/implementation.md`, and never launches another agent or commits/merges.
- Codex invokes `$acceptance-review` against the actual diff and writes `.agent/review.md`.
- One failed review permits one `/implement` repair. A second failure stops for human direction.
- Use one shared worktree for the serial flow. Create separate worktrees only for genuinely
  independent tasks with distinct owners and file boundaries.
- Stage boundaries are fail-closed: a Stage 2M task must not modify or merge Stage 3 implementation.
