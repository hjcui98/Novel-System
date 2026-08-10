# Project status

> Lifecycle: `AUTHORITATIVE`
>
> Status date: 2026-08-09 +08:00
>
> Cross-cutting engineering constraint clarified: 2026-08-08 +08:00
>
> Clean executable implementation: commit `5ef295f`, fingerprint `sha256:20daa522...b089b881`
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

The 2026-08-09 Codex acceptance decision is `PASS` for the APC/TIO architecture implementation and
formal-run admission prerequisites. Typed planning context, strict D9, Planner/Grounder/Validator,
per-channel query compilation, five-segment reporting, exact-slice Claim Support, endpoint-global
request/KV scheduling, failure telemetry, frozen Planner replay, and current-fingerprint bounded
serial/safe-concurrent parity are accepted. This supersedes the 2026-08-03 repair direction as the
current implementation status without rewriting its historical evidence.

Stage 2M Gate M4 and the new Gate 0-3 remain `HOLD/PENDING`: no clean-source APC/TIO P001-P005 formal
matrix has run. The next permitted action is to form a clean executable identity and run the fresh
Phase 4 matrix with independent checkpoint artifacts. The accepted bounded P002 evidence proves the
mechanism and scheduling boundary, not benchmark quality thresholds.

Stage 3 preparation is active. Writer Core has a substantial isolated implementation in a separate
worktree, but it is uncommitted, unmerged, based on the former Stage 2B namespace, and integrated
against the legacy `Stage1ContextPackage` rather than the accepted `WriterContextPackage`.

All permitted work is additionally constrained by minimum-sufficient engineering. Current repairs
must extend the existing semantic owners, Model Gateway/scheduler boundary, artifact store, report
hierarchy, and module topology. No speculative platform, second pipeline, new infrastructure family,
or cross-stage generalization is authorized without a demonstrated requirement and a new Codex
architecture decision. This does not relax any safety, quality, reproducibility, or Gate evidence.

## 2. Stage status

| Stage | Development status | Quality/gate status | Default or next action |
|---|---|---|---|
| Stage 0 | Complete | PASS | Frozen foundation |
| Stage 1A/1B | Engineering complete | Historical formal gate absorbed into Stage 2A evidence | Maintain regression coverage |
| Stage 2A | Development complete | CONDITIONAL_PASS | Freeze deterministic Memory Gateway |
| Stage 2R | Complete | Physical index/basis gate passed | Maintenance only |
| Stage 2W | Complete | Canon safety and recovery evidence accepted | Maintenance only |
| Stage 2M | APC/TIO architecture implemented; final-fingerprint bounded admission accepted | M4 HOLD; Gate 0-3 formal matrix pending | Form a clean executable identity, then run fresh APC P001-P005 plus TIO ablation with independent checkpoint artifacts |
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

### 4.6 2026-08-02 support-closure continuous task

Newer evidence supersedes the next-action wording above without rewriting its historical numbers:

- VAC C40 currentness/contradiction has been repaired and is not scheduled for another rerun;
- the port-8002 VAC C60 v21/v6 run completed 11 proposer and 3 verifier batches with zero endpoint
  errors/timeouts, but G06/G09 rank-complete evidence still did not become complete Ledger claims;
- the current working diff implements `trusted_claim_support_producer.v25`, allowing strictly
  compatible selected evidence to cross retrieval Need routes while retaining target-only ownership,
  exact provenance, and counter-evidence verification;
- 44 focused tests pass; the remaining pre-runtime engineering action is to close strict typing in
  the new tests and pass full `make quality`;
- execution is now one Codex-designed/OpenCode-executed task: full quality, real C60 monitoring,
  frozen first-loss diagnosis, evidence-driven repairs/replays, conditional C95, and document
  handoff to Codex.

### 4.7 2026-08-02 support-closure campaign executed

The `stage2m-support-closure` campaign ran to completion inside one `/implement` and is handed back
for the final Codex review:

- **Quality closed**: the 19 strict-MyPy typing errors in the new tests were fixed; `make quality`
  passes (1402 tests, 100% statement/branch coverage).
