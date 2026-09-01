"""Contract tests for the thin Planner-gap to Memory Write adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.memory_write.teacher_forced import _chapter_index
from novel_agent.adapters.runtime.memory_maintenance import (
    FINDING_MEDIA_TYPE,
    WORKFLOW_RESULT_MEDIA_TYPE,
    MemoryMaintenanceAdapter,
    MemoryMaintenancePolicy,
)
from novel_agent.domain.artifacts import ArtifactRef, RootKind
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
    InformationBoundary,
    MemoryGapClassification,
    MemoryRepairFinding,
    MemoryRepairOwner,
    MemoryWriteBudget,
    MemoryWriteWorkflowPhase,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
    NarrativePosition,
    RepairScope,
    SourceProvenance,
    SourceVisibilityReceipt,
)
from novel_agent.domain.runtime import TaskKind, TaskPurpose, TaskRecord, TaskStatus
from novel_agent.domain.stage2 import AccessScope, ContractRef
from novel_agent.domain.text import SourceBoundEvidenceRequirement, TextSpanRef
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import manifest_commit_id
from novel_agent.services.content_addressing import canonical_json_bytes
from tests.factories import make_manifest

VERSION = SchemaVersion("1.0.0")
PROJECT = ProjectId("project.u8b3.contract")
HASH = ArtifactId("sha256:" + "1" * 64)
PERMISSION_HASH = "sha256:" + "2" * 64


class _Commits:
    def __init__(self, manifest: Any) -> None:
        self.manifest = manifest

    def load_manifest(self, commit_id: CommitId) -> Any:
        assert commit_id == manifest_commit_id(self.manifest)
        return self.manifest


class _Workflow:
    def __init__(self) -> None:
        self.request: Any | None = None

    async def execute(self, request: Any) -> MemoryWriteWorkflowResult:
        self.request = request
        return MemoryWriteWorkflowResult(
            request_id=request.request_id,
            status=MemoryWriteWorkflowStatus.NOOP,
            workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
            canonical_commit_accepted=False,
            base_commit=request.base_commit,
        )


class _RecoveringWorkflow(_Workflow):
    def __init__(self, checkpoint_ref: ArtifactRef) -> None:
        super().__init__()
        self.recovery_checkpoint_ref = checkpoint_ref
        self.recovery_args: dict[str, object] | None = None

    def prepare_validation_retry(
        self, checkpoint_ref: ArtifactRef, **kwargs: object
    ) -> ArtifactRef:
        self.recovery_args = {"checkpoint_ref": checkpoint_ref, **kwargs}
        return self.recovery_checkpoint_ref


def _contract(name: str, fingerprint: ArtifactId = HASH) -> ContractRef:
    return ContractRef(
        contract_id=StableId(name),
        version=VERSION,
        content_hash=fingerprint,
    )


def _task_and_finding(
    artifacts: ArtifactRepository,
) -> tuple[TaskRecord, MemoryRepairFinding, ArtifactRef, CommitId]:
    manifest = make_manifest(PROJECT)
    base = manifest_commit_id(manifest)
    boundary = InformationBoundary(
        boundary_id=StableId("boundary.u8b3.contract"),
        base_commit=base,
        maximum_visible_position=NarrativePosition(chapter_index=4),
        evaluator_sources_forbidden=True,
        policy_ref=_contract("contract.u8b3.boundary"),
    )
    source = artifacts.put(b"visible source", "text/plain", VERSION)
    visibility = SourceVisibilityReceipt(
        receipt_id=StableId("visibility.u8b3.source"),
        source_artifact=source,
        boundary_id=boundary.boundary_id,
        visible_through=NarrativePosition(chapter_index=4),
        access_scope=AccessScope.WRITER_SAFE,
        provenance=SourceProvenance.CANONICAL_ROOT,
        issuer=StableId("issuer.u8b3.contract"),
        receipt_hash=HASH,
    )
    visibility_ref = artifacts.put(
        canonical_json_bytes(visibility.model_dump(mode="json")),
        "application/vnd.novel-agent.source-visibility-receipt+json",
        VERSION,
    )
    finding = MemoryRepairFinding(
        finding_id=StableId("finding.u8b3.contract"),
        incident_id=StableId("incident.u8b3.contract"),
        planner_run_id=RunId("run.u8b3.contract"),
        planner_task_id=TaskId("task.u8b3.planner"),
        planner_attempt_id=StableId("attempt.u8b3.planner"),
        planner_request_id=StableId("request.u8b3.planner"),
        planner_intent_ref=source,
        planner_checkpoint_ref=source,
        project_id=PROJECT,
        base_commit=base,
        basis_snapshot_id=StableId("snapshot.u8b3.base"),
        information_boundary=boundary,
        cutoff=NarrativePosition(chapter_index=4),
        access_scope=AccessScope.WRITER_SAFE,
        source_artifact_refs=(source,),
        source_visibility_receipt_refs=(visibility_ref,),
        source_chapter_indices=(2,),
        source_evidence_requirement=SourceBoundEvidenceRequirement(
            source_artifact_id=source.artifact_id,
            source_chapter_index=2,
            source_chapter_id=StableId("chapter.u8b3.contract.2"),
            required_span=TextSpanRef(
                block_id=StableId("block.u8b3.contract.2.0"),
                start=0,
                end=7,
            ),
            required_consequence_markers=("visible",),
        ),
        need_id=StableId("need.u8b3.contract"),
        need_query="which visible relation is missing?",
        semantic_question="which source supports the relation?",
        mandatory_facet_ids=(StableId("facet.relation"),),
        classification=MemoryGapClassification.CANON_EXTRACTION_GAP,
        repair_owner=MemoryRepairOwner.GRAPH_CURATOR,
        target_root_kind=RootKind.WORLD,
        repair_scope=RepairScope(field_paths=("relations",)),
        no_progress_key=StableId("progress.u8b3.contract"),
    )
    finding_ref = artifacts.put(
        canonical_json_bytes(finding.model_dump(mode="json")),
        FINDING_MEDIA_TYPE,
        VERSION,
    )
    task = TaskRecord(
        task_id=TaskId("task.u8b3.maintenance"),
        run_id=finding.planner_run_id,
        project_id=PROJECT,
        kind=TaskKind.MAINTENANCE,
        purpose=TaskPurpose.DERIVED_MAINTENANCE,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        basis_snapshot=finding.basis_snapshot_id,
        policy_hash=PERMISSION_HASH,
        permission_hash=PERMISSION_HASH,
        input_artifact_refs=(finding_ref,),
    )
    return task, finding, finding_ref, base


def _adapter(
    tmp_path: Path,
    *,
    budget: MemoryWriteBudget | None = None,
) -> tuple[MemoryMaintenanceAdapter, _Workflow, TaskRecord, MemoryRepairFinding, CommitId]:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    task, finding, _, base = _task_and_finding(artifacts)
    workflow = _Workflow()
    manifest = make_manifest(PROJECT)
    policy = MemoryMaintenancePolicy(
        curator_agent_spec=_contract("contract.u8b3.curator"),
        tool_policy_ref=_contract("contract.u8b3.tool"),
        repair_policy_ref=_contract("contract.u8b3.repair"),
        configuration_fingerprint=HASH,
        boundary_policy_ref=_contract("contract.u8b3.boundary"),
        budget=budget or MemoryWriteBudget(),
    )
    adapter = MemoryMaintenanceAdapter(
        workflow=cast(Any, workflow),
        commits=cast(Any, _Commits(manifest)),
        artifacts=artifacts,
        policy=policy,
    )
    return adapter, workflow, task, finding, base


def test_memory_maintenance_maps_finding_to_existing_workflow(tmp_path: Path) -> None:
    adapter, workflow, task, finding, base = _adapter(tmp_path)

    result = asyncio.run(adapter.run(task, finding))

    assert result.status is MemoryWriteWorkflowStatus.NOOP
    assert workflow.request is not None
    request = workflow.request
    assert request.request_id == StableId("memory-write.finding.u8b3.contract")
    assert request.trigger.maintenance_task_id == task.task_id
    assert request.commit_profile.value == "require_canonical_commit"
    assert request.base_commit == base
    assert request.canonical_root_refs.project_id == PROJECT
    assert request.source_artifacts == finding.source_artifact_refs
    assert request.source_provenance == (SourceProvenance.CANONICAL_ROOT,)
    assert request.root_update_intents == ()
    assert request.budget == finding.budget
    assert request.repair_owner is MemoryRepairOwner.GRAPH_CURATOR
    assert request.repair_query == finding.semantic_question
    assert request.source_evidence_requirement == finding.source_evidence_requirement
    assert request.trigger.chapter_indices == finding.source_chapter_indices == (2,)
    assert _chapter_index(request) == finding.source_chapter_indices[0]


def test_memory_maintenance_uses_campaign_policy_budget_override(tmp_path: Path) -> None:
    configured = MemoryWriteBudget(token_budget=128_000)
    adapter, workflow, task, finding, _ = _adapter(tmp_path, budget=configured)

    asyncio.run(adapter.run(task, finding))

    assert workflow.request is not None
    assert workflow.request.budget == configured
    assert workflow.request.budget != finding.budget


def test_memory_maintenance_bounds_long_finding_request_identity(tmp_path: Path) -> None:
    adapter, workflow, task, finding, _ = _adapter(tmp_path)
    long_finding = finding.model_copy(update={"finding_id": StableId("f" * 128)})
    finding_ref = adapter._artifacts.put(
        canonical_json_bytes(long_finding.model_dump(mode="json")),
        FINDING_MEDIA_TYPE,
        VERSION,
    )
    long_task = task.model_copy(update={"input_artifact_refs": (finding_ref,)})

    asyncio.run(adapter.run(long_task, long_finding))

    assert workflow.request is not None
    assert workflow.request.request_id.root == (
        "memory-write.task.u8b3.planner.attempt.u8b3.planner"
    )
    assert len(workflow.request.request_id.root) <= 128


def test_memory_maintenance_preserves_finding_scope_when_planner_task_is_max_length(
    tmp_path: Path,
) -> None:
    adapter, workflow, task, finding, _ = _adapter(tmp_path)
    long_finding = finding.model_copy(
        update={
            "finding_id": StableId("f" * 128),
            "planner_task_id": TaskId("t" * 128),
        }
    )
    finding_ref = adapter._artifacts.put(
        canonical_json_bytes(long_finding.model_dump(mode="json")),
        FINDING_MEDIA_TYPE,
        VERSION,
    )
    long_task = task.model_copy(update={"input_artifact_refs": (finding_ref,)})

    asyncio.run(adapter.run(long_task, long_finding))

    assert workflow.request is not None
    assert workflow.request.request_id == long_finding.finding_id


def test_memory_maintenance_rejects_non_canon_gap_before_workflow(tmp_path: Path) -> None:
    adapter, workflow, task, finding, _ = _adapter(tmp_path)
    finding = finding.model_copy(
        update={
            "classification": MemoryGapClassification.SOURCE_EVIDENCE_ABSENT,
            "repair_owner": MemoryRepairOwner.OPERATOR,
        }
    )

    with pytest.raises(ValueError, match="only accepts CANON_EXTRACTION_GAP"):
        asyncio.run(adapter.run(task, finding))

    assert workflow.request is None


def test_memory_maintenance_reuses_checkpoint_from_resumable_result(tmp_path: Path) -> None:
    adapter, workflow, task, finding, base = _adapter(tmp_path)
    artifacts = adapter._artifacts
    checkpoint_ref = artifacts.put(b"checkpoint", "application/json", VERSION)
    suspended = MemoryWriteWorkflowResult(
        request_id=StableId("request.u8b3.suspended"),
        status=MemoryWriteWorkflowStatus.SUSPENDED,
        workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        canonical_commit_accepted=False,
        base_commit=base,
        checkpoint_ref=checkpoint_ref,
    )
    result_ref = artifacts.put(
        canonical_json_bytes(suspended.model_dump(mode="json")),
        WORKFLOW_RESULT_MEDIA_TYPE,
        VERSION,
    )
    resumed = task.model_copy(update={"terminal_artifact_refs": (result_ref,)})

    asyncio.run(adapter.run(resumed, finding))

    assert workflow.request is not None
    assert workflow.request.resume_checkpoint == checkpoint_ref


def test_memory_maintenance_reopens_bounded_validation_id_fatal_checkpoint(
    tmp_path: Path,
) -> None:
    adapter, _, task, finding, base = _adapter(tmp_path)
    checkpoint_ref = adapter._artifacts.put(b"old checkpoint", "application/json", VERSION)
    retry_ref = adapter._artifacts.put(b"retry checkpoint", "application/json", VERSION)
    recovering = _RecoveringWorkflow(retry_ref)
    adapter = MemoryMaintenanceAdapter(
        workflow=cast(Any, recovering),
        commits=adapter._commits,
        artifacts=adapter._artifacts,
        policy=adapter._policy,
    )
    fatal = MemoryWriteWorkflowResult(
        request_id=StableId("request.u8b3.fatal-validation-id"),
        status=MemoryWriteWorkflowStatus.FATAL,
        workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        canonical_commit_accepted=False,
        base_commit=base,
        checkpoint_ref=checkpoint_ref,
        terminal_codes=(
            "1 validation error for StableId\\n"
            "input_value='validation.bundle.candid...d42d.2.36790667aa08e09b'",
        ),
    )
    result_ref = adapter._artifacts.put(
        canonical_json_bytes(fatal.model_dump(mode="json")),
        WORKFLOW_RESULT_MEDIA_TYPE,
        VERSION,
    )
    resumed = task.model_copy(update={"terminal_artifact_refs": (result_ref,)})

    asyncio.run(adapter.run(resumed, finding))

    assert recovering.recovery_args is not None
    assert recovering.recovery_args["checkpoint_ref"] == checkpoint_ref
    assert recovering.recovery_args["task_id"] == task.task_id
    assert recovering.recovery_args["run_id"] == task.run_id
    assert recovering.recovery_args["base_commit"] == base
    assert recovering.request is not None
    assert recovering.request.resume_checkpoint == retry_ref
