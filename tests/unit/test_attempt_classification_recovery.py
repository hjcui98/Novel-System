"""N4 (2026-09-13 guidance): canonical failure classification and a real exit code.

The v23 driver matched uppercase words in ``block_cause``.  That field is only set
for ``BLOCKED``, so a task waiting for a retry reported ``block_cause=null`` and the
driver fell through to "a deterministic failure, no blind retry" -- stopping on a
failure that was retryable.  The classification was in the attempt row the whole
time, as a lowercase :class:`FailureClass` the driver never read.

These tests go through the real chain the CLI uses: a persisted attempt row, the
effect projection for that task, and the shared failure policy.  A test that
invented a ``block_cause`` the script liked would prove nothing about the run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.models import (
    ModelCallLedgerRow,
    RuntimeEffectProjectionRow,
    RuntimeTaskAttemptRow,
)
from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.domain.ids import ProjectId, RunId, StableId, TaskId
from novel_agent.domain.model_calls import ModelCallLedgerStatus
from novel_agent.domain.runtime import (
    AttemptOutcome,
    EffectStatus,
    FailureClass,
    RetryOwner,
    TaskAttempt,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.services.attempt_classification import (
    RecoveryAction,
    classify_attempt,
    policy_for,
)

HASH = "sha256:" + "1" * 64
PROJECT = ProjectId("project.n4")
RUN = RunId("run.n4")
NOW = datetime(2026, 9, 13, tzinfo=UTC)


def _attempt(
    *,
    task_id: str = "task.n4",
    attempt_no: int = 1,
    failure: FailureClass | None = FailureClass.PROVIDER_TRANSIENT,
    outcome: AttemptOutcome | None = AttemptOutcome.FAILED,
) -> TaskAttempt:
    return TaskAttempt(
        attempt_id=StableId(f"attempt.n4.{attempt_no}"),
        task_id=TaskId(task_id),
        attempt_no=attempt_no,
        worker_id="worker.n4",
        claim_token_digest=HASH,
        fence_generation=1,
        claimed_at=NOW,
        heartbeat_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=60),
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=5),
        outcome=outcome,
        failure_class=failure,
    )


def _classify(
    *,
    attempt: TaskAttempt | None,
    status: TaskStatus = TaskStatus.WAITING_RETRY,
    unsettled: tuple[str, ...] = (),
    outstanding: tuple[str, ...] = (),
    completed: tuple[str, ...] = (),
    block_cause: str | None = None,
):
    return classify_attempt(
        task_id=StableId("task.n4"),
        task_status=status,
        attempt=attempt,
        unsettled_sends=unsettled,
        outstanding_request_ids=outstanding,
        completed_response_refs=completed,
        block_cause=block_cause,
    )


# ------------------------------------------------- V10: classification from the ledger


def test_waiting_retry_with_a_null_block_cause_is_still_classified() -> None:
    """The v23 snapshot's exact shape: waiting_retry, block_cause null."""

    result = _classify(attempt=_attempt(), block_cause=None)

    assert result.action is RecoveryAction.RETRY_UNDER_POLICY
    assert result.safe_to_retry
    assert result.failure_class is FailureClass.PROVIDER_TRANSIENT
    assert result.settled_attempt_id == StableId("attempt.n4.1")
    assert result.retry_owner is RetryOwner.MODEL_GATEWAY


def test_a_lowercase_provider_transient_class_is_read_as_its_enum() -> None:
    """The canonical class is a lowercase enum, not an uppercase log word."""

    result = _classify(attempt=_attempt(failure=FailureClass("provider_transient")))

    assert result.failure_class is FailureClass.PROVIDER_TRANSIENT
    assert result.action is RecoveryAction.RETRY_UNDER_POLICY


