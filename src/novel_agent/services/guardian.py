"""Deterministic patch-risk routing and fail-closed Guardian write gate."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from novel_agent.domain.changes import (
    ChangeOperationType,
    ObservedChangeSet,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.ids import ProjectId, StableId
from novel_agent.domain.stage2 import (
    AuthorApprovalStatus,
    GuardianDecision,
    GuardianOutcome,
    PatchApprovalDecision,
    PatchApprovalRequest,
    PatchRiskAssessment,
    PatchRiskLevel,
    WriteGateDecision,
    WriteGateOutcome,
)
from novel_agent.services.content_addressing import content_id


class GuardianGateError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


class PatchApprovalRepository(Protocol):
    def request(self, request: PatchApprovalRequest) -> PatchApprovalRequest: ...

    def decide(self, decision: PatchApprovalDecision) -> PatchApprovalDecision: ...

    def load_request(self, approval_request_id: StableId) -> PatchApprovalRequest: ...

    def load_decision(self, approval_request_id: StableId) -> PatchApprovalDecision | None: ...


class InMemoryPatchApprovalRepository:
    def __init__(self) -> None:
        self._requests: dict[StableId, PatchApprovalRequest] = {}
        self._decisions: dict[StableId, PatchApprovalDecision] = {}

    def request(self, request: PatchApprovalRequest) -> PatchApprovalRequest:
        existing = self._requests.get(request.approval_request_id)
        if existing is not None and not PatchApprovalCoordinator.same_request_basis(
            existing, request
        ):
            raise GuardianGateError("patch approval request identity collision")
        if existing is None:
            self._requests[request.approval_request_id] = request
        return existing or request

    def decide(self, decision: PatchApprovalDecision) -> PatchApprovalDecision:
        request = self.load_request(decision.approval_request_id)
        PatchApprovalCoordinator.validate_decision_basis(request, decision)
        existing = self._decisions.get(decision.approval_request_id)
        if existing is not None and existing != decision:
            raise GuardianGateError("patch approval request already decided differently")
        if existing is None:
            self._decisions[decision.approval_request_id] = decision
            self._requests[decision.approval_request_id] = request.model_copy(
                update={"status": decision.status}
            )
        return existing or decision

    def load_request(self, approval_request_id: StableId) -> PatchApprovalRequest:
        try:
            return self._requests[approval_request_id]
        except KeyError as error:
            raise GuardianGateError("unknown patch approval request") from error

    def load_decision(self, approval_request_id: StableId) -> PatchApprovalDecision | None:
        return self._decisions.get(approval_request_id)


class PatchApprovalCoordinator:
    def __init__(
        self,
        repository: PatchApprovalRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._clock = clock

    def request(
        self,
        project_id: ProjectId,
        changes: ObservedChangeSet,
        risk: PatchRiskAssessment,
        guardian: GuardianDecision,
    ) -> PatchApprovalRequest:
        if (
            risk.change_set_id != changes.change_set_id
            or risk.base_commit != changes.base_commit
            or guardian.proposal_id != changes.change_set_id
            or guardian.base_commit != changes.base_commit
        ):
            raise GuardianGateError("patch approval inputs do not share one candidate basis")
        if not (risk.requires_human_review or guardian.outcome is GuardianOutcome.HUMAN_REVIEW):
            raise GuardianGateError("patch does not require human approval")
        request_hash = content_id(
            {
                "project_id": project_id.root,
                "change_set_id": changes.change_set_id.root,
                "base_commit": changes.base_commit.root,
                "risk_assessment_id": risk.assessment_id.root,
                "guardian_decision_id": guardian.decision_id.root,
            }
        )
        request = PatchApprovalRequest(
            approval_request_id=StableId(
                f"patch-approval.{request_hash.root.removeprefix('sha256:')[:24]}"
            ),
            project_id=project_id,
            change_set_id=changes.change_set_id,
            base_commit=changes.base_commit,
            risk_assessment_id=risk.assessment_id,
            guardian_decision_id=guardian.decision_id,
            requested_at=self._clock(),
        )
        return self._repository.request(request)

    @staticmethod
    def same_request_basis(
        left: PatchApprovalRequest,
        right: PatchApprovalRequest,
    ) -> bool:
        return (
            left.approval_request_id,
            left.project_id,
            left.change_set_id,
            left.base_commit,
            left.risk_assessment_id,
            left.guardian_decision_id,
        ) == (
            right.approval_request_id,
            right.project_id,
            right.change_set_id,
            right.base_commit,
            right.risk_assessment_id,
            right.guardian_decision_id,
        )

    @staticmethod
    def validate_decision_basis(
        request: PatchApprovalRequest,
        decision: PatchApprovalDecision,
    ) -> None:
        if (
            request.approval_request_id != decision.approval_request_id
            or request.project_id != decision.project_id
            or request.change_set_id != decision.change_set_id
            or request.base_commit != decision.base_commit
            or request.risk_assessment_id != decision.risk_assessment_id
            or request.guardian_decision_id != decision.guardian_decision_id
        ):
            raise GuardianGateError("patch approval decision does not match request basis")


class PatchRiskClassifier:
    """Classifies only explicit structural fields; free-text model claims grant no authority."""

    _LIFECYCLE_EVENTS = frozenset({"death", "resurrection", "identity_reveal"})
    _OWNERSHIP_PREDICATES = frozenset({"owner", "owns", "holder", "possesses"})
    _NON_FACT_TRUTH = frozenset({"assertion", "rumor", "dream", "prediction"})
    _DESTRUCTIVE_ACTIONS = frozenset({"forget", "retcon", "merge", "split"})

    def assess(
        self,
        changes: ObservedChangeSet,
        validation: ValidationReport,
    ) -> PatchRiskAssessment:
        high: set[str] = set()
        critical: set[str] = set()
        if validation.status is ValidationStatus.NEEDS_REVIEW:
            high.add("VALIDATOR_NEEDS_REVIEW")
        if any("CONFLICT" in finding.code for finding in validation.findings):
            high.add("EVIDENCE_CONFLICT")
        for operation in changes.operations:
            if operation.operation is ChangeOperationType.RETIRE:
                critical.add("DESTRUCTIVE_RETIRE")
            payload = operation.payload
            if not isinstance(payload, dict):
                continue
            record_type = payload.get("record_type")
            raw_record = payload.get("record")
            if not isinstance(raw_record, dict):
                continue
            if record_type == "state" and operation.operation is ChangeOperationType.REPLACE:
                high.add("STATE_OVERWRITE")
            event_kind = raw_record.get("event_type", raw_record.get("kind"))
            if record_type == "event" and event_kind in self._LIFECYCLE_EVENTS:
                high.add("LIFECYCLE_OR_IDENTITY_EVENT")
            predicate = raw_record.get("predicate")
            if (
                record_type == "relation"
                and operation.operation is ChangeOperationType.REPLACE
                and predicate in self._OWNERSHIP_PREDICATES
            ):
                high.add("UNIQUE_OWNERSHIP_TRANSFER")
            if (
                raw_record.get("truth_class") == "accepted_world_fact"
                and raw_record.get("prior_truth_class") in self._NON_FACT_TRUTH
            ):
                critical.add("TRUTH_PROMOTION")
            if raw_record.get("action") in self._DESTRUCTIVE_ACTIONS:
                critical.add("DESTRUCTIVE_MEMORY_ACTION")
        codes = tuple(sorted((*critical, *high)))
        level = (
            PatchRiskLevel.CRITICAL
            if critical
            else PatchRiskLevel.HIGH
            if high
            else PatchRiskLevel.LOW
        )
        deterministic_block = validation.status is ValidationStatus.FAILED
        return PatchRiskAssessment(
            assessment_id=StableId(f"patch-risk.{changes.change_set_id.root}"),
            change_set_id=changes.change_set_id,
            base_commit=changes.base_commit,
            level=level,
            risk_codes=codes,
            requires_guardian=not deterministic_block and level is not PatchRiskLevel.LOW,
            requires_human_review=not deterministic_block and level is PatchRiskLevel.CRITICAL,
        )


class GuardianWriteGate:
    """Guardian may add restrictions, but can never override deterministic rejection."""

    def decide(
        self,
        changes: ObservedChangeSet,
        validation: ValidationReport,
        risk: PatchRiskAssessment,
        *,
        guardian: GuardianDecision | None = None,
        human_approval: PatchApprovalDecision | None = None,
    ) -> WriteGateDecision:
        if human_approval is not None and guardian is None:
            raise GuardianGateError("human approval requires its Guardian decision")
        if risk.change_set_id != changes.change_set_id or risk.base_commit != changes.base_commit:
            raise GuardianGateError("risk assessment does not belong to the candidate patch")
        outcome: WriteGateOutcome
        reasons: tuple[str, ...]
        guardian_id = guardian.decision_id if guardian else None
        human_decision_id = human_approval.decision_id if human_approval else None
        if validation.status is ValidationStatus.FAILED:
            outcome = WriteGateOutcome.BLOCK_VALIDATION
            reasons = tuple(finding.code for finding in validation.findings) or (
                "DETERMINISTIC_VALIDATION_FAILED",
            )
        elif risk.requires_guardian and guardian is None:
            outcome = WriteGateOutcome.REQUIRE_GUARDIAN
            reasons = risk.risk_codes
        else:
            if guardian is not None:
                if (
                    guardian.proposal_id != changes.change_set_id
                    or guardian.base_commit != changes.base_commit
                ):
                    raise GuardianGateError(
                        "Guardian decision does not belong to the candidate patch"
                    )
                if guardian.outcome in {GuardianOutcome.REJECT, GuardianOutcome.REVISE}:
                    if human_approval is not None:
                        raise GuardianGateError(
                            "human approval cannot override Guardian reject or revise"
                        )
                    outcome = WriteGateOutcome.BLOCK_GUARDIAN
                    reasons = guardian.reasons
                else:
                    requires_human = (
                        guardian.outcome is GuardianOutcome.HUMAN_REVIEW
                        or risk.requires_human_review
                    )
                    if requires_human and human_approval is None:
                        outcome = WriteGateOutcome.REQUIRE_HUMAN
                        reasons = guardian.reasons or risk.risk_codes
                    elif requires_human:
                        assert human_approval is not None
                        self._validate_human_approval(
                            changes,
                            risk,
                            guardian,
                            human_approval,
                        )
                        if human_approval.status is AuthorApprovalStatus.APPROVED:
                            outcome = WriteGateOutcome.ALLOW_COMMIT
                            reasons = ()
                        else:
                            outcome = WriteGateOutcome.BLOCK_HUMAN
                            reasons = (human_approval.reason,)
                    else:
                        if human_approval is not None:
                            raise GuardianGateError(
                                "human approval supplied for a patch that does not require it"
                            )
                        outcome = WriteGateOutcome.ALLOW_COMMIT
                        reasons = ()
            else:
                outcome = WriteGateOutcome.ALLOW_COMMIT
                reasons = ()
        return WriteGateDecision(
            decision_id=StableId(f"write-gate.{changes.change_set_id.root}"),
            change_set_id=changes.change_set_id,
            base_commit=changes.base_commit,
            outcome=outcome,
            risk_assessment_id=risk.assessment_id,
            guardian_decision_id=guardian_id,
            human_approval_decision_id=human_decision_id,
            reasons=reasons,
        )

    @staticmethod
    def _validate_human_approval(
        changes: ObservedChangeSet,
        risk: PatchRiskAssessment,
        guardian: GuardianDecision,
        approval: PatchApprovalDecision,
    ) -> None:
        if (
            approval.change_set_id != changes.change_set_id
            or approval.base_commit != changes.base_commit
            or approval.risk_assessment_id != risk.assessment_id
            or approval.guardian_decision_id != guardian.decision_id
        ):
            raise GuardianGateError("human approval does not belong to the candidate patch")
