"""Versioned candidate acceptance without Canon mutation authority."""

from __future__ import annotations

import hashlib

from novel_agent.domain.creative_runtime import (
    AcceptanceCommand,
    AcceptanceDecision,
    AcceptanceReceipt,
    AcceptedCandidateBinding,
    ActorKind,
    AutomationMode,
    CandidateKind,
    CreativeRunPolicy,
)
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.runtime import TaskKind, TaskStatus
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.runtime_commands import (
    RuntimeCommandConflictError,
    RuntimeCommandService,
)

ACCEPTANCE_MEDIA_TYPE = "application/vnd.novel-agent.stage5-acceptance-receipt+json"
ACCEPTANCE_SCHEMA_VERSION = SchemaVersion("1.0.0")


class RuntimeAcceptanceService:
    def __init__(
        self,
        commands: RuntimeCommandService,
        commits: CommitService,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion = ACCEPTANCE_SCHEMA_VERSION,
    ) -> None:
        self._commands = commands
        self._commits = commits
        self._artifacts = artifacts
        self._schema_version = schema_version

    def submit(
        self,
        command: AcceptanceCommand,
        *,
        policy: CreativeRunPolicy,
    ) -> AcceptanceReceipt:
        command_bytes = canonical_json_bytes(command.model_dump(mode="json"))
        command_hash = f"sha256:{hashlib.sha256(command_bytes).hexdigest()}"
        task = self._commands.get_task(command.task_id)
        expected_kind = {
            CandidateKind.PLAN: TaskKind.PLAN_ACCEPTANCE,
            CandidateKind.DRAFT: TaskKind.DRAFT_ACCEPTANCE,
        }[command.candidate.kind]
        if task.kind is not expected_kind or task.run_id != command.run_id:
            raise RuntimeCommandConflictError("candidate is bound to another acceptance task")
        if task.project_id != command.project_id:
            raise RuntimeCommandConflictError("acceptance project mismatch")
        if command.acceptance_policy_hash != policy.policy_hash:
            raise RuntimeCommandConflictError("acceptance policy hash mismatch")
        if command.expected_project_commit != self._commits.current_commit(command.project_id):
            raise RuntimeCommandConflictError("acceptance expected commit is stale")
        if command.actor_kind is ActorKind.POLICY:
            allowed = policy.automation_mode is AutomationMode.AUTO and (
                policy.auto_accept_plan
                if command.candidate.kind is CandidateKind.PLAN
                else policy.auto_accept_draft
            )
            if not allowed:
                raise RuntimeCommandConflictError("automatic acceptance is not profile-pinned")
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.CANCELLED}:
            if len(task.terminal_artifact_refs) != 1:
                raise RuntimeCommandConflictError("settled acceptance has invalid receipt lineage")
            prior = AcceptanceReceipt.model_validate_json(
                self._artifacts.read_verified(task.terminal_artifact_refs[0])
            )
            if prior.command_hash != command_hash:
                raise RuntimeCommandConflictError(
                    "acceptance identity was reused with another payload"
                )
            return prior
        if task.status is not TaskStatus.WAITING_INPUT:
            raise RuntimeCommandConflictError("acceptance task is not waiting for input")

        accepted = None
        if command.decision is AcceptanceDecision.ACCEPT:
            accepted = AcceptedCandidateBinding(
                acceptance_id=StableId("acceptance." + command_hash.removeprefix("sha256:")[:48]),
                command_id=command.command_id,
                project_id=command.project_id,
                run_id=command.run_id,
                task_id=command.task_id,
                candidate=command.candidate,
                actor_kind=command.actor_kind,
                actor_id=command.actor_id,
                accepted_at=command.issued_at,
                expected_project_commit=command.expected_project_commit,
            )
        receipt = AcceptanceReceipt(
            receipt_id=StableId("receipt." + command_hash.removeprefix("sha256:")[:48]),
            command_id=command.command_id,
            idempotency_identity=command.idempotency_identity,
            command_hash=command_hash,
            decision=command.decision,
            candidate=command.candidate,
            accepted_binding=accepted,
            reason=command.reason,
            recorded_at=command.issued_at,
        )
        receipt_ref = self._artifacts.put(
            canonical_json_bytes(receipt.model_dump(mode="json")),
            ACCEPTANCE_MEDIA_TYPE,
            self._schema_version,
        )
        self._commands.complete_waiting_task(
            command.task_id,
            receipt=receipt,
            receipt_ref=receipt_ref,
        )
        return receipt


__all__ = ["RuntimeAcceptanceService"]
