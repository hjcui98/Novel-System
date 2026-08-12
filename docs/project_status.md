# Project status

> Lifecycle: `AUTHORITATIVE`
>
> Status date: 2026-08-12 +08:00
>
> Cross-cutting engineering constraint clarified: 2026-08-08 +08:00
>
> Clean executable implementation: commit `5ef295f`, fingerprint `sha256:20daa522...b089b881`
>
> Canonical naming: `docs/adr/0006-three-product-stage-topology.md`

## 1. Executive status

The project has completed Stage 2A development and accepted its infrastructure/safety result as
`CONDITIONAL_PASS`. The deterministic real-hybrid Memory Gateway is frozen as the safe default.
Agentic retrieval remains experimental.

The legacy claim-first Stage 2M Gate M4 campaign remains frozen as diagnosis. P0/P1 code and the strict formal reporter exist, and the
five prescribed P2 sentinels have completed real diagnostic/non-formal runs. P2 is accepted as
`ACCEPTED_AS_DIAGNOSIS`; a new formal WP7/P3 run has not been executed. The old ten-checkpoint WP8
diagnostic is readable failure evidence only: its cases use the legacy schema and its scenario
lifecycle is incomplete. That legacy M4/WP8 line remains `HOLD` and formal WP8 is frozen. This does not reopen
Canon safety or C95 infrastructure acceptance; it blocks claims that the Memory benchmark or
agentic promotion has passed.

The 2026-08-09 Codex acceptance decision remains valid for the APC/TIO architecture and bounded
admission mechanisms. A later v33 APC replay completed ch0-95 and produced five frozen checkpoint
diagnostics; the 2026-08-11 repair run additionally demonstrated Planner scope/coverage fallback,
runtime entity binding and stable Claim Support multi generation without changing Canon.

Stage 2M architecture-repair Gate is `PASS/ACCEPTED`. The TextRoot ancestry/evaluator repair is historical
diagnostic infrastructure, while ADR-0008 removes Claim Support and semantic evaluation from the
Memory Agent's default product path. The evidence-first package/readiness repair, bounded
World/KG/R1/L1/L2 repair corridor, and same-repair-snapshot joint checkpoint input are now
accepted with full deterministic and real evidence. The isolated real API/real-hybrid five-checkpoint
plus Round 3 joint run completed with aggregate mechanical PASS and all five cases READY. External
model/human scoring remains post-freeze and outside Agent READY; the legacy claim-first/WP8 diagnostic
is not retroactively promoted to PASS.

The later-stage architecture is now organized around three products. Stage 3 is the Writer Agent and
Writing Context Loop; Stage 4 is an independent Planner Agent and Planning Context Loop; Stage 5 is
the long-running Creative Runtime that integrates accepted plan and draft candidates. Stage 3 and 4
may prepare in parallel over the shared Stage 2 Memory contracts. This documentation decision does
not alter the active Stage 2M formal run or authorize implementation by itself.

Stage 3 engineering implementation is now complete in the isolated
`codex/stage3-writer-context-loop` branch at `bab4451`: the Writer/Editor/Observer candidate loop,
reactive Memory, shared event-derived Context Runtime, compaction/recovery and evaluation runner are
present, with 1893 deterministic tests, 100% coverage and full pre-commit reported. It remains a
conditional Gate rather than current-main product acceptance because the branch is unmerged and the
real model/infrastructure semantic gates were not completed. Stage 4 implementation is actively
advancing in `codex/stage4-planner-context-loop`; its candidate commits include the inquiry-conditioned
Planner loop and shared Context Runtime connection, but no formal Stage 4 PASS is recorded yet.

Stage 5 now has an active design/execution pair. A bounded Runtime Kernel may be developed in an
isolated clean worktree while Stage 4 continues: fixed topology, Task/Attempt/fence, acceptance
commands, existing Commit/Projection/Freshness integration, settled recovery and minimal maintenance.
Real Planner binding and the product Plan→Write→Commit Gate still wait for Stage 4 acceptance.
Multi-worker lease/reclaim, scheduled fire, external Hook ingress, Skill evolution and Temporal remain
evidence-triggered rather than pre-authorized platform work.

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
| Stage 2M | Evidence-first package/readiness and bounded World/KG/R1/L1/L2 repair accepted; v33 frozen source retained | Architecture-repair PASS; deterministic quality and unified real gate PASS | Keep external scoring post-freeze; maintain accepted contracts |
| Stage 3 | Engineering implementation complete in isolated branch `bab4451` | Deterministic quality reported; real model/infrastructure Gate conditional; unmerged | Independent acceptance/convergence on the final common executable identity |
| Stage 4 | Planner Context Loop candidate implemented and shared Context Runtime connected in active isolated branch | Full quality, real planning quality and independent Stage 4 Gate pending | Continue Stage 4 implementation and final unified test campaign |
| Stage 5 | Design/execution ready; isolated Runtime Kernel admitted | Implementation not started; full product Gate waits for Stage 4 | Build only the isolated fixed-topology/Task/Attempt/Commit/recovery kernel; bind real Planner after Stage 4 PASS |

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

