"""Frozen Pre-Candidate proposal contracts, poison bounds, and crash recovery."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.changes import (
    ChangeOperationType,
    ChapterChangeDraft,
    CuratedOperationDraft,
    CuratorEntityRecord,
    CuratorEvidenceSelection,
    ObservedChangeSet,
    WorldRecordKind,
)
from novel_agent.domain.ids import ArtifactId, RunId, StableId
from novel_agent.domain.memory_write import (
    CuratorProposalAccepted,
    CuratorProposalAttemptReceipt,
    CuratorProposalAttemptStatus,
    CuratorProposalRejected,
    CuratorProposalRejection,
    CuratorProposalRepairDirective,
    CuratorWorldProposalInput,
    MemoryWriteBudget,
    MemoryWriteBudgetRemaining,
    MemoryWriteCommitProfile,
    MemoryWriteState,
    MemoryWriteWorkflowStatus,
    ProposalConflict,
    ProposalHumanDecisionKind,
    ProposalHumanReviewDecision,
    ProposalRejectionKind,
    ProposalRejectionStage,
    ProposalRepairScope,
)
from novel_agent.ports.memory_write import (
    CuratorProposalAttemptRequest,
    CuratorProposalTransportError,
)
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.information_boundary import InformationBoundaryViolation
from novel_agent.services.memory_write_workflow import (
    InMemoryArtifactRepository,
    InMemoryCandidateLineageRepository,
    InMemoryCheckpointRepository,
    InMemoryCommitPort,
    LocalMemoryWriteWorkflow,
    MemoryWriteIdentityCollision,
    MemoryWriteWorkflowError,
    _WorkflowData,
)
from novel_agent.services.model_curation import (
    CuratorProposalSemanticRejected,
    ModelCurator,
)
from novel_agent.services.pre_candidate_repair import (
    BoundedPreCandidateRepairPolicy,
    InMemoryCuratorProposalAttemptRepository,
    ProposalAttemptIdentityCollision,
    proposal_rejection_signature,
    requested_attempt,
)
from tests.contract.test_memory_write_workflow_contract import (
    BASE,
    _artifact,
    _BoundarySpy,
    _Canonical,
    _contract,
    _id,
    _request,
)


def _selection(block: str = "block.1") -> CuratorEvidenceSelection:
    return CuratorEvidenceSelection(
        block_id=StableId(block),
        start=0,
        end=1,
    )


def _operation(
    target: str,
    *,
    label: str = "Lin",
    block: str = "block.1",
) -> CuratedOperationDraft:
    return CuratedOperationDraft(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.ENTITY,
        target_id=StableId(target),
        record=CuratorEntityRecord(entity_type="person", internal_label=label),
        evidence_refs=(_selection(block),),
    )


def _curator_request(*, budget: MemoryWriteBudget | None = None) -> Any:
    request = _request()
    return request.model_copy(
        update={
            "world_mutation": CuratorWorldProposalInput(
                curator_agent_spec=_contract("agent.curator.proposal")
            ),
            "budget": budget or request.budget,
        }
    )


def _chapter_curator_request() -> Any:
    request = _request(
        profile=MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC,
        chapter_text_changed=True,
    )
    return request.model_copy(
        update={
            "world_mutation": CuratorWorldProposalInput(
                curator_agent_spec=_contract("agent.curator.proposal")
            ),
            "budget": request.budget.model_copy(update={"max_curator_proposal_attempts": 4}),
        }
    )


def _requested(request: Any, attempt_no: int = 1) -> CuratorProposalAttemptReceipt:
    return requested_attempt(
        attempt_id=StableId(f"proposal-attempt.{request.request_id.root}.{attempt_no}"),
        workflow_request_id=request.request_id,
        run_id=request.run_id,
        task_id=request.task_id,
        attempt_no=attempt_no,
        base_commit=request.base_commit,
        boundary_id=request.information_boundary.boundary_id,
        configuration_fingerprint=request.configuration_fingerprint,
        prompt_fingerprint=request.configuration_fingerprint,
    )


class _RejectedCurator:
    def __init__(self, artifacts: InMemoryArtifactRepository) -> None:
        self.artifacts = artifacts
        self.requests: list[CuratorProposalAttemptRequest] = []

    async def propose_attempt(
        self, request: CuratorProposalAttemptRequest
    ) -> CuratorProposalAccepted | CuratorProposalRejected:
        self.requests.append(request)
        output = self.artifacts.put(
            b'{"duplicate":true}',
            "application/vnd.novel-agent.curator-proposal-draft-untrusted+json",
            request.request.canonical_root_refs.schema_version,
        )
        signature = _id("7")
        rejection = CuratorProposalRejection(
            rejection_id=StableId(f"rejection.{request.attempt_id.root}"),
            attempt_id=request.attempt_id,
            workflow_request_id=request.request.request_id,
            base_commit=request.request.base_commit,
            stage=ProposalRejectionStage.STRUCTURED_SCHEMA,
            kind=ProposalRejectionKind.DUPLICATE_TARGET,
            reason_code="CURATOR_PROPOSAL_DUPLICATE_TARGET",
            retryable=True,
            rejection_signature=signature,
            output_hash=output.artifact_id,
            validation_error_paths=("operations",),
            safe_feedback=("Return one operation for each record identity.",),
            raw_draft_ref=output,
            created_at=datetime.now(UTC),
        )
        rejection_ref = self.artifacts.put(
            canonical_json_bytes(rejection.model_dump(mode="json")),
            "application/vnd.novel-agent.curator-proposal-rejection+json",
            request.request.canonical_root_refs.schema_version,
        )
        call_ref = self.artifacts.put(
            canonical_json_bytes({"request_id": request.model_request_id.root}),
            "application/vnd.novel-agent.model-call-record+json",
            request.request.canonical_root_refs.schema_version,
        )
        receipt = _requested(request.request, request.attempt_no).model_copy(
            update={
                "status": CuratorProposalAttemptStatus.REJECTED,
                "model_request_ids": (request.model_request_id,),
                "model_call_receipt_refs": (call_ref,),
                "raw_response_refs": (output,),
                "output_hashes": (output.artifact_id,),
                "rejection_ref": rejection_ref,
                "provider_call_count": 1,
                "transport_attempt_count": 1,
                "completed_at": datetime.now(UTC),
            }
        )
        return CuratorProposalRejected(rejection=rejection, attempt_receipt=receipt)


class _TransportCurator:
    async def propose_attempt(self, request: CuratorProposalAttemptRequest) -> None:
        raise CuratorProposalTransportError(
            "provider timeout",
            model_request_ids=(request.model_request_id,),
        )


class _SequenceCurator(_RejectedCurator):
    def __init__(
        self,
        artifacts: InMemoryArtifactRepository,
        *,
        reject_first: bool,
    ) -> None:
        super().__init__(artifacts)
        self.reject_first = reject_first

    async def propose_attempt(
        self, request: CuratorProposalAttemptRequest
    ) -> CuratorProposalAccepted | CuratorProposalRejected:
        if self.reject_first and request.attempt_no <= 1:
            return await super().propose_attempt(request)
        self.requests.append(request)
        source = self.artifacts.put(
            b"c8",
            "application/vnd.novel-agent.chapter+json",
            request.request.canonical_root_refs.schema_version,
        )
        observed = ObservedChangeSet(
            change_set_id=StableId(f"changes.accepted.{request.attempt_id.root}"),
            base_commit=request.request.base_commit,
            source_artifact=source,
            operations=(),
        )
        normalized = self.artifacts.put(
            canonical_json_bytes(observed.model_dump(mode="json")),
            "application/vnd.novel-agent.observed-change-set+json",
            request.request.canonical_root_refs.schema_version,
        )
        call_ref = self.artifacts.put(
            canonical_json_bytes({"request_id": request.model_request_id.root}),
            "application/vnd.novel-agent.model-call-record+json",
            request.request.canonical_root_refs.schema_version,
        )
        receipt = _requested(request.request, request.attempt_no).model_copy(
            update={
                "status": CuratorProposalAttemptStatus.ACCEPTED,
                "model_request_ids": (request.model_request_id,),
                "model_call_receipt_refs": (call_ref,),
                "normalized_output_ref": normalized,
                "output_hashes": (normalized.artifact_id,),
                "agent_execution_receipt_ref": _artifact("4"),
                "producer_receipt_ref": _artifact(
                    "3",
                    "application/vnd.novel-agent.boundary-propagation-receipt+json",
                ),
                "provider_call_count": 1,
                "transport_attempt_count": 1,
                "completed_at": datetime.now(UTC),
            }
        )
        return CuratorProposalAccepted(
            observed_changes=observed,
            attempt_receipt=receipt,
        )


class _InjectedProcessCrash(BaseException):
    pass


class _FaultAt:
    def __init__(self, point: str) -> None:
        self.point = point
        self.checkpoint: ArtifactRef | None = None

    def hit(self, point: str, data: _WorkflowData) -> None:
        if point == self.point:
            self.checkpoint = data.checkpoint_ref
            raise _InjectedProcessCrash(point)


def _workflow(
    *,
    artifacts: InMemoryArtifactRepository,
    lineage: InMemoryCandidateLineageRepository,
    checkpoint: InMemoryCheckpointRepository,
    attempts: InMemoryCuratorProposalAttemptRepository,
    curator: object | None,
    commit: object | None = None,
    fault_injector: object | None = None,
    proposal_human: object | None = None,
) -> LocalMemoryWriteWorkflow:
    return LocalMemoryWriteWorkflow(
        canonical_read=_Canonical(),
        curator=curator,
        commit=commit or object(),
        information_boundary=_BoundarySpy(),
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        proposal_attempts=attempts,
        fault_injector=fault_injector,
        proposal_human=proposal_human,
    )


class _ProposalHuman:
    def __init__(self, decision: object | None = None) -> None:
        self.decision = decision
        self.requests: list[object] = []

    def request(self, request: object) -> ArtifactRef:
        self.requests.append(request)
        return _artifact("9")

    def read_decision(self, _request_id: StableId) -> object | None:
        return self.decision


def test_raw_equivalent_duplicate_identity_reaches_trusted_merge() -> None:
    payload = {
        "chapter_index": 1,
        "operations": (
            _operation("entity.same").model_dump(mode="json"),
            _operation("entity.same", block="block.2").model_dump(mode="json"),
        ),
    }

    draft = ChapterChangeDraft.model_validate_json(canonical_json_bytes(payload))
    merged, receipts = ModelCurator._merge_normalized_collisions(draft, BASE)

    assert len(merged.operations) == 1
    assert len(receipts) == 1


def test_run4_c8_characterization_is_offline_future_safe_and_replayable() -> None:
    fixture_path = (
        Path(__file__).parents[1] / "fixtures" / "stage2w" / "c8_pre_candidate_duplicate.json"
    )
    raw = fixture_path.read_text(encoding="utf-8")
    fixture = json.loads(raw)
    forbidden = ("gold", "evaluator", "chapter_9", "future_text", "c9")

    assert not any(token in raw.casefold() for token in forbidden)
    assert fixture["base_commit"] == (
        "sha256:f9a472f530355517879bca77b2c41b6eb4b91fff780a604eb6f01e4e04e84eb4"
    )
    assert all(item["chapter_index"] == 8 for item in fixture["source_refs"])
    assert fixture["pre_failure_canon"] == fixture["post_failure_canon"]
    draft = ChapterChangeDraft.model_validate_json(canonical_json_bytes(fixture["raw_draft"]))
    identities = tuple((item.record_kind, item.target_id) for item in draft.operations)
    assert len(identities) > len(set(identities))


def test_normalized_collision_merges_only_evidence_equivalent_payloads() -> None:
    equivalent = ChapterChangeDraft.model_construct(
        chapter_index=1,
        operations=(
            _operation("entity.canonical", block="block.1"),
            _operation("entity.canonical", block="block.2"),
        ),
        coverage=1.0,
        unresolved=(),
        declared_vs_observed_diff=(),
    )
    merged, receipts = ModelCurator._merge_normalized_collisions(equivalent, BASE)
    assert len(merged.operations) == 1
    assert tuple(item.block_id.root for item in merged.operations[0].evidence_refs) == (
        "block.1",
        "block.2",
    )
    assert len(receipts) == 1

    conflict = equivalent.model_copy(
        update={
            "operations": (
                equivalent.operations[0],
                _operation("entity.canonical", label="Changed", block="block.2"),
            )
        }
    )
    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_NORMALIZED_TARGET_COLLISION",
    ):
        ModelCurator._merge_normalized_collisions(conflict, BASE)


def test_attempt_repository_is_cas_and_terminal_settlement_is_immutable() -> None:
    artifacts = InMemoryArtifactRepository()
    repository = InMemoryCuratorProposalAttemptRepository(artifacts)
    request = _curator_request()
    receipt = _requested(request)
    assert repository.create_requested(receipt) == repository.create_requested(receipt)

    collision = receipt.model_copy(update={"run_id": RunId("run.other")})
    with pytest.raises(ProposalAttemptIdentityCollision):
        repository.create_requested(collision)

    running_ref = repository.mark_running(receipt.attempt_id, StableId("model.proposal.1"))
    assert running_ref == repository.mark_running(receipt.attempt_id, StableId("model.proposal.1"))
    repository.mark_running(receipt.attempt_id, StableId("model.proposal.schema-retry2"))
    assert repository.load(receipt.attempt_id).model_request_ids == (
        StableId("model.proposal.1"),
        StableId("model.proposal.schema-retry2"),
    )


def test_attempt_repository_rejects_invalid_transitions_and_double_settlement() -> None:
    artifacts = InMemoryArtifactRepository()
    repository = InMemoryCuratorProposalAttemptRepository(artifacts)
    request = _curator_request()
    requested = _requested(request)
    with pytest.raises(ValueError, match="start REQUESTED"):
        repository.create_requested(
            requested.model_copy(update={"status": CuratorProposalAttemptStatus.RUNNING})
        )
    with pytest.raises(LookupError, match="unknown proposal attempt"):
        repository.load(StableId("proposal-attempt.unknown"))
    repository.create_requested(requested)
    budget_ref = artifacts.put(
        b"budget",
        "application/vnd.novel-agent.proposal-budget-reservation+json",
        request.canonical_root_refs.schema_version,
    )
    attempt_request = CuratorProposalAttemptRequest(
        request=request,
        basis=_Canonical().load_verified(request.project_id, request.base_commit),
        attempt_id=requested.attempt_id,
        attempt_no=1,
        model_request_id=StableId("model.proposal.terminal"),
        source_artifacts=(),
        source_visibility_receipts=(),
        budget_reservation_ref=budget_ref,
    )
    outcome = asyncio.run(_RejectedCurator(artifacts).propose_attempt(attempt_request))
    assert isinstance(outcome, CuratorProposalRejected)
    with pytest.raises(ProposalAttemptIdentityCollision, match="another attempt"):
        repository.settle_rejected(
            requested.attempt_id,
            outcome.rejection.model_copy(update={"attempt_id": StableId("proposal-attempt.other")}),
            outcome.attempt_receipt,
        )
    terminal_ref = repository.settle_rejected(
        requested.attempt_id,
        outcome.rejection,
        outcome.attempt_receipt,
    )
    assert repository.reference(requested.attempt_id) == terminal_ref
    assert repository.load_rejection(requested.attempt_id) == outcome.rejection
    assert (
        repository.settle_rejected(
            requested.attempt_id,
            outcome.rejection,
            outcome.attempt_receipt,
        )
        == terminal_ref
    )
    with pytest.raises(ProposalAttemptIdentityCollision, match="cannot run again"):
        repository.mark_running(requested.attempt_id, StableId("model.after-terminal"))
    with pytest.raises(ProposalAttemptIdentityCollision, match="cannot become uncertain"):
        repository.mark_uncertain(requested.attempt_id, "late")
    with pytest.raises(ValueError, match="requires a reason"):
        InMemoryCuratorProposalAttemptRepository(artifacts).mark_uncertain(StableId("missing"), "")
    with pytest.raises(ProposalAttemptIdentityCollision, match="settled twice"):
        repository.settle_rejected(
            requested.attempt_id,
            outcome.rejection,
            outcome.attempt_receipt.model_copy(update={"output_tokens": 1}),
        )
    with pytest.raises(ProposalAttemptIdentityCollision, match="settlement changed"):
        repository.settle_rejected(
            requested.attempt_id,
            outcome.rejection.model_copy(update={"reason_code": "DIFFERENT"}),
            outcome.attempt_receipt,
        )

    second = _requested(request, 2)
    repository.create_requested(second)
    with pytest.raises(ProposalAttemptIdentityCollision, match="identity collision"):
        repository.settle_rejected(
            second.attempt_id,
            outcome.rejection.model_copy(update={"attempt_id": second.attempt_id}),
            outcome.attempt_receipt.model_copy(
                update={
                    "attempt_id": second.attempt_id,
                    "attempt_no": 99,
                }
            ),
        )
    with pytest.raises(ValueError, match="requires rejected receipt"):
        repository._settle(
            second.attempt_id,
            second,
            CuratorProposalAttemptStatus.REJECTED,
        )


def test_proposal_domain_models_reject_all_incoherent_shapes() -> None:
    artifacts = InMemoryArtifactRepository()
    request = _curator_request()
    budget_ref = artifacts.put(
        b"budget",
        "application/vnd.novel-agent.proposal-budget-reservation+json",
        request.canonical_root_refs.schema_version,
    )
    attempt_request = CuratorProposalAttemptRequest(
        request=request,
        basis=_Canonical().load_verified(request.project_id, request.base_commit),
        attempt_id=_requested(request).attempt_id,
        attempt_no=1,
        model_request_id=StableId("model.domain"),
        source_artifacts=(),
        source_visibility_receipts=(),
        budget_reservation_ref=budget_ref,
    )
    rejected = asyncio.run(_RejectedCurator(artifacts).propose_attempt(attempt_request))
    assert isinstance(rejected, CuratorProposalRejected)
    receipt = rejected.attempt_receipt

    with pytest.raises(ValidationError, match="unique and ascending"):
        ProposalConflict(
            record_kind=WorldRecordKind.ENTITY,
            target_id=StableId("entity.conflict"),
            operation_indexes=(1, 0),
            semantic_hashes=(ArtifactId("sha256:" + "1" * 64),),
        )
    for updates, message in (
        (
            {
                "stage": ProposalRejectionStage.INFORMATION_BOUNDARY,
                "retryable": True,
            },
            "cannot be retried",
        ),
        ({"safe_feedback": ("",)}, "non-empty bounded"),
        ({"safe_feedback": ("x" * 241,)}, "non-empty bounded"),
    ):
        with pytest.raises(ValidationError, match=message):
            type(rejected.rejection).model_validate(
                rejected.rejection.model_dump(mode="python") | updates
            )

    invalid_receipts: tuple[tuple[dict[str, Any], str], ...] = (
        (
            {"model_request_ids": (receipt.model_request_ids[0],) * 2},
            "must be unique",
        ),
        ({"model_call_receipt_refs": ()}, "one call receipt"),
        ({"provider_call_count": 0}, "smaller than call receipts"),
        (
            {
                "status": CuratorProposalAttemptStatus.ACCEPTED,
                "rejection_ref": None,
            },
            "requires output and producer receipts",
        ),
        (
            {
                "status": CuratorProposalAttemptStatus.REQUESTED,
                "rejection_ref": None,
                "normalized_output_ref": _artifact("4"),
                "completed_at": None,
            },
            "cannot carry accepted output",
        ),
        ({"rejection_ref": None}, "requires rejection and completion"),
        ({"completed_at": None}, "requires rejection and completion"),
        (
            {
                "status": CuratorProposalAttemptStatus.UNCERTAIN,
                "rejection_ref": None,
            },
            "cannot be completed",
        ),
    )
    for updates, message in invalid_receipts:
        with pytest.raises(ValidationError, match=message):
            CuratorProposalAttemptReceipt.model_validate(
                receipt.model_dump(mode="python") | updates
            )

    with pytest.raises(ValidationError, match="accepted attempt receipt"):
        CuratorProposalAccepted(
            observed_changes=ObservedChangeSet(
                change_set_id=StableId("changes.invalid.accepted"),
                base_commit=request.base_commit,
                source_artifact=_artifact("2"),
                operations=(),
            ),
            attempt_receipt=receipt,
        )
    accepted = asyncio.run(
        _SequenceCurator(artifacts, reject_first=False).propose_attempt(attempt_request)
    )
    assert isinstance(accepted, CuratorProposalAccepted)
    with pytest.raises(ValidationError, match="rejected attempt receipt"):
        CuratorProposalRejected(
            rejection=rejected.rejection,
            attempt_receipt=accepted.attempt_receipt,
        )
    with pytest.raises(ValidationError, match="another attempt"):
        CuratorProposalRejected(
            rejection=rejected.rejection.model_copy(
                update={"attempt_id": StableId("proposal-attempt.other")}
            ),
            attempt_receipt=receipt,
        )

    with pytest.raises(ValidationError, match="unique and ascending"):
        ProposalRepairScope(mutable_operation_indexes=(1, 0))
    with pytest.raises(ValidationError, match="complete replacement"):
        ProposalRepairScope(
            mutable_operation_indexes=(0,),
            allow_complete_replacement=True,
        )
    decision = {
        "decision_id": StableId("proposal-decision.invalid"),
        "approval_request_id": StableId("proposal-approval.invalid"),
        "workflow_request_id": request.request_id,
        "base_commit": request.base_commit,
        "decided_at": datetime.now(UTC),
    }
    with pytest.raises(ValidationError, match="requires a Draft artifact"):
        ProposalHumanReviewDecision(
            **decision,
            kind=ProposalHumanDecisionKind.TRUSTED_REPLACEMENT,
        )
    with pytest.raises(ValidationError, match="requires a reason"):
        ProposalHumanReviewDecision(
            **decision,
            kind=ProposalHumanDecisionKind.REJECT,
        )


def test_pre_candidate_policy_covers_human_boundary_and_signature_routes() -> None:
    artifacts = InMemoryArtifactRepository()
    request = _curator_request()
    budget_ref = artifacts.put(
        b"budget",
        "application/vnd.novel-agent.proposal-budget-reservation+json",
        request.canonical_root_refs.schema_version,
    )
    attempt_request = CuratorProposalAttemptRequest(
        request=request,
        basis=_Canonical().load_verified(request.project_id, request.base_commit),
        attempt_id=_requested(request).attempt_id,
        attempt_no=1,
        model_request_id=StableId("model.policy"),
        source_artifacts=(),
        source_visibility_receipts=(),
        budget_reservation_ref=budget_ref,
    )
    outcome = asyncio.run(_RejectedCurator(artifacts).propose_attempt(attempt_request))
    assert isinstance(outcome, CuratorProposalRejected)
    policy = BoundedPreCandidateRepairPolicy()
    remaining = MemoryWriteBudgetRemaining(
        candidate_revisions=1,
        curator_repairs=1,
        normalization_passes=1,
        guardian_reviews=1,
        context_refreshes=1,
        total_model_calls=1,
        token_budget=1,
        wall_clock_budget_ms=1,
    )
    human = policy.decide(
        rejection=outcome.rejection.model_copy(update={"retryable": False}),
        attempt_count=1,
        rejection_count=1,
        same_output_count=1,
        same_rejection_count=1,
        budget=request.budget,
        remaining=remaining,
    )
    assert human.action == "human_review"
    boundary = policy.decide(
        rejection=outcome.rejection.model_copy(
            update={
                "stage": ProposalRejectionStage.INFORMATION_BOUNDARY,
                "retryable": False,
            }
        ),
        attempt_count=1,
        rejection_count=1,
        same_output_count=1,
        same_rejection_count=1,
        budget=request.budget,
        remaining=remaining,
    )
    assert boundary.action == "fatal"
    progressive_budget = request.budget.model_copy(
        update={
            "max_curator_proposal_attempts": 3,
            "max_curator_proposal_rejections": 3,
            "same_content_hash_limit": 3,
            "same_finding_signature_limit": 3,
        }
    )
    second_same_finding = policy.decide(
        rejection=outcome.rejection,
        attempt_count=2,
        rejection_count=2,
        same_output_count=2,
        same_rejection_count=2,
        budget=progressive_budget,
        remaining=remaining,
    )
    assert second_same_finding.action == "retry_with_feedback"
    third_same_finding = policy.decide(
        rejection=outcome.rejection,
        attempt_count=3,
        rejection_count=3,
        same_output_count=3,
        same_rejection_count=3,
        budget=progressive_budget,
        remaining=remaining.model_copy(
            update={
                "total_model_calls": 0,
                "token_budget": 0,
                "wall_clock_budget_ms": 0,
            }
        ),
    )
    assert third_same_finding.action == "quarantine"
    third_changed_finding = policy.decide(
        rejection=outcome.rejection,
        attempt_count=3,
        rejection_count=3,
        same_output_count=1,
        same_rejection_count=1,
        budget=progressive_budget,
        remaining=remaining,
    )
    assert third_changed_finding.action == "budget_stop"
    assert proposal_rejection_signature({"reason": "same"}) == proposal_rejection_signature(
        {"reason": "same"}
    )


def _human_review_fixture(
    *,
    decision: object | None = None,
) -> tuple[LocalMemoryWriteWorkflow, _WorkflowData, _ProposalHuman]:
    artifacts = InMemoryArtifactRepository()
    request = _curator_request()
    budget_ref = artifacts.put(
        b"budget",
        "application/vnd.novel-agent.proposal-budget-reservation+json",
        request.canonical_root_refs.schema_version,
    )
    attempt_request = CuratorProposalAttemptRequest(
        request=request,
        basis=_Canonical().load_verified(request.project_id, request.base_commit),
        attempt_id=_requested(request).attempt_id,
        attempt_no=1,
        model_request_id=StableId("model.human"),
        source_artifacts=(),
        source_visibility_receipts=(),
        budget_reservation_ref=budget_ref,
    )
    outcome = asyncio.run(_RejectedCurator(artifacts).propose_attempt(attempt_request))
    assert isinstance(outcome, CuratorProposalRejected)
    human = _ProposalHuman(decision)
    attempts = InMemoryCuratorProposalAttemptRepository(artifacts)
    attempts.create_requested(_requested(request))
    attempt_ref = attempts.settle_rejected(
        outcome.attempt_receipt.attempt_id,
        outcome.rejection,
        outcome.attempt_receipt,
    )
    workflow = _workflow(
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
        checkpoint=InMemoryCheckpointRepository(artifacts),
        attempts=attempts,
        curator=object(),
        proposal_human=human,
    )
    directive = CuratorProposalRepairDirective(
        directive_id=StableId("proposal-directive.human"),
        workflow_request_id=request.request_id,
        prior_attempt_id=outcome.attempt_receipt.attempt_id,
        action="human_review",
        reason_codes=("HUMAN_REVIEW",),
        rejection_signature=outcome.rejection.rejection_signature,
        scope=ProposalRepairScope(),
    )
    directive_ref = artifacts.put(
        canonical_json_bytes(directive.model_dump(mode="json")),
        "application/vnd.novel-agent.curator-proposal-repair-directive+json",
        request.canonical_root_refs.schema_version,
    )
    rejection_ref = outcome.attempt_receipt.rejection_ref
    assert rejection_ref is not None
    data = _WorkflowData(
        request=request,
        artifacts=artifacts,
        basis=attempt_request.basis,
        state=MemoryWriteState.PROPOSAL_HUMAN_SUSPEND,
        inflight_proposal_attempt=outcome.attempt_receipt,
        proposal_rejections=[outcome.rejection],
        proposal_attempt_refs=[attempt_ref],
        proposal_rejection_refs=[rejection_ref],
        proposal_directive=directive,
        proposal_directive_ref=directive_ref,
    )
    return workflow, data, human


def _human_decision(
    data: _WorkflowData,
    kind: ProposalHumanDecisionKind,
    **updates: object,
) -> ProposalHumanReviewDecision:
    assert data.proposal_human_request is not None
    return ProposalHumanReviewDecision.model_validate(
        {
            "decision_id": StableId(f"proposal-decision.{kind.value}"),
            "approval_request_id": data.proposal_human_request.approval_request_id,
            "workflow_request_id": data.request.request_id,
            "base_commit": data.request.base_commit,
            "kind": kind,
            "decided_at": datetime.now(UTC),
            **updates,
        }
    )


def test_proposal_human_suspend_and_all_resume_decisions_are_typed() -> None:
    workflow, data, human = _human_review_fixture()
    required = workflow._proposal_human_required(data)
    assert required.status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED
    assert data.proposal_human_request is not None
    assert len(human.requests) == 1

    data.state = MemoryWriteState.PROPOSAL_HUMAN_RESUME
    waiting = workflow._resume_proposal_human(data)
    assert waiting is not None
    assert waiting.status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED
    assert len(human.requests) == 1

    human.decision = _human_decision(data, ProposalHumanDecisionKind.RETRY)
    assert workflow._resume_proposal_human(data) is None
    assert data.state is MemoryWriteState.PROPOSAL_RETRY

    workflow, data, human = _human_review_fixture()
    workflow._proposal_human_required(data)
    human.decision = _human_decision(
        data,
        ProposalHumanDecisionKind.REJECT,
        reason="operator rejected",
    )
    rejected = workflow._resume_proposal_human(data)
    assert rejected is not None
    assert rejected.status is MemoryWriteWorkflowStatus.QUARANTINED

    workflow, data, human = _human_review_fixture()
    workflow._proposal_human_required(data)
    human.decision = _human_decision(
        data,
        ProposalHumanDecisionKind.TRUSTED_REPLACEMENT,
        trusted_replacement_draft_ref=_artifact("8"),
    )
    replacement = workflow._resume_proposal_human(data)
    assert replacement is not None
    assert replacement.status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED
    assert "PROPOSAL_TRUSTED_REPLACEMENT_VALIDATION_REQUIRED" in replacement.terminal_codes

    workflow, data, human = _human_review_fixture()
    workflow._proposal_human_required(data)
    human.decision = _human_decision(data, ProposalHumanDecisionKind.RETRY).model_copy(
        update={"workflow_request_id": StableId("request.other")}
    )
    mismatch = workflow._resume_proposal_human(data)
    assert mismatch is not None
    assert mismatch.status is MemoryWriteWorkflowStatus.FATAL
    assert "PROPOSAL_HUMAN_DECISION_BINDING_MISMATCH" in mismatch.terminal_codes


def test_proposal_human_guards_missing_draft_port_and_synchronous_decision() -> None:
    workflow, data, _ = _human_review_fixture()
    data.proposal_rejections[-1] = data.proposal_rejections[-1].model_copy(
        update={"raw_draft_ref": None}
    )
    data.inflight_proposal_attempt = None
    missing = workflow._proposal_human_required(data)
    assert missing.status is MemoryWriteWorkflowStatus.FATAL
    assert "PROPOSAL_HUMAN_DRAFT_MISSING" in missing.terminal_codes

    workflow, data, _ = _human_review_fixture()
    workflow._proposal_human = None
    waiting = workflow._resume_proposal_human(data)
    assert waiting is not None
    assert waiting.status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED

    class AwaitableDecision:
        def __await__(self) -> Any:
            if False:
                yield None
            return None

    workflow, data, human = _human_review_fixture()
    workflow._proposal_human_required(data)
    human.decision = AwaitableDecision()
    with pytest.raises(MemoryWriteWorkflowError, match="must be synchronous"):
        workflow._resume_proposal_human(data)


def test_typed_proposal_port_and_workflow_identity_guards_fail_closed() -> None:
    artifacts = InMemoryArtifactRepository()
    request = _curator_request()
    attempts = InMemoryCuratorProposalAttemptRepository(artifacts)
    data = _WorkflowData(
        request=request,
        artifacts=artifacts,
        basis=_Canonical().load_verified(request.project_id, request.base_commit),
        proposal_attempt_refs=[_artifact("7")],
    )
    assert data.basis is not None
    attempt_request = CuratorProposalAttemptRequest(
        request=request,
        basis=data.basis,
        attempt_id=_requested(request).attempt_id,
        attempt_no=1,
        model_request_id=StableId("model.guard"),
        source_artifacts=(),
        source_visibility_receipts=(),
        budget_reservation_ref=_artifact("6"),
    )

    for curator, message in (
        (None, "unavailable"),
        (object(), "must implement"),
    ):
        workflow = _workflow(
            artifacts=artifacts,
            lineage=InMemoryCandidateLineageRepository(),
            checkpoint=InMemoryCheckpointRepository(artifacts),
            attempts=attempts,
            curator=curator,
        )
        with pytest.raises(MemoryWriteWorkflowError, match=message):
            asyncio.run(workflow._execute_proposal_attempt(data, attempt_request))

    class UntypedCurator:
        async def propose_attempt(self, _request: object) -> object:
            return object()

    workflow = _workflow(
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
        checkpoint=InMemoryCheckpointRepository(artifacts),
        attempts=attempts,
        curator=UntypedCurator(),
    )
    with pytest.raises(MemoryWriteWorkflowError, match="untyped outcome"):
        asyncio.run(workflow._execute_proposal_attempt(data, attempt_request))

    outcome = asyncio.run(_RejectedCurator(artifacts).propose_attempt(attempt_request))
    assert isinstance(outcome, CuratorProposalRejected)
    foreign = outcome.model_copy(
        update={
            "attempt_receipt": outcome.attempt_receipt.model_copy(
                update={"workflow_request_id": StableId("request.foreign")}
            )
        }
    )
    with pytest.raises(MemoryWriteIdentityCollision, match="another workflow"):
        workflow._settle_proposal_outcome(data, foreign)


def test_proposal_resume_detects_corrupt_attempt_and_loads_human_request() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoints = InMemoryCheckpointRepository(artifacts)
    attempts = InMemoryCuratorProposalAttemptRepository(artifacts)
    request = _curator_request()
    workflow = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoints,
        attempts=attempts,
        curator=object(),
    )
    data = _WorkflowData(
        request=request,
        artifacts=artifacts,
        basis=_Canonical().load_verified(request.project_id, request.base_commit),
        state=MemoryWriteState.CURATE_ATTEMPT_PREPARE,
    )
    assert asyncio.run(workflow._step(data)) is None
    assert data.checkpoint_ref is not None
    checkpoint = checkpoints.load(data.checkpoint_ref)
    corrupt_attempt = artifacts.put(
        b"not-json",
        "application/vnd.novel-agent.curator-proposal-attempt-receipt+json",
        request.canonical_root_refs.schema_version,
    )
    corrupt_checkpoint = checkpoints.save(
        checkpoint.model_copy(update={"inflight_proposal_attempt_ref": corrupt_attempt})
    )
    restarted = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoints,
        attempts=InMemoryCuratorProposalAttemptRepository(artifacts),
        curator=object(),
    )
    corrupt_result = asyncio.run(
        restarted.execute(request.model_copy(update={"resume_checkpoint": corrupt_checkpoint}))
    )
    assert corrupt_result.status is MemoryWriteWorkflowStatus.FATAL
    assert corrupt_result.terminal_codes[0] == "PROPOSAL_ATTEMPT_CORRUPT"

    fallback_workflow, fallback_data, _ = _human_review_fixture()
    fallback_data.proposal_rejections[-1] = fallback_data.proposal_rejections[-1].model_copy(
        update={"raw_draft_ref": None}
    )
    fallback_required = asyncio.run(fallback_workflow._step(fallback_data))
    assert fallback_required is not None
    assert fallback_required.status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED

    human_workflow, human_data, _ = _human_review_fixture()
    required = asyncio.run(human_workflow._step(human_data))
    assert required is not None
    assert required.status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED
    assert required.checkpoint_ref is not None
    human_workflow._checkpoint_states.clear()
    resumed = asyncio.run(
        human_workflow.execute(
            human_data.request.model_copy(update={"resume_checkpoint": required.checkpoint_ref})
        )
    )
    assert resumed.status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED


def test_proposal_step_routes_boundary_human_fatal_and_unsupported_policy() -> None:
    class BoundaryCurator:
        async def propose_attempt(self, _request: object) -> None:
            raise InformationBoundaryViolation("future source")

    artifacts = InMemoryArtifactRepository()
    workflow = _workflow(
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
        checkpoint=InMemoryCheckpointRepository(artifacts),
        attempts=InMemoryCuratorProposalAttemptRepository(artifacts),
        curator=BoundaryCurator(),
    )
    request = _curator_request()
    data = _WorkflowData(
        request=request,
        artifacts=artifacts,
        basis=_Canonical().load_verified(request.project_id, request.base_commit),
        state=MemoryWriteState.CURATE_ATTEMPT_PREPARE,
    )
    assert asyncio.run(workflow._step(data)) is None
    with pytest.raises(InformationBoundaryViolation, match="future source"):
        asyncio.run(workflow._step(data))

    human_workflow, human_data, _ = _human_review_fixture()
    human_data.state = MemoryWriteState.PROPOSAL_REPAIR_POLICY
    human_data.proposal_rejections[-1] = human_data.proposal_rejections[-1].model_copy(
        update={"retryable": False}
    )
    assert asyncio.run(human_workflow._step(human_data)) is None
    assert human_data.state is MemoryWriteState.PROPOSAL_HUMAN_SUSPEND
    required = asyncio.run(human_workflow._step(human_data))
    assert required is not None
    assert required.status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED
    human_data.state = MemoryWriteState.PROPOSAL_HUMAN_RESUME
    waiting = asyncio.run(human_workflow._step(human_data))
    assert waiting is not None
    assert waiting.status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED

    empty_workflow, empty_data, _ = _human_review_fixture()
    empty_data.state = MemoryWriteState.PROPOSAL_REPAIR_POLICY
    empty_data.proposal_rejections = []
    empty = asyncio.run(empty_workflow._step(empty_data))
    assert empty is not None
    assert "PROPOSAL_REPAIR_WITHOUT_REJECTION" in empty.terminal_codes

    fatal_workflow, fatal_data, _ = _human_review_fixture()
    fatal_data.state = MemoryWriteState.PROPOSAL_REPAIR_POLICY
    fatal_data.proposal_rejections[-1] = fatal_data.proposal_rejections[-1].model_copy(
        update={
            "stage": ProposalRejectionStage.INFORMATION_BOUNDARY,
            "retryable": False,
        }
    )
    fatal = asyncio.run(fatal_workflow._step(fatal_data))
    assert fatal is not None
    assert "CURATOR_PROPOSAL_INFORMATION_BOUNDARY" in fatal.terminal_codes

    unsupported_workflow, unsupported_data, _ = _human_review_fixture()
    unsupported_data.state = MemoryWriteState.PROPOSAL_REPAIR_POLICY
    directive = unsupported_data.proposal_directive
    assert directive is not None
    frozen_directive = directive

    class UnsupportedPolicy:
        def decide(self, **_: object) -> CuratorProposalRepairDirective:
            return frozen_directive.model_copy(update={"action": "deterministic_evidence_merge"})

    unsupported_workflow._proposal_policy = UnsupportedPolicy()
    unsupported = asyncio.run(unsupported_workflow._step(unsupported_data))
    assert unsupported is not None
    assert "UNSUPPORTED_PROPOSAL_REPAIR_DIRECTIVE" in unsupported.terminal_codes

    unsupported_workflow._fault_injector = object()
    unsupported_workflow._fault("non-callable", unsupported_data)


def test_only_accepted_attempt_crosses_candidate_v1_boundary_once() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = InMemoryCheckpointRepository(artifacts)
    attempts = InMemoryCuratorProposalAttemptRepository(artifacts)
    request = _curator_request()
    workflow = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        attempts=attempts,
        curator=object(),
    )
    source = artifacts.put(
        b"c8",
        "application/vnd.novel-agent.chapter+json",
        request.canonical_root_refs.schema_version,
    )
    observed = ObservedChangeSet(
        change_set_id=StableId("changes.c8.accepted"),
        base_commit=request.base_commit,
        source_artifact=source,
        operations=(),
    )
    normalized_ref = artifacts.put(
        canonical_json_bytes(observed.model_dump(mode="json")),
        "application/vnd.novel-agent.observed-change-set+json",
        request.canonical_root_refs.schema_version,
    )
    attempt = _requested(request).model_copy(
        update={
            "status": CuratorProposalAttemptStatus.ACCEPTED,
            "model_request_ids": (StableId("model.c8.proposal-1.schema-1"),),
            "model_call_receipt_refs": (_artifact("5"),),
            "normalized_output_ref": normalized_ref,
            "output_hashes": (normalized_ref.artifact_id,),
            "agent_execution_receipt_ref": _artifact("6"),
            "producer_receipt_ref": _artifact(
                "7",
                "application/vnd.novel-agent.boundary-propagation-receipt+json",
            ),
            "provider_call_count": 1,
            "transport_attempt_count": 1,
            "completed_at": datetime.now(UTC),
        }
    )
    data = _WorkflowData(
        request=request,
        artifacts=artifacts,
        basis=_Canonical().load_verified(request.project_id, request.base_commit),
        state=MemoryWriteState.PROPOSAL_VALIDATE,
        proposal_outcome=CuratorProposalAccepted(
            observed_changes=observed,
            attempt_receipt=attempt,
        ),
        inflight_proposal_attempt=attempt,
        proposal_attempt_no=1,
        proposal_attempt_refs=[_artifact("8")],
    )

    assert asyncio.run(workflow._step(data)) is None
    assert data.state is MemoryWriteState.NORMALIZE
    assert data.candidate is not None
    assert data.candidate.revision_no == 1
    assert data.candidate.origin_proposal_attempt_id == attempt.attempt_id
    assert data.candidate.proposal_attempt_chain_refs == tuple(data.proposal_attempt_refs)
    assert lineage.list_for_request(request.request_id) == (data.candidate,)
    assert data.usage.candidate_revisions == 1


def test_poison_loop_stops_without_candidate_or_commit_and_uses_new_requests() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = InMemoryCheckpointRepository(artifacts)
    attempts = InMemoryCuratorProposalAttemptRepository(artifacts)
    curator = _RejectedCurator(artifacts)
    workflow = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        attempts=attempts,
        curator=curator,
    )
    request = _curator_request()

    result = asyncio.run(workflow.execute(request))

    assert result.status is MemoryWriteWorkflowStatus.QUARANTINED
    assert result.terminal_candidate_id is None
    assert lineage.list_for_request(request.request_id) == ()
    assert len(curator.requests) == 2
    assert curator.requests[0].model_request_id != curator.requests[1].model_request_id
    assert curator.requests[0].feedback_artifact_ref is None
    assert curator.requests[1].feedback_artifact_ref is not None
    assert result.budget_usage.curator_proposal_attempts == 2
    assert result.budget_usage.candidate_revisions == 0
    assert result.checkpoint_ref is not None


def test_proposal_attempt_budget_stops_with_checkpoint_before_candidate() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = InMemoryCheckpointRepository(artifacts)
    attempts = InMemoryCuratorProposalAttemptRepository(artifacts)
    curator = _RejectedCurator(artifacts)
    workflow = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        attempts=attempts,
        curator=curator,
    )
    request = _curator_request(
        budget=MemoryWriteBudget(
            max_curator_proposal_attempts=1,
            max_curator_proposal_rejections=3,
        )
    )

    result = asyncio.run(workflow.execute(request))

    assert result.status is MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED
    assert result.checkpoint_ref is not None
    assert lineage.list_for_request(request.request_id) == ()
    assert len(curator.requests) == 1


def test_c8_first_duplicate_then_corrected_commits_one_candidate_once() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = InMemoryCheckpointRepository(artifacts)
    attempts = InMemoryCuratorProposalAttemptRepository(artifacts)
    commit = InMemoryCommitPort(current_commit=BASE)
    curator = _SequenceCurator(artifacts, reject_first=True)
    workflow = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        attempts=attempts,
        curator=curator,
        commit=commit,
    )
    request = _chapter_curator_request()

    result = asyncio.run(workflow.execute(request))

    assert result.status is MemoryWriteWorkflowStatus.COMMITTED
    assert commit.calls == 1
    assert len(curator.requests) == 2
    assert curator.requests[0].model_request_id != curator.requests[1].model_request_id
    assert curator.requests[1].feedback_artifact_ref is not None
    assert result.budget_usage.curator_proposal_attempts == 2
    assert result.budget_usage.curator_proposal_rejections == 1
    assert result.budget_usage.total_model_calls == 2
    candidates = lineage.list_for_request(request.request_id)
    assert len(candidates) == 1
    assert candidates[0].revision_no == 1


def test_transport_unavailable_is_typed_suspension_with_uncertain_attempt() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = InMemoryCheckpointRepository(artifacts)
    attempts = InMemoryCuratorProposalAttemptRepository(artifacts)
    workflow = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        attempts=attempts,
        curator=_TransportCurator(),
    )
    request = _curator_request()

    result = asyncio.run(workflow.execute(request))

    assert result.status is MemoryWriteWorkflowStatus.SUSPENDED
    assert result.checkpoint_ref is not None
    assert result.budget_usage.total_model_calls == 1
    assert attempts.list_for_workflow(request.request_id)[0].status is (
        CuratorProposalAttemptStatus.UNCERTAIN
    )


def test_new_workflow_instance_never_blindly_resends_inflight_attempt() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = InMemoryCheckpointRepository(artifacts)
    attempts = InMemoryCuratorProposalAttemptRepository(artifacts)
    curator = _RejectedCurator(artifacts)
    workflow = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        attempts=attempts,
        curator=curator,
    )
    request = _curator_request()
    data = _WorkflowData(
        request=request,
        artifacts=artifacts,
        state=MemoryWriteState.CURATE_ATTEMPT_PREPARE,
        basis=_Canonical().load_verified(request.project_id, request.base_commit),
    )
    assert asyncio.run(workflow._step(data)) is None
    assert data.checkpoint_ref is not None

    restarted = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        attempts=attempts,
        curator=curator,
    )
    resumed = request.model_copy(update={"resume_checkpoint": data.checkpoint_ref})
    result = asyncio.run(restarted.execute(resumed))

    assert result.status is MemoryWriteWorkflowStatus.SUSPENDED
    assert curator.requests == []
    assert attempts.list_for_workflow(request.request_id)[0].status is (
        CuratorProposalAttemptStatus.UNCERTAIN
    )


@pytest.mark.parametrize(
    ("point", "reject_first"),
    (
        ("attempt_checkpoint_committed", False),
        ("provider_outcome_committed", True),
        ("typed_rejection_committed", True),
        ("rejection_policy_checkpoint_committed", True),
        ("feedback_checkpoint_committed", True),
        ("accepted_attempt_committed", False),
        ("candidate_v1_committed", False),
        ("candidate_validation_committed", False),
        ("commit_request_checkpoint_committed", False),
        ("commit_accepted_before_checkpoint", False),
    ),
)
def test_real_pre_candidate_crash_points_resume_in_new_workflow_and_commit_once(
    point: str,
    reject_first: bool,
) -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = InMemoryCheckpointRepository(artifacts)
    attempts = InMemoryCuratorProposalAttemptRepository(artifacts)
    commit = InMemoryCommitPort(current_commit=BASE)
    curator = _SequenceCurator(artifacts, reject_first=reject_first)
    fault = _FaultAt(point)
    request = _chapter_curator_request()
    workflow = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        attempts=attempts,
        curator=curator,
        commit=commit,
        fault_injector=fault,
    )

    with pytest.raises(_InjectedProcessCrash, match=point):
        asyncio.run(workflow.execute(request))
    assert fault.checkpoint is not None

    resumed_request = request.model_copy(update={"resume_checkpoint": fault.checkpoint})
    restarted = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        attempts=attempts,
        curator=curator,
        commit=commit,
    )
    result = asyncio.run(restarted.execute(resumed_request))
    if result.status is MemoryWriteWorkflowStatus.SUSPENDED:
        assert result.checkpoint_ref is not None
        resumed_request = request.model_copy(update={"resume_checkpoint": result.checkpoint_ref})
        restarted = _workflow(
            artifacts=artifacts,
            lineage=lineage,
            checkpoint=checkpoint,
            attempts=attempts,
            curator=curator,
            commit=commit,
        )
        result = asyncio.run(restarted.execute(resumed_request))

    assert result.status is MemoryWriteWorkflowStatus.COMMITTED
    assert result.canonical_commit_accepted is True
    assert commit.calls == 1
    candidates = lineage.list_for_request(request.request_id)
    assert len(candidates) == 1
    assert candidates[0].revision_no == 1
    assert len({item.attempt_id for item in attempts.list_for_workflow(request.request_id)}) == len(
        attempts.list_for_workflow(request.request_id)
    )


def test_support_rejection_preserves_operation_indexes_and_json_pointers() -> None:
    """Field-level rejection metadata must flow into ProposalRepairScope."""
    from novel_agent.domain.ids import ArtifactId, CommitId, StableId
    from novel_agent.domain.memory_write import (
        CuratorProposalRejection,
        MemoryWriteBudget,
        MemoryWriteBudgetRemaining,
        ProposalRejectionKind,
        ProposalRejectionStage,
    )
    from novel_agent.services.pre_candidate_repair import (
        BoundedPreCandidateRepairPolicy,
        proposal_rejection_signature,
    )

    rejection = CuratorProposalRejection(
        rejection_id=StableId("rejection.fields"),
        attempt_id=StableId("attempt.fields"),
        workflow_request_id=StableId("workflow.fields"),
        base_commit=CommitId("sha256:" + "0" * 64),
        stage=ProposalRejectionStage.SEMANTIC_CONTRACT,
        kind=ProposalRejectionKind.INVALID_EVIDENCE,
        reason_code="CURATOR_PROPOSAL_EVIDENCE_UNSUPPORTED",
        retryable=True,
        rejection_signature=proposal_rejection_signature(
            {"reason": "unsupported", "stage": "semantic_contract"}
        ),
        output_hash=ArtifactId("sha256:" + "1" * 64),
        operation_indexes=(2, 5),
        json_pointers=("/operations/2/evidence_refs/0", "/operations/5/evidence_refs/1"),
        violation_rule="candidate_text_must_support_record",
        safe_feedback=("Replace evidence candidate for operation 2 and 5.",),
        created_at=datetime.now(UTC),
    )
    policy = BoundedPreCandidateRepairPolicy()
    directive = policy.decide(
        rejection=rejection,
        attempt_count=1,
        rejection_count=1,
        same_output_count=0,
        same_rejection_count=0,
        budget=MemoryWriteBudget(),
        remaining=MemoryWriteBudgetRemaining(
            candidate_revisions=2,
            curator_repairs=1,
            normalization_passes=3,
            guardian_reviews=2,
            context_refreshes=1,
            total_model_calls=2,
            token_budget=24000,
            wall_clock_budget_ms=180000,
        ),
    )
    scope = directive.scope
    assert scope.mutable_operation_indexes == (2, 5)
    assert scope.json_pointers == (
        "/operations/2/evidence_refs/0",
        "/operations/5/evidence_refs/1",
    )
    assert scope.violation_rule == "candidate_text_must_support_record"


def test_dangling_entity_rejection_allows_complete_draft_replacement() -> None:
    rejection = CuratorProposalRejection(
        rejection_id=StableId("rejection.dangling-entity"),
        attempt_id=StableId("attempt.dangling-entity"),
        workflow_request_id=StableId("workflow.dangling-entity"),
        base_commit=BASE,
        stage=ProposalRejectionStage.SEMANTIC_CONTRACT,
        kind=ProposalRejectionKind.DANGLING_ENTITY_REFERENCE,
        reason_code="CURATOR_PROPOSAL_DANGLING_ENTITY_REFERENCE",
        retryable=True,
        rejection_signature=proposal_rejection_signature(
            {"reason": "dangling-entity", "stage": "semantic_contract"}
        ),
        operation_indexes=(0, 1, 2, 3),
        json_pointers=(
            "/operations/0/record/subject_id",
            "/operations/1/record/subject_id",
            "/operations/2/record/subject_id",
            "/operations/3/record/subject_id",
        ),
        violation_rule="referenced_entity_must_exist_or_be_created_in_same_proposal",
        safe_feedback=("Create the missing entity or remove its dependent operations.",),
        created_at=datetime.now(UTC),
    )

    directive = BoundedPreCandidateRepairPolicy().decide(
        rejection=rejection,
        attempt_count=1,
        rejection_count=1,
        same_output_count=0,
        same_rejection_count=0,
        budget=MemoryWriteBudget(),
        remaining=MemoryWriteBudgetRemaining(
            candidate_revisions=2,
            curator_repairs=1,
            normalization_passes=3,
            guardian_reviews=2,
            context_refreshes=1,
            total_model_calls=2,
            token_budget=24_000,
            wall_clock_budget_ms=180_000,
        ),
    )

    assert directive.action == "retry_with_feedback"
    assert directive.scope.mutable_operation_indexes == ()
    assert directive.scope.allow_complete_replacement is True
