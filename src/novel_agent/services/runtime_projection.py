"""Pure full-replay projector for Stage 5 Task/Attempt/Effect query state."""

from __future__ import annotations

from pydantic import Field

from novel_agent.domain.base import DomainModel
from novel_agent.domain.runtime import (
    AcceptanceRecordedPayload,
    ControlIntentPayload,
    EffectReceipt,
    EffectRequestedPayload,
    EffectTerminalPayload,
    RunEvent,
    RunEventType,
    TaskAttempt,
    TaskAttemptSettledPayload,
    TaskAttemptStartedPayload,
    TaskClaimedPayload,
    TaskCreatedPayload,
    TaskRecord,
    TaskStatus,
    WriterClaimedPayload,
    failure_policy,
)


class RuntimeProjectionState(DomainModel):
    tasks: dict[str, TaskRecord] = Field(default_factory=dict)
    attempts: dict[str, TaskAttempt] = Field(default_factory=dict)
    effects: dict[str, EffectReceipt] = Field(default_factory=dict)


def project_runtime_events(events: tuple[RunEvent, ...]) -> RuntimeProjectionState:
    tasks: dict[str, TaskRecord] = {}
    attempts: dict[str, TaskAttempt] = {}
    effects: dict[str, EffectReceipt] = {}
    expected_sequence = 1
    active_run = None
    for event in events:
        if active_run is None:
            active_run = event.run_id
        if event.run_id != active_run or event.sequence_no != expected_sequence:
            raise ValueError("runtime replay requires one contiguous run stream")
        expected_sequence += 1
        task_key = None if event.task_id is None else event.task_id.root
        if event.event_type is RunEventType.RUNTIME_TASK_CREATED:
            created_payload = TaskCreatedPayload.model_validate(event.payload, strict=False)
            if created_payload.task.task_id.root in tasks:
                raise ValueError("runtime replay encountered duplicate task creation")
            tasks[created_payload.task.task_id.root] = created_payload.task
            continue
        if task_key is None or task_key not in tasks:
            continue
        task = tasks[task_key]
        if event.event_type is RunEventType.RUNTIME_TASK_CLAIMED:
            claimed_payload = TaskClaimedPayload.model_validate(event.payload, strict=False)
            attempts[claimed_payload.attempt.attempt_id.root] = claimed_payload.attempt
            tasks[task_key] = task.model_copy(
                update={
                    "status": TaskStatus.RUNNING,
                    "current_attempt_id": claimed_payload.attempt.attempt_id,
                    "task_revision": task.task_revision + 1,
                }
            )
        elif event.event_type is RunEventType.RUNTIME_ATTEMPT_STARTED:
            started_payload = TaskAttemptStartedPayload.model_validate(event.payload, strict=False)
            attempt = attempts[started_payload.attempt_id.root]
            attempts[started_payload.attempt_id.root] = attempt.model_copy(
                update={"started_at": started_payload.started_at}
            )
        elif event.event_type is RunEventType.RUNTIME_ATTEMPT_SETTLED:
            settled_payload = TaskAttemptSettledPayload.model_validate(event.payload, strict=False)
            attempt = attempts[settled_payload.attempt_id.root]
            attempts[settled_payload.attempt_id.root] = attempt.model_copy(
                update={
                    "ended_at": settled_payload.ended_at,
                    "outcome": settled_payload.outcome,
                    "failure_class": settled_payload.failure_class,
                }
            )
            tasks[task_key] = task.model_copy(
                update={
                    "status": settled_payload.task_status,
                    "current_attempt_id": None,
                    "terminal_artifact_refs": settled_payload.terminal_artifact_refs,
                    "block_cause": settled_payload.block_cause,
                    "failure_budget": max(
                        0,
                        task.failure_budget
                        - (
                            1
                            if settled_payload.failure_class is not None
                            and failure_policy(settled_payload.failure_class).consumes_task_budget
                            else 0
                        ),
                    ),
                    "task_revision": task.task_revision + 1,
                }
            )
        elif event.event_type is RunEventType.RUNTIME_ACCEPTANCE_RECORDED:
            acceptance_payload = AcceptanceRecordedPayload.model_validate(
                event.payload, strict=False
            )
            tasks[task_key] = task.model_copy(
                update={
                    "status": (
                        TaskStatus.SUCCEEDED
                        if acceptance_payload.decision == "accept"
                        else TaskStatus.CANCELLED
                    ),
                    "terminal_artifact_refs": event.artifact_refs,
                    "task_revision": task.task_revision + 1,
                }
            )
        elif event.event_type is RunEventType.RUNTIME_CONTROL_RECORDED:
            control_payload = ControlIntentPayload.model_validate(event.payload, strict=False)
            if control_payload.action == "operator_reconcile":
                continue
            status = task.status
            paused = task.paused
            cancel_requested = task.cancel_requested
            if control_payload.action == "pause":
                paused = True
                if task.current_attempt_id is None:
                    status = TaskStatus.PENDING
            elif control_payload.action == "resume":
                paused = False
                cancel_requested = False
                status = TaskStatus.READY
            elif control_payload.action == "cancel":
                paused = True
                cancel_requested = True
                status = (
                    TaskStatus.CANCELLED
                    if task.current_attempt_id is None
                    else TaskStatus.RECOVERY_PENDING
                )
            elif control_payload.action in {"retry", "unblock"}:
                status = TaskStatus.READY
            elif control_payload.action == "recovery_pending":  # pragma: no cover - last branch
                status = TaskStatus.RECOVERY_PENDING
            tasks[task_key] = task.model_copy(
                update={
                    "status": status,
                    "paused": paused,
                    "cancel_requested": cancel_requested,
                    "block_cause": (
                        None if control_payload.action == "unblock" else task.block_cause
                    ),
                    "task_revision": task.task_revision + 1,
                }
            )
        elif event.event_type is RunEventType.RUNTIME_WRITER_CLAIMED:
            writer_payload = WriterClaimedPayload.model_validate(event.payload, strict=False)
            tasks[task_key] = task.model_copy(
                update={
                    "writer_generation": writer_payload.writer_generation,
                    "task_revision": task.task_revision + 1,
                }
            )
        elif event.event_type is RunEventType.RUNTIME_EFFECT_REQUESTED:
            requested_payload = EffectRequestedPayload.model_validate(event.payload, strict=False)
            effects[requested_payload.effect.effect_identity.root] = requested_payload.effect
        elif event.event_type is RunEventType.RUNTIME_EFFECT_TERMINAL:
            terminal_payload = EffectTerminalPayload.model_validate(event.payload, strict=False)
            effects[terminal_payload.effect.effect_identity.root] = terminal_payload.effect
    return RuntimeProjectionState(tasks=tasks, attempts=attempts, effects=effects)


def assert_task_projection_matches(
    events: tuple[RunEvent, ...], incremental: tuple[TaskRecord, ...]
) -> None:
    rebuilt = project_runtime_events(events)
    actual = {item.task_id.root: item for item in incremental}
    if rebuilt.tasks != actual:
        raise RuntimeError("incremental runtime task projection differs from full replay")


__all__ = [
    "RuntimeProjectionState",
    "assert_task_projection_matches",
    "project_runtime_events",
]
