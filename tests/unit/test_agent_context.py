from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from pydantic import JsonValue, ValidationError
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.domain.agent_context import (
    CONTEXT_EVENT_SCHEMA_VERSION,
    AgentContextView,
    ContextCompactedPayload,
    ContextCompactionReceipt,
    ContextConsumer,
    ContextDelta,
    ContextDeltaAppliedPayload,
    ContextDeltaStatus,
    ContextItemKind,
    ContextLayer,
    ContextPressure,
    ContextViewItem,
    ProviderValidityReceipt,
    WriterWorkPlanSettledPayload,
    validate_stage3_event_payload,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.runtime import RunEvent, RunEventType
from novel_agent.services.agent_context import (
    AgentContextProjector,
    AgentContextRuntime,
    ContextCompactor,
    ContextLimitError,
    ContextProjectionError,
    ContextWindowPolicy,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.writer_context_assembler import WriterContextAssembler
from tests.fixtures.stage2_memory_benchmark import writer_context_inputs

VERSION = SchemaVersion("1.0.0")
ZERO = ArtifactId("sha256:" + "0" * 64)


def _count(text: str) -> int:
    return max(1, len(text))


def _ref(label: str = "a", media_type: str = "application/json") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + label * 64),
        media_type=media_type,
        byte_length=1,
        schema_version=VERSION,
    )


def _item(
    suffix: str,
    *,
    layer: ContextLayer = ContextLayer.MEMORY,
    kind: ContextItemKind = ContextItemKind.MEMORY_CLAIM,
    content: str = "visible fact",
    mandatory: bool = False,
    group: str | None = None,
    event_range: tuple[int, int] | None = None,
    pending: bool = False,
    scope: Literal["writer_safe", "planner_safe", "runtime"] = "writer_safe",
) -> ContextViewItem:
    return ContextViewItem(
        item_id=StableId(f"item.{suffix}"),
        layer=layer,
        kind=kind,
        content=content,
        token_count=_count(content),
        atomic_group_id=StableId(group) if group is not None else None,
        source_event_range=event_range,
        mandatory=mandatory,
        information_scope=scope,
        pending_effect=pending,
    )


def _view(**updates: object) -> AgentContextView:
    values: dict[str, object] = {
        "run_id": RunId("run.context"),
        "task_id": TaskId("task.context"),
        "consumer": ContextConsumer.WRITER,
        "revision": 0,
        "generation": 0,
        "basis_event_position": 1,
        "base_commit": CommitId("sha256:" + "1" * 64),
        "snapshot_id": StableId("snapshot.context"),
        "profile_ref": _ref("b"),
        "plan_ref": _ref("c"),
        "information_scope": "writer_safe",
        "seed_package_ref": _ref("d"),
        "protected_items": (
            _item(
                "protected",
                layer=ContextLayer.PROTECTED,
                kind=ContextItemKind.WRITING_TASK,
                mandatory=True,
            ),
        ),
        "context_hash": ZERO,
    }
    values.update(updates)
    return AgentContextView.model_validate(values)


def _event(
    sequence: int,
    event_type: RunEventType,
    payload: JsonValue,
    *,
    run_id: str = "run.context",
    task_id: str = "task.context",
) -> RunEvent:
    return RunEvent(
        event_id=StableId(f"event.context.{sequence}"),
        run_id=RunId(run_id),
        task_id=TaskId(task_id),
        sequence_no=sequence,
        event_type=event_type,
        occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
        idempotency_identity=StableId(f"event-identity.context.{sequence}"),
        payload_schema_version=CONTEXT_EVENT_SCHEMA_VERSION,
        trace_id="trace-context",
        payload=payload,
    )


