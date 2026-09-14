"""Focused transaction-boundary evidence for Stage 5 successor creation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.domain.creative_runtime import (
    AcceptanceDecision,
    AcceptanceReceipt,
    AcceptedCandidateBinding,
    ActorKind,
    CandidateBinding,
    CandidateKind,
    commit_task_from_acceptance,
)
from novel_agent.domain.ids import CommitId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.runtime import AttemptOutcome, TaskKind, TaskRecord, TaskStatus
from novel_agent.services.commits import CommitService, manifest_commit_id
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.runtime_commands import (
    RuntimeCommandConflictError,
    RuntimeCommandService,
)
from novel_agent.services.runtime_projection import assert_task_projection_matches
from tests.factories import make_artifact, make_commit_request, make_manifest

HASH = "sha256:" + "1" * 64
PERMISSION_HASH = "sha256:" + "2" * 64
NOW = datetime(2026, 8, 13, tzinfo=UTC)


def _task(
    *,
    task_id: str,
    run_id: str,
    kind: TaskKind,
    status: TaskStatus,
    basis: CommitId,
    dependency: TaskId | None = None,
) -> TaskRecord:
    return TaskRecord(
        task_id=TaskId(task_id),
        run_id=RunId(run_id),
        project_id=ProjectId("project.test"),
        kind=kind,
        task_revision=0,
        status=status,
        basis_commit=basis,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
        dependency_task_ids=(() if dependency is None else (dependency,)),
    )


@pytest.fixture
def kernel() -> tuple[
    RuntimeCommandService,
    CommitService,
    RunEventLogRepository,
    RuntimeTaskQueryRepository,
    CommitId,
]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    events = RunEventLogRepository(factory)
    commands = RuntimeCommandService(factory, events, lambda _project_id: PERMISSION_HASH)
    return commands, commits, events, RuntimeTaskQueryRepository(factory), base


def _assert_replay(
    events: RunEventLogRepository,
    query: RuntimeTaskQueryRepository,
    run_id: RunId,
) -> None:
    assert_task_projection_matches(events.replay(run_id), query.list_run(run_id))


def test_candidate_settlement_and_acceptance_successor_share_one_transaction(
    kernel: tuple[
        RuntimeCommandService,
        CommitService,
        RunEventLogRepository,
        RuntimeTaskQueryRepository,
        CommitId,
    ],
) -> None:
    commands, _commits, events, query, base = kernel
    candidate = commands.create_task(
        _task(
            task_id="task.atomic-candidate",
            run_id="run.atomic-candidate",
            kind=TaskKind.PLAN_CANDIDATE,
            status=TaskStatus.READY,
            basis=base,
        )
    )
    _, fence = commands.claim(candidate.task_id, worker_id="planner")
    commands.mark_started(fence)
    successor = _task(
        task_id="task.atomic-candidate.accept",
        run_id=candidate.run_id.root,
        kind=TaskKind.PLAN_ACCEPTANCE,
        status=TaskStatus.WAITING_INPUT,
        basis=base,
        dependency=candidate.task_id,
    )

    settled = commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
        successor_tasks=(successor,),
    )

    assert settled.status is TaskStatus.SUCCEEDED
    assert commands.get_task(successor.task_id) == successor
    _assert_replay(events, query, candidate.run_id)

    rollback = commands.create_task(
        _task(
            task_id="task.atomic-rollback",
            run_id="run.atomic-rollback",
            kind=TaskKind.PLAN_CANDIDATE,
            status=TaskStatus.READY,
            basis=base,
        )
    )
    _, rollback_fence = commands.claim(rollback.task_id, worker_id="planner")
    commands.mark_started(rollback_fence)
    illegal = _task(
        task_id="task.atomic-rollback.commit",
        run_id=rollback.run_id.root,
        kind=TaskKind.PLAN_COMMIT,
        status=TaskStatus.READY,
        basis=base,
        dependency=rollback.task_id,
    )
    with pytest.raises(RuntimeCommandConflictError, match="fixed runtime topology"):
        commands.settle_attempt(
            rollback_fence,
            outcome=AttemptOutcome.SUCCEEDED,
            terminal_status=TaskStatus.SUCCEEDED,
            successor_tasks=(illegal,),
        )
    assert commands.get_task(rollback.task_id).status is TaskStatus.RUNNING
    with pytest.raises(LookupError):
        commands.get_task(illegal.task_id)


def test_acceptance_settlement_and_commit_successor_share_one_transaction(
    kernel: tuple[
        RuntimeCommandService,
        CommitService,
        RunEventLogRepository,
        RuntimeTaskQueryRepository,
        CommitId,
    ],
) -> None:
    commands, _commits, events, query, base = kernel
    artifact = make_artifact("a")
    candidate = CandidateBinding(
        candidate_id=StableId("candidate.atomic-acceptance"),
        kind=CandidateKind.PLAN,
        artifact_ref=artifact,
        candidate_hash=artifact.artifact_id.root,
        basis_commit=base,
    )
    waiting = commands.create_task(
        _task(
            task_id="task.atomic-acceptance",
            run_id="run.atomic-acceptance",
            kind=TaskKind.PLAN_ACCEPTANCE,
            status=TaskStatus.WAITING_INPUT,
            basis=base,
        ).model_copy(update={"input_artifact_refs": (artifact,)})
    )
    binding = AcceptedCandidateBinding(
        acceptance_id=StableId("acceptance.atomic"),
        command_id=StableId("command.atomic"),
        project_id=waiting.project_id,
        run_id=waiting.run_id,
        task_id=waiting.task_id,
        candidate=candidate,
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        accepted_at=NOW,
        expected_project_commit=base,
    )
    receipt = AcceptanceReceipt(
        receipt_id=StableId("receipt.atomic"),
        command_id=binding.command_id,
        idempotency_identity=StableId("acceptance.atomic.identity"),
        command_hash=HASH,
        decision=AcceptanceDecision.ACCEPT,
        candidate=candidate,
        accepted_binding=binding,
        reason="approved",
        recorded_at=NOW,
    )
    settled_view = waiting.model_copy(
        update={
            "task_revision": 1,
            "status": TaskStatus.SUCCEEDED,
            "terminal_artifact_refs": (artifact,),
        }
    )
    successor = commit_task_from_acceptance(settled_view, receipt)

    settled = commands.complete_waiting_task(
        waiting.task_id,
        receipt=receipt,
        receipt_ref=artifact,
        successor_tasks=(successor,),
    )

    assert settled.status is TaskStatus.SUCCEEDED
    assert commands.get_task(successor.task_id) == successor
    _assert_replay(events, query, waiting.run_id)


def test_commit_and_projection_successor_share_one_transaction(
    kernel: tuple[
        RuntimeCommandService,
        CommitService,
        RunEventLogRepository,
        RuntimeTaskQueryRepository,
        CommitId,
    ],
) -> None:
    commands, commits, events, query, base = kernel
    task = commands.create_task(
        _task(
            task_id="task.atomic-commit",
            run_id="run.atomic-commit",
            kind=TaskKind.PLAN_COMMIT,
            status=TaskStatus.READY,
            basis=base,
        )
    )
    _, fence = commands.claim(task.task_id, worker_id="commit-worker")
    commands.mark_started(fence)
    fence = commands.claim_writer_lane(fence)
    request = make_commit_request(base, idempotency_key="commit.atomic")
    commit_id = manifest_commit_id(request.bundle.proposed_roots)
    successor = _task(
        task_id="task.atomic-commit.projection",
        run_id=task.run_id.root,
        kind=TaskKind.PROJECTION_FRESHNESS,
        status=TaskStatus.READY,
        basis=commit_id,
        dependency=task.task_id,
    ).model_copy(update={"projection_after": "plan"})

    result = commands.commit_accepted_candidate(
        fence,
        request,
        commits,
        successor_tasks=(successor,),
    )

    assert result.commit_id == commit_id
    assert commands.get_task(task.task_id).status is TaskStatus.SUCCEEDED
    assert commands.get_task(successor.task_id) == successor
    _assert_replay(events, query, task.run_id)


def test_projection_settlement_creates_all_ready_successors_atomically(
    kernel: tuple[
        RuntimeCommandService,
        CommitService,
        RunEventLogRepository,
        RuntimeTaskQueryRepository,
        CommitId,
    ],
) -> None:
    commands, _commits, events, query, base = kernel
    projection = commands.create_task(
        _task(
            task_id="task.atomic-projection",
            run_id="run.atomic-projection",
            kind=TaskKind.PROJECTION_FRESHNESS,
            status=TaskStatus.READY,
            basis=base,
        ).model_copy(update={"projection_after": "plan"})
    )
    _, fence = commands.claim(projection.task_id, worker_id="projection-worker")
    commands.mark_started(fence)
    draft = _task(
        task_id="task.atomic-projection.draft",
        run_id=projection.run_id.root,
        kind=TaskKind.DRAFT_CANDIDATE,
        status=TaskStatus.READY,
        basis=base,
        dependency=projection.task_id,
    )
    lookahead = _task(
        task_id="task.atomic-projection.lookahead",
        run_id=projection.run_id.root,
        kind=TaskKind.PLAN_CANDIDATE,
        status=TaskStatus.READY,
        basis=base,
        dependency=projection.task_id,
    )

    settled = commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
        successor_tasks=(draft, lookahead),
    )

    assert settled.status is TaskStatus.SUCCEEDED
    assert commands.get_task(draft.task_id) == draft
    assert commands.get_task(lookahead.task_id) == lookahead
    _assert_replay(events, query, projection.run_id)


def test_projection_settlement_updates_unclaimed_draft_candidate_with_newer_basis(
    kernel: tuple[
        RuntimeCommandService,
        CommitService,
        RunEventLogRepository,
        RuntimeTaskQueryRepository,
        CommitId,
    ],
) -> None:
    commands, commits, events, query, base = kernel
    new_request = make_commit_request(base, idempotency_key="commit.new-plan")
    new_commit_id = manifest_commit_id(new_request.bundle.proposed_roots)
    commits.commit(new_request)

    initial_draft = _task(
        task_id="run.rebase.draft.4",
        run_id="run.rebase",
        kind=TaskKind.DRAFT_CANDIDATE,
        status=TaskStatus.READY,
        basis=base,
    ).model_copy(update={"chapter_index": 4})
    commands.create_task(initial_draft)

    plan_projection = commands.create_task(
        _task(
            task_id="task.rebase-projection",
            run_id="run.rebase",
            kind=TaskKind.PROJECTION_FRESHNESS,
            status=TaskStatus.READY,
            basis=new_commit_id,
        ).model_copy(update={"projection_after": "plan"})
    )
    _, fence = commands.claim(plan_projection.task_id, worker_id="projection-worker")
    commands.mark_started(fence)

    updated_draft = _task(
        task_id="run.rebase.draft.4",
        run_id="run.rebase",
        kind=TaskKind.DRAFT_CANDIDATE,
        status=TaskStatus.READY,
        basis=new_commit_id,
        dependency=plan_projection.task_id,
    ).model_copy(update={"chapter_index": 4, "horizon_start": 4, "horizon_end": 8})

    settled = commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
        successor_tasks=(updated_draft,),
    )

    assert settled.status is TaskStatus.SUCCEEDED
    stored = commands.get_task(initial_draft.task_id)
    assert stored.basis_commit == new_commit_id
    assert stored.dependency_task_ids == (plan_projection.task_id,)
    assert stored.horizon_start == 4
    assert stored.horizon_end == 8
    _assert_replay(events, query, plan_projection.run_id)


def test_projection_settlement_rebases_completed_draft_with_new_identity(
    kernel: tuple[
        RuntimeCommandService,
        CommitService,
        RunEventLogRepository,
        RuntimeTaskQueryRepository,
        CommitId,
    ],
) -> None:
    commands, commits, events, query, base = kernel
    old_draft = _task(
        task_id="run.rebase-completed.draft.2",
        run_id="run.rebase-completed",
        kind=TaskKind.DRAFT_CANDIDATE,
        status=TaskStatus.READY,
        basis=base,
    ).model_copy(update={"chapter_index": 2})
    commands.create_task(old_draft)
    _, old_fence = commands.claim(old_draft.task_id, worker_id="draft-worker")
    commands.mark_started(old_fence)
    old_acceptance = _task(
        task_id="run.rebase-completed.draft.2.accept",
        run_id=old_draft.run_id.root,
        kind=TaskKind.DRAFT_ACCEPTANCE,
        status=TaskStatus.WAITING_INPUT,
        basis=base,
        dependency=old_draft.task_id,
    ).model_copy(update={"chapter_index": 2})
    commands.settle_attempt(
        old_fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
        successor_tasks=(old_acceptance,),
    )

    new_request = make_commit_request(base, idempotency_key="commit.rebase-completed")
    new_commit_id = manifest_commit_id(new_request.bundle.proposed_roots)
    commits.commit(new_request)
    plan_projection = commands.create_task(
        _task(
            task_id="task.rebase-completed-projection",
            run_id=old_draft.run_id.root,
            kind=TaskKind.PROJECTION_FRESHNESS,
            status=TaskStatus.READY,
            basis=new_commit_id,
        ).model_copy(update={"projection_after": "plan"})
    )
    _, projection_fence = commands.claim(
        plan_projection.task_id,
        worker_id="projection-worker",
    )
    commands.mark_started(projection_fence)
    rebased_draft = old_draft.model_copy(
        update={
            "basis_commit": new_commit_id,
            "basis_snapshot": StableId("snapshot.rebase-completed"),
            "dependency_task_ids": (plan_projection.task_id,),
            "status": TaskStatus.READY,
            "task_revision": 0,
            "current_attempt_id": None,
        }
    )

    settled = commands.settle_attempt(
        projection_fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
        successor_tasks=(rebased_draft,),
    )

    assert settled.status is TaskStatus.SUCCEEDED
    preserved = commands.get_task(old_draft.task_id)
    assert preserved.status is TaskStatus.SUCCEEDED
    assert preserved.basis_commit == base
    stale_acceptance = commands.get_task(old_acceptance.task_id)
    assert stale_acceptance.status is TaskStatus.CANCELLED
    assert stale_acceptance.superseded is True
    new_candidates = tuple(
        task
        for task in query.list_run(plan_projection.run_id)
        if task.kind is TaskKind.DRAFT_CANDIDATE and task.basis_commit == new_commit_id
    )
    assert len(new_candidates) == 1
    assert new_candidates[0].task_id != old_draft.task_id
    assert new_candidates[0].task_id.root.startswith("run.rebase-completed.draft.2.basis.")
    assert new_candidates[0].dependency_task_ids == (plan_projection.task_id,)
    _assert_replay(events, query, plan_projection.run_id)


def test_projection_settlement_rejects_updating_claimed_draft_candidate(
    kernel: tuple[
        RuntimeCommandService,
        CommitService,
        RunEventLogRepository,
        RuntimeTaskQueryRepository,
        CommitId,
    ],
) -> None:
    commands, commits, _events, _query, base = kernel

    initial_draft = _task(
        task_id="run.claimed.draft.4",
        run_id="run.claimed",
        kind=TaskKind.DRAFT_CANDIDATE,
        status=TaskStatus.READY,
        basis=base,
    ).model_copy(update={"chapter_index": 4})
    commands.create_task(initial_draft)

    # Claim draft candidate so it is no longer unclaimed initial task
    _, draft_fence = commands.claim(initial_draft.task_id, worker_id="draft-worker")
    commands.mark_started(draft_fence)

    new_request = make_commit_request(base, idempotency_key="commit.new-plan-2")
    new_commit_id = manifest_commit_id(new_request.bundle.proposed_roots)
    commits.commit(new_request)

    plan_projection = commands.create_task(
        _task(
            task_id="task.claimed-rebase-projection",
            run_id="run.claimed",
            kind=TaskKind.PROJECTION_FRESHNESS,
            status=TaskStatus.READY,
            basis=new_commit_id,
        ).model_copy(update={"projection_after": "plan"})
    )
    _, fence = commands.claim(plan_projection.task_id, worker_id="projection-worker")
    commands.mark_started(fence)

    updated_draft = _task(
        task_id="run.claimed.draft.4",
        run_id="run.claimed",
        kind=TaskKind.DRAFT_CANDIDATE,
        status=TaskStatus.READY,
        basis=new_commit_id,
        dependency=plan_projection.task_id,
    ).model_copy(update={"chapter_index": 4})

    with pytest.raises(RuntimeCommandConflictError, match="successor task identity collision"):
        commands.settle_attempt(
            fence,
            outcome=AttemptOutcome.SUCCEEDED,
            terminal_status=TaskStatus.SUCCEEDED,
            successor_tasks=(updated_draft,),
        )
