# Codex acceptance review

- Outcome: `REPAIR`
- Reviewed: 2026-08-04
- Scope: REPAIR direction §1-§4 实施（R4-R7 corridor repairs）、b9 真实 VAC C60 run、模型合成层 residual
- Review mode: read-only; no tests, quality suites, services, benchmarks, model/API calls, replays,
  or monitoring were run

## Decision

**Accept** the corridor repairs (R4-R7) as demonstrated with regression backing, final-code `make quality` 1496 passed 100% 覆盖, strict mypy/ruff 全绿。The handle→workset boundaries are now fully audited, the first loss is located and repaired, the source families ch2/ch36/ch50/ch56 survive segmentation into workset/chunks/raw Ledger (trace4 + b9 both confirm; verified against the authoritative ZTJ-P003 gold `accepted_evidence_sets`: G06 = {ch2, ch56}, G09 = {ch36, ch50, ch56}), transport is serialized-request budgeted with closure-based early exit, and failed-call diagnostics are complete and sanitized.

**Do not accept** the b9 handoff as the final 2M C60 evidence. G06 UNTRACEABLE / G09 MISS at verdict level (same as A1-A4), weighted coverage zero. The residual first loss is now precisely the **model synthesis layer**: with the required exact slices present in the semantic input of the first chunks (event Needs carry {ch2, ch56} in chunk0; the xuan-yuan-po and knowledge Needs carry the full {ch36, ch50, ch56} triple in every chunk), the model's multi-slice synthesis produced verbatim slice copies, single-punctuation fragments (3 claims <4 chars), and chapter-title-prefixed text (4 claims starting with "第...章"), and never combined the cross-slice complete conclusion for G06's {ch2, ch56} or G09's {ch36, ch50, ch56}. This is the model layer — the same layer the previous review identified as beyond host enforcement ("模型合规性无法被 host 强制").

The review's evidence item 4 (complete G06/G09 verified claims at verdict level) is not met. One more bounded in-corridor attempt at the synthesis instruction + fail-closed garbage-claim guard is authorized before the definitive architectural return. The producer version string must be bumped to v31 so the b10 fingerprint reflects the final mechanism.

## Accepted findings

1. **Membership audit**: 10 boundaries with typed keep/drop reasons per Need, `blocks_resolved` 0→45, deterministic trace locates the exact first-loss boundary at compact-first evidence dedup + pre-compatible ranked cap. This meets review §1.

2. **Boundary repair**: L0-family canonicalization (`_family_canonicalize`: block > span > compact > anchor, compact prefix adjudicated before unit kind) + chapter-diverse handle budget + removal of pre-compatible `ordered[:20]` ranked cap. trace4 confirms ch2=345, ch50=476, ch36=1512, ch56=839 slices in workset/chunks/Ledger (was 0/2/756/465 in A1-A4). Per-Need membership: event needs carry {ch2, ch56} in chunk0 (5 ch2 + 4 ch56 slices each); knowledge/relationship/marriage needs carry {ch36, ch50, ch56}. All behavior remains Gold/entity/chapter/case agnostic. This meets review §2.

3. **Serialized-request transport budget**: `_workset_chunks` partitions by complete serialized prompt estimate (15K total budget, 4096 output headroom, token estimator calibrated against live endpoint at CJK 1.23 and ASCII 4.46 chars/token). Request artifacts record `estimated_input_tokens/prompt_bytes/max_output_tokens/timeout_seconds/applied_input_token_budget`. Closure-based early exit recomputes covered facets after every emitted verified claim — b9 achieves `facet_not_closed=1` (A4 was 13). Chunk transport failure isolates that chunk only. This meets review §3.

4. **Failed-call diagnostics**: content-addressed `failed_input_ref` retained via artifact store; `_classify_failed_call` walks exception cause chain for httpx.TimeoutException + httpx.HTTPStatusError and message-patterns for output-length-truncation / invalid JSON / missing structured content; retry exhaustion detected via adapter attempts; sanitized detail strips URLs/credentials and caps at 500 chars. This meets review §3.

