"""Fail-closed information-boundary and propagation-receipt verification."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, SchemaVersion
from novel_agent.domain.memory_write import (
    BoundaryPropagationReceipt,
    CanonicalWriteBasis,
    InformationBoundary,
    MemoryWriteWorkflowRequest,
    NarrativePosition,
    SourceVisibilityReceipt,
    TrustedWorldCandidateInput,
)
from novel_agent.domain.stage2 import AccessScope
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes


class InformationBoundaryViolation(ValueError):
    """A source or derived artifact cannot be proven safe for this workflow."""


class InformationBoundaryPort:
    """In-process implementation of the Stage 2W boundary port.

    Receipts are registered by a trusted producer adapter.  The verifier never
    trusts a producer name alone: it checks every hash, base, boundary, policy,
    and recursively walks the derivation DAG.
    """

    def __init__(
        self,
        artifact_reader: Any | None = None,
        *,
        trusted_policy_hashes: Iterable[ArtifactId] = (),
    ) -> None:
        self._artifact_reader = artifact_reader
        self._visibility_by_source: dict[ArtifactId, SourceVisibilityReceipt] = {}
        self._derivation_by_receipt: dict[ArtifactId, BoundaryPropagationReceipt] = {}
        self._derivation_artifact_by_output: dict[ArtifactId, ArtifactId] = {}
        self._output_ref_by_artifact: dict[ArtifactId, ArtifactRef] = {}
        self._trusted_policy_hashes = frozenset(trusted_policy_hashes)

    def register_visibility(self, receipt: SourceVisibilityReceipt) -> None:
        self._validate_visibility_hash(receipt)
        existing = self._visibility_by_source.get(receipt.source_artifact.artifact_id)
        if existing is not None and existing != receipt:
            raise InformationBoundaryViolation("source artifact has multiple visibility receipts")
        self._visibility_by_source[receipt.source_artifact.artifact_id] = receipt

    def register_derivation(
        self,
        receipt: BoundaryPropagationReceipt,
        receipt_artifact: ArtifactRef | None = None,
        output_artifact: ArtifactRef | None = None,
    ) -> ArtifactRef:
        self._validate_receipt_hash(receipt)
        self._verify_builder_policy(receipt.builder_policy_hash)
        if (
            output_artifact is not None
            and output_artifact.artifact_id != receipt.output_artifact_hash
        ):
            raise InformationBoundaryViolation("derivation output reference does not match receipt")
        key = receipt_artifact.artifact_id if receipt_artifact else receipt.receipt_hash
        existing = self._derivation_by_receipt.get(key)
        if existing is not None and existing != receipt:
            raise InformationBoundaryViolation("derivation receipt identity collision")
        self._derivation_by_receipt[key] = receipt
        self._derivation_artifact_by_output[receipt.output_artifact_hash] = key
        if output_artifact is not None:
            self._output_ref_by_artifact[receipt.output_artifact_hash] = output_artifact
        return receipt_artifact or ArtifactRef(
            artifact_id=receipt.receipt_hash,
            media_type="application/vnd.novel-agent.boundary-propagation-receipt+json",
            byte_length=len(canonical_json_bytes(receipt.model_dump(mode="json"))),
            schema_version=receipt_schema_version(receipt),
        )

    def verify_request_and_derivation_graph(
        self,
        request: MemoryWriteWorkflowRequest,
        basis: CanonicalWriteBasis,
    ) -> None:
        if request.information_boundary.base_commit != request.base_commit:
            raise InformationBoundaryViolation("request boundary base differs from request base")
        if basis.commit_id != request.base_commit or basis.project_id != request.project_id:
            raise InformationBoundaryViolation("canonical basis does not match request identity")
        if len(request.source_artifacts) != len(request.source_visibility_receipts):
            raise InformationBoundaryViolation(
                "source visibility receipt count does not match sources"
            )
        if request.source_provenance and len(request.source_provenance) != len(
            request.source_artifacts
        ):
            raise InformationBoundaryViolation("source provenance count does not match sources")
        for index, (artifact, receipt) in enumerate(
            zip(request.source_artifacts, request.source_visibility_receipts, strict=True)
        ):
            self.register_visibility(receipt)
            if receipt.source_artifact != artifact:
                raise InformationBoundaryViolation(
                    "visibility receipt content does not match source"
                )
            self._verify_artifact_hash(artifact)
            if receipt.boundary_id != request.information_boundary.boundary_id:
                raise InformationBoundaryViolation("source receipt belongs to another boundary")
            if (
                request.source_provenance
                and receipt.provenance is not request.source_provenance[index]
            ):
                raise InformationBoundaryViolation(
                    "source provenance differs from visibility receipt"
                )
            self._verify_visibility(request.information_boundary, receipt, request.access_scope)

        for intent in request.root_update_intents:
            self.verify_derivation_chain(
                artifact=intent.update_artifact,
                producer_receipt=intent.producer_receipt,
                boundary=request.information_boundary,
                configuration_fingerprint=request.configuration_fingerprint,
                expected_builder_policy=intent.builder_policy_ref.content_hash,
            )
        world_input = request.world_mutation
        if isinstance(world_input, TrustedWorldCandidateInput):
            self.verify_derivation_chain(
                artifact=world_input.candidate_artifact,
                producer_receipt=world_input.producer_receipt,
                boundary=request.information_boundary,
                configuration_fingerprint=request.configuration_fingerprint,
            )

    def verify_derivation_chain(
        self,
        *,
        artifact: ArtifactRef,
        producer_receipt: ArtifactRef,
        boundary: InformationBoundary,
        configuration_fingerprint: ArtifactId,
        expected_builder_policy: ArtifactId | None = None,
    ) -> BoundaryPropagationReceipt:
        receipt = self._resolve_derivation(producer_receipt)
        visited: set[ArtifactId] = set()
        self._verify_derivation(
            receipt,
            artifact,
            boundary,
            configuration_fingerprint,
            expected_builder_policy,
            visited,
        )
        return receipt

    def verify_candidate_derivation(
        self,
        *,
        artifact: ArtifactRef,
        producer_receipt: ArtifactRef,
        boundary: InformationBoundary,
        configuration_fingerprint: ArtifactId,
    ) -> BoundaryPropagationReceipt:
        return self.verify_derivation_chain(
            artifact=artifact,
            producer_receipt=producer_receipt,
            boundary=boundary,
            configuration_fingerprint=configuration_fingerprint,
        )

    def read_derivation_receipt(self, reference: ArtifactRef) -> BoundaryPropagationReceipt:
        """Read a registered or content-addressed derivation receipt."""

        return self._resolve_derivation(reference)

    def _verify_derivation(
        self,
        receipt: BoundaryPropagationReceipt,
        artifact: ArtifactRef,
        boundary: InformationBoundary,
        configuration_fingerprint: ArtifactId,
        expected_builder_policy: ArtifactId | None,
        visited: set[ArtifactId],
    ) -> None:
        if receipt.receipt_hash in visited:
            raise InformationBoundaryViolation("derivation receipt graph contains a cycle")
        visited.add(receipt.receipt_hash)
        if receipt.boundary_id != boundary.boundary_id:
            raise InformationBoundaryViolation("derivation receipt belongs to another boundary")
        if receipt.base_commit != boundary.base_commit:
            raise InformationBoundaryViolation(
                "derivation receipt base commit differs from boundary"
            )
        if receipt.output_artifact_hash != artifact.artifact_id:
            raise InformationBoundaryViolation("derivation output hash differs from artifact")
        if (
            expected_builder_policy is not None
            and receipt.builder_policy_hash != expected_builder_policy
        ):
            raise InformationBoundaryViolation("derivation builder policy is not the intent policy")
        self._verify_builder_policy(receipt.builder_policy_hash)
        self._verify_artifact_hash(artifact)
        if not _position_within(
            receipt.effective_visible_through, boundary.maximum_visible_position
        ):
            raise InformationBoundaryViolation(
                "derived artifact exceeds the visible narrative position"
            )
        self._verify_scope(receipt.effective_access_scope, boundary, ())

        resolved_source_receipts = tuple(
            self._resolve_visibility_receipt(reference, boundary)
            for reference in receipt.source_visibility_receipt_refs
        )
        resolved_source_ids = {
            item.source_artifact.artifact_id for item in resolved_source_receipts
        }
        input_source_ids = {item.artifact_id for item in receipt.input_source_artifact_refs}
        if resolved_source_ids != input_source_ids:
            raise InformationBoundaryViolation(
                "derivation visibility receipts do not exactly cover source inputs"
            )
        for source in receipt.input_source_artifact_refs:
            visibility = self._visibility_by_source.get(source.artifact_id)
            if visibility is None:
                raise InformationBoundaryViolation(
                    "derivation has a source leaf without visibility receipt"
                )
            if visibility.boundary_id != boundary.boundary_id:
                raise InformationBoundaryViolation(
                    "derivation source crosses information boundaries"
                )
            self._verify_visibility(
                boundary,
                visibility,
                receipt.effective_access_scope,
                receipt.effective_visible_through,
                derived_output=True,
            )
            self._verify_artifact_hash(source)

        for parent_ref in receipt.input_derivation_receipt_refs:
            parent = self._resolve_derivation(parent_ref)
            parent_artifact = self._output_ref_by_artifact.get(
                parent.output_artifact_hash,
                ArtifactRef(
                    artifact_id=parent.output_artifact_hash,
                    media_type="application/octet-stream",
                    byte_length=0,
                    schema_version=receipt_schema_version(parent),
                ),
            )
            self._verify_derivation(
                parent,
                parent_artifact,
                boundary,
                configuration_fingerprint,
                None,
                visited,
            )
            if not _position_within(
                receipt.effective_visible_through, parent.effective_visible_through
            ):
                raise InformationBoundaryViolation("derivation widens visible narrative position")
            self._verify_scope(
                receipt.effective_access_scope,
                boundary,
                (parent.effective_access_scope,),
            )
        visited.remove(receipt.receipt_hash)

    def _resolve_derivation(self, reference: ArtifactRef) -> BoundaryPropagationReceipt:
        receipt = self._derivation_by_receipt.get(reference.artifact_id)
        if receipt is None:
            receipt = self._derivation_by_receipt.get(
                self._derivation_artifact_by_output.get(
                    reference.artifact_id, ArtifactId("sha256:" + "0" * 64)
                )
            )
        if receipt is None and self._artifact_reader is not None:
            try:
                raw = self._artifact_reader.read_verified(reference)
                receipt = BoundaryPropagationReceipt.model_validate_json(raw, strict=True)
            except Exception as error:  # pragma: no cover - adapter-specific corruption path
                raise InformationBoundaryViolation("propagation receipt cannot be read") from error
        if receipt is None:
            raise InformationBoundaryViolation("missing propagation receipt")
        self._validate_receipt_hash(receipt)
        return receipt

    def _resolve_visibility_receipt(
        self,
        reference: ArtifactRef,
        boundary: InformationBoundary,
    ) -> SourceVisibilityReceipt:
        for receipt in self._visibility_by_source.values():
            if receipt.receipt_hash == reference.artifact_id:
                if receipt.boundary_id != boundary.boundary_id:
                    raise InformationBoundaryViolation("visibility receipt crosses boundary")
                return receipt
        raise InformationBoundaryViolation("unknown source visibility receipt artifact")

    def _verify_visibility(
        self,
        boundary: InformationBoundary,
        receipt: SourceVisibilityReceipt,
        requested_scope: AccessScope,
        derived_position: NarrativePosition | None = None,
        derived_output: bool = False,
    ) -> None:
        if receipt.boundary_id != boundary.boundary_id:
            raise InformationBoundaryViolation("source visibility receipt has the wrong boundary")
        if boundary.evaluator_sources_forbidden and receipt.access_scope is AccessScope.EVALUATOR:
            raise InformationBoundaryViolation(
                "evaluator-only source is forbidden by this boundary"
            )
        if not _position_within(receipt.visible_through, boundary.maximum_visible_position):
            raise InformationBoundaryViolation("source visibility exceeds the workflow cutoff")
        if not _position_within(derived_position, receipt.visible_through):
            raise InformationBoundaryViolation("derived artifact exceeds source visibility")
        if derived_output:
            self._verify_scope(requested_scope, boundary, (receipt.access_scope,))
        else:
            self._verify_scope(receipt.access_scope, boundary, (requested_scope,))

    def _verify_scope(
        self,
        effective: AccessScope,
        boundary: InformationBoundary,
        parents: Iterable[AccessScope],
    ) -> None:
        if boundary.evaluator_sources_forbidden and effective is AccessScope.EVALUATOR:
            raise InformationBoundaryViolation("evaluator scope is forbidden by the boundary")
        if any(_scope_width(effective) > _scope_width(parent) for parent in parents):
            raise InformationBoundaryViolation("derivation widens access scope")

    def _verify_artifact_hash(self, artifact: ArtifactRef) -> None:
        if self._artifact_reader is None:
            return
        if artifact.byte_length == 0 and artifact.media_type == "application/octet-stream":
            artifact = self._output_ref_by_artifact.get(artifact.artifact_id, artifact)
            if artifact.byte_length == 0:
                raise InformationBoundaryViolation(
                    "derivation output metadata is unavailable for artifact verification"
                )
        try:
            self._artifact_reader.read_verified(artifact)
        except Exception as error:  # pragma: no cover - adapter-specific corruption path
            raise InformationBoundaryViolation(
                "artifact hash or metadata verification failed: "
                f"{artifact.artifact_id.root} ({artifact.media_type})"
            ) from error

    def _verify_builder_policy(self, policy_hash: ArtifactId) -> None:
        if policy_hash not in self._trusted_policy_hashes:
            raise InformationBoundaryViolation("derivation builder policy is not trusted")

    @staticmethod
    def _validate_receipt_hash(receipt: BoundaryPropagationReceipt) -> None:
        payload = receipt.model_dump(mode="json")
        payload["receipt_hash"] = None
        expected = sha256_id(canonical_json_bytes(payload))
        if receipt.receipt_hash != expected:
            raise InformationBoundaryViolation("propagation receipt hash is invalid")

    @staticmethod
    def _validate_visibility_hash(receipt: SourceVisibilityReceipt) -> None:
        payload = receipt.model_dump(mode="json")
        payload["receipt_hash"] = None
        expected = sha256_id(canonical_json_bytes(payload))
        if receipt.receipt_hash != expected:
            raise InformationBoundaryViolation("source visibility receipt hash is invalid")


class PermissiveInformationBoundaryPort:
    """Explicit test adapter for ports that already enforce their own boundary."""

    def verify_request_and_derivation_graph(
        self, request: MemoryWriteWorkflowRequest, basis: CanonicalWriteBasis
    ) -> None:
        if request.base_commit != basis.commit_id or request.project_id != basis.project_id:
            raise InformationBoundaryViolation("test boundary adapter received a mismatched basis")

    def verify_derivation_chain(self, **_: Any) -> BoundaryPropagationReceipt:
        raise InformationBoundaryViolation("permissive adapter has no derivation receipt authority")


def _position_within(
    value: NarrativePosition | None,
    maximum: NarrativePosition | None,
) -> bool:
    if maximum is None or value is None:
        return True
    return _position_key(value) <= _position_key(maximum)


def _position_key(position: NarrativePosition) -> tuple[int, int, int]:
    return (
        position.chapter_index,
        -1 if position.scene_index is None else position.scene_index,
        -1 if position.block_index is None else position.block_index,
    )


def _scope_width(scope: AccessScope) -> int:
    # A larger number means more information is exposed.  WRITER_SAFE is the
    # narrowest scope, while EVALUATOR is the broadest and is separately
    # forbidden by normal write boundaries.  Derived data may only preserve or
    # narrow this width.
    return {
        AccessScope.WRITER_SAFE: 0,
        AccessScope.AUTHOR_PLANNING: 1,
        AccessScope.EVALUATOR: 2,
    }[scope]


def receipt_schema_version(receipt: DomainModel) -> SchemaVersion:
    del receipt
    return SchemaVersion("0.1.0")


__all__ = [
    "InformationBoundaryPort",
    "InformationBoundaryViolation",
    "PermissiveInformationBoundaryPort",
]
