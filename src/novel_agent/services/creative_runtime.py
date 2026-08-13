"""Thin fixed-topology Stage 5 application coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast

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
    PlanningLoopResult,
    PlanningTerminalStatus,
    commit_task_from_acceptance,
)
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId, TaskId
from novel_agent.domain.memory import FreshnessMode, FreshnessRequest, FreshnessStatus
from novel_agent.domain.memory_write import MemoryWriteWorkflowResult, MemoryWriteWorkflowStatus
from novel_agent.domain.runtime import (
    AttemptFence,
    AttemptOutcome,
    EffectReceipt,
    EffectStatus,
    FailureClass,
    TaskKind,
    TaskPurpose,
    TaskRecord,
    TaskStatus,
)
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.ports.creative_runtime import (
    CandidateMaterializationError,
    CandidateMaterializer,
    ChapterSettlementPort,
    PlanningLeafPort,
    RuntimeTaskReader,
    WritingLeafPort,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService, manifest_commit_id
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.projection import (
    DerivedProjectionService,
    DerivedSnapshotRepository,
    FreshnessGate,
)
from novel_agent.services.runtime_acceptance import RuntimeAcceptanceService
from novel_agent.services.runtime_commands import RuntimeCommandService

CANDIDATE_BINDING_MEDIA_TYPE = "application/vnd.novel-agent.stage5-candidate-binding+json"
WRITING_LOOP_RESULT_MEDIA_TYPE = "application/vnd.novel-agent.writing-loop-result+json"
CHAPTER_SETTLEMENT_EXTERNAL_SYSTEM = "stage2w.chapter_reveal_atomic"
CHAPTER_SETTLEMENT_RECONCILIATION_MEDIA_TYPE = (
    "application/vnd.novel-agent.chapter-settlement-reconciliation+json"
)


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
        chapter_settlement: ChapterSettlementPort | None = None,
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
        self._chapter_settlement = chapter_settlement

    @property
    def writing_request_factory(self) -> Callable[[TaskRecord], WritingLoopRequest]:
        return self._writing_request_factory

    @property
    def planner_leaf(self) -> PlanningLeafPort:
        return self._planner

    @property
    def writer_leaf(self) -> WritingLeafPort:
        return self._writer

    @property
    def plan_materializer(self) -> CandidateMaterializer:
        return self._plan_materializer

    @property
    def draft_materializer(self) -> CandidateMaterializer:
        return self._draft_materializer

    @property
    def chapter_settlement(self) -> ChapterSettlementPort | None:
        return self._chapter_settlement

    def start(self, request: CreativeRunRequest) -> CreativeRunResult:
        task = self._commands.create_run_and_initial_task(request)
        return self._result(task, CreativeRunTerminal.PROGRESSED, "run_created")

    def recover_boundary(self, task_id: TaskId) -> CreativeRunResult | None:
        """Repair one durable local-runtime boundary without redispatching leaf work."""

        task = self._commands.get_task(task_id)
        if (
            task.kind in {TaskKind.PLAN_ACCEPTANCE, TaskKind.DRAFT_ACCEPTANCE}
            and task.status is TaskStatus.WAITING_INPUT
        ):
            return self._auto_accept(task, self._candidate_for_task(task))
        if (
            task.kind is TaskKind.DRAFT_COMMIT
            and task.current_attempt_id is not None
            and self._chapter_settlement is not None
        ):
            return self._recover_chapter_settlement(task)
        if (
            task.kind is TaskKind.PROJECTION_FRESHNESS
            and task.projection_after == "draft"
            and task.status is TaskStatus.SUCCEEDED
        ):
            return self._repair_post_draft_projection(task)
        return None

    def _recover_chapter_settlement(self, task: TaskRecord) -> CreativeRunResult:
        assert self._chapter_settlement is not None
        accepted = self._accepted_binding(task)
        prior = self._commands.effect_for_current_attempt(
            task.task_id, external_system=CHAPTER_SETTLEMENT_EXTERNAL_SYSTEM
        )
        commit_result = self._chapter_settlement.resolve_commit(accepted)
        if commit_result is not None and commit_result.status is CommitStatus.ACCEPTED:
            if commit_result.commit_id is None:
                raise RuntimeError("accepted Chapter Settlement receipt has no commit")
            if prior is None:
                pending = self._commands.mark_recovery_pending(
                    task.task_id,
                    command_id=StableId(f"recovery-pending.{task.task_id.root}"[:128]),
                    actor_id="vertical-runner",
                    reason="accepted Chapter Settlement has no Stage 5 outer effect",
                    observed_revision=task.task_revision,
                )
                return self._result(
                    pending,
                    CreativeRunTerminal.RECOVERY_PENDING,
                    "chapter_settlement_effect_missing",
                )
            result_ref = self._artifacts.put(
                canonical_json_bytes(commit_result.model_dump(mode="json")),
                CHAPTER_SETTLEMENT_RECONCILIATION_MEDIA_TYPE,
                SchemaVersion("1.0.0"),
            )
            completed = prior.model_copy(
                update={
                    "status": EffectStatus.COMPLETED,
                    "provider_request_id": commit_result.commit_id.root,
                    "result_artifact_ref": result_ref,
                    "completed_at": datetime.now(UTC),
                }
            )
            projection = self._projection_task(task, commit_result.commit_id)
            self._commands.reconcile_external_commit(
                task.task_id,
                commit_result.commit_id,
                commits=self._commits,
                effect_receipt=completed,
                successor_tasks=(projection,),
                artifact_refs=(result_ref,),
                observed_revision=task.task_revision,
            )
            return self._result(
                projection,
                CreativeRunTerminal.PROGRESSED,
                "chapter_settlement_reconciled",
            )
        if prior is not None and prior.status in {
            EffectStatus.REQUESTED,
            EffectStatus.UNCERTAIN,
        }:
            compensated = prior.model_copy(
                update={
                    "status": EffectStatus.COMPENSATED,
                    "completed_at": datetime.now(UTC),
                }
            )
            self._commands.reconcile_effect(
                task.task_id,
                compensated,
                command_id=StableId(f"reconcile.{prior.effect_identity.root}"[:128]),
                observed_revision=task.task_revision,
            )
        elif prior is not None and prior.status is EffectStatus.COMPLETED:
            pending = self._commands.mark_recovery_pending(
                task.task_id,
                command_id=StableId(f"recovery-pending.{task.task_id.root}"[:128]),
                actor_id="vertical-runner",
                reason="completed Chapter Settlement effect has no accepted Commit receipt",
                observed_revision=task.task_revision,
            )
            return self._result(
                pending,
                CreativeRunTerminal.RECOVERY_PENDING,
                "chapter_settlement_receipt_inconsistent",
            )
        deterministic_failure = commit_result is not None
        settled = self._commands.operator_reconcile_attempt(
            task.task_id,
            command_id=StableId(f"retry-interrupted.{task.task_id.root}"[:128]),
            actor_id="vertical-runner",
            reason=(
                "Chapter Settlement did not commit before the local worker stopped"
                if commit_result is None
                else f"Chapter Settlement returned {commit_result.status.value}"
            ),
            terminal_status=(
                TaskStatus.BLOCKED if deterministic_failure else TaskStatus.WAITING_RETRY
            ),
            failure_class=(
                FailureClass.COMMIT_CONFLICT
                if deterministic_failure
                else FailureClass.WORKER_STARTUP
            ),
            observed_revision=task.task_revision,
        )
        return self._result(
            settled,
            (
                CreativeRunTerminal.BLOCKED
                if deterministic_failure
                else CreativeRunTerminal.WAITING_RETRY
            ),
            (
                "chapter_settlement_receipt_rejected"
                if deterministic_failure
                else "chapter_settlement_safe_to_retry"
            ),
        )

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
        attempt, fence = self._commands.claim(
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
                continuation_artifact_refs=task.terminal_artifact_refs,
                planner_memory_budget_extensions=task.planner_memory_budget_extensions,
                purpose=task.purpose,
                chapter_index=task.chapter_index,
                horizon_start=task.horizon_start,
                horizon_end=task.horizon_end,
                protected_chapter_index=task.protected_chapter_index,
            )
            planning_result = cast(
                PlanningLoopResult,
                await self._await_with_heartbeat(
                    fence,
                    self._planner.run(planning_request),
                ),
            )
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
                waiting = self._acceptance_task(task, candidate)
                self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.SUCCEEDED,
                    terminal_status=TaskStatus.SUCCEEDED,
                    artifact_refs=planning_result.artifact_refs,
                    successor_tasks=(waiting,),
                )
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
            if planning_result.status is PlanningTerminalStatus.YIELDED:
                slice_yields = {
                    "INQUIRY_REVISION_SLICE_EXHAUSTED",
                    "PLAN_REVISION_SLICE_EXHAUSTED",
                    "REVIEWER_MEMORY_SLICE_EXHAUSTED",
                    "PLANNER_MEMORY_SLICE_EXHAUSTED",
                    "MODEL_TOKEN_SLICE_EXHAUSTED",
                }
                budget_wait = planning_result.failure_code not in slice_yields
                settled = self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.SUSPENDED,
                    terminal_status=(
                        TaskStatus.BUDGET_REVIEW if budget_wait else TaskStatus.READY
                    ),
                    artifact_refs=planning_result.artifact_refs,
                )
                return self._result(
                    settled,
                    (
                        CreativeRunTerminal.BUDGET_REVIEW
                        if budget_wait
                        else CreativeRunTerminal.PROGRESSED
                    ),
                    planning_result.failure_code or "planner_yielded",
                )
            failure, status, terminal = self._planner_failure(planning_result.status)
            settled = self._commands.settle_attempt(
                fence,
                outcome=AttemptOutcome.SUSPENDED,
                terminal_status=status,
                artifact_refs=planning_result.artifact_refs,
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
            writing_result = cast(
                WritingLoopResult,
                await self._await_with_heartbeat(
                    fence,
                    self._writer.run(writing_request),
                ),
            )
            if isinstance(writing_result, WritingLoopResult):
                result_ref = self._artifacts.put(
                    canonical_json_bytes(writing_result.model_dump(mode="json")),
                    WRITING_LOOP_RESULT_MEDIA_TYPE,
                    SchemaVersion("1.0.0"),
                )
                result_artifacts = tuple(dict.fromkeys((*writing_result.artifacts, result_ref)))
            else:  # Backward-compatible isolated fault fixtures.
                result_artifacts = writing_result.artifacts
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
                    lineage_artifact_refs=result_artifacts,
                    affects_future_plan=(
                        None if observation is None else bool(observation.changes)
                    ),
                )
                waiting = self._acceptance_task(task, candidate)
                self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.SUCCEEDED,
                    terminal_status=TaskStatus.SUCCEEDED,
                    artifact_refs=result_artifacts,
                    successor_tasks=(waiting,),
                )
                automatic = self._auto_accept(waiting, candidate)
                if automatic is not None:
                    return automatic
                return self._result(
                    waiting,
                    CreativeRunTerminal.WAITING_DRAFT_ACCEPTANCE,
                    "draft_candidate_ready",
                )
            if writing_result.status is WritingLoopTerminalStatus.YIELDED:
                settled = self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.SUSPENDED,
                    terminal_status=TaskStatus.READY,
                    artifact_refs=result_artifacts,
                )
                return self._result(
                    settled,
                    CreativeRunTerminal.PROGRESSED,
                    "writer_yielded",
                )
            if (
                writing_result.status
                is WritingLoopTerminalStatus.MEMORY_BUDGET_EXHAUSTED
            ):
                settled = self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.SUSPENDED,
                    terminal_status=TaskStatus.BUDGET_REVIEW,
                    artifact_refs=result_artifacts,
                    failure_class=FailureClass.BUDGET_EXHAUSTED,
                )
                return self._result(
                    settled,
                    CreativeRunTerminal.BUDGET_REVIEW,
                    "writer_memory_budget_exhausted",
                )
            failure, status, terminal = self._writer_failure(writing_result.status)
            settled = self._commands.settle_attempt(
                fence,
                outcome=AttemptOutcome.SUSPENDED,
                terminal_status=status,
                artifact_refs=result_artifacts,
                failure_class=failure,
            )
            return self._result(settled, terminal, writing_result.status.value.lower())
        if task.kind in {TaskKind.PLAN_COMMIT, TaskKind.DRAFT_COMMIT}:
            fence = self._commands.claim_writer_lane(fence)
            accepted = self._accepted_binding(task)
            if task.kind is TaskKind.DRAFT_COMMIT and self._chapter_settlement is not None:
                settlement_identity = self._chapter_settlement.effect_identity(accepted)
                attempt_suffix = f".attempt.{attempt.attempt_no}"
                effect_identity = StableId(
                    f"{settlement_identity.root[: 128 - len(attempt_suffix)]}{attempt_suffix}"
                )
                requested_effect = EffectReceipt(
                    effect_identity=effect_identity,
                    external_system=CHAPTER_SETTLEMENT_EXTERNAL_SYSTEM,
                    request_identity=effect_identity,
                    status=EffectStatus.REQUESTED,
                    attempt_no=attempt.attempt_no,
                )
                self._commands.record_effect_requested(fence, requested_effect)
                try:
                    settlement = cast(
                        MemoryWriteWorkflowResult,
                        await self._await_with_heartbeat(
                            fence,
                            self._chapter_settlement.settle(accepted),
                        ),
                    )
                except (CandidateMaterializationError, ValueError):
                    self._commands.record_effect_terminal(
                        fence,
                        requested_effect.model_copy(
                            update={
                                "status": EffectStatus.COMPENSATED,
                                "completed_at": datetime.now(UTC),
                            }
                        ),
                    )
                    settled = self._commands.settle_attempt(
                        fence,
                        outcome=AttemptOutcome.FAILED,
                        terminal_status=TaskStatus.BLOCKED,
                        failure_class=FailureClass.VALIDATION_REJECTED,
                    )
                    return self._result(
                        settled,
                        CreativeRunTerminal.REVIEW_REQUIRED,
                        "chapter_settlement_rejected",
                    )
                settlement_refs = self._settlement_refs(settlement)
                if settlement.canonical_commit_accepted:
                    assert settlement.resulting_commit is not None
                    completed_effect = requested_effect.model_copy(
                        update={
                            "status": EffectStatus.COMPLETED,
                            "provider_request_id": settlement.resulting_commit.root,
                            "result_artifact_ref": settlement.commit_receipt,
                            "completed_at": datetime.now(UTC),
                        }
                    )
                    projection_task = self._projection_task(
                        task, settlement.resulting_commit
                    )
                    self._commands.record_external_commit(
                        fence,
                        settlement.resulting_commit,
                        commits=self._commits,
                        artifact_refs=settlement_refs,
                        effect_receipt=completed_effect,
                        successor_tasks=(projection_task,),
                    )
                    return self._result(
                        projection_task,
                        CreativeRunTerminal.PROGRESSED,
                        "chapter_settlement_committed",
                    )
                self._commands.record_effect_terminal(
                    fence,
                    requested_effect.model_copy(
                        update={
                            "status": EffectStatus.COMPENSATED,
                            "result_artifact_ref": settlement.checkpoint_ref,
                            "completed_at": datetime.now(UTC),
                        }
                    ),
                )
                retryable = settlement.status is MemoryWriteWorkflowStatus.SUSPENDED
                settled = self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.SUSPENDED if retryable else AttemptOutcome.FAILED,
                    terminal_status=(
                        TaskStatus.WAITING_RETRY if retryable else TaskStatus.BLOCKED
                    ),
                    artifact_refs=settlement_refs,
                    failure_class=(
                        FailureClass.PROVIDER_TRANSIENT
                        if retryable
                        else FailureClass.LEAF_REVIEW_REQUIRED
                    ),
                )
                return self._result(
                    settled,
                    CreativeRunTerminal.WAITING_RETRY
                    if retryable
                    else CreativeRunTerminal.REVIEW_REQUIRED,
                    f"chapter_settlement_{settlement.status.value}",
                )
            materializer = (
                self._plan_materializer
                if task.kind is TaskKind.PLAN_COMMIT
                else self._draft_materializer
            )
            try:
                bundle, report = materializer.materialize(accepted)
            except CandidateMaterializationError:
                settled = self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.FAILED,
                    terminal_status=TaskStatus.BLOCKED,
                    failure_class=FailureClass.VALIDATION_REJECTED,
                )
                return self._result(
                    settled,
                    CreativeRunTerminal.REVIEW_REQUIRED,
                    "candidate_materialization_rejected",
                )
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
            predicted_commit = manifest_commit_id(commit_request.bundle.proposed_roots)
            projection_task = self._projection_task(task, predicted_commit)
            commit_result = self._commands.commit_accepted_candidate(
                fence,
                commit_request,
                self._commits,
                successor_tasks=(projection_task,),
            )
            settled = self._commands.get_task(task.task_id)
            if commit_result.status is not CommitStatus.ACCEPTED or commit_result.commit_id is None:
                return self._result(
                    settled, CreativeRunTerminal.BLOCKED, commit_result.status.value
                )
            if commit_result.commit_id != predicted_commit:
                raise RuntimeError("accepted commit identity differs from its proposed roots")
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
            if task.projection_after == "plan":
                next_chapter = task.chapter_index + 1
                draft = self._draft_task(task, snapshot.snapshot_id, next_chapter)
                policy = self._policy_resolver(task.policy_hash)
                if policy.enable_planner_lookahead and next_chapter < task.target_chapters:
                    lookahead = self._lookahead_task(
                        task,
                        snapshot.snapshot_id,
                        protected_chapter=next_chapter,
                        policy=policy,
                    )
                    self._commands.settle_attempt(
                        fence,
                        outcome=AttemptOutcome.SUCCEEDED,
                        terminal_status=TaskStatus.SUCCEEDED,
                        successor_tasks=(draft, lookahead),
                    )
                    return self._result(
                        draft,
                        CreativeRunTerminal.PROGRESSED,
                        "freshness_ready_with_lookahead",
                    )
                self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.SUCCEEDED,
                    terminal_status=TaskStatus.SUCCEEDED,
                    successor_tasks=(draft,),
                )
                return self._result(draft, CreativeRunTerminal.PROGRESSED, "freshness_ready")
            if task.chapter_index < task.target_chapters:
                policy = self._policy_resolver(task.policy_hash)
                if policy.enable_planner_lookahead:
                    settled = self._commands.settle_attempt(
                        fence,
                        outcome=AttemptOutcome.SUCCEEDED,
                        terminal_status=TaskStatus.SUCCEEDED,
                    )
                    repaired = self._repair_post_draft_projection(settled)
                    if repaired is not None:
                        return repaired
                    return self._result(
                        settled, CreativeRunTerminal.PROGRESSED, "lookahead_pending"
                    )
                if task.horizon_end is not None and task.chapter_index >= task.horizon_end:
                    planning = self._rolling_plan_task(
                        task,
                        snapshot.snapshot_id,
                        policy=policy,
                    )
                    self._commands.settle_attempt(
                        fence,
                        outcome=AttemptOutcome.SUCCEEDED,
                        terminal_status=TaskStatus.SUCCEEDED,
                        successor_tasks=(planning,),
                    )
                    return self._result(
                        planning,
                        CreativeRunTerminal.PROGRESSED,
                        "planning_horizon_advanced",
                    )
                draft = self._draft_task(task, snapshot.snapshot_id, task.chapter_index + 1)
                self._commands.settle_attempt(
                    fence,
                    outcome=AttemptOutcome.SUCCEEDED,
                    terminal_status=TaskStatus.SUCCEEDED,
                    successor_tasks=(draft,),
                )
                return self._result(draft, CreativeRunTerminal.PROGRESSED, "freshness_ready")
            settled = self._commands.settle_attempt(
                fence,
                outcome=AttemptOutcome.SUCCEEDED,
                terminal_status=TaskStatus.SUCCEEDED,
            )
            return self._result(settled, CreativeRunTerminal.COMPLETED, "target_completed")
        raise ValueError(f"dispatcher cannot execute task kind: {task.kind.value}")

    async def _await_with_heartbeat(
        self,
        fence: AttemptFence,
        operation: Awaitable[object],
    ) -> object:
        """Await one long leaf operation while renewing its durable Attempt lease."""

        work = asyncio.ensure_future(operation)
        while True:
            done, _pending = await asyncio.wait(
                {work},
                timeout=self._commands.heartbeat_interval_seconds,
            )
            if done:
                return work.result()
            self._commands.heartbeat(fence)

    @staticmethod
    def _settlement_refs(settlement: object) -> tuple[ArtifactRef, ...]:
        refs = [
            getattr(settlement, name)
            for name in (
                "validation_receipt",
                "guardian_receipt",
                "commit_receipt",
                "projection_receipt_ref",
                "freshness_receipt_ref",
                "checkpoint_ref",
            )
        ]
        refs.extend(getattr(settlement, "quarantine_refs", ()))
        return tuple(dict.fromkeys(ref for ref in refs if isinstance(ref, ArtifactRef)))

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
        commit_task = self._commands.get_task(commit_task.task_id)
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
            failure_budget=previous.retry_tranche_size,
            retry_tranche_size=previous.retry_tranche_size,
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
            failure_budget=previous.retry_tranche_size,
            retry_tranche_size=previous.retry_tranche_size,
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
            failure_budget=previous.retry_tranche_size,
            retry_tranche_size=previous.retry_tranche_size,
            chapter_index=chapter_index,
            target_chapters=previous.target_chapters,
            horizon_start=previous.horizon_start,
            horizon_end=previous.horizon_end,
        )

    def _rolling_plan_task(
        self,
        previous: TaskRecord,
        snapshot_id: StableId,
        *,
        policy: CreativeRunPolicy,
    ) -> TaskRecord:
        horizon_start = previous.chapter_index + 1
        horizon_end = min(
            previous.target_chapters,
            previous.chapter_index + policy.planning_horizon,
        )
        return TaskRecord(
            task_id=TaskId(
                f"{previous.run_id.root}.plan.{horizon_start}-{horizon_end}"
            ),
            run_id=previous.run_id,
            project_id=previous.project_id,
            kind=TaskKind.PLAN_CANDIDATE,
            purpose=TaskPurpose.NORMAL,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=previous.basis_commit,
            basis_snapshot=snapshot_id,
            policy_hash=previous.policy_hash,
            permission_hash=previous.permission_hash,
            input_artifact_refs=self._planning_inputs(previous),
            dependency_task_ids=(previous.task_id,),
            failure_budget=previous.retry_tranche_size,
            retry_tranche_size=previous.retry_tranche_size,
            chapter_index=previous.chapter_index,
            target_chapters=previous.target_chapters,
            horizon_start=horizon_start,
            horizon_end=horizon_end,
        )

    def _planning_inputs(self, previous: TaskRecord) -> tuple[ArtifactRef, ...]:
        if self._task_reader is None:
            raise RuntimeError("rolling Planner work requires the runtime task reader")
        initial = min(
            (
                task
                for task in self._task_reader.list_run(previous.run_id)
                if task.kind is TaskKind.PLAN_CANDIDATE
                and task.purpose is TaskPurpose.NORMAL
            ),
            key=lambda task: (task.chapter_index, task.task_id.root),
            default=None,
        )
        if initial is None:
            raise RuntimeError("run has no normal Planner input owner")
        return initial.input_artifact_refs

    def _lookahead_task(
        self,
        previous: TaskRecord,
        snapshot_id: StableId,
        *,
        protected_chapter: int,
        policy: CreativeRunPolicy,
    ) -> TaskRecord:
        inputs = self._planning_inputs(previous)
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
            failure_budget=previous.retry_tranche_size,
            retry_tranche_size=previous.retry_tranche_size,
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
            failure_budget=waiting.retry_tranche_size,
            retry_tranche_size=waiting.retry_tranche_size,
            chapter_index=protected,
            target_chapters=waiting.target_chapters,
            horizon_start=waiting.horizon_start,
            horizon_end=waiting.horizon_end,
            protected_chapter_index=protected,
        )
        self._commands.create_task(replanned)
        return self._result(replanned, CreativeRunTerminal.PROGRESSED, "lookahead_replan_required")

    def _repair_post_draft_projection(
        self, projection: TaskRecord
    ) -> CreativeRunResult | None:
        policy = self._policy_resolver(projection.policy_hash)
        if (
            not policy.enable_planner_lookahead
            or self._task_reader is None
            or projection.chapter_index >= projection.target_chapters
        ):
            return None
        revalidated = self._revalidate_lookahead(projection)
        if revalidated is not None:
            return revalidated
        tasks = self._task_reader.list_run(projection.run_id)
        existing = next(
            (
                task
                for task in reversed(tasks)
                if projection.task_id in task.dependency_task_ids
                and task.kind in {TaskKind.PLAN_CANDIDATE, TaskKind.PLAN_ACCEPTANCE}
                and task.purpose is not TaskPurpose.LOOKAHEAD
                and not task.superseded
                and task.status not in {
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.BLOCKED,
                }
            ),
            None,
        )
        if existing is not None:
            return None
        lookahead = tuple(
            task
            for task in tasks
            if task.purpose is TaskPurpose.LOOKAHEAD
            and task.protected_chapter_index == projection.chapter_index
            and not task.superseded
        )
        if any(
            task.status in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING}
            for task in lookahead
        ):
            return None
        for task in lookahead:
            if task.status in {
                TaskStatus.PENDING,
                TaskStatus.READY,
                TaskStatus.WAITING_INPUT,
                TaskStatus.WAITING_RETRY,
                TaskStatus.BUDGET_REVIEW,
                TaskStatus.BLOCKED,
            }:
                self._commands.supersede_task(
                    task.task_id,
                    reason="lookahead cannot provide the next foreground Plan",
                )
        snapshot = self._snapshots.get_for_commit(projection.basis_commit)
        if snapshot is None or snapshot.build_status.value != "exact":
            return None
        planning = self._rolling_plan_task(
            projection,
            snapshot.snapshot_id,
            policy=policy,
        )
        self._commands.create_task(planning)
        return self._result(
            planning,
            CreativeRunTerminal.PROGRESSED,
            "lookahead_fallback_to_rolling_plan",
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
            failure_budget=waiting.retry_tranche_size,
            retry_tranche_size=waiting.retry_tranche_size,
            chapter_index=waiting.chapter_index,
            target_chapters=waiting.target_chapters,
            horizon_start=waiting.horizon_start,
            horizon_end=waiting.horizon_end,
            protected_chapter_index=waiting.protected_chapter_index,
        )

    def _result(
        self, task: TaskRecord, terminal: CreativeRunTerminal, reason: str
    ) -> CreativeRunResult:
        if task.status is TaskStatus.BUDGET_REVIEW:
            if terminal is not CreativeRunTerminal.BUDGET_REVIEW:
                reason = "task_retry_budget_exhausted"
            terminal = CreativeRunTerminal.BUDGET_REVIEW
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
        if task.status is TaskStatus.BUDGET_REVIEW:
            return ("extend_budget", "cancel")
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
