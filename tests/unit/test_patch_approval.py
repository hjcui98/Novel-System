"""Durable patch-approval repository identity and replay tests."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.domain.changes import ChangeOperationType, ValidationStatus
from novel_agent.domain.ids import ProjectId, StableId
from novel_agent.domain.stage2 import (
    GuardianDecision,
    GuardianOutcome,
    PatchApprovalRequest,
    PatchRiskAssessment,
)
from novel_agent.services.guardian import (
    GuardianGateError,
    PatchApprovalCoordinator,
    PatchRiskClassifier,
)
from novel_agent.services.patch_approval import SqlPatchApprovalRepository
from tests.unit.test_stage2_guardian import (
    changes,
    guardian,
    human_approval,
    operation,
    validation,
)


def _repository() -> SqlPatchApprovalRepository:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return SqlPatchApprovalRepository(build_session_factory(engine))


def _approval_basis() -> tuple[PatchApprovalRequest, PatchRiskAssessment, GuardianDecision]:
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
    return (
        PatchApprovalCoordinator(_repository()).request(
            ProjectId("project.guardian"),
            candidate,
            risk,
            guardian_decision,
        ),
        risk,
        guardian_decision,
    )


def test_patch_approval_request_decision_and_exact_replay() -> None:
    repository = _repository()
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
    request = PatchApprovalCoordinator(repository).request(
        ProjectId("project.guardian"), candidate, risk, guardian_decision
    )
    assert repository.request(request) == request
    assert repository.load_request(request.approval_request_id) == request
    assert repository.load_decision(request.approval_request_id) is None

    decision = human_approval(risk, guardian_decision).model_copy(
        update={"approval_request_id": request.approval_request_id}
    )
    assert repository.decide(decision) == decision
    assert repository.decide(decision) == decision
    assert repository.load_decision(request.approval_request_id) == decision

    collision = request.model_copy(update={"project_id": ProjectId("project.other")})
    with pytest.raises(GuardianGateError, match="identity collision"):
        repository.request(collision)
    with pytest.raises(GuardianGateError, match="decided differently"):
        repository.decide(decision.model_copy(update={"reason": "different"}))


def test_patch_approval_unknown_and_basis_errors() -> None:
    repository = _repository()
    unknown = StableId("patch-approval.unknown")
    with pytest.raises(GuardianGateError, match="unknown"):
        repository.load_request(unknown)
    assert repository.load_decision(unknown) is None

    request, risk, guardian_decision = _approval_basis()
    decision = human_approval(risk, guardian_decision).model_copy(
        update={"approval_request_id": request.approval_request_id}
    )
    with pytest.raises(GuardianGateError, match="unknown"):
        repository.decide(decision)
