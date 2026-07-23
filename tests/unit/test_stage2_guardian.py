from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import JsonValue, ValidationError

from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.changes import (
    ChangeOperation,
    ChangeOperationType,
    ObservedChangeSet,
    ValidationFinding,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    AuthorApprovalStatus,
    ContractRef,
    ExecutionStatus,
    GuardianDecision,
    GuardianOutcome,
    PatchApprovalDecision,
    PatchRiskAssessment,
    PatchRiskLevel,
    WriteGateOutcome,
)
from novel_agent.services.guardian import (
    GuardianGateError,
    GuardianWriteGate,
    InMemoryPatchApprovalRepository,
    PatchApprovalCoordinator,
    PatchRiskClassifier,
)

VERSION = SchemaVersion("2.0.0")
HASH = ArtifactId("sha256:" + "a" * 64)
BASE = CommitId("sha256:" + "b" * 64)
NOW = datetime(2026, 7, 21, tzinfo=UTC)


def operation(
    index: int,
    operation_type: ChangeOperationType,
    payload: JsonValue,
) -> ChangeOperation:
    return ChangeOperation(
        operation_id=StableId(f"operation.{index}"),
        root_kind=RootKind.WORLD,
        operation=operation_type,
        target_id=StableId(f"target.{index}"),
        payload=payload,
    )


def changes(*operations: ChangeOperation) -> ObservedChangeSet:
    return ObservedChangeSet(
        change_set_id=StableId("changes.guardian"),
        base_commit=BASE,
        source_artifact=ArtifactRef(
            artifact_id=HASH,
            media_type="application/json",
            byte_length=1,
            schema_version=VERSION,
        ),
        operations=operations,
    )


def validation(
    status: ValidationStatus,
    *findings: ValidationFinding,
) -> ValidationReport:
    return ValidationReport(
        report_id=StableId(f"validation.{status.value}"),
        bundle_id=StableId("bundle.guardian"),
        status=status,
        findings=findings,
        schema_version=VERSION,
        validated_at=NOW,
    )


def receipt() -> AgentExecutionReceipt:
    return AgentExecutionReceipt(
        receipt_id=StableId("receipt.guardian"),
        run_id=RunId("run.guardian"),
        task_id=TaskId("task.guardian"),
        agent_spec=ContractRef(
            contract_id=StableId("agent.guardian"), version=VERSION, content_hash=HASH
        ),
        agent_type=AgentType.MEMORY_GUARDIAN,
        agent_mode=AgentMode.RISK_REVIEW,
        prompt_fingerprint=HASH,
        configuration_fingerprint=HASH,
        base_commit=BASE,
        status=ExecutionStatus.SUCCEEDED,
        started_at=NOW,
        completed_at=NOW,
        latency_ms=0,
    )


def guardian(outcome: GuardianOutcome) -> GuardianDecision:
    return GuardianDecision(
        decision_id=StableId(f"guardian.{outcome.value}"),
        proposal_id=StableId("changes.guardian"),
        base_commit=BASE,
        outcome=outcome,
        risk_codes=("RISK",),
        reasons=(outcome.value,),
        revised_candidate=(
            ArtifactRef(
                artifact_id=HASH,
                media_type="application/json",
                byte_length=1,
                schema_version=VERSION,
            )
            if outcome is GuardianOutcome.REVISE
            else None
        ),
        receipt=receipt(),
    )


def human_approval(
    risk: PatchRiskAssessment,
    guardian_decision: GuardianDecision,
) -> PatchApprovalDecision:
    return PatchApprovalDecision(
        decision_id=StableId("patch-decision.approved"),
        approval_request_id=StableId("patch-approval.request"),
        project_id=ProjectId("project.guardian"),
        change_set_id=StableId("changes.guardian"),
        base_commit=BASE,
        risk_assessment_id=risk.assessment_id,
        guardian_decision_id=guardian_decision.decision_id,
        status=AuthorApprovalStatus.APPROVED,
        author_id=StableId("author.test"),
        reason="explicit test approval",
        decided_at=NOW,
    )


