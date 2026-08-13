from __future__ import annotations

import asyncio
from typing import cast

from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
    CreativeRunResult,
    CreativeRunTerminal,
)
from novel_agent.domain.ids import CommitId, ProjectId, RunId, TaskId
from novel_agent.domain.runtime import TaskKind, TaskPurpose, TaskRecord, TaskStatus
from novel_agent.domain.stage5_evaluation import VerticalRunStatus
from novel_agent.ports.creative_runtime import RuntimeTaskReader
from novel_agent.runtime.creative_dispatcher import CreativeDispatcher
from novel_agent.runtime.vertical_runner import VerticalCreativeRunner
from novel_agent.services.creative_runtime import CreativeRuntimeService

HASH = "sha256:" + "1" * 64
BASE = CommitId("sha256:" + "2" * 64)
FINAL = CommitId("sha256:" + "3" * 64)


def _result(terminal: CreativeRunTerminal, commit: CommitId) -> CreativeRunResult:
    return CreativeRunResult(
        run_id=RunId("run.vertical"),
        project_id=ProjectId("project.vertical"),
        terminal=terminal,
        basis_commit=BASE,
        current_commit=commit,
        reason_code=terminal.value.lower(),
    )


def _ready_task(identity: str, *, chapter_index: int = 21) -> TaskRecord:
    return TaskRecord(
        task_id=TaskId(identity),
        run_id=RunId("run.vertical"),
        project_id=ProjectId("project.vertical"),
        kind=TaskKind.DRAFT_CANDIDATE,
        task_revision=1,
        status=TaskStatus.READY,
        basis_commit=BASE,
        policy_hash=HASH,
        permission_hash=HASH,
        chapter_index=chapter_index,
        target_chapters=21,
    )


def _request() -> CreativeRunRequest:
    return CreativeRunRequest(
        run_id=RunId("run.vertical"),
        project_id=ProjectId("project.vertical"),
        basis_commit=BASE,
        current_chapter=20,
        target_chapters=21,
        policy=CreativeRunPolicy(
            automation_mode=AutomationMode.AUTO,
            policy_hash=HASH,
            permission_hash=HASH,
            auto_accept_plan=True,
            auto_accept_draft=True,
        ),
    )


def test_vertical_runner_executes_dispatcher_and_freezes_only_after_completion() -> None:
    class _Runtime:
        def start(self, request: CreativeRunRequest) -> CreativeRunResult:
            assert request.current_chapter == 20
            return _result(CreativeRunTerminal.PROGRESSED, BASE)

    class _Dispatcher:
        def __init__(self) -> None:
            self.calls = 0

        async def run_bounded(self, *, max_tasks: int):
            assert max_tasks == 1
            self.calls += 1
            if self.calls == 1:
                return (_result(CreativeRunTerminal.PROGRESSED, FINAL),)
            if self.calls == 2:
                return (_result(CreativeRunTerminal.COMPLETED, FINAL),)
            raise AssertionError("runner must stop after target completion")

    class _Tasks:
        def __init__(self) -> None:
            self.calls = 0

        def list_run(self, run_id: RunId):
            assert run_id == RunId("run.vertical")
            self.calls += 1
            if self.calls == 1:
                return ()
            if self.calls in {2, 3}:
                return (_ready_task(f"task.vertical.ready.{self.calls}"),)
            return (
                TaskRecord(
                    task_id=TaskId("task.vertical.chapter.21.projection"),
                    run_id=run_id,
                    project_id=ProjectId("project.vertical"),
                    kind=TaskKind.PROJECTION_FRESHNESS,
                    task_revision=1,
                    status=TaskStatus.SUCCEEDED,
                    basis_commit=FINAL,
                    policy_hash=HASH,
                    permission_hash=HASH,
                    chapter_index=21,
                    target_chapters=21,
                    projection_after="draft",
                ),
            )

    request = _request()
    runner = VerticalCreativeRunner(
        runtime=cast(CreativeRuntimeService, _Runtime()),
        dispatcher=cast(CreativeDispatcher, _Dispatcher()),
        tasks=cast(RuntimeTaskReader, _Tasks()),
    )

    report = asyncio.run(runner.run(request, max_tasks=1))

    assert report.status is VerticalRunStatus.COMPLETED
    assert report.outputs_frozen is True
    assert report.completed_chapters == (21,)
    assert report.final_commit == FINAL
    assert report.dispatch_slices == 2


