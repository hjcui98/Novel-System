# Project status

> Lifecycle: `AUTHORITATIVE`
>
> Status date: 2026-07-31 +08:00
>
> Code baseline: `ca9c78e` plus the current dirty Stage 2M worktree fingerprint
>
> Canonical naming: `docs/adr/0005-stage-numbering-and-document-lifecycle.md`

## 1. Executive status

The project has completed Stage 2A development and accepted its infrastructure/safety result as
`CONDITIONAL_PASS`. The deterministic real-hybrid Memory Gateway is frozen as the safe default.
Agentic retrieval remains experimental.

Stage 2M is running a post-gate Writer Context quality program. Its code and deterministic
ten-checkpoint execution path exist, but Gate M4 quality is not met and the WP8 A/B/C comparison is
incomplete. This does not reopen Canon safety or C95 infrastructure acceptance; it blocks claims
that the Memory benchmark or agentic promotion has passed.

Stage 3 preparation is active. Writer Core has a substantial isolated implementation in a separate
worktree, but it is uncommitted, unmerged, based on the former Stage 2B namespace, and integrated
against the legacy `Stage1ContextPackage` rather than the accepted `WriterContextPackage`.

## 2. Stage status

| Stage | Development status | Quality/gate status | Default or next action |
|---|---|---|---|
| Stage 0 | Complete | PASS | Frozen foundation |
| Stage 1A/1B | Engineering complete | Historical formal gate absorbed into Stage 2A evidence | Maintain regression coverage |
| Stage 2A | Development complete | CONDITIONAL_PASS | Freeze deterministic Memory Gateway |
| Stage 2R | Complete | Physical index/basis gate passed | Maintenance only |
| Stage 2W | Complete | Canon safety and recovery evidence accepted | Maintenance only |
| Stage 2M | Active at WP8 diagnostic | M4 not passed; M5 incomplete | Repair task-to-evidence quality and finish typed A/B/C |
| Stage 3 | Preparation active | Engineering integration pending; semantic gate not run | Rebase/migrate isolated Writer Core |
| Stage 4 | Planned | Not run | Blocked by Stage 3 semantic evidence |
| Stage 5 | Planned | Not run | Blocked |
| Stage 6 | Planned | Not run | Blocked |
| Stage 7 | Planned | Not run | Blocked |

## 3. Accepted Stage 2A evidence

- Genesis plus C1-C95 produced 96 accepted commits.
- The C20/C40/C60/C80/C95 chain is consistent.
- Future leakage and future-isolation failures are zero in the accepted C95 window.
- R1, Anchor, Grounded, hierarchy, dense, lexical, reranker, and typed-graph projections have real
  physical-index evidence.
- rejected or exhausted proposals do not create partial Canon commits.
- deterministic real-hybrid is the frozen default; agentic is opt-in only.

The accepted evidence is recorded by `docs/stage2_memory_gate_c95_acceptance.md` and ADR-0003.

## 4. Stage 2M current execution

### 4.1 Completed

- `memory_benchmark.v0.2` public/private task boundary;
- task/plan-conditioned Need generation;
- Writer Context and Evidence Ledger separation;
- strict Writer token budget and typed assembly failures;
- per-Gold evaluation and stage-loss diagnostics;
- two isolated information profiles;
- deterministic Arm A at both profiles and all five checkpoints;
- zero reported contradictions and zero future leakage in the current diagnostic window.

### 4.2 2026-07-31 WP8 diagnostic result

The StableId overflow in `controller_legal_actions.py` was fixed for long
`need_id + step_id` combinations. All ten checkpoint jobs produced Arm A results.

`author_plan_conditioned`:

| Checkpoint | Weighted coverage | Mandatory hit |
|---|---:|---:|
| C20 | 0.435 | 0.600 |
| C40 | 0.000 | 0.000 |
| C60 | 0.210 | 0.417 |
| C80 | 0.079 | 0.071 |
| C95 | 0.071 | 0.200 |

`visible_at_cutoff`:

| Checkpoint | Weighted coverage |
|---|---:|
| C20 | 0.000 |
| C40 | 0.409 |
| C60 | 0.000 |
| C80 | 0.000 |
| C95 | 0.000 |

Arm B/C artifacts exist only for a subset of the early checkpoints. C60 and later ran Arm A only
after the deterministic gate or Agentic path prevented a valid comparison. A/B/C execution beyond
that subset is not a completed M5 result.

### 4.3 Stage 2M blockers

1. quality is far below Gate M4's 95% current-state and operational/plan targets and 90% historical
   recall target;
2. B/C does not cover both profiles and all five checkpoints;
3. some Agentic failures are silent or represented as Arm A-only execution instead of an explicit
   typed skip/failure artifact;
4. trace completeness and cutoff-safe provenance remain incomplete in several cases;
5. current scenario lifecycle reports still contain incomplete checkpoint/freeze status;
6. no stable Agentic or hybrid quality gain without safety regression has been demonstrated.

## 5. Stage 3 preparation status

The current Stage 3 scope, minimal generation loop, four development workstreams, and restrained
verification strategy are defined in `docs/stage3_writer_core_overall_design.md`. Four detailed
development execution documents and one independent acceptance document are now available.
Personnel assignment and code execution remain `NOT_STARTED`.

The legacy worktree `/home/cuihengjia/agent/novel/NS-stage2b-writer` contains uncommitted
implementation for:

- Writer `DRAFT`, `CONTINUE`, and `MAJOR_REWRITE` contracts;
- candidate-only `DraftArtifact`, sidecar, lineage, typed terminals, and idempotency;
- Writer AgentSpec, prompts, skills, and zero raw-retrieval tool policy;
- offline shadow runner and schema export;
- a DRAFT-only deterministic Memory Gate handoff;
- focused unit/contract tests and historical engineering acceptance records.

The worktree's five focused Writer suites were rerun read-only on 2026-07-31 with model calls
forbidden: 101 tests passed. This confirms the isolated implementation remains executable, but it
is not current-main acceptance. It must be rebased and retested after contract migration.

Current Stage 3 gate:

```text
Writer isolated implementation = PRESENT_UNMERGED
Writer current-main engineering gate = NOT_RUN
Memory integration gate = REQUIRES_WRITER_CONTEXT_MIGRATION
Writer semantic gate = NOT_RUN
Editor/reconciliation implementation = NOT_STARTED
Candidate Commit/Canon integration = BLOCKED
Production gate = BLOCKED
```

## 6. Next permitted transitions

Stage 2M may:

- fix B/C silent failures and emit typed results;
- repair Need/routing/retrieval/evidence/assembly losses;
- rerun deterministic M4 and, only with a declared exception or a passing prerequisite, complete
  the formal WP8 M5 comparison.

Stage 3 preparation may:

- rebase and review the isolated Writer worktree;
- migrate Stage 2B names to Stage 3;
- develop Writing Core, Editor/reconciliation, and evaluation tooling in parallel with frozen
  fixtures;
- replace `Stage1ContextPackage` with `WriterContextPackage` in the later integration workstream;
- run focused developer self-checks, followed by independent Stage 3 acceptance.

Stage 3 may not yet claim semantic quality, connect Writer output to Canon, or promote production
behavior.
