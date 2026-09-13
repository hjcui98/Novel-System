"""The stage driver consumes complete evidence, not root counters."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.domain.benchmark import ChapterGoal, PlanRootDocument, TextRootDocument
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import (
    DerivedBuildStatus,
    DerivedSnapshotLite,
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    WorldRootDocument,
)
from novel_agent.domain.runtime import TaskKind, TaskRecord, TaskStatus
from novel_agent.domain.world import Entity, PlanLevel, PlanNode
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.stage_exit_audit import (
    StageRuntimeEvidence,
    audit_stage_roots,
    runtime_evidence_from_tasks,
)

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "a" * 64)
COMMIT = CommitId("sha256:" + "b" * 64)
OWNER = StableId("entity.protagonist")
OBLIGATION = StableId("obligation.story.0.objective")


def _node(
    node_id: str,
    level: PlanLevel,
    start: int,
    end: int,
    *,
    parent: str | None = None,
    source: bool = False,
    obligation: bool = False,
) -> PlanNode:
    return PlanNode(
        plan_node_id=StableId(node_id),
        node_type=level.value,
        title=node_id,
        summary="bounded plan node",
        parent_id=None if parent is None else StableId(parent),
        source_ids=(StableId("source.author"),) if source else (),
        obligation_ids=(OBLIGATION,) if obligation else (),
        plan_level=level,
        chapter_start=start,
        chapter_end=end,
    )


def _roots() -> tuple[PlanRootDocument, WorldRootDocument, TextRootDocument]:
    story = _node("story", PlanLevel.STORY, 1, 800, source=True)
    volumes = tuple(
        _node(
            f"volume.{index}",
            PlanLevel.ARC_VOLUME,
            start,
            start + 99,
            parent="story",
            source=True,
            obligation=index == 1,
        )
        for index, start in enumerate(range(1, 801, 100), start=1)
    )
    wrapper = _node(
        "chapter-set.1-5",
        PlanLevel.CHAPTER_SET,
        1,
        5,
        parent="volume.1",
        source=True,
    )
    chapters = tuple(
        _node(
            f"chapter.{index}",
            PlanLevel.CHAPTER,
            index,
            index,
            parent="chapter-set.1-5",
            source=True,
        )
        for index in range(1, 6)
    )
    goals = tuple(
        ChapterGoal(
            goal_id=StableId(f"chapter.{index}"),
            chapter_index=index,
            summary=f"advance chapter {index}",
            obligation_ids=(OBLIGATION,) if index == 1 else (),
            source_ids=(StableId("source.author"),),
            payload=(
                {
                    "obligation_actions": [
                        {
                            "obligation_id": OBLIGATION.root,
                            "action": "SETUP",
                            "expected_delta": "the first threshold is established",
                        }
                    ]
                }
                if index == 1
                else {
                    "history_retrieval": {
                        "requirement": "REQUIRED",
                        "reason": "prior causal state is needed",
                        "needs": [{"kind": "causal_history", "query": "prior state"}],
                    },
                    "obligation_actions": [],
                }
            ),
        )
        for index in range(1, 6)
    )
    plan = PlanRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        nodes=(story, *volumes, wrapper, *chapters),
        chapter_goals=goals,
    )
    world = WorldRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        source_commit=COMMIT,
        entities=(Entity(entity_id=OWNER, entity_type="person", internal_label="protagonist"),),
        obligations=(
            PlanObligation(
                obligation_id=OBLIGATION,
                kind=ObligationKind.OBJECTIVE,
                description="the protagonist reaches the first threshold",
                status=ObligationStatus.OPEN,
                owner_ids=(OWNER,),
                due_chapter=5,
                evidence_refs=(),
            ),
        ),
    )
    text = TextRootDocument(root_hash=HASH, schema_version=VERSION, chapters=())
    return plan, world, text


def _runtime() -> StageRuntimeEvidence:
    return StageRuntimeEvidence(
        plan_reviewed=True,
        plan_accepted=True,
        plan_committed=True,
        plan_projected=True,
    )


def test_g0_requires_complete_volume_obligation_chapter_and_projection_evidence() -> None:
    plan, world, text = _roots()
    snapshot = DerivedSnapshotLite(
        snapshot_id=StableId("snapshot.current"),
        source_commit=COMMIT,
        anchor_build_id=StableId("anchor.current"),
        anchor_index_version="anchor-v1",
        grounded_index_version="grounded-v1",
        embedding_profile="embedding-v1",
        fusion_profile="fusion-v1",
        build_status=DerivedBuildStatus.EXACT,
        published_at=datetime.now(UTC),
    )

    evidence = audit_stage_roots(
        commit_id=COMMIT,
        plan=plan,
        world=world,
        text=text,
        snapshot=snapshot,
        runtime=_runtime(),
    )

    assert evidence["volume_coverage_complete"] is True
    assert evidence["formal_obligations_complete"] is True
    assert evidence["chapter_set_contract_complete"] is True
    assert evidence["g0_evidence_complete"] is True


def test_eight_volume_count_with_a_gap_does_not_pass_g0() -> None:
    plan, world, text = _roots()
    gap = plan.nodes[2].model_copy(update={"chapter_start": 102, "chapter_end": 200})
    plan = plan.model_copy(update={"nodes": (*plan.nodes[:2], gap, *plan.nodes[3:])})

    evidence = audit_stage_roots(
        commit_id=COMMIT,
        plan=plan,
        world=world,
        text=text,
        runtime=_runtime(),
    )

    assert evidence["committed_volumes"] == 8
    assert evidence["volume_coverage_complete"] is False
    assert evidence["g0_evidence_complete"] is False


def test_g1_needs_history_consumption_and_recovery_proof() -> None:
    plan, world, text = _roots()
    evidence = audit_stage_roots(
        commit_id=COMMIT,
        plan=plan,
        world=world,
        text=text,
        runtime=StageRuntimeEvidence(
            history_consumed_pairs=frozenset({(1, 2)}),
            recovery_without_memory_rerun=True,
        ),
    )

    assert evidence["g1_evidence_complete"] is False


def test_successful_commit_task_without_typed_settlement_is_not_atomic() -> None:
    plan, world, text = _roots()
    task = TaskRecord(
        task_id=TaskId("task.chapter.1.commit"),
        run_id=RunId("run.audit"),
        project_id=ProjectId("project.audit"),
        kind=TaskKind.DRAFT_COMMIT,
        task_revision=0,
        status=TaskStatus.SUCCEEDED,
        basis_commit=COMMIT,
        policy_hash="sha256:" + "c" * 64,
        permission_hash="sha256:" + "d" * 64,
        chapter_index=1,
    )

    runtime = runtime_evidence_from_tasks(
        (task,),
        (),
        plan_reviewed=True,
        plan=plan,
        text=text,
        world=world,
    )

    assert runtime.atomic_chapter_writes == frozenset()
    assert runtime.content_reviewed_chapters == frozenset()


def test_atomic_write_requires_typed_workflow_and_matching_projection() -> None:
    from novel_agent.services.memory_write_workflow import (
        ImmediateProjectionReadinessPort,
        InMemoryArtifactRepository,
        InMemoryCandidateLineageRepository,
        InMemoryCheckpointRepository,
        InMemoryCommitPort,
    )
    from tests.contract.test_memory_write_workflow_contract import BASE, PROJECT
    from tests.unit.test_memory_write_resume import _ready_data, _workflow

    artifacts = InMemoryArtifactRepository()
    commit = InMemoryCommitPort(current_commit=BASE)
    workflow = _workflow(
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
        checkpoint=InMemoryCheckpointRepository(artifacts),
        commit=commit,
        projection=ImmediateProjectionReadinessPort(artifacts=artifacts),
    )
    data = _ready_data(
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
    )
    assert data.bundle is not None
    assert data.materialization is not None
    data.bundle = data.bundle.model_copy(update={"run_id": data.request.run_id})
    data.materialization = data.materialization.model_copy(update={"bundle": data.bundle})
    assert workflow._prepare_commit(data) is None
    assert workflow._commit(data) is None
    assert workflow._project(data) is None
    result = workflow._freshness(data)
    assert result is not None
    assert result.terminal_result_ref is not None
    assert result.checkpoint_ref is not None
    assert result.resulting_commit is not None

    candidate_task = TaskRecord(
        task_id=TaskId("task.chapter.1.candidate"),
        run_id=data.request.run_id,
        project_id=PROJECT,
        kind=TaskKind.DRAFT_CANDIDATE,
        task_revision=1,
        status=TaskStatus.SUCCEEDED,
        basis_commit=data.request.base_commit,
        policy_hash="sha256:" + "c" * 64,
        permission_hash="sha256:" + "d" * 64,
        chapter_index=1,
    )
    acceptance_task = candidate_task.model_copy(
        update={
            "task_id": TaskId("task.chapter.1.acceptance"),
            "kind": TaskKind.DRAFT_ACCEPTANCE,
            "dependency_task_ids": (candidate_task.task_id,),
        }
    )
    commit_task = TaskRecord(
        task_id=data.request.task_id,
        run_id=data.request.run_id,
        project_id=PROJECT,
        kind=TaskKind.DRAFT_COMMIT,
        task_revision=1,
        status=TaskStatus.SUCCEEDED,
        basis_commit=data.request.base_commit,
        policy_hash="sha256:" + "c" * 64,
        permission_hash="sha256:" + "d" * 64,
        dependency_task_ids=(acceptance_task.task_id,),
        terminal_artifact_refs=(result.terminal_result_ref, result.checkpoint_ref),
        chapter_index=1,
    )
    projection_task = TaskRecord(
        task_id=TaskId("task.chapter.1.projection"),
        run_id=data.request.run_id,
        project_id=PROJECT,
        kind=TaskKind.PROJECTION_FRESHNESS,
        task_revision=1,
        status=TaskStatus.SUCCEEDED,
        basis_commit=result.resulting_commit,
        policy_hash="sha256:" + "c" * 64,
        permission_hash="sha256:" + "d" * 64,
        dependency_task_ids=(commit_task.task_id,),
        chapter_index=1,
        projection_after="draft",
    )

    runtime = runtime_evidence_from_tasks(
        (candidate_task, acceptance_task, commit_task, projection_task),
        (),
        plan_reviewed=False,
        artifact_reader=artifacts.read_verified,
    )

    assert runtime.atomic_chapter_writes == frozenset({1})


def test_writer_result_proves_history_and_independent_observation(tmp_path: Path) -> None:
    from sqlalchemy import create_engine

    from novel_agent.adapters.postgres.database import Base, build_session_factory
    from novel_agent.domain.editorial import EditorialVerdict
    from novel_agent.domain.model_calls import (
        BudgetSource,
        EffectiveBudgetResult,
        ModelCallLedgerEntry,
        ModelCallLedgerStatus,
    )
    from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
    from tests.fixtures.stage1_synthetic import make_synthetic_bundle
    from tests.integration.test_writer_context_loop import _loop, _request

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    request = _request(artifacts, "audit-runtime")
    loop, model_request, _ = _loop(
        tmp_path,
        (RunEventLogRepository(factory), RunCheckpointRepository(factory)),
        request,
        EditorialVerdict.PASS,
        artifact_repository=artifacts,
    )
    result = asyncio.run(loop.execute(request, model_request, object()))
    result_ref = artifacts.put(
        canonical_json_bytes(result.model_dump(mode="json")),
        "application/vnd.novel-agent.writing-loop-result+json",
        VERSION,
    )
    candidate_task = TaskRecord(
        task_id=TaskId(request.task_id.root),
        run_id=request.run_id,
        project_id=request.project_id,
        kind=TaskKind.DRAFT_CANDIDATE,
        task_revision=0,
        status=TaskStatus.SUCCEEDED,
        basis_commit=request.base_commit,
        basis_snapshot=request.snapshot_id,
        policy_hash="sha256:" + "c" * 64,
        permission_hash="sha256:" + "d" * 64,
        terminal_artifact_refs=(result_ref,),
        chapter_index=request.writing_task.target_chapter,
        target_chapters=request.writing_task.target_chapter,
    )
    acceptance_task = candidate_task.model_copy(
        update={
            "task_id": TaskId(f"{request.task_id.root}.accept"),
            "kind": TaskKind.DRAFT_ACCEPTANCE,
            "status": TaskStatus.SUCCEEDED,
            "candidate_binding_ref": None,
            "dependency_task_ids": (candidate_task.task_id,),
            "terminal_artifact_refs": (),
        }
    )
    task = TaskRecord(
        task_id=TaskId(f"{request.task_id.root}.commit"),
        run_id=request.run_id,
        project_id=request.project_id,
        kind=TaskKind.DRAFT_COMMIT,
        task_revision=0,
        status=TaskStatus.SUCCEEDED,
        basis_commit=request.base_commit,
        basis_snapshot=request.snapshot_id,
        policy_hash="sha256:" + "c" * 64,
        permission_hash="sha256:" + "d" * 64,
        dependency_task_ids=(acceptance_task.task_id,),
        terminal_artifact_refs=(),
        chapter_index=request.writing_task.target_chapter,
        target_chapters=request.writing_task.target_chapter,
    )
    projection_task = task.model_copy(
        update={
            "task_id": TaskId(f"{request.task_id.root}.projection"),
            "kind": TaskKind.PROJECTION_FRESHNESS,
            "basis_commit": request.base_commit,
            "dependency_task_ids": (task.task_id,),
            "terminal_artifact_refs": (),
            "projection_after": "draft",
        }
    )
    text = next(item for item in make_synthetic_bundle().text_roots if len(item.chapters) == 20)
    events = RunEventLogRepository(factory).replay(request.run_id)
    budget = EffectiveBudgetResult(
        budget_source=BudgetSource.ENDPOINT_DEFAULT,
        context_limit=100_000,
        estimated_input_tokens=1,
        body_output_budget=1,
        thinking_budget=0,
        total_output_budget=1,
        safety_allowance_tokens=1,
        reserved_sequence_tokens=3,
        available_input_tokens=99_998,
    )
    model_calls = tuple(
        ModelCallLedgerEntry(
            request_id=record.request_id,
            run_id=record.run_id,
            task_id=record.task_id,
            request_hash=ArtifactId("sha256:" + "c" * 64),
            effective_budget=budget,
            reasoning_included_in_completion_tokens=False,
            status=ModelCallLedgerStatus.COMPLETED,
            logical_phase="writer-test",
            raw_response_hash=ArtifactId("sha256:" + "d" * 64),
            call_record=record,
            requested_at=record.started_at,
            completed_at=record.completed_at,
        )
        for record in result.model_call_records
    )

    runtime = runtime_evidence_from_tasks(
        (candidate_task, acceptance_task, task, projection_task),
        events,
        plan_reviewed=False,
        text=text,
        artifact_reader=artifacts.read_verified,
        model_calls=model_calls,
    )

    assert result.status.value == "DRAFT_CANDIDATE_READY"
    assert runtime.content_reviewed_chapters == frozenset({21})
    assert runtime.curator_observed_chapters == frozenset({21})
    assert runtime.history_consumed_pairs == frozenset({(20, 21)})
    assert runtime.budget_continuity_verified is True

    uncertain_runtime = runtime_evidence_from_tasks(
        (candidate_task, acceptance_task, task, projection_task),
        events,
        plan_reviewed=False,
        text=text,
        artifact_reader=artifacts.read_verified,
        model_calls=(
            model_calls[0].model_copy(update={"status": ModelCallLedgerStatus.UNCERTAIN}),
            *model_calls[1:],
        ),
    )
    assert uncertain_runtime.budget_continuity_verified is False
    engine.dispose()