def test_vertical_runner_recognizes_an_already_completed_run() -> None:
    class _Runtime:
        def start(self, request: CreativeRunRequest) -> CreativeRunResult:
            raise AssertionError("an existing run must not be started again")

    class _Dispatcher:
        async def run_bounded(self, *, max_tasks: int):
            assert max_tasks == 1
            return ()

    class _Tasks:
        def list_run(self, run_id: RunId):
            return (
                TaskRecord(
                    task_id=TaskId("task.vertical.chapter.21.projection"),
                    run_id=run_id,
                    project_id=ProjectId("project.vertical"),
                    kind=TaskKind.PROJECTION_FRESHNESS,
                    task_revision=1,
                    status=TaskStatus.SUCCEEDED,
                    basis_commit=FINAL,
                    policy_hash=HASH,
                    permission_hash=HASH,
                    chapter_index=21,
                    target_chapters=21,
                    projection_after="draft",
                ),
            )

    request = CreativeRunRequest(
        run_id=RunId("run.vertical"),
        project_id=ProjectId("project.vertical"),
        basis_commit=BASE,
        current_chapter=20,
        target_chapters=21,
        policy=CreativeRunPolicy(
            automation_mode=AutomationMode.AUTO,
            policy_hash=HASH,
            permission_hash=HASH,
        ),
    )
    report = asyncio.run(
        VerticalCreativeRunner(
            runtime=cast(CreativeRuntimeService, _Runtime()),
            dispatcher=cast(CreativeDispatcher, _Dispatcher()),
            tasks=cast(RuntimeTaskReader, _Tasks()),
        ).run(request, max_tasks=1)
    )

    assert report.status is VerticalRunStatus.COMPLETED
    assert report.outputs_frozen is True
    assert report.dispatch_slices == 0


def test_vertical_runner_yields_only_at_an_explicit_slice_limit() -> None:
    class _Runtime:
        def start(self, request: CreativeRunRequest) -> CreativeRunResult:
            raise AssertionError("an existing run must not be started again")

    class _Dispatcher:
        def __init__(self) -> None:
            self.calls = 0

        async def run_bounded(self, *, max_tasks: int):
            assert max_tasks == 1
            self.calls += 1
            return (_result(CreativeRunTerminal.PROGRESSED, FINAL),)

    class _Tasks:
        def __init__(self) -> None:
            self.calls = 0

        def list_run(self, run_id: RunId):
            self.calls += 1
            return (_ready_task(f"task.vertical.slice.{self.calls}"),)

    dispatcher = _Dispatcher()
    report = asyncio.run(
        VerticalCreativeRunner(
            runtime=cast(CreativeRuntimeService, _Runtime()),
            dispatcher=cast(CreativeDispatcher, dispatcher),
            tasks=cast(RuntimeTaskReader, _Tasks()),
        ).run(_request(), max_tasks=1, max_slices=1)
    )

    assert report.status is VerticalRunStatus.YIELDED
    assert report.outputs_frozen is False
    assert report.dispatch_slices == 1
    assert dispatcher.calls == 1
    assert report.tasks[0].status is TaskStatus.READY


def test_vertical_runner_ignores_cancelled_or_superseded_background_work() -> None:
    class _Runtime:
        def start(self, request: CreativeRunRequest) -> CreativeRunResult:
            raise AssertionError("an existing run must not be started again")

    class _Dispatcher:
        async def run_bounded(self, *, max_tasks: int):
            raise AssertionError("a waiting run has no runnable work")

    class _Tasks:
        def list_run(self, run_id: RunId):
            return (
                TaskRecord(
                    task_id=TaskId("task.vertical.waiting"),
                    run_id=run_id,
                    project_id=ProjectId("project.vertical"),
                    kind=TaskKind.DRAFT_ACCEPTANCE,
                    task_revision=1,
                    status=TaskStatus.WAITING_INPUT,
                    basis_commit=BASE,
                    policy_hash=HASH,
                    permission_hash=HASH,
                    chapter_index=21,
                    target_chapters=21,
                ),
                TaskRecord(
                    task_id=TaskId("task.vertical.cancelled-lookahead"),
                    run_id=run_id,
                    project_id=ProjectId("project.vertical"),
                    kind=TaskKind.PLAN_CANDIDATE,
                    purpose=TaskPurpose.LOOKAHEAD,
                    task_revision=1,
                    status=TaskStatus.CANCELLED,
                    basis_commit=BASE,
                    policy_hash=HASH,
                    permission_hash=HASH,
                    chapter_index=21,
                    target_chapters=21,
                    protected_chapter_index=21,
                    horizon_start=22,
                    horizon_end=23,
                ),
                TaskRecord(
                    task_id=TaskId("task.vertical.superseded-normal"),
                    run_id=run_id,
                    project_id=ProjectId("project.vertical"),
                    kind=TaskKind.PLAN_CANDIDATE,
                    task_revision=1,
                    status=TaskStatus.CANCELLED,
                    basis_commit=BASE,
                    policy_hash=HASH,
                    permission_hash=HASH,
                    chapter_index=21,
                    target_chapters=21,
                    superseded=True,
                ),
            )

    report = asyncio.run(
        VerticalCreativeRunner(
            runtime=cast(CreativeRuntimeService, _Runtime()),
            dispatcher=cast(CreativeDispatcher, _Dispatcher()),
            tasks=cast(RuntimeTaskReader, _Tasks()),
        ).run(_request(), max_tasks=1)
    )

    assert report.status is VerticalRunStatus.WAITING
    assert report.outputs_frozen is False
    assert report.dispatch_slices == 0


