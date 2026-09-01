"""U6-A production adapter public-input and phase contract tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.artifacts import (
    PlanRootRef,
    ProjectProfileRootRef,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.benchmark import PlanRootDocument
from novel_agent.domain.ids import CommitId, ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.u6_continuous_replay import (
    U6ACanaryJob,
    U6AReadoutPhaseResult,
    U6AReadoutTrack,
    U6BasisKind,
    U6BasisStatus,
    U6CheckpointBasis,
)
from novel_agent.domain.v05_readout import V05ReadoutCampaignManifest
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.model_gateway import ModelGateway
from novel_agent.services.u4s_seed_readout import U4SPublicCorpus, _empty_world
from novel_agent.services.u6a_production_readout import (
    SCHEMA_VERSION,
    U6AProductionReadoutAdapter,
    U6AProductionReadoutConfig,
    _memory_identity,
)

BUNDLE = Path("benchmarks/private/ztj_novelmem_v0.5")
RUN_ID = RunId("run.u6a.adapter.test")


def _typed_ref(ref_type: Any, ref: Any) -> Any:
    return ref_type.model_validate(ref.model_dump())


def _basis(tmp_path: Path) -> tuple[ArtifactRepository, U6CheckpointBasis]:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "basis-objects"))
    commit = CommitId("sha256:" + "a" * 64)
    corpus = U4SPublicCorpus(BUNDLE)
    text = corpus.text_root(100)
    world = _empty_world(commit)
    plan = PlanRootDocument(
        root_hash=content_id({"plan": "u6a-test"}),
        schema_version=SCHEMA_VERSION,
    )
    profile_payload = canonical_json_bytes({"profile": "test"})
    text_ref = _typed_ref(
        TextRootRef,
        repository.put(
            text.model_dump_json().encode("utf-8"),
            "application/vnd.novel-agent.text-root+json",
            SchemaVersion("1.0.0"),
        ),
    )
    world_ref = _typed_ref(
        WorldRootRef,
        repository.put(
            world.model_dump_json().encode("utf-8"),
            "application/vnd.novel-agent.world-root+json",
            SchemaVersion("1.0.0"),
        ),
    )
    plan_ref = _typed_ref(
        PlanRootRef,
        repository.put(
            plan.model_dump_json().encode("utf-8"),
            "application/vnd.novel-agent.plan-root+json",
            SchemaVersion("1.0.0"),
        ),
    )
    profile_ref = _typed_ref(
        ProjectProfileRootRef,
        repository.put(
            profile_payload,
            "application/vnd.novel-agent.project-profile-root+json",
            SchemaVersion("1.0.0"),
        ),
    )
    return repository, U6CheckpointBasis(
        basis_id=StableId("basis.100"),
        checkpoint_chapter=100,
        kind=U6BasisKind.INTERNAL_N_MINUS_1,
        status=U6BasisStatus.FROZEN,
        commit_id=commit,
        snapshot_id=StableId("snapshot.u6a.adapter.test"),
        plan_root_ref=cast(PlanRootRef, plan_ref),
        text_root_ref=cast(TextRootRef, text_ref),
        world_root_ref=cast(WorldRootRef, world_ref),
        profile_root_ref=cast(ProjectProfileRootRef, profile_ref),
    )


def _adapter(
    tmp_path: Path,
) -> tuple[U6AProductionReadoutAdapter, U6ACanaryJob, U6CheckpointBasis]:
    basis_artifacts, basis = _basis(tmp_path)
    config = U6AProductionReadoutConfig(
        manifest=cast(
            V05ReadoutCampaignManifest,
            SimpleNamespace(readout_manifest=SimpleNamespace(tasks=())),
        ),
        corpus=U4SPublicCorpus(BUNDLE),
        basis_artifacts=basis_artifacts,
        artifacts=ArtifactRepository(FilesystemObjectStore(tmp_path / "run-objects")),
        gateway=cast(ModelGateway, object()),
        bundle_root=BUNDLE,
        project_id=ProjectId("project.u6a.adapter.test"),
        run_id=RUN_ID,
    )
    adapter = U6AProductionReadoutAdapter(config)
    job = U6ACanaryJob(
        job_id=StableId("croll-101"),
        track=U6AReadoutTrack.C_ROLL,
        checkpoint_chapter=basis.checkpoint_chapter,
        basis_id=basis.basis_id,
    )
    return adapter, job, basis


def test_croll_release_is_public_apc_projection(tmp_path: Path) -> None:
    adapter, job, basis = _adapter(tmp_path)
    asyncio.run(adapter.execute_phase(phase="freeze", item=job, basis=basis, run_id=RUN_ID))
    released = asyncio.run(
        adapter.execute_phase(phase="release", item=job, basis=basis, run_id=RUN_ID)
    )

    assert released.phase == "release"
    assert released.future_leakage_count == 0
    state = adapter._states[job.job_id]
    assert state.task_input is not None
    assert state.task_input.task.information_profile.value == "author_plan_conditioned"
    assert state.task_input.plan.nodes
    assert state.task_input.planning_context.planner_may_read_plan is True
    assert state.task_input.task.target_chapter_start == 101
    assert state.task_input.task.target_chapter_end == 103


def test_dshort_release_keeps_need_task_id_typed(tmp_path: Path) -> None:
    adapter, _croll_job, basis = _adapter(tmp_path)
    job = U6ACanaryJob(
        job_id=StableId("dshort-101"),
        track=U6AReadoutTrack.D_SHORT,
        checkpoint_chapter=basis.checkpoint_chapter,
        basis_id=basis.basis_id,
    )
    asyncio.run(adapter.execute_phase(phase="freeze", item=job, basis=basis, run_id=RUN_ID))
    asyncio.run(adapter.execute_phase(phase="release", item=job, basis=basis, run_id=RUN_ID))

    state = adapter._states[job.job_id]
    assert state.task_input is not None
    assert isinstance(state.task_input.need.task_id, TaskId)
    assert state.task_input.need.task_id == TaskId("u6a.canary.task.dshort-101")


def test_u6a_phase_result_is_strictly_reparseable(tmp_path: Path) -> None:
    adapter, job, basis = _adapter(tmp_path)
    result = asyncio.run(
        adapter.execute_phase(phase="freeze", item=job, basis=basis, run_id=RUN_ID)
    )
    reparsed = U6AReadoutPhaseResult.model_validate_json(result.model_dump_json(), strict=True)
    assert reparsed == result
    assert result.evaluation_refs == result.artifact_refs


def test_wcp_runs_sync_planner_owner_outside_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, job, basis = _adapter(tmp_path)
    running_loop_seen: list[bool] = []

    def fake_wcp(item: Any, frozen_basis: Any, state: Any) -> U6AReadoutPhaseResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            running_loop_seen.append(False)
        else:
            running_loop_seen.append(True)
        return adapter._phase("wcp", (), (), _memory_identity(frozen_basis))

    monkeypatch.setattr(adapter, "_wcp", fake_wcp)
    result = asyncio.run(adapter.execute_phase(phase="wcp", item=job, basis=basis, run_id=RUN_ID))

    assert result.phase == "wcp"
    assert running_loop_seen == [False]
