"""Thin fixed-topology Stage 5 application coordinator."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from novel_agent.domain.artifacts import ArtifactRef
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
    LookaheadRevalidationOutcome,
    LookaheadRevalidationReceipt,
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
    TaskPurpose,
    TaskRecord,
    TaskStatus,
)
from novel_agent.domain.writing_loop import WritingLoopTerminalStatus
from novel_agent.ports.creative_runtime import (
    CandidateMaterializer,
    PlanningLeafPort,
    RuntimeTaskReader,
    WritingLeafPort,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
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
        task_reader: RuntimeTaskReader | None = None,
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
        self._task_reader = task_reader

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
                purpose=task.purpose,
                chapter_index=task.chapter_index,
                horizon_start=task.horizon_start,
                horizon_end=task.horizon_end,
                protected_chapter_index=task.protected_chapter_index,
            )
            planning_result = await self._planner.run(planning_request)
            if planning_result.status is PlanningTerminalStatus.PLAN_CANDIDATE_READY:
                assert planning_result.candidate is not None
                candidate = planning_result.candidate.model_copy(
                    update={
                        "planning_purpose": task.purpose,
                        "horizon_start": task.horizon_start,
                        "horizon_end": task.horizon_end,
                        "protected_chapter_index": task.protected_chapter_index,
                    }
                )
                settled = self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.SUCCEEDED,
                    terminal_status=TaskStatus.SUCCEEDED,
                    artifact_refs=planning_result.artifact_refs,
                )
                waiting = self._acceptance_task(settled, candidate)
                self._commands.create_task(waiting)
                if task.purpose is TaskPurpose.LOOKAHEAD:
                    revalidated = self._revalidate_lookahead(waiting)
                    if revalidated is not None:
                        return revalidated
                automatic = self._auto_accept(waiting, candidate)
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
                observation = getattr(writing_result, "observation", None)
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
                    affects_future_plan=(
                        None
                        if observation is None
                        else bool(observation.changes)
                    ),
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
            if task.projection_after == "plan":
                next_chapter = task.chapter_index + 1
                draft = self._draft_task(settled, snapshot.snapshot_id, next_chapter)
                self._commands.create_task(draft)
                policy = self._policy_resolver(task.policy_hash)
                if policy.enable_planner_lookahead and next_chapter < task.target_chapters:
                    lookahead = self._lookahead_task(
                        settled,
                        snapshot.snapshot_id,
                        protected_chapter=next_chapter,
                        policy=policy,
                    )
                    self._commands.create_task(lookahead)
                    return self._result(
                        draft,
                        CreativeRunTerminal.PROGRESSED,
                        "freshness_ready_with_lookahead",
                    )
                return self._result(draft, CreativeRunTerminal.PROGRESSED, "freshness_ready")
            if task.chapter_index < task.target_chapters:
                policy = self._policy_resolver(task.policy_hash)
                if policy.enable_planner_lookahead:
                    revalidated = self._revalidate_lookahead(settled)
                    if revalidated is not None:
                        return revalidated
                    return self._result(
                        settled, CreativeRunTerminal.PROGRESSED, "lookahead_pending"
                    )
                draft = self._draft_task(
                    settled, snapshot.snapshot_id, task.chapter_index + 1
                )
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
        if command.candidate.planning_purpose is TaskPurpose.LOOKAHEAD:
            raise ValueError("lookahead candidate must pass post-Draft revalidation first")
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
        if candidate.planning_purpose is TaskPurpose.LOOKAHEAD:
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
            purpose=previous.purpose,
            horizon_start=previous.horizon_start,
            horizon_end=previous.horizon_end,
            protected_chapter_index=previous.protected_chapter_index,
            affects_future_plan=candidate.affects_future_plan,
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
            purpose=previous.purpose,
            horizon_start=previous.horizon_start,
            horizon_end=previous.horizon_end,
            protected_chapter_index=previous.protected_chapter_index,
            affects_future_plan=previous.affects_future_plan,
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

    def _lookahead_task(
        self,
        previous: TaskRecord,
        snapshot_id: StableId,
        *,
        protected_chapter: int,
        policy: CreativeRunPolicy,
    ) -> TaskRecord:
        if self._task_reader is None:
            raise RuntimeError("Planner lookahead requires the runtime task reader")
        inputs = next(
            (
                task.input_artifact_refs
                for task in self._task_reader.list_run(previous.run_id)
                if task.kind is TaskKind.PLAN_CANDIDATE
                and task.purpose is TaskPurpose.NORMAL
                and task.chapter_index == 0
            ),
            (),
        )
        horizon_start = protected_chapter + 1
        horizon_end = min(previous.target_chapters, horizon_start + policy.lookahead_horizon - 1)
        return TaskRecord(
            task_id=TaskId(f"{previous.run_id.root}.lookahead.after.{protected_chapter}"),
            run_id=previous.run_id,
            project_id=previous.project_id,
            kind=TaskKind.PLAN_CANDIDATE,
            purpose=TaskPurpose.LOOKAHEAD,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=previous.basis_commit,
            basis_snapshot=snapshot_id,
            policy_hash=previous.policy_hash,
            permission_hash=previous.permission_hash,
            input_artifact_refs=inputs,
            dependency_task_ids=(previous.task_id,),
            failure_budget=previous.failure_budget,
            chapter_index=protected_chapter,
            target_chapters=previous.target_chapters,
            horizon_start=horizon_start,
            horizon_end=horizon_end,
            protected_chapter_index=protected_chapter,
        )

    def _revalidate_lookahead(self, trigger: TaskRecord) -> CreativeRunResult | None:
        if self._task_reader is None:
            return None
        tasks = self._task_reader.list_run(trigger.run_id)
        current_commit = self._commits.current_commit(trigger.project_id)
        waiting = next(
            (
                task
                for task in reversed(tasks)
                if task.kind is TaskKind.PLAN_ACCEPTANCE
                and task.purpose is TaskPurpose.LOOKAHEAD
                and task.status is TaskStatus.WAITING_INPUT
                and not task.superseded
            ),
            None,
        )
        if waiting is None or waiting.basis_commit == current_commit:
            return None
        protected = waiting.protected_chapter_index
        assert protected is not None
        projection = next(
            (
                task
                for task in reversed(tasks)
                if task.kind is TaskKind.PROJECTION_FRESHNESS
                and task.projection_after == "draft"
                and task.chapter_index == protected
                and task.status is TaskStatus.SUCCEEDED
                and task.basis_commit == current_commit
            ),
            None,
        )
        if projection is None:
            return None
        snapshot = self._snapshots.get_for_commit(current_commit)
        if snapshot is None or snapshot.build_status.value != "exact":
            return None
        candidate = self._candidate_for_task(waiting)
        assert waiting.horizon_start is not None and waiting.horizon_end is not None
        if projection.affects_future_plan is False:
            outcome = LookaheadRevalidationOutcome.PROMOTED
            reason = "accepted Draft reported no future-Plan-affecting change"
        elif projection.affects_future_plan is True:
            outcome = LookaheadRevalidationOutcome.REPLAN_REQUIRED
            reason = "accepted Draft changed future-Plan-relevant state"
        else:
            outcome = LookaheadRevalidationOutcome.SUPERSEDED
            reason = "Draft impact was unavailable; stale lookahead cannot be promoted"
        receipt = LookaheadRevalidationReceipt(
            receipt_id=StableId(
                "lookahead-revalidation."
                + content_id(
                    {
                        "task": waiting.task_id.root,
                        "from": waiting.basis_commit.root,
                        "to": current_commit.root,
                        "outcome": outcome.value,
                    }
                ).root[-48:]
            ),
            run_id=waiting.run_id,
            lookahead_task_id=waiting.task_id,
            original_basis_commit=waiting.basis_commit,
            current_commit=current_commit,
            current_snapshot=snapshot.snapshot_id,
            protected_chapter_index=protected,
            horizon_start=waiting.horizon_start,
            horizon_end=waiting.horizon_end,
            affects_future_plan=projection.affects_future_plan,
            outcome=outcome,
            reason=reason,
        )
        receipt_ref = self._artifacts.put(
            canonical_json_bytes(receipt.model_dump(mode="json")),
            "application/vnd.novel-agent.lookahead-revalidation+json",
            SchemaVersion("1.0.0"),
        )
        self._commands.supersede_task(waiting.task_id, reason=reason)
        if outcome is LookaheadRevalidationOutcome.PROMOTED:
            promoted = candidate.model_copy(
                update={
                    "candidate_id": StableId(
                        f"{candidate.candidate_id.root}.promoted.{current_commit.root[-12:]}"[:128]
                    ),
                    "basis_commit": current_commit,
                    "basis_snapshot": snapshot.snapshot_id,
                    "lineage_artifact_refs": (
                        *candidate.lineage_artifact_refs,
                        receipt_ref,
                    ),
                    "planning_purpose": TaskPurpose.NORMAL,
                }
            )
            promoted_task = self._promoted_acceptance_task(
                waiting, projection, promoted, receipt_ref
            )
            self._commands.create_task(promoted_task)
            automatic = self._auto_accept(promoted_task, promoted)
            if automatic is not None:
                return automatic
            return self._result(
                promoted_task,
                CreativeRunTerminal.WAITING_PLAN_ACCEPTANCE,
                "lookahead_promoted",
            )
        parent_id = waiting.dependency_task_ids[0]
        parent = next(task for task in tasks if task.task_id == parent_id)
        replanned = TaskRecord(
            task_id=TaskId(f"{waiting.run_id.root}.replan.after.{protected}"),
            run_id=waiting.run_id,
            project_id=waiting.project_id,
            kind=TaskKind.PLAN_CANDIDATE,
            purpose=TaskPurpose.REPLAN,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=current_commit,
            basis_snapshot=snapshot.snapshot_id,
            policy_hash=waiting.policy_hash,
            permission_hash=waiting.permission_hash,
            input_artifact_refs=(*parent.input_artifact_refs, receipt_ref),
            dependency_task_ids=(projection.task_id,),
            failure_budget=waiting.failure_budget,
            chapter_index=protected,
            target_chapters=waiting.target_chapters,
            horizon_start=waiting.horizon_start,
            horizon_end=waiting.horizon_end,
            protected_chapter_index=protected,
        )
        self._commands.create_task(replanned)
        return self._result(
            replanned, CreativeRunTerminal.PROGRESSED, "lookahead_replan_required"
        )

    def _promoted_acceptance_task(
        self,
        waiting: TaskRecord,
        projection: TaskRecord,
        candidate: CandidateBinding,
        receipt_ref: ArtifactRef,
    ) -> TaskRecord:
        binding_ref = self._artifacts.put(
            canonical_json_bytes(candidate.model_dump(mode="json")),
            CANDIDATE_BINDING_MEDIA_TYPE,
            SchemaVersion("1.0.0"),
        )
        return TaskRecord(
            task_id=TaskId(f"{waiting.task_id.root}.promoted"),
            run_id=waiting.run_id,
            project_id=waiting.project_id,
            kind=TaskKind.PLAN_ACCEPTANCE,
            purpose=TaskPurpose.NORMAL,
            task_revision=0,
            status=TaskStatus.WAITING_INPUT,
            basis_commit=projection.basis_commit,
            basis_snapshot=candidate.basis_snapshot,
            policy_hash=waiting.policy_hash,
            permission_hash=waiting.permission_hash,
            input_artifact_refs=(candidate.artifact_ref,),
            candidate_binding_ref=binding_ref,
            dependency_task_ids=(projection.task_id,),
            terminal_artifact_refs=(receipt_ref,),
            failure_budget=waiting.failure_budget,
            chapter_index=waiting.chapter_index,
            target_chapters=waiting.target_chapters,
            horizon_start=waiting.horizon_start,
            horizon_end=waiting.horizon_end,
            protected_chapter_index=waiting.protected_chapter_index,
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
            if task.purpose is TaskPurpose.LOOKAHEAD:
                return ("wait_for_revalidation", "cancel")
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
