"""Security and contract tests for Stage 2W information-boundary receipts."""

from __future__ import annotations

import pytest

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory_write import (
    BoundaryPropagationReceipt,
    CanonicalWriteBasis,
    InformationBoundary,
    MemoryWriteCommitProfile,
    NarrativePosition,
    SourceProvenance,
    SourceVisibilityReceipt,
    TrustedWorldCandidateInput,
)
from novel_agent.domain.stage2 import AccessScope, ContractRef
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.information_boundary import (
    InformationBoundaryPort,
    InformationBoundaryViolation,
    PermissiveInformationBoundaryPort,
    _position_key,
    _position_within,
    _scope_width,
    receipt_schema_version,
)
from tests.contract.test_memory_write_workflow_contract import PROJECT, _manifest, _request

VERSION = SchemaVersion("0.1.0")
TRUSTED_POLICY = ArtifactId("sha256:" + "c" * 64)
UNTRUSTED_POLICY = ArtifactId("sha256:" + "d" * 64)
BOUNDARY_ID = StableId("boundary.unit")


def _artifact(digit: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + digit * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=VERSION,
    )


def _receipt(
    *,
    policy: ArtifactId = TRUSTED_POLICY,
    receipt_hash: ArtifactId | None = None,
) -> BoundaryPropagationReceipt:
    receipt = BoundaryPropagationReceipt(
        receipt_id=StableId("receipt.boundary.unit"),
        boundary_id=StableId("boundary.unit"),
        base_commit=CommitId("sha256:" + "1" * 64),
        input_source_artifact_refs=(_artifact("2"),),
        source_visibility_receipt_refs=(_artifact("3"),),
        output_artifact_hash=_artifact("4").artifact_id,
        builder_policy_hash=policy,
        effective_visible_through=None,
        effective_access_scope=AccessScope.WRITER_SAFE,
        receipt_hash=ArtifactId("sha256:" + "0" * 64),
    )
    payload = receipt.model_dump(mode="json")
    payload["receipt_hash"] = None
    return receipt.model_copy(
        update={"receipt_hash": receipt_hash or sha256_id(canonical_json_bytes(payload))}
    )


def _parented_receipt(
    *,
    name: str,
    output: ArtifactRef,
    parent_ref: ArtifactRef,
    boundary_id: StableId = BOUNDARY_ID,
) -> BoundaryPropagationReceipt:
    receipt = BoundaryPropagationReceipt(
        receipt_id=StableId(f"receipt.{name}"),
        boundary_id=boundary_id,
        base_commit=CommitId("sha256:" + "1" * 64),
        input_derivation_receipt_refs=(parent_ref,),
        output_artifact_hash=output.artifact_id,
        builder_policy_hash=TRUSTED_POLICY,
        effective_visible_through=None,
        effective_access_scope=AccessScope.WRITER_SAFE,
        receipt_hash=ArtifactId("sha256:" + "0" * 64),
    )
    payload = receipt.model_dump(mode="json")
    payload["receipt_hash"] = None
    return receipt.model_copy(update={"receipt_hash": sha256_id(canonical_json_bytes(payload))})


def _boundary() -> InformationBoundary:
    return InformationBoundary(
        boundary_id=StableId("boundary.unit"),
        base_commit=CommitId("sha256:" + "1" * 64),
        evaluator_sources_forbidden=True,
        policy_ref=ContractRef(
            contract_id=StableId("policy.boundary.unit"),
            version=VERSION,
            content_hash=TRUSTED_POLICY,
        ),
    )


def _visibility(
    source: ArtifactRef,
    *,
    boundary_id: StableId = BOUNDARY_ID,
    scope: AccessScope = AccessScope.WRITER_SAFE,
    position: NarrativePosition | None = None,
    provenance: SourceProvenance = SourceProvenance.REVEALED_TEXT,
) -> SourceVisibilityReceipt:
    receipt = SourceVisibilityReceipt(
        receipt_id=StableId(f"visibility.{source.artifact_id.root[7:15]}"),
        source_artifact=source,
        boundary_id=boundary_id,
        visible_through=position,
        access_scope=scope,
        provenance=provenance,
        issuer=StableId("issuer.visibility.unit"),
        receipt_hash=ArtifactId("sha256:" + "0" * 64),
    )
    payload = receipt.model_dump(mode="json")
    payload["receipt_hash"] = None
    return receipt.model_copy(update={"receipt_hash": sha256_id(canonical_json_bytes(payload))})


