"""Trusted RootUpdateIntent materialization for Stage 2W."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootKind,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.changes import CandidateChangeBundle
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId
from novel_agent.domain.memory_write import (
    CandidateMaterialization,
    CandidateRevision,
    CanonicalWriteBasis,
    MemoryWriteCandidatePayload,
    RootUpdateKind,
)
from novel_agent.domain.stage2 import ContractRef
from novel_agent.ports.memory_write import RootMaterializationResult
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.overlay import OverlayError, WorldOverlay


class RootUpdateMaterializationError(ValueError):
    """An update intent is not a trusted, same-basis Root transformation."""


class RootUpdateMaterializer:
    """Materialize all Root intents and the World candidate as one bundle.

    The service never writes a canonical root.  It only constructs an immutable
    ``CandidateChangeBundle`` which must subsequently pass Validation and Commit.
    """

    def __init__(
        self,
        *,
        payload_loader: Callable[[ArtifactRef], MemoryWriteCandidatePayload] | None = None,
        artifact_writer: Any | None = None,
        policy_ref: ContractRef | None = None,
    ) -> None:
        self._payload_loader = payload_loader
        self._artifact_writer = artifact_writer
        self._policy_ref = policy_ref or ContractRef(
            contract_id=StableId("policy.root-update-materializer"),
            version=SchemaVersion("0.1.0"),
            content_hash=ArtifactId("sha256:" + "1" * 64),
        )

    def materialize_atomic_bundle(
        self,
        *,
        candidate: CandidateRevision,
        basis: CanonicalWriteBasis,
    ) -> RootMaterializationResult:
        if candidate.base_commit != basis.commit_id:
            raise RootUpdateMaterializationError("candidate and canonical basis commits differ")
        manifest = basis.root_manifest
        if manifest is None:  # guarded by CanonicalWriteBasis, keeps type narrowing explicit
            raise RootUpdateMaterializationError("canonical basis has no RootManifest")
        payload = self._load_payload(candidate)
        if payload.observed_changes.base_commit != basis.commit_id:
            raise RootUpdateMaterializationError("candidate change set has another base commit")

        proposed = manifest
        changed_root_kinds: list[str] = []
        produced_artifacts: list[ArtifactRef] = []
        for intent in payload.root_update_intents:
            base_root = _root_for_kind(proposed, intent.root_kind)
            if not _same_artifact(base_root, intent.expected_base_root):
                raise RootUpdateMaterializationError(
                    f"{intent.root_kind.value} intent is not bound to the canonical base root"
                )
            if intent.update_kind is RootUpdateKind.NOOP:
                continue
            new_root = _typed_root_ref(intent.root_kind, intent.update_artifact)
            proposed = _replace_root(proposed, intent.root_kind, new_root)
            if new_root.artifact_id != base_root.artifact_id:
                changed_root_kinds.append(intent.root_kind.value)
                produced_artifacts.append(intent.update_artifact)

        world_noop = True
        if payload.observed_changes.operations:
            if basis.canonical_world is None:
                raise RootUpdateMaterializationError(
                    "World operations require a verified canonical WorldRoot"
                )
            try:
                proposed_world = WorldOverlay().apply(
                    basis.canonical_world,
                    payload.observed_changes,
                    canonical_commit=basis.commit_id,
                )
            except OverlayError as error:
                raise RootUpdateMaterializationError(str(error)) from error
            world_noop = proposed_world.root_hash == basis.canonical_world.root_hash
            world_bytes = canonical_json_bytes(proposed_world.model_dump(mode="json"))
            stored_world = self._put_bytes(
                world_bytes,
                "application/vnd.novel-agent.world-root+json",
                proposed_world.schema_version,
            )
            world_ref = WorldRootRef.model_validate(stored_world.model_dump())
            if world_ref.artifact_id != manifest.world_root.artifact_id:
                proposed = proposed.model_copy(update={"world_root": world_ref})
                changed_root_kinds.append(RootKind.WORLD.value)
                produced_artifacts.append(world_ref)

        proposed = proposed.model_copy(update={"parent_commit_ids": (basis.commit_id,)})
        bundle = CandidateChangeBundle(
            bundle_id=StableId(f"bundle.{candidate.candidate_id.root}"),
            project_id=basis.project_id,
            run_id=_run_id(candidate),
            base_commit=basis.commit_id,
            observed_changes=payload.observed_changes,
            proposed_roots=proposed,
            produced_artifacts=tuple(produced_artifacts),
        )
        bundle_artifact = self._put_model(
            bundle,
            "application/vnd.novel-agent.candidate-change-bundle+json",
            manifest.schema_version,
        )
        materialization_payload = {
            "candidate_id": candidate.candidate_id.root,
            "candidate_content_hash": candidate.content_hash.root,
            "proposed_roots_hash": _manifest_hash(proposed).root,
            "bundle_artifact": bundle_artifact.artifact_id.root,
            "policy": self._policy_ref.content_hash.root,
        }
        materialization_receipt = self._put_bytes(
            canonical_json_bytes(materialization_payload),
            "application/vnd.novel-agent.candidate-materialization+json",
            manifest.schema_version,
        )
        materialization = CandidateMaterialization(
            candidate_id=candidate.candidate_id,
            candidate_content_hash=candidate.content_hash,
            bundle_artifact=bundle_artifact,
            proposed_roots_hash=_manifest_hash(proposed),
            materialization_receipt=materialization_receipt,
            materializer_policy_ref=self._policy_ref,
            bundle=bundle,
        )
        return RootMaterializationResult(
            materialization=materialization,
            bundle=bundle,
            world_mutation_noop=world_noop,
            changed_root_kinds=tuple(dict.fromkeys(changed_root_kinds)),
        )

    def _load_payload(self, candidate: CandidateRevision) -> MemoryWriteCandidatePayload:
        if self._payload_loader is None:
            raise RootUpdateMaterializationError("candidate payload loader is not configured")
        try:
            return self._payload_loader(candidate.candidate_artifact)
        except Exception as error:
            raise RootUpdateMaterializationError("candidate payload cannot be loaded") from error

    def _put_model(self, model: Any, media_type: str, version: SchemaVersion) -> ArtifactRef:
        return self._put_bytes(
            canonical_json_bytes(model.model_dump(mode="json")), media_type, version
        )

    def _put_bytes(self, data: bytes, media_type: str, version: SchemaVersion) -> ArtifactRef:
        if self._artifact_writer is not None:
            result = self._artifact_writer.put(data, media_type, version)
            if not isinstance(result, ArtifactRef):
                raise TypeError("artifact writer returned a non-ArtifactRef")
            return result
        return ArtifactRef(
            artifact_id=sha256_id(data),
            media_type=media_type,
            byte_length=len(data),
            schema_version=version,
        )


def _root_for_kind(manifest: RootManifest, kind: RootKind) -> ArtifactRef:
    return {
        RootKind.TEXT: manifest.text_root,
        RootKind.PLAN: manifest.plan_root,
        RootKind.WORLD: manifest.world_root,
        RootKind.REFERENCE: manifest.reference_root,
        RootKind.PROJECT_PROFILE: manifest.project_profile_root,
    }[kind]


def _replace_root(manifest: RootManifest, kind: RootKind, root: ArtifactRef) -> RootManifest:
    return manifest.model_copy(
        update={
            {
                RootKind.TEXT: "text_root",
                RootKind.PLAN: "plan_root",
                RootKind.WORLD: "world_root",
                RootKind.REFERENCE: "reference_root",
                RootKind.PROJECT_PROFILE: "project_profile_root",
            }[kind]: root
        }
    )


def _typed_root_ref(kind: RootKind, ref: ArtifactRef) -> ArtifactRef:
    payload = ref.model_dump()
    if kind is RootKind.TEXT:
        return cast(ArtifactRef, TextRootRef.model_validate(payload))
    if kind is RootKind.PLAN:
        return cast(ArtifactRef, PlanRootRef.model_validate(payload))
    if kind is RootKind.WORLD:
        return cast(ArtifactRef, WorldRootRef.model_validate(payload))
    if kind is RootKind.REFERENCE:
        return cast(ArtifactRef, ReferenceRootRef.model_validate(payload))
    return cast(ArtifactRef, ProjectProfileRootRef.model_validate(payload))


def _manifest_hash(manifest: RootManifest) -> ArtifactId:
    return sha256_id(canonical_json_bytes(manifest.model_dump(mode="json")))


def _same_artifact(left: ArtifactRef, right: ArtifactRef) -> bool:
    return (
        left.artifact_id == right.artifact_id
        and left.media_type == right.media_type
        and left.byte_length == right.byte_length
        and left.schema_version == right.schema_version
    )


def _run_id(candidate: CandidateRevision) -> RunId:
    # CandidateRevision intentionally does not carry run identity.  The runtime
    # binds it in the observed change set's source artifact lineage; use a stable
    # adapter identity for the generic materializer.
    return RunId(f"run.memory-write.{candidate.candidate_id.root[:80]}")


__all__ = ["RootUpdateMaterializationError", "RootUpdateMaterializer"]