def test_low_risk_patch_bypasses_guardian_and_can_commit() -> None:
    candidate = changes(
        operation(
            1,
            ChangeOperationType.CREATE,
            {"record_type": "state", "record": {"predicate": "location"}},
        ),
        operation(2, ChangeOperationType.CREATE, "opaque"),
        operation(3, ChangeOperationType.CREATE, {"record_type": "event", "record": "bad"}),
    )
    report = validation(ValidationStatus.PASSED)
    risk = PatchRiskClassifier().assess(candidate, report)
    decision = GuardianWriteGate().decide(candidate, report, risk)

    assert risk.level is PatchRiskLevel.LOW
    assert risk.risk_codes == ()
    assert decision.outcome is WriteGateOutcome.ALLOW_COMMIT


def test_classifier_routes_all_structural_high_risk_signals() -> None:
    candidate = changes(
        operation(
            1,
            ChangeOperationType.REPLACE,
            {"record_type": "state", "record": {"predicate": "alive"}},
        ),
        operation(
            2,
            ChangeOperationType.CREATE,
            {"record_type": "event", "record": {"event_type": "death"}},
        ),
        operation(
            3,
            ChangeOperationType.REPLACE,
            {"record_type": "relation", "record": {"predicate": "owner"}},
        ),
    )
    report = validation(
        ValidationStatus.NEEDS_REVIEW,
        ValidationFinding(code="EVIDENCE_CONFLICT", severity="warning", message="conflict"),
    )
    risk = PatchRiskClassifier().assess(candidate, report)

    assert risk.level is PatchRiskLevel.HIGH
    assert set(risk.risk_codes) == {
        "EVIDENCE_CONFLICT",
        "LIFECYCLE_OR_IDENTITY_EVENT",
        "STATE_OVERWRITE",
        "UNIQUE_OWNERSHIP_TRANSFER",
        "VALIDATOR_NEEDS_REVIEW",
    }
    assert risk.requires_guardian is True
    assert risk.requires_human_review is False
    assert (
        GuardianWriteGate().decide(candidate, report, risk).outcome
        is WriteGateOutcome.REQUIRE_GUARDIAN
    )
    assert (
        GuardianWriteGate()
        .decide(candidate, report, risk, guardian=guardian(GuardianOutcome.APPROVE))
        .outcome
        is WriteGateOutcome.ALLOW_COMMIT
    )


def test_critical_patch_requires_guardian_and_explicit_human_approval() -> None:
    candidate = changes(
        operation(
            1,
            ChangeOperationType.RETIRE,
            {
                "record_type": "event",
                "record": {
                    "truth_class": "accepted_world_fact",
                    "prior_truth_class": "rumor",
                    "action": "retcon",
                },
            },
        )
    )
    report = validation(ValidationStatus.PASSED)
    risk = PatchRiskClassifier().assess(candidate, report)
    gate = GuardianWriteGate()

    assert risk.level is PatchRiskLevel.CRITICAL
    assert set(risk.risk_codes) == {
        "DESTRUCTIVE_MEMORY_ACTION",
        "DESTRUCTIVE_RETIRE",
        "TRUTH_PROMOTION",
    }
    assert (
        gate.decide(candidate, report, risk, guardian=guardian(GuardianOutcome.APPROVE)).outcome
        is WriteGateOutcome.REQUIRE_HUMAN
    )
    guardian_decision = guardian(GuardianOutcome.APPROVE)
    assert (
        gate.decide(
            candidate,
            report,
            risk,
            guardian=guardian_decision,
            human_approval=human_approval(risk, guardian_decision),
        ).outcome
        is WriteGateOutcome.ALLOW_COMMIT
    )


@pytest.mark.parametrize("outcome", (GuardianOutcome.REJECT, GuardianOutcome.REVISE))
def test_guardian_reject_or_revise_blocks_patch(outcome: GuardianOutcome) -> None:
    candidate = changes(
        operation(
            1,
            ChangeOperationType.REPLACE,
            {"record_type": "state", "record": {"predicate": "injury"}},
        )
    )
    report = validation(ValidationStatus.PASSED)
    risk = PatchRiskClassifier().assess(candidate, report)

    decision = GuardianWriteGate().decide(candidate, report, risk, guardian=guardian(outcome))

    assert decision.outcome is WriteGateOutcome.BLOCK_GUARDIAN
    assert decision.reasons == (outcome.value,)


