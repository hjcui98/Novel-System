# Codex acceptance review

- Outcome: `PASS`
- Reviewed: `2026-08-10 +08:00`
- Scope: chapter-32 Curator resolver-valid feedback repair, license-free regressions, final quality
  evidence, and frozen-context real diagnostic
- Review mode: read-only; Codex did not rerun tests, quality, pre-commit, services, replay,
  benchmark, model, or real API calls
- Accepted executable identity:
  - base HEAD `5ef295fe6a5fedfcef4b02af620dbb988244a58f`
  - clean local commit containing this accepted repair and review
  - executable-source fingerprint
    `sha256:1e7d1f4f48ce86a63a9a808dd1bf8bbb13d2c75be4c437107f329382e7baa2de`

## Decision

The chapter-32 repair passes. It closes the demonstrated poison-loop mechanism at the correct
existing owners without changing the upper architecture: `EvidenceCandidateGenerator` owns
resolver-valid catalog-literal selection and `ModelCurator` owns quote-specific typed feedback. The
strict resolver, provenance binding, ambiguity rejection, retry/budget semantics, Gold, prompts and
Stage boundaries remain unchanged.

This `PASS` accepts the repair mechanism and focused admission evidence only. It is not a Stage 2M,
M4, Gate 0-3, APC/TIO matrix or benchmark-quality pass. The authorized clean local commit is now
formed, so formal execution may proceed under `.agent/plan.md` §6.3.

## Evidence accepted

1. `copyable_literal_for()` reuses the same `resolve_evidence_quotes()` implementation and considers
   only complete catalog strings within the caller's bounded literal budget. Similarity determines
   order only; it never creates an evidence binding. When no string passes, it returns `None`.
2. `ModelCurator` now resolves each quote independently. The first actual failure produces one
   matching JSON pointer and feedback for that quote; successful quote bindings retain their stable
   order and duplicate removal.
3. A feedback string advertises a literal only after the exact bounded string passes the strict
   resolver. The 240-character cap is applied by shrinking the reason prefix; the validated literal
   is not truncated. The no-literal path makes only a generic longer/full-sentence request.
4. The submitted license-free regressions cover an ambiguous nearest candidate with a lower-ranked
   resolvable candidate, resolver validity of every advertised literal, no post-validation
   truncation, generic fallback, exact multi-quote pointer attribution, invalid max length and the
   continuing fail-closed ambiguity/no-auto-binding behavior.
5. OpenCode's final-tree evidence reports `1650 passed, 9 deselected`, 100% branch coverage, strict
   MyPy/Ruff cleanliness, full pre-commit success and clean `git diff --check`. Codex accepted these
   existing results without rerunning them.
6. The diagnostic manifest binds the repair fingerprint `1e7d...a2de`, frozen benchmark hash,
   chapter-31 base commit, new diagnostic DB/output root, endpoint limit 1, and unchanged semantic,
   Claim Support, Writer and Ledger budgets.
7. The successful chapter-32 checkpoint
   `sha256:72578a45c9512fcdb2a4d1ecdac648ee4f13e28a0c668a8bbaec4d6e56ed9d06`
   records three proposals and two rejections. The rejection receipts point precisely to quote
   indexes `/2` and `/0`, contain complete resolver-valid catalog literals, and lead to proposal 3
   being accepted rather than repeating one poison signature.
8. The successful checkpoint commits from frozen base `b0061432...` to `3504a572...`, with commit
   receipt `sha256:f985b75669c4736df831eeeef9e8e1b7a103a7a36d737fe43137c53ea0ffe105`.
   `scenario_run.json` and `project/progress_manifest.json` independently bind chapter 32 to that
   resulting commit and show the accepted observed change/evidence reference.

## Artifact attribution correction

The diagnostic output root contains two sequential invocations under the same diagnostic identity:

- the top-level `memory_write_pause_trace.json` and `flow_summary.json`, written around 10:00 +08:00,
  belong to an earlier invocation that exhausted five proposal attempts with five rejections; it had
  `poison_loops=0` but did not commit chapter 32;
- `scenario_run.json`, `project/progress_manifest.json` and checkpoint `72578a45...`, written around
  10:06 +08:00, belong to the subsequent invocation that accepted proposal 3 and committed chapter
  32.

`.agent/implementation.md` §26 reports the successful invocation but does not mention the earlier
budget-exhausted invocation. The immutable timestamps and checkpoint refs make the two attempts
independently attributable, so this omission does not invalidate the mechanism or require another
real run. It does mean the top-level stale `flow_summary.json` must not be cited as success evidence.
The formal matrix must continue to persist and address each attempt/checkpoint independently.

## Accepted scope and next action

The accepted executable repair scope is limited to:

- `src/novel_agent/services/evidence_candidates.py`;
- `src/novel_agent/services/model_curation.py`;
- `tests/unit/test_evidence_candidate_generation.py`;
- `tests/unit/test_curator_evidence_contract_v2.py`.

Codex-owned review/plan/status updates and `.agent/implementation.md` evidence may accompany that
scope. Unrelated `.gitignore` changes, handover/draft files, `agentmemory_lab/`, and unrelated
technical-reference documents are not accepted by this review and must not enter the repair commit.

Next sequence:

1. OpenCode verifies the committed executable fingerprint and clean executable scope;
2. OpenCode starts APC P001-P005 from ch0 with a new experiment ID, database and output root, then
   runs TIO under its own new identity;
3. do not resume or reuse either `stage2m-phase4-final-apc-20260809` or
   `stage2m-repair-ch32-diag-20260810`; preserve both as diagnostic evidence;
4. keep the fixed concurrency, `false/0/2048`, Writer/Ledger budgets, independent checkpoint reports,
   no in-run tuning and no Agentic paired claim exactly as specified by `.agent/plan.md` §6.3.

Codex formed the authorized clean local commit containing this review. It did not merge, push or
launch the formal matrix.
