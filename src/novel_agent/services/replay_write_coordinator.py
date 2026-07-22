"""Resumable Curator write-side coordinator; only an allowing gate may reach Commit."""

from __future__ import annotations

from typing import Protocol

from novel_agent.domain.changes import ValidationReport
from novel_agent.domain.ids import ProjectId, StableId
from novel_agent.domain.stage2 import (
    CanonicalWriteOutcome,
    CuratorReplayResult,
    GuardianDecision,
    PatchApprovalDecision,
    PatchApprovalRequest,
    ReplayWriteResult,
    ReplayWriteStatus,
    ScenarioChapterTransition,
    WriteGateOutcome,
)
from novel_agent.services.guardian import (
    GuardianWriteGate,
    PatchApprovalCoordinator,
    PatchRiskClassifier,
)


class CanonicalWritePort(Protocol):
    def commit(
        self,
        curator: CuratorReplayResult,
        validation: ValidationReport,
    ) -> CanonicalWriteOutcome: ...


class ReplayWriteCoordinator:
    def __init__(
        self,
        writer: CanonicalWritePort,
        *,
        risk_classifier: PatchRiskClassifier | None = None,
        write_gate: GuardianWriteGate | None = None,
        approvals: PatchApprovalCoordinator | None = None,
    ) -> None:
        self._writer = writer
        self._risk_classifier = risk_classifier or PatchRiskClassifier()
        self._write_gate = write_gate or GuardianWriteGate()
        self._approvals = approvals

    def process(
        self,
        *,
        project_id: ProjectId,
        source_id: StableId,
        curator: CuratorReplayResult,
        validation: ValidationReport,
        guardian: GuardianDecision | None = None,
        approval_request: PatchApprovalRequest | None = None,
        approval_decision: PatchApprovalDecision | None = None,
    ) -> ReplayWriteResult:
        changes = curator.observed_changes
        risk = self._risk_classifier.assess(changes, validation)
        if approval_decision is not None:
            if approval_request is None:
                raise ValueError("patch approval decision requires its persisted request")
            PatchApprovalCoordinator.validate_decision_basis(
                approval_request,
                approval_decision,
            )
            if (
                approval_request.project_id != project_id
                or approval_request.change_set_id != changes.change_set_id
                or approval_request.base_commit != changes.base_commit
                or approval_request.risk_assessment_id != risk.assessment_id
                or guardian is None
                or approval_request.guardian_decision_id != guardian.decision_id
            ):
                raise ValueError("patch approval resume basis differs from replay candidate")
        gate = self._write_gate.decide(
            changes,
            validation,
            risk,
            guardian=guardian,
            human_approval=approval_decision,
        )
        if gate.outcome is WriteGateOutcome.REQUIRE_HUMAN:
            if guardian is None or self._approvals is None:
                return ReplayWriteResult(
                    status=ReplayWriteStatus.SUSPENDED,
                    curator_result=curator,
                    validation_report_id=validation.report_id,
                    risk_assessment=risk,
                    guardian_decision=guardian,
                    approval_request=approval_request,
                    approval_decision=approval_decision,
                    write_gate=gate,
                )
            generated = self._approvals.request(project_id, changes, risk, guardian)
            if approval_request is not None and (
                not PatchApprovalCoordinator.same_request_basis(approval_request, generated)
            ):
                raise ValueError("resume supplied another patch approval request")
            return ReplayWriteResult(
                status=ReplayWriteStatus.SUSPENDED,
                curator_result=curator,
                validation_report_id=validation.report_id,
                risk_assessment=risk,
                guardian_decision=guardian,
                approval_request=generated,
                approval_decision=approval_decision,
                write_gate=gate,
            )
        if gate.outcome is not WriteGateOutcome.ALLOW_COMMIT:
            status = (
                ReplayWriteStatus.SUSPENDED
                if gate.outcome is WriteGateOutcome.REQUIRE_GUARDIAN
                else ReplayWriteStatus.BLOCKED
            )
            return ReplayWriteResult(
                status=status,
                curator_result=curator,
                validation_report_id=validation.report_id,
                risk_assessment=risk,
                guardian_decision=guardian,
                approval_request=approval_request,
                approval_decision=approval_decision,
                write_gate=gate,
            )
        write = self._writer.commit(curator, validation)
        if (
            write.change_set_id != changes.change_set_id
            or write.parent_commit != changes.base_commit
        ):
            raise ValueError("CanonicalWritePort returned another candidate basis")
        transition = ScenarioChapterTransition(
            source_id=source_id,
            parent_commit=write.parent_commit,
            resulting_commit=write.resulting_commit,
            curator_receipt=curator.receipt,
            validation_artifact=write.validation_artifact,
            projection_snapshot_id=write.projection_snapshot_id,
            freshness=write.freshness,
            checkpoint_artifacts=write.checkpoint_artifacts,
        )
        return ReplayWriteResult(
            status=ReplayWriteStatus.COMMITTED,
            curator_result=curator,
            validation_report_id=validation.report_id,
            risk_assessment=risk,
            guardian_decision=guardian,
            approval_request=approval_request,
            approval_decision=approval_decision,
            write_gate=gate,
            transition=transition,
        )