The focused repair is committed locally as `b06b8de`. The user then explicitly authorized continuing
the current APC campaign from its latest accepted checkpoint because the repair changes only rejected
draft feedback and does not rewrite accepted chapter state. APC therefore does not restart from ch0.
An already-running writer continues alone; if a new process is required, it uses a new continuation
experiment/output manifest referencing the existing v33 project/database and records the
`660abf8 -> b06b8de` executable boundary. TASK_INTENT_ONLY still starts separately from ch0 with a
fresh identity.

### 4.12 2026-08-11 frozen-checkpoint diagnostic review

The v33 replay has completed through C95, and the frozen-checkpoint repair diagnostic reused the
existing C20/C40/C60/C80/C95 commits without changing the 97-commit Canon chain. The accepted parts
of the handoff are narrow: scope canonicalization no longer drops legal Planner drafts, incomplete
goal coverage fails over to the existing full fallback, five-segment entity labels bind to runtime
World IDs, and Claim Support multi with endpoint limit 1 completed 148 recorded proposal calls with
natural `stop` and no repetition/length collapse.

The handoff is nevertheless `REPAIR`. The ancestry builder mixes TextRoot artifact identity with
logical root identity, silently degrades to a matcher without proof, and persists no proof ref in the
case report. All 47 observed Gold therefore still have zero matched Ledger entries even though 44
have an object match, 33 have an object+span overlap and 6 have a complete alternative in the frozen
Ledger. Fifteen semantic judgments remain explicitly rejected as
`TRACEABLE_CONTEXT_ITEM_IDS_NOT_MATCHER_BOUND`.

The reported `mandatory_hit_rate=0` is also not a direct model-quality result: all 25 Plan Gold are
mandatory and have plan-node-only accepted alternatives, while strict D9 correctly requires zero
plan citations in Writer Claims. Those Plan Gold now belong to Plan Goal Coverage and must be typed
out of the observed Claim/Evidence denominator without changing their Gold fields or thresholds.

After scoring is repaired, the remaining real quality signal is already visible: only 8 of 45
five-segment-available observed/operational Gold have a full Need match; 36 have entity misses, 9
scope misses and 3 facet misses. That Need/query/claim quality work is not authorized inside the
scoring repair and will be designed from the corrected offline first-loss table.

### 4.13 2026-08-11 evaluator accepted; Need quality is the active first loss

Codex accepted the evaluator/provenance repair after the repeatable v3 offline rescore. Matcher v6
now validates both EvidenceRefs against concrete TextRoots; ancestry proof, evaluator manifest v2
and derived semantic receipts are content-addressed and verified; Plan-only Gold is separated from
the observed Claim denominator. The corrected observed matcher results are P001 4/8, P002 8/9,
P003 2/9, P004 8/10 and P005 11/11. All five proof chains reconcile to 22/42/62/82/97 commits and
future/plan leakage remains zero. This acceptance is scoped to evaluation correctness and does not
promote M4 or Gate 0-3.

The corrected five-segment result localizes the active product failure before retrieval: all 45
observed Gold have eligible Ledger candidates and 33 have an accepted evidence alternative in the
Ledger, but only 5/45 have a full Need match. There are 39 entity misses, 9 scope misses and 3 facet
misses. P004/P005 cover all five Plan goals yet Need Recall is 0/10 and 1/11; P001-P003 fallback is
also weak. Final Claims remain 11 UNTRACEABLE and 34 MISS.

Artifact inspection identifies one general mechanism: `NeedDraftGrounder` only grounds the
Planner's explicit `entity_mentions` array. It ignores names already present in the same semantic
question, query hints and trigger goal. P004 asks about Chen Changsheng teaching Luoluo but records
only Chen's entity id; a P003 fallback query names Black Dragon and Chen while carrying no entity
ids. The next permitted Stage 2M task is deterministic, World-bounded entity-mention closure in the
existing Grounder/generator, followed by a frozen five-checkpoint APC rerun. Claim Support may be
changed only if the new artifacts prove a correct full Need and accepted evidence reached the
proposal input but the evidence-bound claim still failed.

