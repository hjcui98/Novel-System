# Stage 2M bounded model-layer repair and final C60 b10 attempt

- State: `REPAIR_REQUIRED`
- Plan refreshed: `2026-08-04`
- Git baseline: `e2c9705`
- Working tree: preserve the existing uncommitted R4-R7 implementation, trace4 + b9 artifacts,
  and all user changes; do not reset, discard, or overwrite unrelated work
- Architect and final reviewer: Codex
- Implementation, tests, real API execution, monitoring, artifact analysis, and in-direction repair:
  OpenCode default `build`
- Commit and merge owner: Codex

## 0. Authority and task boundary

This is the current task-local implementation supplement. It does not replace the document
hierarchy in `docs/README.md`. Read these authorities before implementation:

- overall architecture §13.1.1 in
  `长篇小说Agent总体架构设计_v2.2_完整合并版.md`;
- technical contracts §12.13 and §13.5 in
  `长篇小说Agent技术实施与选型设计_v0.1.md`;
- the Stage 3 deferral boundary in `长篇小说Agent正式开发执行规划_v0.1.md` §6.2 and
  `docs/stage3_writer_core_overall_design.md` §3.2;
- the active Stage 2M decision in
  `docs/stage2_memory_benchmark_task_closure_execution.md` §0.1.10 and §21.3;
- accepted read-side product ownership in
  `docs/adr/0004-stage2m-writer-context-product.md`;
- the current `REPAIR` decision in `.agent/review.md`;
- immutable R4-R7 execution evidence in `.agent/implementation.md` §13;
- immutable trace4 and b9 evidence in their respective artifact directories.

`agentmemory_lab/results/REPORT_20260803.md` is comparison evidence only. Its protocol-v1 score is
not a Stage 2M Gate, and agentmemory is not a production dependency or truth store.

## 1. Starting evidence

The R4-R7 corridor repairs are complete and demonstrated (`make quality` 1496 passed 100%
coverage, strict mypy/ruff clean):

- **R4 membership audit**: 10 boundaries with typed keep/drop reasons, `blocks_resolved` 0→45.
- **R5-R6 boundary repair**: L0-family canonicalization + chapter-diverse budget → ch2/ch36/ch50/
  ch56 survive all boundaries (trace4 confirms: ch2=345, ch50=476, ch36=1512, ch56=839 slices in
  workset; was 0/2/756/465 in A1-A4).
- **R7 transport**: serialized-request budget (15K total, 4096 output headroom), closure-based
  early-exit (`facet_not_closed` 13→1), failed-call diagnostics (content-addressed failed-input
  refs, sanitized error categories).
- **b9 real VAC C60** (experiment `stage2m-exactslice-v31-c60-b9-20260804`): 58 requests
  49 completed, 21 verified multi-slice claims, required ch2/ch36/ch50/ch56 exact slices in
  semantic input and raw Ledger, zero leakage, Writer 3993/4000, Ledger 11964/12000.

The residual first loss is the **model synthesis layer** (`.agent/review.md` §Accepted findings
6-9, confirmed against authoritative ZTJ-P003 `gold.yaml` `accepted_evidence_sets`):

- P003-G06 (TRUTH_BOUNDARY, mandatory, weight 2): accepted evidence = {ch2, ch56}
  (ch2 quote "十二岁远赴南方圣女峰研习天书", ch56 quote "时至今日，他都没有与对方说过一句话").
  Event Needs carry {ch2, ch56} in chunk0 (ch2=5, ch56=4-5); verified claims cite ch16/ch28/ch32
  (e.g., the sheep scene for the black-dragon Need).
- P003-G09 (WORLD_STATE, optional, weight 1): accepted evidence = {ch36, ch50, ch56}
  (ch36 quote "他在名册上添上落落的名字", ch50 quote "成为了国教学院的第三名学生", ch56 quote
  "他借着国教学院的历史与复起的声势"). The `xuan-yuan-po-student-of-chen-changsheng` Need and
  the `knowledge` Need carry the full triple in every chunk; verified claims cite only one of the
  three families.
- b9 claim quality: 3 claims pure fragments (<4 chars), 4 claims start with chapter titles, most
  are single-slice verbatim copies. The model returns claims about background slices (sheep scene)
  instead of the required-facet conclusion or `insufficient_need_ids`.
- b9 verdict: G06 UNTRACEABLE, G09 MISS, weighted coverage 0.0 — same as A1-A4.

This is a bounded model-layer repair (two code items) plus one final VAC C60 attempt with a hard
stop condition (`.agent/review.md` "Required repair direction"). Do not modify the corridor
boundaries, the retrieval/ranking/cap/resolver code, the audit, or the transport budget.

## 2. Implementation work

### 2.1 Producer version bump

Edit `src/novel_agent/services/claim_support.py` line ~483:

```python
version = "trusted_claim_support_producer.v31"
```

This ensures the b10 configuration fingerprint reflects the final mechanism (family canonicalization,
membership audit, serialized transport budget, json_object framing). The v30 string in b9's
manifest is stale.

### 2.2 Strengthen multi-slice synthesis instruction