def _visibility_ref(receipt: SourceVisibilityReceipt) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=receipt.receipt_hash,
        media_type="application/vnd.novel-agent.source-visibility-receipt+json",
        byte_length=1,
        schema_version=VERSION,
    )


def _rehash(receipt: BoundaryPropagationReceipt) -> BoundaryPropagationReceipt:
    payload = receipt.model_dump(mode="json")
    payload["receipt_hash"] = None
    return receipt.model_copy(update={"receipt_hash": sha256_id(canonical_json_bytes(payload))})


@pytest.mark.parametrize("digit", ("a", "b"))
def test_rejects_magic_receipt_hashes_that_do_not_bind_content(digit: str) -> None:
    port = InformationBoundaryPort()

    with pytest.raises(InformationBoundaryViolation, match="hash is invalid"):
        port.register_derivation(_receipt(receipt_hash=ArtifactId("sha256:" + digit * 64)))


def test_receipt_cannot_add_its_builder_policy_to_the_trust_set() -> None:
    port = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,))

    with pytest.raises(InformationBoundaryViolation, match="builder policy is not trusted"):
        port.register_derivation(_receipt(policy=UNTRUSTED_POLICY))


def test_derivation_dag_cycle_is_rejected() -> None:
    port = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,))
    ref_a = _artifact("5").model_copy(
        update={"media_type": "application/vnd.novel-agent.boundary-propagation-receipt+json"}
    )
    ref_b = _artifact("6").model_copy(
        update={"media_type": "application/vnd.novel-agent.boundary-propagation-receipt+json"}
    )
    output_a = _artifact("7")
    output_b = _artifact("8")
    receipt_a = _parented_receipt(name="a", output=output_a, parent_ref=ref_b)
    receipt_b = _parented_receipt(name="b", output=output_b, parent_ref=ref_a)
    port.register_derivation(receipt_a, receipt_artifact=ref_a, output_artifact=output_a)
    port.register_derivation(receipt_b, receipt_artifact=ref_b, output_artifact=output_b)

    with pytest.raises(InformationBoundaryViolation, match="contains a cycle"):
        port.verify_derivation_chain(
            artifact=output_a,
            producer_receipt=ref_a,
            boundary=_boundary(),
            configuration_fingerprint=TRUSTED_POLICY,
        )


def test_derivation_dag_cannot_cross_information_boundaries() -> None:
    port = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,))
    parent_ref = _artifact("5").model_copy(
        update={"media_type": "application/vnd.novel-agent.boundary-propagation-receipt+json"}
    )
    child_ref = _artifact("6").model_copy(
        update={"media_type": "application/vnd.novel-agent.boundary-propagation-receipt+json"}
    )
    parent_output = _artifact("7")
    child_output = _artifact("8")
    parent = _parented_receipt(
        name="foreign",
        output=parent_output,
        parent_ref=child_ref,
        boundary_id=StableId("boundary.foreign"),
    )
    child = _parented_receipt(name="child", output=child_output, parent_ref=parent_ref)
    port.register_derivation(parent, receipt_artifact=parent_ref, output_artifact=parent_output)
    port.register_derivation(child, receipt_artifact=child_ref, output_artifact=child_output)

    with pytest.raises(InformationBoundaryViolation, match="another boundary"):
        port.verify_derivation_chain(
            artifact=child_output,
            producer_receipt=child_ref,
            boundary=_boundary(),
            configuration_fingerprint=TRUSTED_POLICY,
        )


def test_registration_identity_and_output_bindings_are_immutable() -> None:
    port = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,))
    visibility = _visibility(_artifact("2"))
    port.register_visibility(visibility)
    with pytest.raises(InformationBoundaryViolation, match="multiple visibility"):
        port.register_visibility(
            _visibility(_artifact("2"), provenance=SourceProvenance.AUTHOR_INPUT)
        )

    receipt = _receipt()
    with pytest.raises(InformationBoundaryViolation, match="output reference"):
        port.register_derivation(receipt, output_artifact=_artifact("5"))

    ref = _artifact("6")
    port.register_derivation(receipt, receipt_artifact=ref)
    collision = _rehash(
        receipt.model_copy(update={"receipt_id": StableId("receipt.boundary.collision")})
    )
    with pytest.raises(InformationBoundaryViolation, match="identity collision"):
        port.register_derivation(collision, receipt_artifact=ref)

    generated = InformationBoundaryPort(
        trusted_policy_hashes=(TRUSTED_POLICY,)
    ).register_derivation(receipt)
    assert generated.artifact_id == receipt.receipt_hash
    assert receipt_schema_version(receipt) == VERSION


