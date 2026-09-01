from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import RunEventRow
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelCallRecord,
    ModelRole,
    ModelUsage,
    RetrievalInferenceCallRecord,
    RetrievalInferenceOperation,
    RetrievalInferenceStatus,
    RetrievalInferenceUsage,
)
from novel_agent.domain.runtime import (
    ResumabilityStatus,
    RunCheckpoint,
    RunEvent,
    RunEventType,
)
from novel_agent.services.event_log import (
    CheckpointConflictError,
    EventLogConflictError,
    EventSequenceError,
    RunCheckpointRepository,
    RunEventLogRepository,
)


@pytest.fixture
def repositories() -> Iterator[tuple[RunEventLogRepository, RunCheckpointRepository]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    yield RunEventLogRepository(factory), RunCheckpointRepository(factory)
    engine.dispose()


def make_event(
    sequence_no: int,
    *,
    run_id: str = "run.test",
    event_id: str | None = None,
    identity: str | None = None,
) -> RunEvent:
    return RunEvent(
        event_id=StableId(event_id or f"event.{run_id}.{sequence_no}"),
        run_id=RunId(run_id),
        task_id=TaskId("task.test"),
        sequence_no=sequence_no,
        event_type=RunEventType.TASK_STARTED,
        occurred_at=datetime(2026, 7, 20, 12, sequence_no, tzinfo=UTC),
        idempotency_identity=StableId(identity or f"identity.{run_id}.{sequence_no}"),
        payload_schema_version=SchemaVersion("0.1.0"),
        trace_id=f"trace-{sequence_no}",
        payload={"sequence": sequence_no},
    )


def make_checkpoint(position: int, *, checkpoint_id: str = "checkpoint.test") -> RunCheckpoint:
    return RunCheckpoint(
        checkpoint_id=StableId(checkpoint_id),
        run_id=RunId("run.test"),
        event_position=position,
        logical_stage="process_chapter",
        state_artifact_ref=ArtifactRef(
            artifact_id=ArtifactId("sha256:" + "a" * 64),
            media_type="application/json",
            byte_length=10,
            schema_version=SchemaVersion("0.1.0"),
        ),
        resumability_status=ResumabilityStatus.RESUMABLE,
    )


def make_model_record(**updates: object) -> ModelCallRecord:
    values: dict[str, object] = {
        "request_id": StableId("model.request.1"),
        "run_id": RunId("run.test"),
        "task_id": TaskId("task.test"),
        "model_role": ModelRole.BATCH_TEST,
        "purpose": ModelCallPurpose.BATCH_TEST,
        "trace_id": "trace-model",
        "span_id": "span-model",
        "endpoint": "batch-endpoint",
        "model": "batch-model",
        "model_version": "batch-v1",
        "usage": ModelUsage(input_tokens=10, output_tokens=2, cost_usd=Decimal("0.01")),
        "latency_ms": 25,
        "started_at": datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        "completed_at": datetime(2026, 7, 20, 12, 0, 1, tzinfo=UTC),
    }
    values.update(updates)
    return ModelCallRecord.model_validate(values)


def make_model_event(
    record: ModelCallRecord | RetrievalInferenceCallRecord | None,
) -> RunEvent:
    return RunEvent(
        event_id=StableId("event.run.test.1"),
        run_id=RunId("run.test"),
        task_id=TaskId("task.test"),
        sequence_no=1,
        event_type=RunEventType.MODEL_COMPLETED,
        occurred_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        idempotency_identity=StableId("identity.run.test.1"),
        payload_schema_version=SchemaVersion("0.1.0"),
        trace_id="trace-model",
        span_id="span-model",
        payload={"purpose": "batch_test"},
        model_call_record=record,
    )


def make_retrieval_record() -> RetrievalInferenceCallRecord:
    return RetrievalInferenceCallRecord(
        call_id=StableId("retrieval-call.test.1"),
        run_id=RunId("run.test"),
        task_id=TaskId("task.test"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.EVALUATION,
        trace_id="trace-model",
        span_id="span-model",
        endpoint="http://127.0.0.1:8081/v1/embeddings",
        model="BAAI/bge-m3",
        revision="a" * 40,
        runtime_fingerprint="b" * 64,
        operation=RetrievalInferenceOperation.EMBEDDING,
        usage=RetrievalInferenceUsage(
            input_items=2,
            input_characters=20,
            output_items=2,
            cost_usd=Decimal("0"),
        ),
        latency_ms=10,
        started_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        completed_at=datetime(2026, 7, 20, 12, 0, 1, tzinfo=UTC),
        status=RetrievalInferenceStatus.SUCCEEDED,
    )


def test_append_is_ordered_idempotent_and_replayable(
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    events, _ = repositories
    first = make_event(1)
    second = make_event(2)

    assert events.next_sequence(RunId("run.test")) == 1
    assert events.append(first) == first
    assert events.next_sequence(RunId("run.test")) == 2
    assert events.append(first) == first
    assert events.append(second) == second
    assert events.next_sequence(RunId("run.test")) == 3
    assert events.replay(RunId("run.test")) == (first, second)
    assert events.replay(RunId("run.test"), after_sequence=1) == (second,)
    assert events.replay(RunId("run.missing")) == ()


def test_completed_model_call_audit_round_trips_through_event_log(
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    events, _ = repositories
    event = make_model_event(make_model_record())

    events.append(event)

    restored = events.replay(RunId("run.test"))[0]
    assert restored == event
    assert isinstance(restored.model_call_record, ModelCallRecord)
    assert restored.model_call_record.model_role is ModelRole.BATCH_TEST
    assert restored.model_call_record.endpoint == "batch-endpoint"
    assert restored.model_call_record.model_version == "batch-v1"
    assert restored.model_call_record.usage.cost_usd == Decimal("0.01")
    assert restored.model_call_record.latency_ms == 25
    assert restored.model_call_record.purpose is ModelCallPurpose.BATCH_TEST


def test_retrieval_inference_audit_round_trips_through_event_log(
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    events, _ = repositories
    event = make_model_event(make_retrieval_record())

    events.append(event)

    restored = events.replay(RunId("run.test"))[0]
    assert restored == event
    assert isinstance(restored.model_call_record, RetrievalInferenceCallRecord)
    assert restored.model_call_record.operation is RetrievalInferenceOperation.EMBEDDING
    assert restored.model_call_record.usage.input_items == 2


def test_model_call_audit_contract_rejects_missing_or_misaligned_records() -> None:
    with pytest.raises(ValidationError, match="requires a complete"):
        make_model_event(None)

    valid = make_model_record()
    with pytest.raises(ValidationError, match="only valid on model events"):
        RunEvent.model_validate(
            make_model_event(valid).model_dump() | {"event_type": RunEventType.TASK_STARTED}
        )

    for record in (
        make_model_record(run_id=RunId("run.other")),
        make_model_record(task_id=TaskId("task.other")),
    ):
        with pytest.raises(ValidationError, match="event run and task"):
            make_model_event(record)

    for record in (
        make_model_record(trace_id="trace-other"),
        make_model_record(span_id="span-other"),
    ):
        with pytest.raises(ValidationError, match="event trace and span"):
            make_model_event(record)


def test_idempotent_retry_preserves_the_first_occurrence_time(
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    events, _ = repositories
    first = make_event(1)
    retry = first.model_copy(update={"occurred_at": datetime(2026, 7, 20, 13, 0, tzinfo=UTC)})

    events.append(first)

    assert events.append(retry) == first


def test_each_run_has_an_independent_sequence(
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    events, _ = repositories
    first = make_event(1, run_id="run.one")
    other = make_event(1, run_id="run.two")

    events.append(first)
    events.append(other)

    assert events.replay(RunId("run.one")) == (first,)
    assert events.replay(RunId("run.two")) == (other,)


def test_sequence_gaps_and_rewinds_are_rejected(
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    events, _ = repositories
    with pytest.raises(EventSequenceError, match="expects sequence 1"):
        events.append(make_event(2))
    events.append(make_event(1))
    with pytest.raises(EventSequenceError, match="expects sequence 2"):
        events.append(make_event(3))


def test_event_id_and_idempotency_collisions_are_rejected(
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    events, _ = repositories
    events.append(make_event(1))

    with pytest.raises(EventLogConflictError, match="idempotency"):
        events.append(make_event(2, identity="identity.run.test.1"))
    with pytest.raises(EventLogConflictError, match="event_id"):
        events.append(make_event(2, event_id="event.run.test.1"))


def test_checkpoint_is_bound_to_an_existing_event_and_idempotent(
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    events, checkpoints = repositories
    events.append(make_event(1))
    checkpoint = make_checkpoint(1)

    assert checkpoints.save(checkpoint) == checkpoint
    assert checkpoints.save(checkpoint) == checkpoint
    assert checkpoints.latest(RunId("run.test")) == checkpoint


def test_latest_checkpoint_uses_highest_event_position(
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    events, checkpoints = repositories
    events.append(make_event(1))
    events.append(make_event(2))
    first = make_checkpoint(1, checkpoint_id="checkpoint.1")
    second = make_checkpoint(2, checkpoint_id="checkpoint.2")

    checkpoints.save(first)
    checkpoints.save(second)

    assert checkpoints.latest(RunId("run.test")) == second


def test_invalid_checkpoints_are_rejected(
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    events, checkpoints = repositories
    assert checkpoints.latest(RunId("run.missing")) is None
    with pytest.raises(CheckpointConflictError, match="high watermark"):
        checkpoints.save(make_checkpoint(1))

    events.append(make_event(1))
    with pytest.raises(CheckpointConflictError, match="high watermark"):
        checkpoints.save(make_checkpoint(2))

    checkpoint = make_checkpoint(1)
    checkpoints.save(checkpoint)
    changed = checkpoint.model_copy(update={"logical_stage": "different"})
    with pytest.raises(CheckpointConflictError, match="another checkpoint"):
        checkpoints.save(changed)


def test_checkpoint_rejects_missing_position_inside_high_watermark(
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    events, checkpoints = repositories
    events.append(make_event(1))
    events.append(make_event(2))
    factory = events._session_factory
    with factory.begin() as session:
        row = session.get(RunEventRow, "event.run.test.1")
        assert row is not None
        session.delete(row)

    with pytest.raises(CheckpointConflictError, match="does not exist"):
        checkpoints.save(make_checkpoint(1))
