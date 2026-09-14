"""Settled-checkpoint and uncertain-effect recovery for Stage 5."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import RuntimeEffectProjectionRow
from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.domain.creative_runtime import (
    RUNTIME_MODEL_REPLAY_EVIDENCE_MEDIA_TYPE,
    RuntimeModelReplayEvidence,
    RuntimeModelReplayResponse,
)
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId, TaskId
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
from novel_agent.services.attempt_classification import RecoveryAction, classify_attempt
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.runtime_commands import (
    RuntimeCommandConflictError,
    RuntimeCommandService,
)
from novel_agent.services.runtime_projection import project_runtime_events


def _bounded_recovery_command_id(
    prefix: str,
    identity: str,
    *,
    task_id: TaskId,
    run_id: RunId,
) -> StableId:
    """Keep recovery commands bounded while retaining an existing scope."""

    for value in (
        f"{prefix}.{identity}",
        f"{prefix}.{task_id.root}",
        f"{prefix}.{run_id.root}",
    ):
        try:
            return StableId(value)
        except ValueError:
            continue
    raise RuntimeCommandConflictError("recovery command identity is too long")


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

    def select_safe_checkpoint(
        self,
        task_id: TaskId,
        *,
        current_configuration_fingerprint: ArtifactId | None = None,
    ) -> RunCheckpoint:
        task = self._commands.get_task(task_id)
        if (
            current_configuration_fingerprint is None
            or task.policy_hash != current_configuration_fingerprint.root
        ):
            raise RuntimeCommandConflictError("RUN_CONFIGURATION_CHANGED")
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
        task = self._commands.get_task(task_id)
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
                    command_id=_bounded_recovery_command_id(
                        "recovery-pending",
                        receipt.effect_identity.root,
                        task_id=task_id,
                        run_id=task.run_id,
                    ),
                    actor_id="runtime-reconciler",
                    reason="external effect remains unresolved",
                )
                raise RuntimeCommandConflictError("external effect remains unresolved")
            self._commands.reconcile_effect(
                task_id,
                receipt,
                command_id=_bounded_recovery_command_id(
                    "reconcile",
                    receipt.effect_identity.root,
                    task_id=task_id,
                    run_id=task.run_id,
                ),
            )
            resolved.append(receipt)
        return tuple(resolved)

    def resume(
        self,
        task_id: TaskId,
        *,
        worker_id: str,
        actor_id: str,
        current_configuration_fingerprint: ArtifactId | None = None,
    ) -> tuple[RunCheckpoint, TaskAttempt, AttemptFence]:
        checkpoint = self.select_safe_checkpoint(
            task_id,
            current_configuration_fingerprint=current_configuration_fingerprint,
        )
        self.reconcile_uncertain_effects(task_id)
        task = self._commands.get_task(task_id)
        if task.current_attempt_id is not None:
            raise RuntimeCommandConflictError("old attempt must be reconciled before resume")
        evidence = RuntimeTaskQueryRepository(self._session_factory).attempt_effect_evidence(
            task_id
        )
        if (
            evidence.completed_response_refs
            or evidence.unavailable_response_ids
            or evidence.unsettled_sends
            or evidence.outstanding_request_ids
        ):
            if (
                evidence.completed_response_refs
                and not evidence.unavailable_response_ids
                and not evidence.unsettled_sends
                and not evidence.outstanding_request_ids
            ):
                query = RuntimeTaskQueryRepository(self._session_factory)
                classification = classify_attempt(
                    task_id=StableId(task_id.root),
                    task_status=task.status,
                    attempt=query.last_settled_attempt(task_id),
                    frontier_attempt_id=evidence.frontier_attempt_id,
                    completed_response_refs=evidence.completed_response_refs,
                    consumed_response_ids=evidence.consumed_response_ids,
                    unavailable_response_ids=evidence.unavailable_response_ids,
                    unsettled_sends=evidence.unsettled_sends,
                    outstanding_request_ids=evidence.outstanding_request_ids,
                    block_cause=task.block_cause,
                )
                if classification.action is RecoveryAction.REPLAY_COMPLETED:
                    responses = query.replayable_model_responses(
                        task_id,
                        attempt_id=evidence.frontier_attempt_id,
                    )
                    if not responses or tuple(
                        sorted(item.raw_artifact_ref.artifact_id.root for item in responses)
                    ) != tuple(sorted(evidence.completed_response_refs)):
                        raise RuntimeCommandConflictError(
                            "completed model evidence is not a stable replay frontier"
                        )
                    source_attempt_id = responses[0].source_attempt_id
                    if any(
                        item.source_attempt_id != source_attempt_id for item in responses
                    ):
                        raise RuntimeCommandConflictError(
                            "model replay frontier spans multiple source attempts"
                        )
                    replay = RuntimeModelReplayEvidence(
                        run_id=task.run_id,
                        task_id=task.task_id,
                        source_attempt_id=source_attempt_id,
                        responses=tuple(
                            RuntimeModelReplayResponse(
                                request_id=item.request_id,
                                source_attempt_id=item.source_attempt_id,
                                request_hash=item.request_hash.root,
                                logical_phase=item.logical_phase,
                                raw_artifact_ref=item.raw_artifact_ref,
                            )
                            for item in responses
                        ),
                    )
                    replay_ref = self._artifacts.put(
                        canonical_json_bytes(replay.model_dump(mode="json")),
                        RUNTIME_MODEL_REPLAY_EVIDENCE_MEDIA_TYPE,
                        SchemaVersion("1.0.0"),
                    )
                    self._commands.requeue_model_replay(
                        task_id,
                        replay_evidence_ref=replay_ref,
                        command_id=_bounded_recovery_command_id(
                            "replay-model",
                            replay_ref.artifact_id.root,
                            task_id=task_id,
                            run_id=task.run_id,
                        ),
                        actor_id=actor_id,
                        reason=classification.reason,
                        observed_revision=task.task_revision,
                    )
                    task = self._commands.get_task(task_id)
                else:
                    raise RuntimeCommandConflictError(
                        "recovery cannot replay the settled model response: "
                        f"{classification.action.value}"
                    )
            else:
                raise RuntimeCommandConflictError(
                    "recovery must reconcile or replay prior model evidence before claiming "
                    "a fresh attempt"
                )
        if task.paused:
            self._commands.resume(
                task_id,
                command_id=_bounded_recovery_command_id(
                    "resume",
                    checkpoint.checkpoint_id.root,
                    task_id=task_id,
                    run_id=task.run_id,
                ),
                actor_id=actor_id,
                reason="resume from latest settled checkpoint",
                observed_revision=task.task_revision,
            )
        elif task.status is TaskStatus.WAITING_RETRY:
            self._commands.control(
                task_id,
                command_id=_bounded_recovery_command_id(
                    "retry",
                    checkpoint.checkpoint_id.root,
                    task_id=task_id,
                    run_id=task.run_id,
                ),
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
