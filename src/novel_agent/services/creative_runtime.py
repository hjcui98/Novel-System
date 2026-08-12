"""Thin fixed-topology Stage 5 application coordinator."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from novel_agent.domain.changes import CommitRequest, CommitStatus, ValidationStatus
from novel_agent.domain.creative_runtime import (
    AcceptanceCommand,
    AcceptanceDecision,
    AcceptanceReceipt,
    AcceptedCandidateBinding,
    ActorKind,
    CandidateBinding,
    CandidateKind,
    CreativeRunPolicy,
    CreativeRunRequest,
    CreativeRunResult,
    CreativeRunTerminal,
    PlanningLoopRequest,
    PlanningTerminalStatus,
    commit_task_from_acceptance,
)
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId, TaskId
from novel_agent.domain.memory import FreshnessMode, FreshnessRequest, FreshnessStatus
from novel_agent.domain.runtime import (
    AttemptOutcome,
    FailureClass,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.domain.writing_loop import WritingLoopTerminalStatus
from novel_agent.ports.creative_runtime import (
    CandidateMaterializer,
    PlanningLeafPort,
    WritingLeafPort,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.projection import (
    DerivedProjectionService,
    DerivedSnapshotRepository,
    FreshnessGate,
)
from novel_agent.services.runtime_acceptance import RuntimeAcceptanceService
from novel_agent.services.runtime_commands import RuntimeCommandService

CANDIDATE_BINDING_MEDIA_TYPE = "application/vnd.novel-agent.stage5-candidate-binding+json"


class CreativeRuntimeService:
    def __init__(
        self,
        commands: RuntimeCommandService,
        acceptance: RuntimeAcceptanceService,
        commits: CommitService,
        artifacts: ArtifactRepository,
        planner: PlanningLeafPort,
        writer: WritingLeafPort,
        writing_request_factory: Callable[[TaskRecord], WritingLoopRequest],
        plan_materializer: CandidateMaterializer,
        draft_materializer: CandidateMaterializer,
        projection: DerivedProjectionService,
        snapshots: DerivedSnapshotRepository,
        policy_resolver: Callable[[str], CreativeRunPolicy],
    ) -> None:
        self._commands = commands
        self._acceptance = acceptance
        self._commits = commits
        self._artifacts = artifacts
        self._planner = planner
        self._writer = writer
        self._writing_request_factory = writing_request_factory
        self._plan_materializer = plan_materializer
        self._draft_materializer = draft_materializer
        self._projection = projection
        self._snapshots = snapshots
        self._policy_resolver = policy_resolver

    def start(self, request: CreativeRunRequest) -> CreativeRunResult:
        task = self._commands.create_run_and_initial_task(request)
        return self._result(task, CreativeRunTerminal.PROGRESSED, "run_created")

    async def advance(self, task_id: TaskId, *, worker_id: str) -> CreativeRunResult:
        task = self._commands.get_task(task_id)
        if task.kind in {TaskKind.PLAN_ACCEPTANCE, TaskKind.DRAFT_ACCEPTANCE}:
            automatic = self._auto_accept(task, self._candidate_for_task(task))
            if automatic is not None:
                return automatic
            return self._result(
                task,
                CreativeRunTerminal.WAITING_PLAN_ACCEPTANCE
                if task.kind is TaskKind.PLAN_ACCEPTANCE
                else CreativeRunTerminal.WAITING_DRAFT_ACCEPTANCE,
                "acceptance_required",
            )
        _, fence = self._commands.claim(
            task.task_id,
            worker_id=worker_id,
            observed_revision=task.task_revision,
        )
        self._commands.mark_started(fence)
        if task.kind is TaskKind.PLAN_CANDIDATE:
            planning_request = PlanningLoopRequest(
                run_id=task.run_id,
                task_id=task.task_id,
                project_id=task.project_id,
                basis_commit=task.basis_commit,
                basis_snapshot=task.basis_snapshot,
                input_artifact_refs=task.input_artifact_refs,
            )
            planning_result = await self._planner.run(planning_request)
            if planning_result.status is PlanningTerminalStatus.PLAN_CANDIDATE_READY:
                assert planning_result.candidate is not None
                settled = self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.SUCCEEDED,
                    terminal_status=TaskStatus.SUCCEEDED,
                    artifact_refs=planning_result.artifact_refs,
                )
                waiting = self._acceptance_task(settled, planning_result.candidate)
                self._commands.create_task(waiting)
                automatic = self._auto_accept(waiting, planning_result.candidate)
                if automatic is not None:
                    return automatic
                return self._result(
                    waiting,
                    CreativeRunTerminal.WAITING_PLAN_ACCEPTANCE,
                    "plan_candidate_ready",
                )
            failure, status, terminal = self._planner_failure(planning_result.status)
            settled = self._commands.settle_attempt(
                fence,
                outcome=AttemptOutcome.SUSPENDED,
                terminal_status=status,
                failure_class=failure,
            )
            return self._result(settled, terminal, planning_result.failure_code or "planner_failed")
        if task.kind is TaskKind.DRAFT_CANDIDATE:
            writing_request = self._writing_request_factory(task)
            if (
                writing_request.run_id != task.run_id
                or writing_request.task_id != task.task_id
                or writing_request.base_commit != task.basis_commit
                or writing_request.snapshot_id != task.basis_snapshot
            ):
                raise ValueError("Writer request factory violated the durable task basis")
            writing_result = await self._writer.run(writing_request)
            if writing_result.status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY:
                assert writing_result.final_candidate_id is not None
                assert writing_result.final_text_artifact is not None
                candidate = CandidateBinding(
                    candidate_id=StableId(
                        "draft-candidate."
                        + writing_result.final_candidate_id.root.removeprefix("sha256:")[:48]
                    ),
                    kind=CandidateKind.DRAFT,
                    artifact_ref=writing_result.final_text_artifact,
                    candidate_hash=writing_result.final_text_artifact.artifact_id.root,
                    basis_commit=task.basis_commit,
                    basis_snapshot=task.basis_snapshot,
                    lineage_artifact_refs=writing_result.artifacts,
                )
                settled = self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.SUCCEEDED,
                    terminal_status=TaskStatus.SUCCEEDED,
                    artifact_refs=writing_result.artifacts,
                )
                waiting = self._acceptance_task(settled, candidate)
                self._commands.create_task(waiting)
                automatic = self._auto_accept(waiting, candidate)
                if automatic is not None:
                    return automatic
                return self._result(
                    waiting,
                    CreativeRunTerminal.WAITING_DRAFT_ACCEPTANCE,
                    "draft_candidate_ready",
                )
            failure, status, terminal = self._writer_failure(writing_result.status)
            settled = self._commands.settle_attempt(
                fence,
                outcome=AttemptOutcome.SUSPENDED,
                terminal_status=status,
                artifact_refs=writing_result.artifacts,
                failure_class=failure,
            )
            return self._result(settled, terminal, writing_result.status.value.lower())
        if task.kind in {TaskKind.PLAN_COMMIT, TaskKind.DRAFT_COMMIT}:
            fence = self._commands.claim_writer_lane(fence)
            accepted = self._accepted_binding(task)
            materializer = (
                self._plan_materializer
                if task.kind is TaskKind.PLAN_COMMIT
                else self._draft_materializer
            )
            bundle, report = materializer.materialize(accepted)
            if report.status is not ValidationStatus.PASSED:
                settled = self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.FAILED,
                    terminal_status=TaskStatus.BLOCKED,
                    artifact_refs=bundle.produced_artifacts,
                    failure_class=FailureClass.VALIDATION_REJECTED,
                )
                return self._result(
                    settled, CreativeRunTerminal.REVIEW_REQUIRED, "validation_rejected"
                )
            commit_request = CommitRequest(
                request_id=StableId(f"commit-request.{accepted.acceptance_id.root}"),
                project_id=task.project_id,
                base_commit=task.basis_commit,
                idempotency_key=StableId(f"commit.{accepted.acceptance_id.root}"),
                bundle=bundle,
                validation_report=report,
            )
            commit_result = self._commands.commit_accepted_candidate(
                fence, commit_request, self._commits
            )
            settled = self._commands.get_task(task.task_id)
            if commit_result.status is not CommitStatus.ACCEPTED or commit_result.commit_id is None:
                return self._result(
                    settled, CreativeRunTerminal.BLOCKED, commit_result.status.value
                )
            projection_task = self._projection_task(settled, commit_result.commit_id)
            self._commands.create_task(projection_task)
            return self._result(projection_task, CreativeRunTerminal.PROGRESSED, "commit_accepted")
        if task.kind is TaskKind.PROJECTION_FRESHNESS:
            try:
                self._projection.process_all()
            except Exception:
                settled = self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.FAILED,
                    terminal_status=TaskStatus.WAITING_RETRY,
                    failure_class=FailureClass.PROJECTION_FAILED,
                )
                return self._result(settled, CreativeRunTerminal.WAITING_RETRY, "projection_failed")
            snapshot = self._snapshots.get_for_commit(task.basis_commit)
            if snapshot is None:
                settled = self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.SUSPENDED,
                    terminal_status=TaskStatus.WAITING_RETRY,
                    failure_class=FailureClass.FRESHNESS_WAITING,
                )
                return self._result(
                    settled, CreativeRunTerminal.WAITING_RETRY, "snapshot_not_published"
                )
            decision = FreshnessGate.evaluate(
                FreshnessRequest(
                    canonical_commit=task.basis_commit,
                    r1_basis_commit=task.basis_commit,
                    required_snapshot_id=snapshot.snapshot_id,
                    actual_alias_commit=task.basis_commit,
                    actual_snapshot=snapshot,
                    mode=FreshnessMode.WAIT_FOR_EXACT,
                )
            )
            if decision.status is not FreshnessStatus.READY:
                settled = self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.SUSPENDED,
                    terminal_status=TaskStatus.WAITING_RETRY,
                    failure_class=FailureClass.FRESHNESS_WAITING,
                )
                return self._result(
                    settled, CreativeRunTerminal.WAITING_RETRY, "freshness_not_ready"
                )
            settled = self._commands.settle_attempt(
                fence,
                outcome=AttemptOutcome.SUCCEEDED,
                terminal_status=TaskStatus.SUCCEEDED,
            )
            if task.projection_after == "plan" or task.chapter_index < task.target_chapters:
                next_chapter = 1 if task.projection_after == "plan" else task.chapter_index + 1
                draft = self._draft_task(settled, snapshot.snapshot_id, next_chapter)
                self._commands.create_task(draft)
                return self._result(draft, CreativeRunTerminal.PROGRESSED, "freshness_ready")
            return self._result(settled, CreativeRunTerminal.COMPLETED, "target_completed")
        raise ValueError(f"dispatcher cannot execute task kind: {task.kind.value}")

    def submit_acceptance(
        self,
        command: AcceptanceCommand,
        *,
        policy: CreativeRunPolicy,
    ) -> CreativeRunResult:
        receipt = self._acceptance.submit(command, policy=policy)
        task = self._commands.get_task(command.task_id)
        if receipt.accepted_binding is None:
            return self._result(task, CreativeRunTerminal.CANCELLED, "candidate_rejected")
        commit_task = commit_task_from_acceptance(task, receipt)
        self._commands.create_task(commit_task)
        return self._result(commit_task, CreativeRunTerminal.PROGRESSED, "candidate_accepted")

    def _accepted_binding(self, task: TaskRecord) -> AcceptedCandidateBinding:
        if len(task.input_artifact_refs) != 1:
            raise ValueError("commit task requires exactly one acceptance receipt")
        receipt = AcceptanceReceipt.model_validate_json(
            self._artifacts.read_verified(task.input_artifact_refs[0])
        )
        if receipt.accepted_binding is None:
            raise ValueError("rejected candidate cannot reach a commit task")
        return receipt.accepted_binding

    def _auto_accept(
        self, task: TaskRecord, candidate: CandidateBinding
    ) -> CreativeRunResult | None:
        policy = self._policy_resolver(task.policy_hash)
        enabled = (
            policy.auto_accept_plan
            if candidate.kind is CandidateKind.PLAN
            else policy.auto_accept_draft
        )
        if not enabled:
            return None
        identity = StableId(f"auto-accept.{candidate.candidate_id.root}"[:128])
        return self.submit_acceptance(
            AcceptanceCommand(
                command_id=identity,
                project_id=task.project_id,
                run_id=task.run_id,
                task_id=task.task_id,
                candidate=candidate,
                acceptance_policy_hash=policy.policy_hash,
                actor_kind=ActorKind.POLICY,
                actor_id="pinned-runtime-policy",
                decision=AcceptanceDecision.ACCEPT,
                reason="candidate accepted by pinned auto policy",
                expected_project_commit=task.basis_commit,
                idempotency_identity=identity,
                issued_at=datetime.now(UTC),
            ),
            policy=policy,
        )

    def _candidate_for_task(self, task: TaskRecord) -> CandidateBinding:
        if task.candidate_binding_ref is None:
            raise ValueError("acceptance task is missing its immutable candidate binding")
        return CandidateBinding.model_validate_json(
            self._artifacts.read_verified(task.candidate_binding_ref)
        )

    def _acceptance_task(self, previous: TaskRecord, candidate: CandidateBinding) -> TaskRecord:
        kind = (
            TaskKind.PLAN_ACCEPTANCE
            if candidate.kind is CandidateKind.PLAN
            else TaskKind.DRAFT_ACCEPTANCE
        )
        binding_ref = self._artifacts.put(
            canonical_json_bytes(candidate.model_dump(mode="json")),
            CANDIDATE_BINDING_MEDIA_TYPE,
            SchemaVersion("1.0.0"),
        )
        return TaskRecord(
            task_id=TaskId(f"{previous.task_id.root}.accept"),
            run_id=previous.run_id,
            project_id=previous.project_id,
            kind=kind,
            task_revision=0,
            status=TaskStatus.WAITING_INPUT,
            basis_commit=previous.basis_commit,
            basis_snapshot=previous.basis_snapshot,
            policy_hash=previous.policy_hash,
            permission_hash=previous.permission_hash,
            input_artifact_refs=(candidate.artifact_ref,),
            candidate_binding_ref=binding_ref,
            dependency_task_ids=(previous.task_id,),
            failure_budget=previous.failure_budget,
            chapter_index=previous.chapter_index,
            target_chapters=previous.target_chapters,
        )

    @staticmethod
    def _projection_task(previous: TaskRecord, commit_id: CommitId) -> TaskRecord:
        return TaskRecord(
            task_id=TaskId(f"{previous.task_id.root}.projection"),
            run_id=previous.run_id,
            project_id=previous.project_id,
            kind=TaskKind.PROJECTION_FRESHNESS,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=commit_id,
            policy_hash=previous.policy_hash,
            permission_hash=previous.permission_hash,
            dependency_task_ids=(previous.task_id,),
            failure_budget=previous.failure_budget,
            chapter_index=previous.chapter_index,
            target_chapters=previous.target_chapters,
            projection_after=("plan" if previous.kind is TaskKind.PLAN_COMMIT else "draft"),
        )

    @staticmethod
    def _draft_task(previous: TaskRecord, snapshot_id: StableId, chapter_index: int) -> TaskRecord:
        return TaskRecord(
            task_id=TaskId(f"{previous.run_id.root}.draft.{chapter_index}"),
            run_id=previous.run_id,
            project_id=previous.project_id,
            kind=TaskKind.DRAFT_CANDIDATE,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=previous.basis_commit,
            basis_snapshot=snapshot_id,
            policy_hash=previous.policy_hash,
            permission_hash=previous.permission_hash,
            dependency_task_ids=(previous.task_id,),
            failure_budget=previous.failure_budget,
            chapter_index=chapter_index,
            target_chapters=previous.target_chapters,
        )

    def _result(
        self, task: TaskRecord, terminal: CreativeRunTerminal, reason: str
    ) -> CreativeRunResult:
        return CreativeRunResult(
            run_id=task.run_id,
            project_id=task.project_id,
            terminal=terminal,
            current_task_id=task.task_id,
            current_attempt_id=task.current_attempt_id,
            basis_commit=task.basis_commit,
            current_commit=self._commits.current_commit(task.project_id),
            artifact_refs=task.terminal_artifact_refs,
            next_legal_commands=self._legal_commands(task),
            reason_code=reason,
        )

    @staticmethod
    def _legal_commands(task: TaskRecord) -> tuple[str, ...]:
        if task.status is TaskStatus.WAITING_INPUT:
            return ("accept", "reject", "cancel")
        if task.status is TaskStatus.WAITING_RETRY:
            return ("retry", "cancel")
        if task.status is TaskStatus.BLOCKED:
            return ("unblock", "cancel")
        if task.status in {TaskStatus.READY, TaskStatus.PENDING}:
            return ("advance", "pause", "cancel")
        return ()

    @staticmethod
    def _planner_failure(
        status: PlanningTerminalStatus,
    ) -> tuple[FailureClass, TaskStatus, CreativeRunTerminal]:
        if status is PlanningTerminalStatus.SUSPENDED:
            return (
                FailureClass.PROVIDER_TRANSIENT,
                TaskStatus.WAITING_RETRY,
                CreativeRunTerminal.WAITING_RETRY,
            )
        if status is PlanningTerminalStatus.REVIEW_REQUIRED:
            return (
                FailureClass.LEAF_REVIEW_REQUIRED,
                TaskStatus.BLOCKED,
                CreativeRunTerminal.REVIEW_REQUIRED,
            )
        return FailureClass.BASIS_CHANGED, TaskStatus.BLOCKED, CreativeRunTerminal.BLOCKED

    @staticmethod
    def _writer_failure(
        status: WritingLoopTerminalStatus,
    ) -> tuple[FailureClass, TaskStatus, CreativeRunTerminal]:
        if status in {
            WritingLoopTerminalStatus.MODEL_UNAVAILABLE,
            WritingLoopTerminalStatus.WRITER_FAILED,
        }:
            return (
                FailureClass.PROVIDER_TRANSIENT,
                TaskStatus.WAITING_RETRY,
                CreativeRunTerminal.WAITING_RETRY,
            )
        if status is WritingLoopTerminalStatus.BASIS_CHANGED:
            return FailureClass.BASIS_CHANGED, TaskStatus.BLOCKED, CreativeRunTerminal.BLOCKED
        return (
            FailureClass.LEAF_REVIEW_REQUIRED,
            TaskStatus.BLOCKED,
            CreativeRunTerminal.REVIEW_REQUIRED,
        )


__all__ = ["CreativeRuntimeService"]
