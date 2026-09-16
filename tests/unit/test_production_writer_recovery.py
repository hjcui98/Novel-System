"""The production Writer entry reads canonical prose and restores frozen Memory first.

Two defects lived at this seam.  The readiness gate read ``chapter.blocks`` on a
``ChapterDocument`` that only has ``scenes``, so a production request could not even
reach the gate.  And the request factory built a fresh Stage 2M Memory package before
it looked for a durable recovery checkpoint, so a restarted attempt re-ran Memory and
re-billed model calls the first attempt had already paid for.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import DerivedSnapshotRow
from novel_agent.adapters.runtime.stage3_writer import (
    ProductionWritingRequestFactory,
    Stage2MWriterContextInvocation,
    WriterRecoveryRefused,
    WritingRequestPolicy,
)
from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.benchmark import ChapterGoal, SceneDocument, TextRootDocument
from novel_agent.domain.generation import WritingLoopBudgets
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite
from novel_agent.domain.retrieval_decision import (
    FIRST_CHAPTER_WAIVER_REF,
    HistoryRetrievalDecision,
    HistoryRetrievalRequirement,
)
from novel_agent.domain.runtime import TaskKind, TaskRecord, TaskStatus
from novel_agent.domain.writing_loop import WRITING_LOOP_CHECKPOINT_MEDIA_TYPE
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes, plan_root_content_id
from novel_agent.services.evidence_first_writer_context_assembler import (
    EvidenceFirstAssemblyResult,
    EvidenceFirstWriterContextAssembler,
    NeedEvidenceSelection,
    SliceSelectionTrace,
)
from novel_agent.services.evidence_slice_resolver import EvidenceSliceResolver
from novel_agent.services.projection import DerivedSnapshotRepository
from novel_agent.services.recent_prose import RecentProseAssembler, RecentProseAssemblyError
from tests.factories import make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.fixtures.stage2_memory_benchmark import writer_context_inputs
from tests.unit.test_stage5_production_factories import _profile, _writing_policy
from tests.unit.test_writing_loop_repair_frontier import _checkpoint, _model_call

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "1" * 64)
PROJECT = ProjectId("project.test")
SNAPSHOT = StableId("snapshot.production-writer")


def _put(artifacts: ArtifactRepository, value: object, media_type: str) -> ArtifactRef:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return artifacts.put(canonical_json_bytes(payload), media_type, VERSION)


def _canonical(
    tmp_path: Path,
    *,
    text: TextRootDocument,
    target_chapter: int,
) -> tuple[ArtifactRepository, CommitService, CommitId, DerivedSnapshotRepository]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)
    commits = CommitService(session_factory)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    synthetic_goal = bundle.plan_roots[0].chapter_goals[0]
    goal = ChapterGoal(
        goal_id=StableId(f"plan.chapter.{target_chapter}"),
        chapter_index=target_chapter,
        summary="Continue the tower approach with the injured arm.",
        obligation_ids=synthetic_goal.obligation_ids,
        payload={
            "history_retrieval": (
                HistoryRetrievalDecision.first_chapter_waiver().model_dump(mode="json")
                if target_chapter == 1
                else {
                    "requirement": "REQUIRED",
                    "needs": [
                        {
                            "kind": "causal_history",
                            "query": "what happened before the tower approach",
                            "why_needed": "continuity",
                            "source_chapter_end": target_chapter - 1,
                        }
                    ],
                }
            )
        },
    )
    provisional = bundle.plan_roots[0].model_copy(
        update={"root_hash": ArtifactId("sha256:" + "0" * 64), "chapter_goals": (goal,)}
    )
    plan = provisional.model_copy(update={"root_hash": plan_root_content_id(provisional)})
    text_ref = _put(artifacts, text, "application/vnd.novel-agent.text-root+json")
    world_ref = _put(artifacts, world, "application/vnd.novel-agent.world-root+json")
    plan_ref = _put(artifacts, plan, "application/vnd.novel-agent.plan-root+json")
    profile_ref = _put(
        artifacts, _profile(), "application/vnd.novel-agent.project-profile-root+json"
    )
    manifest = make_manifest().model_copy(
        update={
            "text_root": TextRootRef(**text_ref.model_dump(mode="python")),
            "world_root": WorldRootRef(**world_ref.model_dump(mode="python")),
            "plan_root": PlanRootRef(**plan_ref.model_dump(mode="python")),
            "project_profile_root": ProjectProfileRootRef(**profile_ref.model_dump(mode="python")),
        }
    )
    base = commits.initialize_project(manifest)
    published = DerivedSnapshotLite(
        snapshot_id=SNAPSHOT,
        source_commit=base,
        anchor_build_id=StableId("anchor.production-writer"),
        anchor_index_version="anchor.v1",
        grounded_index_version="grounded.v1",
        embedding_profile="embedding.v1",
        fusion_profile="fusion.v1",
        build_status=DerivedBuildStatus.EXACT,
        published_at=datetime(2026, 9, 13, tzinfo=UTC),
    )
    with session_factory() as session:
        session.add(
            DerivedSnapshotRow(
                snapshot_id=SNAPSHOT.root,
                project_id=PROJECT.root,
                source_commit=base.root,
                build_status=DerivedBuildStatus.EXACT.value,
                snapshot_json=published.model_dump(mode="json"),
                published_at=published.published_at,
            )
        )
        session.commit()
    return artifacts, commits, base, DerivedSnapshotRepository(session_factory)


def _stage2m(invocation: Stage2MWriterContextInvocation) -> EvidenceFirstAssemblyResult:
    """Assemble like the production wrapper: lineage, requirement and freeze refs."""

    _fixture_task, needs, _units, _fixture_base = writer_context_inputs()
    blocks = [
        block
        for chapter in invocation.text.chapters
        for scene in chapter.scenes
        for block in scene.blocks
    ]
    first_chapter = invocation.task.target_chapter_start == 1
    if not blocks:
        empty = EvidenceFirstWriterContextAssembler().assemble(
            task=invocation.task,
            selections=(),
            text_root=invocation.text,
            basis_commit_id=invocation.base_commit,
            basis_snapshot_id=invocation.snapshot_id,
            retrieval_requirement=HistoryRetrievalRequirement.NOT_REQUIRED,
            history_waiver_ref=FIRST_CHAPTER_WAIVER_REF,
            plan_root_ref=invocation.plan_root_ref,
            plan_revision=invocation.plan_revision,
            chapter_goal_ids=invocation.chapter_goal_ids,
            planning_context_ref=invocation.planning_context_ref,
        )
        assert empty.status.value == "READY", empty.status
        return empty
    block = blocks[-1]
    requirement = (
        HistoryRetrievalRequirement.NOT_REQUIRED
        if first_chapter
        else HistoryRetrievalRequirement.REQUIRED
    )
    need = needs[0].model_copy(
        update={
            "run_id": invocation.run_id,
            "task_id": invocation.task.task_id,
            "base_commit": invocation.base_commit,
            "horizon_target": (invocation.task.target_chapter_start,) * 2,
        }
    )
    slice_ = EvidenceSliceResolver().resolve_block(
        block,
        source_commit=invocation.base_commit,
        snapshot_id=invocation.snapshot_id,
        access_scope=need.access_scope,
    )[0]
    result = EvidenceFirstWriterContextAssembler().assemble(
        task=invocation.task,
        selections=(
            NeedEvidenceSelection(
                need=need,
                selections=(
                    SliceSelectionTrace(
                        slice_id=slice_.slice_id,
                        unit_id=StableId("unit.production-writer"),
                        route_channel="r1_exact",
                        fused_rank=1,
                        selection_reason="production writer recovery",
                    ),
                ),
                slices=(slice_,),
            ),
        ),
        text_root=invocation.text,
        basis_commit_id=invocation.base_commit,
        basis_snapshot_id=invocation.snapshot_id,
        gateway_context_artifact=invocation.planning_context_ref,
        frozen_evidence_selections_artifact=invocation.plan_root_ref,
        retrieval_requirement=requirement,
        history_waiver_ref=FIRST_CHAPTER_WAIVER_REF if first_chapter else None,
        plan_root_ref=invocation.plan_root_ref,
        plan_revision=invocation.plan_revision,
        chapter_goal_ids=invocation.chapter_goal_ids,
        planning_context_ref=invocation.planning_context_ref,
    )
    assert result.status.value == "READY"
    package = result.package.model_copy(
        update={
            "semantic_status": "COMPLETE",
            "unclosed_mandatory_need_facets": (),
            "usable_with_gaps": True,
        }
    )
    return result.model_copy(update={"package": package, "semantic_status": "COMPLETE"})


class _CountingStage2M:
    """Counts how often the factory builds a Stage 2M Memory package."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, invocation: Stage2MWriterContextInvocation) -> EvidenceFirstAssemblyResult:
        self.calls += 1
        return _stage2m(invocation)