- **Real C60 ×3**（budget exhausted; all VAC Arm A, real hybrid, Qwen3.6/8002, comparable）:
  - A0 (producer v25): batch-10 content-level `OpenAIChatEndpointError`（server 200; not an outage）,
    batch-4 coverage-contract failure; 11 semantic receipts;
  - A1 (v26): zero diagnostics, 17 semantic receipts; knowledge draft cited an out-of-pool unit and
    was dropped at normalization;
  - A2 (v27): 18 semantic receipts; G06 first accepted ref reached the Evidence Ledger (0/2→1/2);
    untraceable 0.111→0; zero endpoint errors.
- **Mechanism gain confirmed, target gate not met**: G06/G09 still lack a single verified Writer
  claim citing the complete evidence group; the residual first loss sits in the model synthesis layer
  (query-narrow conclusions over partial evidence groups) after the host corridor (pool/prompt/
  contract) was exhausted → `CAMPAIGN_HOLD / C60_BUDGET_EXHAUSTED`; C95 was not run (admission
  unmet).
- **Next architecture directions for Codex** (not implemented in the campaign): cross-Need fusion of
  verified drafts (new semantic owner); candidate/rank-layer repair for G01-G05/G07/G08
  (memory_pipeline corridor, needs its own evidence); query alignment for the natural owner Needs of
  G06/G09. Reference-count movement alone is not mechanism evidence.

The task-local implementation supplement is `.agent/plan.md`; OpenCode records execution evidence
in `.agent/implementation.md`, and Codex integrates accepted results into this status page and the
active Stage 2M execution document. Gate M4 remains `HOLD`, P3 remains `NOT_STARTED`, and Stage 3 is
outside this task.

### 4.8 2026-08-03 A4-A13 acceptance and corrected repair direction

Codex reviewed the stable handoffs without rerunning tests, benchmarks, services, or real APIs and
returned `REPAIR`:

- A6-A9 retain G06 `2/2` and G09 `3/3` at Stage1, proving the earlier assembly starvation is fixed;
- compact lineage and pool ordering move individual target refs into support groups/Ledger;
- A8 reaches only G06 `1/2`, G09 `1/3` in the Ledger; A9 reaches G06 `0/2`, G09 `1/3`;
- no run forms a complete verified G06/G09 Writer claim, and weighted/mandatory coverage remains 0.

A10-A13 add 304 workset slices, 228 proposed atoms, 227 verified atoms, and 18 verified compounds,
but G06/G09 remain `0/2` and `0/3` in the Ledger. This exposes a read-grain problem before claim
grouping: a benchmark chapter/scene file becomes one large `TextBlock`, and the support path uses
short compact/semantic excerpts before deriving natural paragraph slices. Current architecture now
requires deterministic paragraph/contiguous-window exact slices, token-bounded semantic-input/Ledger packing,
selective single- or multi-slice claim production, independent whole-claim verification, exact cited
reference union, and terminal per-item diagnostics. Universal per-slice atomization, fixed first-three
selection, and host enumeration of two/three-atom groups are superseded. Writer/Ledger budgets and all
cutoff, basis, scope, taint, provenance, Gate, and Stage boundaries remain unchanged. External
agentmemory protocol v1 remains comparison evidence only.

### 4.9 2026-08-09 APC/TIO implementation and formal-run admission

Codex accepted the stable implementation handoff without rerunning tests, benchmarks, services or real APIs:

- final executable identity is producer `trusted_claim_support_producer.v32` and source fingerprint
  `sha256:20daa522f815c88c5ab823d2b03ff896b6751264dd6edac2777a4d93b089b881`;
- `make quality` reports 1642 passed, 9 deselected and 100% branch coverage; full pre-commit passed;
- the frozen non-fallback P002 Planner artifact is replayable and basis/hash checked;
- Claim Support multi is fixed at `thinking=false / reasoning=0 / max output=2048`;
- current-fingerprint P002 serial `(Need 1 / endpoint 1)` and safe-concurrent `(Need 2 / endpoint 1)`
  execute the same 14 semantic requests and each persist two proposal→whole-verifier→verified-receipt
  chains, with balanced leases and zero timeout/OOM/context reduction/leakage;
- Writer 1,061/4,000 and Ledger 11,981/12,000 remain within budget; both runs have identical rendered
  context, 107 semantic Ledger entries and five-segment report;
- endpoint limit 2 is prohibited for the current local Qwen workload because it deterministically
  caused the same multi prompt to grow to 2048 and truncate.

