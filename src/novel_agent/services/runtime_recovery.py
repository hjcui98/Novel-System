"""Settled-checkpoint and uncertain-effect recovery for Stage 5."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import RuntimeEffectProjectionRow
from novel_agent.domain.ids import StableId, TaskId
from novel_agent.domain.runtime import (
    AttemptFence,
    EffectReceipt,
    EffectStatus,
    ResumabilityStatus,
    RunCheckpoint,
    TaskAttempt,
    TaskStatus,
)
from novel_agent.ports.creative_runtime import EffectStatusResolver
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.runtime_commands import (
    RuntimeCommandConflictError,
    RuntimeCommandService,
)
from novel_agent.services.runtime_projection import project_runtime_events


class RuntimeRecoveryService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        commands: RuntimeCommandService,
        checkpoints: RunCheckpointRepository,
        artifacts: ArtifactRepository,
        commits: CommitService,
        resolver: EffectStatusResolver,
    ) -> None:
        self._session_factory = session_factory
        self._commands = commands
        self._checkpoints = checkpoints
        self._events = RunEventLogRepository(session_factory)
        self._artifacts = artifacts
        self._commits = commits
        self._resolver = resolver

    def select_safe_checkpoint(self, task_id: TaskId) -> RunCheckpoint:
        task = self._commands.get_task(task_id)
        checkpoint = self._checkpoints.latest_resumable(task.run_id)
        if checkpoint is None or checkpoint.resumability_status is not ResumabilityStatus.RESUMABLE:
            raise RuntimeCommandConflictError("run has no settled resumable checkpoint")
        self._artifacts.read_verified(checkpoint.state_artifact_ref)
        replay_prefix = tuple(
            event
            for event in self._events.replay(task.run_id)
            if event.sequence_no <= checkpoint.event_position
        )
        rebuilt = project_runtime_events(replay_prefix)
        checkpoint_task = rebuilt.tasks.get(task.task_id.root)
        if checkpoint_task is None:
            raise RuntimeCommandConflictError(  # pragma: no cover - unreachable
                "checkpoint does not contain the durable task"
            )
        if (
            checkpoint_task.project_id != task.project_id
            or checkpoint_task.basis_commit != task.basis_commit
            or checkpoint_task.policy_hash != task.policy_hash
            or checkpoint_task.permission_hash != task.permission_hash
        ):
            raise RuntimeCommandConflictError(  # pragma: no cover - identity/policy are immutable
                "checkpoint task identity or policy drifted"
            )
        if self._commits.current_commit(task.project_id) != task.basis_commit:
            raise RuntimeCommandConflictError("checkpoint basis is no longer current")
        return checkpoint

    def reconcile_uncertain_effects(self, task_id: TaskId) -> tuple[EffectReceipt, ...]:
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(RuntimeEffectProjectionRow).where(
                        RuntimeEffectProjectionRow.task_id == task_id.root,
                        RuntimeEffectProjectionRow.status.in_(
                            (EffectStatus.REQUESTED.value, EffectStatus.UNCERTAIN.value)
                        ),
                    )
                )
            )
        resolved: list[EffectReceipt] = []
        for row in rows:
            prior = EffectReceipt.model_validate_json(json.dumps(row.effect_json))
            resolution = self._resolver.resolve(prior)
            receipt = resolution.receipt
            if receipt.status in {EffectStatus.REQUESTED, EffectStatus.UNCERTAIN}:
                self._commands.mark_recovery_pending(
                    task_id,
                    command_id=StableId(f"recovery-pending.{receipt.effect_identity.root}"),
                    actor_id="runtime-reconciler",
                    reason="external effect remains unresolved",
                )
                raise RuntimeCommandConflictError("external effect remains unresolved")
            self._commands.reconcile_effect(
                task_id,
                receipt,
                command_id=StableId(f"reconcile.{receipt.effect_identity.root}"),
            )
            resolved.append(receipt)
        return tuple(resolved)

    def resume(
        self,
        task_id: TaskId,
        *,
        worker_id: str,
        actor_id: str,
    ) -> tuple[RunCheckpoint, TaskAttempt, AttemptFence]:
        checkpoint = self.select_safe_checkpoint(task_id)
        self.reconcile_uncertain_effects(task_id)
        task = self._commands.get_task(task_id)
        if task.current_attempt_id is not None:
            raise RuntimeCommandConflictError("old attempt must be reconciled before resume")
        if task.paused:
            self._commands.resume(
                task_id,
                command_id=StableId(f"resume.{checkpoint.checkpoint_id.root}"),
                actor_id=actor_id,
                reason="resume from latest settled checkpoint",
                observed_revision=task.task_revision,
            )
        elif task.status is TaskStatus.WAITING_RETRY:
            self._commands.control(
                task_id,
                command_id=StableId(f"retry.{checkpoint.checkpoint_id.root}"),
                action="retry",
                actor_id=actor_id,
                reason="retry from latest settled checkpoint",
                observed_revision=task.task_revision,
            )
        elif task.status is not TaskStatus.READY:
            raise RuntimeCommandConflictError("task is not eligible for a fresh recovery attempt")
        ready = self._commands.get_task(task_id)
        attempt, fence = self._commands.claim(
            task_id,
            worker_id=worker_id,
            observed_revision=ready.task_revision,
        )
        return checkpoint, attempt, fence


__all__ = ["RuntimeRecoveryService"]
