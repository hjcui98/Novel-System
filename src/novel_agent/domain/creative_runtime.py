"""Stage 5 fixed creative topology and candidate acceptance contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import CommitId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.runtime import TaskKind, TaskPurpose, TaskRecord, TaskStatus

Hash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class AutomationMode(StrEnum):
    MANUAL = "manual"
    SEMI = "semi"
    AUTO = "auto"


class CandidateKind(StrEnum):
    PLAN = "plan"
    DRAFT = "draft"


class PlanningTerminalStatus(StrEnum):
    PLAN_CANDIDATE_READY = "PLAN_CANDIDATE_READY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SUSPENDED = "SUSPENDED"
    BLOCKED = "BLOCKED"


class AcceptanceDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    REVOKE = "revoke"


class ActorKind(StrEnum):
    AUTHOR = "author"
    OPERATOR = "operator"
    POLICY = "policy"


class CreativeRunTerminal(StrEnum):
    PROGRESSED = "PROGRESSED"
    WAITING_PLAN_ACCEPTANCE = "WAITING_PLAN_ACCEPTANCE"
    WAITING_DRAFT_ACCEPTANCE = "WAITING_DRAFT_ACCEPTANCE"
    WAITING_RETRY = "WAITING_RETRY"
    RECOVERY_PENDING = "RECOVERY_PENDING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class CreativeRunPolicy(DomainModel):
    automation_mode: AutomationMode
    policy_hash: Hash
    permission_hash: Hash
    auto_accept_plan: bool = False
    auto_accept_draft: bool = False
    max_task_attempts: int = Field(default=3, ge=1, le=20)
    max_tasks_per_advance: int = Field(default=1, ge=1, le=10)
    runtime_parallelism: Literal[1, 2] = 1
    enable_planner_lookahead: bool = False
    lookahead_horizon: int = Field(default=3, ge=1, le=12)

    @model_validator(mode="after")
    def validate_auto_policy(self) -> CreativeRunPolicy:
        if self.automation_mode is not AutomationMode.AUTO and (
            self.auto_accept_plan or self.auto_accept_draft
        ):
            raise ValueError("only auto mode may enable policy acceptance")
        if self.enable_planner_lookahead and self.runtime_parallelism != 2:
            raise ValueError("Planner lookahead requires runtime parallelism 2")
        return self


class CreativeRunRequest(DomainModel):
    run_id: RunId
    project_id: ProjectId
    basis_commit: CommitId
    basis_snapshot: StableId | None = None
    policy: CreativeRunPolicy
    input_artifact_refs: tuple[ArtifactRef, ...] = ()
    target_chapters: int = Field(default=1, ge=1, le=10000)


class CreativeTaskSpec(DomainModel):
    task_id: TaskId
    kind: TaskKind
    basis_commit: CommitId
    basis_snapshot: StableId | None = None
    dependency_task_ids: tuple[TaskId, ...] = ()
    input_artifact_refs: tuple[ArtifactRef, ...] = ()


class CandidateBinding(DomainModel):
    candidate_id: StableId
    kind: CandidateKind
    artifact_ref: ArtifactRef
    candidate_hash: Hash
    basis_commit: CommitId
    basis_snapshot: StableId | None = None
    lineage_artifact_refs: tuple[ArtifactRef, ...] = ()
    planning_purpose: TaskPurpose = TaskPurpose.NORMAL
    horizon_start: int | None = Field(default=None, ge=1)
    horizon_end: int | None = Field(default=None, ge=1)
    protected_chapter_index: int | None = Field(default=None, ge=1)
    affects_future_plan: bool | None = None

    @model_validator(mode="after")
    def validate_hash(self) -> CandidateBinding:
        if self.candidate_hash != self.artifact_ref.artifact_id.root:
            raise ValueError("candidate hash must match the immutable candidate artifact")
        if self.planning_purpose is TaskPurpose.LOOKAHEAD and (
            self.kind is not CandidateKind.PLAN
            or self.horizon_start is None
            or self.horizon_end is None
            or self.protected_chapter_index is None
            or self.horizon_start <= self.protected_chapter_index
        ):
            raise ValueError("lookahead candidate requires its protected future horizon")
        if self.affects_future_plan is not None and self.kind is not CandidateKind.DRAFT:
            raise ValueError("future-Plan impact belongs only to a Draft candidate")
        return self


class PlanningLoopRequest(DomainModel):
    run_id: RunId
    task_id: TaskId
    project_id: ProjectId
    basis_commit: CommitId
    basis_snapshot: StableId | None = None
    input_artifact_refs: tuple[ArtifactRef, ...] = ()
    purpose: TaskPurpose = TaskPurpose.NORMAL
    chapter_index: int = Field(default=0, ge=0)
    horizon_start: int | None = Field(default=None, ge=1)
    horizon_end: int | None = Field(default=None, ge=1)
    protected_chapter_index: int | None = Field(default=None, ge=1)


class LookaheadRevalidationOutcome(StrEnum):
    PROMOTED = "promoted"
    REPLAN_REQUIRED = "replan_required"
    SUPERSEDED = "superseded"


class LookaheadRevalidationReceipt(DomainModel):
    receipt_id: StableId
    run_id: RunId
    lookahead_task_id: TaskId
    original_basis_commit: CommitId
    current_commit: CommitId
    current_snapshot: StableId
    protected_chapter_index: int
    horizon_start: int
    horizon_end: int
    affects_future_plan: bool | None = None
    outcome: LookaheadRevalidationOutcome
    reason: str = Field(min_length=1, max_length=256)


class PlanningLoopResult(DomainModel):
    result_id: StableId
    run_id: RunId
    task_id: TaskId
    status: PlanningTerminalStatus
    candidate: CandidateBinding | None = None
    artifact_refs: tuple[ArtifactRef, ...] = ()
    failure_code: str | None = Field(default=None, min_length=1, max_length=128)
    failure_detail: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_terminal(self) -> PlanningLoopResult:
        ready = self.status is PlanningTerminalStatus.PLAN_CANDIDATE_READY
        if ready != (self.candidate is not None):
            raise ValueError("only PLAN_CANDIDATE_READY carries a candidate")
        if ready and self.failure_code is not None:
            raise ValueError("ready Planner result cannot carry a failure")
        if not ready and (self.failure_code is None or self.failure_detail is None):
            raise ValueError("non-ready Planner result requires typed failure detail")
        return self


class AcceptanceCommand(DomainModel):
    kind: Literal["acceptance"] = "acceptance"
    command_id: StableId
    project_id: ProjectId
    run_id: RunId
    task_id: TaskId
    candidate: CandidateBinding
    acceptance_policy_hash: Hash
    actor_kind: ActorKind
    actor_id: str = Field(min_length=1, max_length=128)
    decision: AcceptanceDecision
    reason: str = Field(min_length=1, max_length=512)
    expected_project_commit: CommitId
    idempotency_identity: StableId
    issued_at: datetime

    @model_validator(mode="after")
    def validate_basis(self) -> AcceptanceCommand:
        if self.candidate.basis_commit != self.expected_project_commit:
            raise ValueError("candidate basis must be the expected current commit")
        return self


class AcceptedCandidateBinding(DomainModel):
    acceptance_id: StableId
    command_id: StableId
    project_id: ProjectId
    run_id: RunId
    task_id: TaskId
    candidate: CandidateBinding
    actor_kind: ActorKind
    actor_id: str = Field(min_length=1, max_length=128)
    accepted_at: datetime
    expected_project_commit: CommitId


class AcceptanceReceipt(DomainModel):
    receipt_id: StableId
    command_id: StableId
    idempotency_identity: StableId
    command_hash: Hash
    decision: AcceptanceDecision
    candidate: CandidateBinding
    accepted_binding: AcceptedCandidateBinding | None = None
    reason: str = Field(min_length=1, max_length=512)
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_decision(self) -> AcceptanceReceipt:
        accepted = self.decision is AcceptanceDecision.ACCEPT
        if accepted != (self.accepted_binding is not None):
            raise ValueError("only accepted decisions carry an accepted binding")
        return self


class _OperatorCommand(DomainModel):
    command_id: StableId
    run_id: RunId
    task_id: TaskId
    actor_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)


class PauseCommand(_OperatorCommand):
    kind: Literal["pause"] = "pause"


class ResumeCommand(_OperatorCommand):
    kind: Literal["resume"] = "resume"


class CancelCommand(_OperatorCommand):
    kind: Literal["cancel"] = "cancel"


class RetryCommand(_OperatorCommand):
    kind: Literal["retry"] = "retry"


class UnblockCommand(_OperatorCommand):
    kind: Literal["unblock"] = "unblock"
    block_cause_fingerprint: Hash
    changed_evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)


CreativeRuntimeCommand = Annotated[
    AcceptanceCommand
    | PauseCommand
    | ResumeCommand
    | CancelCommand
    | RetryCommand
    | UnblockCommand,
    Field(discriminator="kind"),
]


class CreativeRunResult(DomainModel):
    run_id: RunId
    project_id: ProjectId
    terminal: CreativeRunTerminal
    current_task_id: TaskId | None = None
    current_attempt_id: StableId | None = None
    basis_commit: CommitId
    current_commit: CommitId
    artifact_refs: tuple[ArtifactRef, ...] = ()
    receipt_refs: tuple[ArtifactRef, ...] = ()
    next_legal_commands: tuple[str, ...] = ()
    reason_code: str = Field(min_length=1, max_length=128)


_NEXT_KIND: dict[TaskKind, TaskKind] = {
    TaskKind.PLAN_CANDIDATE: TaskKind.PLAN_ACCEPTANCE,
    TaskKind.PLAN_ACCEPTANCE: TaskKind.PLAN_COMMIT,
    TaskKind.PLAN_COMMIT: TaskKind.PROJECTION_FRESHNESS,
    TaskKind.DRAFT_CANDIDATE: TaskKind.DRAFT_ACCEPTANCE,
    TaskKind.DRAFT_ACCEPTANCE: TaskKind.DRAFT_COMMIT,
    TaskKind.DRAFT_COMMIT: TaskKind.PROJECTION_FRESHNESS,
}


def next_task_kind(task: TaskRecord, *, after_projection: CandidateKind | None = None) -> TaskKind:
    """Return the only legal fixed-topology successor for a settled task."""

    if task.status is not TaskStatus.SUCCEEDED:
        raise ValueError("only a succeeded task may unlock a successor")
    if task.kind is TaskKind.PROJECTION_FRESHNESS:
        if after_projection is CandidateKind.PLAN:
            return TaskKind.DRAFT_CANDIDATE
        if after_projection is CandidateKind.DRAFT:
            return TaskKind.DRAFT_CANDIDATE
        raise ValueError("projection successor requires the committed candidate kind")
    try:
        return _NEXT_KIND[task.kind]
    except KeyError as error:
        raise ValueError(f"task kind has no automatic successor: {task.kind.value}") from error


def validate_successor(previous: TaskRecord, successor: CreativeTaskSpec) -> None:
    expected = next_task_kind(previous)
    if successor.kind is not expected or previous.task_id not in successor.dependency_task_ids:
        raise ValueError("successor skips a fixed topology boundary")
    if successor.basis_commit != previous.basis_commit:
        raise ValueError("successor basis differs before a trusted commit boundary")


def commit_task_from_acceptance(previous: TaskRecord, receipt: AcceptanceReceipt) -> TaskRecord:
    """Pure fixed-topology reducer for the acceptance-to-commit boundary."""

    if previous.status is not TaskStatus.SUCCEEDED or receipt.accepted_binding is None:
        raise ValueError("only an accepted, settled candidate can create a commit task")
    expected_kind = (
        TaskKind.PLAN_ACCEPTANCE
        if receipt.candidate.kind is CandidateKind.PLAN
        else TaskKind.DRAFT_ACCEPTANCE
    )
    if (
        previous.kind is not expected_kind
        or receipt.candidate.basis_commit != previous.basis_commit
    ):
        raise ValueError("acceptance receipt does not match the fixed topology basis")
    kind = (
        TaskKind.PLAN_COMMIT
        if receipt.candidate.kind is CandidateKind.PLAN
        else TaskKind.DRAFT_COMMIT
    )
    return TaskRecord(
        task_id=TaskId(f"{previous.task_id.root}.commit"),
        run_id=previous.run_id,
        project_id=previous.project_id,
        kind=kind,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=previous.basis_commit,
        basis_snapshot=previous.basis_snapshot,
        policy_hash=previous.policy_hash,
        permission_hash=previous.permission_hash,
        input_artifact_refs=previous.terminal_artifact_refs,
        dependency_task_ids=(previous.task_id,),
        failure_budget=previous.failure_budget,
        chapter_index=previous.chapter_index,
        target_chapters=previous.target_chapters,
        purpose=previous.purpose,
        horizon_start=previous.horizon_start,
        horizon_end=previous.horizon_end,
        protected_chapter_index=previous.protected_chapter_index,
        affects_future_plan=receipt.candidate.affects_future_plan,
    )


__all__ = [
    "AcceptanceCommand",
    "AcceptanceDecision",
    "AcceptanceReceipt",
    "AcceptedCandidateBinding",
    "ActorKind",
    "AutomationMode",
    "CandidateBinding",
    "CandidateKind",
    "CreativeRunPolicy",
    "CreativeRunRequest",
    "CreativeRunResult",
    "CreativeRunTerminal",
    "CreativeTaskSpec",
    "LookaheadRevalidationOutcome",
    "LookaheadRevalidationReceipt",
    "PlanningLoopRequest",
    "PlanningLoopResult",
    "PlanningTerminalStatus",
    "commit_task_from_acceptance",
    "next_task_kind",
    "validate_successor",
]