def test_request_identity_and_source_shapes_fail_closed() -> None:
    basis = CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=CommitId("sha256:" + "1" * 64),
        root_manifest=_manifest(),
    )
    request = _request()
    wrong_boundary = request.model_copy(
        update={
            "information_boundary": request.information_boundary.model_copy(
                update={"base_commit": CommitId("sha256:" + "9" * 64)}
            )
        }
    )
    with pytest.raises(InformationBoundaryViolation, match="boundary base"):
        InformationBoundaryPort(
            trusted_policy_hashes=(TRUSTED_POLICY,)
        ).verify_request_and_derivation_graph(wrong_boundary, basis)

    with pytest.raises(InformationBoundaryViolation, match="canonical basis"):
        InformationBoundaryPort(
            trusted_policy_hashes=(TRUSTED_POLICY,)
        ).verify_request_and_derivation_graph(
            request,
            basis.model_copy(update={"project_id": type(PROJECT)("project.other")}),
        )

    source = _artifact("2")
    visibility = _visibility(
        source,
        boundary_id=request.information_boundary.boundary_id,
    )
    cases = (
        (
            request.model_copy(update={"source_artifacts": (source,)}),
            "receipt count",
        ),
        (
            request.model_copy(
                update={
                    "source_artifacts": (source,),
                    "source_visibility_receipts": (visibility,),
                    "source_provenance": (
                        SourceProvenance.REVEALED_TEXT,
                        SourceProvenance.AUTHOR_INPUT,
                    ),
                }
            ),
            "provenance count",
        ),
        (
            request.model_copy(
                update={
                    "source_artifacts": (_artifact("3"),),
                    "source_visibility_receipts": (visibility,),
                }
            ),
            "content does not match",
        ),
        (
            request.model_copy(
                update={
                    "source_artifacts": (source,),
                    "source_visibility_receipts": (
                        _visibility(source, boundary_id=StableId("boundary.other")),
                    ),
                }
            ),
            "another boundary",
        ),
        (
            request.model_copy(
                update={
                    "source_artifacts": (source,),
                    "source_visibility_receipts": (visibility,),
                    "source_provenance": (SourceProvenance.AUTHOR_INPUT,),
                }
            ),
            "provenance differs",
        ),
    )
    for malformed, message in cases:
        port = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,))
        with pytest.raises(InformationBoundaryViolation, match=message):
            port.verify_request_and_derivation_graph(malformed, basis)


def test_visibility_hash_position_scope_and_boundary_checks() -> None:
    source = _artifact("2")
    port = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,))
    invalid = _visibility(source).model_copy(
        update={"receipt_hash": ArtifactId("sha256:" + "9" * 64)}
    )
    with pytest.raises(InformationBoundaryViolation, match="visibility receipt hash"):
        port.register_visibility(invalid)

    boundary = _boundary().model_copy(
        update={"maximum_visible_position": NarrativePosition(chapter_index=3)}
    )
    for receipt, message in (
        (
            _visibility(source, boundary_id=StableId("boundary.other")),
            "wrong boundary",
        ),
        (
            _visibility(source, scope=AccessScope.EVALUATOR),
            "evaluator-only",
        ),
        (
            _visibility(source, position=NarrativePosition(chapter_index=4)),
            "workflow cutoff",
        ),
    ):
        with pytest.raises(InformationBoundaryViolation, match=message):
            port._verify_visibility(boundary, receipt, AccessScope.WRITER_SAFE)

    receipt = _visibility(source, position=NarrativePosition(chapter_index=2))
    with pytest.raises(InformationBoundaryViolation, match="source visibility"):
        port._verify_visibility(
            boundary,
            receipt,
            AccessScope.WRITER_SAFE,
            NarrativePosition(chapter_index=3),
        )
    with pytest.raises(InformationBoundaryViolation, match="widens access"):
        port._verify_visibility(
            boundary.model_copy(update={"evaluator_sources_forbidden": False}),
            _visibility(source, scope=AccessScope.WRITER_SAFE),
            AccessScope.AUTHOR_PLANNING,
            derived_output=True,
        )


