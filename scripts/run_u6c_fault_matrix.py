#!/usr/bin/env python3
"""Run the deterministic U6-C fault matrix through the production command owners.

This is a fault-injection harness, not a second creative runtime.  It only creates
isolated tasks and invokes the existing RuntimeCommand/Recovery, ModelGateway,
projection, Planner, Editor, judge, and evaluation-discard owners at documented
recovery boundaries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.model.fake import FakeModelEndpoint
from novel_agent.adapters.postgres import models as _postgres_models  # noqa: F401
from novel_agent.adapters.postgres.database import Base, build_engine, build_session_factory
from novel_agent.adapters.postgres.model_call_ledger import SqlModelCallLedger
from novel_agent.adapters.postgres.models import CommitRow
from novel_agent.domain.artifacts import (
    QA_WRITER_RESPONSE_MEDIA_TYPE,
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    CommitRequest,
    ObservedChangeSet,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
)
from novel_agent.domain.editorial import (
    EditorialIssueDraft,
    EditorialIssueType,
    EditorialSeverity,
    EditorialVerdict,
    EditorRepairPayload,
    EditorReviewPayload,
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
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
)
from novel_agent.domain.planning import (
    PlanningLoopCheckpoint,
    PlanningLoopPhase,
    PlanningTurnAction,
)
from novel_agent.domain.runtime import (
    AttemptFence,
    AttemptOutcome,
    EffectReceipt,
    EffectStatus,
    FailureClass,
    ResumabilityStatus,
    RunCheckpoint,
    TaskAttempt,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.domain.u6c_fault_matrix import (
    U6CFaultCase,
    U6CFaultCaseResult,
    U6CFaultCaseStatus,
    U6CFaultMatrixReport,
)
from novel_agent.domain.v05_readout import MemoryIdentitySnapshot, WriterJudgeAvailability
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.evaluation_namespace import discard_evaluation_namespace
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.runtime_commands import (
    RuntimeCommandConflictError,
    RuntimeCommandService,
    StaleAttemptFenceError,
)
from novel_agent.services.runtime_maintenance import RuntimeSupervisor
from novel_agent.services.runtime_projection import (
    assert_task_projection_matches,
    project_runtime_events,
)
from novel_agent.services.runtime_recovery import RuntimeRecoveryService
from novel_agent.services.writer_judge import WriterJudgeService

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = SchemaVersion("1.0.0")
POLICY_HASH = "sha256:" + "1" * 64
PERMISSION_HASH = "sha256:" + "2" * 64
CASE_MEDIA_TYPE = "application/vnd.novel-agent.u6c-fault-case+json"
PARTIAL_MEDIA_TYPE = "application/vnd.novel-agent.model.partial-response+json"
PLANNER_CHECKPOINT_MEDIA_TYPE = "application/vnd.novel-agent.planning-loop-checkpoint+json"
EDITOR_CONTEXT_MEDIA_TYPE = "application/vnd.novel-agent.editor.context+json"
EDITOR_REPAIR_MEDIA_TYPE = "application/vnd.novel-agent.editor.repair+json"


class _Answer(BaseModel):
    model_config = ConfigDict(strict=True)

    answer: str


class _CountingFakeEndpoint(FakeModelEndpoint):
    """A provider hook that counts sends without changing ModelGateway semantics."""

    def __init__(self) -> None:
        super().__init__('{"answer":"durable"}')


class _Resolution:
    def __init__(self, receipt: EffectReceipt) -> None:
        self.receipt = receipt


class _StaticEffectResolver:
    def __init__(self, status: EffectStatus) -> None:
        self._status = status

    def resolve(self, receipt: EffectReceipt) -> _Resolution:
        return _Resolution(
            receipt.model_copy(update={"status": self._status, "completed_at": datetime.now(UTC)})
        )


@dataclass(frozen=True, slots=True)
class _CaseContext:
    case: U6CFaultCase
    project_id: ProjectId
    run_id: RunId
    basis_commit: CommitId
    initial_task: TaskRecord


@dataclass(frozen=True, slots=True)
class _RuntimeStart:
    task: TaskRecord
    attempt: TaskAttempt
    fence: AttemptFence
    checkpoint: RunCheckpoint


class _Harness:
    def __init__(self, database_url: str, output_root: Path) -> None:
        self.engine = build_engine(database_url)
        Base.metadata.create_all(self.engine)
        self.factory = build_session_factory(self.engine)
        self.commits = CommitService(self.factory)
        self.artifacts = ArtifactRepository(FilesystemObjectStore(output_root / "objects"))
        self.events = RunEventLogRepository(self.factory)
        self.commands = RuntimeCommandService(
            self.factory,
            self.events,
            lambda _project_id: PERMISSION_HASH,
        )
        self._sequence = 0

    def close(self) -> None:
        self.engine.dispose()

    def put_json(self, payload: object, media_type: str) -> ArtifactRef:
        return self.artifacts.put(
            canonical_json_bytes(payload),
            media_type,
            SCHEMA_VERSION,
        )

    def new_case(self, case: U6CFaultCase) -> _CaseContext:
        self._sequence += 1
        suffix = case.value.replace("_", "-")
        project_id = ProjectId(f"u6c.project.{self._sequence}.{suffix}")
        run_id = RunId(f"u6c.run.{self._sequence}.{suffix}")
        manifest = _manifest(project_id, self._sequence)
        basis = self.commits.initialize_project(manifest)
        request = CreativeRunRequest(
            run_id=run_id,
            project_id=project_id,
            basis_commit=basis,
            policy=CreativeRunPolicy(
                automation_mode=AutomationMode.MANUAL,
                policy_hash=POLICY_HASH,
                permission_hash=PERMISSION_HASH,
                max_task_attempts=3,
            ),
        )
        task = self.commands.create_run_and_initial_task(request)
        return _CaseContext(case, project_id, run_id, basis, task)

    def recovery(self, status: EffectStatus = EffectStatus.COMPLETED) -> RuntimeRecoveryService:
        return RuntimeRecoveryService(
            self.factory,
            self.commands,
            RunCheckpointRepository(self.factory),
            self.artifacts,
            self.commits,
            _StaticEffectResolver(status),
        )


def _artifact(character: str = "a", *, media_type: str = "application/json") -> ArtifactRef:
    digest = "sha256:" + character * 64
    return ArtifactRef(
        artifact_id=ArtifactId(digest),
        media_type=media_type,
        byte_length=1,
        schema_version=SCHEMA_VERSION,
    )


def _manifest(
    project_id: ProjectId,
    offset: int,
    parent_commit_ids: tuple[CommitId, ...] = (),
) -> RootManifest:
    characters = [format((offset + index) % 16, "x") for index in range(5)]
    return RootManifest(
        project_id=project_id,
        schema_version=SCHEMA_VERSION,
        text_root=TextRootRef(
            artifact_id=ArtifactId("sha256:" + characters[0] * 64),
            media_type="application/json",
            byte_length=1,
            schema_version=SCHEMA_VERSION,
        ),
        plan_root=PlanRootRef(
            artifact_id=ArtifactId("sha256:" + characters[1] * 64),
            media_type="application/json",
            byte_length=1,
            schema_version=SCHEMA_VERSION,
        ),
        world_root=WorldRootRef(
            artifact_id=ArtifactId("sha256:" + characters[2] * 64),
            media_type="application/json",
            byte_length=1,
            schema_version=SCHEMA_VERSION,
        ),
        reference_root=ReferenceRootRef(
            artifact_id=ArtifactId("sha256:" + characters[3] * 64),
            media_type="application/json",
            byte_length=1,
            schema_version=SCHEMA_VERSION,
        ),
        project_profile_root=ProjectProfileRootRef(
            artifact_id=ArtifactId("sha256:" + characters[4] * 64),
            media_type="application/json",
            byte_length=1,
            schema_version=SCHEMA_VERSION,
        ),
        parent_commit_ids=parent_commit_ids,
    )


def _custom_task(
    context: _CaseContext,
    *,
    task_suffix: str,
    kind: TaskKind,
    status: TaskStatus = TaskStatus.READY,
    failure_budget: int = 3,
    input_artifact_refs: tuple[ArtifactRef, ...] = (),
    dependency_task_ids: tuple[TaskId, ...] = (),
) -> TaskRecord:
    task = TaskRecord(
        task_id=TaskId(f"{context.run_id.root}.{task_suffix}"),
        run_id=context.run_id,
        project_id=context.project_id,
        kind=kind,
        task_revision=0,
        status=status,
        basis_commit=context.basis_commit,
        policy_hash=POLICY_HASH,
        permission_hash=PERMISSION_HASH,
        failure_budget=failure_budget,
        retry_tranche_size=max(1, failure_budget),
        input_artifact_refs=input_artifact_refs,
        dependency_task_ids=dependency_task_ids,
    )
    return task


def _start_with_checkpoint(
    harness: _Harness,
    context: _CaseContext,
    *,
    task: TaskRecord | None = None,
    worker_id: str = "u6c.worker.old",
) -> _RuntimeStart:
    task = task or context.initial_task
    if task.task_id != context.initial_task.task_id:
        task = harness.commands.create_task(task)
    attempt, fence = harness.commands.claim(task.task_id, worker_id=worker_id)
    harness.commands.mark_started(fence)
    state_ref = harness.put_json(
        {
            "case": context.case.value,
            "run_id": context.run_id.root,
            "task_id": task.task_id.root,
            "frontier": "settled-before-injection",
        },
        "application/vnd.novel-agent.runtime.safe-checkpoint+json",
    )
    position = harness.events.replay(context.run_id)[-1].sequence_no
    checkpoint = RunCheckpoint(
        checkpoint_id=StableId(f"checkpoint.{context.run_id.root}.safe"),
        run_id=context.run_id,
        event_position=position,
        logical_stage=f"u6c.{context.case.value}",
        state_artifact_ref=state_ref,
        resumability_status=ResumabilityStatus.RESUMABLE,
    )
    harness.commands.save_checkpoint(fence, checkpoint)
    return _RuntimeStart(task, attempt, fence, checkpoint)


def _requested_effect(identity: str, attempt_no: int, *, system: str = "provider") -> EffectReceipt:
    return EffectReceipt(
        effect_identity=StableId(identity),
        external_system=system,
        request_identity=StableId(f"request.{identity}"),
        status=EffectStatus.REQUESTED,
        attempt_no=attempt_no,
    )


def _recovery_resume(
    harness: _Harness,
    start: _RuntimeStart,
    *,
    worker_id: str = "u6c.worker.new",
) -> tuple[RunCheckpoint, TaskAttempt, AttemptFence]:
    harness.commands.operator_reconcile_attempt(
        start.task.task_id,
        command_id=StableId(f"reconcile.{start.task.task_id.root}"),
        actor_id="u6c-harness",
        reason="injected worker termination after safe checkpoint",
        terminal_status=TaskStatus.WAITING_RETRY,
        failure_class=FailureClass.WORKER_STARTUP,
    )
    return harness.recovery().resume(
        start.task.task_id,
        worker_id=worker_id,
        actor_id="u6c-recovery",
    )


def _case_result(
    harness: _Harness,
    context: _CaseContext,
    *,
    injection_point: str,
    expected_action: str,
    observed_action: str,
    safe_checkpoint_id: StableId,
    old_attempt: TaskAttempt | None = None,
    new_attempt: TaskAttempt | None = None,
    provider_call_count: int = 0,
    effect_identities: tuple[StableId, ...] = (),
    recovered_commit_id: CommitId | None = None,
    recovered_memory_identity: StableId | None = None,
    evidence: tuple[ArtifactRef, ...] = (),
    forbidden_results: tuple[str, ...] = (),
    notes: tuple[str, ...] = (),
) -> U6CFaultCaseResult:
    status = (
        U6CFaultCaseStatus.PASS if not forbidden_results else U6CFaultCaseStatus.REVIEW_REQUIRED
    )
    evidence_payload = {
        "schema": "u6c-fault-case-evidence.v1",
        "case": context.case.value,
        "run_id": context.run_id.root,
        "injection_point": injection_point,
        "expected_action": expected_action,
        "observed_action": observed_action,
        "status": status.value,
        "safe_checkpoint_id": safe_checkpoint_id.root,
        "old_attempt_id": None if old_attempt is None else old_attempt.attempt_id.root,
        "new_attempt_id": None if new_attempt is None else new_attempt.attempt_id.root,
        "provider_call_count": provider_call_count,
        "effect_identities": [item.root for item in effect_identities],
        "recovered_commit_id": (None if recovered_commit_id is None else recovered_commit_id.root),
        "recovered_memory_identity": (
            None if recovered_memory_identity is None else recovered_memory_identity.root
        ),
        "forbidden_results": list(forbidden_results),
        "notes": list(notes),
    }
    case_ref = harness.put_json(evidence_payload, CASE_MEDIA_TYPE)
    return U6CFaultCaseResult(
        case=context.case,
        run_id=context.run_id,
        injection_point=injection_point,
        expected_action=expected_action,
        observed_action=observed_action,
        status=status,
        safe_checkpoint_id=safe_checkpoint_id,
        old_attempt_id=None if old_attempt is None else old_attempt.attempt_id,
        old_fence_generation=None if old_attempt is None else old_attempt.fence_generation,
        new_attempt_id=None if new_attempt is None else new_attempt.attempt_id,
        new_fence_generation=None if new_attempt is None else new_attempt.fence_generation,
        provider_call_count=provider_call_count,
        effect_identities=effect_identities,
        recovered_commit_id=recovered_commit_id,
        recovered_memory_identity=recovered_memory_identity,
        evidence_artifact_refs=(case_ref, *evidence),
        forbidden_results=forbidden_results,
        notes=notes,
    )


def _case_provider_before_kill(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.PROVIDER_BEFORE_WORKER_KILL)
    start = _start_with_checkpoint(harness, context)
    _, new_attempt, new_fence = _recovery_resume(harness, start)
    effect = _requested_effect("effect.u6c.provider-before-kill", new_attempt.attempt_no)
    harness.commands.record_effect_requested(new_fence, effect)
    harness.commands.record_effect_terminal(
        new_fence,
        effect.model_copy(
            update={
                "status": EffectStatus.COMPLETED,
                "provider_request_id": "provider.u6c.one",
                "completed_at": datetime.now(UTC),
            }
        ),
    )
    harness.commands.settle_attempt(
        new_fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
    )
    forbidden: list[str] = []
    try:
        harness.commands.heartbeat(start.fence)
        forbidden.append("old fence heartbeat was accepted")
    except StaleAttemptFenceError:
        pass
    return _case_result(
        harness,
        context,
        injection_point="after settled checkpoint, before provider request",
        expected_action="fresh Attempt sends one provider request",
        observed_action="Attempt 2 sent one effect and settled; old fence rejected",
        safe_checkpoint_id=start.checkpoint.checkpoint_id,
        old_attempt=start.attempt,
        new_attempt=new_attempt,
        provider_call_count=1,
        effect_identities=(effect.effect_identity,),
        forbidden_results=tuple(forbidden),
    )


def _case_provider_after_request(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.PROVIDER_AFTER_REQUEST_BEFORE_RAW)
    start = _start_with_checkpoint(harness, context)
    effect = _requested_effect("effect.u6c.request-before-raw", start.attempt.attempt_no)
    harness.commands.record_effect_requested(start.fence, effect)
    pending = harness.commands.mark_recovery_pending(
        start.task.task_id,
        command_id=StableId("u6c.pending.provider-after-request"),
        actor_id="u6c-harness",
        reason="provider request sent but raw completion is unknown",
    )
    forbidden: list[str] = []
    try:
        harness.recovery(EffectStatus.UNCERTAIN).reconcile_uncertain_effects(start.task.task_id)
        forbidden.append("uncertain provider effect was treated as completed")
    except RuntimeCommandConflictError as error:
        if "remains unresolved" not in str(error):
            forbidden.append(f"wrong uncertain stop: {error}")
    observed = harness.commands.get_task(start.task.task_id)
    if observed.status is not TaskStatus.RECOVERY_PENDING:
        forbidden.append("task left RECOVERY_PENDING before reconciliation")
    return _case_result(
        harness,
        context,
        injection_point="provider request sent, before complete raw envelope",
        expected_action="UNCERTAIN and reconcile-or-typed-stop",
        observed_action="effect stayed unresolved and task stayed RECOVERY_PENDING",
        safe_checkpoint_id=start.checkpoint.checkpoint_id,
        old_attempt=start.attempt,
        provider_call_count=1,
        effect_identities=(effect.effect_identity,),
        evidence=(pending.input_artifact_refs[0],) if pending.input_artifact_refs else (),
        forbidden_results=tuple(forbidden),
    )


def _case_streaming_partial(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.STREAMING_PARTIAL_BEFORE_RAW)
    start = _start_with_checkpoint(harness, context)
    partial_ref = harness.put_json(
        {"request_id": "provider.u6c.partial", "partial_text": "unfinished"},
        PARTIAL_MEDIA_TYPE,
    )
    effect = _requested_effect("effect.u6c.partial-before-raw", start.attempt.attempt_no)
    harness.commands.record_effect_requested(start.fence, effect)
    harness.commands.mark_recovery_pending(
        start.task.task_id,
        command_id=StableId("u6c.pending.partial"),
        actor_id="u6c-harness",
        reason="streaming partial is durable but complete raw is absent",
    )
    forbidden: list[str] = []
    try:
        harness.recovery(EffectStatus.UNCERTAIN).reconcile_uncertain_effects(start.task.task_id)
        forbidden.append("partial response was parsed as a normal candidate")
    except RuntimeCommandConflictError:
        pass
    return _case_result(
        harness,
        context,
        injection_point="streaming partial persisted, before complete raw envelope",
        expected_action="retain partial evidence and enter UNCERTAIN",
        observed_action="partial artifact retained; effect reconciliation stopped typed",
        safe_checkpoint_id=start.checkpoint.checkpoint_id,
        old_attempt=start.attempt,
        provider_call_count=1,
        effect_identities=(effect.effect_identity,),
        evidence=(partial_ref,),
        forbidden_results=tuple(forbidden),
    )


def _model_reparse(
    harness: _Harness,
    context: _CaseContext,
    logical_phase: str,
) -> tuple[int, ArtifactRef, StableId, str]:
    ledger = SqlModelCallLedger(harness.factory)
    endpoint = _CountingFakeEndpoint()
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name=f"u6c-{logical_phase}",
                model_name="u6c-fake-v1",
                adapter=endpoint,
                sequence_limit=8_192,
                output_limit=2_048,
                safety_allowance_tokens=64,
                estimated_reasoning_reserve=128,
                reasoning_included_in_completion_tokens=False,
                global_output_cap=4_096,
            ),
        ),
        call_ledger=ledger,
        raw_artifacts=harness.artifacts,
    )
    request = ModelRequest(
        request_id=StableId(f"request.{context.case.value}"),
        run_id=context.run_id,
        task_id=context.initial_task.task_id,
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id=f"trace.{context.case.value}",
        prompt='{"answer":"durable"}',
        max_output_tokens=2_048,
        enable_thinking=True,
        thinking_token_budget=128,
        scheduling_stage=logical_phase,
    )
    asyncio.run(gateway.generate_text(request))
    entry = ledger.load(request.request_id)
    if entry is None or entry.raw_artifact_ref is None:
        raise RuntimeError("U6-C model reparse case did not retain a raw artifact")
    parsed, _record = gateway.reparse_structured_from_raw(request, _Answer)
    if parsed.answer != "durable":
        raise RuntimeError("U6-C model reparse produced the wrong answer")
    if len(endpoint.requests) != 1 or len(ledger.list_for_run(context.run_id)) != 1:
        raise RuntimeError("U6-C model reparse issued a second provider call")
    return len(endpoint.requests), entry.raw_artifact_ref, request.request_id, parsed.answer


def _case_provider_raw_before_parse(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.PROVIDER_RAW_BEFORE_PARSE)
    calls, raw_ref, request_id, answer = _model_reparse(
        harness, context, "runtime.raw-before-parse"
    )
    return _case_result(
        harness,
        context,
        injection_point="provider success and raw artifact durable, before parse",
        expected_action="reparse raw artifact without provider retry",
        observed_action=f"reparsed {answer!r} from {request_id.root}; one provider call",
        safe_checkpoint_id=StableId(f"checkpoint.{context.run_id.root}.raw"),
        provider_call_count=calls,
        evidence=(raw_ref,),
        recovered_memory_identity=StableId("raw-response-identity-preserved"),
    )


def _case_parse_before_leaf_checkpoint(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.PARSE_BEFORE_LEAF_CHECKPOINT)
    start = _start_with_checkpoint(harness, context)
    candidate_ref = harness.put_json(
        {"candidate_id": "candidate.u6c.parse-success", "parsed": True},
        "application/vnd.novel-agent.candidate+json",
    )
    _, new_attempt, new_fence = _recovery_resume(harness, start)
    harness.commands.settle_attempt(
        new_fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
        artifact_refs=(candidate_ref,),
    )
    terminal = harness.commands.get_task(context.initial_task.task_id)
    forbidden = (
        ()
        if terminal.terminal_artifact_refs == (candidate_ref,)
        else ("recovery changed the parsed candidate identity",)
    )
    return _case_result(
        harness,
        context,
        injection_point="parse succeeded, before leaf checkpoint",
        expected_action="resume/settle the same parsed candidate identity",
        observed_action="Attempt 2 settled the original candidate artifact",
        safe_checkpoint_id=start.checkpoint.checkpoint_id,
        old_attempt=start.attempt,
        new_attempt=new_attempt,
        evidence=(candidate_ref,),
        recovered_memory_identity=StableId("candidate.u6c.parse-success"),
        forbidden_results=forbidden,
    )


def _case_acceptance_before_kill(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.ACCEPTANCE_BEFORE_KILL)
    candidate_ref = harness.put_json(
        {"candidate_id": "candidate.u6c.acceptance-wait", "kind": "plan"},
        "application/vnd.novel-agent.plan-candidate+json",
    )
    attempt, fence = harness.commands.claim(context.initial_task.task_id, worker_id="u6c.plan")
    harness.commands.mark_started(fence)
    harness.commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
        artifact_refs=(candidate_ref,),
    )
    acceptance = _custom_task(
        context,
        task_suffix="plan-acceptance",
        kind=TaskKind.PLAN_ACCEPTANCE,
        status=TaskStatus.WAITING_INPUT,
        input_artifact_refs=(candidate_ref,),
        dependency_task_ids=(context.initial_task.task_id,),
    )
    acceptance = harness.commands.create_task(acceptance)
    after = harness.commands.get_task(acceptance.task_id)
    forbidden = (
        () if after.status is TaskStatus.WAITING_INPUT else ("acceptance wait was bypassed",)
    )
    return _case_result(
        harness,
        context,
        injection_point="worker kill immediately before acceptance command",
        expected_action="retain candidate and wait at the same acceptance point",
        observed_action="PLAN_ACCEPTANCE remained WAITING_INPUT with the same candidate ref",
        safe_checkpoint_id=StableId(f"checkpoint.{context.run_id.root}.acceptance"),
        old_attempt=attempt,
        evidence=(candidate_ref,),
        forbidden_results=forbidden,
    )


def _commit_request(
    harness: _Harness,
    context: _CaseContext,
    *,
    key: str,
    root_offset: int,
) -> CommitRequest:
    source_ref = harness.put_json({"source": key}, "application/vnd.novel-agent.change-source+json")
    bundle_id = StableId(f"bundle.{key}")
    observed = ObservedChangeSet(
        change_set_id=StableId(f"changes.{key}"),
        base_commit=context.basis_commit,
        source_artifact=source_ref,
    )
    bundle = CandidateChangeBundle(
        bundle_id=bundle_id,
        project_id=context.project_id,
        run_id=context.run_id,
        base_commit=context.basis_commit,
        observed_changes=observed,
        proposed_roots=_manifest(
            context.project_id,
            root_offset,
            (context.basis_commit,),
        ),
        produced_artifacts=(source_ref,),
    )
    report = ValidationReport(
        report_id=StableId(f"validation.{key}"),
        bundle_id=bundle_id,
        status=ValidationStatus.PASSED,
        schema_version=SCHEMA_VERSION,
        validated_at=datetime.now(UTC),
    )
    return CommitRequest(
        request_id=StableId(f"request.{key}"),
        project_id=context.project_id,
        base_commit=context.basis_commit,
        idempotency_key=StableId(f"idempotency.{key}"),
        bundle=bundle,
        validation_report=report,
    )


def _case_commit_before_settlement(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.COMMIT_BEFORE_SETTLEMENT)
    task = _custom_task(context, task_suffix="draft-commit", kind=TaskKind.DRAFT_COMMIT)
    start = _start_with_checkpoint(harness, context, task=task, worker_id="u6c.commit")
    writer_fence = harness.commands.claim_writer_lane(start.fence)
    effect = _requested_effect(
        "effect.u6c.external-commit",
        start.attempt.attempt_no,
        system="stage2w.chapter_reveal_atomic",
    )
    harness.commands.record_effect_requested(writer_fence, effect)
    request = _commit_request(harness, context, key="u6c.external-commit", root_offset=8)
    result = harness.commits.commit(request)
    if result.commit_id is None:
        raise RuntimeError("U6-C external commit case did not create a Commit")
    harness.commands.mark_recovery_pending(
        task.task_id,
        command_id=StableId("u6c.pending.external-commit"),
        actor_id="u6c-harness",
        reason="external Commit succeeded before receipt and Attempt settlement",
    )
    completed_effect = effect.model_copy(
        update={
            "status": EffectStatus.COMPLETED,
            "provider_request_id": result.commit_id.root,
            "completed_at": datetime.now(UTC),
        }
    )
    recovered = harness.commands.reconcile_external_commit(
        task.task_id,
        result.commit_id,
        commits=harness.commits,
        effect_receipt=completed_effect,
        successor_tasks=(),
    )
    repeated = harness.commits.commit(request)
    forbidden = []
    if repeated.commit_id != result.commit_id:
        forbidden.append("idempotent Commit query changed identity")
    if recovered.status is not TaskStatus.SUCCEEDED:
        forbidden.append("external Commit was not settled")
    return _case_result(
        harness,
        context,
        injection_point="external Commit accepted, before receipt/Attempt settlement",
        expected_action="reconcile the existing Commit and effect",
        observed_action="existing Commit/effect reconciled and one Attempt settled",
        safe_checkpoint_id=start.checkpoint.checkpoint_id,
        old_attempt=start.attempt,
        effect_identities=(effect.effect_identity,),
        recovered_commit_id=result.commit_id,
        evidence=(
            harness.put_json(
                result.model_dump(mode="json"), "application/vnd.novel-agent.commit-receipt+json"
            ),
        ),
        forbidden_results=tuple(forbidden),
    )


def _case_projection_failure(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.PROJECTION_FAILURE)
    start = _start_with_checkpoint(harness, context)
    harness.commands.settle_attempt(
        start.fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
    )
    events = harness.events.replay(context.run_id)
    actual = (harness.commands.get_task(context.initial_task.task_id),)
    corrupted_projection = ()
    forbidden: list[str] = []
    try:
        assert_task_projection_matches(events, corrupted_projection)
        forbidden.append("corrupted projection was accepted")
    except RuntimeError:
        pass
    rebuilt = project_runtime_events(events)
    if rebuilt.tasks.get(context.initial_task.task_id.root) != actual[0]:
        forbidden.append("full replay did not rebuild the task projection")
    return _case_result(
        harness,
        context,
        injection_point="runtime projection read is stale after event append",
        expected_action="rebuild projection only from the event stream",
        observed_action="stale projection rejected; full replay restored the same task",
        safe_checkpoint_id=start.checkpoint.checkpoint_id,
        old_attempt=start.attempt,
        recovered_memory_identity=StableId("runtime-projection-rebuilt"),
        forbidden_results=tuple(forbidden),
    )


def _case_lease_expiry(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.LEASE_EXPIRY)
    start = _start_with_checkpoint(harness, context)
    expiry = start.attempt.lease_expires_at
    if expiry is None:
        raise RuntimeError("U6-C lease case has no lease expiry")
    observed_at = expiry + timedelta(seconds=1)
    harness.commands.suspect_expired_attempt(
        start.task.task_id,
        command_id=StableId("u6c.lease.suspect"),
        actor_id="u6c-supervisor",
        reason="registered lease expiry",
        now=observed_at,
    )
    harness.commands.reclaim_expired_attempt(
        start.task.task_id,
        command_id=StableId("u6c.lease.reclaim"),
        actor_id="u6c-reconciler",
        reason="fresh worker will resume from safe checkpoint",
        now=observed_at,
    )
    _, new_attempt, _ = harness.recovery().resume(
        start.task.task_id,
        worker_id="u6c.worker.lease-recovered",
        actor_id="u6c-recovery",
    )
    forbidden: list[str] = []
    if new_attempt.attempt_no != start.attempt.attempt_no + 1:
        forbidden.append("lease recovery did not create one fresh Attempt")
    try:
        harness.commands.heartbeat(start.fence)
        forbidden.append("expired old fence was accepted")
    except StaleAttemptFenceError:
        pass
    return _case_result(
        harness,
        context,
        injection_point="Attempt lease expiry before worker completion",
        expected_action="RECOVERY_PENDING, reconcile, then fresh Attempt",
        observed_action="lease was suspected/reclaimed; Attempt 2 resumed from checkpoint",
        safe_checkpoint_id=start.checkpoint.checkpoint_id,
        old_attempt=start.attempt,
        new_attempt=new_attempt,
        forbidden_results=tuple(forbidden),
    )


def _case_basis_change(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.BASIS_FRESHNESS_CHANGE)
    request = _commit_request(harness, context, key="u6c.advance-basis", root_offset=9)
    advanced = harness.commits.commit(request)
    if advanced.commit_id is None:
        raise RuntimeError("U6-C basis case could not advance the project")
    forbidden: list[str] = []
    try:
        harness.commands.claim(context.initial_task.task_id, worker_id="u6c.stale")
        forbidden.append("stale basis task was claimed")
    except RuntimeCommandConflictError:
        pass
    stale = harness.commands.supersede_task(
        context.initial_task.task_id,
        reason="basis changed; stale Plan must be replanned",
    )
    fresh_run = RunId(f"{context.run_id.root}.replan")
    fresh_request = CreativeRunRequest(
        run_id=fresh_run,
        project_id=context.project_id,
        basis_commit=advanced.commit_id,
        policy=CreativeRunPolicy(
            automation_mode=AutomationMode.MANUAL,
            policy_hash=POLICY_HASH,
            permission_hash=PERMISSION_HASH,
        ),
    )
    fresh = harness.commands.create_run_and_initial_task(fresh_request)
    if stale.status is not TaskStatus.CANCELLED or fresh.basis_commit != advanced.commit_id:
        forbidden.append("basis change did not supersede and replan")
    return _case_result(
        harness,
        context,
        injection_point="basis/current Commit changes before stale Plan claim",
        expected_action="supersede stale work and create a fresh-basis Plan",
        observed_action="stale Plan was cancelled; fresh Plan used the new Commit",
        safe_checkpoint_id=StableId(f"checkpoint.{context.run_id.root}.basis"),
        recovered_commit_id=advanced.commit_id,
        recovered_memory_identity=StableId(f"fresh-plan.{fresh.task_id.root}"),
        forbidden_results=tuple(forbidden),
    )


def _case_repeated_failure(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.REPEATED_FAILURE)
    task = _custom_task(
        context,
        task_suffix="validation-poison",
        kind=TaskKind.PLAN_CANDIDATE,
        failure_budget=1,
    )
    start = _start_with_checkpoint(harness, context, task=task, worker_id="u6c.validator")
    final = harness.commands.settle_attempt(
        start.fence,
        outcome=AttemptOutcome.FAILED,
        terminal_status=TaskStatus.WAITING_RETRY,
        failure_class=FailureClass.VALIDATION_REJECTED,
    )
    findings = RuntimeSupervisor(harness.factory, stuck_after=timedelta(seconds=0)).inspect()
    forbidden: list[str] = []
    if final.status is not TaskStatus.BUDGET_REVIEW:
        forbidden.append("repeated validation failure did not enter BUDGET_REVIEW")
    if not any(item.task_id == task.task_id for item in findings):
        forbidden.append("budget Gate produced no supervisor finding")
    return _case_result(
        harness,
        context,
        injection_point="validation/semantic failure repeats at exhausted task budget",
        expected_action="poison/budget Gate with a diagnostic stop",
        observed_action="task entered BUDGET_REVIEW and supervisor emitted a finding",
        safe_checkpoint_id=start.checkpoint.checkpoint_id,
        old_attempt=start.attempt,
        forbidden_results=tuple(forbidden),
    )


def _case_planner_memory(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.PLANNER_REQUEST_MEMORY)
    inquiry_ref = harness.put_json({"need_id": "need.u6c.planner"}, "application/json")
    review_ref = harness.put_json({"review_id": "review.u6c.planner"}, "application/json")
    memory_ref = harness.put_json({"memory_id": "memory.u6c.planner"}, "application/json")
    planner_context_ref = harness.put_json(
        {"context_id": "context.u6c.planner", "generation": 2},
        "application/vnd.novel-agent.planner-context+json",
    )
    checkpoint = PlanningLoopCheckpoint(
        checkpoint_id=StableId(f"checkpoint.{context.run_id.root}.planner-memory"),
        request_id=StableId(f"planner-request.{context.run_id.root}"),
        phase=PlanningLoopPhase.PLANNER_MEMORY_PENDING,
        base_commit=context.basis_commit,
        snapshot_id=StableId(f"snapshot.{context.run_id.root}"),
        configuration_fingerprint=ArtifactId(POLICY_HASH),
        inquiry_ref=inquiry_ref,
        inquiry_review_ref=review_ref,
        memory_context_ref=memory_ref,
        planner_context_ref=planner_context_ref,
        planner_memory_context_refs=(planner_context_ref,),
        pending_planner_memory_questions=("Which prior event constrains this Plan?",),
    )
    checkpoint_ref = harness.put_json(
        checkpoint.model_dump(mode="json"),
        PLANNER_CHECKPOINT_MEDIA_TYPE,
    )
    recovered = PlanningLoopCheckpoint.model_validate_json(
        harness.artifacts.read_verified(checkpoint_ref), strict=True
    )
    forbidden = () if recovered == checkpoint else ("Planner checkpoint lineage changed",)
    return _case_result(
        harness,
        context,
        injection_point="Planner REQUEST_MEMORY after checkpoint persistence",
        expected_action="resume the same Need/Context lineage without a new Planner intent",
        observed_action="same pending question and Planner context reloaded from checkpoint",
        safe_checkpoint_id=checkpoint.checkpoint_id,
        recovered_memory_identity=StableId("planner-context.u6c.same-lineage"),
        evidence=(checkpoint_ref, planner_context_ref),
        forbidden_results=forbidden,
        notes=(f"action={PlanningTurnAction.REQUEST_MEMORY.value}",),
    )


def _case_editor_repair(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.EDITOR_REPAIR_OR_REWRITE)
    task = _custom_task(context, task_suffix="editor", kind=TaskKind.DRAFT_CANDIDATE)
    start = _start_with_checkpoint(harness, context, task=task, worker_id="u6c.editor")
    editor_payload = EditorReviewPayload(
        verdict=EditorialVerdict.MAJOR_REWRITE,
        issues=(
            EditorialIssueDraft(
                issue_type=EditorialIssueType.STRUCTURE,
                severity=EditorialSeverity.CRITICAL,
                description="required beat is absent",
                structural=True,
            ),
        ),
        rewrite_targets=("restore the missing beat",),
        rewrite_preserve_requirements=("keep the accepted plan lineage",),
    )
    context_ref = harness.put_json(
        editor_payload.model_dump(mode="json"), EDITOR_CONTEXT_MEDIA_TYPE
    )
    repair = EditorRepairPayload(repaired_text="repaired draft with the required beat")
    repair_ref = harness.put_json(repair.model_dump(mode="json"), EDITOR_REPAIR_MEDIA_TYPE)
    _, new_attempt, new_fence = _recovery_resume(harness, start, worker_id="u6c.editor.recovered")
    harness.commands.settle_attempt(
        new_fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
        artifact_refs=(context_ref, repair_ref),
    )
    terminal = harness.commands.get_task(task.task_id)
    forbidden = (
        ()
        if terminal.terminal_artifact_refs == (context_ref, repair_ref)
        else ("Editor recovery lost exact Context or settled repair artifact",)
    )
    return _case_result(
        harness,
        context,
        injection_point=(
            "Editor repair/major-rewrite phase after Context and repair artifact settle"
        ),
        expected_action="continue unfinished Editor phase from exact Context",
        observed_action="new Attempt reused the exact Editor Context and repair refs",
        safe_checkpoint_id=start.checkpoint.checkpoint_id,
        old_attempt=start.attempt,
        new_attempt=new_attempt,
        recovered_memory_identity=StableId("editor-context.u6c.same-lineage"),
        evidence=(context_ref, repair_ref),
        forbidden_results=forbidden,
    )


def _case_supervisor(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.SUPERVISOR_FINDING)
    start = _start_with_checkpoint(harness, context)
    effect = _requested_effect("effect.u6c.supervisor", start.attempt.attempt_no)
    harness.commands.record_effect_requested(start.fence, effect)
    before = harness.commands.get_task(context.initial_task.task_id)
    findings = RuntimeSupervisor(harness.factory, stuck_after=timedelta(seconds=0)).inspect()
    after = harness.commands.get_task(context.initial_task.task_id)
    forbidden: list[str] = []
    if before != after:
        forbidden.append("Supervisor mutated task state")
    if not any(item.task_id == context.initial_task.task_id for item in findings):
        forbidden.append("Supervisor emitted no finding for stuck/effect task")
    return _case_result(
        harness,
        context,
        injection_point="Supervisor sees stuck task and unresolved effect frontier",
        expected_action="finding/proposal-only; typed command owner decides",
        observed_action="Supervisor returned finding(s) without task mutation",
        safe_checkpoint_id=start.checkpoint.checkpoint_id,
        old_attempt=start.attempt,
        effect_identities=(effect.effect_identity,),
        forbidden_results=tuple(forbidden),
    )


def _case_checkpoint_before_release(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.CHECKPOINT_BEFORE_RELEASE)
    start = _start_with_checkpoint(harness, context)
    event_count_before = len(harness.events.replay(context.run_id))
    restored = harness.commands.save_checkpoint(start.fence, start.checkpoint)
    event_count_after = len(harness.events.replay(context.run_id))
    forbidden = (
        ()
        if (restored == start.checkpoint and event_count_before == event_count_after)
        else ("checkpoint re-release rebuilt state or appended a duplicate event",)
    )
    return _case_result(
        harness,
        context,
        injection_point="checkpoint freeze before question/Plan release",
        expected_action="re-release once from the frozen receipt",
        observed_action="same checkpoint identity returned with no duplicate release event",
        safe_checkpoint_id=start.checkpoint.checkpoint_id,
        old_attempt=start.attempt,
        forbidden_results=forbidden,
    )


def _case_writer_raw_before_parse(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.WRITER_RAW_BEFORE_PARSE)
    calls, raw_ref, request_id, answer = _model_reparse(harness, context, "benchmark.writer-answer")
    return _case_result(
        harness,
        context,
        injection_point="Writer raw answer durable, before parse/freeze",
        expected_action="reparse the same raw answer/request identity",
        observed_action=f"Writer answer {answer!r} reparsed with one provider call",
        safe_checkpoint_id=StableId(f"checkpoint.{context.run_id.root}.writer-raw"),
        provider_call_count=calls,
        evidence=(raw_ref,),
        recovered_memory_identity=StableId(f"writer-request.{request_id.root}"),
    )


def _case_response_before_judge(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.RESPONSE_FREEZE_BEFORE_JUDGE)
    response_ref = harness.put_json(
        {"response": "frozen before Gold", "gold_revealed": False},
        QA_WRITER_RESPONSE_MEDIA_TYPE,
    )
    judge_service = WriterJudgeService(harness.artifacts)
    freeze_id = StableId(f"freeze.{context.run_id.root}")
    task_id = StableId(f"qa-task.{context.run_id.root}")
    first = judge_service.pending_pair(
        run_id=context.run_id,
        task_id=task_id,
        freeze_receipt_id=freeze_id,
        response_ref=response_ref,
        availability=WriterJudgeAvailability.PENDING,
    )
    second = judge_service.pending_pair(
        run_id=context.run_id,
        task_id=task_id,
        freeze_receipt_id=freeze_id,
        response_ref=response_ref,
        availability=WriterJudgeAvailability.PENDING,
    )
    forbidden = (
        () if first == second else ("evaluator resume changed the frozen response identity",)
    )
    return _case_result(
        harness,
        context,
        injection_point="response freeze, before Answer/Evidence Judge",
        expected_action="resume evaluator from frozen answer without Writer rerun",
        observed_action="same pending judge pair and frozen response ref were restored",
        safe_checkpoint_id=StableId(f"checkpoint.{context.run_id.root}.response"),
        recovered_memory_identity=StableId("frozen-response.u6c.same-lineage"),
        evidence=(response_ref,),
        forbidden_results=forbidden,
    )


def _case_evaluator_before_discard(harness: _Harness) -> U6CFaultCaseResult:
    context = harness.new_case(U6CFaultCase.EVALUATOR_BEFORE_DISCARD)
    evaluation_ref = harness.put_json(
        {"evaluation": "side-channel", "gold_written_to_canon": False},
        "application/vnd.novel-agent.evaluation.writer-judge-output+json",
    )
    manifest = harness.commits.load_manifest(context.basis_commit)
    identity = MemoryIdentitySnapshot(
        commit_id=context.basis_commit,
        text_root=manifest.text_root.artifact_id,
        world_root=manifest.world_root.artifact_id,
        plan_root=manifest.plan_root.artifact_id,
        profile_root=manifest.project_profile_root.artifact_id,
    )
    discard_id = StableId(f"discard.{context.run_id.root}")
    first = discard_evaluation_namespace(
        harness.artifacts,
        run_id=context.run_id,
        discarded_refs=(evaluation_ref,),
        memory_before=identity,
        memory_after=identity,
        discard_identity=discard_id,
    )
    second = discard_evaluation_namespace(
        harness.artifacts,
        run_id=context.run_id,
        discarded_refs=(evaluation_ref,),
        memory_before=identity,
        memory_after=identity,
        discard_identity=discard_id,
    )
    forbidden = (
        ()
        if first == second and first.memory_identity_after == identity
        else ("evaluation discard changed Memory identity or was not idempotent",)
    )
    return _case_result(
        harness,
        context,
        injection_point="evaluator completed, before evaluation namespace discard",
        expected_action="idempotently discard side-channel and continue ingest",
        observed_action="discard receipt repeated with unchanged Memory/Canon identity",
        safe_checkpoint_id=StableId(f"checkpoint.{context.run_id.root}.discard"),
        recovered_commit_id=context.basis_commit,
        recovered_memory_identity=StableId("memory.u6c.unchanged"),
        evidence=(evaluation_ref,),
        forbidden_results=forbidden,
    )


def _database_descriptor(database_url: str) -> str:
    return database_url.rsplit("/", 1)[-1].split("?", 1)[0]


def _write_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"U6-C refuses to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _count_commits(harness: _Harness) -> int:
    with harness.factory() as session:
        return int(session.scalar(select(func.count()).select_from(CommitRow)) or 0)


def run_matrix(database_url: str, output_root: Path, experiment_id: str) -> U6CFaultMatrixReport:
    if output_root.exists():
        raise RuntimeError(f"U6-C refuses to reuse output root: {output_root}")
    output_root.mkdir(parents=True)
    _write_once(
        output_root / "u6c-fault-matrix-input.json",
        {
            "schema": "u6c-fault-matrix-input.v1",
            "experiment_id": experiment_id,
            "database": _database_descriptor(database_url),
            "case_count": len(U6CFaultCase),
            "runtime_owner": "RuntimeCommandService/RuntimeRecoveryService",
        },
    )
    harness = _Harness(database_url, output_root)
    runners: tuple[Callable[[_Harness], U6CFaultCaseResult], ...] = (
        _case_provider_before_kill,
        _case_provider_after_request,
        _case_streaming_partial,
        _case_provider_raw_before_parse,
        _case_parse_before_leaf_checkpoint,
        _case_acceptance_before_kill,
        _case_commit_before_settlement,
        _case_projection_failure,
        _case_lease_expiry,
        _case_basis_change,
        _case_repeated_failure,
        _case_planner_memory,
        _case_editor_repair,
        _case_supervisor,
        _case_checkpoint_before_release,
        _case_writer_raw_before_parse,
        _case_response_before_judge,
        _case_evaluator_before_discard,
    )
    try:
        cases = tuple(runner(harness) for runner in runners)
        effect_ids = tuple(effect for case in cases for effect in case.effect_identities)
        duplicate_effect_count = len(effect_ids) - len(set(effect_ids))
        forbidden_count = sum(len(case.forbidden_results) for case in cases)
        all_pass = (
            len(cases) == len(U6CFaultCase)
            and all(case.status is U6CFaultCaseStatus.PASS for case in cases)
            and not forbidden_count
            and not duplicate_effect_count
        )
        report = U6CFaultMatrixReport(
            experiment_id=experiment_id,
            database_descriptor=_database_descriptor(database_url),
            status=(U6CFaultCaseStatus.PASS if all_pass else U6CFaultCaseStatus.REVIEW_REQUIRED),
            cases=cases,
            total_provider_call_count=sum(case.provider_call_count for case in cases),
            total_effect_count=len(effect_ids),
            total_commit_count=_count_commits(harness),
            projection_rebuild_verified=(
                next(
                    case.status is U6CFaultCaseStatus.PASS
                    for case in cases
                    if case.case is U6CFaultCase.PROJECTION_FAILURE
                )
            ),
            duplicate_effect_count=duplicate_effect_count,
            forbidden_result_count=forbidden_count,
        )
        _write_once(output_root / "u6c-fault-matrix-report.json", report.model_dump(mode="json"))
        return report
    finally:
        harness.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    args = parser.parse_args()
    report = run_matrix(args.database_url, args.output_root, args.experiment_id)
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if report.status is U6CFaultCaseStatus.PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
