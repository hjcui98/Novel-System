# Approved-slice candidate: Stage 2M support closure

- Plan state: `WAITING_FOR_HUMAN_APPROVAL`
- Stage: `Stage 2M`
- Production-code base: `2d8067b` (`fix(stage2m): isolate paired execution contracts from stage3`)
- Revision budget: `0/1`
- Merge policy: `CODEX_ON_PASS`

## Authority and current evidence

Follow the current chain in `docs/stage2_memory_benchmark_task_closure_execution.md` sections
0.1.7/0.2 and `docs/stage2m_gate_m4_root_cause_and_remediation_20260730.md` sections 7/8.3.

Frozen/real C60 evidence shows G06 and G09 complete at rank (`2/2`, `3/3`) but incomplete after
Stage1/Writer Ledger. The endpoint and earlier 120-second proposer timeout are not the current main
failure. The rejected ns-6 experiment broadened precise evidence refs to whole blocks, failed 4 of
8 new tests, and did not prove better retrieval or a complete Writer-readable claim.

## Objective

Make rank-complete, cutoff-safe evidence for a public compound Need become one atomic, verifier-safe,
Writer-readable support group and claim without Gold-aware behavior or free semantic inference in
the deterministic Assembler.

## Allowed files

- `src/novel_agent/services/claim_support.py`
- `src/novel_agent/services/memory_pipeline.py` only if frozen-trace evidence proves the loss occurs
  before claim production
- `tests/unit/test_claim_support_selection.py`
- `tests/unit/test_stage1_memory_pipeline.py` only with the corresponding production change
- `.agent/implementation.md`

## Forbidden scope

- Stage 3 files and worktrees
- Gold matcher, accepted-evidence fixtures, Gate formula, fixed denominators, evaluator verdict logic
- Need generator hard caps or checkpoint/Gold/case-specific runtime branches
- deterministic Assembler as a new semantic decision owner
- C80, C95 before C60 mechanism improvement, P3, full five-point matrix, A/B/C, `make quality`

## Implementation steps

1. Reproduce the G06/G09 loss offline from the frozen C60 trace or an equivalent public synthetic
   fixture. Record candidate/rank/Stage1/support-group/claim boundaries.
2. Identify whether complete evidence is split across units of the same public Need/facets or dropped
   before `TrustedClaimSupportProducer` can form a compound candidate.
3. Implement the smallest general rule that groups jointly necessary, same-Need, cutoff-safe,
   scope-visible evidence. Require explicit provenance per cited unit and existing verifier approval.
4. Keep precise evidence references precise. Any enclosing-block binding must prove same source,
   chapter and actual span containment; do not bind merely because `object_hash` and span length match.
5. Add unseen synthetic regressions for complete compound support, cross-Need isolation,
   contradiction, cutoff, scope, taint, duplicate content in different chapters, and deterministic
   receipt closure.

## Required checks

```bash
.conda-env/bin/pytest -q tests/unit/test_claim_support_selection.py \
  tests/unit/test_stage1_memory_pipeline.py --no-cov
.conda-env/bin/ruff check src/novel_agent/services/claim_support.py \
  src/novel_agent/services/memory_pipeline.py tests/unit/test_claim_support_selection.py \
  tests/unit/test_stage1_memory_pipeline.py
git diff --check
```

Do not run a real canary during initial implementation. Codex reviews the offline mechanism first.
After review PASS, Codex may authorize one C60 canary only if 8002 health preflight is stable. C95 is
authorized only if C60 shows a mechanism-level improvement with zero endpoint errors.

## Acceptance and stop conditions

- New and existing focused tests pass; no future leakage, scope crossover, tainted evidence,
  unbudgeted Writer/Ledger output, or unsupported claim is introduced.
- The improvement is a complete Writer-readable claim with valid support, not only higher accepted
  reference matching.
- Runtime code contains no Gold/case/checkpoint-derived branch and no arbitrary small Need cap.
- Endpoint failure makes the canary `RUNTIME_FAILED / NON_COMPARABLE`; do not retry automatically.
- Any need to change forbidden files, architecture, Gate policy, or Stage boundary is `BLOCKED` and
  returns to Codex/human planning.