5. **Real-run transport optimization**: The local model in `json_schema` strict grammar mode produces unbounded exhaustive claims (>8192 output tokens for 79-slice chunks) that cannot complete within the domain ModelRequest 600-second timeout. The evidenced transport repair switches proposal calls to `generate_text` (json_object framing) with explicit JSON-shape guidance in the prompt template and host-side pydantic `model_validate_json` as the fail-closed backstop. Verifier stays on grammar mode (0 transport failures in b9). The single-slice probe window is reduced from 12K to 4K slice tokens (internal budget, reported). These transport-framing decisions are evidence-driven and within the internal semantic-budget authority.

6. **Claim quality evidence**: b9 produced 21 verified multi-slice claims from 27 attempts. Yet 3 claims are pure fragments (<4 semantic characters), 4 claims start with chapter titles like "第23章 星之海洋", and most claims are single-slice verbatim copies rather than cross-slice synthesis. The whole-claim verifier approved garbage claims (a single quote mark "”" passed verification as a valid claim for its facet). The model's G09-adjacent claims ("轩辕破是陈长生的学生，落落称陈长生为先生") are directionally correct but incomplete (missing xuanyuanpo_third_student, academy_protective_effect, impact_is_risk). The model's event-need claims cite the wrong slices ("陈长生喂羊后..." citing ch16 sheep scene for a black-dragon need that carries ch2+ch56 in its input).

7. **G06 verdict analysis**: The evaluator marked G06 UNTRACEABLE with the explanation "a semantically matching claim has no accepted cutoff-safe provenance"; the case file records `matched_context_item_ids: []` and `matched_evidence_ledger_ids: []` but does not record the exact source of the semantic match, so the match may have come from a raw Ledger slice (which carries the gold-relevant text under raw identity, not as a Writer claim) rather than from a Writer claim. The observable, verifiable fact is: no verified Writer claim cited both the ch2 and ch56 exact slices required by the G06 accepted evidence set, and no verified claim expressed the complete G06 conclusion at verdict level. G09 is MISS — no semantically matching claim exists at all. The review does not rely on the unverifiable part of the UNTRACEABLE explanation.

8. **Authoritative gold verification (ZTJ-P003 / C60 case)**: The `accepted_evidence_sets` in `benchmarks/private/ztj_memory_pilot_v0.1/cases/ZTJ-P003/gold.yaml` confirm the source-family requirements used throughout this review:
   - P003-G06 (TRUTH_BOUNDARY, weight 2, mandatory): accepted set = chapter **2** (quote "十二岁远赴南方圣女峰研习天书") + chapter **56** (quote "时至今日，他都没有与对方说过一句话"); components `xu_is_in_south, no_direct_conversation, cannot_claim_her_intent`.
   - P003-G09 (WORLD_STATE, weight 1, optional): accepted set = chapter **36** ("他在名册上添上落落的名字") + chapter **50** ("成为了国教学院的第三名学生") + chapter **56** ("他借着国教学院的历史与复起的声势"); components `luoluo_enrolled, xuanyuanpo_third_student, academy_protective_effect, impact_is_risk`.
   The gold matcher matches evidence refs by object hash + overlapping span, so a verified claim citing the exact ch2/ch56 (G06) or ch36/ch50/ch56 (G09) slices with the complete conclusion text would register a HIT. This is the precise acceptance target for b10.

9. **G09-specific model-layer evidence**: the `xuan-yuan-po-student-of-chen-changsheng` Need's chunk0 semantic input carries the FULL G09 triple (ch36=6, ch50=7, ch56=5 slices) and chunk1 carries ch36=8, ch50=8, ch56=8. Its verified claim ("轩辕破是陈长生的学生，落落称陈长生为先生。") cited exactly ONE slice (the ch50 curator span) — the model saw all three required families and cited one. The knowledge Need's chunk0-2 likewise carry the full triple (ch36=5-6, ch50=5, ch56=5 in every chunk) and its claims cited only ch56 anchors/curator pieces. For G06, the event Needs' chunk0 carries {ch2=5, ch56=4-5}; their verified claims cited ch16/ch28/ch32 (e.g., a sheep scene for the black-dragon Need). In all cases the required slices were in the semantic input — the model failed to combine them. This isolates the residual definitively to the synthesis layer.

