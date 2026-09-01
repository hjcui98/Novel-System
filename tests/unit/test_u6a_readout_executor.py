"""U6-A lifecycle executor behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, SchemaVersion, StableId
from novel_agent.domain.u6_continuous_replay import (
    U6ACanaryJob,
    U6AReadoutPhaseResult,
    U6AReadoutPlan,
    U6AReadoutTask,
    U6AReadoutTrack,
    U6BasisKind,
    U6BasisStatus,
    U6CheckpointBasis,
    U6CheckpointBasisManifest,
    U6CheckpointLineage,
    U6ContinuousReplayReport,
)
from novel_agent.domain.v05_readout import MemoryIdentitySnapshot
from novel_agent.services.artifacts import ArtifactRepository, sha256_id
from novel_agent.services.u6a_readout_executor import U6AReadoutExecutor

SCHEMA = SchemaVersion("1.0.0")
RUN_ID = RunId("run.u6a.test")
MANIFEST_MEDIA = "application/vnd.novel-agent.evaluation.u6-checkpoint-basis-manifest+json"
PLAN_MEDIA = "application/vnd.novel-agent.evaluation.u6a-readout-plan+json"
EVAL_MEDIA = "application/vnd.novel-agent.evaluation.test-ref+json"


def _artifact(label: str, media_type: str = "application/json") -> ArtifactRef:
    data = label.encode("utf-8")
    return ArtifactRef(
        artifact_id=sha256_id(data),
        media_type=media_type,
        byte_length=len(data),
        schema_version=SCHEMA,
    )


def _typed_ref(kind: type[ArtifactRef], label: str) -> ArtifactRef:
    ref = _artifact(label, f"application/vnd.novel-agent.{label}+json")
    return kind.model_validate(ref.model_dump())


def _fixture() -> tuple[
    U6AReadoutPlan,
    ArtifactRef,
    U6CheckpointBasisManifest,
    U6ContinuousReplayReport,
]:
    nodes: list[U6CheckpointBasis] = []
    lineages: list[U6CheckpointLineage] = []
    for chapter in (1, 2):
        commit = CommitId(f"sha256:{str(chapter) * 64}")
        text = _typed_ref(TextRootRef, f"text-root-{chapter}")
        plan = _typed_ref(PlanRootRef, f"plan-root-{chapter}")
        world = _typed_ref(WorldRootRef, f"world-root-{chapter}")
        profile = _typed_ref(ProjectProfileRootRef, f"profile-root-{chapter}")
        basis_id = StableId(f"basis.{chapter}")
        nodes.append(
            U6CheckpointBasis(
                basis_id=basis_id,
                checkpoint_chapter=chapter,
                kind=U6BasisKind.PUBLIC_DECLARED,
                status=U6BasisStatus.FROZEN,
                commit_id=commit,
                snapshot_id=StableId(f"snapshot.{chapter}"),
                plan_root_ref=plan,
                text_root_ref=text,
                world_root_ref=world,
                profile_root_ref=profile,
            )
        )
        lineages.append(
            U6CheckpointLineage(
                basis_id=basis_id,
                checkpoint_chapter=chapter,
                commit_id=commit,
                snapshot_id=StableId(f"snapshot.{chapter}"),
                plan_root_ref=plan,
                text_root_ref=text,
                world_root_ref=world,
                profile_root_ref=profile,
                index_lineage_ref=_artifact(f"index-{chapter}"),
                memory_identity_before=ArtifactId(f"sha256:{str(chapter + 4) * 64}"),
                memory_identity_after=ArtifactId(f"sha256:{str(chapter + 4) * 64}"),
                control_replay_identity=ArtifactId(f"sha256:{str(chapter + 2) * 64}"),
                evaluation_namespace="PENDING_READOUT",
                identity_match=True,
            )
        )
    basis_ref = _artifact("basis-manifest", MANIFEST_MEDIA)
    manifest = U6CheckpointBasisManifest(
        benchmark_id="test-benchmark",
        version="0.1",
        frozen_build_id="test-build",
        status=U6BasisStatus.FROZEN,
        replay_scope="0..300 sequential_once",
        status_note="test basis",
        basis_nodes=tuple(nodes),
    )
    report = U6ContinuousReplayReport(
        campaign_id=StableId("campaign.u6a.test"),
        run_id=RUN_ID,
        project_id=ProjectId("project.u6a.test"),
        benchmark_id="test-benchmark",
        benchmark_version="0.1",
        basis_manifest_ref=basis_ref,
        chapters_declared=2,
        chapters_ingested=2,
        ingest_passes=1,
        public_basis_count=2,
        internal_basis_count=0,
        basis_count=2,
        canary_job_count=1,
        expected_readout_task_count=1,
        completed_readout_task_count=0,
        evaluation_discard_count=0,
        future_leakage_count=0,
        duplicate_checkpoint_declarations=0,
        control_replay_identity=ArtifactId(f"sha256:{'a' * 64}"),
        lineage=tuple(lineages),
        status="BASIS_FROZEN",
    )
    task = U6AReadoutTask(
        task_id=StableId("task.qa.1"),
        track="novelmem_qa",
        checkpoint_chapter=1,
        basis_id=StableId("basis.1"),
        source_task_id=StableId("source.qa.1"),
    )
    job = U6ACanaryJob(
        job_id=StableId("dshort-2"),
        track=U6AReadoutTrack.D_SHORT,
        checkpoint_chapter=2,
        basis_id=StableId("basis.2"),
    )
    plan_ref = _artifact("readout-plan", PLAN_MEDIA)
    plan = U6AReadoutPlan(
        campaign_id=StableId("campaign.u6a.test"),
        basis_manifest_ref=basis_ref,
        source_readout_manifest_ref=_artifact("source-manifest"),
        tasks=(task,),
        canary_jobs=(job,),
        qa_task_count=1,
        context_task_count=0,
        canary_job_count=1,
        status="READY",
    )
    return plan, plan_ref, manifest, report


def _memory(basis: U6CheckpointBasis) -> MemoryIdentitySnapshot:
    assert basis.commit_id is not None
    assert basis.text_root_ref is not None
    assert basis.world_root_ref is not None
    assert basis.plan_root_ref is not None
    assert basis.profile_root_ref is not None
    return MemoryIdentitySnapshot(
        commit_id=basis.commit_id,
        text_root=basis.text_root_ref.artifact_id,
        world_root=basis.world_root_ref.artifact_id,
        plan_root=basis.plan_root_ref.artifact_id,
        profile_root=basis.profile_root_ref.artifact_id,
    )


class _Adapter:
    def __init__(
        self,
        *,
        changed_phase: str | None = None,
        future_phase: str | None = None,
        error_phase: str | None = None,
    ) -> None:
        self.calls: list[tuple[int, str, str]] = []
        self.changed_phase = changed_phase
        self.future_phase = future_phase
        self.error_phase = error_phase

    def execute_phase(self, *, phase, item, basis, run_id):
        item_name = item.task_id.root if isinstance(item, U6AReadoutTask) else item.job_id.root
        self.calls.append((basis.checkpoint_chapter, phase, item_name))
        if phase == self.error_phase:
            raise RuntimeError("diagnostic boundary failure")
        identity = _memory(basis)
        if phase == self.changed_phase:
            identity = identity.model_copy(update={"commit_id": CommitId(f"sha256:{'f' * 64}")})
        ref = _artifact(f"{basis.checkpoint_chapter}-{item_name}-{phase}", EVAL_MEDIA)
        return U6AReadoutPhaseResult(
            phase=phase,
            artifact_refs=(ref,),
            evaluation_refs=(ref,),
            memory_identity=identity,
            future_leakage_count=1 if phase == self.future_phase else 0,
        )


def _run(tmp_path: Path, adapter: _Adapter):
    plan, plan_ref, manifest, basis_report = _fixture()
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    return asyncio.run(
        U6AReadoutExecutor(
            plan=plan,
            plan_ref=plan_ref,
            basis_manifest=manifest,
            basis_report=basis_report,
            adapter=adapter,
            artifacts=artifacts,
            run_id=RUN_ID,
        ).run()
    )


def test_u6a_runs_each_checkpoint_then_discards_before_next_checkpoint(tmp_path: Path) -> None:
    adapter = _Adapter()
    execution = _run(tmp_path, adapter)

    assert execution.report.status == "COMPLETED"
    assert execution.report.completed_item_count == 2
    assert execution.report.completed_checkpoint_count == 2
    assert execution.report.evaluation_discard_count == 2
    assert [chapter for chapter, _, _ in adapter.calls] == [
        1,
        1,
        1,
        1,
        1,
        1,
        2,
        2,
        2,
        2,
        2,
        2,
    ]
    assert all(item.discard_receipt_ref is not None for item in execution.report.items)


def test_u6a_memory_identity_change_stops_before_next_checkpoint(tmp_path: Path) -> None:
    adapter = _Adapter(changed_phase="writer")
    execution = _run(tmp_path, adapter)

    assert execution.report.status == "REVIEW_REQUIRED"
    assert execution.report.first_failure_phase == "writer"
    assert execution.report.completed_item_count == 0
    assert all(chapter == 1 for chapter, _, _ in adapter.calls)
    assert execution.report.evaluation_discard_count == 0


def test_u6a_future_leakage_is_a_typed_stop(tmp_path: Path) -> None:
    adapter = _Adapter(future_phase="writer")
    execution = _run(tmp_path, adapter)

    assert execution.report.status == "REVIEW_REQUIRED"
    assert execution.report.first_failure_phase == "writer"
    assert execution.report.completed_checkpoint_count == 0


def test_u6a_review_report_retains_boundary_failure_detail(tmp_path: Path) -> None:
    execution = _run(tmp_path, _Adapter(error_phase="writer"))

    assert execution.report.status == "REVIEW_REQUIRED"
    assert execution.report.first_failure_type == "U6AReadoutExecutionError"
    assert execution.report.first_failure_detail == (
        "U6-A writer adapter failed: RuntimeError: diagnostic boundary failure"
    )
