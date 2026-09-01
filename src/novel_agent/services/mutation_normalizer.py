"""Deterministic, proof-bounded candidate mutation normalization."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.changes import (
    ChangeOperation,
    ChangeOperationType,
    WorldRecordKind,
)
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.memory_write import (
    CandidateProducerKind,
    CandidateRevision,
    CanonicalWriteBasis,
    MemoryWriteCandidatePayload,
    NormalizationResult,
    NormalizationStatus,
    NormalizationTransformReceipt,
    RepairDirective,
    RepairScope,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes


class NormalizationAmbiguity(ValueError):
    """A candidate cannot be changed without guessing business meaning."""


class MutationNormalizer:
    """Apply only transformations that can be proven from the current basis.

    The normalizer is intentionally conservative.  An ambiguous candidate is
    returned unchanged and the repair policy decides whether to ask Curator,
    refresh evidence, quarantine, or stop.
    """

    def __init__(
        self,
        *,
        payload_loader: Callable[[ArtifactRef], MemoryWriteCandidatePayload] | None = None,
        artifact_writer: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._payload_loader = payload_loader
        self._artifact_writer = artifact_writer
        self._clock = clock or (lambda: datetime.now(UTC))

    def normalize(
        self,
        candidate: CandidateRevision,
        canonical: CanonicalWriteBasis,
        directive: RepairDirective | None = None,
    ) -> NormalizationResult:
        payload = self._load_payload(candidate)
        before_hash = _payload_hash(payload)
        operations = list(payload.observed_changes.operations)
        transforms: list[NormalizationTransformReceipt] = []
        ambiguous: list[str] = []

        world = canonical.canonical_world
        existing = _world_records(world)
        normalized: list[ChangeOperation] = []
        for operation in operations:
            if (
                directive is not None
                and directive.operation_ids
                and operation.operation_id not in directive.operation_ids
            ):
                normalized.append(operation)
                continue
            try:
                result, operation_transforms = self._normalize_operation(
                    operation,
                    existing,
                    directive.allowed_scope if directive is not None else None,
                )
                normalized.extend(result)
                transforms.extend(operation_transforms)
            except NormalizationAmbiguity as error:
                ambiguous.append(f"{operation.operation_id.root}:{error}")
                normalized.append(operation)

        normalized = self._merge_duplicate_operations(normalized, transforms)
        if ambiguous:
            return NormalizationResult(
                status=NormalizationStatus.AMBIGUOUS,
                candidate=candidate,
                transforms=tuple(transforms),
                reason_codes=("NORMALIZATION_AMBIGUOUS", *ambiguous),
            )
        if not transforms and normalized == operations:
            return NormalizationResult(
                status=NormalizationStatus.UNCHANGED,
                candidate=candidate,
            )

        observed = payload.observed_changes.model_copy(
            update={
                "operations": tuple(normalized),
                "change_set_id": StableId(f"changes.normalized.{_content_digest(normalized)[:32]}"),
            }
        )
        normalized_payload = payload.model_copy(update={"observed_changes": observed})
        after_hash = _payload_hash(normalized_payload)
        if after_hash == before_hash:
            return NormalizationResult(
                status=NormalizationStatus.UNCHANGED,
                candidate=candidate,
                transforms=tuple(transforms),
            )

        transform_artifact = self._put_model(
            tuple(item.model_dump(mode="json") for item in transforms),
            "application/vnd.novel-agent.normalization-receipt+json",
            candidate.candidate_artifact.schema_version,
        )
        child = _child_candidate(
            candidate,
            normalized_payload,
            producer_kind=CandidateProducerKind.DETERMINISTIC_NORMALIZER,
            producer_receipt=transform_artifact,
            repair_scope=None if directive is None else directive.allowed_scope,
            directive=None if directive is None else directive,
            artifact_writer=self._artifact_writer,
            now=self._clock(),
        )
        return NormalizationResult(
            status=NormalizationStatus.TRANSFORMED,
            candidate=child,
            transforms=tuple(transforms),
        )

    def _normalize_operation(
        self,
        operation: ChangeOperation,
        existing: dict[tuple[WorldRecordKind, StableId], dict[str, Any]],
        scope: RepairScope | None,
    ) -> tuple[list[ChangeOperation], list[NormalizationTransformReceipt]]:
        payload = operation.payload
        if not isinstance(payload, dict):
            return [operation], []
        raw_kind = payload.get("record_type")
        raw_record = payload.get("record")
        if not isinstance(raw_kind, str) or not isinstance(raw_record, dict):
            return [operation], []
        try:
            kind = WorldRecordKind(raw_kind)
        except ValueError:
            return [operation], []
        transforms: list[NormalizationTransformReceipt] = []
        key = (kind, operation.target_id)
        current = existing.get(key)

        if operation.operation is ChangeOperationType.CREATE and current is not None:
            if _same_business_record(current, raw_record, kind):
                transforms.append(
                    self._receipt(
                        operation,
                        "normalize.exact-duplicate-v1",
                        "exact duplicate CREATE removed as NOOP",
                        after={"result": "noop", "operation_id": operation.operation_id.root},
                    )
                )
                return [], transforms
            if _same_identity(current, raw_record, kind):
                replaced = operation.model_copy(update={"operation": ChangeOperationType.REPLACE})
                transforms.append(
                    self._receipt(
                        operation,
                        "normalize.create-to-replace-v1",
                        "existing business identity is updated by REPLACE",
                        after=replaced,
                    )
                )
                return [replaced], transforms
            raise NormalizationAmbiguity("CREATE target exists with another business identity")

        if operation.operation is ChangeOperationType.REPLACE and current is None:
            record_id = _record_id(raw_record, kind)
            if record_id != operation.target_id:
                raise NormalizationAmbiguity("missing REPLACE target does not prove a new record")
            transforms.append(
                self._receipt(
                    operation,
                    "normalize.replace-to-create-v1",
                    "missing target with self-consistent identity becomes CREATE",
                    after=operation.model_copy(update={"operation": ChangeOperationType.CREATE}),
                )
            )
            return [
                operation.model_copy(update={"operation": ChangeOperationType.CREATE})
            ], transforms

        if operation.operation is ChangeOperationType.REPLACE and current is not None:
            if kind is WorldRecordKind.STATE and not _same_state_identity(current, raw_record):
                if not _successor_proof(raw_record, payload, scope):
                    raise NormalizationAmbiguity("state identity mutation has no successor proof")
                return self._successor_operations(operation, current, raw_record, transforms)
            if not _same_identity(current, raw_record, kind):
                raise NormalizationAmbiguity("replacement payload changes the business identity")

        evidence = payload.get("record")
        raw_evidence = evidence.get("evidence_refs") if isinstance(evidence, dict) else None
        if (
            isinstance(evidence, dict)
            and isinstance(raw_evidence, list)
            and all(isinstance(item, dict) for item in raw_evidence)
        ):
            evidence_items = cast(list[dict[str, Any]], raw_evidence)
            canonical_evidence = sorted(
                evidence_items, key=lambda item: str(item.get("evidence_id", ""))
            )
            canonical_operation_evidence = tuple(
                sorted(
                    operation.evidence_refs,
                    key=lambda item: item.evidence_id.root,
                )
            )
            if (
                canonical_evidence != evidence_items
                or canonical_operation_evidence != operation.evidence_refs
            ):
                new_record: dict[str, Any] = dict(evidence)
                new_record["evidence_refs"] = canonical_evidence
                new_payload = dict(payload)
                new_payload["record"] = new_record
                normalized = operation.model_copy(
                    update={
                        "payload": new_payload,
                        "evidence_refs": canonical_operation_evidence,
                    }
                )
                transforms.append(
                    self._receipt(
                        operation,
                        "normalize.evidence-order-v1",
                        "EvidenceRef order normalized without changing spans",
                        after=normalized,
                    )
                )
                return [normalized], transforms
        return [operation], transforms

    def _successor_operations(
        self,
        operation: ChangeOperation,
        current: dict[str, Any],
        new_record: dict[str, Any],
        transforms: list[NormalizationTransformReceipt],
    ) -> tuple[list[ChangeOperation], list[NormalizationTransformReceipt]]:
        valid_time = new_record.get("valid_time")
        old_time = current.get("valid_time")
        if not isinstance(valid_time, dict) or not isinstance(old_time, dict):
            raise NormalizationAmbiguity("successor state lacks valid-time boundaries")
        new_start = valid_time.get("start_ordinal")
        old_start = old_time.get("start_ordinal")
        if (
            not isinstance(new_start, int)
            or not isinstance(old_start, int)
            or new_start <= old_start
        ):
            raise NormalizationAmbiguity("successor state does not advance effective time")
        successor_id = StableId(
            "state.successor."
            + hashlib.sha256(
                canonical_json_bytes(
                    {
                        "subject": new_record.get("subject_id"),
                        "predicate": new_record.get("predicate"),
                        "effective": new_start,
                        "evidence": new_record.get("evidence_refs", []),
                        "predecessor": operation.target_id.root,
                    }
                )
            ).hexdigest()[:40]
        )
        ended_old = dict(current)
        ended_time = dict(old_time)
        ended_time["end_ordinal"] = new_start - 1
        ended_old["valid_time"] = ended_time
        successor_record = dict(new_record)
        successor_record[_id_field(WorldRecordKind.STATE)] = successor_id.root
        old_payload: dict[str, Any] = {
            "record_type": WorldRecordKind.STATE.value,
            "record": ended_old,
        }
        new_payload: dict[str, Any] = {
            "record_type": WorldRecordKind.STATE.value,
            "record": successor_record,
        }
        old_operation = ChangeOperation(
            operation_id=StableId(f"{operation.operation_id.root}.end"),
            root_kind=RootKind.WORLD,
            operation=ChangeOperationType.REPLACE,
            target_id=operation.target_id,
            payload=old_payload,
            evidence_refs=operation.evidence_refs,
        )
        new_operation = ChangeOperation(
            operation_id=StableId(f"{operation.operation_id.root}.successor"),
            root_kind=RootKind.WORLD,
            operation=ChangeOperationType.CREATE,
            target_id=successor_id,
            payload=new_payload,
            evidence_refs=operation.evidence_refs,
        )
        transforms.append(
            self._receipt(
                operation,
                "normalize.state-successor-v1",
                "state identity mutation converted to a time-bounded successor",
                after=(old_operation, new_operation),
            )
        )
        return [old_operation, new_operation], transforms

    @staticmethod
    def _merge_duplicate_operations(
        operations: list[ChangeOperation],
        transforms: list[NormalizationTransformReceipt],
    ) -> list[ChangeOperation]:
        by_identity: dict[tuple[RootKind, StableId], ChangeOperation] = {}
        result: list[ChangeOperation] = []
        for operation in operations:
            key = (operation.root_kind, operation.target_id)
            previous = by_identity.get(key)
            if previous is None:
                by_identity[key] = operation
                result.append(operation)
                continue
            if previous.operation == operation.operation and previous.payload == operation.payload:
                transforms.append(
                    NormalizationTransformReceipt(
                        receipt_id=StableId(
                            f"normalization.duplicate.{operation.operation_id.root}"
                        ),
                        rule_id=StableId("normalize.duplicate-merge-v1"),
                        before_hash=sha256_id(
                            canonical_json_bytes(previous.model_dump(mode="json"))
                        ),
                        after_hash=sha256_id(
                            canonical_json_bytes(
                                {
                                    "merged": previous.model_dump(mode="json"),
                                    "removed_operation_id": operation.operation_id.root,
                                }
                            )
                        ),
                        affected_operation_ids=(previous.operation_id, operation.operation_id),
                        reason="identical duplicate operation merged",
                    )
                )
                continue
            # A pair of different writes to one identity is semantically
            # ambiguous; retain both so the deterministic validator can block it.
            result.append(operation)
        return result

    @staticmethod
    def _receipt(
        operation: ChangeOperation,
        rule: str,
        reason: str,
        *,
        after: Any,
    ) -> NormalizationTransformReceipt:
        rule_id = StableId(rule)
        before = sha256_id(canonical_json_bytes(operation.model_dump(mode="json")))
        after_hash = sha256_id(canonical_json_bytes(_jsonable(after)))
        return NormalizationTransformReceipt(
            receipt_id=StableId(f"normalization.{operation.operation_id.root}.{rule_id.root}"),
            rule_id=rule_id,
            before_hash=before,
            after_hash=after_hash,
            affected_operation_ids=(operation.operation_id,),
            reason=reason,
        )

    def _load_payload(self, candidate: CandidateRevision) -> MemoryWriteCandidatePayload:
        if self._payload_loader is None:
            raise NormalizationAmbiguity("candidate payload loader is not configured")
        try:
            return self._payload_loader(candidate.candidate_artifact)
        except Exception as error:
            raise NormalizationAmbiguity("candidate payload cannot be loaded") from error

    def _put_model(self, value: Any, media_type: str, version: SchemaVersion) -> ArtifactRef:
        data = canonical_json_bytes(
            value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        )
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


def _child_candidate(
    parent: CandidateRevision,
    payload: MemoryWriteCandidatePayload,
    *,
    producer_kind: CandidateProducerKind,
    producer_receipt: ArtifactRef | None,
    repair_scope: RepairScope | None,
    directive: RepairDirective | None,
    artifact_writer: Any | None,
    now: datetime,
) -> CandidateRevision:
    data = canonical_json_bytes(payload.model_dump(mode="json"))
    artifact = (
        artifact_writer.put(
            data,
            "application/vnd.novel-agent.memory-write-candidate+json",
            parent.candidate_artifact.schema_version,
        )
        if artifact_writer is not None
        else ArtifactRef(
            artifact_id=sha256_id(data),
            media_type="application/vnd.novel-agent.memory-write-candidate+json",
            byte_length=len(data),
            schema_version=parent.candidate_artifact.schema_version,
        )
    )
    content_hash = sha256_id(data)
    candidate_id = _child_candidate_id(parent, content_hash, payload)
    return CandidateRevision(
        candidate_id=candidate_id,
        parent_candidate_id=parent.candidate_id,
        revision_no=parent.revision_no + 1,
        base_commit=parent.base_commit,
        basis_hash=parent.basis_hash,
        candidate_artifact=artifact,
        source_artifacts=parent.source_artifacts,
        source_evidence_requirement=parent.source_evidence_requirement,
        producer_kind=producer_kind,
        producer_receipt=producer_receipt,
        repair_scope=repair_scope,
        applied_directive_ids=(
            *parent.applied_directive_ids,
            *(() if directive is None else (directive.directive_id,)),
        ),
        supersedes_candidate_id=parent.candidate_id,
        content_hash=content_hash,
        created_at=now,
    )


def _payload_hash(payload: MemoryWriteCandidatePayload) -> ArtifactId:
    return sha256_id(canonical_json_bytes(payload.model_dump(mode="json")))


def _child_candidate_id(
    parent: CandidateRevision,
    content_hash: ArtifactId,
    payload: MemoryWriteCandidatePayload,
) -> StableId:
    """Derive a bounded child identity without losing deterministic lineage."""

    revision = parent.revision_no + 1
    content_suffix = content_hash.root.removeprefix("sha256:")[:16]
    readable = f"candidate.{parent.candidate_id.root}.{revision}.{content_suffix}"
    if len(readable) <= 128:
        return StableId(readable)
    parent_hash = hashlib.sha256(parent.candidate_id.root.encode("utf-8")).hexdigest()[:32]
    payload_hash = hashlib.sha256(canonical_json_bytes(payload.model_dump(mode="json"))).hexdigest()
    return StableId(f"candidate.{parent_hash}.{revision}.{payload_hash[:32]}")


def _content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(_jsonable(value))).hexdigest()


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _world_records(
    world: WorldRootDocument | None,
) -> dict[tuple[WorldRecordKind, StableId], dict[str, Any]]:
    if world is None:
        return {}
    records: dict[tuple[WorldRecordKind, StableId], dict[str, Any]] = {}
    for kind, values in (
        (WorldRecordKind.ENTITY, world.entities),
        (WorldRecordKind.EVENT, world.events),
        (WorldRecordKind.STATE, world.states),
        (WorldRecordKind.RELATION, world.relations),
        (WorldRecordKind.OBLIGATION, world.obligations),
    ):
        for value in values:
            record = value.model_dump(mode="json")
            records[(kind, StableId(record[_id_field(kind)]))] = record
    return records


def _id_field(kind: WorldRecordKind) -> str:
    return {
        WorldRecordKind.ENTITY: "entity_id",
        WorldRecordKind.EVENT: "event_id",
        WorldRecordKind.STATE: "state_id",
        WorldRecordKind.RELATION: "relation_id",
        WorldRecordKind.OBLIGATION: "obligation_id",
    }[kind]


def _record_id(record: dict[str, Any], kind: WorldRecordKind) -> StableId | None:
    raw = record.get(_id_field(kind))
    return None if not isinstance(raw, str) else StableId(raw)


def _same_state_identity(current: dict[str, Any], new: dict[str, Any]) -> bool:
    return current.get("subject_id") == new.get("subject_id") and current.get(
        "predicate"
    ) == new.get("predicate")


def _same_identity(current: dict[str, Any], new: dict[str, Any], kind: WorldRecordKind) -> bool:
    if kind is WorldRecordKind.STATE:
        return _same_state_identity(current, new)
    if kind is WorldRecordKind.RELATION:
        return (
            current.get("predicate") == new.get("predicate")
            and current.get("subject_id") == new.get("subject_id")
            and current.get("object_id") == new.get("object_id")
        )
    if kind is WorldRecordKind.ENTITY:
        return current.get("entity_type") == new.get("entity_type")
    if kind is WorldRecordKind.EVENT:
        return current.get("event_type") == new.get("event_type")
    if kind is WorldRecordKind.OBLIGATION:
        return current.get("kind") == new.get("kind")
    return False


def _same_business_record(
    current: dict[str, Any], new: dict[str, Any], kind: WorldRecordKind
) -> bool:
    return _same_identity(current, new, kind) and _strip_volatile(current, kind) == _strip_volatile(
        new, kind
    )


def _strip_volatile(record: dict[str, Any], kind: WorldRecordKind) -> dict[str, Any]:
    result = dict(record)
    result.pop(_id_field(kind), None)
    result.pop("evidence_refs", None)
    return result


def _successor_proof(
    record: dict[str, Any], payload: dict[str, Any], scope: RepairScope | None
) -> bool:
    return bool(
        payload.get("successor_proof") is True
        or isinstance(payload.get("successor_proof"), dict)
        or (scope is not None and scope.allow_successor_creation)
        or record.get("predecessor_id") is not None
    )


__all__ = [
    "MutationNormalizer",
    "NormalizationAmbiguity",
]