def test_vertical_runner_reports_foreground_crash_frontier_as_recovery_pending() -> None:
    class _Runtime:
        def start(self, request: CreativeRunRequest) -> CreativeRunResult:
            raise AssertionError("an existing run must not be started again")

    class _Dispatcher:
        async def run_bounded(self, *, max_tasks: int):
            raise AssertionError("a recovery frontier must not be redispatched")

    class _Tasks:
        def list_run(self, run_id: RunId):
            return (
                _ready_task("task.vertical.crashed").model_copy(
                    update={
                        "status": TaskStatus.RUNNING,
                        "current_attempt_id": TaskId("attempt.vertical.crashed"),
                    }
                ),
            )

    report = asyncio.run(
        VerticalCreativeRunner(
            runtime=cast(CreativeRuntimeService, _Runtime()),
            dispatcher=cast(CreativeDispatcher, _Dispatcher()),
            tasks=cast(RuntimeTaskReader, _Tasks()),
        ).run(_request(), max_tasks=1)
    )

    assert report.status is VerticalRunStatus.RECOVERY_PENDING
    assert report.outputs_frozen is False
    assert report.dispatch_slices == 0


def test_vertical_runner_repairs_auto_acceptance_before_polling_ready_work() -> None:
    waiting = TaskRecord(
        task_id=TaskId("task.vertical.auto-wait"),
        run_id=RunId("run.vertical"),
        project_id=ProjectId("project.vertical"),
        kind=TaskKind.DRAFT_ACCEPTANCE,
        task_revision=1,
        status=TaskStatus.WAITING_INPUT,
        basis_commit=BASE,
        policy_hash=HASH,
        permission_hash=HASH,
        chapter_index=21,
        target_chapters=21,
    )
    state: dict[str, tuple[TaskRecord, ...]] = {"tasks": (waiting,)}

    class _Runtime:
        calls = 0

        def start(self, request: CreativeRunRequest) -> CreativeRunResult:
            raise AssertionError("an existing run must not be started again")

        def recover_boundary(self, task_id: TaskId) -> CreativeRunResult:
            assert task_id == waiting.task_id
            self.calls += 1
            state["tasks"] = (_ready_task("task.vertical.after-auto"),)
            return _result(CreativeRunTerminal.PROGRESSED, BASE)

    class _Dispatcher:
        calls = 0

        async def run_bounded(self, *, max_tasks: int):
            assert max_tasks == 1
            self.calls += 1
            state["tasks"] = (
                TaskRecord(
                    task_id=TaskId("task.vertical.auto-complete"),
                    run_id=RunId("run.vertical"),
                    project_id=ProjectId("project.vertical"),
                    kind=TaskKind.PROJECTION_FRESHNESS,
                    task_revision=1,
                    status=TaskStatus.SUCCEEDED,
                    basis_commit=FINAL,
                    policy_hash=HASH,
                    permission_hash=HASH,
                    chapter_index=21,
                    target_chapters=21,
                    projection_after="draft",
                ),
            )
            return (_result(CreativeRunTerminal.COMPLETED, FINAL),)

    class _Tasks:
        def list_run(self, run_id: RunId) -> tuple[TaskRecord, ...]:
            assert run_id == RunId("run.vertical")
            return state["tasks"]

    runtime = _Runtime()
    dispatcher = _Dispatcher()
    report = asyncio.run(
        VerticalCreativeRunner(
            runtime=cast(CreativeRuntimeService, runtime),
            dispatcher=cast(CreativeDispatcher, dispatcher),
            tasks=cast(RuntimeTaskReader, _Tasks()),
        ).run(_request(), max_tasks=1)
    )

    assert runtime.calls == 1
    assert dispatcher.calls == 1
    assert report.status is VerticalRunStatus.COMPLETED
    assert report.dispatch_slices == 1


def test_vertical_runner_rejects_invalid_slice_limits() -> None:
    runner = VerticalCreativeRunner(
        runtime=cast(CreativeRuntimeService, object()),
        dispatcher=cast(CreativeDispatcher, object()),
        tasks=cast(RuntimeTaskReader, object()),
    )

    for kwargs in ({"max_tasks": 0}, {"max_tasks": 1, "max_slices": 0}):
        try:
            asyncio.run(runner.run(_request(), **kwargs))
        except ValueError:
            pass
        else:
            raise AssertionError("invalid dispatch limits must fail before using runtime ports")