def test_an_unsettled_send_outranks_a_retryable_class() -> None:
    """A transient failure with a send in flight is not safe to retry blind."""

    result = _classify(
        attempt=_attempt(),
        unsettled=("effect.n4.provider-send",),
    )

    assert result.action is RecoveryAction.RECONCILE_FIRST
    assert not result.safe_to_retry
    assert result.unsettled_sends == ("effect.n4.provider-send",)
    assert "reconcile" in result.reason


def test_an_outstanding_request_outranks_a_retryable_class() -> None:
    result = _classify(attempt=_attempt(), outstanding=("effect.n4.requested",))

    assert result.action is RecoveryAction.RECONCILE_FIRST
    assert not result.safe_to_retry


def test_a_completed_response_is_replayed_not_re_requested() -> None:
    """The provider already answered; a second call would buy the same answer."""

    result = _classify(
        attempt=_attempt(),
        completed=("effect.n4.done",),
    )

    assert result.action is RecoveryAction.REPLAY_COMPLETED
    assert not result.safe_to_retry
    assert result.completed_response_refs == ("effect.n4.done",)


def test_a_durable_response_outranks_an_uncertain_send() -> None:
    """Replay is safe even when a send is uncertain: it answers the question."""

    result = _classify(
        attempt=_attempt(),
        unsettled=("effect.n4.uncertain",),
        completed=("effect.n4.done",),
    )

    assert result.action is RecoveryAction.RECONCILE_FIRST


def test_configuration_and_basis_drift_stop_dependent_work() -> None:
    for failure in (FailureClass.BASIS_CHANGED, FailureClass.PERMISSION_DENIED):
        result = _classify(attempt=_attempt(failure=failure))
        assert result.action is RecoveryAction.STOP_DEPENDENT_WORK, failure
        assert not result.safe_to_retry


def test_a_deterministic_refusal_goes_to_the_owning_module() -> None:
    for failure in (
        FailureClass.LEAF_SCHEMA_REJECTED,
        FailureClass.LEAF_REVIEW_REQUIRED,
        FailureClass.VALIDATION_REJECTED,
        FailureClass.PROJECTION_FAILED,
    ):
        result = _classify(attempt=_attempt(failure=failure))
        assert result.action is RecoveryAction.REPAIR_OWNING_MODULE, failure
        assert not result.safe_to_retry


def test_an_uncertain_effect_is_reconciled_first() -> None:
    result = _classify(attempt=_attempt(failure=FailureClass.EFFECT_UNCERTAIN))

    assert result.action is RecoveryAction.RECONCILE_FIRST
    assert not result.safe_to_retry


def test_an_exhausted_budget_escalates_rather_than_retrying() -> None:
    result = _classify(attempt=_attempt(failure=FailureClass.BUDGET_EXHAUSTED))

    assert result.action is RecoveryAction.ESCALATE_BUDGET
    assert not result.safe_to_retry


def test_an_unknown_failure_is_reported_as_undetermined() -> None:
    """Unknown must not be presented as deterministic, nor default to retryable."""

    result = _classify(attempt=_attempt(failure=FailureClass.UNKNOWN))

    assert result.action is RecoveryAction.UNDETERMINED
    assert not result.safe_to_retry
    assert "must not be presented" in result.reason


def test_no_settled_attempt_is_undetermined_not_retryable() -> None:
    result = _classify(attempt=None, block_cause="SOMETHING_UPPERCASE")

    assert result.action is RecoveryAction.UNDETERMINED
    assert not result.safe_to_retry
    assert "no attempt has settled" in result.reason
    # The non-canonical field is reported, and explicitly not used to decide.
    assert "not the canonical classification" in result.reason


def test_an_attempt_with_no_failure_class_is_undetermined() -> None:
    result = _classify(attempt=_attempt(failure=None))

    assert result.action is RecoveryAction.UNDETERMINED
    assert "recorded no failure class" in result.reason