This closes implementation and bounded scheduling admission only. Formal APC/TIO Phase 4 must use a
clean executable-source identity, new DB/output/experiment identities and one immutable child report per
checkpoint. P001-P005 are one fixed peer benchmark matrix with no development/validation/test split;
leakage remains zero-tolerance, and no Stage/Gate PASS is inferred.

### 4.10 2026-08-10 formal APC chapter-32 block

The first clean-identity formal APC run committed chapters 0-31 and then stopped fail-closed at
chapter 32. Immutable quarantine evidence shows a deterministic Curator poison loop: the strict
evidence resolver correctly rejects an ambiguous quote, but similarity-only rejection feedback tells
the model to copy another catalog literal that the same resolver also rejects. The stopped experiment,
database and output root remain diagnostic-only and must not be resumed.

Codex accepted the bounded repair on 2026-08-10. The final candidate fingerprint is
`sha256:1e7d1f4f48ce86a63a9a808dd1bf8bbb13d2c75be4c437107f329382e7baa2de`; quality evidence reports
1650 passed, 9 deselected, 100% branch coverage and full pre-commit success. A fresh frozen-context
diagnostic produced resolver-valid quote-specific feedback and committed chapter 32 without a poison
loop. The repair preserves the strict resolver and all semantic/budget boundaries.

Codex has formed the authorized clean local commit of the accepted repair scope. The replacement
formal APC/TIO matrix is now permitted and must restart from ch0 with new experiment, DB and output
identities.

### 4.11 2026-08-10 v33 chapter-9 relevant-literal repair

The clean v33 APC replacement run stopped at chapter 9 after repeated model attempts emitted the
near-miss quote `他又没有任何意外地落榜。`. The correct source clause
`然后又没有任何意外地落榜。` was present in the visible candidate catalog, but the containing
candidate exceeded the feedback-size budget. The current selector considered only complete candidate
texts, skipped the relevant candidate, and repeatedly advertised an unrelated but resolver-valid anger
sentence. This was actionable-feedback selection failure, not missing context or a reason to weaken
the strict evidence gate.

Codex implemented a minimum local repair at `EvidenceCandidateGenerator`: resolver-checked feedback
may now select bounded verbatim sentence/clause spans from a candidate, ranked against the failing
quote. No fuzzy evidence binding, skipped operation, no-op conversion, budget change or case-specific
rule was added. Frozen ch9 input now selects the correct 13-character clause and uniquely resolves it.
`make quality` reports 1652 passed, 9 deselected and 100% branch coverage; the candidate fingerprint is
`sha256:7bea7d597b01251b641ff93d3d1c854b3953dce77a777abcddb92c40b535342a`.

The v33 run and its DB/output remain diagnostic-only and may not be resumed after this source change.
Current next action is a user-authorized clean commit of the focused repair, followed by another wholly
new APC/TIO matrix from ch0.

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

- use the smallest change at the demonstrated responsible layer and reuse existing contracts and
  infrastructure; defer unrelated generalization and return to Codex before adding a parallel
  framework or new platform;
- preserve the accepted APC/TIO architecture, strict evidence resolver and fixed semantic/transport
  budgets;
- preserve the accepted chapter-32 repair and the focused chapter-9 relevant-literal repair; do not
  weaken strict evidence binding or resume the invalidated v33 identity;
- after forming a user-authorized clean commit for the chapter-9 repair, run `.agent/plan.md` §6.3
  from ch0 with new DB, output root and experiment identities;
- then run APC P001-P005 as the main profile and TASK_INTENT_ONLY as the defined ablation, treating all
  five as fixed peer benchmark cases and keeping checkpoint reports independently addressable;
- report Gate 0-3 and M4 measurements without a development/validation/test split or in-run tuning;
- leave Agentic comparison and any higher-concurrency service work deferred until the formal Arm A
  evidence establishes a new need.

Stage 3 preparation may:

- rebase and review the isolated Writer worktree;
- migrate Stage 2B names to Stage 3;
- develop Writing Core, Editor/reconciliation, and evaluation tooling in parallel with frozen
  fixtures;
- replace `Stage1ContextPackage` with `WriterContextPackage` in the later integration workstream;
- run focused developer self-checks, followed by independent Stage 3 acceptance.

Stage 3 may not yet claim semantic quality, connect Writer output to Canon, or promote production
behavior.