def test_derivation_bindings_visibility_and_parent_monotonicity() -> None:
    port = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,))
    output = _artifact("4")
    ref = _artifact("5")
    receipt = _receipt()
    port.register_derivation(receipt, receipt_artifact=ref, output_artifact=output)

    mutations = (
        ({"base_commit": CommitId("sha256:" + "9" * 64)}, "base commit"),
        ({"output_artifact_hash": _artifact("9").artifact_id}, "output hash"),
        ({"builder_policy_hash": UNTRUSTED_POLICY}, "intent policy"),
        (
            {"effective_visible_through": NarrativePosition(chapter_index=5)},
            "visible narrative",
        ),
    )
    for updates, message in mutations:
        altered = _rehash(receipt.model_copy(update=updates))
        other = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY, UNTRUSTED_POLICY))
        other.register_derivation(
            altered,
            receipt_artifact=ref,
            output_artifact=None,
        )
        with pytest.raises(InformationBoundaryViolation, match=message):
            other.verify_derivation_chain(
                artifact=output,
                producer_receipt=ref,
                boundary=_boundary().model_copy(
                    update={"maximum_visible_position": NarrativePosition(chapter_index=3)}
                ),
                configuration_fingerprint=TRUSTED_POLICY,
                expected_builder_policy=TRUSTED_POLICY,
            )

    visibility = _visibility(_artifact("2"))
    port = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,))
    port.register_visibility(visibility)
    uncovered = _rehash(
        receipt.model_copy(update={"source_visibility_receipt_refs": (_artifact("8"),)})
    )
    uncovered_ref = _artifact("7")
    port.register_derivation(uncovered, receipt_artifact=uncovered_ref)
    with pytest.raises(InformationBoundaryViolation, match="unknown source visibility"):
        port.verify_derivation_chain(
            artifact=output,
            producer_receipt=uncovered_ref,
            boundary=_boundary(),
            configuration_fingerprint=TRUSTED_POLICY,
        )


def test_receipt_resolution_reader_and_missing_metadata_fail_closed() -> None:
    visibility = _visibility(_artifact("2"))
    receipt = _rehash(
        _receipt().model_copy(
            update={"source_visibility_receipt_refs": (_visibility_ref(visibility),)}
        )
    )

    class Reader:
        def __init__(self, raw: bytes) -> None:
            self.raw = raw

        def read_verified(self, _: ArtifactRef) -> bytes:
            return self.raw

    ref = _artifact("5")
    reader_port = InformationBoundaryPort(
        Reader(canonical_json_bytes(receipt.model_dump(mode="json"))),
        trusted_policy_hashes=(TRUSTED_POLICY,),
    )
    reader_port.register_visibility(visibility)
    assert reader_port.read_derivation_receipt(ref) == receipt
    assert (
        reader_port.verify_candidate_derivation(
            artifact=_artifact("4"),
            producer_receipt=ref,
            boundary=_boundary(),
            configuration_fingerprint=TRUSTED_POLICY,
        )
        == receipt
    )

    with pytest.raises(InformationBoundaryViolation, match="cannot be read"):
        InformationBoundaryPort(
            Reader(b"not-json"),
            trusted_policy_hashes=(TRUSTED_POLICY,),
        ).read_derivation_receipt(ref)
    with pytest.raises(InformationBoundaryViolation, match="missing propagation"):
        InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,)).read_derivation_receipt(
            ref
        )

    metadata_only = ArtifactRef(
        artifact_id=_artifact("7").artifact_id,
        media_type="application/octet-stream",
        byte_length=0,
        schema_version=VERSION,
    )
    with pytest.raises(InformationBoundaryViolation, match="metadata is unavailable"):
        reader_port._verify_artifact_hash(metadata_only)


