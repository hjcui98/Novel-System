# Codex acceptance review

## Stage 2M semantic repair implementation acceptance (2026-08-14)

- Outcome: `PASS / IMPLEMENTATION_REPAIR_ACCEPTED / PRODUCT_GATE_HOLD`
- Review mode: read-only; no tests, benchmark, model endpoint or replay was rerun
- Scope: sections 28.25-28.31 and all review-driven repairs through Stage 1 schema synchronization

### Decision

The implementation repair is accepted. The Need-admission, facet-loop retrieval, exact-L0 packing
and structured State/Relation/Event/Obligation support path now has one fail-closed semantic owner:
predicate support is bound per required facet, grounded and identity-only units cannot fabricate
closure, package assembly preserves unsupported facets as typed gaps, and mechanical delivery is
reported separately from mandatory semantic completion.

The final Stage 2M product gate remains `HOLD`, not because another implementation repair is known,
but because the active acceptance document requires the fixed-commit, single-identity clean-Genesis
C1-C95 P001-P005 run. That run must demonstrate every mandatory facet closed by dereferenceable
exact L0 evidence with no unresolved, insufficient or transport outcome before product `PASS`.

### Accepted Evidence

- Multi-facet predicate bindings are explicit in `NeedCompletionSpec.predicates_by_facet`; the
  Need-level predicate set is only their retrieval union.
- Planner and template generation populate facet bindings from matching State, Relation, Event and
  Obligation record kinds, with focused multi-facet and cross-kind regressions.
- The shared evaluator, retrieval traces and assembler consume the same facet support ids; empty
  support no longer expands to every facet.
- `mandatory_facet_closure` is required and fail-closed in runtime models and formal package
  schemas; driver case and aggregate semantic status are covered through production helpers.
- Stage 1, Stage 2 and Stage 3 checked-in schemas now contain the facet contract. The new Stage 1
  contract test compares all four defining/embedding schemas directly with current model-generated
  schemas and pins the embedded field.
- The submitted final quality evidence is `2290 passed` with three documented pre-existing Stage 5
  integration failures. It was not rerun during this review.

### Remaining Gate

Freeze one implementation commit and run the already-defined clean-Genesis C1-C95 P001-P005
campaign. Preserve mechanical and semantic status separately, stop on any mandatory facet gap,
and use that single-identity artifact set for the final product acceptance decision. The older C20
output remains useful honest diagnostic evidence but cannot substitute for this gate.

## Stage 2M facet-binding contract follow-up (2026-08-14)

- Outcome: `REPAIR / STAGE1_SCHEMA_EXPORT_MISSING`
- Review mode: read-only; no tests, benchmark, model endpoint or replay was rerun
- Scope: section 28.30 facet-level binding and exported contracts

The facet-level implementation is accepted. `NeedCompletionSpec.predicates_by_facet` is consumed
per facet, the Need-level predicate set is only the retrieval union, and the new multi-facet plus
Relation/Event/Obligation generator tests cover the previously open main-path error.

### Finding

1. **P1 - the Stage 1 public schemas still reject the new Need contract.**
   `NeedCompletionSpec` is a Stage 1 boundary, but none of the four Stage 1 schemas that define or
   embed it contains `predicates_by_facet`: `NeedCompletionSpec`, `Stage1MemoryNeed`,
   `HorizonNeedSet` and `Stage1ContextPackage`. Those schemas use `additionalProperties=false`, so
   a Need produced by the repaired code is invalid at the Stage 1 JSON boundary even though the
   Stage 2 and Stage 3 embedded copies were refreshed. This is a reachable contract failure, not a
   documentation-only mismatch.

### Accepted Evidence

- A predicate now closes only the facet whose explicit binding contains it; same-kind predicates
  no longer cross-close a multi-facet Need.
- Planner/template generation derives separate State, Relation, Event and Obligation bindings and
  retains their union only for R1 candidate retrieval.