def test_guardian_human_review_outcome_requires_human() -> None:
    candidate = changes(
        operation(
            1,
            ChangeOperationType.REPLACE,
            {"record_type": "state", "record": {"predicate": "injury"}},
        )
    )
    report = validation(ValidationStatus.PASSED)
    risk = PatchRiskClassifier().assess(candidate, report)
    guardian_decision = guardian(GuardianOutcome.HUMAN_REVIEW)
    decision = GuardianWriteGate().decide(
        candidate,
        report,
        risk,
        guardian=guardian_decision,
        human_approval=human_approval(risk, guardian_decision),
    )
    assert decision.outcome is WriteGateOutcome.ALLOW_COMMIT


def test_deterministic_failure_cannot_be_overridden_by_guardian() -> None:
    candidate = changes()
    report = validation(
        ValidationStatus.FAILED,
        ValidationFinding(code="MISSING_EVIDENCE", severity="error", message="missing"),
    )
    risk = PatchRiskClassifier().assess(candidate, report)
    decision = GuardianWriteGate().decide(
        candidate, report, risk, guardian=guardian(GuardianOutcome.APPROVE)
    )
    assert risk.requires_guardian is False
    assert decision.outcome is WriteGateOutcome.BLOCK_VALIDATION
    assert decision.reasons == ("MISSING_EVIDENCE",)

    empty_report = validation(ValidationStatus.FAILED)
    fallback = GuardianWriteGate().decide(
        candidate,
        empty_report,
        PatchRiskClassifier().assess(candidate, empty_report),
    )
    assert fallback.reasons == ("DETERMINISTIC_VALIDATION_FAILED",)


def test_gate_rejects_cross_patch_risk_or_guardian_decisions() -> None:
    candidate = changes(
        operation(
            1,
            ChangeOperationType.REPLACE,
            {"record_type": "state", "record": {"predicate": "injury"}},
        )
    )
    report = validation(ValidationStatus.PASSED)
    risk = PatchRiskClassifier().assess(candidate, report)
    with pytest.raises(GuardianGateError, match="risk assessment"):
        GuardianWriteGate().decide(
            candidate,
            report,
            risk.model_copy(update={"change_set_id": StableId("changes.other")}),
        )
    foreign = guardian(GuardianOutcome.APPROVE).model_copy(
        update={"proposal_id": StableId("changes.other")}
    )
    with pytest.raises(GuardianGateError, match="Guardian decision"):
        GuardianWriteGate().decide(candidate, report, risk, guardian=foreign)


def test_patch_risk_contract_rejects_contradictory_routing() -> None:
    base = {
        "assessment_id": StableId("risk.invalid"),
        "change_set_id": StableId("changes.guardian"),
        "base_commit": BASE,
        "level": PatchRiskLevel.LOW,
        "risk_codes": (),
        "requires_guardian": False,
        "requires_human_review": False,
    }
    with pytest.raises(ValidationError, match="low-risk"):
        PatchRiskAssessment.model_validate(base | {"risk_codes": ("RISK",)})
    with pytest.raises(ValidationError, match="requires Guardian"):
        PatchRiskAssessment.model_validate(base | {"requires_human_review": True})
    with pytest.raises(ValidationError, match="critical patch"):
        PatchRiskAssessment.model_validate(
            base
            | {
                "level": PatchRiskLevel.CRITICAL,
                "risk_codes": ("RISK",),
                "requires_guardian": True,
            }
        )


def test_in_memory_patch_approval_repository_is_idempotent_and_fail_closed() -> None:
    candidate = changes(
        operation(
            1,
            ChangeOperationType.REPLACE,
            {"record_type": "event", "record": {"event_type": "death"}},
        )
    )
    report = validation(ValidationStatus.NEEDS_REVIEW)
    risk = PatchRiskClassifier().assess(candidate, report)
    guardian_decision = guardian(GuardianOutcome.HUMAN_REVIEW)
    repository = InMemoryPatchApprovalRepository()
    coordinator = PatchApprovalCoordinator(repository, clock=lambda: NOW)
    request = coordinator.request(
        ProjectId("project.guardian"),
        candidate,
        risk,
        guardian_decision,
    )
    assert repository.request(request) == request
    assert repository.load_request(request.approval_request_id) == request
    assert repository.load_decision(request.approval_request_id) is None

    with pytest.raises(GuardianGateError, match="identity collision"):
        repository.request(request.model_copy(update={"project_id": ProjectId("project.other")}))
    with pytest.raises(GuardianGateError, match="unknown"):
        repository.load_request(StableId("approval.unknown"))

    decision = human_approval(risk, guardian_decision).model_copy(
        update={"approval_request_id": request.approval_request_id}
    )
    assert repository.decide(decision) == decision
    assert repository.decide(decision) == decision
    assert repository.load_decision(request.approval_request_id) == decision
    with pytest.raises(GuardianGateError, match="decided differently"):
        repository.decide(decision.model_copy(update={"reason": "different"}))


