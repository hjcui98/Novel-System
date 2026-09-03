from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.adapters.runtime.isolated import (
    StrictDeterministicCandidateMaterializer,
    StrictFakePlanningLeaf,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import CandidateKind, CreativeRunRequest
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import CommitId, ProjectId, RunId, SchemaVersion, StableId
from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite
from novel_agent.domain.runtime import TaskKind, TaskRecord
from novel_agent.domain.stage2 import (
    AgentMode,
    BootstrapStrategy,
    PlannerExecutionResult,
    ProjectIntentModel,
    ProposalProvenance,
    ProposedItem,
)
from novel_agent.domain.world import PlanLevel
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.ports.creative_runtime import WritingLeafPort
from novel_agent.runtime.production_novel_bootstrap import ProductionNovelBootstrap
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.creative_runtime import CreativeRuntimeService
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.projection import (
    DerivedProjectionService,
    DerivedSnapshotRepository,
    ProjectionOutboxRepository,
)
from novel_agent.services.runtime_acceptance import RuntimeAcceptanceService
from novel_agent.services.runtime_commands import RuntimeCommandService
from tests.unit.test_stage2_bootstrap_workflow import proposals

VERSION = SchemaVersion("1.0.0")
NOW = datetime(2026, 9, 3, tzinfo=UTC)


class _ProjectionBuilder:
    def build(self, project_id: ProjectId, source_commit: CommitId) -> DerivedSnapshotLite:
        del project_id
        suffix = source_commit.root.removeprefix("sha256:")
        return DerivedSnapshotLite(
            snapshot_id=StableId(f"snapshot.{suffix}"),
            source_commit=source_commit,
            anchor_build_id=StableId(f"anchor.{suffix[:24]}"),
            anchor_index_version="anchor-v1",
            grounded_index_version="grounded-v1",
            embedding_profile="offline-v1",
            fusion_profile="rrf-v1",
            build_status=DerivedBuildStatus.EXACT,
            published_at=NOW,
        )


class _WritingResult:
    def __init__(self, ref: ArtifactRef) -> None:
        self.status = WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
        self.final_candidate_id = ref.artifact_id
        self.final_text_artifact = ref
        self.artifacts = (ref,)
        self.observation = type("Observation", (), {"changes": ()})()


class _Writer:
    is_fixture = False

    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts

    async def run(self, request: WritingLoopRequest) -> WritingLoopResult:
        ref = self._artifacts.put(
            request.task_id.root.encode(), "text/plain", SchemaVersion("1.0.0")
        )
        return cast(WritingLoopResult, _WritingResult(ref))


def _chain(tasks: dict[str, TaskRecord], start: TaskRecord) -> tuple[TaskRecord, ...]:
    ordered = [start]
    current = start
    seen = {current.task_id.root}
    while current.dependency_task_ids:
        parent = tasks[current.dependency_task_ids[0].root]
        if parent.task_id.root in seen:
            break
        ordered.append(parent)
        seen.add(parent.task_id.root)
        current = parent
    return tuple(reversed(ordered))


def test_bootstrap_commit_schedules_story_volume_chapter_set_before_writer(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions: sessionmaker[Session] = build_session_factory(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    project_id = ProjectId("project.bootstrap.novel")
    plan_proposal, world_patch = proposals(project_id)
    plan_proposal = plan_proposal.model_copy(
        update={"strategy": BootstrapStrategy.DEVELOP_CANDIDATES}
    )
    planner_result = PlannerExecutionResult(
        mode=AgentMode.PROJECT_BOOTSTRAP,
        project_intent=ProjectIntentModel(
            intent_id=StableId("intent.bootstrap"),
            project_id=project_id,
            strategy=BootstrapStrategy.DEVELOP_CANDIDATES,
            items=(
                ProposedItem(
                    item_id=StableId("intent.item"),
                    kind="premise",
                    payload={"summary": "story"},
                    provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                    source_ids=(StableId("source.author-initial-brief"),),
                ),
            ),
            source_ids=(StableId("source.author-initial-brief"),),
            coverage=1,
        ),
        plan_proposal=plan_proposal.model_copy(
            update={
                "items": (
                    ProposedItem(
                        item_id=StableId("plan.item"),
                        kind="premise",
                        payload={"summary": "story"},
                        provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                        source_ids=(StableId("source.author-initial-brief"),),
                    ),
                ),
                "strategy": BootstrapStrategy.DEVELOP_CANDIDATES,
            }
        ),
        output_artifact=artifacts.put(b"planner", "application/json", VERSION),
        receipt=plan_proposal.receipt,
    )
    world_patch = world_patch.model_copy(
        update={
            "origin_source_ids": (StableId("source.author-initial-brief"),),
            "items": (
                ProposedItem(
                    item_id=StableId("world.item"),
                    kind="baseline_state",
                    payload={"fact": "known", "label": "Lin"},
                    provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                    source_ids=(StableId("source.author-initial-brief"),),
                ),
            ),
        }
    )

    async def planner() -> PlannerExecutionResult:
        return planner_result

    async def curator() -> object:
        return world_patch

    bootstrap = ProductionNovelBootstrap(
        artifacts=artifacts,
        session_factory=sessions,
        planner=planner,
        curator=curator,  # type: ignore[arg-type]
    )
    prepared = asyncio.run(
        bootstrap.prepare(project_id=project_id, brief_text="A wounded heir enters the tower.")
    )
    policy, request, _descriptor = bootstrap.commit(
        prepared=prepared.document,
        author_id=StableId("author.1"),
        reason="reviewed Plan/World/Profile",
        target_chapters=10,
        run_id=RunId("run.hierarchy.startup"),
        object_store_root=tmp_path / "objects",
    )
    assert request.plan_level is PlanLevel.STORY
    assert isinstance(request, CreativeRunRequest)

    commands = RuntimeCommandService(
        sessions,
        RunEventLogRepository(sessions),
        lambda _project_id: policy.permission_hash,
    )
    commits = CommitService(sessions)
    tasks = RuntimeTaskQueryRepository(sessions)
    runtime = CreativeRuntimeService(
        commands,
        RuntimeAcceptanceService(commands, commits, artifacts),
        commits,
        artifacts,
        StrictFakePlanningLeaf(artifacts),
        cast(WritingLeafPort, _Writer(artifacts)),
        lambda task: cast(
            WritingLoopRequest,
            type(
                "WritingRequest",
                (),
                {
                    "run_id": task.run_id,
                    "task_id": task.task_id,
                    "base_commit": task.basis_commit,
                    "snapshot_id": task.basis_snapshot,
                },
            )(),
        ),
        StrictDeterministicCandidateMaterializer(commits, candidate_kind=CandidateKind.PLAN),
        StrictDeterministicCandidateMaterializer(commits, candidate_kind=CandidateKind.DRAFT),
        DerivedProjectionService(ProjectionOutboxRepository(sessions), _ProjectionBuilder()),
        DerivedSnapshotRepository(sessions),
        lambda policy_hash: policy
        if policy_hash == policy.policy_hash
        else (_ for _ in ()).throw(KeyError(policy_hash)),
        tasks,
    )

    start = runtime.start(request)
    assert start.current_task_id is not None
    current = start.current_task_id
    draft: TaskRecord | None = None
    for step in range(12):
        task = commands.get_task(current)
        if task.kind is TaskKind.DRAFT_CANDIDATE:
            draft = task
            break
        result = asyncio.run(runtime.advance(current, worker_id=f"worker.{step}"))
        assert result.current_task_id is not None
        current = result.current_task_id
    assert draft is not None
    assert draft.chapter_index == 1

    by_id = {item.task_id.root: item for item in tasks.list_run(request.run_id)}
    sequence = tuple(
        (item.kind, item.plan_level, item.horizon_start, item.horizon_end)
        for item in _chain(by_id, draft)
    )
    assert sequence == (
        (TaskKind.PLAN_CANDIDATE, PlanLevel.STORY, None, None),
        (TaskKind.PLAN_ACCEPTANCE, PlanLevel.STORY, None, None),
        (TaskKind.PLAN_COMMIT, PlanLevel.STORY, None, None),
        (TaskKind.PROJECTION_FRESHNESS, PlanLevel.STORY, None, None),
        (TaskKind.PLAN_CANDIDATE, PlanLevel.ARC_VOLUME, None, None),
        (TaskKind.PLAN_ACCEPTANCE, PlanLevel.ARC_VOLUME, None, None),
        (TaskKind.PLAN_COMMIT, PlanLevel.ARC_VOLUME, None, None),
        (TaskKind.PROJECTION_FRESHNESS, PlanLevel.ARC_VOLUME, None, None),
        (TaskKind.PLAN_CANDIDATE, PlanLevel.CHAPTER_SET, 1, 5),
        (TaskKind.PLAN_ACCEPTANCE, PlanLevel.CHAPTER_SET, 1, 5),
        (TaskKind.PLAN_COMMIT, PlanLevel.CHAPTER_SET, 1, 5),
        (TaskKind.PROJECTION_FRESHNESS, PlanLevel.CHAPTER_SET, 1, 5),
        (TaskKind.DRAFT_CANDIDATE, None, 1, 5),
    )
    engine.dispose()
