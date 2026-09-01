from __future__ import annotations

import pytest
from sqlalchemy import create_engine, delete

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import RuntimeTaskAttemptRow
from novel_agent.domain.ids import ProjectId, RunId, StableId, TaskId
from novel_agent.domain.runtime import (
    EffectReceipt,
    EffectStatus,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.services.commits import CommitService
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.runtime_commands import RuntimeCommandConflictError, RuntimeCommandService
from tests.factories import make_commit_request, make_manifest

HASH = "sha256:" + "1" * 64


def test_runtime_records_stage2w_commit_under_the_existing_writer_fence() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    commands = RuntimeCommandService(
        factory,
        RunEventLogRepository(factory),
        lambda _project_id: HASH,
    )
    task = commands.create_task(
        TaskRecord(
            task_id=TaskId("task.chapter-settlement.commit"),
            run_id=RunId("run.chapter-settlement.commit"),
            project_id=ProjectId("project.test"),
            kind=TaskKind.DRAFT_COMMIT,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=base,
            policy_hash=HASH,
            permission_hash=HASH,
            chapter_index=21,
            target_chapters=25,
        )
    )
    claimed, fence = commands.claim(
        task.task_id,
        worker_id="chapter-settlement-test",
        observed_revision=task.task_revision,
    )
    commands.mark_started(fence)
    fence = commands.claim_writer_lane(fence)
    identity = StableId("chapter-settlement.external")
    requested = EffectReceipt(
        effect_identity=identity,
        external_system="stage2w.chapter_reveal_atomic",
        request_identity=identity,
        status=EffectStatus.REQUESTED,
        attempt_no=claimed.attempt_no,
    )
    commands.record_effect_requested(fence, requested)
    external = commits.commit(make_commit_request(base, idempotency_key=identity.root))
    assert external.commit_id is not None
    completed = requested.model_copy(
        update={
            "status": EffectStatus.COMPLETED,
            "provider_request_id": external.commit_id.root,
        }
    )
    projection = TaskRecord(
        task_id=TaskId("task.chapter-settlement.commit.projection"),
        run_id=task.run_id,
        project_id=task.project_id,
        kind=TaskKind.PROJECTION_FRESHNESS,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=external.commit_id,
        policy_hash=task.policy_hash,
        permission_hash=task.permission_hash,
        dependency_task_ids=(task.task_id,),
        chapter_index=task.chapter_index,
        target_chapters=task.target_chapters,
        projection_after="draft",
    )

    settled = commands.record_external_commit(
        fence,
        external.commit_id,
        commits=commits,
        effect_receipt=completed,
        successor_tasks=(projection,),
    )

    assert settled.status is TaskStatus.SUCCEEDED
    assert settled.current_attempt_id is None
    assert commands.get_task(projection.task_id) == projection
    assert commits.result_for_idempotency(task.project_id, identity) == external
    assert commits.current_commit(ProjectId("project.test")) == external.commit_id


def test_runtime_reconciles_stage2w_receipt_after_the_worker_fence_is_lost() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    commands = RuntimeCommandService(
        factory,
        RunEventLogRepository(factory),
        lambda _project_id: HASH,
    )
    task = commands.create_task(
        TaskRecord(
            task_id=TaskId("task.chapter-settlement.recover"),
            run_id=RunId("run.chapter-settlement.recover"),
            project_id=ProjectId("project.test"),
            kind=TaskKind.DRAFT_COMMIT,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=base,
            policy_hash=HASH,
            permission_hash=HASH,
            chapter_index=21,
            target_chapters=25,
        )
    )
    claimed, fence = commands.claim(task.task_id, worker_id="lost-worker")
    commands.mark_started(fence)
    fence = commands.claim_writer_lane(fence)
    identity = StableId("chapter-settlement.recover")
    requested = EffectReceipt(
        effect_identity=identity,
        external_system="stage2w.chapter_reveal_atomic",
        request_identity=identity,
        status=EffectStatus.REQUESTED,
        attempt_no=claimed.attempt_no,
    )
    commands.record_effect_requested(fence, requested)
    external = commits.commit(make_commit_request(base, idempotency_key=identity.root))
    assert external.commit_id is not None
    projection = TaskRecord(
        task_id=TaskId("task.chapter-settlement.recover.projection"),
        run_id=task.run_id,
        project_id=task.project_id,
        kind=TaskKind.PROJECTION_FRESHNESS,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=external.commit_id,
        policy_hash=task.policy_hash,
        permission_hash=task.permission_hash,
        dependency_task_ids=(task.task_id,),
        chapter_index=task.chapter_index,
        target_chapters=task.target_chapters,
        projection_after="draft",
    )
    completed = requested.model_copy(
        update={
            "status": EffectStatus.COMPLETED,
            "provider_request_id": external.commit_id.root,
        }
    )

    settled = commands.reconcile_external_commit(
        task.task_id,
        external.commit_id,
        commits=commits,
        effect_receipt=completed,
        successor_tasks=(projection,),
        observed_revision=commands.get_task(task.task_id).task_revision,
    )

    assert settled.status is TaskStatus.SUCCEEDED
    assert settled.current_attempt_id is None
    assert commands.get_task(projection.task_id) == projection


def test_reconcile_and_record_external_commit_fail_closed() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    commands = RuntimeCommandService(
        factory,
        RunEventLogRepository(factory),
        lambda _project_id: HASH,
    )
    try:
        idle = commands.create_task(
            TaskRecord(
                task_id=TaskId("task.chapter-settlement.idle"),
                run_id=RunId("run.chapter-settlement.idle"),
                project_id=ProjectId("project.test"),
                kind=TaskKind.DRAFT_COMMIT,
                task_revision=0,
                status=TaskStatus.READY,
                basis_commit=base,
                policy_hash=HASH,
                permission_hash=HASH,
            )
        )
        with pytest.raises(RuntimeCommandConflictError, match="no interrupted external Attempt"):
            commands.reconcile_external_commit(
                idle.task_id,
                base,
                commits=commits,
                effect_receipt=EffectReceipt(
                    effect_identity=StableId("effect.idle"),
                    external_system="stage2w.chapter_reveal_atomic",
                    request_identity=StableId("effect.idle"),
                    status=EffectStatus.COMPLETED,
                    attempt_no=1,
                    provider_request_id=base.root,
                ),
                successor_tasks=(),
            )

        plan_project = ProjectId("project.plan-commit")
        plan_base = commits.initialize_project(make_manifest(plan_project))
        plan = commands.create_task(
            TaskRecord(
                task_id=TaskId("task.chapter-settlement.plan"),
                run_id=RunId("run.chapter-settlement.plan"),
                project_id=plan_project,
                kind=TaskKind.PLAN_COMMIT,
                task_revision=0,
                status=TaskStatus.READY,
                basis_commit=plan_base,
                policy_hash=HASH,
                permission_hash=HASH,
            )
        )
        _, plan_fence = commands.claim(plan.task_id, worker_id="plan-worker")
        commands.mark_started(plan_fence)
        plan_fence = commands.claim_writer_lane(plan_fence)
        with pytest.raises(RuntimeCommandConflictError, match="Draft commit task"):
            commands.record_external_commit(plan_fence, plan_base, commits=commits)

        task = commands.create_task(
            TaskRecord(
                task_id=TaskId("task.chapter-settlement.edges"),
                run_id=RunId("run.chapter-settlement.edges"),
                project_id=ProjectId("project.test"),
                kind=TaskKind.DRAFT_COMMIT,
                task_revision=0,
                status=TaskStatus.READY,
                basis_commit=base,
                policy_hash=HASH,
                permission_hash=HASH,
                chapter_index=21,
                target_chapters=25,
            )
        )
        claimed, fence = commands.claim(task.task_id, worker_id="edge-worker")
        commands.mark_started(fence)
        fence = commands.claim_writer_lane(fence)
        identity = StableId("chapter-settlement.edges")
        requested = EffectReceipt(
            effect_identity=identity,
            external_system="stage2w.chapter_reveal_atomic",
            request_identity=identity,
            status=EffectStatus.REQUESTED,
            attempt_no=claimed.attempt_no,
        )
        commands.record_effect_requested(fence, requested)
        with pytest.raises(RuntimeCommandConflictError, match="does not advance"):
            commands.record_external_commit(fence, base, commits=commits)

        external = commits.commit(
            make_commit_request(base, idempotency_key="chapter-settlement.edges")
        )
        assert external.commit_id is not None
        with pytest.raises(RuntimeCommandConflictError, match="does not prove"):
            commands.record_external_commit(
                fence,
                external.commit_id,
                commits=commits,
                effect_receipt=requested,
            )
        completed = requested.model_copy(
            update={
                "status": EffectStatus.COMPLETED,
                "provider_request_id": external.commit_id.root,
            }
        )
        foreign = completed.model_copy(update={"effect_identity": StableId("effect.foreign")})
        with pytest.raises(RuntimeCommandConflictError, match="another Attempt"):
            commands.record_external_commit(
                fence,
                external.commit_id,
                commits=commits,
                effect_receipt=foreign,
            )
        mutated = completed.model_copy(update={"request_identity": StableId("request.mutated")})
        with pytest.raises(RuntimeCommandConflictError, match="identity changed"):
            commands.record_external_commit(
                fence,
                external.commit_id,
                commits=commits,
                effect_receipt=mutated,
            )

        missing = commands.create_task(
            TaskRecord(
                task_id=TaskId("task.chapter-settlement.missing-row"),
                run_id=RunId("run.chapter-settlement.missing-row"),
                project_id=ProjectId("project.test"),
                kind=TaskKind.DRAFT_COMMIT,
                task_revision=0,
                status=TaskStatus.READY,
                basis_commit=external.commit_id,
                policy_hash=HASH,
                permission_hash=HASH,
            )
        )
        _, missing_fence = commands.claim(missing.task_id, worker_id="missing-worker")
        commands.mark_started(missing_fence)
        with factory() as session, session.begin():
            session.execute(
                delete(RuntimeTaskAttemptRow).where(
                    RuntimeTaskAttemptRow.attempt_id == missing_fence.attempt_id.root
                )
            )
        with pytest.raises(RuntimeCommandConflictError, match="projection is missing"):
            commands.reconcile_external_commit(
                missing.task_id,
                external.commit_id,
                commits=commits,
                effect_receipt=completed,
                successor_tasks=(),
            )
    finally:
        engine.dispose()


def test_record_external_commit_allows_a_fenced_commit_without_an_outer_effect() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    commands = RuntimeCommandService(
        factory,
        RunEventLogRepository(factory),
        lambda _project_id: HASH,
    )
    try:
        task = commands.create_task(
            TaskRecord(
                task_id=TaskId("task.chapter-settlement.no-effect"),
                run_id=RunId("run.chapter-settlement.no-effect"),
                project_id=ProjectId("project.test"),
                kind=TaskKind.DRAFT_COMMIT,
                task_revision=0,
                status=TaskStatus.READY,
                basis_commit=base,
                policy_hash=HASH,
                permission_hash=HASH,
                chapter_index=21,
                target_chapters=25,
            )
        )
        _, fence = commands.claim(task.task_id, worker_id="no-effect-worker")
        commands.mark_started(fence)
        fence = commands.claim_writer_lane(fence)
        external = commits.commit(
            make_commit_request(base, idempotency_key="chapter-settlement.no-effect")
        )
        assert external.commit_id is not None
        settled = commands.record_external_commit(
            fence,
            external.commit_id,
            commits=commits,
        )
        assert settled.status is TaskStatus.SUCCEEDED
        assert settled.current_attempt_id is None
    finally:
        engine.dispose()
