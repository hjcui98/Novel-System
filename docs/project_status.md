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

Stage 2M is in active Gate M4 remediation. P0/P1 code and the strict formal reporter exist, and the
five prescribed P2 sentinels have completed real diagnostic/non-formal runs. P2 is accepted as
`ACCEPTED_AS_DIAGNOSIS`; a new formal WP7/P3 run has not been executed. The old ten-checkpoint WP8
diagnostic is readable failure evidence only: its cases use the legacy schema and its scenario
lifecycle is incomplete. Gate M4 remains `HOLD` and formal WP8 is frozen. This does not reopen
Canon safety or C95 infrastructure acceptance; it blocks claims that the Memory benchmark or
agentic promotion has passed.

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
| Stage 2M | M4 remediation active; P2 accepted as diagnosis; formal P3 pending | M4 HOLD; formal WP8 frozen | Fix C40/C60/C95, run targeted canaries, then run a new P3 only if close to gate |
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

The frozen Writer Context, per-Gold result, and evidence lineage for all ten checkpoints are
available through the human-readable index in
`docs/stage2m_wp8_human_readable_outputs_20260731.md`. This display layer does not alter Gate status.

All ten legacy `stage2m_case_C*_A.json` files lack the six identity/budget fields now required by
the formal case schema, and all ten `scenario_run.completed` values are false. They must not be
backfilled or promoted into a formal WP7/P3 report.

### 4.3 2026-08-01 P2 diagnostic result

The prescribed real-model sentinels completed against the local Qwen3.6 endpoint
(`http://127.0.0.1:8002/v1`, `qwen36-27b-nvfp4`) in isolated diagnostic output directories:

- APC P002/C40: `/tmp/ns-stage2m-formal-p2-remediation27-apc-c40-20260801`;
- VAC P002/C40: `/tmp/ns-stage2m-formal-p2-remediation26-vac-c40-20260801`;
- VAC P003/C60: `/tmp/ns-stage2m-formal-p2-remediation26-vac-c60-20260801`;
- VAC P004/C80: `/tmp/ns-stage2m-formal-p2-remediation27-vac-c80-20260801`;
- VAC P005/C95: `/tmp/ns-stage2m-formal-p2-remediation27-vac-c95-20260801`.

All five runs produced diagnostic partial reports, completed their scenario lifecycle, and did not
emit formal Gate PASS. The selector v3 fix preserved the VAC C40 target support group into the final
Evidence Ledger, but its Gold conclusion remained semantically incomplete. VAC C80/C95 still have
incomplete trace and `UNTRACEABLE` findings; these are diagnostic findings, not Gate evidence. The
P2 result is accepted as diagnosis, while its quality remains below the final Gate.

The existing selected-checkpoint reports are the non-formal comparison baseline:

- VAC: `/tmp/ns-stage2m-selected-checkpoints-vac-20260801/selected_checkpoint_report_A.json`
  covering C40/C60/C80/C95;
- APC: `/tmp/ns-stage2m-selected-checkpoints-apc-20260801/selected_checkpoint_report_A.json`
  covering C40 only.

Together they contain 53 Gold items: 45 `MISS`, 3 `UNTRACEABLE`, 4 `HIT`, and 1 `PARTIAL`.
The reports are source-identity consistent but deliberately remain `formal=false` and
`gate_passed=false`; no human signature is required for this development baseline.

### 4.4 Stage 2M blockers

1. P2 diagnostic quality is far below Gate M4's 95% current-state and operational/plan targets and 90% historical
   recall target; this is a repair target, not a rejection of the diagnosis;
2. the four targeted C40/C60/C95 canaries have run, but the overall result is not close to the final
   thresholds, so the new formal double-profile P3 run has not started;
3. the legacy ten-point cases fail the current formal schema/run-identity/lifecycle contract; this
   remains a final-P3 input requirement, not a daily development gate;
4. trace completeness and cutoff-safe provenance remain incomplete in several diagnostic cases;
5. the 13 `COMPLETE + MISS` items need optional diagnostic review, but no reviewer signature blocks
   development;
6. B/C is incomplete and contains silent Agentic failures, but formal B/C work remains frozen until
   Gate M4 passes.

### 4.5 2026-08-01 focused canary result

Four non-formal Arm A canaries ran against the local Qwen3.6 endpoint without rerunning C80,
C90-C95 continuation, or A/B/C. All four completed their scenario lifecycle with `Assembler=READY`;
future leakage and future-isolation failure were zero, and Writer/Ledger budgets were within limits.
The runs intentionally used `--allow-dirty-diagnostic`, so their manifests retain
`code_source_dirty=true` and cannot be promoted to formal P3.

| Canary | Result versus selected 53-Gold baseline | Decision |
|---|---|---|
| APC C40 | HIT `1→4`; weighted `0.0926→0.2407`; mandatory `0.0909→0.3636` | Local compound-claim improvement, but not closed |
| VAC C40 | HIT `3→2`; weighted `0.3636→0.2273`; one new `CONTRADICTS` | Semantic regression signal; continue C40 diagnosis |
| VAC C60 | `MISS 9→MISS 8 + PARTIAL 1`; weighted `0→0.0192`; `F-NEED` remains dominant | Candidate recall moved, selected/ledger remained zero |
| VAC C95 | `MISS 10 + UNTRACEABLE 1` essentially unchanged; weighted remains `0` | Candidate recall moved slightly, selected/ledger remained zero |

The canary result is therefore diagnostic progress, not Gate progress: continue the targeted C40
semantic/assembly and C60/C95 candidate-to-selected retrieval work, and do not start formal P3.

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

- continue only the targeted C40/C60/C95 repair loop and run an affected canary when the code change
  is ready for evidence;
- allow an optional reviewer to inspect the 13 `COMPLETE + MISS` items in parallel;
- run a new clean double-profile WP7/P3 Arm A matrix and evaluate Gate M4 only when the canary is
  close to the final thresholds and the four fail-closed bottom lines have no regression;
- start a new formal WP8 A/B/C experiment only after both profiles pass Gate M4.

Stage 3 preparation may:

- rebase and review the isolated Writer worktree;
- migrate Stage 2B names to Stage 3;
- develop Writing Core, Editor/reconciliation, and evaluation tooling in parallel with frozen
  fixtures;
- replace `Stage1ContextPackage` with `WriterContextPackage` in the later integration workstream;
- run focused developer self-checks, followed by independent Stage 3 acceptance.

Stage 3 may not yet claim semantic quality, connect Writer output to Canon, or promote production
behavior.
