"""Replay write coordination gates, resume binding, and commit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from novel_agent.domain.changes import ChangeOperationType, ValidationStatus
from novel_agent.domain.ids import CommitId, ProjectId, StableId
from novel_agent.domain.memory import FreshnessDecision, FreshnessStatus
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    AuthorApprovalStatus,
    CanonicalWriteOutcome,
    CuratorReplayResult,
    GuardianOutcome,
    PatchApprovalDecision,
    PatchApprovalRequest,
    PatchRiskAssessment,
    PatchRiskLevel,
    ReplayWriteStatus,
    WriteGateDecision,
    WriteGateOutcome,
)
from novel_agent.services.replay_write_coordinator import ReplayWriteCoordinator
from tests.factories import make_artifact
from tests.unit.test_stage2_guardian import (
    changes,
    guardian,
    operation,
    receipt,
    validation,
)

NOW = datetime(2026, 7, 23, tzinfo=UTC)
PROJECT = ProjectId("project.replay-write")


def _inputs() -> tuple[CuratorReplayResult, Any, PatchRiskAssessment]:
    observed = changes(
        operation(
            1,
            ChangeOperationType.REPLACE,
            {"record_type": "state", "record": {"predicate": "location"}},
        )
    )
    curator_receipt = receipt().model_copy(
        update={
            "agent_type": AgentType.MEMORY_CURATOR,
            "agent_mode": AgentMode.REPLAY,
            "unresolved": (),
        }
    )
    curator = CuratorReplayResult(
        observed_changes=observed,
        coverage=1,
        receipt=curator_receipt,
    )
    report = validation(ValidationStatus.PASSED)
    risk = PatchRiskAssessment(
        assessment_id=StableId("risk.replay-write"),
        change_set_id=observed.change_set_id,
        base_commit=observed.base_commit,
        level=PatchRiskLevel.LOW,
        risk_codes=(),
        requires_guardian=False,
        requires_human_review=False,
    )
    return curator, report, risk


class _Classifier:
    def __init__(self, risk: PatchRiskAssessment) -> None:
        self.risk = risk

    def assess(self, changes: object, validation: object) -> PatchRiskAssessment:
        return self.risk


class _Gate:
    def __init__(self, outcome: WriteGateOutcome, risk: PatchRiskAssessment) -> None:
        self.outcome = outcome
        self.risk = risk

    def decide(
        self,
        changes: Any,
        validation: Any,
        risk: Any,
        **kwargs: Any,
    ) -> WriteGateDecision:
        del validation, risk
        guardian_decision = kwargs.get("guardian")
        approval = kwargs.get("human_approval")
        return WriteGateDecision(
            decision_id=StableId(f"gate.{self.outcome.value}"),
            change_set_id=changes.change_set_id,
            base_commit=changes.base_commit,
            outcome=self.outcome,
            risk_assessment_id=self.risk.assessment_id,
            guardian_decision_id=None
            if guardian_decision is None
            else guardian_decision.decision_id,
            human_approval_decision_id=None if approval is None else approval.decision_id,
        )


def _approval(
    risk: PatchRiskAssessment,
    *,
    project: ProjectId = PROJECT,
) -> tuple[PatchApprovalRequest, PatchApprovalDecision]:
    guardian_decision = guardian(GuardianOutcome.HUMAN_REVIEW)
    request = PatchApprovalRequest(
        approval_request_id=StableId("approval.replay-write"),
        project_id=project,
        change_set_id=risk.change_set_id,
        base_commit=risk.base_commit,
        risk_assessment_id=risk.assessment_id,
        guardian_decision_id=guardian_decision.decision_id,
        requested_at=NOW,
    )
    decision = PatchApprovalDecision(
        decision_id=StableId("decision.replay-write"),
        approval_request_id=request.approval_request_id,
        project_id=request.project_id,
        change_set_id=request.change_set_id,
        base_commit=request.base_commit,
        risk_assessment_id=request.risk_assessment_id,
        guardian_decision_id=request.guardian_decision_id,
        status=AuthorApprovalStatus.APPROVED,
        author_id=StableId("author.replay-write"),
        reason="approved",
        decided_at=NOW,
    )
    return request, decision


def _coordinator(
    outcome: WriteGateOutcome,
    risk: PatchRiskAssessment,
    *,
    writer: object | None = None,
    approvals: object | None = None,
) -> ReplayWriteCoordinator:
    return ReplayWriteCoordinator(
        cast(Any, writer or object()),
        risk_classifier=cast(Any, _Classifier(risk)),
        write_gate=cast(Any, _Gate(outcome, risk)),
        approvals=cast(Any, approvals),
    )


def test_approval_resume_requires_request_and_exact_candidate_basis() -> None:
    curator, report, risk = _inputs()
    _, decision = _approval(risk)
    coordinator = _coordinator(WriteGateOutcome.REQUIRE_HUMAN, risk)
    with pytest.raises(ValueError, match="persisted request"):
        coordinator.process(
            project_id=PROJECT,
            source_id=StableId("source.replay"),
            curator=curator,
            validation=report,
            approval_decision=decision,
        )

    foreign_request, foreign_decision = _approval(
        risk,
        project=ProjectId("project.other"),
    )
    with pytest.raises(ValueError, match="basis differs"):
        coordinator.process(
            project_id=PROJECT,
            source_id=StableId("source.replay"),
            curator=curator,
            validation=report,
            guardian=guardian(GuardianOutcome.HUMAN_REVIEW),
            approval_request=foreign_request,
            approval_decision=foreign_decision,
        )

    request, decision = _approval(risk)
    result = coordinator.process(
        project_id=PROJECT,
        source_id=StableId("source.replay"),
        curator=curator,
        validation=report,
        guardian=guardian(GuardianOutcome.HUMAN_REVIEW),
        approval_request=request,
        approval_decision=decision,
    )
    assert result.status is ReplayWriteStatus.SUSPENDED


def test_human_and_guardian_gate_outcomes_suspend_or_block() -> None:
    curator, report, risk = _inputs()
    suspended = _coordinator(WriteGateOutcome.REQUIRE_HUMAN, risk).process(
        project_id=PROJECT,
        source_id=StableId("source.replay"),
        curator=curator,
        validation=report,
    )
    assert suspended.status is ReplayWriteStatus.SUSPENDED

    for outcome, status in (
        (WriteGateOutcome.REQUIRE_GUARDIAN, ReplayWriteStatus.SUSPENDED),
        (WriteGateOutcome.BLOCK_GUARDIAN, ReplayWriteStatus.BLOCKED),
    ):
        result = _coordinator(outcome, risk).process(
            project_id=PROJECT,
            source_id=StableId("source.replay"),
            curator=curator,
            validation=report,
        )
        assert result.status is status


def test_human_gate_generates_and_checks_persisted_request() -> None:
    curator, report, risk = _inputs()
    guardian_decision = guardian(GuardianOutcome.HUMAN_REVIEW)
    generated, _ = _approval(risk)

    class Approvals:
        def request(self, *args: object) -> PatchApprovalRequest:
            return generated

    coordinator = _coordinator(
        WriteGateOutcome.REQUIRE_HUMAN,
        risk,
        approvals=Approvals(),
    )
    result = coordinator.process(
        project_id=PROJECT,
        source_id=StableId("source.replay"),
        curator=curator,
        validation=report,
        guardian=guardian_decision,
    )
    assert result.approval_request == generated

    with pytest.raises(ValueError, match="another patch approval"):
        coordinator.process(
            project_id=PROJECT,
            source_id=StableId("source.replay"),
            curator=curator,
            validation=report,
            guardian=guardian_decision,
            approval_request=generated.model_copy(
                update={"approval_request_id": StableId("approval.other")}
            ),
        )


def test_allow_commit_rejects_foreign_writer_basis_and_builds_transition() -> None:
    curator, report, risk = _inputs()
    resulting = CommitId("sha256:" + "d" * 64)
    snapshot = StableId("snapshot.replay-write")
    freshness = FreshnessDecision(
        status=FreshnessStatus.READY,
        canonical_commit=resulting,
        r1_basis_commit=resulting,
        required_snapshot_id=snapshot,
        actual_alias_commit=resulting,
        actual_snapshot_id=snapshot,
        actual_snapshot_commit=resulting,
        reason="ready",
    )

    class Writer:
        foreign = True

        def commit(self, curator: object, validation: object) -> CanonicalWriteOutcome:
            return CanonicalWriteOutcome(
                change_set_id=StableId("changes.foreign") if self.foreign else risk.change_set_id,
                parent_commit=risk.base_commit,
                resulting_commit=resulting,
                validation_artifact=make_artifact("8"),
                projection_snapshot_id=snapshot,
                freshness=freshness,
            )

    writer = Writer()
    coordinator = _coordinator(WriteGateOutcome.ALLOW_COMMIT, risk, writer=writer)
    with pytest.raises(ValueError, match="another candidate basis"):
        coordinator.process(
            project_id=PROJECT,
            source_id=StableId("source.replay"),
            curator=curator,
            validation=report,
        )

    writer.foreign = False
    result = coordinator.process(
        project_id=PROJECT,
        source_id=StableId("source.replay"),
        curator=curator,
        validation=report,
    )
    assert result.status is ReplayWriteStatus.COMMITTED
    assert result.transition is not None
    assert result.transition.resulting_commit == resulting