- The assembler, manifest and aggregate-status repairs from the previous reviews remain accepted.
- The reported quality run is implementation evidence and was not rerun during this review.
- The existing C20 output remains an honest historical `mechanical PASS / semantic INCOMPLETE`
  checkpoint; final product PASS still requires the planned clean-Genesis P001-P005 run.

### Required Repair Direction

Use the existing `scripts/export_stage1_schemas.py` owner to regenerate all Stage 1 schemas that
define or embed `NeedCompletionSpec`, and extend the Stage 1 contract check to assert the new field
is present with the same shape as the Stage 2/3 copies. No implementation, model, retrieval or
benchmark change is required.

## Stage 2M predicate-binding second follow-up (2026-08-14)

- Outcome: `REPAIR / NEED_LEVEL_PREDICATE_OR_SET_IS_NOT_FACET_BINDING`
- Review mode: read-only; no tests, benchmark, model endpoint or replay was rerun
- Scope: section 28.29, refreshed focused tests and refreshed C20 artifacts

The assembler fallback, manifest fail-closed contract and aggregate-helper repairs are accepted.
The remaining blocker is one main-path semantic error: predicate membership is recorded per Need,
but closure is claimed per facet.

### Finding

1. **P1 - a multi-facet Need still lets any declared predicate close every same-kind facet.**
   `_matching_facets` checks `unit.predicate in need.predicates` once and then returns every facet
   whose broad unit kind matches. `_build_planner_need` populates that Need-level OR-set with every
   state predicate of every grounded entity, independent of the semantic question and suggested
   facet. Consequently a fresh version of the real P001 knowledge/capability Need can again let a
   location predicate close both `knowledge_boundary` and `capability_status`; the blanket moved
   from evaluator kind membership into Need generation. The new regression does not expose this:
   it uses a unit predicate absent from the Need OR-set, rather than putting two valid predicates
   in one multi-facet Need and asserting that each closes only its bound facet.

   The same generation rule causes the opposite failure for non-state facets. Planner Needs with
   `relation_state`, `causal_history`, `setup`, `commitment` or `unresolved_status` receive only
   state predicates, so matching Relation/Event/Obligation anchors cannot close them even when the
   exact record exists. This would keep P003 and the stated Event/Obligation/Relation path broken
   in the clean-Genesis run.

### Accepted Evidence

- Grounded slices and `FACT_ANCHOR` no longer act as semantic witnesses.
- The assembler now preserves an empty `supported_facet_ids` set instead of expanding it to every
  Need facet.
- Obligation and plan-provenance branches are reachable and have direct unit coverage under the
  current projection metadata contract.
- `mandatory_facet_closure` is required and limited to `COMPLETE | INCOMPLETE` in both runtime
  models and the exported schema.
- The driver uses tested production aggregate helpers. The refreshed C20 artifacts consistently
  report 14 gaps, mechanical `PASS` and semantic `INCOMPLETE`.
- The submitted quality result is recorded as implementation evidence and was not rerun here.

### Required Repair Direction

Bind predicates at the facet boundary, not as one Need-wide OR-set. The minimum-sufficient owner is
the existing `NeedFacet`/`NeedCompletionSpec` contract: retain, for each required facet id, the
exact predicates that can serve it, and make `_matching_facets` consult that binding. Populate
state, relation, event and obligation predicates from the matching record kinds and grounded
entities; do not copy every entity state predicate into every Planner Need. Add one real regression
with a multi-facet Need containing both a knowledge predicate and a capability predicate, proving
that each anchor closes only its own facet, plus Relation/Event/Obligation Planner-Need cases.
No new service, model call or scoring layer is required.

## Stage 2M semantic repair follow-up review (2026-08-14)

- Outcome: `REPAIR / PREDICATE_BINDING_STILL_OPEN`
- Review mode: read-only; no tests, benchmark, model endpoint or replay was rerun
- Scope: section 28.28 repair, focused tests and the refreshed C20 formal artifacts

The reporting repair and frozen read-boundary repair are accepted. The current artifact now
correctly says `mechanical PASS / semantic INCOMPLETE`. Product acceptance remains open because
the facet evaluator still closes semantic facets by broad unit kind rather than the predicate the
Need asked for.

