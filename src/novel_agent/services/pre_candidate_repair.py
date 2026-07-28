"""Durable Pre-Candidate proposal attempts and bounded repair policy."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.memory_write import (
    CuratorProposalAttemptReceipt,
    CuratorProposalAttemptStatus,
    CuratorProposalRejection,
    CuratorProposalRepairDirective,
    MemoryWriteBudget,
    MemoryWriteBudgetRemaining,
    ProposalRejectionKind,
    ProposalRejectionStage,
    ProposalRepairScope,
)
from novel_agent.services.content_addressing import canonical_json_bytes

VERSION = SchemaVersion("0.1.0")


class ProposalAttemptIdentityCollision(RuntimeError):
    """An attempt identity was reused for another immutable envelope or outcome."""


class InMemoryCuratorProposalAttemptRepository:
    """CAS repository whose artifacts remain portable to a durable adapter."""

    def __init__(self, artifacts: Any) -> None:
        self._artifacts = artifacts
        self._items: dict[StableId, CuratorProposalAttemptReceipt] = {}
        self._refs: dict[StableId, ArtifactRef] = {}
        self._rejections: dict[StableId, CuratorProposalRejection] = {}

    def create_requested(self, receipt: CuratorProposalAttemptReceipt) -> ArtifactRef:
        if receipt.status is not CuratorProposalAttemptStatus.REQUESTED:
            raise ValueError("new proposal attempt must start REQUESTED")
        existing = self._items.get(receipt.attempt_id)
        if existing is not None:
            if self._identity(existing) != self._identity(receipt):
                raise ProposalAttemptIdentityCollision("proposal attempt identity collision")
            return self._refs[receipt.attempt_id]
        return self._persist(receipt)

    def mark_running(
        self,
        attempt_id: StableId,
        model_request_id: StableId,
    ) -> ArtifactRef:
        current = self.load(attempt_id)
        if current.status in {
            CuratorProposalAttemptStatus.ACCEPTED,
            CuratorProposalAttemptStatus.REJECTED,
            CuratorProposalAttemptStatus.ABANDONED,
        }:
            raise ProposalAttemptIdentityCollision("terminal proposal attempt cannot run again")
        request_ids = (
            current.model_request_ids
            if model_request_id in current.model_request_ids
            else (*current.model_request_ids, model_request_id)
        )
        return self._persist(
            current.model_copy(
                update={
                    "status": CuratorProposalAttemptStatus.RUNNING,
                    "model_request_ids": request_ids,
                }
            )
        )

    def settle_accepted(
        self,
        attempt_id: StableId,
        receipt: CuratorProposalAttemptReceipt,
    ) -> ArtifactRef:
        return self._settle(attempt_id, receipt, CuratorProposalAttemptStatus.ACCEPTED)

    def settle_rejected(
        self,
        attempt_id: StableId,
        rejection: CuratorProposalRejection,
        receipt: CuratorProposalAttemptReceipt,
    ) -> ArtifactRef:
        if rejection.attempt_id != attempt_id:
            raise ProposalAttemptIdentityCollision("rejection belongs to another attempt")
        existing = self._rejections.get(attempt_id)
        if existing is not None and existing != rejection:
            raise ProposalAttemptIdentityCollision("proposal rejection settlement changed")
        self._rejections[attempt_id] = rejection
        return self._settle(attempt_id, receipt, CuratorProposalAttemptStatus.REJECTED)

    def mark_uncertain(self, attempt_id: StableId, reason: str) -> ArtifactRef:
        if not reason:
            raise ValueError("uncertain proposal attempt requires a reason")
        current = self.load(attempt_id)
        if current.status in {
            CuratorProposalAttemptStatus.ACCEPTED,
            CuratorProposalAttemptStatus.REJECTED,
            CuratorProposalAttemptStatus.ABANDONED,
        }:
            raise ProposalAttemptIdentityCollision(
                "terminal proposal attempt cannot become uncertain"
            )
        return self._persist(
            current.model_copy(update={"status": CuratorProposalAttemptStatus.UNCERTAIN})
        )

    def load(self, attempt_id: StableId) -> CuratorProposalAttemptReceipt:
        try:
            return self._items[attempt_id]
        except KeyError as error:
            raise LookupError(f"unknown proposal attempt: {attempt_id.root}") from error

    def load_rejection(self, attempt_id: StableId) -> CuratorProposalRejection | None:
        return self._rejections.get(attempt_id)

    def list_for_workflow(
        self,
        request_id: StableId,
    ) -> tuple[CuratorProposalAttemptReceipt, ...]:
        return tuple(
            sorted(
                (item for item in self._items.values() if item.workflow_request_id == request_id),
                key=lambda item: item.attempt_no,
            )
        )

    def reference(self, attempt_id: StableId) -> ArtifactRef:
        self.load(attempt_id)
        return self._refs[attempt_id]

    def _settle(
        self,
        attempt_id: StableId,
        receipt: CuratorProposalAttemptReceipt,
        status: CuratorProposalAttemptStatus,
    ) -> ArtifactRef:
        current = self.load(attempt_id)
        if receipt.attempt_id != attempt_id or self._identity(current) != self._identity(receipt):
            raise ProposalAttemptIdentityCollision("proposal settlement identity collision")
        if receipt.status is not status:
            raise ValueError(f"proposal settlement requires {status.value} receipt")
        if current.status in {
            CuratorProposalAttemptStatus.ACCEPTED,
            CuratorProposalAttemptStatus.REJECTED,
        }:
            if current != receipt:
                raise ProposalAttemptIdentityCollision("proposal usage/outcome settled twice")
            return self._refs[attempt_id]
        return self._persist(receipt)

    def _persist(self, receipt: CuratorProposalAttemptReceipt) -> ArtifactRef:
        ref = cast(
            ArtifactRef,
            self._artifacts.put(
                canonical_json_bytes(receipt.model_dump(mode="json")),
                "application/vnd.novel-agent.curator-proposal-attempt-receipt+json",
                VERSION,
            ),
        )
        self._items[receipt.attempt_id] = receipt
        self._refs[receipt.attempt_id] = ref
        return ref

    @staticmethod
    def _identity(receipt: CuratorProposalAttemptReceipt) -> tuple[object, ...]:
        return (
            receipt.attempt_id,
            receipt.workflow_request_id,
            receipt.run_id,
            receipt.task_id,
            receipt.attempt_no,
            receipt.base_commit,
            receipt.boundary_id,
            receipt.configuration_fingerprint,
        )


class BoundedPreCandidateRepairPolicy:
    """Stop repeated invalid outputs and route only registered proposal failures."""

    def decide(
        self,
        *,
        rejection: CuratorProposalRejection,
        attempt_count: int,
        rejection_count: int,
        same_output_count: int,
        same_rejection_count: int,
        budget: MemoryWriteBudget,
        remaining: MemoryWriteBudgetRemaining,
    ) -> CuratorProposalRepairDirective:
        if rejection.stage is ProposalRejectionStage.INFORMATION_BOUNDARY:
            action: Literal[
                "retry_with_feedback",
                "deterministic_evidence_merge",
                "human_review",
                "quarantine",
                "budget_stop",
                "fatal",
            ] = "fatal"
        elif (
            same_output_count >= budget.same_content_hash_limit
            or same_rejection_count >= budget.same_finding_signature_limit
        ):
            action = "quarantine"
        elif (
            attempt_count >= budget.max_curator_proposal_attempts
            or rejection_count >= budget.max_curator_proposal_rejections
            or remaining.total_model_calls < 1
            or remaining.token_budget < 1
            or remaining.wall_clock_budget_ms < 1
        ):
            action = "budget_stop"
        elif rejection.retryable:
            action = "retry_with_feedback"
        else:
            action = "human_review"
        replace_complete_draft = (
            rejection.kind is ProposalRejectionKind.DANGLING_ENTITY_REFERENCE
        )
        mutable = (
            ()
            if replace_complete_draft
            else tuple(
                sorted(
                    {
                        index
                        for conflict in rejection.conflicts
                        for index in conflict.operation_indexes
                    }
                    | set(rejection.operation_indexes)
                )
            )
        )
        return CuratorProposalRepairDirective(
            directive_id=StableId(
                f"proposal-directive.{rejection.rejection_id.root}.{attempt_count}"
            ),
            workflow_request_id=rejection.workflow_request_id,
            prior_attempt_id=rejection.attempt_id,
            action=action,
            reason_codes=(rejection.reason_code,),
            rejection_signature=rejection.rejection_signature,
            previous_output_hash=rejection.output_hash,
            scope=ProposalRepairScope(
                mutable_operation_indexes=mutable,
                allow_complete_replacement=replace_complete_draft or not bool(mutable),
                json_pointers=rejection.json_pointers,
                violation_rule=rejection.violation_rule,
            ),
        )


def proposal_rejection_signature(payload: dict[str, object]) -> ArtifactId:
    from novel_agent.services.artifacts import sha256_id

    return sha256_id(canonical_json_bytes(payload))


def requested_attempt(
    *,
    attempt_id: StableId,
    workflow_request_id: StableId,
    run_id: Any,
    task_id: Any,
    attempt_no: int,
    base_commit: Any,
    boundary_id: StableId,
    configuration_fingerprint: ArtifactId,
    prompt_fingerprint: ArtifactId,
) -> CuratorProposalAttemptReceipt:
    return CuratorProposalAttemptReceipt(
        attempt_id=attempt_id,
        workflow_request_id=workflow_request_id,
        run_id=run_id,
        task_id=task_id,
        attempt_no=attempt_no,
        base_commit=base_commit,
        boundary_id=boundary_id,
        configuration_fingerprint=configuration_fingerprint,
        status=CuratorProposalAttemptStatus.REQUESTED,
        prompt_fingerprint=prompt_fingerprint,
        started_at=datetime.now(UTC),
    )