def test_patch_approval_coordinator_rejects_unrelated_or_unnecessary_requests() -> None:
    candidate = changes()
    report = validation(ValidationStatus.PASSED)
    low = PatchRiskClassifier().assess(candidate, report)
    coordinator = PatchApprovalCoordinator(InMemoryPatchApprovalRepository())
    with pytest.raises(GuardianGateError, match="does not require"):
        coordinator.request(
            ProjectId("project.guardian"),
            candidate,
            low,
            guardian(GuardianOutcome.APPROVE),
        )
    high = low.model_copy(
        update={
            "level": PatchRiskLevel.HIGH,
            "risk_codes": ("RISK",),
            "requires_guardian": True,
        }
    )
    foreign = guardian(GuardianOutcome.HUMAN_REVIEW).model_copy(
        update={"proposal_id": StableId("changes.other")}
    )
    with pytest.raises(GuardianGateError, match="do not share"):
        coordinator.request(ProjectId("project.guardian"), candidate, high, foreign)

    request = PatchApprovalCoordinator(
        InMemoryPatchApprovalRepository(), clock=lambda: NOW
    ).request(
        ProjectId("project.guardian"),
        candidate,
        high,
        guardian(GuardianOutcome.HUMAN_REVIEW),
    )
    decision = human_approval(high, guardian(GuardianOutcome.HUMAN_REVIEW)).model_copy(
        update={
            "approval_request_id": request.approval_request_id,
            "project_id": ProjectId("project.other"),
        }
    )
    with pytest.raises(GuardianGateError, match="does not match request basis"):
        PatchApprovalCoordinator.validate_decision_basis(request, decision)


def test_guardian_gate_rejects_invalid_human_override_shapes() -> None:
    candidate = changes(
        operation(
            1,
            ChangeOperationType.REPLACE,
            {"record_type": "event", "record": {"event_type": "death"}},
        )
    )
    report = validation(ValidationStatus.NEEDS_REVIEW)
    risk = PatchRiskClassifier().assess(candidate, report)
    gate = GuardianWriteGate()
    guardian_decision = guardian(GuardianOutcome.HUMAN_REVIEW)
    approval = human_approval(risk, guardian_decision)

    with pytest.raises(GuardianGateError, match="requires its Guardian"):
        gate.decide(candidate, report, risk, human_approval=approval)
    with pytest.raises(GuardianGateError, match="cannot override"):
        gate.decide(
            candidate,
            report,
            risk,
            guardian=guardian(GuardianOutcome.REJECT),
            human_approval=approval,
        )
    rejected = approval.model_copy(update={"status": AuthorApprovalStatus.REJECTED, "reason": "no"})
    blocked = gate.decide(
        candidate,
        report,
        risk,
        guardian=guardian_decision,
        human_approval=rejected,
    )
    assert blocked.outcome is WriteGateOutcome.BLOCK_HUMAN

    low_candidate = changes()
    low_report = validation(ValidationStatus.PASSED)
    low_risk = PatchRiskClassifier().assess(low_candidate, low_report)
    with pytest.raises(GuardianGateError, match="does not require"):
        gate.decide(
            low_candidate,
            low_report,
            low_risk,
            guardian=guardian(GuardianOutcome.APPROVE),
            human_approval=human_approval(
                low_risk,
                guardian(GuardianOutcome.APPROVE),
            ),
        )
    with pytest.raises(GuardianGateError, match="does not belong"):
        gate.decide(
            candidate,
            report,
            risk,
            guardian=guardian_decision,
            human_approval=approval.model_copy(update={"change_set_id": StableId("changes.other")}),
        )