### Findings

1. **P1 - the same false-closure bug remains for structured anchors.**
   `_facets_for_unit` now rejects grounded slices, but it never compares
   `Stage1MemoryNeed.predicates` with `RetrievalUnit.predicate`. Every state anchor for the same
   entity therefore closes every state-shaped facet, and `FACT_ANCHOR` is allowed to close nearly
   every non-plan semantic facet. This is not hypothetical: the refreshed P001 case closes both
   `knowledge_boundary` and `capability_status` with the identical set of state anchors, including
   location, enrollment, recommendation possession and unrelated belief records. The test named
   `test_same_entity_different_predicate_does_not_close_facet` does not set either predicate; it
   only proves that a state unit kind does not close a relation facet. It therefore does not cover
   the requested same-kind/different-predicate regression.

2. **P1 - obligation anchors are prevented from closing obligation facets.**
   `_STRUCTURED_KINDS_BY_FACET` assigns `PLAN_ANCHOR` to `COMMITMENT` and
   `UNRESOLVED_STATUS`, correctly noting that durable obligations project to that kind. The later
   `PLAN_ANCHOR` special case bypasses the table and returns only `PLAN_NODE` facets. R1 does
   project `WorldRecordKind.OBLIGATION` as `PLAN_ANCHOR`, so the accepted mapping is currently
   unreachable on the real product path. This is a direct false negative for the stated
   Obligation write/retrieve goal.

3. **P1 - the manifest contract still defaults missing semantic evidence to COMPLETE.**
   `EvidenceFirstPackageManifest.mandatory_facet_closure` is an unrestricted string with default
   `COMPLETE`, and the exported schema does not require it. The formal driver supplies the field,
   so the refreshed C20 artifact is correct, but any omitted value validates as product-complete.
   The new contract should be required and limited to `COMPLETE | INCOMPLETE`; absence must not
   silently select the success state. The assembly result should likewise not carry a success
   default when its owner always computes the value.

4. **P2 - the aggregate regression test does not exercise production code.**
   `test_aggregate_semantic_status_requires_all_complete` constructs two local lists and tests a
   local `all(...)` expression. It would continue passing if the driver's aggregate implementation
   were removed or inverted. Extract the existing expression to one small production helper and
   call that helper from both the driver and the test.

### Accepted Evidence

- Grounded block/span units no longer close semantic facets, and the two direct negative tests pin
  that behavior.
- The package manifest, case record and output index now retain mandatory closure. The reviewed C20
  output consistently reports `READY`, aggregate mechanical `PASS` and semantic `INCOMPLETE`, with
  the four unsupported receipts still visible.
- `_parse_frozen_comparison` and `_load_checkpoint_index` have meaningful focused coverage for
  canonical acceptance, drift rejection, Need/context commit mismatch, missing Planner reference
  and strict Planner reading.
- The submitted `2274 passed / 3 pre-existing Stage 5 failures` result is recorded as OpenCode
  evidence; it was not rerun during this review and is not the reason the verdict remains open.

### Required Repair Direction

Keep the shared evaluator and existing models. Bind structured support using the already-present
Need and unit predicate fields: a same-kind anchor with an absent or mismatched predicate must not
close the facet. Remove `FACT_ANCHOR` as a universal semantic witness. Distinguish observed durable
obligations from plan provenance using existing retrieval-unit metadata so obligation anchors close
`COMMITMENT`/`UNRESOLVED_STATUS` while plan nodes close `PLAN_NODE`. Add the actual same-kind,
different-predicate and obligation regressions. Finally make manifest closure required/fail-closed
and test the driver's aggregate helper itself. No new service, model call or framework is needed.

## Stage 2M semantic repair review (2026-08-14)

- Outcome: `REPAIR / PRODUCT_COMPLETION_NOT_ACCEPTED`
- Review mode: read-only; no tests, benchmark, model endpoint or replay was rerun
- Scope: current dirty-worktree implementation, frozen C20 artifacts and
  `.agent/implementation.md` sections 28.25-28.27

