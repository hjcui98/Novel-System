"""Durable Stage 2W commit/projection recovery and exact replay tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import MethodType

import pytest

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.changes import CandidateChangeBundle, ObservedChangeSet
from novel_agent.domain.ids import ArtifactId, RunId, StableId
from novel_agent.domain.memory_write import (
    CandidateMaterialization,
    CanonicalWriteBasis,
    MemoryWriteCheckpoint,
    MemoryWriteState,
    MemoryWriteWorkflowPhase,
    MemoryWriteWorkflowStatus,
    ValidationDecision,
    ValidationDisposition,
)
from novel_agent.domain.runtime import EffectStatus
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    ContractRef,
    GuardianDecision,
    GuardianOutcome,
    PatchRiskAssessment,
    PatchRiskLevel,
)
from novel_agent.ports.memory_write import MemoryWriteCommitStatus
from novel_agent.services.memory_write_workflow import (
    ImmediateProjectionReadinessPort,
    InMemoryArtifactRepository,
    InMemoryCandidateLineageRepository,
    InMemoryCheckpointRepository,
    InMemoryCommitPort,
    LocalMemoryWriteWorkflow,
    _WorkflowData,
)
from tests.contract.test_memory_write_workflow_contract import (
    BASE,
    PROJECT,
    _artifact,
    _BoundarySpy,
    _candidate,
    _manifest,
    _request,
)
from tests.contract.test_stage2_contract import agent_receipt

NOW = datetime(2026, 7, 23, tzinfo=UTC)


class _Canonical:
    def __init__(self, commit: InMemoryCommitPort) -> None:
        self.commit = commit

    def current_commit(self, _: object) -> object:
        return self.commit.current

    def load_verified(self, _: object, commit_id: object) -> object:
        assert commit_id == BASE
        return CanonicalWriteBasis(
            project_id=PROJECT,
            commit_id=BASE,
            root_manifest=_manifest(),
        )


class _FailAcceptedCheckpointOnce(InMemoryCheckpointRepository):
    def __init__(self, artifacts: InMemoryArtifactRepository) -> None:
        super().__init__(artifacts)
        self.fail = True

    def save(self, checkpoint: MemoryWriteCheckpoint) -> ArtifactRef:
        phase = checkpoint.workflow_phase
        if self.fail and phase is MemoryWriteWorkflowPhase.CANON_COMMITTED:
            self.fail = False
            raise OSError("accepted checkpoint fsync failed")
        return super().save(checkpoint)


class _FailPhaseCheckpoint(InMemoryCheckpointRepository):
    def __init__(
        self,
        artifacts: InMemoryArtifactRepository,
        phase: MemoryWriteWorkflowPhase,
    ) -> None:
        super().__init__(artifacts)
        self.phase = phase

    def save(self, checkpoint: MemoryWriteCheckpoint) -> ArtifactRef:
        if checkpoint.workflow_phase is self.phase:
            raise OSError(f"{self.phase.value} checkpoint failed")
        return super().save(checkpoint)


def _ready_data(
    *,
    artifacts: InMemoryArtifactRepository,
    lineage: InMemoryCandidateLineageRepository,
) -> _WorkflowData:
    request = _request()
    candidate = _candidate(_artifact("e"))
    lineage.persist(candidate)
    proposed = _manifest(world="f", parent=(BASE,))
    observed = ObservedChangeSet(
        change_set_id=StableId("changes.resume"),
        base_commit=BASE,
        source_artifact=_artifact("9"),
    )
    bundle = CandidateChangeBundle(
        bundle_id=StableId(f"bundle.{candidate.candidate_id.root}"),
        project_id=PROJECT,
        run_id=RunId("run.resume"),
        base_commit=BASE,
        observed_changes=observed,
        proposed_roots=proposed,
    )
    materialization = CandidateMaterialization(
        candidate_id=candidate.candidate_id,
        candidate_content_hash=candidate.content_hash,
        bundle_artifact=_artifact("8"),
        proposed_roots_hash=ArtifactId("sha256:" + "7" * 64),
        materialization_receipt=_artifact("6"),
        materializer_policy_ref=ContractRef(
            contract_id=StableId("policy.resume.materializer"),
            version=proposed.schema_version,
            content_hash=ArtifactId("sha256:" + "5" * 64),
        ),
        bundle=bundle,
    )
    validation = ValidationDecision(
        decision_id=StableId("validation.resume"),
        candidate_id=candidate.candidate_id,
        candidate_content_hash=candidate.content_hash,
        materialization_receipt=materialization.materialization_receipt,
        proposed_roots_hash=materialization.proposed_roots_hash,
        base_commit=BASE,
        disposition=ValidationDisposition.PASS,
        deterministic_profile="resume-test",
        validated_at=NOW,
    )
    risk = PatchRiskAssessment(
        assessment_id=StableId("risk.resume"),
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
        materialization=materialization,
        bundle=bundle,
        validation=validation,
        risk=risk,
        started_at=NOW,
    )


def _workflow(
    *,
    artifacts: InMemoryArtifactRepository,
    lineage: InMemoryCandidateLineageRepository,
    checkpoint: InMemoryCheckpointRepository,
    commit: InMemoryCommitPort,
    projection: ImmediateProjectionReadinessPort,
) -> LocalMemoryWriteWorkflow:
    return LocalMemoryWriteWorkflow(
        canonical_read=_Canonical(commit),  # type: ignore[arg-type]
        commit=commit,
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        projection=projection,
        information_boundary=_BoundarySpy(),
    )


def test_candidate_persisted_checkpoint_reconstructs_in_a_new_process() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = InMemoryCheckpointRepository(artifacts)
    commit = InMemoryCommitPort(current_commit=BASE)
    first = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        commit=commit,
        projection=ImmediateProjectionReadinessPort(artifacts=artifacts),
    )
    data = _ready_data(artifacts=artifacts, lineage=lineage)
    data.materialization = None
    data.bundle = None
    data.validation = None
    data.risk = None
    candidate_checkpoint = first._save_checkpoint(
        data,
        phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        resume_state=MemoryWriteState.MATERIALIZE,
    )

    restarted = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        commit=commit,
        projection=ImmediateProjectionReadinessPort(artifacts=artifacts),
    )
    restored = asyncio.run(
        restarted._initialize(
            data.request.model_copy(update={"resume_checkpoint": candidate_checkpoint})
        )
    )

    assert isinstance(restored, _WorkflowData)
    assert restored.state is MemoryWriteState.MATERIALIZE
    assert restored.candidate == data.candidate
    assert restored.basis is not None


def test_guardian_decision_checkpoint_reconstructs_in_a_new_process() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = InMemoryCheckpointRepository(artifacts)
    commit = InMemoryCommitPort(current_commit=BASE)
    first = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        commit=commit,
        projection=ImmediateProjectionReadinessPort(artifacts=artifacts),
    )
    data = _ready_data(artifacts=artifacts, lineage=lineage)
    assert data.candidate is not None
    receipt = agent_receipt().model_copy(
        update={
            "agent_type": AgentType.MEMORY_GUARDIAN,
            "agent_mode": AgentMode.RISK_REVIEW,
            "base_commit": BASE,
        }
    )
    data.guardian = GuardianDecision(
        decision_id=StableId("guardian.resume"),
        proposal_id=data.candidate.candidate_id,
        base_commit=BASE,
        outcome=GuardianOutcome.APPROVE,
        risk_codes=("CHECKED",),
        reasons=("safe",),
        receipt=receipt,
    )
    guardian_checkpoint = first._save_checkpoint(
        data,
        phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        resume_state=MemoryWriteState.PRECOMMIT,
    )

    restarted = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        commit=commit,
        projection=ImmediateProjectionReadinessPort(artifacts=artifacts),
    )
    restored = asyncio.run(
        restarted._initialize(
            data.request.model_copy(update={"resume_checkpoint": guardian_checkpoint})
        )
    )

    assert isinstance(restored, _WorkflowData)
    assert restored.state is MemoryWriteState.PRECOMMIT
    assert restored.guardian == data.guardian
    assert restored.materialization == data.materialization
    assert restored.validation == data.validation
    assert restored.risk == data.risk


def test_commit_accepted_before_checkpoint_uses_exact_replay_after_restart() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = _FailAcceptedCheckpointOnce(artifacts)
    commit = InMemoryCommitPort(current_commit=BASE)
    projection = ImmediateProjectionReadinessPort(artifacts=artifacts)
    first = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        commit=commit,
        projection=projection,
    )
    data = _ready_data(artifacts=artifacts, lineage=lineage)
    assert first._prepare_commit(data) is None
    precommit_checkpoint = data.checkpoint_ref
    assert precommit_checkpoint is not None
    assert data.state is MemoryWriteState.COMMIT

    with pytest.raises(OSError, match="fsync"):
        first._commit(data)

    assert data.commit_result is not None
    assert data.commit_result.status == MemoryWriteCommitStatus.ACCEPTED
    accepted_commit = data.commit_result.commit_id
    assert commit.calls == 1

    restarted = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        commit=commit,
        projection=projection,
    )
    resumed_request = data.request.model_copy(update={"resume_checkpoint": precommit_checkpoint})
    result = asyncio.run(restarted.execute(resumed_request))

    assert result.status is MemoryWriteWorkflowStatus.COMMITTED
    assert result.resulting_commit == accepted_commit
    assert result.canonical_commit_accepted is True
    assert commit.calls == 1


def test_projection_pending_checkpoint_resumes_without_recommitting() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = InMemoryCheckpointRepository(artifacts)
    commit = InMemoryCommitPort(current_commit=BASE)
    pending = ImmediateProjectionReadinessPort(pending=True, artifacts=artifacts)
    first = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        commit=commit,
        projection=pending,
    )
    data = _ready_data(artifacts=artifacts, lineage=lineage)
    assert first._prepare_commit(data) is None
    assert first._commit(data) is None
    suspended = first._project(data)
    assert suspended is not None
    assert suspended.status is MemoryWriteWorkflowStatus.SUSPENDED
    assert suspended.workflow_phase is MemoryWriteWorkflowPhase.PROJECTION_PENDING
    projection_checkpoint = suspended.checkpoint_ref
    assert projection_checkpoint is not None
    assert commit.calls == 1

    ready = ImmediateProjectionReadinessPort(pending=False, artifacts=artifacts)
    restarted = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        commit=commit,
        projection=ready,
    )
    resumed_request = data.request.model_copy(update={"resume_checkpoint": projection_checkpoint})
    result = asyncio.run(restarted.execute(resumed_request))

    assert result.status is MemoryWriteWorkflowStatus.COMMITTED
    assert result.canonical_commit_accepted is True
    assert result.workflow_phase is MemoryWriteWorkflowPhase.COMPLETE
    assert commit.calls == 1
    assert len(ready.calls) == 1


def test_uncertain_commit_effect_checkpoint_replays_same_effect() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = InMemoryCheckpointRepository(artifacts)
    commit = InMemoryCommitPort(current_commit=BASE)
    workflow = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        commit=commit,
        projection=ImmediateProjectionReadinessPort(artifacts=artifacts),
    )
    data = _ready_data(artifacts=artifacts, lineage=lineage)
    assert workflow._prepare_commit(data) is None
    uncertain = workflow._save_checkpoint(
        data,
        phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        resume_state=MemoryWriteState.COMMIT,
        commit_attempt_status=EffectStatus.UNCERTAIN,
    )
    loaded = checkpoint.load(uncertain)
    assert loaded.commit_effect_id == data.commit_effect_id
    assert loaded.commit_attempt_status is EffectStatus.UNCERTAIN


def test_precommit_checkpoint_failure_never_calls_commit() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = _FailPhaseCheckpoint(artifacts, MemoryWriteWorkflowPhase.PRECOMMIT)
    commit = InMemoryCommitPort(current_commit=BASE)
    workflow = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        commit=commit,
        projection=ImmediateProjectionReadinessPort(artifacts=artifacts),
    )
    data = _ready_data(artifacts=artifacts, lineage=lineage)
    data.state = MemoryWriteState.PRECOMMIT

    async def initialize(self: LocalMemoryWriteWorkflow, _: object) -> _WorkflowData:
        return data

    workflow._initialize = MethodType(initialize, workflow)  # type: ignore[method-assign]
    result = asyncio.run(workflow.execute(data.request))

    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert result.canonical_commit_accepted is False
    assert commit.calls == 0


def test_complete_checkpoint_failure_reports_canon_and_can_resume_from_canon() -> None:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    commit = InMemoryCommitPort(current_commit=BASE)
    checkpoint = _FailPhaseCheckpoint(artifacts, MemoryWriteWorkflowPhase.COMPLETE)
    projection = ImmediateProjectionReadinessPort(artifacts=artifacts)
    first = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        commit=commit,
        projection=projection,
    )
    data = _ready_data(artifacts=artifacts, lineage=lineage)
    assert first._prepare_commit(data) is None
    assert first._commit(data) is None
    canon_checkpoint = data.checkpoint_ref
    assert canon_checkpoint is not None
    data.state = MemoryWriteState.PROJECT

    async def initialize(self: LocalMemoryWriteWorkflow, _: object) -> _WorkflowData:
        return data

    first._initialize = MethodType(initialize, first)  # type: ignore[method-assign]
    failed = asyncio.run(first.execute(data.request))
    assert failed.status is MemoryWriteWorkflowStatus.FATAL
    assert failed.canonical_commit_accepted is True
    assert commit.calls == 1

    healthy_checkpoint = InMemoryCheckpointRepository(artifacts)
    # Copy the last durable CANON_COMMITTED checkpoint into the healthy adapter.
    canon_model = checkpoint.load(canon_checkpoint)
    healthy_ref = healthy_checkpoint.save(canon_model)
    restarted = _workflow(
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=healthy_checkpoint,
        commit=commit,
        projection=projection,
    )
    resumed = asyncio.run(
        restarted.execute(data.request.model_copy(update={"resume_checkpoint": healthy_ref}))
    )
    assert resumed.status is MemoryWriteWorkflowStatus.COMMITTED
    assert resumed.canonical_commit_accepted is True
    assert commit.calls == 1