10. **Transport framing evidence**: The local model in `json_schema` strict grammar mode writes unbounded exhaustive claims (>8192 output tokens for 79-slice chunks) that cannot complete within the domain ModelRequest 600-second timeout; in `json_object` mode the output is bounded but the shape drifts to the v29-era contract (`claim`/`cited_slice_unit_ids`). The b9 transport repair (json_object + explicit JSON-shape guidance + host-side `model_validate_json`) is the evidenced combination: 49/58 proposals completed, 21 verified claims, `facet_not_closed` 13→1, `slices_not_proposed_transport` 4320→757. The single-slice probe now completes (0 probe truncations in b9) and returned `insufficient_need_ids` for every Need — the model never judged a single slice sufficient, which is consistent with the multi-slice structure of the G06/G09 conclusions. `needs_insufficient=2` records the multi-slice path correctly returning `insufficient_need_ids` instead of a weak claim for two Needs — that instruction-compliant behavior works; the failure is specifically the model writing unrelated/partial claims for the other Needs.

11. **Version identity gap**: b9's `experiment_manifest.json` records `claim_support_producer_version: trusted_claim_support_producer.v30`, but the mechanism changed materially (family canonicalization, membership audit, serialized transport budget, json_object framing). The producer version string in `claim_support.py` must be bumped (v30 → v31) before b10 so the configuration fingerprint and artifact identities reflect the final mechanism.

## Required repair direction

### 1. Strengthen multi-slice synthesis instruction (model-layer, bounded)

The current instruction asks the model to "synthesize ONE complete Writer-facing claim from the supplied exact slices" and the model responds with verbatim copies or single-slice fragments. The model must be directed to produce the **complete required-facet conclusion as one new sentence** that combines the relevant slices' content — not a copy of any one slice.

In `_MULTI_SLICE_PROMPT_TEMPLATE`, extend or replace the synthesis paragraph, front-loading the failure-relevant directives (the b9 evidence shows the model writes an unrelated-slice claim — e.g., the sheep scene for the black-dragon Need — instead of returning insufficient):

- **First directive**: answer ONLY the required facets' questions; if the supplied slices cannot jointly establish the complete required-facet conclusion, return `insufficient_need_ids` — never write a claim about a background or unrelated slice.
- **Explicitly forbid** verbatim copying of any slice text and claims that begin with a chapter title or are mere fragments.
- **Require** that the claim state the complete conclusion for the required facets (not a subset), preserving the epistemic scope (e.g., "不能声称知道她真实心意" for the TRUTH_BOUNDARY conclusion — the epistemic negation is part of the conclusion, not optional).
- **Direct** the model to find the subset of slices whose content jointly establishes the complete conclusion, and cite exactly those.
- Keep the existing 400-character claim bound and the "cite only depended slices" rule.

If after one b10 real run the model still fails to produce complete multi-slice claims, the residual is definitively the model layer and the corridor is exhausted — return for the architecture-level claim-fusion decision that the v29-era analysis already anticipated (`.agent/implementation.md` §9, §11.3).

### 2. Fail-closed claim garbage rejection (host-side, transport layer)

The whole-claim verifier approved a claim of "”" (one punctuation character) — it is entailed by its cited slice but is not a valid Writer claim. The host must fail-closed on claims whose content is clearly not a synthesized conclusion.

Extend `_clean_claim` (or add a new `_reject_claim` guard before `_verify_claim_whole`) to reject and type:

- claims that are pure punctuation or whitespace after stripping (already partially handled),
- claims shorter than 4 non-whitespace characters.

The rejection reason must be typed and recorded as a `proposal_rejected` event (not a transport failure) and must happen **before** the whole-claim verifier call so a garbage claim does not consume a verifier request. The verifier is NOT modified — the host guards against garbage that passed lenient verification.

