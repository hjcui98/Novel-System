"""Atomic RootUpdateIntent and World materialization tests."""

from __future__ import annotations

from typing import Any

import pytest

from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.changes import ChangeOperationType, ObservedChangeSet
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory_write import (
    CanonicalWriteBasis,
    MemoryWriteCandidatePayload,
    RootUpdateIntent,
    RootUpdateKind,
)
from novel_agent.domain.stage2 import ContractRef
from novel_agent.services.memory_write_workflow import InMemoryArtifactRepository
from novel_agent.services.root_update_materializer import (
    RootUpdateMaterializationError,
    RootUpdateMaterializer,
)
from tests.factories import make_artifact
from tests.unit.test_mutation_normalizer import (
    _basis,
    _candidate,
    _operation,
    _payload,
)


def _policy() -> ContractRef:
    return ContractRef(
        contract_id=StableId("policy.root-update.test"),
        version=make_artifact().schema_version,
        content_hash=ArtifactId("sha256:" + "1" * 64),
    )


def _root(basis: CanonicalWriteBasis, kind: RootKind) -> ArtifactRef:
    manifest = basis.root_manifest
    assert manifest is not None
    return {
        RootKind.TEXT: manifest.text_root,
        RootKind.PLAN: manifest.plan_root,
        RootKind.WORLD: manifest.world_root,
        RootKind.REFERENCE: manifest.reference_root,
        RootKind.PROJECT_PROFILE: manifest.project_profile_root,
    }[kind]


def _intent(
    basis: CanonicalWriteBasis,
    kind: RootKind,
    *,
    update_kind: RootUpdateKind = RootUpdateKind.REPLACE,
    expected: ArtifactRef | None = None,
    update: ArtifactRef | None = None,
) -> RootUpdateIntent:
    base = expected or _root(basis, kind)
    return RootUpdateIntent(
        intent_id=StableId(f"intent.materializer.{kind.value}"),
        root_kind=kind,
        update_kind=update_kind,
        expected_base_root=base,
        update_artifact=update
        or (base if update_kind is RootUpdateKind.NOOP else make_artifact("e")),
        producer_receipt=make_artifact("f"),
        builder_policy_ref=_policy(),
    )


def _materialize(
    payload: MemoryWriteCandidatePayload,
    *,
    basis: CanonicalWriteBasis | None = None,
    writer: Any | None = None,
) -> Any:
    return RootUpdateMaterializer(
        payload_loader=lambda _: payload,
        artifact_writer=writer,
    ).materialize_atomic_bundle(
        candidate=_candidate(),
        basis=basis or _basis(),
    )


@pytest.mark.parametrize("kind", tuple(RootKind))
def test_each_root_kind_is_typed_and_atomically_replaced(kind: RootKind) -> None:
    basis = _basis()
    result = _materialize(
        _payload().model_copy(update={"root_update_intents": (_intent(basis, kind),)}),
        basis=basis,
    )

    assert result.changed_root_kinds == (kind.value,)
    assert len(result.bundle.produced_artifacts) == 1
    assert _root(
        CanonicalWriteBasis(
            project_id=basis.project_id,
            commit_id=basis.commit_id,
            root_manifest=result.bundle.proposed_roots,
        ),
        kind,
    ).artifact_id == ArtifactId("sha256:" + "e" * 64)


def test_noop_and_identical_replace_do_not_report_a_changed_root() -> None:
    basis = _basis()
    base = _root(basis, RootKind.TEXT)
    payload = _payload().model_copy(
        update={
            "root_update_intents": (
                _intent(basis, RootKind.TEXT, update_kind=RootUpdateKind.NOOP),
                _intent(basis, RootKind.TEXT, update=base),
            )
        }
    )

    result = _materialize(payload, basis=basis)

    assert result.changed_root_kinds == ()
    assert result.bundle.produced_artifacts == ()


