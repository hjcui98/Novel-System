"""Audited model-assisted Curator producing deterministic, evidence-bound changes."""

from __future__ import annotations

import hashlib

from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import (
    ChangeOperation,
    ChangeOperationType,
    ChapterChangeDraft,
    CuratedOperationDraft,
    CuratorEventRecord,
    CuratorObligationRecord,
    CuratorRelationRecord,
    CuratorStateRecord,
    ObservedChangeSet,
    WorldRecordKind,
)
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.benchmark_importer import validate_evidence_ref
from novel_agent.services.content_addressing import canonical_json_bytes, quote_hash
from novel_agent.services.curation import Stage1Curator
from novel_agent.services.model_gateway import ModelGateway


class ModelCurationContractError(ValueError):
    pass


_RECORD_ID_FIELD = {
    WorldRecordKind.ENTITY: "entity_id",
    WorldRecordKind.EVENT: "event_id",
    WorldRecordKind.STATE: "state_id",
    WorldRecordKind.RELATION: "relation_id",
    WorldRecordKind.OBLIGATION: "obligation_id",
}


class ModelCurator:
    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway

    async def extract(
        self,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        current_world: WorldRootDocument,
        request: ModelRequest,
    ) -> tuple[ObservedChangeSet, ModelCallRecord]:
        changes, call, _ = await self.extract_reported(
            text_root,
            chapter_index,
            base_commit,
            current_world,
            request,
        )
        return changes, call

    async def extract_reported(
        self,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        current_world: WorldRootDocument,
        request: ModelRequest,
        *,
        contract_prompt: str | None = None,
    ) -> tuple[ObservedChangeSet, ModelCallRecord, ChapterChangeDraft]:
        chapter = Stage1Curator._chapter(text_root, chapter_index)
        contract = f"{contract_prompt}\n\n" if contract_prompt else ""
        safe_request = request.model_copy(
            update={
                "prompt": (
                    contract + "Extract ChapterChangeDraft JSON from this revealed chapter only. "
                    "Every operation must cite exact EvidenceRef spans from the supplied chapter; "
                    "preserve assertion/rumor/dream truth classes and do not infer future events.\n"
                    '<CURATOR_INPUT trusted="false">\n'
                    f"BASE_COMMIT={base_commit.root}\n"
                    f"WORLD={current_world.model_dump_json()}\n"
                    f"CHAPTER={chapter.model_dump_json()}\n"
                    "</CURATOR_INPUT>"
                )
            }
        )
        draft, call = await self._gateway.generate_structured(safe_request, ChapterChangeDraft)
        if draft.chapter_index != chapter_index:
            raise ModelCurationContractError("Curator draft chapter differs from requested chapter")
        draft = self._normalize_operations(draft, current_world)
        chapter_blocks = {
            block.block_id: block for scene in chapter.scenes for block in scene.blocks
        }
        scoped_operations = tuple(
            operation
            for operation in draft.operations
            if all(selection.block_id in chapter_blocks for selection in operation.evidence_refs)
        )
        if len(scoped_operations) != len(draft.operations):
            dropped = tuple(
                operation.target_id.root
                for operation in draft.operations
                if operation not in scoped_operations
            )
            original_count = len(draft.operations)
            unresolved = (
                *draft.unresolved,
                ("runtime filtered out-of-chapter evidence targets: " + ", ".join(dropped))[:160],
            )
            draft = draft.model_copy(
                update={
                    "operations": scoped_operations,
                    "unresolved": unresolved[:4],
                    "coverage": min(draft.coverage, len(scoped_operations) / original_count),
                }
            )
        identities = tuple(
            (operation.record_kind, operation.target_id) for operation in draft.operations
        )
        if len(identities) != len(set(identities)):
            raise ModelCurationContractError("Curator draft targets one record more than once")

        operations: list[ChangeOperation] = []
        for operation in draft.operations:
            bound_evidence = []
            for selection in operation.evidence_refs:
                block = chapter_blocks.get(selection.block_id)
                if block is None:
                    raise ModelCurationContractError(
                        "Curator evidence block is outside the requested chapter"
                    )
                # Structured models occasionally report an end offset measured against
                # the serialized block rather than its text.  The selected block is
                # already chapter-scoped and content-addressed. Preserve valid starts
                # and clamp their tails; if both offsets use the wrong coordinate
                # system, conservatively bind the complete selected block. Unknown
                # blocks remain a hard failure above.
                if selection.start >= len(block.text) or selection.end <= selection.start:
                    bounded_selection = selection.model_copy(
                        update={"start": 0, "end": len(block.text)}
                    )
                else:
                    bounded_selection = selection.model_copy(
                        update={"end": min(selection.end, len(block.text))}
                    )
                selected = block.text[bounded_selection.start : bounded_selection.end]
                evidence_digest = self._digest(
                    chapter.chapter_id.root.encode(),
                    bounded_selection.block_id.root.encode(),
                    str(bounded_selection.start).encode(),
                    str(bounded_selection.end).encode(),
                )
                canonical_evidence = EvidenceRef(
                    evidence_id=StableId(f"evidence.curator.{evidence_digest}"),
                    root_hash=text_root.root_hash,
                    object_hash=sha256_id(block.text.encode("utf-8")),
                    chapter_id=block.chapter_id,
                    scene_id=block.scene_id,
                    span=TextSpanRef.model_validate(bounded_selection.model_dump()),
                    quote_hash=quote_hash(selected),
                    resolved_at_commit=base_commit,
                    support_status=EvidenceSupportStatus.CURRENT,
                )
                validate_evidence_ref(canonical_evidence, text_root)
                bound_evidence.append(canonical_evidence)
            evidence_refs = tuple(bound_evidence)
            record = operation.record.model_dump(mode="json")
            record[_RECORD_ID_FIELD[operation.record_kind]] = operation.target_id.root
            if operation.record_kind is not WorldRecordKind.ENTITY:
                record["evidence_refs"] = [
                    evidence.model_dump(mode="json") for evidence in evidence_refs
                ]
            bound_operation = operation.model_copy(update={"evidence_refs": evidence_refs})
            digest = self._digest(canonical_json_bytes(bound_operation.model_dump(mode="json")))
            operations.append(
                ChangeOperation(
                    operation_id=StableId(f"change.model.{digest}"),
                    root_kind=RootKind.WORLD,
                    operation=operation.operation,
                    target_id=operation.target_id,
                    payload={
                        "record_type": operation.record_kind.value,
                        "record": record,
                    },
                    evidence_refs=evidence_refs,
                )
            )
        source_bytes = canonical_json_bytes(chapter.model_dump(mode="json"))
        return (
            ObservedChangeSet(
                change_set_id=StableId(
                    "changes.model."
                    f"{self._digest(base_commit.root.encode(), chapter.chapter_id.root.encode())}"
                ),
                base_commit=base_commit,
                source_artifact=ArtifactRef(
                    artifact_id=sha256_id(source_bytes),
                    media_type="application/vnd.novel-agent.chapter+json",
                    byte_length=len(source_bytes),
                    schema_version=SchemaVersion("0.1.0"),
                ),
                operations=tuple(operations),
            ),
            call,
            draft,
        )

    @staticmethod
    def _normalize_operations(
        draft: ChapterChangeDraft,
        current_world: WorldRootDocument,
    ) -> ChapterChangeDraft:
        """Normalize existence semantics and filter dangling entity references."""
        current_ids = {
            WorldRecordKind.ENTITY: {item.entity_id for item in current_world.entities},
            WorldRecordKind.EVENT: {item.event_id for item in current_world.events},
            WorldRecordKind.STATE: {item.state_id for item in current_world.states},
            WorldRecordKind.RELATION: {item.relation_id for item in current_world.relations},
            WorldRecordKind.OBLIGATION: {item.obligation_id for item in current_world.obligations},
        }
        created_entities = {
            operation.target_id
            for operation in draft.operations
            if operation.record_kind is WorldRecordKind.ENTITY
            and operation.operation is ChangeOperationType.CREATE
        }
        known_entities = current_ids[WorldRecordKind.ENTITY] | created_entities
        accepted: list[CuratedOperationDraft] = []
        dropped: list[str] = []
        for operation in draft.operations:
            # Chapter replay may add or revise observed memory, but it cannot
            # autonomously delete canonical memory. Destructive retirement is a
            # separate patch workflow requiring explicit human approval.
            if operation.operation is ChangeOperationType.RETIRE:
                dropped.append(operation.target_id.root)
                continue
            record = operation.record
            referenced_entities: set[StableId] = set()
            if isinstance(record, CuratorEventRecord):
                referenced_entities.update(record.participant_ids)
            elif isinstance(record, CuratorStateRecord):
                referenced_entities.add(record.subject_id)
            elif isinstance(record, CuratorRelationRecord):
                referenced_entities.update((record.subject_id, record.object_id))
            elif isinstance(record, CuratorObligationRecord):
                referenced_entities.update(record.owner_ids)
            missing = referenced_entities - known_entities
            if missing:
                dropped.append(operation.target_id.root)
                continue
            exists = operation.target_id in current_ids[operation.record_kind]
            normalized_type = operation.operation
            if operation.operation is ChangeOperationType.CREATE and exists:
                normalized_type = ChangeOperationType.REPLACE
            elif operation.operation is ChangeOperationType.REPLACE and not exists:
                normalized_type = ChangeOperationType.CREATE
            accepted.append(operation.model_copy(update={"operation": normalized_type}))
        unresolved = list(draft.unresolved)
        if dropped:
            detail = "runtime filtered dangling or missing targets: " + ", ".join(dropped)
            unresolved.append(detail[:160])
        original_count = len(draft.operations)
        bounded_coverage = (
            draft.coverage
            if original_count == 0
            else min(draft.coverage, len(accepted) / original_count)
        )
        return draft.model_copy(
            update={
                "operations": tuple(accepted),
                "unresolved": tuple(unresolved[:4]),
                "coverage": bounded_coverage,
            }
        )

    @staticmethod
    def _digest(*parts: bytes) -> str:
        return hashlib.sha256(b"\0".join(parts)).hexdigest()[:24]
