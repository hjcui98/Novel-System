"""Deterministic Stage 2W mutation-normalizer characterization tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from novel_agent.domain.artifacts import RootKind
from novel_agent.domain.changes import (
    ChangeOperation,
    ChangeOperationType,
    ObservedChangeSet,
    WorldRecordKind,
)
from novel_agent.domain.ids import ArtifactId, ProjectId, StableId
from novel_agent.domain.memory_write import (
    CandidateProducerKind,
    CandidateRevision,
    CanonicalWriteBasis,
    MemoryWriteCandidatePayload,
    MemoryWriteCommitProfile,
    NormalizationStatus,
    RepairAction,
    RepairDirective,
    RepairScope,
)
from novel_agent.services.memory_write_workflow import InMemoryArtifactRepository
from novel_agent.services.mutation_normalizer import (
    MutationNormalizer,
    NormalizationAmbiguity,
    _same_identity,
)
from tests.factories import make_artifact, make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

NOW = datetime(2026, 7, 23, tzinfo=UTC)
PROJECT = ProjectId("project.synthetic")


def _operation(
    *,
    operation_id: str = "operation.normalizer",
    operation: ChangeOperationType = ChangeOperationType.REPLACE,
    target: str = "obligation.synthetic.north-tower",
    record_type: str = "obligation",
    record: dict[str, Any] | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> ChangeOperation:
    world = make_synthetic_bundle().world_roots[0]
    if record is None:
        record = world.obligations[0].model_dump(mode="json")
    payload: dict[str, Any] = {"record_type": record_type, "record": record}
    payload.update(extra_payload or {})
    return ChangeOperation(
        operation_id=StableId(operation_id),
        root_kind=RootKind.WORLD,
        operation=operation,
        target_id=StableId(target),
        payload=payload,
    )


def _payload(*operations: ChangeOperation) -> MemoryWriteCandidatePayload:
    world = make_synthetic_bundle().world_roots[0]
    return MemoryWriteCandidatePayload(
        observed_changes=ObservedChangeSet(
            change_set_id=StableId("changes.normalizer"),
            base_commit=world.source_commit,
            source_artifact=make_artifact("9"),
            operations=operations,
        ),
        root_update_intents=(),
        commit_profile=MemoryWriteCommitProfile.CHANGED_ROOTS_ONLY,
    )


def _candidate() -> CandidateRevision:
    world = make_synthetic_bundle().world_roots[0]
    return CandidateRevision(
        candidate_id=StableId("candidate.normalizer.1"),
        revision_no=1,
        base_commit=world.source_commit,
        basis_hash=ArtifactId("sha256:" + "a" * 64),
        candidate_artifact=make_artifact("b"),
        producer_kind=CandidateProducerKind.CURATOR_PROPOSE,
        content_hash=ArtifactId("sha256:" + "c" * 64),
        created_at=NOW,
    )


def _basis(*, include_world: bool = True) -> CanonicalWriteBasis:
    world = make_synthetic_bundle().world_roots[0]
    return CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=world.source_commit,
        root_manifest=make_manifest(PROJECT),
        canonical_world=world if include_world else None,
    )


def _normalize(
    payload: MemoryWriteCandidatePayload,
    *,
    directive: RepairDirective | None = None,
    writer: Any | None = None,
) -> Any:
    return MutationNormalizer(
        payload_loader=lambda _: payload,
        artifact_writer=writer or InMemoryArtifactRepository(),
        clock=lambda: NOW,
    ).normalize(_candidate(), _basis(), directive)


def test_payload_loader_is_required_and_fail_closed() -> None:
    with pytest.raises(NormalizationAmbiguity, match="not configured"):
        MutationNormalizer().normalize(_candidate(), _basis())

    def broken(_: object) -> MemoryWriteCandidatePayload:
        raise OSError("corrupt")

    with pytest.raises(NormalizationAmbiguity, match="cannot be loaded"):
        MutationNormalizer(payload_loader=broken).normalize(_candidate(), _basis())


@pytest.mark.parametrize(
    "payload",
    (
        "not-an-object",
        {"record_type": 7, "record": {}},
        {"record_type": "unknown", "record": {}},
        {"record_type": "state", "record": "not-an-object"},
    ),
)
def test_non_structured_operations_are_unchanged(payload: Any) -> None:
    operation = _operation().model_copy(update={"payload": payload})
    result = _normalize(_payload(operation))
    assert result.status is NormalizationStatus.UNCHANGED
    assert result.candidate == _candidate()


def test_create_exact_duplicate_becomes_noop() -> None:
    result = _normalize(_payload(_operation(operation=ChangeOperationType.CREATE)))
    assert result.status is NormalizationStatus.TRANSFORMED
    assert result.transforms[0].rule_id == StableId("normalize.exact-duplicate-v1")


def test_create_same_identity_becomes_replace() -> None:
    world = make_synthetic_bundle().world_roots[0]
    record = world.obligations[0].model_dump(mode="json")
    record["status"] = "resolved"
    result = _normalize(_payload(_operation(operation=ChangeOperationType.CREATE, record=record)))
    assert result.status is NormalizationStatus.TRANSFORMED
    assert result.transforms[0].rule_id == StableId("normalize.create-to-replace-v1")


def test_create_conflicting_identity_is_ambiguous() -> None:
    world = make_synthetic_bundle().world_roots[0]
    record = world.obligations[0].model_dump(mode="json")
    record["kind"] = "promise"
    result = _normalize(_payload(_operation(operation=ChangeOperationType.CREATE, record=record)))
    assert result.status is NormalizationStatus.AMBIGUOUS
    assert "NORMALIZATION_AMBIGUOUS" in result.reason_codes


def test_missing_replace_only_becomes_create_with_self_consistent_id() -> None:
    world = make_synthetic_bundle().world_roots[0]
    record = world.obligations[0].model_dump(mode="json")
    record["obligation_id"] = "obligation.synthetic.new"
    transformed = _normalize(
        _payload(
            _operation(
                operation=ChangeOperationType.REPLACE,
                target="obligation.synthetic.new",
                record=record,
            )
        )
    )
    ambiguous = _normalize(
        _payload(
            _operation(
                operation=ChangeOperationType.REPLACE,
                target="obligation.synthetic.other",
                record=record,
            )
        )
    )
    assert transformed.status is NormalizationStatus.TRANSFORMED
    assert transformed.transforms[0].rule_id == StableId("normalize.replace-to-create-v1")
    assert ambiguous.status is NormalizationStatus.AMBIGUOUS


def test_replacement_cannot_change_non_state_identity() -> None:
    world = make_synthetic_bundle().world_roots[0]
    record = world.obligations[0].model_dump(mode="json")
    record["kind"] = "promise"
    result = _normalize(_payload(_operation(record=record)))
    assert result.status is NormalizationStatus.AMBIGUOUS


def _state_record(**updates: Any) -> dict[str, Any]:
    record = make_synthetic_bundle().world_roots[0].states[0].model_dump(mode="json")
    record.update(updates)
    return record


def test_state_identity_change_requires_successor_proof() -> None:
    record = _state_record(predicate="location")
    ambiguous = _normalize(
        _payload(
            _operation(
                target="state.synthetic.injury",
                record_type="state",
                record=record,
            )
        )
    )
    assert ambiguous.status is NormalizationStatus.AMBIGUOUS


@pytest.mark.parametrize(
    "valid_time",
    (None, {}, {"start_ordinal": "21"}, {"start_ordinal": 20}),
)
def test_successor_requires_advancing_valid_time(valid_time: Any) -> None:
    record = _state_record(predicate="location", valid_time=valid_time)
    result = _normalize(
        _payload(
            _operation(
                target="state.synthetic.injury",
                record_type="state",
                record=record,
                extra_payload={"successor_proof": True},
            )
        )
    )
    assert result.status is NormalizationStatus.AMBIGUOUS


def test_successor_conversion_ends_old_state_and_creates_stable_child() -> None:
    record = _state_record(
        predicate="location",
        valid_time={
            "worldline": "main",
            "start_ordinal": 21,
            "end_ordinal": None,
            "label": None,
        },
    )
    operation = _operation(
        target="state.synthetic.injury",
        record_type="state",
        record=record,
        extra_payload={"successor_proof": {}},
    )
    first = _normalize(_payload(operation))
    second = _normalize(_payload(operation))
    assert first.status is NormalizationStatus.TRANSFORMED
    assert len(first.transforms) == 1
    assert first.candidate.content_hash == second.candidate.content_hash
    assert first.candidate.parent_candidate_id == _candidate().candidate_id
    assert first.candidate.revision_no == 2


def test_evidence_order_is_canonicalized_without_changing_spans() -> None:
    world = make_synthetic_bundle().world_roots[0]
    record = world.obligations[0].model_dump(mode="json")
    evidence = world.obligations[0].evidence_refs[0]
    evidence_z = evidence.model_copy(update={"evidence_id": StableId("evidence.z")})
    evidence_a = evidence.model_copy(update={"evidence_id": StableId("evidence.a")})
    record["evidence_refs"] = [
        evidence_z.model_dump(mode="json"),
        evidence_a.model_dump(mode="json"),
    ]
    operation = _operation(record=record).model_copy(
        update={"evidence_refs": (evidence_z, evidence_a)}
    )
    writer = InMemoryArtifactRepository()
    result = _normalize(_payload(operation), writer=writer)
    assert result.status is NormalizationStatus.TRANSFORMED
    assert result.transforms[0].rule_id == StableId("normalize.evidence-order-v1")
    normalized = writer.read_model(
        result.candidate.candidate_artifact,
        MemoryWriteCandidatePayload,
    )
    normalized_operation = normalized.observed_changes.operations[0]
    expected = ["evidence.a", "evidence.z"]
    assert [item.evidence_id.root for item in normalized_operation.evidence_refs] == expected
    assert [
        item["evidence_id"]
        for item in normalized_operation.payload["record"]["evidence_refs"]
    ] == expected


def test_duplicate_operations_merge_but_conflicting_writes_remain() -> None:
    duplicate = _operation(operation_id="operation.duplicate")
    merged = _normalize(_payload(duplicate, duplicate))
    conflicting = duplicate.model_copy(
        update={
            "operation_id": StableId("operation.conflict"),
            "operation": ChangeOperationType.RETIRE,
        }
    )
    unchanged = _normalize(_payload(duplicate, conflicting))
    assert any(
        item.rule_id == StableId("normalize.duplicate-merge-v1") for item in merged.transforms
    )
    assert unchanged.status is NormalizationStatus.UNCHANGED


def test_directive_limits_normalization_to_selected_operations() -> None:
    selected = _operation(
        operation_id="operation.selected",
        operation=ChangeOperationType.CREATE,
    )
    skipped = _operation(
        operation_id="operation.skipped",
        operation=ChangeOperationType.CREATE,
    )
    directive = RepairDirective(
        directive_id=StableId("directive.normalizer"),
        action=RepairAction.DETERMINISTIC_REPAIR,
        operation_ids=(selected.operation_id,),
        allowed_scope=RepairScope(operation_ids=(selected.operation_id,)),
    )
    result = _normalize(_payload(selected, skipped), directive=directive)
    assert result.status is NormalizationStatus.TRANSFORMED
    assert result.candidate.applied_directive_ids == (directive.directive_id,)


def test_artifact_writer_must_return_an_artifact_ref() -> None:
    class BadWriter:
        def put(self, *_: object) -> str:
            return "bad"

    with pytest.raises(TypeError, match="non-ArtifactRef"):
        _normalize(
            _payload(_operation(operation=ChangeOperationType.CREATE)),
            writer=BadWriter(),
        )


def test_normalizer_handles_basis_without_world_as_empty_registry() -> None:
    payload = _payload(
        _operation(
            operation=ChangeOperationType.CREATE,
            target="obligation.synthetic.new",
            record={
                **make_synthetic_bundle().world_roots[0].obligations[0].model_dump(mode="json"),
                "obligation_id": "obligation.synthetic.new",
            },
        )
    )
    result = MutationNormalizer(payload_loader=lambda _: payload).normalize(
        _candidate(), _basis(include_world=False)
    )
    assert result.status is NormalizationStatus.UNCHANGED


def test_equal_payload_hash_guard_returns_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_agent.services.mutation_normalizer as module

    monkeypatch.setattr(
        module,
        "_payload_hash",
        lambda payload: ArtifactId("sha256:" + "f" * 64),
    )
    world = make_synthetic_bundle().world_roots[0]
    record = world.obligations[0].model_dump(mode="json")
    record["status"] = "resolved"
    result = _normalize(_payload(_operation(operation=ChangeOperationType.CREATE, record=record)))
    assert result.status is NormalizationStatus.UNCHANGED
    assert result.transforms


def test_transform_without_artifact_writer_builds_content_addressed_refs() -> None:
    world = make_synthetic_bundle().world_roots[0]
    record = world.obligations[0].model_dump(mode="json")
    record["status"] = "resolved"
    payload = _payload(_operation(operation=ChangeOperationType.CREATE, record=record))
    result = MutationNormalizer(
        payload_loader=lambda _: payload,
        clock=lambda: NOW,
    ).normalize(_candidate(), _basis())
    assert result.status is NormalizationStatus.TRANSFORMED
    assert result.candidate.candidate_artifact.artifact_id.root.startswith("sha256:")


@pytest.mark.parametrize(
    ("kind", "current", "new", "expected"),
    (
        (
            WorldRecordKind.STATE,
            {"subject_id": "entity.a", "predicate": "state"},
            {"subject_id": "entity.a", "predicate": "state"},
            True,
        ),
        (
            WorldRecordKind.RELATION,
            {"subject_id": "a", "predicate": "knows", "object_id": "b"},
            {"subject_id": "a", "predicate": "knows", "object_id": "b"},
            True,
        ),
        (WorldRecordKind.ENTITY, {"entity_type": "person"}, {"entity_type": "place"}, False),
        (WorldRecordKind.EVENT, {"event_type": "arrival"}, {"event_type": "arrival"}, True),
        (
            WorldRecordKind.OBLIGATION,
            {"kind": "promise"},
            {"kind": "promise"},
            True,
        ),
    ),
)
def test_same_identity_covers_each_world_record_kind(
    kind: WorldRecordKind,
    current: dict[str, Any],
    new: dict[str, Any],
    expected: bool,
) -> None:
    assert _same_identity(current, new, kind) is expected


def test_same_identity_fails_closed_for_unknown_runtime_kind() -> None:
    assert not _same_identity({}, {}, cast(WorldRecordKind, "unknown"))


@pytest.mark.parametrize("evidence_refs", (["invalid"], "not-a-list"))
def test_noncanonical_evidence_shapes_are_left_unchanged(evidence_refs: Any) -> None:
    world = make_synthetic_bundle().world_roots[0]
    record = world.obligations[0].model_dump(mode="json")
    record["evidence_refs"] = evidence_refs
    result = _normalize(_payload(_operation(record=record)))
    assert result.status is NormalizationStatus.UNCHANGED