### 4.14 2026-08-11 product boundary corrected to evidence-first

The user clarified that Stage 2M Memory is not responsible for generating a canonical semantic
answer before Writer consumption and does not embed the benchmark evaluator in the Agent. ADR-0008
therefore supersedes ADR-0004's claim-first payload for active work. The default product is now an
evidence-first `WriterContextPackage + EvidenceLedger`: public Needs organize the material, package
items expose bounded raw previews and exact Ledger refs, and the Ledger preserves cutoff-safe L0
source slices and provenance. Claim proposal, multi-slice synthesis, whole-claim verification,
semantic receipts and Per-Gold evaluation are not package readiness dependencies.

The v3 artifacts are reinterpreted accordingly. Exact accepted evidence is already present in the
Ledger for 33/45 observed/operational Gold: P001 4/8, P002 8/9, P003 2/9, P004 8/10 and P005 11/11.
These are conservative evidence-recovery counts, not new Gate scores, because the legacy claim-first
package did not guarantee that every matched raw entry was explicitly exposed as a Writer-facing
item. The final Claim result of 11 UNTRACEABLE and 34 MISS is no longer a production repair target.

The frozen Planner artifacts also show `relation_count=0`, duplicate canonical/alias labels and a
64-state summary cap. These do not yet justify a World rebuild: P004's prompt already contains
relation-like states such as teaching/learning and generated the correct two-party semantic question;
the concrete loss occurs when Grounder treats exact canonical `落落` as ambiguous with another
entity's alias. The active task therefore fixes exact-label precedence, bounded mention closure and
public-query fallback first. A fresh Curator/World replay is allowed only if the new evidence-first
five-checkpoint artifacts prove that required raw evidence cannot be recovered from the frozen
World/text/index without adding missing canonical records.

The user may score the frozen packages later with an independent strong model or human review; that
external verdict is not part of the tested Agent and cannot feed back into the same experiment
identity.

### 4.15 2026-08-12 Stage 2M default write path is complete

Round 3 graph extraction is now connected to the formal chapter-reveal Memory Write workflow.
For each chapter, ordinary Curator extraction and graph candidate extraction may run concurrently
through one endpoint-global request/KV controller; their outputs are admitted and merged before one
existing atomic commit and projection. Chapter/root evolution remains serial. The formal scripted
flow completed 96 commits and all five checkpoint freezes, and the final deterministic gate reports
1847 passed, 9 deselected, strict MyPy on 304 source files and 100% statement/branch coverage.

The application already supports Qwen/vLLM request concurrency: defaults are 4 independent Support
Needs, 4 evaluator batches, 2 checkpoint workers and endpoint limit 8, with a 200k configured KV
budget. Effective GPU parallel decoding still depends on the deployed server's continuous-batching
configuration. A `max-num-seqs=1` service serializes those requests; a compatible 4/8 service can
batch them. Canonical chapter writers are intentionally never concurrent.

Because this wiring changes the C1-C95 replay executable identity, existing frozen roots remain valid
historical evidence but are not a substitute for rebuilding the benchmark memory under the new
default path. The next formal product run starts from clean Genesis, replays C1-C95 with the real
model, freezes C20/C40/C60/C80/C95 from that run, then executes Evidence-First retrieval/package on
those exact roots.

## 5. Stage 3/4 implementation and Stage 5 isolation status

Stage 3 has completed its engineering implementation in the isolated
`codex/stage3-writer-context-loop` branch. Commit `bab4451` contains the Writer/Editor/Observer loop,
candidate Draft lineage, `WriterWorkPlan`, reactive `REQUEST_MEMORY`, event-derived
`AgentContextView`/ContextDelta/compaction, checkpoint recovery, reconciliation and evaluation
infrastructure. The implementation reports 1893 deterministic tests, 100% coverage and full
pre-commit. It remains `CONDITIONAL_PASS`: it is not merged into main, real infrastructure setup was
incomplete and the formal three-scheme real-model semantic experiment did not run.

