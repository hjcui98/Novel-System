"""Deterministic coverage for identity-bound model-send reconciliation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.model_call_ledger import SqlModelCallLedger
from novel_agent.cli import main
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
)
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, SchemaVersion, StableId
from novel_agent.domain.model_calls import (
    BudgetSource,
    EffectiveBudgetResult,
    ModelCallLedgerStatus,
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
)
from novel_agent.domain.runtime import (
    MODEL_CALL_RECONCILIATION_MEDIA_TYPE,
    ModelCallReconciliationEvidence,
    ModelCallReconciliationOutcome,
    ModelCallReconciliationSource,
    TaskRecord,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.runtime_commands import RuntimeCommandConflictError, RuntimeCommandService
from novel_agent.services.runtime_projection import assert_task_projection_matches
from tests.factories import make_manifest

HASH = "sha256:" + "1" * 64
PERMISSION_HASH = "sha256:" + "2" * 64


@pytest.fixture
def reconciliation_kernel(
    tmp_path: Path,
) -> Iterator[
    tuple[
        sessionmaker[Session],
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ]
]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    events = RunEventLogRepository(factory)
    commands = RuntimeCommandService(
        factory,
        events,
        lambda _project_id: PERMISSION_HASH,
        artifacts=artifacts,
    )
    yield factory, artifacts, events, commands, base
    engine.dispose()


def _policy() -> CreativeRunPolicy:
    return CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )


def _task_and_request(
    factory: sessionmaker[Session],
    commands: RuntimeCommandService,
    base: CommitId,
) -> tuple[TaskRecord, ModelRequest, StableId]:
    task = commands.create_run_and_initial_task(
        CreativeRunRequest(
            run_id=RunId("run.model-reconciliation"),
            project_id=ProjectId("project.test"),
            basis_commit=base,
            policy=_policy(),
        )
    )
    attempt, fence = commands.claim(task.task_id, worker_id="reconciliation-worker")
    commands.mark_started(fence)
    request = ModelRequest(
        request_id=StableId("model-request.reconciliation.1"),
        run_id=task.run_id,
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id="trace.model-reconciliation",
        prompt="reconcile this request",
        scheduling_stage="plan_review",
    )
    budget = EffectiveBudgetResult(
        budget_source=BudgetSource.ENDPOINT_DEFAULT,
        context_limit=1000,
        estimated_input_tokens=10,
        body_output_budget=20,
        thinking_budget=0,
        total_output_budget=20,
        safety_allowance_tokens=5,
        reserved_sequence_tokens=35,
        available_input_tokens=975,
    )
    ledger = SqlModelCallLedger(factory)
    requested = ledger.create_requested(
        request,
        effective_budget=budget,
        reasoning_included_in_completion_tokens=False,
    )
    ledger.settle(
        requested.model_copy(
            update={
                "status": ModelCallLedgerStatus.UNCERTAIN,
                "provider_sent_at": datetime.now(UTC),
                "transport_error_type": "TimeoutError",
            }
        )
    )
    return task, request, attempt.attempt_id


def _evidence_ref(
    artifacts: ArtifactRepository,
    *,
    task: TaskRecord,
    request: ModelRequest,
    attempt_id: StableId,
    request_hash: ArtifactId,
    evidence_locator: str = "provider-audit/reconciliation-1",
) -> ArtifactRef:
    evidence = ModelCallReconciliationEvidence(
        request_id=request.request_id,
        run_id=task.run_id,
        task_id=task.task_id,
        attempt_id=attempt_id,
        request_hash=request_hash,
        outcome=ModelCallReconciliationOutcome.PROVIDER_EXPIRED,
        source=ModelCallReconciliationSource.AUTHORIZED_OPERATOR,
        evidence_locator=evidence_locator,
        attested_by="operator.reconciler",
        observed_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    return artifacts.put(
        canonical_json_bytes(evidence.model_dump(mode="json")),
        MODEL_CALL_RECONCILIATION_MEDIA_TYPE,
        SchemaVersion("1.0.0"),
    )


def test_claim_fences_ready_task_with_unsettled_model_send(
    reconciliation_kernel: tuple[
        sessionmaker[Session],
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, _artifacts, _events, commands, base = reconciliation_kernel
    task = commands.create_run_and_initial_task(
        CreativeRunRequest(
            run_id=RunId("run.model-reconciliation-claim"),
            project_id=ProjectId("project.test"),
            basis_commit=base,
            policy=_policy(),
        )
    )
    request = ModelRequest(
        request_id=StableId("model-request.reconciliation.claim-block"),
        run_id=task.run_id,
        task_id=task.task_id,
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id="trace.model-reconciliation-claim",
        prompt="an unresolved request must fence the claim",
        scheduling_stage="plan_review",
    )
    budget = EffectiveBudgetResult(
        budget_source=BudgetSource.ENDPOINT_DEFAULT,
        context_limit=1000,
        estimated_input_tokens=10,
        body_output_budget=20,
        thinking_budget=0,
        total_output_budget=20,
        safety_allowance_tokens=5,
        reserved_sequence_tokens=35,
        available_input_tokens=975,
    )
    ledger = SqlModelCallLedger(factory)
    requested = ledger.create_requested(
        request,
        effective_budget=budget,
        reasoning_included_in_completion_tokens=False,
    )
    ledger.settle(
        requested.model_copy(
            update={
                "status": ModelCallLedgerStatus.UNCERTAIN,
                "provider_sent_at": datetime.now(UTC),
                "transport_error_type": "TimeoutError",
            }
        )
    )

    with pytest.raises(RuntimeCommandConflictError, match="unresolved provider send"):
        commands.claim(task.task_id, worker_id="reconciliation-claim-block")
    assert commands.get_task(task.task_id) == task


def test_reconcile_model_call_is_atomic_audited_and_idempotent(
    reconciliation_kernel: tuple[
        sessionmaker[Session],
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, artifacts, events, commands, base = reconciliation_kernel
    task, request, attempt_id = _task_and_request(factory, commands, base)
    before = commands.get_task(task.task_id)
    ledger = SqlModelCallLedger(factory)
    entry = ledger.load(request.request_id)
    assert entry is not None
    evidence_ref = _evidence_ref(
        artifacts,
        task=before,
        request=request,
        attempt_id=attempt_id,
        request_hash=entry.request_hash,
    )

    reconciled = commands.reconcile_model_call(
        before.task_id,
        request_id=request.request_id,
        request_hash=entry.request_hash,
        evidence_ref=evidence_ref,
        command_id=StableId("reconcile-model-call.1"),
        actor_id="operator.reconciler",
        reason="provider audit proves the send expired without a usable response",
        observed_revision=before.task_revision,
    )

    assert reconciled.status is ModelCallLedgerStatus.TRANSPORT_EXHAUSTED
    assert reconciled.transport_error_type == "TimeoutError"
    assert commands.get_task(before.task_id) == before
    stored = ledger.load(request.request_id)
    assert stored == reconciled
    run_events = events.replay(before.run_id)
    assert run_events[-1].artifact_refs == (evidence_ref,)
    assert_task_projection_matches(run_events, (before,))

    again = commands.reconcile_model_call(
        before.task_id,
        request_id=request.request_id,
        request_hash=entry.request_hash,
        evidence_ref=evidence_ref,
        command_id=StableId("reconcile-model-call.1"),
        actor_id="operator.reconciler",
        reason="provider audit proves the send expired without a usable response",
        observed_revision=before.task_revision,
    )
    assert again == reconciled
    assert events.replay(before.run_id) == run_events


def test_reconcile_model_call_rejects_identity_drift_and_command_collision(
    reconciliation_kernel: tuple[
        sessionmaker[Session],
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, artifacts, _events, commands, base = reconciliation_kernel
    task, request, attempt_id = _task_and_request(factory, commands, base)
    before = commands.get_task(task.task_id)
    entry = SqlModelCallLedger(factory).load(request.request_id)
    assert entry is not None
    wrong_ref = _evidence_ref(
        artifacts,
        task=before,
        request=request,
        attempt_id=attempt_id,
        request_hash=ArtifactId("sha256:" + "f" * 64),
        evidence_locator="provider-audit/wrong-hash",
    )
    with pytest.raises(RuntimeCommandConflictError, match="identity"):
        commands.reconcile_model_call(
            before.task_id,
            request_id=request.request_id,
            request_hash=entry.request_hash,
            evidence_ref=wrong_ref,
            command_id=StableId("reconcile-model-call.wrong-hash"),
            actor_id="operator.reconciler",
            reason="wrong evidence must fail closed",
            observed_revision=before.task_revision,
        )
    assert SqlModelCallLedger(factory).load(request.request_id) == entry
    assert commands.get_task(task.task_id) == before

    evidence_ref = _evidence_ref(
        artifacts,
        task=before,
        request=request,
        attempt_id=attempt_id,
        request_hash=entry.request_hash,
    )
    command_id = StableId("reconcile-model-call.collision")
    commands.reconcile_model_call(
        before.task_id,
        request_id=request.request_id,
        request_hash=entry.request_hash,
        evidence_ref=evidence_ref,
        command_id=command_id,
        actor_id="operator.reconciler",
        reason="first authoritative settlement",
        observed_revision=before.task_revision,
    )
    with pytest.raises(RuntimeCommandConflictError, match="only an uncertain"):
        commands.reconcile_model_call(
            before.task_id,
            request_id=request.request_id,
            request_hash=entry.request_hash,
            evidence_ref=evidence_ref,
            command_id=command_id,
            actor_id="operator.reconciler",
            reason="different command intent",
            observed_revision=before.task_revision,
        )


def test_reconcile_model_call_cli_persists_evidence_and_returns_entry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "runtime.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    objects = tmp_path / "objects"
    artifacts = ArtifactRepository(FilesystemObjectStore(objects))
    events = RunEventLogRepository(factory)
    commands = RuntimeCommandService(
        factory,
        events,
        lambda _project_id: PERMISSION_HASH,
        artifacts=artifacts,
    )
    task, request, attempt_id = _task_and_request(factory, commands, base)
    entry = SqlModelCallLedger(factory).load(request.request_id)
    assert entry is not None
    evidence = ModelCallReconciliationEvidence(
        request_id=request.request_id,
        run_id=task.run_id,
        task_id=task.task_id,
        attempt_id=attempt_id,
        request_hash=entry.request_hash,
        outcome=ModelCallReconciliationOutcome.PROVIDER_CANCELLED,
        source=ModelCallReconciliationSource.AUTHORIZED_OPERATOR,
        evidence_locator="operator-record/cli-test",
        attested_by="operator.cli",
        observed_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_bytes(canonical_json_bytes(evidence.model_dump(mode="json")))
    engine.dispose()

    exit_code = main(
        [
            "runtime",
            "--database-url",
            f"sqlite+pysqlite:///{database_path}",
            "reconcile-model-call",
            "--project-id",
            task.project_id.root,
            "--run-id",
            task.run_id.root,
            "--task-id",
            task.task_id.root,
            "--request-id",
            request.request_id.root,
            "--request-hash",
            entry.request_hash.root,
            "--observed-revision",
            str(task.task_revision + 1),
            "--command-id",
            "reconcile-model-call.cli",
            "--actor-id",
            "operator.cli",
            "--reason",
            "provider audit confirms cancellation",
            "--evidence",
            str(evidence_path),
            "--object-store-root",
            str(objects),
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["entry"]["status"] == ModelCallLedgerStatus.TRANSPORT_EXHAUSTED.value
    verify_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    verify_factory = build_session_factory(verify_engine)
    verified = SqlModelCallLedger(verify_factory).load(request.request_id)
    assert verified is not None
    assert verified.status is ModelCallLedgerStatus.TRANSPORT_EXHAUSTED
    verify_engine.dispose()
