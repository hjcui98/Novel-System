"""Exhaustive branch tests for the bounded Stage 2W repair policy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.memory_write import (
    BlockingScope,
    FindingRetryability,
    MemoryWriteBudgetRemaining,
    RepairAction,
    RepairActionReceipt,
    RepairContext,
    RepairScope,
    ValidationSeverity,
)
from novel_agent.domain.stage2 import GuardianOutcome, WriteGateOutcome
from novel_agent.services.memory_repair_policy import (
    BoundedMemoryRepairPolicy,
    FindingRule,
)

BASE = CommitId("sha256:" + "1" * 64)
OTHER_BASE = CommitId("sha256:" + "2" * 64)
CONTENT = ArtifactId("sha256:" + "3" * 64)
OPERATION = StableId("operation.repair-policy")


def _budget(**updates: int) -> MemoryWriteBudgetRemaining:
    values = {
        "candidate_revisions": 2,
        "curator_repairs": 2,
        "normalization_passes": 2,
        "guardian_reviews": 2,
        "context_refreshes": 1,
        "total_model_calls": 3,
        "token_budget": 100,
        "wall_clock_budget_ms": 100,
    }
    values.update(updates)
    return MemoryWriteBudgetRemaining(**values)


def _finding(
    code: str,
    *,
    retryability: FindingRetryability = FindingRetryability.REPAIRABLE,
    severity: ValidationSeverity = ValidationSeverity.ERROR,
    human: bool = False,
    refresh: bool = False,
) -> Any:
    return SimpleNamespace(
        finding_id=StableId(f"finding.{code.lower().replace('_', '-')}"),
        code=code,
        severity=severity,
        retryability=retryability,
        operation_ids=(OPERATION,),
        allowed_repair_scope=RepairScope(
            operation_ids=(OPERATION,),
            allow_identity_rebind=True,
            allow_successor_creation=True,
        ),
        blocking_scope=BlockingScope.OPERATION,
        requires_human=human,
        requires_context_refresh=refresh,
    )


def _context(
    *,
    finding: Any | None = None,
    budget: MemoryWriteBudgetRemaining | None = None,
    current_commit: CommitId = BASE,
    repeated: tuple[ArtifactId, ...] = (),
    prior: tuple[RepairActionReceipt, ...] = (),
    guardian: GuardianOutcome | None = None,
    gate: WriteGateOutcome | None = None,
    requires_guardian: bool = False,
) -> RepairContext:
    validation = None if finding is None else SimpleNamespace(findings=(finding,))
    guardian_value = None if guardian is None else SimpleNamespace(outcome=guardian)
    gate_value = None if gate is None else SimpleNamespace(outcome=gate)
    risk = SimpleNamespace(requires_guardian=requires_guardian)
    return cast(
        RepairContext,
        SimpleNamespace(
            candidate=SimpleNamespace(
                base_commit=BASE,
                content_hash=CONTENT,
                candidate_id=StableId("candidate.repair-policy"),
            ),
            validation=validation,
            risk=risk,
            guardian=guardian_value,
            gate=gate_value,
            budget_remaining=budget or _budget(),
            prior_actions=prior,
            repeated_content_hashes=repeated,
            current_canonical_commit=current_commit,
        ),
    )


def test_poison_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        BoundedMemoryRepairPolicy(same_content_hash_limit=0)
    with pytest.raises(ValueError, match="positive"):
        BoundedMemoryRepairPolicy(same_finding_signature_limit=0)


def test_basis_change_always_replans() -> None:
    directive = BoundedMemoryRepairPolicy().decide(_context(current_commit=OTHER_BASE))
    assert directive.action is RepairAction.REPLAN
    assert directive.checkpoint_required is True


def test_content_and_finding_poison_loops_quarantine() -> None:
    policy = BoundedMemoryRepairPolicy(same_content_hash_limit=2, same_finding_signature_limit=2)
    finding = _finding("STATE_IDENTITY_MUTATION")
    content = policy.decide(_context(finding=finding, repeated=(CONTENT, CONTENT)))
    prior = tuple(
        RepairActionReceipt(
            receipt_id=StableId(f"receipt.poison.{index}"),
            action=RepairAction.CURATOR_REPAIR,
            directive_id=StableId(f"directive.poison.{index}"),
            candidate_id=StableId("candidate.repair-policy"),
            reason_codes=("STATE_IDENTITY_MUTATION",),
        )
        for index in range(2)
    )
    signature = policy.decide(_context(finding=finding, prior=prior))
    assert content.action is RepairAction.QUARANTINE_OPERATION
    assert signature.action is RepairAction.QUARANTINE_OPERATION
    assert content.operation_ids == (OPERATION,)
    assert signature.operation_ids == (OPERATION,)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    (
        (GuardianOutcome.REVISE, RepairAction.CURATOR_REPAIR),
        (GuardianOutcome.REJECT, RepairAction.QUARANTINE_OPERATION),
        (GuardianOutcome.HUMAN_REVIEW, RepairAction.HUMAN),
        (GuardianOutcome.APPROVE, RepairAction.DETERMINISTIC_REPAIR),
    ),
)
def test_guardian_outcomes_route_one_edge(outcome: GuardianOutcome, expected: RepairAction) -> None:
    result = BoundedMemoryRepairPolicy().decide(
        _context(finding=_finding("STATE_IDENTITY_MUTATION"), guardian=outcome)
    )
    assert result.action is expected


@pytest.mark.parametrize(
    ("outcome", "expected"),
    (
        (WriteGateOutcome.REQUIRE_HUMAN, RepairAction.HUMAN),
        (WriteGateOutcome.REQUIRE_GUARDIAN, RepairAction.GUARDIAN_REVIEW),
        (WriteGateOutcome.BLOCK_GUARDIAN, RepairAction.QUARANTINE_OPERATION),
        (WriteGateOutcome.BLOCK_HUMAN, RepairAction.QUARANTINE_OPERATION),
        (WriteGateOutcome.ALLOW_COMMIT, RepairAction.DETERMINISTIC_REPAIR),
    ),
)
def test_gate_outcomes_route_one_edge(outcome: WriteGateOutcome, expected: RepairAction) -> None:
    result = BoundedMemoryRepairPolicy().decide(
        _context(finding=_finding("STATE_IDENTITY_MUTATION"), gate=outcome)
    )
    assert result.action is expected


def test_empty_findings_route_guardian_or_fatal() -> None:
    policy = BoundedMemoryRepairPolicy()
    guardian = policy.decide(_context(requires_guardian=True))
    fatal = policy.decide(_context())
    assert guardian.action is RepairAction.GUARDIAN_REVIEW
    assert fatal.action is RepairAction.STOP_FATAL


@pytest.mark.parametrize(
    ("finding", "expected"),
    (
        (_finding("FUTURE_EVIDENCE"), RepairAction.STOP_FATAL),
        (_finding("BASE_MISMATCH"), RepairAction.REPLAN),
        (_finding("UNKNOWN_HUMAN", human=True), RepairAction.HUMAN),
        (
            _finding("INVALID_EVIDENCE_REF", refresh=True),
            RepairAction.RETRY_AFTER_SOURCE_CONTEXT_REFRESH,
        ),
        (_finding("TRUTH_PROMOTION"), RepairAction.GUARDIAN_REVIEW),
        (_finding("ILLEGAL_STATE_TRANSITION"), RepairAction.CURATOR_REPAIR),
        (_finding("STATE_IDENTITY_MUTATION"), RepairAction.DETERMINISTIC_REPAIR),
    ),
)
def test_finding_registry_routes_expected_action(finding: Any, expected: RepairAction) -> None:
    assert BoundedMemoryRepairPolicy().decide(_context(finding=finding)).action is expected


def test_deterministic_repeat_and_exhaustion_fall_back_to_curator() -> None:
    finding = _finding("STATE_IDENTITY_MUTATION")
    prior = (
        RepairActionReceipt(
            receipt_id=StableId("receipt.deterministic"),
            action=RepairAction.DETERMINISTIC_REPAIR,
            directive_id=StableId("directive.deterministic"),
            candidate_id=StableId("candidate.repair-policy"),
            reason_codes=("STATE_IDENTITY_MUTATION",),
        ),
    )
    policy = BoundedMemoryRepairPolicy()
    repeated = policy.decide(_context(finding=finding, prior=prior))
    exhausted = policy.decide(_context(finding=finding, budget=_budget(normalization_passes=0)))
    assert repeated.action is RepairAction.CURATOR_REPAIR
    assert exhausted.action is RepairAction.CURATOR_REPAIR


def test_model_budgets_stop_curator_and_guardian() -> None:
    policy = BoundedMemoryRepairPolicy()
    curator = policy.decide(
        _context(
            finding=_finding("ILLEGAL_STATE_TRANSITION"),
            budget=_budget(curator_repairs=0),
        )
    )
    guardian = policy.decide(
        _context(
            finding=_finding("TRUTH_PROMOTION"),
            budget=_budget(guardian_reviews=0),
        )
    )
    assert curator.action is RepairAction.STOP_BUDGET_EXHAUSTED
    assert guardian.action is RepairAction.STOP_BUDGET_EXHAUSTED


def test_refresh_exhaustion_uses_curator_then_budget_stop() -> None:
    finding = _finding("INVALID_EVIDENCE_REF", refresh=True)
    policy = BoundedMemoryRepairPolicy()
    curator = policy.decide(_context(finding=finding, budget=_budget(context_refreshes=0)))
    stopped = policy.decide(
        _context(
            finding=finding,
            budget=_budget(context_refreshes=0, curator_repairs=0),
        )
    )
    assert curator.action is RepairAction.CURATOR_REPAIR
    assert stopped.action is RepairAction.STOP_BUDGET_EXHAUSTED


def test_unknown_retryability_fallbacks_are_fail_closed() -> None:
    policy = BoundedMemoryRepairPolicy()
    fatal = policy.decide(
        _context(
            finding=_finding(
                "UNKNOWN_FATAL",
                retryability=FindingRetryability.NON_REPAIRABLE,
            )
        )
    )
    guardian = policy.decide(
        _context(finding=_finding("UNKNOWN_REVIEW", retryability=FindingRetryability.REVIEW))
    )
    curator = policy.decide(
        _context(finding=_finding("UNKNOWN_REPAIR", retryability=FindingRetryability.CONDITIONAL))
    )
    assert fatal.action is RepairAction.STOP_FATAL
    assert guardian.action is RepairAction.GUARDIAN_REVIEW
    assert curator.action is RepairAction.CURATOR_REPAIR


def test_registered_refresh_rule_without_refresh_flag_uses_rule_action() -> None:
    finding = _finding("INVALID_EVIDENCE_REF", refresh=False)
    result = BoundedMemoryRepairPolicy().decide(_context(finding=finding))
    assert result.action is RepairAction.RETRY_AFTER_SOURCE_CONTEXT_REFRESH


def test_unhandled_registered_action_falls_back_to_curator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_agent.services.memory_repair_policy as module

    rule = FindingRule(
        code="CUSTOM_HUMAN_ACTION",
        retryability=FindingRetryability.REPAIRABLE,
        strategies=(),
        default_action=RepairAction.HUMAN,
        category="candidate",
    )
    monkeypatch.setitem(module._RULES, rule.code, rule)
    result = BoundedMemoryRepairPolicy().decide(_context(finding=_finding(rule.code)))
    assert result.action is RepairAction.CURATOR_REPAIR

    replan = FindingRule(
        code="CUSTOM_REPLAN_ACTION",
        retryability=FindingRetryability.NON_REPAIRABLE,
        strategies=(),
        default_action=RepairAction.REPLAN,
        category="basis",
    )
    monkeypatch.setitem(module._RULES, replan.code, replan)
    result = BoundedMemoryRepairPolicy().decide(_context(finding=_finding(replan.code)))
    assert result.action is RepairAction.REPLAN


def test_directive_filters_operations_outside_explicit_scope() -> None:
    policy = BoundedMemoryRepairPolicy()
    outside = StableId("operation.outside")
    directive = policy._directive(
        RepairAction.DETERMINISTIC_REPAIR,
        operation_ids=(OPERATION, outside),
        allowed_scope=RepairScope(operation_ids=(OPERATION,)),
    )
    assert directive.operation_ids == (OPERATION,)