def _factory(
    artifacts: ArtifactRepository,
    commits: CommitService,
    snapshots: DerivedSnapshotRepository,
    stage2m: object,
    policy: WritingRequestPolicy | None = None,
) -> ProductionWritingRequestFactory:
    return ProductionWritingRequestFactory(
        commits=commits,
        artifacts=artifacts,
        recent_prose=RecentProseAssembler(artifacts, VERSION),
        writer_context=cast("object", stage2m),  # type: ignore[arg-type]
        policy=policy or _writing_policy(),
        schema_version=VERSION,
        snapshots=snapshots,
    )


def _task(
    *,
    base: CommitId,
    chapter_index: int,
    terminal_artifact_refs: tuple[ArtifactRef, ...] = (),
) -> TaskRecord:
    return TaskRecord(
        task_id=TaskId("task.production-writer"),
        run_id=RunId("run.production-writer"),
        project_id=PROJECT,
        kind=TaskKind.DRAFT_CANDIDATE,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        basis_snapshot=SNAPSHOT,
        policy_hash=HASH.root,
        permission_hash=HASH.root,
        chapter_index=chapter_index,
        target_chapters=25,
        current_attempt_id=StableId("attempt.production-writer"),
        terminal_artifact_refs=terminal_artifact_refs,
    )