def test_candidate_and_change_set_must_share_the_verified_basis() -> None:
    basis = _basis()
    other = type(basis.commit_id)("sha256:" + "9" * 64)
    wrong_candidate = _candidate().model_copy(update={"base_commit": other})
    service = RootUpdateMaterializer(payload_loader=lambda _: _payload())
    with pytest.raises(RootUpdateMaterializationError, match="basis commits differ"):
        service.materialize_atomic_bundle(candidate=wrong_candidate, basis=basis)

    changes = _payload().observed_changes.model_copy(update={"base_commit": other})
    with pytest.raises(RootUpdateMaterializationError, match="another base commit"):
        _materialize(_payload().model_copy(update={"observed_changes": changes}), basis=basis)


def test_intent_expected_root_must_match_the_current_manifest_exactly() -> None:
    basis = _basis()
    wrong = _root(basis, RootKind.TEXT).model_copy(update={"byte_length": 999})
    payload = _payload().model_copy(
        update={"root_update_intents": (_intent(basis, RootKind.TEXT, expected=wrong),)}
    )

    with pytest.raises(RootUpdateMaterializationError, match="canonical base root"):
        _materialize(payload, basis=basis)


def test_world_operations_require_typed_world_and_overlay_validity() -> None:
    operation = _operation()
    payload = _payload(operation)
    with pytest.raises(RootUpdateMaterializationError, match="verified canonical WorldRoot"):
        _materialize(payload, basis=_basis(include_world=False))

    missing = _operation(
        operation=ChangeOperationType.REPLACE,
        target="obligation.synthetic.missing",
    )
    with pytest.raises(RootUpdateMaterializationError):
        _materialize(_payload(missing))


def test_world_operation_materializes_a_new_world_root() -> None:
    basis = _basis()
    assert basis.canonical_world is not None
    record = basis.canonical_world.obligations[0].model_dump(mode="json")
    record["status"] = "resolved"
    result = _materialize(
        _payload(_operation(record=record)),
        writer=InMemoryArtifactRepository(),
    )
    assert result.world_mutation_noop is False
    assert RootKind.WORLD.value in result.changed_root_kinds
    assert result.bundle.proposed_roots.parent_commit_ids == (_basis().commit_id,)


def test_missing_manifest_and_identical_stored_world_are_handled() -> None:
    basis = _basis()
    invalid_basis = basis.model_copy(update={"root_manifest": None})
    with pytest.raises(RootUpdateMaterializationError, match="no RootManifest"):
        _materialize(_payload(), basis=invalid_basis)

    manifest = basis.root_manifest
    assert manifest is not None
    world_ref = manifest.world_root

    class SameWorldWriter:
        def put(self, *_: object) -> ArtifactRef:
            return world_ref

    result = _materialize(
        _payload(_operation()),
        basis=basis,
        writer=SameWorldWriter(),
    )
    assert result.changed_root_kinds == ()


def test_payload_loader_and_artifact_writer_fail_closed() -> None:
    service = RootUpdateMaterializer()
    with pytest.raises(RootUpdateMaterializationError, match="not configured"):
        service.materialize_atomic_bundle(candidate=_candidate(), basis=_basis())

    def broken(_: ArtifactRef) -> MemoryWriteCandidatePayload:
        raise OSError("corrupt")

    with pytest.raises(RootUpdateMaterializationError, match="cannot be loaded"):
        RootUpdateMaterializer(payload_loader=broken).materialize_atomic_bundle(
            candidate=_candidate(),
            basis=_basis(),
        )

    class BadWriter:
        def put(self, *_: object) -> str:
            return "not-an-artifact"

    with pytest.raises(TypeError, match="non-ArtifactRef"):
        _materialize(_payload(), writer=BadWriter())


def test_duplicate_changed_root_kind_is_deduplicated_in_result() -> None:
    basis = _basis()
    first = _intent(basis, RootKind.TEXT, update=make_artifact("d"))
    second = _intent(
        basis,
        RootKind.TEXT,
        expected=make_artifact("d"),
        update=make_artifact("e"),
    )
    payload = MemoryWriteCandidatePayload(
        observed_changes=ObservedChangeSet(
            change_set_id=StableId("changes.root-update.sequence"),
            base_commit=basis.commit_id,
            source_artifact=make_artifact("9"),
        ),
        root_update_intents=(first, second),
        commit_profile=_payload().commit_profile,
    )

    result = _materialize(payload, basis=basis)

    assert result.changed_root_kinds == (RootKind.TEXT.value,)
    assert len(result.bundle.produced_artifacts) == 2