def test_permissive_adapter_and_ordering_helpers_cover_all_shapes() -> None:
    request = _request()
    basis = CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=request.base_commit,
        root_manifest=_manifest(),
    )
    permissive = PermissiveInformationBoundaryPort()
    permissive.verify_request_and_derivation_graph(request, basis)
    with pytest.raises(InformationBoundaryViolation, match="mismatched basis"):
        permissive.verify_request_and_derivation_graph(
            request,
            basis.model_copy(update={"commit_id": CommitId("sha256:" + "9" * 64)}),
        )
    with pytest.raises(InformationBoundaryViolation, match="no derivation"):
        permissive.verify_derivation_chain()

    assert _position_within(None, NarrativePosition(chapter_index=1))
    assert _position_within(NarrativePosition(chapter_index=9), None)
    position = NarrativePosition(chapter_index=2, scene_index=3, block_index=4)
    assert _position_key(position) == (2, 3, 4)
    assert _position_key(NarrativePosition(chapter_index=2)) == (2, -1, -1)
    assert [_scope_width(item) for item in AccessScope] == [0, 1, 2]


def test_request_verifies_root_intents_and_trusted_world_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(
        profile=MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC,
        chapter_text_changed=True,
    )
    trusted = TrustedWorldCandidateInput(
        candidate_artifact=_artifact("b"),
        producer_receipt=_artifact("c"),
    )
    request = request.model_copy(update={"world_mutation": trusted})
    basis = CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=request.base_commit,
        root_manifest=_manifest(),
    )
    port = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,))
    calls: list[dict[str, object]] = []

    def record(**kwargs: object) -> BoundaryPropagationReceipt:
        calls.append(kwargs)
        return _receipt()

    monkeypatch.setattr(port, "verify_derivation_chain", record)
    port.verify_request_and_derivation_graph(request, basis)

    assert [call["artifact"] for call in calls] == [
        request.root_update_intents[0].update_artifact,
        trusted.candidate_artifact,
    ]
    assert calls[0]["expected_builder_policy"] == (
        request.root_update_intents[0].builder_policy_ref.content_hash
    )


def test_request_accepts_a_valid_direct_source_and_no_world_mutation() -> None:
    request = _request()
    source = _artifact("2")
    visibility = _visibility(
        source,
        boundary_id=request.information_boundary.boundary_id,
        position=NarrativePosition(chapter_index=1),
    )
    request = request.model_copy(
        update={
            "source_artifacts": (source,),
            "source_visibility_receipts": (visibility,),
            "source_provenance": (SourceProvenance.REVEALED_TEXT,),
        }
    )
    basis = CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=request.base_commit,
        root_manifest=_manifest(),
    )

    InformationBoundaryPort(
        trusted_policy_hashes=(TRUSTED_POLICY,)
    ).verify_request_and_derivation_graph(request, basis)


@pytest.mark.parametrize(
    ("child_position", "child_scope", "message"),
    (
        (
            NarrativePosition(chapter_index=3),
            AccessScope.WRITER_SAFE,
            "widens visible narrative",
        ),
        (
            NarrativePosition(chapter_index=2),
            AccessScope.AUTHOR_PLANNING,
            "widens access scope",
        ),
    ),
)
def test_parent_derivation_cannot_widen_position_or_scope(
    child_position: NarrativePosition,
    child_scope: AccessScope,
    message: str,
) -> None:
    source = _artifact("2")
    visibility = _visibility(
        source,
        position=NarrativePosition(chapter_index=2),
        scope=AccessScope.WRITER_SAFE,
    )
    parent_ref = _artifact("5")
    parent_output = _artifact("7")
    parent = _rehash(
        _receipt().model_copy(
            update={
                "source_visibility_receipt_refs": (_visibility_ref(visibility),),
                "output_artifact_hash": parent_output.artifact_id,
                "effective_visible_through": NarrativePosition(chapter_index=2),
            }
        )
    )
    child_ref = _artifact("6")
    child_output = _artifact("8")
    child = _rehash(
        _parented_receipt(
            name="monotonic-child",
            output=child_output,
            parent_ref=parent_ref,
        ).model_copy(
            update={
                "effective_visible_through": child_position,
                "effective_access_scope": child_scope,
            }
        )
    )
    port = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,))
    port.register_visibility(visibility)
    port.register_derivation(parent, parent_ref, parent_output)
    port.register_derivation(child, child_ref, child_output)

    with pytest.raises(InformationBoundaryViolation, match=message):
        port.verify_derivation_chain(
            artifact=child_output,
            producer_receipt=child_ref,
            boundary=_boundary().model_copy(
                update={"maximum_visible_position": NarrativePosition(chapter_index=4)}
            ),
            configuration_fingerprint=TRUSTED_POLICY,
        )