def _twenty_chapters() -> TextRootDocument:
    bundle = make_synthetic_bundle()
    text = next(item for item in bundle.text_roots if len(item.chapters) == 20)
    assert text.chapters[-1].scenes[0].blocks
    return text


def test_the_first_chapter_builds_from_an_empty_text_root(tmp_path: Path) -> None:
    """An empty canonical basis has no prose, and the first chapter needs no history."""

    text = TextRootDocument(root_hash=HASH, schema_version=VERSION, chapters=())
    artifacts, commits, base, snapshots = _canonical(tmp_path, text=text, target_chapter=1)

    request = _factory(artifacts, commits, snapshots, _CountingStage2M())(
        _task(base=base, chapter_index=1)
    )

    assert request.recent_prose_context.previous_chapter is None
    assert request.recent_prose_context.checkpoint_chapter == 0


def test_the_twenty_first_chapter_builds_and_still_requires_history(tmp_path: Path) -> None:
    """The prose read comes from scenes, and chapter 21 keeps its history duty."""

    artifacts, commits, base, snapshots = _canonical(
        tmp_path, text=_twenty_chapters(), target_chapter=21
    )

    request = _factory(artifacts, commits, snapshots, _CountingStage2M())(
        _task(base=base, chapter_index=21)
    )

    previous = request.recent_prose_context.previous_chapter
    assert previous is not None
    assert previous.chapter_index == 20
    assert request.writer_context_package.retrieval_requirement is (
        HistoryRetrievalRequirement.REQUIRED
    )


def test_a_chapter_with_no_prose_still_fails_the_history_requirement(tmp_path: Path) -> None:
    """A chapter that exists without blocks is not history and must not pass as one."""

    text = _twenty_chapters()
    empty = text.chapters[-1].model_copy(
        update={
            "scenes": (
                SceneDocument(
                    scene_id=text.chapters[-1].scenes[0].scene_id,
                    scene_index=0,
                    blocks=(),
                ),
            )
        }
    )
    hollow = text.model_copy(update={"chapters": (*text.chapters[:-1], empty)})
    artifacts, commits, base, snapshots = _canonical(tmp_path, text=hollow, target_chapter=21)

    with pytest.raises(RecentProseAssemblyError, match="no prose blocks"):
        _factory(artifacts, commits, snapshots, _CountingStage2M())(
            _task(base=base, chapter_index=21)
        )


def _recovery_checkpoint(request: object, *, task: TaskRecord, base_commit: CommitId) -> object:
    """One coherent durable checkpoint for the request the factory just built."""

    template = _checkpoint()
    writing_task_ref = request.writing_task_artifact  # type: ignore[attr-defined]
    accepted_plan_ref = request.accepted_plan.artifact  # type: ignore[attr-defined]
    project_profile_ref = request.project_profile_artifact  # type: ignore[attr-defined]
    writer_context_ref = request.writer_context_package_artifact  # type: ignore[attr-defined]
    view = template.context_view.model_copy(
        update={
            "run_id": task.run_id,
            "task_id": task.task_id,
            "base_commit": base_commit,
            "snapshot_id": task.basis_snapshot,
            "plan_ref": accepted_plan_ref,
            "profile_ref": project_profile_ref,
            "seed_package_ref": writer_context_ref,
        }
    )
    work_plan = template.work_plan.model_copy(
        update={
            "work_plan": template.work_plan.work_plan.model_copy(
                update={
                    "writing_task_ref": writing_task_ref,
                    "accepted_plan_ref": accepted_plan_ref,
                    "writer_context_ref": writer_context_ref,
                }
            )
        }
    )
    return _checkpoint(
        run_id=task.run_id,
        task_id=task.task_id,
        base_commit=base_commit,
        snapshot_id=task.basis_snapshot,
        writing_task_ref=writing_task_ref,
        accepted_plan_ref=accepted_plan_ref,
        project_profile_ref=project_profile_ref,
        writer_context_ref=writer_context_ref,
        recent_prose_ref=request.recent_prose_context_artifact,  # type: ignore[attr-defined]
        context_view=view,
        work_plan=work_plan,
        active_writer_model_call=_model_call().model_copy(
            update={"run_id": task.run_id, "task_id": task.task_id}
        ),
    )


