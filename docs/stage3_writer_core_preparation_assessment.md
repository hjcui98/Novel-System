# Stage 3 Writer Core preparation assessment

> Lifecycle: `ACTIVE`
>
> Date: 2026-07-31
>
> Stage: Stage 3 — Writer Core and Generation Quality
>
> Previous name: Stage 2B
>
> Assumption: Stage 2A development is complete with `CONDITIONAL_PASS`
>
> Current decision: `PREPARATION_GO / INTEGRATION_HOLD`

## 1. Assessment result

Stage 3 preparation can proceed now because the Writer boundary, candidate-only artifact model,
deterministic Memory default, future-isolation contract, and controlled commit boundary are stable.

Stage 3 integration cannot be declared ready. The isolated Writer implementation predates the
Stage 2M Writer Context product, exists only as uncommitted changes in a separate worktree, and has
not passed current-main quality gates. Writer semantic quality has never been evaluated.

The appropriate transition is:

```text
Stage 2A CONDITIONAL_PASS
  -> Stage 3 preparation and isolated engineering
  -> current-contract rebase/migration
  -> Writer DRAFT engineering gate
  -> preregistered semantic experiment
  -> only then Editor/Curator/Commit integration
```

## 2. Existing implementation inventory

Legacy worktree:

```text
/home/cuihengjia/agent/novel/NS-stage2b-writer
branch: codex/stage2b-writer-shadow
base: ca9c78e
state: uncommitted and unmerged
```

Implemented areas:

| Area | Evidence | Readiness |
|---|---|---|
| Writer domain | `domain/generation.py` | Strong isolated baseline |
| Writer agent | `agents/writer.py` | DRAFT/CONTINUE/MAJOR_REWRITE contracts present |
| Generation service | `services/writer_generation.py` | Candidate artifacts, typed failures, replay/idempotency present |
| DRAFT handoff | `services/writer_draft_integration.py` | Deterministic-only legacy handoff present |
| Prompt/Skill | `prompts/writer_*`, three Writer skills | Present; current-context review required |
| Schema/export | legacy `schemas/stage2b`, exporter | Present; namespace migration required |
| Tests | focused unit/contract/shadow/integration tests | Historical evidence; rerun required |
| Acceptance docs | three legacy Stage 2B documents in the worktree | Historical, not current-main acceptance |

Read-only verification on 2026-07-31 ran the five focused Writer unit/contract suites with model
calls forbidden and repository cache disabled: `101 passed in 8.52s`. This verifies the isolated
code path only; it does not test current-main integration or semantic quality.

The implementation correctly preserves several long-lived invariants:

- Writer output is untrusted;
- `DraftArtifact` is candidate-only and never a `TextRoot`;
- Writer has no raw retrieval, database, Memory write, Commit, or Canon permission;
- declared memory hints are weak sidecar signals, not Evidence or MemoryPatch;
- future/evaluator/Gold taints fail closed;
- failed terminals do not contain a successful Draft/receipt;
- continuation and rewrite preserve parent lineage.

## 3. Required migration before merge

### 3.1 Writer Context contract

The isolated implementation consumes `Stage1ContextPackage`. ADR-0004 now defines
`WriterContextPackage` as the Memory read-side product. Stage 3 must consume the latter directly or
use a small, versioned adapter whose output is exactly the Writer-facing package.

Do not inject the Evidence Ledger or raw retrieval trace into the Writer prompt by default.
Additional evidence expansion must be an explicit, budgeted Memory request.

### 3.2 Stage naming and schema namespace

The following legacy identifiers must not become new public Stage 3 contracts:

```text
schemas/stage2b/
scripts/export_stage2b_schemas.py
scripts/run_stage2b_writer_shadow.py
tests/...stage2b...
reports/stage2b/...
```

Preferred new identifiers use `stage3`. If compatibility with an already-published schema is
needed, publish an explicit version mapping instead of silently reusing a Stage 2B label.

The existing branch and worktree names may remain unchanged as provenance.

### 3.3 Current Gate semantics