This decision governs the current semantic-repair completion claim. The frozen-driver read fix is
accepted as a valid local repair, but the evidence does not establish the product completion gate
defined by `.agent/plan.md` and the active semantic-repair execution document.

### Findings

1. **P1 - exact evidence is still being confused with semantic facet support.**
   `FacetSupportEvaluator._facets_for_unit` treats any `GROUNDED_BLOCK` or `GROUNDED_SPAN` with an
   evidence reference and overlapping entity as support for every non-plan facet on the Need. A
   slice can therefore close `setup`, `causal_history`, `relation_state` or `unresolved_status`
   without supporting that predicate. This is the main-path version of the original false-closure
   problem, and it makes the template smoke's `mandatory_facet_closure=COMPLETE` insufficient as
   semantic evidence. The repair must remove blanket closure and bind grounded slices only to the
   facet semantics actually established by retrieval/rerank output. Add negative regression cases
   where an exact slice about the same entity but a different predicate does not close the facet.

2. **P1 - the frozen driver computes mandatory closure and then drops it from the formal result.**
   `EvidenceFirstAssemblyResult.mandatory_facet_closure` is not written to the package manifest,
   case record or output index. Driver readiness and aggregate status consider assembly,
   mechanical failures and leakage only, so the real C20 run is reported as `READY` / aggregate
   mechanical `PASS` even though four mandatory facet receipts are `unsupported`. Mechanical PASS
   is legitimate and should remain separate; campaign/product status must be `INCOMPLETE` whenever
   any mandatory facet is unsupported, unresolved, insufficient or transport-failed. Persist the
   closure in all three artifacts and cover the aggregate behavior with a focused regression test.

3. **P1 - the claimed end-to-end goal is not demonstrated by the submitted real evidence.**
   The reviewed formal artifact contains one P001/C20 case, one package gap and four unsupported
   mandatory facets. The real C1-C20 world has zero Event records, so Event write/retrieval is not
   proven on real data. The required fixed-identity clean-Genesis C1-C95 P001-P005 run has not
   occurred. Under the active completion predicate, that run is acceptance evidence, not a
   post-completion administrative step. Sections 28.25-28.27 may claim the driver mechanism works
   and faithfully reproduces the frozen C20 state; they must not mark the Stage 2M semantic product
   goal complete.

4. **P2 - the driver bug fix has no focused regression test.**
   The real run demonstrates successful canonical input, but there is no automated coverage for
   checkpoint-index loading, accepted canonical JSON, rejected canonical drift or commit mismatch.
   Add one focused test around this read boundary; no broader framework is needed.

### Accepted Evidence

- The `strict=False` parse followed by canonical byte equality is a sound minimal workaround for
  this Pydantic model family's JSON/tuple validation behavior and preserves fail-closed drift
  detection.
- The checkpoint-index route faithfully reproduced the frozen C20 deterministic receipts, made no
  forbidden Planner/Claim/verifier/evaluator model calls and introduced no future leakage.
- The four unsupported receipts are useful evidence: they correctly expose missing C20 support
  instead of fabricating it. They are a reason to keep the product gate open, not a driver defect.
- The three reported Stage 5 deterministic-suite failures are outside this Stage 2M change and are
  not the reason for this verdict.

### Required Repair Direction

Keep the current architecture and owners. Repair the two false-success paths: make facet support
predicate-specific, then propagate mandatory closure into the driver artifacts and campaign
status. After focused regression coverage, preserve the current C20 result as an honest
`mechanical PASS / semantic INCOMPLETE` checkpoint. Only a later fixed-commit, single-identity
clean-Genesis P001-P005 run with every mandatory facet closed by dereferenceable exact L0 evidence
can change this review to `PASS`.

## Final Memory workflow integration acceptance (2026-08-12)

- Outcome: `PASS / STAGE2M_FINAL_ENGINEERING_ACCEPTANCE`
- Accepted behavior: graph extraction is now part of the default chapter-reveal Memory Write path,
  not only the isolated Round 3 backfill runner.