Edit `_MULTI_SLICE_PROMPT_TEMPLATE` in `claim_support.py`. Replace the synthesis paragraph (the
part beginning "No single supplied slice directly expresses the complete conclusion, so
synthesize ONE complete Writer-facing claim...") with a front-loaded directive that addresses
the specific b9 failure modes.

The new paragraph must front-load the directives the b9 run showed the model violating:

1. **First directive** (before any synthesis instruction): "Answer ONLY the required facets'
   questions. If the supplied slices cannot jointly establish the complete required-facet
   conclusion, return `insufficient_need_ids` — never write a claim about a background or
   unrelated slice, and never claim a slice supports a conclusion it does not contain."

2. **Synthesis instruction**: "Synthesize ONE complete Writer-facing claim from the subset of
   supplied exact slices whose content jointly establishes the complete required-facet
   conclusion. The claim must be a new sentence combining the slices' content; it must not be a
   verbatim copy of any slice text, and it must not begin with a chapter title."

3. **Citation**: "Cite in `slice_unit_ids` ONLY the slices whose content the claim's clauses
   directly depend on — never the whole supplied list."

4. **Claim bound**: "The claim must be at most 400 characters. If the complete conclusion cannot
   be expressed within that bound, return `insufficient_need_ids`."

5. **Epistemic scope** (already partially present; verify it applies to the synthesis paragraph):
   "Preserve all material qualifications, negation, and epistemic scope. Treat facet kinds as
   questions to resolve, not asserted values."

The existing JSON-shape guidance block, the "insufficient_need_ids" fallback, and all existing
qualifications (scope, cutoff, taint) remain unchanged. Only the synthesis paragraph is
replaced/extended — not the full template.

### 2.3 Fail-closed host-side claim garbage rejection

Add a new method to `TrustedClaimSupportProducer`:

```python
@staticmethod
def _reject_garbage_claim(claim_text: str) -> bool:
    """Return True for a claim that is purely punctuation or too short to be a conclusion."""
    stripped = "".join(char for char in claim_text if not re.match(r"[\s\p{P}\p{S}]", char))
    if not stripped or len(stripped.strip()) < 4:
        return True
    return False
```

(Use the existing `re` module; strip Unicode whitespace/punctuation/symbols, count remaining
semantic characters.)

Call `_reject_garbage_claim(claim.claim_text)` at TWO points in the corridor (before
verification in each case):

1. In the single-slice path: after `single_audit` is not None and `claim.single_slice_sufficient`,
   before `self._verify_claim_whole(...)`. If rejected: `funnel.proposals_rejected += 1`,
   record a `stage="proposal_rejected", reason="garbage_claim"` progress event, `return` (skip
   verification and emission).

2. In the multi-slice chunk loop: after the host validates `cited_ids ⊆ chunk_ids` and
   `facet_ids ⊆ legal_facets`, before `self._verify_claim_whole(...)`. If rejected: same
   treatment as above.

The rejection reason must be `"rejected:garbage_claim"` (distinct from the existing
`proposal_rejected` which is for missing/insufficient claim shape). Record it in
`_record_progress(stage="proposal_rejected", ...)` — a new event stage so rejected garbage is
typed separately from insufficient-return and shape-validation rejections.

Do NOT add any semantic match or verbatim-copy rejection. The gold conclusions are themselves
near-verbatim paraphrases of slice text, so a substring or text-similarity check would reject
the legitimate G06/G09 conclusions (`.agent/review.md` §2 Scope warning).

### 2.4 Regression tests

Add license-free regressions for:

1. `_reject_garbage_claim`:
   - Pure punctuation (e.g., `"”"`, `"。”`) → True.
   - Whitespace + 1-3 semantic characters → True.
   - 4+ semantic characters → False (valid claims pass through).
   - Whitespace-only → True.
   - CJK garbage (only punctuation chars) → True.

2. Instruction content assertions:
   - The `_MULTI_SLICE_PROMPT_TEMPLATE` contains the front-loaded first directive text
     ("Answer ONLY the required facets' questions") and the "never write a claim about a
     background or unrelated slice" phrase.
   - The existing epistemic-scope language ("Preserve all material qualifications, negation,
     and epistemic scope") is preserved.
   - The JSON-shape guidance text is preserved.

3. The producer version string equals `"trusted_claim_support_producer.v31"`.

All existing tests must continue to pass; no new test may reference Gold, G06, G09, chapter
numbers, entity ids, case ids, or checkpoint numbers.

### 2.5 Quality gate

Run `make quality` on the complete working tree. It must produce 100% statement/branch coverage
with strict mypy/ruff clean. Record the exact command, result, and code fingerprint (commit hash
+ dirty status) in `.agent/implementation.md` — the fingerprint must be identical to the b10
run's recorded fingerprint.

## 3. Real evidence (b10 VAC C60)

Launch one new real VAC C60 with a fresh experiment identity (`stage2m-v31-c60-b10-20260804`).
The run recipe must match the established b9 recipe exactly:

- Source: `benchmarks/private/ztj_memory_pilot_v0.1`
- Information profile: `visible_at_cutoff`
- Arms: `A`
- Retrieval backend: `real_hybrid`
- Resume commit: `sha256:da501411530ab54da79233e0b10da173639888f918f73d13762ac955cb8d52d7`
- Resume chapter: `60`
- Max chapter: `60`
- Resume project: `reports/stage2m/isolated_projects/precise_p13_v2_20260730/visible_at_cutoff`
- Database: `na_s2m_vac_v1_20260729` (credentials from `.env`)
- Model: `qwen36-27b-nvfp4` @ `http://127.0.0.1:8002/v1`
- Semantic backend: `local_openai`
- Model max output tokens: `8192`
- Model max retries: `1`
- Quality repair config: `/tmp/quality_repair_flags.json`
- Allow dirty diagnostic: `true`
- Output directory: `/tmp/ns-stage2m-v31-c60-b10-20260804`

Monitor endpoint health (`/models` 200) before and during the run. Retain the complete
`support_progress.json`, `stage2m_case_C60_A.json`, `console.log`, and `flow_summary.json`.

## 4. Acceptance and hard stop condition

After b10 completes, the next review requires:

1. The funnel records the revised synthesis parameters (producer v31, multi-slice output ceiling
   4096, serialized request budget 15K, closure early-exit, garbage-claim rejections).
2. The required ch2/ch36/ch50/ch56 exact slices remain in semantic input and raw Ledger (no
   regression from b9).
3. **Either**:
   a. At least one verified Writer claim for G06 (citing both ch2 and ch56 exact slices and
      expressing the full G06 conclusion: `xu_is_in_south` + `no_direct_conversation` +
      `cannot_claim_her_intent`) OR at least one verified Writer claim for G09 (citing
      ch36 + ch50 + ch56 and expressing `luoluo_enrolled` + `xuanyuanpo_third_student` +
      `academy_protective_effect` + `impact_is_risk`), registered at verdict level by the
      frozen evaluator, **OR**
   b. The hard stop evidence: the model definitively fails cross-slice synthesis — with the
      final funnel, rejected claims, and model output evidence supporting the return.
4. Writer ≤ 4000 tokens, Ledger ≤ 12000 tokens, future leakage = 0, profile contamination = 0.
5. `.agent/implementation.md` ends in a stable final handoff: either `COMPLETE` (G06/G09 claims
   demonstrated) or `RETURN_TO_CODEX` (model layer insufficient, architecture decision required).
6. C95 remains prohibited. P3, formal A/B/C, Gate evaluation, and Stage 3 remain frozen.

Partial success (only G06 or only G09) does not meet the C95 admission condition — plan §6
requires the same final-code C60 to meet the complete mechanism. Return for architecture
decision in that case.

## 5. Safety, budgets, and non-goals

Do not change Writer `4000`, Ledger `12000`, public domain/schema, ADR-0004, Gate formulas,
evaluator/Gold matcher, model concurrency/retry policy, or Stage 3 code. The L0-family
canonicalization, chapter-diverse budget, membership audit, serialized transport budget,
closure early-exit, and failed-call diagnostics are part of the maintained corridor and must
not regress.

Do not add:
- Any Gold, G06, G09, chapter-number, entity, case-ID, checkpoint, or accepted-ref special case.
- Any host-side claim-text semantic judgment, text-similarity check, or verbatim-copy rejection.
- Any new retrieval, ranking, pool-cap, or resolver change.
- Any change to the single-slice probe, verifier, or their output ceilings/timeouts.
- Any change to the workset budget (40K) or evidence-ledger retention budget (8K).
- Any new reporting system or public schema beyond the existing `support_progress.json` events.

## 6. Stop and return conditions

Return to Codex with evidence when:

- `make quality` 100% is achieved on the final tree and the fingerprint is recorded.
- A new real VAC C60 b10 has completed and its complete funnel + verdict evidence is available.
- The hard stop condition is met (G06/G09 claim achieved → `COMPLETE`, or model layer definitively
  failed → `RETURN_TO_CODEX`).
- The real endpoint/infrastructure becomes unavailable after in-scope diagnosis.
- An unintended regression in existing safety or product boundaries is observed.

A failed assertion in an existing test that is not related to the garbage-claim guard or
instruction change is a regression — repair it in-direction before continuing. A model call
failure or a malformed model item is not by itself a stop condition.

## 7. Reporting

Keep `.agent/implementation.md` as the single implementation evidence log. Include the exact
instruction change, the `_reject_garbage_claim` code path, regression test names and results,
`make quality` output and fingerprint, the b10 experiment ID and path, endpoint health logs,
the per-stage funnel counts, the G06/G09 verdict outcome, any evidence-driven corrections
during b10 execution, safety/budget results, and the final `COMPLETE` or `RETURN_TO_CODEX`
handoff state.

Do not edit `.agent/task.md`, `.agent/plan.md`, `.agent/review.md`, architecture/design/ADR/status,
or active execution documents. Do not create another campaign or remediation document. Do not
commit or merge.

This plan is intentionally narrow: one instruction paragraph edit, one `_reject_garbage_claim`
guard, one version bump, one quality cycle, one real run, and a hard stop. No boundary, retrieval,
ranking, pool, resolver, budget, or architecture change is authorized.