def test_every_failure_class_has_a_decided_action() -> None:
    """No class may fall through to a default that happens to look like a retry."""

    for failure in FailureClass:
        result = _classify(attempt=_attempt(failure=failure))
        assert result.action in set(RecoveryAction), failure
        if failure is FailureClass.UNKNOWN:
            assert result.action is RecoveryAction.UNDETERMINED
            continue
        if result.action is RecoveryAction.RETRY_UNDER_POLICY:
            # A retry decision must agree with the shared policy, not override it.
            assert policy_for(failure).retryable, failure


def test_the_classification_carries_the_shared_policy_it_used() -> None:
    result = _classify(attempt=_attempt(failure=FailureClass.LEAF_SCHEMA_REJECTED))
    policy = policy_for(FailureClass.LEAF_SCHEMA_REJECTED)

    assert result.retryable is policy.retryable
    assert result.retry_owner is policy.retry_owner
    assert result.consumes_task_budget is policy.consumes_task_budget
    assert result.consumes_creative_budget is policy.consumes_creative_budget


# ----------------------------------------------------- the persisted read path


@pytest.fixture
def repository(tmp_path: Path) -> RuntimeTaskQueryRepository:
    from novel_agent.adapters.postgres.models import Base, RuntimeTaskProjectionRow

    engine = build_engine(f"sqlite+pysqlite:///{tmp_path / 'n4.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session, session.begin():
        session.add(
            RuntimeTaskProjectionRow(
                task_id="task.n4.persisted",
                run_id=RUN.root,
                project_id=PROJECT.root,
                kind=TaskKind.PLAN_CANDIDATE.value,
                status=TaskStatus.WAITING_RETRY.value,
                revision=3,
                priority=0,
                updated_at=NOW,
                basis_commit=HASH,
                policy_hash=HASH,
                permission_hash=HASH,
                task_json=TaskRecord(
                    task_id=TaskId("task.n4.persisted"),
                    run_id=RUN,
                    project_id=PROJECT,
                    kind=TaskKind.PLAN_CANDIDATE,
                    task_revision=3,
                    status=TaskStatus.WAITING_RETRY,
                    basis_commit=HASH,
                    policy_hash=HASH,
                    permission_hash=HASH,
                ).model_dump(mode="json"),
            )
        )
    return RuntimeTaskQueryRepository(factory)


def _persist_attempt(
    repository: RuntimeTaskQueryRepository,
    attempt: TaskAttempt,
    *,
    effects: tuple[tuple[str, EffectStatus], ...] = (),
) -> None:
    with repository.session_factory() as session, session.begin():
        session.add(
            RuntimeTaskAttemptRow(
                attempt_id=attempt.attempt_id.root,
                task_id=attempt.task_id.root,
                attempt_no=attempt.attempt_no,
                worker_id=attempt.worker_id,
                claim_digest=attempt.claim_token_digest,
                fence_generation=attempt.fence_generation,
                claimed_at=attempt.claimed_at,
                heartbeat_at=attempt.heartbeat_at,
                lease_expires_at=attempt.lease_expires_at,
                started_at=attempt.started_at,
                ended_at=attempt.ended_at,
                outcome=attempt.outcome,
                failure_class=(
                    None if attempt.failure_class is None else attempt.failure_class.value
                ),
                attempt_json=attempt.model_dump(mode="json"),
            )
        )
        for effect_id, status in effects:
            session.add(
                RuntimeEffectProjectionRow(
                    effect_identity=effect_id,
                    request_identity=f"{effect_id}.request",
                    run_id=RUN.root,
                    task_id=attempt.task_id.root,
                    attempt_id=attempt.attempt_id.root,
                    status=status.value,
                    effect_json={},
                )
            )


def _persist_model_call(
    repository: RuntimeTaskQueryRepository,
    *,
    request_id: str,
    task_id: str = "task.n4.persisted",
    attempt_id: str = "attempt.n4.1",
    status: ModelCallLedgerStatus,
    raw_artifact_id: str | None = None,
    response_consumed_at: datetime | None = None,
) -> None:
    with repository.session_factory() as session, session.begin():
        session.add(
            ModelCallLedgerRow(
                request_id=request_id,
                run_id=RUN.root,
                task_id=task_id,
                attempt_id=attempt_id,
                request_hash=HASH,
                status=status.value,
                logical_phase="plan",
                effective_budget_json={},
                reasoning_included_in_completion_tokens=False,
                provider_request_id=f"provider.{request_id}",
                provider_sent_at=NOW,
                raw_response_hash=None,
                raw_artifact_json=(
                    None if raw_artifact_id is None else {"artifact_id": raw_artifact_id}
                ),
                call_record_json=None,
                validation_error=None,
                transport_error_type=None,
                requested_at=NOW,
                completed_at=(NOW + timedelta(seconds=5))
                if status is ModelCallLedgerStatus.COMPLETED
                else None,
                response_consumed_at=response_consumed_at,
            )
        )


def test_the_persisted_read_path_classifies_a_waiting_task(
    repository: RuntimeTaskQueryRepository,
) -> None:
    """The chain the CLI uses: attempt row -> ledger -> classification."""

    attempt = _attempt(task_id="task.n4.persisted")
    _persist_attempt(repository, attempt)
    task_id = TaskId("task.n4.persisted")

    loaded = repository.last_settled_attempt(task_id)
    unsettled, outstanding, completed = repository.attempt_effect_ledger(task_id)
    result = classify_attempt(
        task_id=StableId(task_id.root),
        task_status=repository.get_task(task_id).status,
        attempt=loaded,
        unsettled_sends=unsettled,
        outstanding_request_ids=outstanding,
        completed_response_refs=completed,
    )

    assert loaded is not None and loaded.failure_class is FailureClass.PROVIDER_TRANSIENT
    assert result.action is RecoveryAction.RETRY_UNDER_POLICY
    assert result.task_status is TaskStatus.WAITING_RETRY


def test_the_read_path_surfaces_an_uncertain_send(
    repository: RuntimeTaskQueryRepository,
) -> None:
    attempt = _attempt(task_id="task.n4.persisted")
    _persist_attempt(repository, attempt, effects=(("effect.uncertain", EffectStatus.UNCERTAIN),))
    task_id = TaskId("task.n4.persisted")

    unsettled, outstanding, completed = repository.attempt_effect_ledger(task_id)
    result = classify_attempt(
        task_id=StableId(task_id.root),
        task_status=TaskStatus.WAITING_RETRY,
        attempt=repository.last_settled_attempt(task_id),
        unsettled_sends=unsettled,
        outstanding_request_ids=outstanding,
        completed_response_refs=completed,
    )

    assert result.action is RecoveryAction.RECONCILE_FIRST
    assert not result.safe_to_retry


def test_the_read_path_surfaces_a_completed_response(
    repository: RuntimeTaskQueryRepository,
) -> None:
    attempt = _attempt(task_id="task.n4.persisted")
    _persist_attempt(repository, attempt, effects=(("effect.done", EffectStatus.COMPLETED),))
    task_id = TaskId("task.n4.persisted")

    unsettled, outstanding, completed = repository.attempt_effect_ledger(task_id)
    result = classify_attempt(
        task_id=StableId(task_id.root),
        task_status=TaskStatus.WAITING_RETRY,
        attempt=repository.last_settled_attempt(task_id),
        unsettled_sends=unsettled,
        outstanding_request_ids=outstanding,
        completed_response_refs=completed,
    )

    # A completed commit/projection effect is not a model response and must not
    # make a provider retry look replayable.
    assert result.action is RecoveryAction.RETRY_UNDER_POLICY
    assert completed == ()


def test_historical_completed_response_is_not_the_current_recovery_frontier(
    repository: RuntimeTaskQueryRepository,
) -> None:
    first = _attempt(task_id="task.n4.persisted", attempt_no=1)
    _persist_attempt(repository, first)
    _persist_model_call(
        repository,
        request_id="request.n4.old.completed",
        attempt_id=first.attempt_id.root,
        status=ModelCallLedgerStatus.COMPLETED,
        raw_artifact_id="artifact.old.response",
    )
    current = _attempt(
        task_id="task.n4.persisted",
        attempt_no=2,
        failure=FailureClass.PROVIDER_TRANSIENT,
    )
    _persist_attempt(repository, current)
    _persist_model_call(
        repository,
        request_id="request.n4.current.uncertain",
        attempt_id=current.attempt_id.root,
        status=ModelCallLedgerStatus.UNCERTAIN,
    )

    evidence = repository.attempt_effect_evidence(TaskId("task.n4.persisted"))
    result = classify_attempt(
        task_id=StableId("task.n4.persisted"),
        task_status=TaskStatus.WAITING_RETRY,
        attempt=repository.last_settled_attempt(TaskId("task.n4.persisted")),
        unsettled_sends=evidence.unsettled_sends,
        outstanding_request_ids=evidence.outstanding_request_ids,
        completed_response_refs=evidence.completed_response_refs,
        unavailable_response_ids=evidence.unavailable_response_ids,
        frontier_attempt_id=evidence.frontier_attempt_id,
    )

    assert evidence.frontier_attempt_id == current.attempt_id
    assert evidence.completed_response_refs == ()
    assert evidence.unsettled_sends == ("request.n4.current.uncertain",)
    assert result.action is RecoveryAction.RECONCILE_FIRST
    assert not result.safe_to_retry


def test_current_completed_response_is_replayable_but_old_one_is_not(
    repository: RuntimeTaskQueryRepository,
) -> None:
    first = _attempt(task_id="task.n4.persisted", attempt_no=1)
    _persist_attempt(repository, first)
    _persist_model_call(
        repository,
        request_id="request.n4.old.completed",
        attempt_id=first.attempt_id.root,
        status=ModelCallLedgerStatus.COMPLETED,
        raw_artifact_id="artifact.old.response",
    )
    current = _attempt(task_id="task.n4.persisted", attempt_no=2)
    _persist_attempt(repository, current)
    _persist_model_call(
        repository,
        request_id="request.n4.current.completed",
        attempt_id=current.attempt_id.root,
        status=ModelCallLedgerStatus.COMPLETED,
        raw_artifact_id="artifact.current.response",
    )

    evidence = repository.attempt_effect_evidence(TaskId("task.n4.persisted"))

    assert evidence.completed_response_refs == ("artifact.current.response",)


def test_completed_model_call_without_raw_evidence_requires_reconciliation(
    repository: RuntimeTaskQueryRepository,
) -> None:
    attempt = _attempt(task_id="task.n4.persisted")
    _persist_attempt(repository, attempt)
    _persist_model_call(
        repository,
        request_id="request.n4.missing.raw",
        status=ModelCallLedgerStatus.COMPLETED,
    )

    evidence = repository.attempt_effect_evidence(TaskId("task.n4.persisted"))
    result = classify_attempt(
        task_id=StableId("task.n4.persisted"),
        task_status=TaskStatus.WAITING_RETRY,
        attempt=repository.last_settled_attempt(TaskId("task.n4.persisted")),
        unavailable_response_ids=evidence.unavailable_response_ids,
        frontier_attempt_id=evidence.frontier_attempt_id,
    )

    assert evidence.completed_response_refs == ()
    assert evidence.unavailable_response_ids == ("request.n4.missing.raw",)
    assert result.action is RecoveryAction.RECONCILE_FIRST
    assert not result.safe_to_retry


def test_consumed_response_does_not_mask_a_later_deterministic_failure(
    repository: RuntimeTaskQueryRepository,
) -> None:
    """A completed Memory response is history once the later logical phase consumed it."""

    attempt = _attempt(
        task_id="task.n4.persisted",
        failure=FailureClass.LEAF_SCHEMA_REJECTED,
    )
    _persist_attempt(repository, attempt)
    _persist_model_call(
        repository,
        request_id="request.n4.memory.completed",
        status=ModelCallLedgerStatus.COMPLETED,
        raw_artifact_id="artifact.memory.response",
        response_consumed_at=NOW + timedelta(seconds=6),
    )

    evidence = repository.attempt_effect_evidence(TaskId("task.n4.persisted"))
    result = classify_attempt(
        task_id=StableId("task.n4.persisted"),
        task_status=TaskStatus.WAITING_RETRY,
        attempt=attempt,
        completed_response_refs=evidence.completed_response_refs,
        consumed_response_ids=evidence.consumed_response_ids,
        unavailable_response_ids=evidence.unavailable_response_ids,
        frontier_attempt_id=evidence.frontier_attempt_id,
    )

    assert evidence.completed_response_refs == ()
    assert evidence.consumed_response_ids == ("request.n4.memory.completed",)
    assert result.action is RecoveryAction.REPAIR_OWNING_MODULE
    assert result.completed_response_refs == ()


def test_the_persisted_model_ledger_surfaces_an_uncertain_provider_request(
    repository: RuntimeTaskQueryRepository,
) -> None:
    _persist_attempt(repository, _attempt(task_id="task.n4.persisted"))
    _persist_model_call(
        repository,
        request_id="request.n4.uncertain",
        status=ModelCallLedgerStatus.UNCERTAIN,
    )

    unsettled, outstanding, completed = repository.attempt_effect_ledger(
        TaskId("task.n4.persisted")
    )

    assert unsettled == ("request.n4.uncertain",)
    assert outstanding == ()
    assert completed == ()


def test_the_persisted_completed_model_ledger_returns_the_response_artifact(
    repository: RuntimeTaskQueryRepository,
) -> None:
    _persist_attempt(repository, _attempt(task_id="task.n4.persisted"))
    _persist_model_call(
        repository,
        request_id="request.n4.completed",
        status=ModelCallLedgerStatus.COMPLETED,
        raw_artifact_id="artifact.response.n4",
    )

    unsettled, outstanding, completed = repository.attempt_effect_ledger(
        TaskId("task.n4.persisted")
    )
    result = classify_attempt(
        task_id=StableId("task.n4.persisted"),
        task_status=TaskStatus.WAITING_RETRY,
        attempt=repository.last_settled_attempt(TaskId("task.n4.persisted")),
        unsettled_sends=unsettled,
        outstanding_request_ids=outstanding,
        completed_response_refs=completed,
    )

    assert unsettled == ()
    assert outstanding == ()
    assert completed == ("artifact.response.n4",)
    assert result.action is RecoveryAction.REPLAY_COMPLETED
    assert result.completed_response_refs == ("artifact.response.n4",)


def test_only_the_latest_settled_attempt_classifies(
    repository: RuntimeTaskQueryRepository,
) -> None:
    """An older attempt's failure must not be re-read as the current one."""

    task_id = "task.n4.persisted"
    _persist_attempt(repository, _attempt(task_id=task_id, attempt_no=1))
    _persist_attempt(
        repository,
        _attempt(task_id=task_id, attempt_no=2, failure=FailureClass.LEAF_SCHEMA_REJECTED),
    )

    loaded = repository.last_settled_attempt(TaskId(task_id))
    result = classify_attempt(
        task_id=StableId(task_id),
        task_status=TaskStatus.BLOCKED,
        attempt=loaded,
    )

    assert loaded is not None and loaded.attempt_no == 2
    assert result.failure_class is FailureClass.LEAF_SCHEMA_REJECTED
    assert result.action is RecoveryAction.REPAIR_OWNING_MODULE
