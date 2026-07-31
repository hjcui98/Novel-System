# ADR-0003: Freeze deterministic Memory Gateway after C95

- Status: accepted
- Date: 2026-07-29
- Supersedes: ADR-0002 for the current evidence window
- Gate evidence: `reports/stage2a/current_gate_evidence.json`
- Gate report: `reports/stage2a/current_gate_report.json`
- C95 run: `reports/stage2a/teacher_forced_real/author_plan_conditioned_qwen36_c73_restart_ca9c78e_from_1a3ea3b_20260728_r35`
- Deterministic checkpoint gate: same run directory, `retrieval_gate_c20_c95.json`
- Independent ledger: `reports/stage2a/evaluation/ledger.sqlite3`

## Context

The author-plan-conditioned real-hybrid replay completed Genesis plus C1–C95 with 96 accepted
chapter commits. The final Canon head is
`sha256:d4920c29bcdcbc07b64de4b0ffac4772d4aae0fefb900d67d343b01f1ec29ba9`.

The completed window provides:

- one approved Genesis;
- 95 committed chapter transitions and a consistent checkpoint/resume chain;
- zero future leakage and zero future-isolation failures;
- 27 Curator proposals, including five typed rejections that were repaired before commit;
- zero rejected-patch pollution and zero poison loops;
- 97 exact derived snapshots and 97 completed projection outbox entries, including the prelude
  backfill;
- exact deterministic retrieval evidence at C20, C40, C60, C80, and C95;
- five checkpoint entries appended to the independent Evaluation Ledger.

The retrieval gate originally queried mutable aliases when auditing historical checkpoints. That
could compare a historical attestation with a later snapshot's document totals. The gate now
queries each attestation's immutable `physical_name`, matching runtime retrieval and retention
semantics. All five retained physical indexes passed count and basis verification.

Current agentic evidence does not justify promotion. The predeclared held-out complex-query gain
condition remains false, and the current C80/C95 agentic comparisons timed out and safely fell back
to deterministic results.

## Decision

Accept the Stage 2A Memory Gate as `CONDITIONAL_PASS`.

Freeze the deterministic real-hybrid Memory Gateway as the safe default. Keep bounded/agentic
Controller execution experimental and opt-in. It must not become the default until a later,
predeclared evidence window demonstrates stable held-out gain without safety regression.

The formal report therefore records:

```text
verdict = conditional_pass
controller_promotion = freeze_deterministic_gateway
memory_gateway_frozen = true
blocker = controller.held_out_gain
```

This decision permits a Writer integration slice that consumes only frozen,
`writer_safe` deterministic ContextPackages. It does not authorize:

- bounded Controller promotion;
- Writer access to raw retrieval tools;
- Editor, Curator, Commit, or Canon wiring from Writer;
- Writer semantic-quality or production promotion.

## Consequences

- Writer integration starts with `DRAFT` only.
- The integration adapter must fail closed unless the selected Memory Gateway result is
  deterministic, exact, basis-matched, future-isolation safe, and frozen.
- `CONTINUE` and `MAJOR_REWRITE` remain implemented only in isolation.
- Writer Semantic Gate remains `NOT_RUN`.
- Production Gate remains `BLOCKED`.
- A later agentic promotion requires a new ADR and a new formal Gate report.
