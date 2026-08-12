# Codex acceptance review

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
