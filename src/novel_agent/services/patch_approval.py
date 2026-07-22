"""Durable repository for resumable Guardian-triggered human patch approval."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import PatchApprovalRow
from novel_agent.domain.ids import StableId
from novel_agent.domain.stage2 import (
    AuthorApprovalStatus,
    PatchApprovalDecision,
    PatchApprovalRequest,
)
from novel_agent.services.guardian import GuardianGateError, PatchApprovalCoordinator


class SqlPatchApprovalRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def request(self, request: PatchApprovalRequest) -> PatchApprovalRequest:
        with self._session_factory() as session, session.begin():
            row = session.get(PatchApprovalRow, request.approval_request_id.root)
            if row is not None:
                persisted = PatchApprovalRequest.model_validate_json(
                    json.dumps(row.request_json)
                ).model_copy(update={"status": AuthorApprovalStatus(row.status)})
                if not PatchApprovalCoordinator.same_request_basis(persisted, request):
                    raise GuardianGateError("patch approval request identity collision")
                return persisted
            session.add(
                PatchApprovalRow(
                    approval_request_id=request.approval_request_id.root,
                    project_id=request.project_id.root,
                    change_set_id=request.change_set_id.root,
                    base_commit=request.base_commit.root,
                    status=request.status.value,
                    request_json=request.model_dump(mode="json"),
                    decision_json=None,
                    requested_at=request.requested_at,
                    decided_at=None,
                )
            )
        return request

    def decide(self, decision: PatchApprovalDecision) -> PatchApprovalDecision:
        with self._session_factory() as session, session.begin():
            row = session.get(PatchApprovalRow, decision.approval_request_id.root)
            if row is None:
                raise GuardianGateError("unknown patch approval request")
            request = PatchApprovalRequest.model_validate_json(json.dumps(row.request_json))
            PatchApprovalCoordinator.validate_decision_basis(request, decision)
            if row.decision_json is not None:
                persisted = PatchApprovalDecision.model_validate_json(json.dumps(row.decision_json))
                if persisted != decision:
                    raise GuardianGateError("patch approval request already decided differently")
                return persisted
            row.status = decision.status.value
            row.decision_json = decision.model_dump(mode="json")
            row.decided_at = decision.decided_at
        return decision

    def load_request(self, approval_request_id: StableId) -> PatchApprovalRequest:
        with self._session_factory() as session:
            row = session.get(PatchApprovalRow, approval_request_id.root)
            if row is None:
                raise GuardianGateError("unknown patch approval request")
            request = PatchApprovalRequest.model_validate_json(json.dumps(row.request_json))
            return request.model_copy(update={"status": AuthorApprovalStatus(row.status)})

    def load_decision(self, approval_request_id: StableId) -> PatchApprovalDecision | None:
        with self._session_factory() as session:
            row = session.get(PatchApprovalRow, approval_request_id.root)
            if row is None or row.decision_json is None:
                return None
            return PatchApprovalDecision.model_validate_json(json.dumps(row.decision_json))
