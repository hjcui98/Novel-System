"""Candidate WorldRoot overlay construction without mutating Canonical state."""

from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter, ValidationError

from novel_agent.domain.artifacts import RootManifest, WorldRootRef
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ChangeOperation,
    ChangeOperationType,
    ObservedChangeSet,
    WorldRecordKind,
)
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, StableId
from novel_agent.domain.memory import PlanObligation, WorldRootDocument
from novel_agent.domain.world import Entity, Event, RelationRecord, StateRecord
from novel_agent.services.content_addressing import canonical_json_bytes, world_root_content_id


class OverlayError(ValueError):
    pass


_MODEL_BY_KIND = {
    WorldRecordKind.ENTITY: Entity,
    WorldRecordKind.EVENT: Event,
    WorldRecordKind.STATE: StateRecord,
    WorldRecordKind.RELATION: RelationRecord,
    WorldRecordKind.OBLIGATION: PlanObligation,
}
_ID_FIELD_BY_KIND = {
    WorldRecordKind.ENTITY: "entity_id",
    WorldRecordKind.EVENT: "event_id",
    WorldRecordKind.STATE: "state_id",
    WorldRecordKind.RELATION: "relation_id",
    WorldRecordKind.OBLIGATION: "obligation_id",
}


class WorldOverlay:
    def apply(
        self,
        world: WorldRootDocument,
        changes: ObservedChangeSet,
        *,
        canonical_commit: CommitId | None = None,
    ) -> WorldRootDocument:
        expected_base = canonical_commit or world.source_commit
        if changes.base_commit != expected_base:
            raise OverlayError("change set base commit differs from WorldRoot basis")
        records: dict[WorldRecordKind, list[Any]] = {
            WorldRecordKind.ENTITY: list(world.entities),
            WorldRecordKind.EVENT: list(world.events),
            WorldRecordKind.STATE: list(world.states),
            WorldRecordKind.RELATION: list(world.relations),
            WorldRecordKind.OBLIGATION: list(world.obligations),
        }
        for operation in changes.operations:
            self._apply_operation(records, operation)
        provisional = WorldRootDocument(
            root_hash=ArtifactId("sha256:" + "0" * 64),
            schema_version=world.schema_version,
            source_commit=world.source_commit,
            entities=tuple(records[WorldRecordKind.ENTITY]),
            events=tuple(records[WorldRecordKind.EVENT]),
            states=tuple(records[WorldRecordKind.STATE]),
            relations=tuple(records[WorldRecordKind.RELATION]),
            obligations=tuple(records[WorldRecordKind.OBLIGATION]),
        )
        return provisional.model_copy(update={"root_hash": world_root_content_id(provisional)})

    @staticmethod
    def _apply_operation(
        records: dict[WorldRecordKind, list[Any]], operation: ChangeOperation
    ) -> None:
        if operation.root_kind != "world" or not isinstance(operation.payload, dict):
            raise OverlayError("Stage 1 overlay accepts only structured WorldRoot operations")
        try:
            raw_kind = operation.payload["record_type"]
            raw_record = operation.payload["record"]
            if not isinstance(raw_kind, str) or not isinstance(raw_record, dict):
                raise TypeError("record type and record have invalid shapes")
            kind = WorldRecordKind(raw_kind)
            model_type = _MODEL_BY_KIND[kind]
            record: Any = TypeAdapter(model_type).validate_json(
                json.dumps(raw_record, ensure_ascii=False), strict=True
            )
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise OverlayError("WorldRoot operation payload is invalid") from error
        identity = getattr(record, _ID_FIELD_BY_KIND[kind])
        if identity != operation.target_id:
            raise OverlayError("operation target does not match record identity")
        collection = records[kind]
        positions = [
            index
            for index, item in enumerate(collection)
            if getattr(item, _ID_FIELD_BY_KIND[kind]) == operation.target_id
        ]
        if operation.operation is ChangeOperationType.CREATE:
            if positions:
                raise OverlayError("create target already exists")
            collection.append(record)
        elif operation.operation is ChangeOperationType.REPLACE:
            if not positions:
                raise OverlayError("replace target does not exist")
            collection[positions[0]] = record
        else:
            if not positions:
                raise OverlayError("retire target does not exist")
            collection.pop(positions[0])


def build_candidate_bundle(
    *,
    project_id: ProjectId,
    run_id: RunId,
    current_manifest: RootManifest,
    changes: ObservedChangeSet,
    proposed_world: WorldRootDocument,
) -> CandidateChangeBundle:
    world_bytes = canonical_json_bytes(proposed_world.model_dump(mode="json"))
    world_ref = WorldRootRef(
        artifact_id=proposed_world.root_hash,
        media_type="application/vnd.novel-agent.world-root+json",
        byte_length=len(world_bytes),
        schema_version=proposed_world.schema_version,
    )
    proposed_roots = current_manifest.model_copy(
        update={"world_root": world_ref, "parent_commit_ids": (changes.base_commit,)}
    )
    return CandidateChangeBundle(
        bundle_id=StableId(f"bundle.{changes.change_set_id.root}"),
        project_id=project_id,
        run_id=run_id,
        base_commit=changes.base_commit,
        observed_changes=changes,
        proposed_roots=proposed_roots,
        produced_artifacts=(world_ref,),
    )