- One chapter launches ordinary Curator and graph candidate extraction concurrently through the
  shared endpoint admission controller, then merges once into the existing normalize/validate/
  atomic-commit/projection chain. Canonical chapter commits remain strictly ordered.
- Lineage includes both model profiles, graph candidate batches, graph admission receipt and the
  merged `ObservedChangeSet`; no alternate graph store or write path was introduced.
- Verification: formal scripted lifecycle completed 96 commits and five checkpoint freezes;
  `make quality` passed with strict MyPy on 304 files, 1847 passed/9 deselected and 100% statement/
  branch coverage. Full pre-commit passed after the final documentation update.
- Benchmark boundary: the earlier P005 repair and five-point joint package remain accepted evidence,
  but they are not a full C1-C95 rebuild through this newly wired default path. A clean real-model
  Genesis-to-C95 replay is the next product benchmark and must not be reported as already complete.

This section supersedes any reading of the architecture-repair acceptance below that implied the
isolated backfill runner was already connected to ordinary chapter writes.

## Stage 2M architecture repair final acceptance (2026-08-12)

- Outcome: `PASS / STAGE2M_ARCHITECTURE_REPAIR_ACCEPTED / UNIFIED_REAL_GATE_PASS`
- Scope: Round 1/2 Evidence-First integration plus Round 3 World/KG/R1/L1/L2 repair and the §24
  same-basis real cross-round gate
- Exclusion: the legacy claim-first semantic M4/WP8 campaign remains historical `HOLD` diagnostic
  evidence; ADR-0008's external model/human scoring remains post-freeze and outside Agent READY

### Decision

The architecture repair is accepted. Round 1/2 and Round 3 now share one Writer-facing
Evidence-First contract and one immutable repair basis; no parallel runner, graph truth store,
mutation contract or new graph dependency was introduced.

Final Round 3 evidence is
`/tmp/ns-stage2m-round3-world-repair-20260812-v5`. Repair commit
`sha256:b3488cd83bcae744afa4131ff6ca6d676afee841dac189bc241f56f260b5582b` and snapshot
`snapshot.b3488cd83bcae744afa4131ff6ca6d676afee841dac189bc241f56f260b5582b` are bound to P005's
exact checkpoint PlanRoot. The source C95 commit, source WorldRoot/TextRoot/PlanRoot, source head,
frozen DB and source indexes remained unchanged.

The real repair closed candidate accounting (28 candidates; 7 accepted operations, 21 rejected,
0 deduped) and exercised generic evidence-backed missing-entity admission: the new canonical
`entity.graph.ab64f02a66047f1e521ee8d7` is absent from the frozen source, present in the repair World,
and consumed by an accepted `enrolled_in` relation. The accepted projection has 2 relation rows,
2 graph edges, 169 R1 records, 165 entity associations, 265 anchor documents and 96 grounded
documents. Its persisted attestation is exact across all 8 channels with zero failed/degraded units.
The graph-path receipt resolves to an existing R1 relation row and is `l0_verified` against the exact
TextRoot slice.

Final joint evidence is
`/tmp/ns-stage2m-evidence-first-joint-20260812-v4/output_index.json`: aggregate mechanical status
`PASS`; P001-P005 all `READY`; zero gap, dereference, scope, cutoff and leakage failures; unchanged
roots; all default Claim Support/Planner model/whole verifier/semantic evaluator calls zero. P005 is
explicitly `joint_repair=true` and binds the v5 repair commit, snapshot, project, physical indexes,
exact P005 PlanRoot and original C95 source checkpoint commit.

### Verification

- `make quality`: PASS — strict MyPy on 304 source files; 1843 passed, 9 deselected; 24,246
  statements / 6,942 branches at 100% coverage.
- `.conda-env/bin/pre-commit run --all-files`: PASS after final code and documentation changes.
- Exact acceptance assertions: five READY cases, zero forbidden default semantic calls, closed repair
  accounting, real missing-entity CREATE, relation-row/graph-edge equality, persisted derived
  snapshot, L0-verified path-to-row, same P005 basis and unchanged source roots all PASS.