def test_a_durable_checkpoint_restores_memory_before_it_is_rebuilt(tmp_path: Path) -> None:
    artifacts, commits, base, snapshots = _canonical(
        tmp_path, text=_twenty_chapters(), target_chapter=21
    )
    task = _task(base=base, chapter_index=21)
    first_memory = _CountingStage2M()
    first = _factory(artifacts, commits, snapshots, first_memory)(task)
    assert first_memory.calls == 1

    checkpoint = _recovery_checkpoint(first, task=task, base_commit=base)
    checkpoint_ref = _put(artifacts, checkpoint, WRITING_LOOP_CHECKPOINT_MEDIA_TYPE)
    resumed_task = task.model_copy(update={"terminal_artifact_refs": (checkpoint_ref,)})
    poisoned = _CountingStage2M()
    resumed = _factory(artifacts, commits, snapshots, poisoned)(resumed_task)

    assert poisoned.calls == 0
    assert resumed.resume_checkpoint_ref == checkpoint_ref
    assert resumed.writer_context_package_artifact == first.writer_context_package_artifact
    assert resumed.recent_prose_context_artifact == first.recent_prose_context_artifact
    assert resumed.writer_context_package == first.writer_context_package


def test_a_corrupt_checkpoint_refuses_recovery_instead_of_rebuilding(tmp_path: Path) -> None:
    artifacts, commits, base, snapshots = _canonical(
        tmp_path, text=_twenty_chapters(), target_chapter=21
    )
    task = _task(base=base, chapter_index=21)
    broken_ref = artifacts.put(b"{not a checkpoint", WRITING_LOOP_CHECKPOINT_MEDIA_TYPE, VERSION)
    poisoned = _CountingStage2M()

    with pytest.raises(WriterRecoveryRefused, match="unreadable"):
        _factory(artifacts, commits, snapshots, poisoned)(
            task.model_copy(update={"terminal_artifact_refs": (broken_ref,)})
        )
    assert poisoned.calls == 0


def test_a_checkpoint_from_another_basis_refuses_recovery(tmp_path: Path) -> None:
    artifacts, commits, base, snapshots = _canonical(
        tmp_path, text=_twenty_chapters(), target_chapter=21
    )
    task = _task(base=base, chapter_index=21)
    first = _factory(artifacts, commits, snapshots, _CountingStage2M())(task)
    checkpoint = _recovery_checkpoint(first, task=task, base_commit=base)
    other_snapshot = StableId("snapshot.other-attempt")
    coherent = cast("object", checkpoint)
    mismatch = coherent.model_copy(  # type: ignore[attr-defined]
        update={
            "snapshot_id": other_snapshot,
            "context_view": coherent.context_view.model_copy(  # type: ignore[attr-defined]
                update={"snapshot_id": other_snapshot}
            ),
        }
    )
    mismatch_ref = _put(artifacts, mismatch, WRITING_LOOP_CHECKPOINT_MEDIA_TYPE)
    poisoned = _CountingStage2M()

    with pytest.raises(WriterRecoveryRefused, match="current task basis"):
        _factory(artifacts, commits, snapshots, poisoned)(
            task.model_copy(update={"terminal_artifact_refs": (mismatch_ref,)})
        )
    assert poisoned.calls == 0


def test_a_resumed_request_keeps_the_frozen_writer_contract(tmp_path: Path) -> None:
    """The restored package still binds the frozen contract, not a rebuilt one."""

    artifacts, commits, base, snapshots = _canonical(
        tmp_path, text=_twenty_chapters(), target_chapter=21
    )
    task = _task(base=base, chapter_index=21)
    first = _factory(artifacts, commits, snapshots, _CountingStage2M())(task)
    checkpoint = _recovery_checkpoint(first, task=task, base_commit=base)
    checkpoint_ref = _put(artifacts, checkpoint, WRITING_LOOP_CHECKPOINT_MEDIA_TYPE)
    changed = replace(
        _writing_policy(),
        budgets=WritingLoopBudgets(
            context_sequence_limit=32_000,
            reserved_output_tokens=6_000,
            context_safety_allowance_tokens=1_000,
            context_soft_limit_tokens=24_000,
        ),
    )

    resumed = _factory(artifacts, commits, snapshots, _CountingStage2M(), policy=changed)(
        task.model_copy(update={"terminal_artifact_refs": (checkpoint_ref,)})
    )

    assert resumed.writer_context_package.task_contract == (
        first.writer_context_package.task_contract
    )
    assert resumed.budgets == changed.budgets