**Scope warning (corrected from an earlier draft)**: do NOT add a verbatim-copy / substring-of-cited-slice rejection. The gold's accepted conclusions are themselves near-verbatim paraphrases of the exact slice text (P003-G06's ch56 quote "时至今日，他都没有与对方说过一句话" and the conclusion "不能声称知道她真实心意" are the same content), so a substring check would reject the legitimate G06/G09 conclusions this corridor exists to produce. Host rejection must be limited to deterministic non-semantic garbage (punctuation/shortness), not text-similarity judgment.

### 3. Version identity, quality gate, and one new real VAC C60 with hard stop condition

Before the real run, two small implementation items:

- **Bump the producer version** `trusted_claim_support_producer.v30` → `.v31` in `claim_support.py` so the b10 configuration fingerprint and artifact identities reflect the final mechanism (the v30 string in b9's manifest is stale for the changed corridor).
- **Add the fail-closed claim guard** from direction 2 with license-free regressions (pure-punctuation and <4-char claims are rejected as `proposal_rejected` before verification; no substring/verbatim rejection).

Then run one new VAC C60 with a fresh experiment identity (b10). The run must use the same code fingerprint discipline as b9: `make quality` 100% coverage before launch, identical tree, recorded fingerprint. Record the terminal state (b9 was `failed` due to 9 transport failures) but treat state as informational — the acceptance gate is verdict-level, not state.

**Hard stop condition**: If b10 does not produce at least one complete verified Writer claim for G06 (citing both ch2 and ch56 exact slices and expressing the full G06 conclusion at verdict level) OR for G09 (citing ch36, ch50, and ch56 and expressing the full G09 conclusion), the model layer is proven incapable of combining cross-slice conclusions under the unified synthesis prompt. **At that point, do not iterate further on prompt engineering. Return for an architecture-level decision: the claim-fusion / deterministic composition / new semantic owner design described in `.agent/implementation.md` §9, §11.3.** The corridor repair is fully demonstrated; the next step needs a new plan for the deterministic synthesis of complete multi-family conclusions.

Note: partial success (only G06 or only G09) does not meet the C95 admission condition — plan §6 requires the same final-code C60 to meet the complete mechanism for both.

### 4. Preserve existing safety and product boundaries (unchanged)

No changes to Writer `4000`, Ledger `12000`, public domain/schema, ADR-0004, Gate formulas, evaluator/Gold matcher, model concurrency/retry policy, or Stage 3 code. The L0-family canonicalization, chapter-diverse budget, membership audit, serialized transport budget, closure early-exit, and failed-call diagnostics are part of the maintained corridor. The probe output ceiling (4096), multi-slice output ceiling (4096), and multi-slice timeout (600s) are internal semantic budgets recorded in artifacts and may be reported as changed since the last review but are within the plan's authority.

## Evidence required for the next review

1. The multi-slice instruction strengthening and the fail-closed claim guard (pure-punctuation / <4-char rejection, typed `proposal_rejected`, before verification) with license-free regression coverage. No substring/verbatim-copy rejection; no new boundary or architecture regressions.
2. Producer version bumped to `trusted_claim_support_producer.v31`; final-code `make quality` 100% coverage, strict mypy/ruff clean. The exact command/result and code fingerprint must be recorded and must equal the b10 run's fingerprint.
3. One new real VAC C60 (b10, new experiment identity) demonstrates either:
   a. **Complete G06 and/or G09 verified Writer claims** at verdict level (HIT or at minimum a traceable claim with correct slice-family membership {ch2,ch56} / {ch36,ch50,ch56} and complete conclusion text), **OR**
   b. the hard stop evidence: the model definitively fails cross-slice synthesis, with the final funnel and model claim quality evidence supporting the return.

   In either case: terminal transport artifacts, required exact slices in semantic input and raw Ledger, exact claim-to-Ledger lineage, zero leakage/profile contamination, unchanged Writer/Ledger budgets.
4. `.agent/implementation.md` updated with b10 evidence and final handoff state: either `COMPLETE` (G06/G09 claims demonstrated) or `RETURN_TO_CODEX` (model layer proved insufficient, architecture decision required).
5. C95 remains prohibited unless b10 meets the admission condition. P3, formal A/B/C, Gate evaluation, and Stage 3 remain frozen.
