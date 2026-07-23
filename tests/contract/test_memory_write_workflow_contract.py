"""Frozen Stage 2W workflow contracts and failure semantics."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import get_type_hints

import pytest

from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootKind,
    RootManifest,
    TextRootRef,
    WorldRootRef,
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
from novel_agent.domain.memory_write import (
    CandidateProducerKind,
    CandidateRevision,
    CanonicalWriteBasis,
    ChapterRevealTrigger,
    InformationBoundary,
    MaintenanceTrigger,
    MemoryWriteCommitProfile,
    MemoryWriteWorkflowRequest,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
    NarrativePosition,
    NoWorldMutationInput,
    RootUpdateIntent,
    RootUpdateKind,
    ValidationDecision,
    ValidationDisposition,
)
from novel_agent.domain.stage2 import (
    AccessScope,
    ContractRef,
    PatchRiskAssessment,
    PatchRiskLevel,
)
from novel_agent.ports.memory_write import (
    MemoryWriteWorkflowPort,
)
from novel_agent.services.memory_write_workflow import (
    InMemoryArtifactRepository,
    LocalMemoryWriteWorkflow,
    _WorkflowData,
)

VERSION = SchemaVersion("0.1.0")
PROJECT = ProjectId("project.stage2w.contract")
BASE = CommitId("sha256:" + "1" * 64)


def _id(digit: str) -> ArtifactId:
    return ArtifactId("sha256:" + digit * 64)


def _artifact(digit: str, media_type: str = "application/json") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=_id(digit),
        media_type=media_type,
        byte_length=1,
        schema_version=VERSION,
    )


def _manifest(
    *,
    text: str = "2",
    world: str = "4",
    parent: tuple[CommitId, ...] = (),
) -> RootManifest:
    return RootManifest(
        project_id=PROJECT,
        schema_version=VERSION,
        text_root=TextRootRef(**_artifact(text).model_dump()),
        plan_root=PlanRootRef(**_artifact("3").model_dump()),
        world_root=WorldRootRef(**_artifact(world).model_dump()),
        reference_root=ReferenceRootRef(**_artifact("5").model_dump()),
        project_profile_root=ProjectProfileRootRef(**_artifact("6").model_dump()),
        parent_commit_ids=parent,
    )


def _contract(name: str, digit: str = "7") -> ContractRef:
    return ContractRef(
        contract_id=StableId(name),
        version=VERSION,
        content_hash=_id(digit),
    )


def _request(
    *,
    profile: MemoryWriteCommitProfile = MemoryWriteCommitProfile.CHANGED_ROOTS_ONLY,
    chapter_text_changed: bool = False,
) -> MemoryWriteWorkflowRequest:
    boundary = InformationBoundary(
        boundary_id=StableId("boundary.stage2w.contract"),
        base_commit=BASE,
        maximum_visible_position=NarrativePosition(chapter_index=1),
        evaluator_sources_forbidden=True,
        policy_ref=_contract("policy.boundary"),
    )
    intents: tuple[RootUpdateIntent, ...] = ()
    trigger: ChapterRevealTrigger | MaintenanceTrigger = MaintenanceTrigger(
        maintenance_task_id=StableId("maintenance.stage2w.contract")
    )
    if profile is MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC:
        trigger = ChapterRevealTrigger(
            chapter_id=StableId("chapter.1"),
            chapter_index=1,
            reveal_position=NarrativePosition(chapter_index=1),
        )
        base_text = _manifest().text_root
        update = TextRootRef(**_artifact("8").model_dump()) if chapter_text_changed else base_text
        intents = (
            RootUpdateIntent(
                intent_id=StableId("intent.text"),
                root_kind=RootKind.TEXT,
                update_kind=(
                    RootUpdateKind.REPLACE if chapter_text_changed else RootUpdateKind.NOOP
                ),
                expected_base_root=base_text,
                update_artifact=update,
                producer_receipt=_artifact(
                    "9",
                    "application/vnd.novel-agent.boundary-propagation-receipt+json",
                ),
                builder_policy_ref=_contract("policy.text"),
            ),
        )
    return MemoryWriteWorkflowRequest(
        request_id=StableId("request.stage2w.contract"),
        run_id=RunId("run.stage2w.contract"),
        task_id=TaskId("task.stage2w.contract"),
        project_id=PROJECT,
        trigger=trigger,
        commit_profile=profile,
        base_commit=BASE,
        root_update_intents=intents,
        world_mutation=NoWorldMutationInput(),
        canonical_root_refs=_manifest(),
        information_boundary=boundary,
        access_scope=AccessScope.WRITER_SAFE,
        configuration_fingerprint=_id("a"),
        tool_policy_ref=_contract("policy.tool"),
        repair_policy_ref=_contract("policy.repair"),
        idempotency_key=StableId("idempotency.stage2w.contract"),
    )


class _Canonical:
    def load_verified(self, project_id: ProjectId, commit_id: CommitId) -> CanonicalWriteBasis:
        assert project_id == PROJECT
        assert commit_id == BASE
        return CanonicalWriteBasis(
            project_id=PROJECT,
            commit_id=BASE,
            root_manifest=_manifest(),
        )

    def current_commit(self, project_id: ProjectId) -> CommitId:
        assert project_id == PROJECT
        return BASE


class _BoundarySpy:
    def __init__(self) -> None:
        self.calls = 0

    def verify_request_and_derivation_graph(self, *_: object) -> None:
        self.calls += 1

    def verify_derivation_chain(self, **_: object) -> None:
        self.calls += 1


def _workflow(*, boundary: object | None = None) -> LocalMemoryWriteWorkflow:
    return LocalMemoryWriteWorkflow(
        canonical_read=_Canonical(),
        commit=object(),
        information_boundary=boundary,
    )


def _candidate(receipt: ArtifactRef) -> CandidateRevision:
    return CandidateRevision(
        candidate_id=StableId("candidate.request.stage2w.contract.1"),
        revision_no=1,
        base_commit=BASE,
        basis_hash=_id("b"),
        candidate_artifact=_artifact("c"),
        producer_kind=CandidateProducerKind.CURATOR_PROPOSE,
        producer_receipt=receipt,
        content_hash=_id("d"),
        created_at=datetime.now(UTC),
    )


def _precommit_data(
    request: MemoryWriteWorkflowRequest,
    artifacts: InMemoryArtifactRepository,
    proposed_roots: RootManifest,
) -> _WorkflowData:
    candidate = _candidate(_artifact("e"))
    validation = ValidationDecision(
        decision_id=StableId("validation.stage2w.contract"),
        candidate_id=candidate.candidate_id,
        candidate_content_hash=candidate.content_hash,
        materialization_receipt=_artifact("9"),
        proposed_roots_hash=_id("8"),
        base_commit=BASE,
        disposition=ValidationDisposition.PASS,
        deterministic_profile="stage2w-contract",
        validated_at=datetime.now(UTC),
    )
    risk = PatchRiskAssessment(
        assessment_id=StableId("risk.stage2w.contract"),
        change_set_id=candidate.candidate_id,
        base_commit=BASE,
        level=PatchRiskLevel.LOW,
        risk_codes=(),
        requires_guardian=False,
        requires_human_review=False,
    )
    return _WorkflowData(
        request=request,
        artifacts=artifacts,
        candidate=candidate,
        materialization=SimpleNamespace(),
        bundle=SimpleNamespace(proposed_roots=proposed_roots),
        validation=validation,
        risk=risk,
    )


def test_stable_workflow_port_freezes_the_typed_result() -> None:
    hints = get_type_hints(MemoryWriteWorkflowPort.execute)
    assert hints["return"] is MemoryWriteWorkflowResult


def test_curator_candidate_with_propagation_mime_still_verifies_the_dag() -> None:
    boundary = _BoundarySpy()
    workflow = _workflow(boundary=boundary)
    data = _WorkflowData(request=_request())
    receipt = _artifact("e", "application/vnd.novel-agent.boundary-propagation-receipt+json")

    workflow._persist_candidate(data, _candidate(receipt))

    assert boundary.calls == 1


def test_chapter_reveal_rejects_identical_text_even_when_world_changes() -> None:
    artifacts = InMemoryArtifactRepository()
    workflow = LocalMemoryWriteWorkflow(
        canonical_read=_Canonical(),
        commit=object(),
        information_boundary=_BoundarySpy(),
        artifacts=artifacts,
    )
    request = _request(
        profile=MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC,
        chapter_text_changed=False,
    )
    data = _precommit_data(request, artifacts, _manifest(world="f"))

    result = workflow._prepare_commit(data)

    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert result.canonical_commit_accepted is False
    assert "CHAPTER_TEXT_UPDATE_REQUIRED" in result.terminal_codes
    assert data.commit_request is None


@pytest.mark.parametrize(
    ("profile", "expected_status", "expected_code"),
    (
        (MemoryWriteCommitProfile.CHANGED_ROOTS_ONLY, None, None),
        (
            MemoryWriteCommitProfile.REQUIRE_CANONICAL_COMMIT,
            MemoryWriteWorkflowStatus.FATAL,
            "REQUIRED_ROOT_UPDATE_MISSING",
        ),
    ),
)
def test_identical_root_profiles_never_create_a_commit(
    profile: MemoryWriteCommitProfile,
    expected_status: MemoryWriteWorkflowStatus | None,
    expected_code: str | None,
) -> None:
    artifacts = InMemoryArtifactRepository()
    workflow = LocalMemoryWriteWorkflow(
        canonical_read=_Canonical(),
        commit=object(),
        information_boundary=_BoundarySpy(),
        artifacts=artifacts,
    )
    data = _precommit_data(_request(profile=profile), artifacts, _manifest())

    result = workflow._prepare_commit(data)

    assert data.commit_request is None
    if expected_status is None:
        assert result is None
        assert data.state.value == "complete"
    else:
        assert result is not None
        assert result.status is expected_status
        assert expected_code in result.terminal_codes
