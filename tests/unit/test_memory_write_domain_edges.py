"""Negative and normalization contracts for Stage 2W domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from novel_agent.domain.artifacts import RootKind
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, StableId
from novel_agent.domain.memory_write import (
    BlockingScope,
    BoundaryPropagationReceipt,
    CanonicalWriteBasis,
    ContinuationDecision,
    CuratorWorldProposalInput,
    FindingRetryability,
    HumanApprovalDecision,
    HumanDecisionKind,
    MemoryWriteCheckpoint,
    MemoryWriteCommitProfile,
    MemoryWriteState,
    MemoryWriteWorkflowPhase,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
    NarrativePosition,
    PlanChangeTrigger,
    ProjectionReadinessResult,
    ProjectionReadinessStatus,
    RepairAction,
    RepairDirective,
    RepairScope,
    RootUpdateKind,
    SourceProvenance,
    SourceVisibilityReceipt,
    TrustedWorldCandidateInput,
    ValidationDisposition,
    ValidationFindingCategory,
    ValidationFindingV2,
    ValidationSeverity,
)
from novel_agent.domain.runtime import EffectStatus, ResumabilityStatus
from novel_agent.ports.memory_write import (
    MemoryWriteCommitResult,
    MemoryWriteCommitStatus,
)
from novel_agent.services.memory_write_workflow import (
    InMemoryArtifactRepository,
    InMemoryCandidateLineageRepository,
    _model_ref,
)
from tests.contract.test_memory_write_workflow_contract import (
    BASE,
    PROJECT,
    _artifact,
    _contract,
    _manifest,
    _request,
)
from tests.unit.test_memory_write_resume import _ready_data
from tests.unit.test_memory_write_workflow import _workflow_and_data

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _validate(model: Any, **updates: Any) -> Any:
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return type(model).model_validate(payload)


def test_root_intent_boundary_and_propagation_receipt_invariants() -> None:
    request = _request(
        profile=MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC,
        chapter_text_changed=True,
    )
    intent = request.root_update_intents[0]
    with pytest.raises(ValidationError, match="NOOP"):
        _validate(
            intent,
            update_kind=RootUpdateKind.NOOP,
            update_artifact=_artifact("7"),
        )

    boundary = request.information_boundary
    with pytest.raises(ValidationError, match="cannot precede"):
        _validate(
            boundary,
            reveal_position=NarrativePosition(chapter_index=2),
            maximum_visible_position=NarrativePosition(chapter_index=1),
        )

    receipt = BoundaryPropagationReceipt(
        receipt_id=StableId("receipt.domain.edge"),
        boundary_id=boundary.boundary_id,
        base_commit=BASE,
        input_source_artifact_refs=(_artifact("8"),),
        source_visibility_receipt_refs=(_artifact("9"),),
        output_artifact_hash=ArtifactId("sha256:" + "7" * 64),
        builder_policy_hash=request.configuration_fingerprint,
        effective_visible_through=None,
        effective_access_scope=request.access_scope,
        receipt_hash=ArtifactId("sha256:" + "6" * 64),
    )
    with pytest.raises(ValidationError, match="direct input"):
        _validate(receipt, input_source_artifact_refs=())
    with pytest.raises(ValidationError, match="must be unique"):
        _validate(
            receipt,
            source_visibility_receipt_refs=(
                receipt.source_visibility_receipt_refs[0],
                receipt.source_visibility_receipt_refs[0],
            ),
        )


def test_canonical_basis_normalizes_alias_and_rejects_missing_or_foreign_manifest() -> None:
    manifest = _manifest()
    alias = CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=BASE,
        canonical_root_refs=manifest,
    )
    assert alias.root_manifest == manifest
    assert alias.canonical_root_refs == manifest
    with pytest.raises(ValidationError, match="requires a RootManifest"):
        CanonicalWriteBasis(project_id=PROJECT, commit_id=BASE)
    with pytest.raises(ValidationError, match="another project"):
        CanonicalWriteBasis(
            project_id=ProjectId("project.other"),
            commit_id=BASE,
            root_manifest=manifest,
        )


def test_workflow_request_rejects_misaligned_sources_intents_profiles_and_trusted_input() -> None:
    request = _request()
    other_base = CommitId("sha256:" + "9" * 64)
    with pytest.raises(ValidationError, match="information boundary"):
        _validate(
            request,
            information_boundary=request.information_boundary.model_copy(
                update={"base_commit": other_base}
            ),
        )
    with pytest.raises(ValidationError, match="visibility receipt"):
        _validate(request, source_artifacts=(_artifact("8"),))
    source = _artifact("8")
    visibility = SourceVisibilityReceipt(
        receipt_id=StableId("visibility.domain.edge"),
        source_artifact=source,
        boundary_id=request.information_boundary.boundary_id,
        visible_through=None,
        access_scope=request.access_scope,
        provenance=SourceProvenance.AUTHOR_INPUT,
        issuer=StableId("issuer.domain.edge"),
        receipt_hash=ArtifactId("sha256:" + "5" * 64),
    )
    with pytest.raises(ValidationError, match="source provenance"):
        _validate(
            request,
            source_artifacts=(source,),
            source_visibility_receipts=(visibility,),
            source_provenance=(
                SourceProvenance.AUTHOR_INPUT,
                SourceProvenance.AUTHOR_INPUT,
            ),
        )
    with pytest.raises(ValidationError, match="source provenance"):
        _validate(
            request,
            source_artifacts=(source,),
            source_visibility_receipts=(visibility,),
            source_provenance=(),
        )

    chapter = _request(
        profile=MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC,
        chapter_text_changed=True,
    )
    intent = chapter.root_update_intents[0]
    with pytest.raises(ValidationError, match="root kind"):
        _validate(
            chapter,
            root_update_intents=(intent.model_copy(update={"root_kind": RootKind.WORLD}),),
        )
    with pytest.raises(ValidationError, match="ids must be unique"):
        _validate(chapter, root_update_intents=(intent, intent))
    duplicate_kind = intent.model_copy(update={"intent_id": StableId("intent.other")})
    with pytest.raises(ValidationError, match="one intent per Root"):
        _validate(chapter, root_update_intents=(intent, duplicate_kind))
    with pytest.raises(ValidationError, match="ChapterReveal trigger"):
        _validate(request, commit_profile=MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC)
    with pytest.raises(ValidationError, match="TextRoot"):
        _validate(chapter, root_update_intents=())
    with pytest.raises(ValidationError, match="CHANGED_ROOTS_ONLY"):
        _validate(chapter, commit_profile=MemoryWriteCommitProfile.CHANGED_ROOTS_ONLY)
    canonical = _validate(
        request,
        commit_profile=MemoryWriteCommitProfile.REQUIRE_CANONICAL_COMMIT,
    )
    assert canonical.commit_profile is MemoryWriteCommitProfile.REQUIRE_CANONICAL_COMMIT
    with pytest.raises(ValidationError, match="REQUIRE_CANONICAL_COMMIT"):
        _validate(
            chapter,
            commit_profile=MemoryWriteCommitProfile.REQUIRE_CANONICAL_COMMIT,
        )
    with pytest.raises(ValidationError, match="PlanChange"):
        _validate(
            request,
            trigger=PlanChangeTrigger(plan_change_id=StableId("plan-change.edge")),
        )

    trusted_ref = _artifact("8")
    with pytest.raises(ValidationError, match="must be distinct"):
        _validate(
            request,
            world_mutation=TrustedWorldCandidateInput(
                candidate_artifact=trusted_ref,
                producer_receipt=trusted_ref,
            ),
        )


def test_result_contract_rejects_incoherent_terminal_shapes() -> None:
    base = MemoryWriteWorkflowResult(
        request_id=StableId("request.result.edge"),
        status=MemoryWriteWorkflowStatus.NOOP,
        workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
        canonical_commit_accepted=False,
        base_commit=BASE,
    )
    commit_id = CommitId("sha256:" + "8" * 64)
    receipt = _artifact("7")
    cases = (
        ({"canonical_commit_accepted": True}, "requires resulting commit"),
        (
            {
                "resulting_commit": commit_id,
                "commit_receipt": receipt,
            },
            "unaccepted workflow",
        ),
        (
            {
                "status": MemoryWriteWorkflowStatus.FATAL,
                "workflow_phase": MemoryWriteWorkflowPhase.CANON_COMMITTED,
            },
            "PRECOMMIT phase",
        ),
        (
            {
                "status": MemoryWriteWorkflowStatus.COMMITTED,
                "workflow_phase": MemoryWriteWorkflowPhase.CANON_COMMITTED,
                "canonical_commit_accepted": True,
                "resulting_commit": commit_id,
                "commit_receipt": receipt,
            },
            "completed accepted",
        ),
        (
            {
                "status": MemoryWriteWorkflowStatus.HUMAN_REQUIRED,
                "workflow_phase": MemoryWriteWorkflowPhase.PRECOMMIT,
            },
            "requires a checkpoint",
        ),
        (
            {
                "status": MemoryWriteWorkflowStatus.QUARANTINED,
                "workflow_phase": MemoryWriteWorkflowPhase.PRECOMMIT,
            },
            "quarantine artifact",
        ),
        (
            {
                "status": MemoryWriteWorkflowStatus.FATAL,
                "workflow_phase": MemoryWriteWorkflowPhase.PRECOMMIT,
                "continuation_decision": ContinuationDecision.SAFE_TO_CONTINUE,
            },
            "cannot continue safely",
        ),
    )
    for updates, message in cases:
        with pytest.raises(ValidationError, match=message):
            _validate(base, **updates)

    accepted = {
        "canonical_commit_accepted": True,
        "resulting_commit": commit_id,
        "commit_receipt": receipt,
    }
    with pytest.raises(ValidationError, match="cannot remain in PRECOMMIT"):
        _validate(
            base,
            status=MemoryWriteWorkflowStatus.FATAL,
            workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            **accepted,
        )
    with pytest.raises(ValidationError, match="requires validation"):
        _validate(
            base,
            status=MemoryWriteWorkflowStatus.COMMITTED,
            workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
            **accepted,
        )
    with pytest.raises(ValidationError, match="NOOP"):
        _validate(base, **accepted)
    with pytest.raises(ValidationError, match="post-commit"):
        _validate(
            base,
            status=MemoryWriteWorkflowStatus.FATAL,
            workflow_phase=MemoryWriteWorkflowPhase.CANON_COMMITTED,
            continuation_decision=ContinuationDecision.SAFE_TO_CONTINUE,
            **accepted,
        )


def test_degraded_result_requires_disjoint_operation_sets_and_quarantine() -> None:
    workflow, data = _workflow_and_data()
    commit_id = CommitId("sha256:" + "e" * 64)
    data.commit_result = MemoryWriteCommitResult(
        request_id=data.request.request_id,
        status=MemoryWriteCommitStatus.ACCEPTED,
        commit_id=commit_id,
        commit_receipt_ref=_artifact("7"),
    )
    data.projection = workflow._projection.request_or_read_by_effect_id(
        data.request.project_id,
        commit_id,
        StableId("effect.domain.degraded"),
    )
    committed = workflow._complete(data)
    operation = StableId("operation.domain.committed")
    quarantined = StableId("operation.domain.quarantined")
    noop = MemoryWriteWorkflowResult(
        request_id=data.request.request_id,
        status=MemoryWriteWorkflowStatus.NOOP,
        workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
        canonical_commit_accepted=False,
        base_commit=BASE,
    )
    with pytest.raises(ValidationError, match="committed candidate"):
        _validate(noop, degraded=True)
    for updates, message in (
        ({"degraded": True}, "committed and quarantined"),
        (
            {
                "degraded": True,
                "committed_operation_ids": (operation,),
                "quarantined_operation_ids": (operation,),
                "quarantine_refs": (_artifact("6"),),
            },
            "must be disjoint",
        ),
        (
            {
                "degraded": True,
                "committed_operation_ids": (operation,),
                "quarantined_operation_ids": (quarantined,),
            },
            "requires quarantine refs",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            _validate(committed, **updates)
    valid = _validate(
        committed,
        degraded=True,
        committed_operation_ids=(operation,),
        quarantined_operation_ids=(quarantined,),
        quarantine_refs=(_artifact("6"),),
    )
    assert valid.degraded is True


def test_candidate_materialization_validation_repair_projection_and_human_shapes() -> None:
    artifacts = InMemoryArtifactRepository()
    data = _ready_data(
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
    )
    assert data.candidate is not None
    assert data.materialization is not None
    assert data.validation is not None
    candidate = data.candidate
    with pytest.raises(ValidationError, match="first Candidate"):
        _validate(candidate, parent_candidate_id=StableId("candidate.parent"))
    with pytest.raises(ValidationError, match="requires a parent"):
        _validate(candidate, revision_no=2)
    with pytest.raises(ValidationError, match="supersede itself"):
        _validate(candidate, supersedes_candidate_id=candidate.candidate_id)
    with pytest.raises(ValidationError, match="attempt id and receipt together"):
        _validate(
            candidate,
            origin_proposal_attempt_id=StableId("proposal-attempt.domain.edge"),
        )
    with pytest.raises(ValidationError, match="only Candidate v1"):
        _validate(
            candidate,
            revision_no=2,
            parent_candidate_id=StableId("candidate.parent"),
            origin_proposal_attempt_id=StableId("proposal-attempt.domain.edge"),
            origin_proposal_attempt_receipt=_artifact("5"),
        )

    assert data.materialization.bundle is not None
    with pytest.raises(ValidationError, match="invalid candidate binding"):
        _validate(
            data.materialization,
            bundle=data.materialization.bundle.model_copy(
                update={"bundle_id": StableId("bundle.other")}
            ),
        )
    with pytest.raises(ValidationError, match="non-repairable"):
        _validate(
            data.validation,
            disposition=ValidationDisposition.NON_REPAIRABLE,
            findings=(),
        )
    finding = ValidationFindingV2(
        finding_id=StableId("finding.blocking.edge"),
        code="BLOCKING_EDGE",
        category=ValidationFindingCategory.UNKNOWN,
        severity=ValidationSeverity.ERROR,
        message="blocking",
        retryability=FindingRetryability.REPAIRABLE,
        blocking_scope=BlockingScope.CANDIDATE,
        allowed_repair_scope=RepairScope(),
    )
    with pytest.raises(ValidationError, match="PASS validation"):
        _validate(
            data.validation,
            disposition=ValidationDisposition.PASS,
            findings=(finding,),
        )

    operation_id = StableId("operation.domain.edge")
    with pytest.raises(ValidationError, match="allowed repair scope"):
        RepairDirective(
            directive_id=StableId("directive.domain.edge"),
            action=RepairAction.DETERMINISTIC_REPAIR,
            operation_ids=(operation_id,),
        )
    with pytest.raises(ValidationError, match="budget stop"):
        RepairDirective(
            directive_id=StableId("directive.budget.edge"),
            action=RepairAction.STOP_BUDGET_EXHAUSTED,
        )

    with pytest.raises(ValidationError, match="requires projection"):
        ProjectionReadinessResult(
            effect_id=StableId("effect.ready.edge"),
            status=ProjectionReadinessStatus.READY,
        )
    with pytest.raises(ValidationError, match="requires a reason"):
        ProjectionReadinessResult(
            effect_id=StableId("effect.pending.edge"),
            status=ProjectionReadinessStatus.PENDING,
        )

    decision = HumanApprovalDecision(
        decision_id=StableId("human.domain.edge"),
        approval_request_id=StableId("approval.domain.edge"),
        request_id=data.request.request_id,
        candidate_id=candidate.candidate_id,
        candidate_content_hash=candidate.content_hash,
        base_commit=BASE,
        kind=HumanDecisionKind.APPROVE_EXACT_CANDIDATE,
        decided_at=NOW,
    )
    for updates, message in (
        ({"kind": HumanDecisionKind.REQUEST_REVISION}, "repair directive"),
        ({"kind": HumanDecisionKind.HUMAN_PATCH}, "child candidate artifact"),
        ({"kind": HumanDecisionKind.REJECT}, "requires a reason"),
        (
            {
                "directive": RepairDirective(
                    directive_id=StableId("directive.exact.edge"),
                    action=RepairAction.HUMAN,
                )
            },
            "exact approval",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            _validate(decision, **updates)


def test_checkpoint_phase_and_resume_invariants() -> None:
    artifacts = InMemoryArtifactRepository()
    data = _ready_data(
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
    )
    request_ref = _model_ref(data.request, data, "request")
    base = MemoryWriteCheckpoint(
        checkpoint_id=StableId("checkpoint.domain.edge"),
        request_identity_hash=ArtifactId("sha256:" + "a" * 64),
        request_artifact_ref=request_ref,
        run_id=data.request.run_id,
        task_id=data.request.task_id,
        project_id=data.request.project_id,
        base_commit=data.request.base_commit,
        source_artifacts=data.request.source_artifacts,
        root_update_intents=data.request.root_update_intents,
        world_mutation=data.request.world_mutation,
        information_boundary=data.request.information_boundary,
        configuration_fingerprint=data.request.configuration_fingerprint,
        workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        state=MemoryWriteState.LOAD_BASIS,
        resume_state=MemoryWriteState.LOAD_BASIS,
        resumability_status=ResumabilityStatus.RESUMABLE,
    )
    cases = (
        (
            {"accepted_commit_id": CommitId("sha256:" + "8" * 64)},
            "cannot carry an accepted commit",
        ),
        (
            {"resume_state": MemoryWriteState.COMMIT},
            "COMMIT resume requires",
        ),
        (
            {"workflow_phase": MemoryWriteWorkflowPhase.CANON_COMMITTED},
            "post-commit checkpoint",
        ),
        (
            {
                "workflow_phase": MemoryWriteWorkflowPhase.PROJECTION_PENDING,
                "accepted_commit_id": CommitId("sha256:" + "8" * 64),
                "commit_receipt_ref": _artifact("7"),
            },
            "requires a projection effect",
        ),
        (
            {
                "workflow_phase": MemoryWriteWorkflowPhase.COMPLETE,
                "resume_state": MemoryWriteState.COMPLETE,
                "accepted_commit_id": CommitId("sha256:" + "8" * 64),
            },
            "requires final receipts",
        ),
        (
            {"resume_state": MemoryWriteState.COMPLETE},
            "resume state is not allowed",
        ),
        (
            {
                "resume_state": MemoryWriteState.CURATE,
                "current_candidate_id": StableId("candidate.edge"),
            },
            "curation resume",
        ),
        (
            {"resume_state": MemoryWriteState.CURATOR_REPAIR},
            "requires its parent",
        ),
        (
            {
                "resume_state": MemoryWriteState.CURATE_ATTEMPT_PREPARE,
                "current_candidate_id": StableId("candidate.edge"),
            },
            "Pre-Candidate checkpoint",
        ),
        (
            {"resume_state": MemoryWriteState.CURATE_ATTEMPT_EXECUTE},
            "requires an inflight attempt",
        ),
        (
            {"resume_state": MemoryWriteState.PROPOSAL_VALIDATE},
            "requires a terminal attempt",
        ),
        (
            {"resume_state": MemoryWriteState.PROPOSAL_REPAIR_POLICY},
            "requires a rejection",
        ),
        (
            {"resume_state": MemoryWriteState.PROPOSAL_RETRY},
            "requires a rejection",
        ),
        (
            {"resume_state": MemoryWriteState.PROPOSAL_HUMAN_RESUME},
            "requires its review request and directive",
        ),
        (
            {
                "resume_state": MemoryWriteState.NORMALIZE,
                "current_candidate_id": StableId("candidate.edge"),
                "world_mutation": CuratorWorldProposalInput(
                    curator_agent_spec=_contract("agent.curator.domain.edge")
                ),
            },
            "requires an accepted proposal attempt",
        ),
    )
    for updates, message in cases:
        with pytest.raises(ValidationError, match=message):
            _validate(base, **updates)

    commit_ready = _validate(
        base,
        resume_state=MemoryWriteState.COMMIT,
        commit_effect_id=StableId("effect.commit.edge"),
        commit_request_ref=_artifact("6"),
        commit_attempt_status=EffectStatus.REQUESTED,
    )
    assert commit_ready.resume_state is MemoryWriteState.COMMIT
