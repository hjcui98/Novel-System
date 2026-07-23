"""Audited model-assisted Curator producing deterministic, evidence-bound changes."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

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
from novel_agent.domain.memory_write import ProposalConflict, ProposalEvidenceMergeReceipt
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.benchmark_importer import validate_evidence_ref
from novel_agent.services.content_addressing import canonical_json_bytes, quote_hash
from novel_agent.services.curation import Stage1Curator
from novel_agent.services.model_gateway import ModelGateway


class ModelCurationContractError(ValueError):
    pass


class CuratorProposalSemanticRejected(ModelCurationContractError):
    def __init__(
        self,
        reason_code: str,
        conflicts: tuple[ProposalConflict, ...],
        *,
        information_boundary: bool = False,
        safe_feedback: tuple[str, ...] = (),
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.conflicts = conflicts
        self.information_boundary = information_boundary
        self.safe_feedback = safe_feedback


_RECORD_ID_FIELD = {
    WorldRecordKind.ENTITY: "entity_id",
    WorldRecordKind.EVENT: "event_id",
    WorldRecordKind.STATE: "state_id",
    WorldRecordKind.RELATION: "relation_id",
    WorldRecordKind.OBLIGATION: "obligation_id",
}


TargetResolver = Callable[[WorldRecordKind, StableId, WorldRootDocument], StableId]


class ModelCurator:
    def __init__(
        self,
        gateway: ModelGateway,
        *,
        target_resolver: TargetResolver | None = None,
    ) -> None:
        self._gateway = gateway
        self._target_resolver = target_resolver or (lambda _kind, target_id, _world: target_id)
        self.last_evidence_merge_receipts: tuple[ProposalEvidenceMergeReceipt, ...] = ()

    @property
    def gateway(self) -> ModelGateway:
        return self._gateway

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
        if any(
            selection.block_id not in chapter_blocks
            for operation in draft.operations
            for selection in operation.evidence_refs
        ):
            raise CuratorProposalSemanticRejected(
                "CURATOR_PROPOSAL_INFORMATION_BOUNDARY",
                (),
                information_boundary=True,
            )
        invalid_selections = tuple(
            (
                selection.block_id,
                selection.start,
                selection.end,
                len(chapter_blocks[selection.block_id].text),
            )
            for operation in draft.operations
            for selection in operation.evidence_refs
            if not (selection.start < selection.end <= len(chapter_blocks[selection.block_id].text))
        )
        if invalid_selections:
            raise CuratorProposalSemanticRejected(
                "CURATOR_PROPOSAL_INVALID_EVIDENCE",
                (),
                safe_feedback=tuple(
                    (
                        f"{block_id.root}: require 0 <= start < end <= {block_length}; "
                        f"received start={start}, end={end}"
                    )[:240]
                    for block_id, start, end, block_length in invalid_selections[:4]
                ),
            )
        draft, merge_receipts = self._merge_normalized_collisions(draft, base_commit)
        self.last_evidence_merge_receipts = merge_receipts

        operations: list[ChangeOperation] = []
        for operation in draft.operations:
            bound_evidence = []
            for selection in operation.evidence_refs:
                # The scope filter above guarantees every retained selection is
                # bound to a block in this chapter.
                block = chapter_blocks[selection.block_id]
                selected = block.text[selection.start : selection.end]
                evidence_digest = self._digest(
                    chapter.chapter_id.root.encode(),
                    selection.block_id.root.encode(),
                    str(selection.start).encode(),
                    str(selection.end).encode(),
                )
                canonical_evidence = EvidenceRef(
                    evidence_id=StableId(f"evidence.curator.{evidence_digest}"),
                    root_hash=text_root.root_hash,
                    object_hash=sha256_id(block.text.encode("utf-8")),
                    chapter_id=block.chapter_id,
                    scene_id=block.scene_id,
                    span=TextSpanRef.model_validate(selection.model_dump()),
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

    def _normalize_operations(
        self,
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
        current_states = {item.state_id: item for item in current_world.states}
        created_entities = {
            operation.target_id
            for operation in draft.operations
            if operation.record_kind is WorldRecordKind.ENTITY
            and operation.operation is ChangeOperationType.CREATE
        }
        known_entities = current_ids[WorldRecordKind.ENTITY] | created_entities
        accepted: list[CuratedOperationDraft] = []
        dropped: list[str] = []
        unchanged: list[str] = []
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
            if (
                operation.record_kind is WorldRecordKind.STATE
                and normalized_type is ChangeOperationType.REPLACE
                and isinstance(record, CuratorStateRecord)
                and (current := current_states.get(operation.target_id)) is not None
                and (
                    record.subject_id,
                    record.predicate,
                    record.value,
                    record.truth_class,
                )
                == (
                    current.subject_id,
                    current.predicate,
                    current.value,
                    current.truth_class,
                )
            ):
                unchanged.append(operation.target_id.root)
                continue
            normalized_target = self._target_resolver(
                operation.record_kind,
                operation.target_id,
                current_world,
            )
            accepted.append(
                operation.model_copy(
                    update={
                        "operation": normalized_type,
                        "target_id": normalized_target,
                    }
                )
            )
        unresolved = list(draft.unresolved)
        if dropped:
            detail = "runtime filtered dangling or missing targets: " + ", ".join(dropped)
            unresolved.append(detail[:160])
        if unchanged:
            detail = "runtime filtered unchanged state targets: " + ", ".join(unchanged)
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

    @classmethod
    def _merge_normalized_collisions(
        cls,
        draft: ChapterChangeDraft,
        base_commit: CommitId,
    ) -> tuple[ChapterChangeDraft, tuple[ProposalEvidenceMergeReceipt, ...]]:
        groups: dict[
            tuple[WorldRecordKind, StableId],
            list[tuple[int, CuratedOperationDraft]],
        ] = {}
        for index, operation in enumerate(draft.operations):
            groups.setdefault((operation.record_kind, operation.target_id), []).append(
                (index, operation)
            )

        merged: list[CuratedOperationDraft] = []
        receipts: list[ProposalEvidenceMergeReceipt] = []
        for (record_kind, target_id), indexed in groups.items():
            if len(indexed) == 1:
                merged.append(indexed[0][1])
                continue
            semantic_payloads = tuple(
                canonical_json_bytes(operation.model_dump(mode="json", exclude={"evidence_refs"}))
                for _, operation in indexed
            )
            semantic_hashes = tuple(sha256_id(payload) for payload in semantic_payloads)
            evidence_payloads = tuple(
                canonical_json_bytes(evidence.model_dump(mode="json"))
                for _, operation in indexed
                for evidence in operation.evidence_refs
            )
            evidence_hashes = tuple(sha256_id(payload) for payload in evidence_payloads)
            if len(set(semantic_hashes)) != 1:
                raise CuratorProposalSemanticRejected(
                    "CURATOR_PROPOSAL_NORMALIZED_TARGET_COLLISION",
                    (
                        ProposalConflict(
                            record_kind=record_kind,
                            target_id=target_id,
                            operation_indexes=tuple(index for index, _ in indexed),
                            semantic_hashes=tuple(
                                sorted(set(semantic_hashes), key=lambda item: item.root)
                            ),
                            evidence_hashes=tuple(
                                sorted(set(evidence_hashes), key=lambda item: item.root)
                            ),
                        ),
                    ),
                )
            unique_evidence = {
                canonical_json_bytes(evidence.model_dump(mode="json")): evidence
                for _, operation in indexed
                for evidence in operation.evidence_refs
            }
            ordered_evidence = tuple(
                unique_evidence[payload] for payload in sorted(unique_evidence)
            )
            merged.append(indexed[0][1].model_copy(update={"evidence_refs": ordered_evidence}))
            source_hashes = tuple(
                sha256_id(canonical_json_bytes(operation.model_dump(mode="json")))
                for _, operation in indexed
            )
            digest = cls._digest(
                base_commit.root.encode(),
                record_kind.value.encode(),
                target_id.root.encode(),
                semantic_hashes[0].root.encode(),
            )
            receipts.append(
                ProposalEvidenceMergeReceipt(
                    transform_id=StableId(f"proposal-evidence-merge.{digest}"),
                    base_commit=base_commit,
                    record_kind=record_kind,
                    target_id=target_id,
                    semantic_hash=semantic_hashes[0],
                    source_operation_hashes=source_hashes,
                    merged_evidence_hashes=tuple(
                        sorted(set(evidence_hashes), key=lambda item: item.root)
                    ),
                )
            )
        return (
            draft.model_copy(update={"operations": tuple(merged)}),
            tuple(receipts),
        )

    @staticmethod
    def _digest(*parts: bytes) -> str:
        return hashlib.sha256(b"\0".join(parts)).hexdigest()[:24]