Stage 4 is actively implementing the independent Planner product in
`codex/stage4-planner-context-loop`. Candidate commits `2d76c3c` and `88e1027` add
`PlanningInquiry/GoalProposal`, Planner-specific Memory/Context, independent Plan Review, bounded
revision, Stage 4 schemas/evaluation and shared Context Runtime integration. The active branch is not
yet a formal accepted identity; Stage 4 still owns all inquiry/Need/Context/review internals and does
not reuse Writer's plan-conditioned Need generator.

The Stage 5 design and execution baselines are
`stage5_long_running_creative_runtime_overall_design.md` and
`stage5_long_running_creative_runtime_execution.md`. They permit a separate clean worktree to develop
the isolated Runtime Kernel against Stage 3's public result and a frozen Stage 4 leaf port. That
kernel may implement only fixed topology, Task/Attempt/fence, typed commands/events, candidate
acceptance, existing Commit/Projection/Freshness, settled recovery, project single-writer and minimal
maintenance. It uses the real Stage 3 public Writer adapter where its offline/contract path is
available, a strict fake Planner until Stage 4 passes, and fault-injection Writer fakes only for the
crash matrix. Any production assembly that still contains a fake leaf must fail closed.

The isolated kernel is not full Stage 5 acceptance. Real Stage 4 binding, Plan candidate→PlanRoot
materialization and the real multi-chapter Plan→Write→Commit product Gate remain blocked until Stage 4
passes. Multi-worker lease/reclaim, scheduled fire, external Hook, Experience/Skill evolution,
Temporal, HA and a general TaskGraph require their own real caller/evidence trigger.

## 6. Next permitted transitions

Stage 2M may:

- use the smallest change at the demonstrated responsible layer and reuse existing contracts and
  infrastructure; defer unrelated generalization and return to Codex before adding a parallel
  framework or new platform;
- preserve the accepted APC/TIO information boundary, strict evidence resolver and fixed product
  budgets; ADR-0008 replaces the claim-first semantic-call path;
- preserve the accepted chapter-32 repair and the focused chapter-9 relevant-literal repair; do not
  weaken strict evidence binding;
- execute the active `.agent/plan.md` Grounder/summary repair and evidence-first materialization
  without rebuilding ch0-95; reuse frozen C20/C40/C60/C80/C95 and keep every package/ledger/manifest
  independently addressable;
- stop the default runner at package freeze; do not call Claim Support or semantic evaluator and do
  not treat legacy Claim MISS/UNTRACEABLE as a package failure;
- hand the five readable packages and cited evidence ledgers to the user for independent model or
  human scoring; do not add that scorer to the Agent or feed its result back into the frozen run;
- report safety, provenance, budget, Need and evidence-delivery measurements without an in-run
  development/validation/test split or tuning;
- leave Agentic comparison and any higher-concurrency service work deferred until the formal Arm A
  evidence establishes a new need.

Stage 2M architecture-repair update (2026-08-12): the accepted isolated Round 3
real-model/real-hybrid workspace is `/tmp/ns-stage2m-round3-world-repair-20260812-v5`, bound to repair
commit `sha256:b3488cd83bcae744afa4131ff6ca6d676afee841dac189bc241f56f260b5582b` and the exact P005
checkpoint PlanRoot. It contains 2 relation rows, 2 typed graph edges, one L0-verified graph-path
receipt, 169 R1 records and an exact eight-channel projection attestation. The complete frozen CAS
mirror's five paired objects were content-hash verified; no benchmark input was regenerated. The
fresh joint output `/tmp/ns-stage2m-evidence-first-joint-20260812-v4/output_index.json` reports
aggregate `PASS`, P001-P005 all `READY`, zero gap/dereference/scope/cutoff/leakage failures, unchanged
roots, and `joint_repair=true` for P005. Final status:
`STAGE2M_ARCHITECTURE_REPAIR_ACCEPTED / UNIFIED_REAL_GATE_PASS`.

Stage 4 may continue in its independent worktree under
`stage4_planner_context_loop_execution.md` and finish all code before its unified quality/real-model
campaign. Stage 3 proceeds to independent acceptance/convergence rather than a second implementation.

Stage 5 may now open a third, clean, isolated worktree and execute only the A-layer Runtime Kernel in
`stage5_long_running_creative_runtime_execution.md`. All A-layer code is developed continuously before
its single unified deterministic/PostgreSQL/crash/isolated-E2E test campaign. The real creative-loop
integration layer starts only after Stage 4 has an accepted clean identity; evidence-triggered
operations/evolution capabilities remain deferred until their named caller and Gate exist.
