"""Branch coverage for CreativeRuntimeService recovery and helper paths."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from novel_agent.domain.agent_context import LoopRoundProgress, LoopRoundProgressKind
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.changes import CommitResult, CommitStatus
from novel_agent.domain.creative_runtime import (
    AcceptanceCommand,
    AcceptanceDecision,
    ActorKind,
    AutomationMode,
    CandidateBinding,
    CandidateKind,
    CreativeRunPolicy,
    CreativeRunTerminal,
    PlanningLoopResult,
    PlanningTerminalStatus,
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
from novel_agent.domain.memory_write import MemoryWriteWorkflowPhase, MemoryWriteWorkflowStatus
from novel_agent.domain.runtime import (
    AttemptFence,
    EffectReceipt,
    EffectStatus,
    TaskAttempt,
    TaskKind,
    TaskPurpose,
    TaskRecord,
    TaskStatus,
)
from novel_agent.domain.writing_loop import WritingLoopTerminalStatus
from novel_agent.ports.creative_runtime import CandidateMaterializationError
from novel_agent.services.creative_runtime import CreativeRuntimeService

HASH = "sha256:" + "1" * 64
COMMIT = CommitId("sha256:" + "a" * 64)
NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _ref(digest: str = "a") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + digest * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )


def _task(**updates: object) -> TaskRecord:
    values: dict[str, object] = {
        "task_id": TaskId("task.recovery"),
        "run_id": RunId("run.recovery"),
        "project_id": ProjectId("project.recovery"),
        "kind": TaskKind.PLAN_CANDIDATE,
        "task_revision": 0,
        "status": TaskStatus.READY,
        "basis_commit": COMMIT,
        "policy_hash": HASH,
        "permission_hash": HASH,
    }
    values.update(updates)
    return TaskRecord.model_validate(values)


def _service(**overrides: object) -> CreativeRuntimeService:
    if "commits" not in overrides:
        commits = Mock()
        commits.current_commit.return_value = COMMIT
        overrides["commits"] = commits
    kwargs = {
        "commands": Mock(),
        "acceptance": Mock(),
        "commits": overrides["commits"],
        "artifacts": Mock(),
        "planner": Mock(),
        "writer": Mock(),
        "writing_request_factory": Mock(),
        "plan_materializer": Mock(),
        "draft_materializer": Mock(),
        "projection": Mock(),
        "snapshots": Mock(),
        "policy_resolver": Mock(),
    }
    kwargs.update(overrides)
    return CreativeRuntimeService(**kwargs)  # type: ignore[arg-type]


def test_recover_boundary_returns_none_for_unrelated_tasks() -> None:
    commands = Mock()
    commands.get_task.return_value = _task()
    service = _service(commands=commands)
    assert service.recover_boundary(TaskId("task.recovery")) is None


def test_recover_boundary_auto_accepts_waiting_plan_acceptance() -> None:
    commands = Mock()
    waiting = _task(kind=TaskKind.PLAN_ACCEPTANCE, status=TaskStatus.WAITING_INPUT)
    commands.get_task.return_value = waiting
    service = _service(commands=commands)
    candidate = CandidateBinding(
        candidate_id=StableId("candidate.plan"),
        kind=CandidateKind.PLAN,
        artifact_ref=_ref(),
        candidate_hash=_ref().artifact_id.root,
        basis_commit=COMMIT,
    )
    cast(Any, service)._candidate_for_task = Mock(return_value=candidate)
    expected = Mock()
    cast(Any, service)._auto_accept = Mock(return_value=expected)
    assert service.recover_boundary(waiting.task_id) is expected
    cast(Any, service)._auto_accept.assert_called_once_with(waiting, candidate)


def test_recover_boundary_repairs_post_draft_projection() -> None:
    commands = Mock()
    projection = _task(
        kind=TaskKind.PROJECTION_FRESHNESS,
        status=TaskStatus.SUCCEEDED,
        projection_after="draft",
    )
    commands.get_task.return_value = projection
    service = _service(commands=commands)
    expected = Mock()
    cast(Any, service)._repair_post_draft_projection = Mock(return_value=expected)
    assert service.recover_boundary(projection.task_id) is expected


def test_recover_boundary_auto_extends_planner_budget_review() -> None:
    commands = Mock()
    waiting = _task(status=TaskStatus.BUDGET_REVIEW, failure_budget=2)
    ready = _task(status=TaskStatus.READY, failure_budget=2, planner_memory_budget_extensions=1)
    commands.get_task.return_value = waiting
    commands.extend_budget.return_value = ready
    service = _service(commands=commands)
    cast(Any, service)._policy_resolver.return_value = CreativeRunPolicy(
        automation_mode=AutomationMode.AUTO,
        policy_hash=HASH,
        permission_hash=HASH,
        auto_accept_plan=True,
        auto_accept_draft=True,
    )
    result = service.recover_boundary(waiting.task_id)
    assert result is not None
    assert result.reason_code == "budget_auto_extended"
    assert result.terminal is CreativeRunTerminal.PROGRESSED
    commands.extend_budget.assert_called_once()
    kwargs = commands.extend_budget.call_args.kwargs
    assert kwargs["additional_planner_memory_tranches"] == 1
    assert kwargs["additional_attempts"] == 0


def test_auto_extend_budget_skips_manual_and_exhausted_tranches() -> None:
    waiting = _task(status=TaskStatus.BUDGET_REVIEW)
    commands = Mock()
    commands.get_task.return_value = waiting
    service = _service(commands=commands)
    cast(Any, service)._policy_resolver.return_value = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=HASH,
    )
    assert service.recover_boundary(waiting.task_id) is None
    commands.extend_budget.assert_not_called()

    capped = _task(status=TaskStatus.BUDGET_REVIEW, planner_memory_budget_extensions=3)
    commands.get_task.return_value = capped
    cast(Any, service)._policy_resolver.return_value = CreativeRunPolicy(
        automation_mode=AutomationMode.AUTO,
        policy_hash=HASH,
        permission_hash=HASH,
        auto_accept_plan=True,
        auto_accept_draft=True,
    )
    assert service.recover_boundary(capped.task_id) is None
    commands.extend_budget.assert_not_called()


def test_recover_chapter_settlement_missing_commit_id_fails_closed() -> None:
    commands = Mock()
    task = _task(
        kind=TaskKind.DRAFT_COMMIT,
        status=TaskStatus.RUNNING,
        current_attempt_id=StableId("attempt.1"),
    )
    commands.get_task.return_value = task
    commands.effect_for_current_attempt.return_value = None
    settlement = Mock()
    settlement.resolve_commit.return_value = CommitResult.model_construct(
        request_id=StableId("commit.missing"),
        status=CommitStatus.ACCEPTED,
        commit_id=None,
    )
    service = _service(commands=commands, chapter_settlement=settlement)
    cast(Any, service)._accepted_binding = Mock(return_value=Mock())
    with pytest.raises(RuntimeError, match="has no commit"):
        service.recover_boundary(task.task_id)


def test_recover_chapter_settlement_marks_missing_outer_effect() -> None:
    commands = Mock()
    task = _task(
        kind=TaskKind.DRAFT_COMMIT,
        status=TaskStatus.RUNNING,
        current_attempt_id=StableId("attempt.1"),
    )
    pending = task.model_copy(update={"status": TaskStatus.RECOVERY_PENDING})
    commands.get_task.return_value = task
    commands.effect_for_current_attempt.return_value = None
    commands.mark_recovery_pending.return_value = pending
    settlement = Mock()
    settlement.resolve_commit.return_value = CommitResult.model_construct(
        request_id=StableId("commit.accepted"),
        status=CommitStatus.ACCEPTED,
        commit_id=COMMIT,
    )
    service = _service(commands=commands, chapter_settlement=settlement)
    cast(Any, service)._accepted_binding = Mock(return_value=Mock())
    result = service.recover_boundary(task.task_id)
    assert result is not None
    assert result.terminal is CreativeRunTerminal.RECOVERY_PENDING
    assert result.reason_code == "chapter_settlement_effect_missing"


def test_recover_chapter_settlement_bounds_max_length_task_command() -> None:
    commands = Mock()
    task = _task(
        task_id=TaskId("t" * 128),
        run_id=RunId("run.recovery-long"),
        kind=TaskKind.DRAFT_COMMIT,
        status=TaskStatus.RUNNING,
        current_attempt_id=StableId("attempt.1"),
    )
    pending = task.model_copy(update={"status": TaskStatus.RECOVERY_PENDING})
    commands.get_task.return_value = task
    commands.effect_for_current_attempt.return_value = None
    commands.mark_recovery_pending.return_value = pending
    settlement = Mock()
    settlement.resolve_commit.return_value = CommitResult.model_construct(
        request_id=StableId("commit.accepted"),
        status=CommitStatus.ACCEPTED,
        commit_id=COMMIT,
    )
    service = _service(commands=commands, chapter_settlement=settlement)
    cast(Any, service)._accepted_binding = Mock(return_value=Mock())

    result = service.recover_boundary(task.task_id)

    assert result is not None
    command_id = commands.mark_recovery_pending.call_args.kwargs["command_id"]
    assert command_id.root == "recovery-pending.run.recovery-long.0"
    assert len(command_id.root) <= 128


def test_recover_chapter_settlement_reconciles_accepted_commit() -> None:
    commands = Mock()
    task = _task(
        kind=TaskKind.DRAFT_COMMIT,
        status=TaskStatus.RUNNING,
        current_attempt_id=StableId("attempt.1"),
    )
    prior = EffectReceipt(
        effect_identity=StableId("effect.settlement"),
        external_system="stage2w.chapter_reveal_atomic",
        request_identity=StableId("effect.settlement"),
        status=EffectStatus.REQUESTED,
        attempt_no=1,
    )
    projection = _task(kind=TaskKind.PROJECTION_FRESHNESS, status=TaskStatus.READY)
    commands.get_task.return_value = task
    commands.effect_for_current_attempt.return_value = prior
    artifacts = Mock()
    artifacts.put.return_value = _ref("b")
    settlement = Mock()
    settlement.resolve_commit.return_value = CommitResult.model_construct(
        request_id=StableId("commit.accepted"),
        status=CommitStatus.ACCEPTED,
        commit_id=COMMIT,
    )
    service = _service(commands=commands, artifacts=artifacts, chapter_settlement=settlement)
    cast(Any, service)._accepted_binding = Mock(return_value=Mock())
    cast(Any, service)._projection_task = Mock(return_value=projection)
    result = service.recover_boundary(task.task_id)
    assert result is not None
    assert result.terminal is CreativeRunTerminal.PROGRESSED
    assert result.reason_code == "chapter_settlement_reconciled"
    commands.reconcile_external_commit.assert_called_once()


def test_recover_chapter_settlement_compensates_requested_then_retries() -> None:
    commands = Mock()
    task = _task(
        kind=TaskKind.DRAFT_COMMIT,
        status=TaskStatus.RUNNING,
        current_attempt_id=StableId("attempt.1"),
    )
    prior = EffectReceipt(
        effect_identity=StableId("effect.settlement"),
        external_system="stage2w.chapter_reveal_atomic",
        request_identity=StableId("effect.settlement"),
        status=EffectStatus.REQUESTED,
        attempt_no=1,
    )
    retrying = task.model_copy(update={"status": TaskStatus.WAITING_RETRY})
    commands.get_task.return_value = task
    commands.effect_for_current_attempt.return_value = prior
    commands.operator_reconcile_attempt.return_value = retrying
    settlement = Mock()
    settlement.resolve_commit.return_value = None
    service = _service(commands=commands, chapter_settlement=settlement)
    cast(Any, service)._accepted_binding = Mock(return_value=Mock())
    result = service.recover_boundary(task.task_id)
    assert result is not None
    assert result.terminal is CreativeRunTerminal.WAITING_RETRY
    assert result.reason_code == "chapter_settlement_safe_to_retry"
    commands.reconcile_effect.assert_called_once()


def test_recover_chapter_settlement_completed_without_receipt_is_pending() -> None:
    commands = Mock()
    task = _task(
        kind=TaskKind.DRAFT_COMMIT,
        status=TaskStatus.RUNNING,
        current_attempt_id=StableId("attempt.1"),
    )
    prior = EffectReceipt(
        effect_identity=StableId("effect.settlement"),
        external_system="stage2w.chapter_reveal_atomic",
        request_identity=StableId("effect.settlement"),
        status=EffectStatus.COMPLETED,
        attempt_no=1,
        completed_at=NOW,
    )
    pending = task.model_copy(update={"status": TaskStatus.RECOVERY_PENDING})
    commands.get_task.return_value = task
    commands.effect_for_current_attempt.return_value = prior
    commands.mark_recovery_pending.return_value = pending
    settlement = Mock()
    settlement.resolve_commit.return_value = None
    service = _service(commands=commands, chapter_settlement=settlement)
    cast(Any, service)._accepted_binding = Mock(return_value=Mock())
    result = service.recover_boundary(task.task_id)
    assert result is not None
    assert result.terminal is CreativeRunTerminal.RECOVERY_PENDING
    assert result.reason_code == "chapter_settlement_receipt_inconsistent"


def test_recover_chapter_settlement_rejected_receipt_blocks() -> None:
    commands = Mock()
    task = _task(
        kind=TaskKind.DRAFT_COMMIT,
        status=TaskStatus.RUNNING,
        current_attempt_id=StableId("attempt.1"),
    )
    blocked = task.model_copy(update={"status": TaskStatus.BLOCKED})
    commands.get_task.return_value = task
    commands.effect_for_current_attempt.return_value = None
    commands.operator_reconcile_attempt.return_value = blocked
    settlement = Mock()
    settlement.resolve_commit.return_value = CommitResult(
        request_id=StableId("commit.rejected"),
        status=CommitStatus.REJECTED,
        reason="conflict",
    )
    service = _service(commands=commands, chapter_settlement=settlement)
    cast(Any, service)._accepted_binding = Mock(return_value=Mock())
    result = service.recover_boundary(task.task_id)
    assert result is not None
    assert result.terminal is CreativeRunTerminal.BLOCKED
    assert result.reason_code == "chapter_settlement_receipt_rejected"


def test_settlement_refs_and_budget_review_override() -> None:
    service = _service()
    refs = service._settlement_refs(
        SimpleNamespace(
            validation_receipt=_ref("c"),
            guardian_receipt="ignored",
            commit_receipt=_ref("d"),
            projection_receipt_ref=None,
            freshness_receipt_ref=_ref("e"),
            checkpoint_ref=_ref("f"),
            terminal_result_ref=_ref("b"),
            quarantine_refs=(_ref("1"), "ignored"),
        )
    )
    assert len(refs) == 6
    budget = _task(status=TaskStatus.BUDGET_REVIEW)
    result = service._result(budget, CreativeRunTerminal.PROGRESSED, "other")
    assert result.terminal is CreativeRunTerminal.BUDGET_REVIEW
    assert result.reason_code == "task_retry_budget_exhausted"


def test_creative_successor_ids_bound_max_length_task_identity() -> None:
    artifacts = Mock()
    artifacts.put.return_value = _ref("b")
    service = _service(artifacts=artifacts)
    previous = _task(
        task_id=TaskId("t" * 128),
        run_id=RunId("run.recovery-long"),
        kind=TaskKind.PLAN_CANDIDATE,
    )
    candidate_ref = _ref("c")
    candidate = CandidateBinding(
        candidate_id=StableId("candidate.recovery-long"),
        kind=CandidateKind.PLAN,
        artifact_ref=candidate_ref,
        candidate_hash=candidate_ref.artifact_id.root,
        basis_commit=COMMIT,
    )

    acceptance = service._acceptance_task(previous, candidate)
    projection = service._projection_task(
        previous.model_copy(update={"kind": TaskKind.PLAN_COMMIT}),
        COMMIT,
    )

    assert acceptance.task_id.root == f"accept.{candidate.candidate_hash}"
    assert projection.task_id.root == f"projection.{COMMIT.root}"
    assert len(acceptance.task_id.root) <= 128
    assert len(projection.task_id.root) <= 128


def test_submit_acceptance_rejects_unpromoted_lookahead() -> None:
    service = _service()
    candidate = CandidateBinding(
        candidate_id=StableId("candidate.lookahead"),
        kind=CandidateKind.PLAN,
        artifact_ref=_ref(),
        candidate_hash=_ref().artifact_id.root,
        basis_commit=COMMIT,
        planning_purpose=TaskPurpose.LOOKAHEAD,
        horizon_start=21,
        horizon_end=25,
        protected_chapter_index=20,
    )
    command = AcceptanceCommand(
        command_id=StableId("accept.lookahead"),
        project_id=ProjectId("project.recovery"),
        run_id=RunId("run.recovery"),
        task_id=TaskId("task.recovery"),
        candidate=candidate,
        acceptance_policy_hash=HASH,
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        decision=AcceptanceDecision.ACCEPT,
        reason="too soon",
        expected_project_commit=COMMIT,
        idempotency_identity=StableId("accept.lookahead.identity"),
        issued_at=NOW,
    )
    with pytest.raises(ValueError, match="lookahead candidate must pass"):
        service.submit_acceptance(
            command,
            policy=CreativeRunPolicy(
                automation_mode=AutomationMode.MANUAL,
                policy_hash=HASH,
                permission_hash=HASH,
            ),
        )


def test_await_with_heartbeat_renews_lease_until_done() -> None:
    commands = Mock()
    commands.heartbeat_interval_seconds = 0.01
    service = _service(commands=commands)

    async def slow() -> str:
        await asyncio.sleep(0.03)
        return "done"

    fence = cast(object, Mock())
    assert asyncio.run(cast(Any, service)._await_with_heartbeat(fence, slow())) == "done"
    assert commands.heartbeat.called


def _lookahead_policy() -> CreativeRunPolicy:
    return CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=HASH,
        enable_planner_lookahead=True,
        runtime_parallelism=2,
    )


def test_repair_post_draft_projection_skips_when_lookahead_disabled() -> None:
    service = _service()
    cast(Any, service)._policy_resolver.return_value = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=HASH,
    )
    projection = _task(
        kind=TaskKind.PROJECTION_FRESHNESS,
        status=TaskStatus.SUCCEEDED,
        chapter_index=1,
        target_chapters=5,
        projection_after="draft",
    )
    assert service._repair_post_draft_projection(projection) is None


def test_repair_post_draft_projection_returns_revalidated_result() -> None:
    service = _service()
    cast(Any, service)._policy_resolver.return_value = _lookahead_policy()
    cast(Any, service)._task_reader = Mock()
    expected = Mock()
    cast(Any, service)._revalidate_lookahead = Mock(return_value=expected)
    projection = _task(
        kind=TaskKind.PROJECTION_FRESHNESS,
        status=TaskStatus.SUCCEEDED,
        chapter_index=1,
        target_chapters=5,
        projection_after="draft",
    )
    assert service._repair_post_draft_projection(projection) is expected


def test_repair_post_draft_projection_falls_back_to_rolling_plan() -> None:
    from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite

    commands = Mock()
    snapshots = Mock()
    snapshots.get_for_commit.return_value = DerivedSnapshotLite(
        snapshot_id=StableId("snapshot.exact"),
        source_commit=COMMIT,
        anchor_build_id=StableId("anchor.exact"),
        anchor_index_version="anchor-v1",
        grounded_index_version="grounded-v1",
        embedding_profile="offline-v1",
        fusion_profile="rrf-v1",
        build_status=DerivedBuildStatus.EXACT,
        published_at=NOW,
    )
    projection = _task(
        kind=TaskKind.PROJECTION_FRESHNESS,
        status=TaskStatus.SUCCEEDED,
        chapter_index=1,
        target_chapters=5,
        projection_after="draft",
        input_artifact_refs=(_ref("2"),),
    )
    stale_lookahead = _task(
        task_id=TaskId("task.lookahead.stale"),
        kind=TaskKind.PLAN_CANDIDATE,
        purpose=TaskPurpose.LOOKAHEAD,
        status=TaskStatus.WAITING_INPUT,
        protected_chapter_index=1,
        horizon_start=2,
        horizon_end=4,
        chapter_index=1,
        target_chapters=5,
    )
    owner = _task(
        task_id=TaskId("task.plan.owner"),
        kind=TaskKind.PLAN_CANDIDATE,
        purpose=TaskPurpose.NORMAL,
        status=TaskStatus.SUCCEEDED,
        chapter_index=0,
        input_artifact_refs=(_ref("2"),),
    )
    reader = Mock()
    reader.list_run.return_value = (owner, projection, stale_lookahead)
    service = _service(commands=commands, snapshots=snapshots, task_reader=reader)
    cast(Any, service)._policy_resolver.return_value = _lookahead_policy()
    cast(Any, service)._revalidate_lookahead = Mock(return_value=None)
    result = service._repair_post_draft_projection(projection)
    assert result is not None
    assert result.terminal is CreativeRunTerminal.PROGRESSED
    assert result.reason_code == "lookahead_fallback_to_rolling_plan"
    commands.supersede_task.assert_called()
    commands.create_task.assert_called()


def test_repair_post_draft_projection_waits_for_live_ready_lookahead() -> None:
    projection = _task(
        kind=TaskKind.PROJECTION_FRESHNESS,
        status=TaskStatus.SUCCEEDED,
        chapter_index=1,
        target_chapters=5,
        projection_after="draft",
    )
    live_lookahead = _task(
        task_id=TaskId("task.lookahead.live"),
        kind=TaskKind.PLAN_CANDIDATE,
        purpose=TaskPurpose.LOOKAHEAD,
        status=TaskStatus.READY,
        protected_chapter_index=1,
        basis_commit=COMMIT,
        horizon_start=2,
        horizon_end=4,
        chapter_index=1,
        target_chapters=5,
    )
    reader = Mock()
    reader.list_run.return_value = (projection, live_lookahead)
    commands = Mock()
    service = _service(commands=commands, task_reader=reader)
    cast(Any, service)._policy_resolver.return_value = _lookahead_policy()
    cast(Any, service)._revalidate_lookahead = Mock(return_value=None)
    assert service._repair_post_draft_projection(projection) is None
    commands.supersede_task.assert_not_called()
    commands.create_task.assert_not_called()


def test_repair_post_draft_projection_falls_back_when_ready_lookahead_is_stale() -> None:
    from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite

    commands = Mock()
    snapshots = Mock()
    snapshots.get_for_commit.return_value = DerivedSnapshotLite(
        snapshot_id=StableId("snapshot.exact"),
        source_commit=COMMIT,
        anchor_build_id=StableId("anchor.exact"),
        anchor_index_version="anchor-v1",
        grounded_index_version="grounded-v1",
        embedding_profile="offline-v1",
        fusion_profile="rrf-v1",
        build_status=DerivedBuildStatus.EXACT,
        published_at=NOW,
    )
    projection = _task(
        kind=TaskKind.PROJECTION_FRESHNESS,
        status=TaskStatus.SUCCEEDED,
        chapter_index=3,
        target_chapters=800,
        projection_after="draft",
        input_artifact_refs=(_ref("2"),),
    )
    stale_lookahead = _task(
        task_id=TaskId("task.lookahead.stale-ready"),
        kind=TaskKind.PLAN_CANDIDATE,
        purpose=TaskPurpose.LOOKAHEAD,
        status=TaskStatus.READY,
        protected_chapter_index=3,
        basis_commit=CommitId("sha256:" + "b" * 64),
        horizon_start=4,
        horizon_end=8,
        chapter_index=3,
        target_chapters=800,
    )
    owner = _task(
        task_id=TaskId("task.plan.owner"),
        kind=TaskKind.PLAN_CANDIDATE,
        purpose=TaskPurpose.NORMAL,
        status=TaskStatus.SUCCEEDED,
        chapter_index=2,
        input_artifact_refs=(_ref("2"),),
    )
    reader = Mock()
    reader.list_run.return_value = (owner, projection, stale_lookahead)
    service = _service(commands=commands, snapshots=snapshots, task_reader=reader)
    cast(Any, service)._policy_resolver.return_value = _lookahead_policy()
    cast(Any, service)._revalidate_lookahead = Mock(return_value=None)
    result = service._repair_post_draft_projection(projection)
    assert result is not None
    assert result.reason_code == "lookahead_fallback_to_rolling_plan"
    commands.supersede_task.assert_called_once()
    commands.create_task.assert_called_once()


def test_repair_post_draft_projection_waits_for_running_lookahead() -> None:
    projection = _task(
        kind=TaskKind.PROJECTION_FRESHNESS,
        status=TaskStatus.SUCCEEDED,
        chapter_index=1,
        target_chapters=5,
        projection_after="draft",
    )
    running = _task(
        task_id=TaskId("task.lookahead.running"),
        kind=TaskKind.PLAN_CANDIDATE,
        purpose=TaskPurpose.LOOKAHEAD,
        status=TaskStatus.RUNNING,
        protected_chapter_index=1,
        basis_commit=CommitId("sha256:" + "b" * 64),
        horizon_start=2,
        horizon_end=4,
        chapter_index=1,
        target_chapters=5,
    )
    reader = Mock()
    reader.list_run.return_value = (projection, running)
    commands = Mock()
    service = _service(commands=commands, task_reader=reader)
    cast(Any, service)._policy_resolver.return_value = _lookahead_policy()
    cast(Any, service)._revalidate_lookahead = Mock(return_value=None)
    assert service._repair_post_draft_projection(projection) is None
    commands.supersede_task.assert_not_called()


def test_revalidate_lookahead_promotes_when_draft_has_no_plan_impact() -> None:
    from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite

    waiting = _task(
        task_id=TaskId("task.lookahead.accept"),
        kind=TaskKind.PLAN_ACCEPTANCE,
        purpose=TaskPurpose.LOOKAHEAD,
        status=TaskStatus.WAITING_INPUT,
        protected_chapter_index=1,
        horizon_start=2,
        horizon_end=4,
        chapter_index=1,
        target_chapters=5,
        candidate_binding_ref=_ref("3"),
    )
    later_commit = CommitId("sha256:" + "b" * 64)
    projection = _task(
        kind=TaskKind.PROJECTION_FRESHNESS,
        status=TaskStatus.SUCCEEDED,
        projection_after="draft",
        chapter_index=1,
        target_chapters=5,
        affects_future_plan=False,
        basis_commit=later_commit,
    )
    commits = Mock()
    commits.current_commit.return_value = later_commit
    snapshots = Mock()
    snapshots.get_for_commit.return_value = DerivedSnapshotLite(
        snapshot_id=StableId("snapshot.later"),
        source_commit=later_commit,
        anchor_build_id=StableId("anchor.later"),
        anchor_index_version="anchor-v1",
        grounded_index_version="grounded-v1",
        embedding_profile="offline-v1",
        fusion_profile="rrf-v1",
        build_status=DerivedBuildStatus.EXACT,
        published_at=NOW,
    )
    artifacts = Mock()
    artifacts.put.return_value = _ref("4")
    reader = Mock()
    reader.list_run.return_value = (waiting, projection)
    commands = Mock()
    service = _service(
        commands=commands,
        commits=commits,
        snapshots=snapshots,
        artifacts=artifacts,
        task_reader=reader,
    )
    candidate = CandidateBinding(
        candidate_id=StableId("candidate.lookahead"),
        kind=CandidateKind.PLAN,
        artifact_ref=_ref("5"),
        candidate_hash=_ref("5").artifact_id.root,
        basis_commit=COMMIT,
        planning_purpose=TaskPurpose.LOOKAHEAD,
        horizon_start=2,
        horizon_end=4,
        protected_chapter_index=1,
    )
    cast(Any, service)._candidate_for_task = Mock(return_value=candidate)
    cast(Any, service)._auto_accept = Mock(return_value=None)
    result = service._revalidate_lookahead(projection)
    assert result is not None
    assert result.reason_code == "lookahead_promoted"
    commands.create_task.assert_called()


def _fence_pair(*, attempt_no: int = 1) -> tuple[TaskAttempt, AttemptFence]:
    fence = AttemptFence(
        project_id=ProjectId("project.recovery"),
        task_id=TaskId("task.recovery"),
        attempt_id=StableId("attempt.1"),
        claim_token=StableId("claim.1"),
        task_revision=1,
        writer_generation=0,
    )
    attempt = TaskAttempt(
        attempt_id=fence.attempt_id,
        task_id=fence.task_id,
        attempt_no=attempt_no,
        worker_id="worker",
        claim_token_digest=HASH,
        fence_generation=1,
        claimed_at=NOW,
        heartbeat_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    return attempt, fence


def _planning_result(
    status: PlanningTerminalStatus,
    *,
    failure_code: str,
) -> PlanningLoopResult:
    return PlanningLoopResult(
        result_id=StableId("planner.result"),
        run_id=RunId("run.recovery"),
        task_id=TaskId("task.recovery"),
        status=status,
        failure_code=failure_code,
        failure_detail="injected planner terminal",
    )


def test_legal_commands_cover_waiting_retry_blocked_and_lookahead() -> None:
    service = _service()
    lookahead = _task(
        kind=TaskKind.PLAN_ACCEPTANCE,
        status=TaskStatus.WAITING_INPUT,
        purpose=TaskPurpose.LOOKAHEAD,
        protected_chapter_index=1,
        horizon_start=2,
        horizon_end=4,
    )
    assert service._legal_commands(lookahead) == ("wait_for_revalidation", "cancel")
    assert service._legal_commands(_task(status=TaskStatus.WAITING_RETRY)) == ("retry", "cancel")
    assert service._legal_commands(_task(status=TaskStatus.BLOCKED)) == ("unblock", "cancel")
    assert service._legal_commands(_task(status=TaskStatus.READY)) == ("advance", "pause", "cancel")
    assert service._legal_commands(_task(status=TaskStatus.RUNNING)) == ()


def test_planning_inputs_require_reader_and_normal_owner() -> None:
    service = _service()
    with pytest.raises(RuntimeError, match="runtime task reader"):
        service._planning_inputs(_task())
    reader = Mock()
    reader.list_run.return_value = ()
    service = _service(task_reader=reader)
    with pytest.raises(RuntimeError, match="no normal Planner input owner"):
        service._planning_inputs(_task())


def test_advance_planner_no_progress_and_yield_and_waiting_input() -> None:
    attempt, fence = _fence_pair(attempt_no=2)
    commands = Mock()
    commands.heartbeat_interval_seconds = 60.0
    commands.claim.return_value = (attempt, fence)
    settled = _task(status=TaskStatus.BUDGET_REVIEW)
    commands.settle_attempt.return_value = settled
    planner = Mock()
    planner.run = AsyncMock(
        return_value=_planning_result(
            PlanningTerminalStatus.SUSPENDED,
            failure_code="PLAN_REVISION_NO_PROGRESS",
        )
    )
    service = _service(commands=commands, planner=planner)
    commands.get_task.return_value = _task()
    result = asyncio.run(service.advance(TaskId("task.recovery"), worker_id="worker"))
    assert result.terminal is CreativeRunTerminal.BUDGET_REVIEW
    assert result.reason_code == "PLAN_REVISION_NO_PROGRESS"

    planner.run = AsyncMock(
        return_value=_planning_result(
            PlanningTerminalStatus.YIELDED,
            failure_code="PLANNER_MEMORY_SLICE_EXHAUSTED",
        )
    )
    ready = _task(status=TaskStatus.READY)
    commands.settle_attempt.return_value = ready
    sliced = asyncio.run(service.advance(TaskId("task.recovery"), worker_id="worker"))
    assert sliced.terminal is CreativeRunTerminal.PROGRESSED
    assert sliced.reason_code == "PLANNER_MEMORY_SLICE_EXHAUSTED"

    planner.run = AsyncMock(
        return_value=_planning_result(
            PlanningTerminalStatus.WAITING_INPUT,
            failure_code="planner_waiting_input",
        )
    )
    waiting = _task(status=TaskStatus.WAITING_INPUT)
    commands.settle_attempt.return_value = waiting
    paused = asyncio.run(service.advance(TaskId("task.recovery"), worker_id="worker"))
    assert paused.terminal is CreativeRunTerminal.WAITING_PLAN_ACCEPTANCE


def test_advance_writer_poison_loop_stops_for_no_progress() -> None:
    attempt, fence = _fence_pair(attempt_no=2)
    commands = Mock()
    commands.heartbeat_interval_seconds = 60.0
    commands.claim.return_value = (attempt, fence)
    commands.settle_attempt.return_value = _task(
        kind=TaskKind.DRAFT_CANDIDATE, status=TaskStatus.BUDGET_REVIEW
    )
    writer = Mock()
    writer.run = AsyncMock(
        return_value=SimpleNamespace(
            status=WritingLoopTerminalStatus.WRITER_FAILED,
            artifacts=(),
            round_progress=LoopRoundProgress(kind=LoopRoundProgressKind.NO_PROGRESS),
        )
    )
    factory = Mock()
    factory.return_value = SimpleNamespace(
        run_id=RunId("run.recovery"),
        task_id=TaskId("task.recovery"),
        base_commit=COMMIT,
        snapshot_id=None,
    )
    service = _service(
        commands=commands,
        writer=writer,
        writing_request_factory=factory,
    )
    commands.get_task.return_value = _task(kind=TaskKind.DRAFT_CANDIDATE)
    result = asyncio.run(service.advance(TaskId("task.recovery"), worker_id="writer"))
    assert result.terminal is CreativeRunTerminal.BUDGET_REVIEW
    assert result.reason_code == "writer_failed"


def test_advance_binds_writer_request_to_claimed_attempt() -> None:
    attempt, fence = _fence_pair()
    commands = Mock()
    commands.heartbeat_interval_seconds = 60.0
    commands.claim.return_value = (attempt, fence)
    commands.settle_attempt.return_value = _task(
        kind=TaskKind.DRAFT_CANDIDATE,
        status=TaskStatus.WAITING_RETRY,
    )
    writer = Mock()
    writer.run = AsyncMock(
        return_value=SimpleNamespace(
            status=WritingLoopTerminalStatus.WRITER_FAILED,
            artifacts=(),
            round_progress=None,
        )
    )
    request_factory = Mock(
        return_value=SimpleNamespace(
            run_id=RunId("run.recovery"),
            task_id=TaskId("task.recovery"),
            base_commit=COMMIT,
            snapshot_id=None,
        )
    )
    service = _service(
        commands=commands,
        writer=writer,
        writing_request_factory=request_factory,
    )
    commands.get_task.return_value = _task(kind=TaskKind.DRAFT_CANDIDATE)

    result = asyncio.run(service.advance(TaskId("task.recovery"), worker_id="writer"))

    assert result.terminal is CreativeRunTerminal.WAITING_RETRY
    claimed_task = request_factory.call_args.args[0]
    assert claimed_task.current_attempt_id == attempt.attempt_id
    assert claimed_task.task_revision == fence.task_revision
    assert claimed_task.status is TaskStatus.RUNNING


def test_advance_chapter_settlement_rejects_and_retries() -> None:
    attempt, fence = _fence_pair()
    commands = Mock()
    commands.heartbeat_interval_seconds = 60.0
    commands.claim.return_value = (attempt, fence)
    commands.claim_writer_lane.return_value = fence
    commands.settle_attempt.return_value = _task(
        kind=TaskKind.DRAFT_COMMIT, status=TaskStatus.BLOCKED
    )
    settlement = Mock()
    settlement.effect_identity.return_value = StableId("settlement.identity")
    settlement.settle = AsyncMock(side_effect=ValueError("invalid chapter"))
    service = _service(commands=commands, chapter_settlement=settlement)
    commands.get_task.return_value = _task(kind=TaskKind.DRAFT_COMMIT)
    cast(Any, service)._accepted_binding = Mock(return_value=Mock())
    rejected = asyncio.run(service.advance(TaskId("task.recovery"), worker_id="commit"))
    assert rejected.reason_code == "chapter_settlement_rejected"
    commands.record_effect_terminal.assert_called()

    from novel_agent.domain.memory_write import MemoryWriteWorkflowResult

    suspended = MemoryWriteWorkflowResult.model_construct(
        request_id=StableId("mw.suspended"),
        status=MemoryWriteWorkflowStatus.SUSPENDED,
        workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        canonical_commit_accepted=False,
        base_commit=COMMIT,
        checkpoint_ref=_ref("c"),
    )
    settlement.settle = AsyncMock(return_value=suspended)
    commands.settle_attempt.return_value = _task(
        kind=TaskKind.DRAFT_COMMIT, status=TaskStatus.WAITING_RETRY
    )
    retryable = asyncio.run(service.advance(TaskId("task.recovery"), worker_id="commit"))
    assert retryable.terminal is CreativeRunTerminal.WAITING_RETRY

    fatal = MemoryWriteWorkflowResult.model_construct(
        request_id=StableId("mw.fatal"),
        status=MemoryWriteWorkflowStatus.FATAL,
        workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        canonical_commit_accepted=False,
        base_commit=COMMIT,
        checkpoint_ref=_ref("c"),
    )
    settlement.settle = AsyncMock(return_value=fatal)
    commands.settle_attempt.return_value = _task(
        kind=TaskKind.DRAFT_COMMIT, status=TaskStatus.BLOCKED
    )
    blocked = asyncio.run(service.advance(TaskId("task.recovery"), worker_id="commit"))
    assert blocked.terminal is CreativeRunTerminal.REVIEW_REQUIRED


def test_advance_chapter_settlement_bounds_max_length_effect_identity() -> None:
    attempt, fence = _fence_pair()
    commands = Mock()
    commands.heartbeat_interval_seconds = 60.0
    commands.claim.return_value = (attempt, fence)
    commands.claim_writer_lane.return_value = fence
    commands.settle_attempt.return_value = _task(
        kind=TaskKind.DRAFT_COMMIT, status=TaskStatus.BLOCKED
    )
    settlement = Mock()
    settlement.effect_identity.return_value = StableId("s" * 128)
    settlement.settle = AsyncMock(side_effect=ValueError("invalid chapter"))
    service = _service(commands=commands, chapter_settlement=settlement)
    commands.get_task.return_value = _task(kind=TaskKind.DRAFT_COMMIT)
    candidate_ref = _ref("d")
    accepted = SimpleNamespace(
        candidate=CandidateBinding(
            candidate_id=StableId("candidate.recovery-long"),
            kind=CandidateKind.DRAFT,
            artifact_ref=candidate_ref,
            candidate_hash=candidate_ref.artifact_id.root,
            basis_commit=COMMIT,
        )
    )
    cast(Any, service)._accepted_binding = Mock(return_value=accepted)

    result = asyncio.run(service.advance(TaskId("task.recovery"), worker_id="commit"))

    assert result.reason_code == "chapter_settlement_rejected"
    requested = commands.record_effect_requested.call_args.args[1]
    assert requested.effect_identity.root == (
        f"chapter-settlement.{accepted.candidate.candidate_hash}.attempt.1"
    )
    assert len(requested.effect_identity.root) <= 128


def test_advance_plan_commit_materializer_error_blocks() -> None:
    attempt, fence = _fence_pair()
    commands = Mock()
    commands.heartbeat_interval_seconds = 60.0
    commands.claim.return_value = (attempt, fence)
    commands.claim_writer_lane.return_value = fence
    commands.settle_attempt.return_value = _task(
        kind=TaskKind.PLAN_COMMIT, status=TaskStatus.BLOCKED
    )
    materializer = Mock()
    materializer.materialize.side_effect = CandidateMaterializationError("bad plan")
    service = _service(commands=commands, plan_materializer=materializer)
    commands.get_task.return_value = _task(kind=TaskKind.PLAN_COMMIT)
    cast(Any, service)._accepted_binding = Mock(return_value=Mock())
    result = asyncio.run(service.advance(TaskId("task.recovery"), worker_id="commit"))
    assert result.reason_code == "candidate_materialization_rejected"


def test_advance_freshness_reports_lookahead_pending() -> None:
    from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite

    attempt, fence = _fence_pair()
    commands = Mock()
    commands.heartbeat_interval_seconds = 60.0
    commands.claim.return_value = (attempt, fence)
    commands.settle_attempt.return_value = _task(
        kind=TaskKind.PROJECTION_FRESHNESS,
        status=TaskStatus.SUCCEEDED,
        chapter_index=1,
        target_chapters=5,
        projection_after="draft",
    )
    snapshots = Mock()
    snapshots.get_for_commit.return_value = DerivedSnapshotLite(
        snapshot_id=StableId("snapshot.exact"),
        source_commit=COMMIT,
        anchor_build_id=StableId("anchor.exact"),
        anchor_index_version="anchor-v1",
        grounded_index_version="grounded-v1",
        embedding_profile="offline-v1",
        fusion_profile="rrf-v1",
        build_status=DerivedBuildStatus.EXACT,
        published_at=NOW,
    )
    service = _service(commands=commands, snapshots=snapshots)
    cast(Any, service)._policy_resolver.return_value = _lookahead_policy()
    cast(Any, service)._repair_post_draft_projection = Mock(return_value=None)
    commands.get_task.return_value = _task(
        kind=TaskKind.PROJECTION_FRESHNESS,
        chapter_index=1,
        target_chapters=5,
        projection_after="draft",
    )
    result = asyncio.run(service.advance(TaskId("task.recovery"), worker_id="projection"))
    assert result.reason_code == "lookahead_pending"


def test_revalidate_lookahead_early_exits_and_replan_outcomes() -> None:
    from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite

    later = CommitId("sha256:" + "b" * 64)
    waiting = _task(
        task_id=TaskId("task.lookahead.accept"),
        kind=TaskKind.PLAN_ACCEPTANCE,
        purpose=TaskPurpose.LOOKAHEAD,
        status=TaskStatus.WAITING_INPUT,
        protected_chapter_index=1,
        horizon_start=2,
        horizon_end=4,
        chapter_index=1,
        target_chapters=5,
        dependency_task_ids=(TaskId("task.plan.owner"),),
        candidate_binding_ref=_ref("3"),
    )
    owner = _task(
        task_id=TaskId("task.plan.owner"),
        kind=TaskKind.PLAN_CANDIDATE,
        status=TaskStatus.SUCCEEDED,
        input_artifact_refs=(_ref("2"),),
    )
    projection = _task(
        kind=TaskKind.PROJECTION_FRESHNESS,
        status=TaskStatus.SUCCEEDED,
        projection_after="draft",
        chapter_index=1,
        target_chapters=5,
        affects_future_plan=True,
        basis_commit=later,
    )
    commits = Mock()
    commits.current_commit.return_value = later
    snapshots = Mock()
    snapshots.get_for_commit.return_value = DerivedSnapshotLite(
        snapshot_id=StableId("snapshot.later"),
        source_commit=later,
        anchor_build_id=StableId("anchor.later"),
        anchor_index_version="anchor-v1",
        grounded_index_version="grounded-v1",
        embedding_profile="offline-v1",
        fusion_profile="rrf-v1",
        build_status=DerivedBuildStatus.EXACT,
        published_at=NOW,
    )
    artifacts = Mock()
    artifacts.put.return_value = _ref("4")
    reader = Mock()
    reader.list_run.return_value = (owner, waiting, projection)
    commands = Mock()
    service = _service(
        commands=commands,
        commits=commits,
        snapshots=snapshots,
        artifacts=artifacts,
        task_reader=reader,
    )
    cast(Any, service)._candidate_for_task = Mock(
        return_value=CandidateBinding(
            candidate_id=StableId("candidate.lookahead"),
            kind=CandidateKind.PLAN,
            artifact_ref=_ref("5"),
            candidate_hash=_ref("5").artifact_id.root,
            basis_commit=COMMIT,
            planning_purpose=TaskPurpose.LOOKAHEAD,
            horizon_start=2,
            horizon_end=4,
            protected_chapter_index=1,
        )
    )
    replanned = service._revalidate_lookahead(projection)
    assert replanned is not None
    assert replanned.reason_code == "lookahead_replan_required"

    projection_unknown = projection.model_copy(update={"affects_future_plan": None})
    reader.list_run.return_value = (owner, waiting, projection_unknown)
    superseded = service._revalidate_lookahead(projection_unknown)
    assert superseded is not None
    assert superseded.reason_code == "lookahead_replan_required"

    empty = Mock()
    empty.list_run.return_value = ()
    none_reader = _service(commands=commands, commits=commits, task_reader=empty)
    assert none_reader._revalidate_lookahead(projection) is None

    same_commit = Mock()
    same_commit.current_commit.return_value = COMMIT
    same = _service(
        commands=commands,
        commits=same_commit,
        snapshots=snapshots,
        task_reader=reader,
    )
    reader.list_run.return_value = (waiting,)
    waiting_same = waiting.model_copy(update={"basis_commit": COMMIT})
    reader.list_run.return_value = (waiting_same,)
    assert same._revalidate_lookahead(projection) is None

    reader.list_run.return_value = (waiting,)
    no_projection = _service(
        commands=commands,
        commits=commits,
        snapshots=snapshots,
        task_reader=reader,
    )
    assert no_projection._revalidate_lookahead(waiting) is None

    snapshots.get_for_commit.return_value = None
    reader.list_run.return_value = (waiting, projection)
    missing_snapshot = _service(
        commands=commands,
        commits=commits,
        snapshots=snapshots,
        task_reader=reader,
    )
    assert missing_snapshot._revalidate_lookahead(projection) is None

    snapshots.get_for_commit.return_value = DerivedSnapshotLite(
        snapshot_id=StableId("snapshot.partial"),
        source_commit=later,
        anchor_build_id=StableId("anchor.partial"),
        anchor_index_version="anchor-v1",
        grounded_index_version="grounded-v1",
        embedding_profile="offline-v1",
        fusion_profile="rrf-v1",
        build_status=DerivedBuildStatus.PARTIAL,
        published_at=NOW,
    )
    stale = _service(
        commands=commands,
        commits=commits,
        snapshots=snapshots,
        artifacts=artifacts,
        task_reader=reader,
    )
    assert stale._revalidate_lookahead(projection) is None