@pytest.fixture
def repositories() -> Iterator[tuple[RunEventLogRepository, RunCheckpointRepository]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    yield RunEventLogRepository(factory), RunCheckpointRepository(factory)
    engine.dispose()


def test_context_item_and_delta_contracts_fail_closed() -> None:
    with pytest.raises(ValidationError, match="event range"):
        _item("range", event_range=(2, 1))
    with pytest.raises(ValidationError, match="masquerade"):
        _item(
            "summary",
            layer=ContextLayer.COMPACTED_PREFIX,
            kind=ContextItemKind.RUNTIME_SUMMARY,
        )
    with pytest.raises(ValidationError, match="pending effects"):
        _item(
            "pending",
            layer=ContextLayer.COMPACTED_PREFIX,
            kind=ContextItemKind.RUNTIME_SUMMARY,
            pending=True,
            scope="runtime",
        )

    view = _view()
    base = {
        "delta_id": StableId("delta.test"),
        "request_ref": _ref("e"),
        "resolution_ref": _ref("f"),
        "parent_view_revision": view.revision,
        "base_commit": view.base_commit,
        "snapshot_id": view.snapshot_id,
        "profile_ref": view.profile_ref,
        "plan_ref": view.plan_ref,
        "token_impact": 0,
        "status": ContextDeltaStatus.INSUFFICIENT,
    }
    with pytest.raises(ValidationError, match="only memory"):
        ContextDelta.model_validate(
            base | {"added_memory_items": (_item("working", layer=ContextLayer.WORKING),)}
        )
    duplicate = _item("duplicate")
    with pytest.raises(ValidationError, match="unique"):
        ContextDelta.model_validate(base | {"added_memory_items": (duplicate, duplicate)})
    with pytest.raises(ValidationError, match="add and supersede"):
        ContextDelta.model_validate(
            base
            | {
                "added_memory_items": (duplicate,),
                "superseded_item_ids": (duplicate.item_id,),
            }
        )
    with pytest.raises(ValidationError, match="must add evidence"):
        ContextDelta.model_validate(base | {"status": ContextDeltaStatus.RESOLVED})
    with pytest.raises(ValidationError, match="denied"):
        ContextDelta.model_validate(
            base
            | {
                "status": ContextDeltaStatus.DENIED,
                "added_memory_items": (duplicate,),
            }
        )


def test_provider_compaction_view_and_pressure_contracts() -> None:
    provider = {
        "receipt_id": StableId("provider.test"),
        "tokenizer": "test",
        "tokenizer_version": "v1",
        "sequence_limit": 100,
        "reserved_output_tokens": 10,
        "safety_allowance_tokens": 10,
        "rendered_input_tokens": 20,
        "available_input_tokens": 80,
        "atomic_groups_valid": True,
        "provider_valid": True,
        "context_hash": ZERO,
    }
    assert ProviderValidityReceipt.model_validate(provider).provider_valid
    with pytest.raises(ValidationError, match="capacity"):
        ProviderValidityReceipt.model_validate(provider | {"available_input_tokens": 81})
    with pytest.raises(ValidationError, match="validity"):
        ProviderValidityReceipt.model_validate(provider | {"provider_valid": False})

    receipt = {
        "receipt_id": StableId("compaction.test"),
        "run_id": RunId("run.context"),
        "parent_view_revision": 0,
        "parent_generation": 0,
        "basis_event_position": 2,
        "covered_event_range": (1, 2),
        "kept_boundary": 2,
        "removed_item_ids": (StableId("item.old"),),
        "compacted_items": (),
        "input_context_hash": ZERO,
        "output_context_hash": _ref("9").artifact_id,
        "deterministic": True,
        "safe_cut": True,
        "published_generation": 1,
    }
    assert ContextCompactionReceipt.model_validate(receipt).published_generation == 1
    for update, message in (
        ({"covered_event_range": (1, 3)}, "coverage"),
        ({"published_generation": 2}, "advance"),
        ({"safe_cut": False}, "unsafe"),
        ({"summary_artifact": _ref("7")}, "together"),
    ):
        with pytest.raises(ValidationError, match=message):
            ContextCompactionReceipt.model_validate(receipt | update)

    with pytest.raises(ValidationError, match="wrong layer"):
        _view(active_memory_items=(_item("wrong", layer=ContextLayer.WORKING),))
    duplicate = _item("same")
    duplicate_working = duplicate.model_copy(
        update={
            "layer": ContextLayer.WORKING,
            "kind": ContextItemKind.WORK_PLAN,
        }
    )
    with pytest.raises(ValidationError, match="unique"):
        _view(active_memory_items=(duplicate,), working_items=(duplicate_working,))
    with pytest.raises(ValidationError, match="mandatory"):
        _view(
            protected_items=(
                _item(
                    "optional-protected",
                    layer=ContextLayer.PROTECTED,
                    kind=ContextItemKind.WRITING_TASK,
                ),
            )
        )
    with pytest.raises(ValidationError, match="planner-only"):
        _view(active_memory_items=(_item("planner", scope="planner_safe"),))
    with pytest.raises(ValidationError, match="event range"):
        _view(covered_event_range=(1, 2))
    with pytest.raises(ValidationError, match="event range"):
        _view(basis_event_position=2, covered_event_range=(0, 1))
    assert _view(basis_event_position=2, covered_event_range=(1, 2)).kept_boundary == 0

    valid_pressure = ContextPressure(
        rendered_input_tokens=9,
        available_input_tokens=10,
        soft_limit_tokens=8,
        hard_limit_tokens=10,
        soft_exceeded=True,
        hard_exceeded=False,
    )
    assert valid_pressure.soft_exceeded
    for update, message in (
        ({"soft_limit_tokens": 11}, "soft context"),
        ({"soft_exceeded": False}, "soft pressure"),
        ({"rendered_input_tokens": 11, "hard_exceeded": False}, "hard pressure"),
    ):
        with pytest.raises(ValidationError, match=message):
            ContextPressure.model_validate(valid_pressure.model_dump() | update)


def test_stage3_event_payloads_are_versioned() -> None:
    item = _item(
        "work-plan",
        layer=ContextLayer.WORKING,
        kind=ContextItemKind.WORK_PLAN,
        mandatory=True,
    )
    payload = WriterWorkPlanSettledPayload(
        work_plan_ref=_ref("1"),
        working_item=item,
    ).model_dump(mode="json")
    validate_stage3_event_payload(
        RunEventType.WRITER_WORK_PLAN_SETTLED.value,
        CONTEXT_EVENT_SCHEMA_VERSION,
        payload,
    )
    validate_stage3_event_payload("task.started", SchemaVersion("0.1.0"), {})
    with pytest.raises(ValueError, match="unknown"):
        validate_stage3_event_payload(
            RunEventType.WRITER_WORK_PLAN_SETTLED.value,
            SchemaVersion("2.0.0"),
            payload,
        )
    with pytest.raises(ValidationError):
        validate_stage3_event_payload(
            RunEventType.WRITER_WORK_PLAN_SETTLED.value,
            CONTEXT_EVENT_SCHEMA_VERSION,
            {},
        )


def test_projector_seed_delta_working_and_replay(tmp_path: Path) -> None:
    task, needs, units, commit = writer_context_inputs()
    package = (
        WriterContextAssembler()
        .assemble(
            task=task,
            units=units,
            needs=needs,
            basis_commit_id=commit,
            basis_snapshot_id=StableId("snapshot.context"),
            arm="A",
            writer_token_budget=20_000,
        )
        .package
    )
    assert package is not None
    projector = AgentContextProjector(_count)
    protected = (
        _item(
            "seed-task",
            layer=ContextLayer.PROTECTED,
            kind=ContextItemKind.WRITING_TASK,
            mandatory=True,
        ),
    )
    view = projector.seed_writer(
        run_id=RunId("run.context"),
        task_id=TaskId("task.context"),
        package=package,
        seed_package_ref=_ref("1"),
        profile_ref=_ref("2"),
        plan_ref=_ref("3"),
        protected_items=protected,
    )
    assert view.active_memory_items
    assert view.token_report["rendered"] > 0
    with pytest.raises(ContextProjectionError, match="protected"):
        projector.seed_writer(
            run_id=view.run_id,
            task_id=view.task_id,
            package=package,
            seed_package_ref=view.seed_package_ref,
            profile_ref=view.profile_ref,
            plan_ref=view.plan_ref,
            protected_items=(_item("not-protected"),),
        )

    added = _item("delta-added")
    delta = ContextDelta(
        delta_id=StableId("delta.applied"),
        request_ref=_ref("4"),
        resolution_ref=_ref("5"),
        parent_view_revision=view.revision,
        base_commit=view.base_commit,
        snapshot_id=view.snapshot_id,
        profile_ref=view.profile_ref,
        plan_ref=view.plan_ref,
        added_memory_items=(added,),
        resolved_need_ids=(StableId("need.closed"),),
        token_impact=added.token_count,
        status=ContextDeltaStatus.RESOLVED,
    )
    updated = projector.apply_delta(view, delta)
    assert updated.revision == view.revision + 1
    assert added in updated.active_memory_items
    for bad, message in (
        (delta.model_copy(update={"parent_view_revision": 9}), "stale"),
        (delta.model_copy(update={"snapshot_id": StableId("snapshot.other")}), "basis"),
        (
            delta.model_copy(
                update={
                    "parent_view_revision": view.revision,
                    "added_memory_items": (view.active_memory_items[0],),
                }
            ),
            "collides",
        ),
    ):
        with pytest.raises(ContextProjectionError, match=message):
            projector.apply_delta(view, bad)

    working = _item(
        "working",
        layer=ContextLayer.WORKING,
        kind=ContextItemKind.WORK_PLAN,
        mandatory=True,
    )
    with_working = projector.put_working_item(view, working)
    replacement = working.model_copy(update={"item_id": StableId("item.replacement")})
    replaced = projector.put_working_item(
        with_working,
        replacement,
        replace_kind=ContextItemKind.WORK_PLAN,
    )
    assert replaced.working_items == (replacement,)
    with pytest.raises(ContextProjectionError, match="wrong layer"):
        projector.put_working_item(view, _item("bad-working"))
    with pytest.raises(ContextProjectionError, match="already"):
        projector.put_working_item(with_working, working)

    event = _event(
        1,
        RunEventType.WRITER_WORK_PLAN_SETTLED,
        WriterWorkPlanSettledPayload(
            work_plan_ref=_ref("6"),
            working_item=working,
        ).model_dump(mode="json"),
    )
    incremental = projector.apply_event(view, event)
    assert projector.full_replay(view, (event,)) == incremental
    with pytest.raises(ContextProjectionError, match="another"):
        projector.apply_event(view, event.model_copy(update={"run_id": RunId("run.other")}))
    with pytest.raises(ContextProjectionError, match="next"):
        projector.apply_event(view, event.model_copy(update={"sequence_no": 2}))

    event_delta = delta.model_copy(update={"parent_view_revision": incremental.revision})
    delta_event = _event(
        2,
        RunEventType.CONTEXT_DELTA_APPLIED,
        ContextDeltaAppliedPayload(delta=event_delta).model_dump(mode="json"),
    )
    after_delta_event = projector.apply_event(incremental, delta_event)
    assert added in after_delta_event.active_memory_items

    store = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    compactor = ContextCompactor(projector, store, VERSION, _count)
    policy = ContextWindowPolicy(
        sequence_limit=10_000,
        reserved_output_tokens=100,
        safety_allowance_tokens=100,
        soft_limit_tokens=9_000,
        tokenizer="test",
        tokenizer_version="v1",
    )
    valid, receipt = compactor.compact(incremental, policy, hard=False)
    assert receipt is None
    assert valid.provider_validity_receipt is not None
    assert valid.provider_validity_receipt.provider_valid


def test_compactor_removes_only_safe_atomic_groups_and_hard_fails(tmp_path: Path) -> None:
    projector = AgentContextProjector(_count)
    store = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    compactor = ContextCompactor(projector, store, VERSION, _count)
    group = (
        _item(
            "tail-a",
            layer=ContextLayer.RECENT_SETTLED,
            kind=ContextItemKind.MODEL_BATCH,
            content="a" * 40,
            group="batch.one",
            event_range=(1, 1),
        ),
        _item(
            "tail-b",
            layer=ContextLayer.RECENT_SETTLED,
            kind=ContextItemKind.MODEL_BATCH,
            content="b" * 40,
            group="batch.one",
            event_range=(1, 1),
        ),
    )
    view = _view(recent_settled_tail=group)
    policy = ContextWindowPolicy(
        sequence_limit=240,
        reserved_output_tokens=10,
        safety_allowance_tokens=10,
        soft_limit_tokens=180,
        tokenizer="test",
        tokenizer_version="v1",
    )
    compacted, receipt = compactor.compact(view, policy, hard=True)
    assert receipt is not None
    assert set(receipt.removed_item_ids) == {item.item_id for item in group}
    assert not compacted.recent_settled_tail
    assert receipt.summary_artifact is not None
    assert receipt.detail_artifact is not None
    assert compactor.provider_receipt(compacted, policy).provider_valid
    applied = projector.apply_compaction(view, receipt)
    assert applied.context_hash == receipt.output_context_hash
    compacted_event = _event(
        2,
        RunEventType.CONTEXT_COMPACTED,
        ContextCompactedPayload(receipt=receipt).model_dump(mode="json"),
    )
    assert projector.apply_event(view, compacted_event).generation == 1
    with pytest.raises(ContextProjectionError, match="CAS"):
        projector.apply_compaction(view, receipt.model_copy(update={"parent_generation": 1}))
    with pytest.raises(ContextProjectionError, match="output hash"):
        projector.apply_compaction(
            view,
            receipt.model_copy(update={"output_context_hash": _ref("8").artifact_id}),
        )

    summarized, summary_receipt = ContextCompactor(
        projector,
        store,
        VERSION,
        _count,
        summary_function=lambda _text: "bounded runtime summary",
    ).compact(view, policy, hard=True)
    assert summary_receipt is not None
    assert summarized.compacted_prefix_items[0].content == "bounded runtime summary"

    blank_summary = ContextCompactor(
        projector,
        store,
        VERSION,
        _count,
        summary_function=lambda _text: "   ",
    )
    with pytest.raises(ContextLimitError, match="safe"):
        blank_summary.compact(view, policy, hard=True)

    mandatory = _view(
        protected_items=(
            _item(
                "huge-protected",
                layer=ContextLayer.PROTECTED,
                kind=ContextItemKind.WRITING_TASK,
                content="x" * 200,
                mandatory=True,
            ),
        )
    )
    with pytest.raises(ContextLimitError, match="safe"):
        compactor.compact(mandatory, policy, hard=True)
    with pytest.raises(ContextLimitError, match="safe"):
        ContextCompactor(
            projector,
            store,
            VERSION,
            _count,
            summary_function=lambda text: text,
        ).compact(mandatory, policy, hard=True)
    same, no_receipt = compactor.compact(mandatory, policy, hard=False)
    assert same == mandatory
    assert no_receipt is None

    for kwargs in (
        {"sequence_limit": 0},
        {"soft_limit_tokens": 221},
    ):
        with pytest.raises(ValueError, match="Context"):
            ContextWindowPolicy(
                sequence_limit=kwargs.get("sequence_limit", 240),
                reserved_output_tokens=10,
                safety_allowance_tokens=10,
                soft_limit_tokens=kwargs.get("soft_limit_tokens", 180),
                tokenizer="test",
                tokenizer_version="v1",
            )

    split_group = _view(
        active_memory_items=(
            _item("group-memory", group="split"),
            _item("group-separator"),
        ),
        working_items=(
            _item(
                "group-working",
                layer=ContextLayer.WORKING,
                kind=ContextItemKind.WORK_PLAN,
                group="split",
            ),
        ),
    )
    assert not compactor.provider_receipt(split_group, policy).atomic_groups_valid


def test_context_checkpoint_restores_view_and_replays_tail(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    events, checkpoints = repositories
    projector = AgentContextProjector(_count)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    runtime = AgentContextRuntime(
        projector,
        artifacts,
        events,
        checkpoints,
        VERSION,
    )
    seed = _view(basis_event_position=0)
    working = _item(
        "runtime-plan",
        layer=ContextLayer.WORKING,
        kind=ContextItemKind.WORK_PLAN,
        mandatory=True,
    )
    first = _event(
        1,
        RunEventType.WRITER_WORK_PLAN_SETTLED,
        WriterWorkPlanSettledPayload(
            work_plan_ref=_ref("8"),
            working_item=working,
        ).model_dump(mode="json"),
    )
    events.append(first)
    settled = projector.apply_event(seed, first)
    checkpoint = runtime.checkpoint(settled, StableId("checkpoint.context"))
    assert checkpoint.event_position == 1
    second = _event(
        2,
        RunEventType.TASK_COMPLETED,
        {"done": True},
    )
    events.append(second)
    restored = runtime.restore(seed.run_id, seed)
    assert restored.basis_event_position == 2
    assert restored.working_items == (working,)
    assert runtime.restore(RunId("run.missing"), seed) == seed

    with pytest.raises(ContextProjectionError, match="settle"):
        runtime.checkpoint(seed, StableId("checkpoint.unsettled"))