def test_source_receipt_set_and_boundary_are_checked_before_derivation() -> None:
    source = _artifact("2")
    other_source = _artifact("3")
    own_visibility = _visibility(source)
    other_visibility = _visibility(other_source)
    foreign_visibility = _visibility(source, boundary_id=StableId("boundary.foreign"))

    for visibility, message in (
        (other_visibility, "exactly cover"),
        (foreign_visibility, "crosses boundary"),
    ):
        receipt = _rehash(
            _receipt().model_copy(
                update={"source_visibility_receipt_refs": (_visibility_ref(visibility),)}
            )
        )
        ref = _artifact("5")
        port = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,))
        port.register_visibility(visibility)
        if visibility is other_visibility:
            port.register_visibility(own_visibility)
        port.register_derivation(receipt, ref, _artifact("4"))

        with pytest.raises(InformationBoundaryViolation, match=message):
            port.verify_derivation_chain(
                artifact=_artifact("4"),
                producer_receipt=ref,
                boundary=_boundary(),
                configuration_fingerprint=TRUSTED_POLICY,
            )


def test_internal_source_index_corruption_and_evaluator_scope_fail_closed() -> None:
    source = _artifact("2")
    visibility = _visibility(source)
    receipt = _rehash(
        _receipt().model_copy(
            update={"source_visibility_receipt_refs": (_visibility_ref(visibility),)}
        )
    )
    ref = _artifact("5")
    port = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,))
    port.register_visibility(visibility)
    port.register_derivation(receipt, ref, _artifact("4"))
    port._visibility_by_source = {_artifact("9").artifact_id: visibility}

    with pytest.raises(InformationBoundaryViolation, match="source leaf"):
        port.verify_derivation_chain(
            artifact=_artifact("4"),
            producer_receipt=ref,
            boundary=_boundary(),
            configuration_fingerprint=TRUSTED_POLICY,
        )
    with pytest.raises(InformationBoundaryViolation, match="evaluator scope"):
        port._verify_scope(AccessScope.EVALUATOR, _boundary(), ())


def test_internal_visibility_boundary_corruption_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _artifact("2")
    foreign_visibility = _visibility(source, boundary_id=StableId("boundary.foreign"))
    receipt = _rehash(
        _receipt().model_copy(
            update={"source_visibility_receipt_refs": (_visibility_ref(foreign_visibility),)}
        )
    )
    ref = _artifact("5")
    port = InformationBoundaryPort(trusted_policy_hashes=(TRUSTED_POLICY,))
    port._visibility_by_source[source.artifact_id] = foreign_visibility
    port.register_derivation(receipt, ref, _artifact("4"))
    monkeypatch.setattr(
        port,
        "_resolve_visibility_receipt",
        lambda *_: foreign_visibility,
    )

    with pytest.raises(InformationBoundaryViolation, match="crosses information"):
        port.verify_derivation_chain(
            artifact=_artifact("4"),
            producer_receipt=ref,
            boundary=_boundary(),
            configuration_fingerprint=TRUSTED_POLICY,
        )


def test_output_reference_alias_and_placeholder_metadata_are_verified() -> None:
    visibility = _visibility(_artifact("2"))
    receipt = _rehash(
        _receipt().model_copy(
            update={"source_visibility_receipt_refs": (_visibility_ref(visibility),)}
        )
    )

    class Reader:
        def read_verified(self, _: ArtifactRef) -> bytes:
            return canonical_json_bytes(receipt.model_dump(mode="json"))

    output = _artifact("4")
    ref = _artifact("5")
    port = InformationBoundaryPort(
        Reader(),
        trusted_policy_hashes=(TRUSTED_POLICY,),
    )
    port.register_visibility(visibility)
    port.register_derivation(receipt, ref, output)

    assert port.read_derivation_receipt(output) == receipt
    port._verify_artifact_hash(
        ArtifactRef(
            artifact_id=output.artifact_id,
            media_type="application/octet-stream",
            byte_length=0,
            schema_version=VERSION,
        )
    )