The legacy integration requires `ControllerStopReason.SUFFICIENT` and rejects any unresolved gap.
The current Stage 2M contract distinguishes:

- Writer Context assembly readiness;
- typed unresolved gaps;
- evidence traceability;
- benchmark quality.

Stage 3 must define which gap classes block generation and which may be surfaced to Writer as
explicit uncertainty. It must not equate Context `READY` with semantic benchmark PASS.

### 3.4 Rebase and generated contracts

The current main worktree has substantial Stage 2M domain and schema changes after the Writer
worktree was created. The Writer branch must:

1. rebase onto a clean, committed Stage 2M baseline;
2. resolve `domain.stage2` enum/schema overlap intentionally;
3. regenerate Stage 3 schemas;
4. rerun 100% branch coverage, Ruff, strict MyPy, schema reproducibility, and full deterministic
   repository tests;
5. record the exact merged commit and configuration fingerprint.

## 4. Technical readiness by boundary

| Boundary | Readiness | Main issue |
|---|---|---|
| Domain and artifact lineage | High | Rebase/schema namespace |
| Model/Agent wrapper | Medium-high | Prompt and current model contract review |
| Memory-to-Writer handoff | Medium | Must migrate to WriterContextPackage |
| Writer semantic evaluation | Low | No preregistered real Writer benchmark result |
| Editor REVIEW/REPAIR | Not implemented in current main | Requires DraftArtifact contract first |
| Curator reconciliation | Not integrated | Must preserve independent extraction |
| Candidate ChangeBundle/Commit | Not integrated | Must remain blocked until validation |
| Runtime/checkpoint recovery | Partial | Shadow idempotency exists; chapter graph absent |
| Production enablement | Blocked | No semantic or long-run evidence |

## 5. Resource envelope

This is a preparation estimate, not the second-stage module assignment.

### 5.1 People

Minimum practical team:

- one architecture/domain owner for contracts, lineage, and authority boundaries;
- one Writer/runtime engineer for generation, Model Gateway, prompts, and recovery;
- one evaluation engineer for Writer benchmark, blind review, and statistical reporting;
- shared support from the Memory/Curator owner during handoff and reconciliation work.

With two full-time engineers plus shared review, expect roughly 6-8 calendar weeks to reach a
credible Stage 3 semantic gate. With four coordinated engineers, the same scope may fit 3-5 weeks,
subject to model-run latency and human evaluation.

Initial engineering envelope: approximately 30-45 engineer-days, excluding large-scale human
literary annotation.

### 5.2 Compute and services

- one isolated Writer model endpoint; the existing Qwen 3.6 endpoint can serve initial experiments
  but single concurrency limits parallel evaluation;
- the frozen deterministic Memory Gateway and real retrieval services;
- separate project/database/index namespaces for Writer experiments;
- an independent evaluator model endpoint or blinded human review path; the tested Writer model
  must not be its only judge;
- artifact storage for prompts, contexts, drafts, sidecars, receipts, and evaluation records;
- no Writer permission to PostgreSQL Canon tables or OpenSearch.

### 5.3 Data

Required before the semantic gate:

- public WritingTaskContract and frozen WriterContextPackage cases;
- private constraint/plan/continuity evaluation annotations;
- future-text and evaluator-only physical isolation;
- baseline arms using the same Writer model and generation budget;
- a held-out partition not used for prompt or skill tuning;
- human review guidance for literary non-inferiority.

## 6. Preparation exit criteria

Stage 3 preparation is complete only when:

1. the isolated Writer changes are committed or represented by a reviewable patch;
2. all public Stage 2B names have a Stage 3 migration decision;
3. Writer consumes `WriterContextPackage`;
4. fake/offline DRAFT runs pass current-main quality gates;
5. no Writer path can reach raw retrieval, Memory write, Commit, or Canon;
6. a real-model DRAFT experiment contract, arms, metrics, sample scope, and evaluator independence
   are preregistered;
7. rollback and typed failure behavior are documented;
8. resource owners and isolated runtime namespaces are assigned.

These criteria authorize Stage 3 implementation and semantic experiments. They do not authorize
production or Canon writes.