- At acceptance time no commit, merge or push had been performed. After explicit human authorization,
  Codex formed one focused local Stage 2M commit from the accepted scope; it remains unmerged and
  unpushed. The pre-existing dirty worktree and unrelated Stage 3+ files were not included.

---

## Historical evaluator/provenance acceptance (2026-08-11)

- Outcome: `PASS`
- Reviewed: `2026-08-11 +08:00`
- Scope: Stage 2M evaluator/provenance repair §29 and five v3 offline rescores
- Review mode: read-only; Codex did not rerun tests, models, World replay or benchmark execution

## Decision

`.agent/plan.md` 原 §R 的 evaluator/provenance/scoring 任务验收通过。五份 v3 产物证明：

- cross-root matcher 在 credit 前分别以 concrete compiled/canonical TextRoot 调用严格
  `validate_evidence_ref()`；forged block/object/quote/span 均有 fail-closed regression；
- ancestry 首项与 checkpoint commit/ArtifactRef/logical root 三重绑定，实际 chain length 为
  22/42/62/82/97；
- evaluator manifest v2 的 proof ref 非 null，五份 manifest/proof/derived semantic receipt 均在
  现有 CAS 中存在，hash、byte length、media type 与 metadata 一致并可 verified read；
- READY 与 typed-failure 使用同一 `GoldEligibility`，25 条 Plan Gold 从 observed Claim/Evidence
  分母排除；five-segment Plan Goal Coverage、fallback 和 leakage 均已进入报告；
- observed matcher 分母正确变为 P001 4/8、P002 8/9、P003 2/9、P004 8/10、P005 11/11，
  plan/future leakage 均为 0。

OpenCode 报告的质量门为 1714 passed、9 deselected、100% statement/branch coverage，strict
MyPy/Ruff 与 full pre-commit 通过；Codex只接受现有证据，没有重跑。

本结论只接受 evaluator/provenance repair，不宣告 M4、Gate 0-3 或 Stage 2M 通过。可读 v3 目录尚
没有单独的 consolidated report-index 文件，production proof 当前也只在 APC 分支构造；这两项不
推翻本次 APC frozen repair 的机制证据，但必须作为下一质量任务的运行前收尾，不能带入正式
APC/TIO 矩阵。

## Accepted identity and evidence

- v3 root: `/tmp/ns-stage2m-frozen-checkpoint-evaluator-rescore-20260811-v3/`
- P001 manifest/proof: `sha256:844cac19...d3a0fb` / `sha256:2fb75f86...b2d219`
- P002 manifest/proof: `sha256:8a50002a...33ba2d` / `sha256:ed83439b...2b1e54`
- P003 manifest/proof: `sha256:af3f8ec2...0d0110` / `sha256:31c78c48...a7ea0`
- P004 manifest/proof: `sha256:21c5e425...95562` / `sha256:be44dc3c...6f267f`
- P005 manifest/proof: `sha256:4a4d7858...66c39` / `sha256:3a87fad6...fbd0e`

## Next permitted task

真实质量首损现已可解释，下一次 `/implement` 执行刷新后的 `.agent/plan.md`：

1. 在既有 `NeedDraftGrounder`/Need generator 中做 deterministic entity-mention closure；
2. 先离线证明 Planner/fallback 文本中已明确出现的唯一实体全部进入 canonical `entity_ids`；
3. 再复用 C20/C40/C60/C80/C95 冻结 Commit/World/index 重跑 APC 五检查点的
   Need→Retrieval→Claim→Evaluator，不重建 ch0-95；
4. 只有新证据证明 full Need 与 accepted evidence 均已到位而 Claim 仍是首损时，才在现有 Claim
   Support owner 内做最小 evidence/facet binding repair；
5. 本轮不得按 Gold/章节/角色组合写规则，不得恢复 Plan evidence、修改 Gold/阈值/预算或建设新
   Planner、Graph、检索器、evaluator。

该任务完成后返回 Codex；正式 clean APC/TIO 矩阵仍需 Codex接受并形成 clean executable identity。
