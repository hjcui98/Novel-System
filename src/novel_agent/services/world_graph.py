"""Evidence-backed World relation repair using the existing canonical World owner."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import (
    ChangeOperation,
    ChangeOperationType,
    ObservedChangeSet,
)
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextBlock
from novel_agent.domain.world import (
    Entity,
    EntityAdmissionReceipt,
    EntityAdmissionStatus,
    EntityAliasResolutionReceipt,
    EntityResolutionStatus,
    GraphCandidateSupportStatus,
    RelationBackfillReceipt,
    RelationBackfillStatus,
    RelationRecord,
    TruthClass,
    WorldGraphCandidateBatch,
    WorldGraphEntityCandidate,
    WorldGraphExtractionReceipt,
    WorldGraphRelationCandidate,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes, quote_hash
from novel_agent.services.overlay import WorldOverlay


class PredicateMultiplicity(StrEnum):
    MANY = "many"
    ONE_OBJECT_PER_SUBJECT = "one_object_per_subject"


class StateRelationDirection(StrEnum):
    SUBJECT_TO_OBJECT = "subject_to_object"
    OBJECT_TO_SUBJECT = "object_to_subject"


@dataclass(frozen=True, slots=True)
class PredicateDefinition:
    predicate: str
    current_caller: str
    owner_layer: str
    protected_invariant: str
    acceptance_evidence: str
    inverse_predicate: str
    temporal_validity: str
    multiplicity: PredicateMultiplicity
    allowed_subject_types: tuple[str, ...]
    allowed_object_types: tuple[str, ...]
    example_evidence_ref: str


@dataclass(frozen=True, slots=True)
class StateRelationRule:
    state_predicate: str
    relation_predicate: str
    direction: StateRelationDirection


class PredicateRegistry:
    """Small Stage 2M relation vocabulary; not a general ontology."""

    VERSION = "stage2m-predicate-registry.v1"
    _ANY_TYPES = ("*",)
    _CHARACTER_TYPES = ("character", "person")
    _PLACE_TYPES = ("location", "organization", "setting")
    _OBJECT_TYPES = ("artifact", "item", "object", "location", "organization")

    def __init__(self) -> None:
        any_types = self._ANY_TYPES
        characters = self._CHARACTER_TYPES
        places = self._PLACE_TYPES
        objects = self._OBJECT_TYPES
        self._definitions = {
            item.predicate: item
            for item in (
                self._definition("affiliated_with", "has_affiliate", any_types, any_types),
                self._definition("member_of", "has_member", characters, places),
                self._definition("enrolled_in", "has_enrollee", characters, places),
                self._definition("mentor_of", "mentored_by", characters, characters),
                self._definition("teacher_of", "student_of", characters, characters),
                self._definition("travels_with", "travels_with", characters, characters),
                self._definition("protects", "protected_by", characters, any_types),
                self._definition("opposes", "opposed_by", any_types, any_types),
                self._definition("possesses", "possessed_by", any_types, objects),
                self._definition("owns", "owned_by", any_types, objects),
                self._definition("transfers_to", "receives_from", any_types, any_types),
                self._definition("knows_about", "known_by", characters, any_types),
                self._definition("hides_from", "is_hidden_from", any_types, any_types),
                self._definition("discloses_to", "receives_disclosure_from", any_types, any_types),
                self._definition("promised_to", "received_promise_from", characters, any_types),
                self._definition("owes", "is_owed_by", any_types, any_types),
                self._definition("located_at", "contains", any_types, places),
                self._definition("resides_at", "residence_of", characters, places),
            )
        }
        self._state_rules = {
            rule.state_predicate: rule
            for rule in (
                StateRelationRule(
                    "affiliation", "affiliated_with", StateRelationDirection.SUBJECT_TO_OBJECT
                ),
                StateRelationRule(
                    "member_of", "member_of", StateRelationDirection.SUBJECT_TO_OBJECT
                ),
                StateRelationRule(
                    "enrollment_status", "enrolled_in", StateRelationDirection.SUBJECT_TO_OBJECT
                ),
                StateRelationRule("mentor", "mentor_of", StateRelationDirection.OBJECT_TO_SUBJECT),
                StateRelationRule(
                    "teacher", "teacher_of", StateRelationDirection.OBJECT_TO_SUBJECT
                ),
                StateRelationRule(
                    "teaches", "teacher_of", StateRelationDirection.SUBJECT_TO_OBJECT
                ),
                StateRelationRule(
                    "travel_companion", "travels_with", StateRelationDirection.SUBJECT_TO_OBJECT
                ),
                StateRelationRule("protects", "protects", StateRelationDirection.SUBJECT_TO_OBJECT),
                StateRelationRule("opposes", "opposes", StateRelationDirection.SUBJECT_TO_OBJECT),
                StateRelationRule(
                    "possesses", "possesses", StateRelationDirection.SUBJECT_TO_OBJECT
                ),
                StateRelationRule("owns", "owns", StateRelationDirection.SUBJECT_TO_OBJECT),
                StateRelationRule("is_owner_of", "owns", StateRelationDirection.SUBJECT_TO_OBJECT),
                StateRelationRule(
                    "transfers_to", "transfers_to", StateRelationDirection.SUBJECT_TO_OBJECT
                ),
                StateRelationRule(
                    "knows_about", "knows_about", StateRelationDirection.SUBJECT_TO_OBJECT
                ),
                StateRelationRule(
                    "hides_from", "hides_from", StateRelationDirection.SUBJECT_TO_OBJECT
                ),
                StateRelationRule(
                    "discloses_to", "discloses_to", StateRelationDirection.SUBJECT_TO_OBJECT
                ),
                StateRelationRule(
                    "promised_to", "promised_to", StateRelationDirection.SUBJECT_TO_OBJECT
                ),
                StateRelationRule("owes", "owes", StateRelationDirection.SUBJECT_TO_OBJECT),
                StateRelationRule(
                    "location", "located_at", StateRelationDirection.SUBJECT_TO_OBJECT
                ),
                StateRelationRule(
                    "residence", "resides_at", StateRelationDirection.SUBJECT_TO_OBJECT
                ),
            )
        }

    @classmethod
    def _definition(
        cls,
        predicate: str,
        inverse: str,
        subject_types: tuple[str, ...],
        object_types: tuple[str, ...],
    ) -> PredicateDefinition:
        return PredicateDefinition(
            predicate=predicate,
            current_caller="WorldGraphExtractionPass",
            owner_layer="services/memory_curation",
            protected_invariant="only evidence-backed canonical entity edges enter WorldRoot",
            acceptance_evidence="accepted RelationBackfillReceipt and concrete EvidenceRef",
            inverse_predicate=inverse,
            temporal_validity="StoryTime interval",
            multiplicity=PredicateMultiplicity.MANY,
            allowed_subject_types=subject_types,
            allowed_object_types=object_types,
            example_evidence_ref="RelationBackfillReceipt.evidence_refs[0]",
        )

    @property
    def definitions(self) -> tuple[PredicateDefinition, ...]:
        return tuple(self._definitions.values())

    def state_rule(self, state_predicate: str) -> StateRelationRule | None:
        return self._state_rules.get(state_predicate)

    def require(self, predicate: str) -> PredicateDefinition:
        try:
            return self._definitions[predicate]
        except KeyError as error:
            raise ValueError(f"predicate is not registered: {predicate}") from error

    def validate_entity_types(
        self,
        predicate: str,
        subject_type: str,
        object_type: str,
    ) -> None:
        definition = self.require(predicate)
        if not self._type_allowed(subject_type, definition.allowed_subject_types):
            raise ValueError(f"predicate {predicate} rejects subject type {subject_type}")
        if not self._type_allowed(object_type, definition.allowed_object_types):
            raise ValueError(f"predicate {predicate} rejects object type {object_type}")

    @staticmethod
    def _type_allowed(entity_type: str, allowed: tuple[str, ...]) -> bool:
        return "*" in allowed or entity_type in allowed


class EntityAliasRepairPolicy:
    """Resolve exact labels before aliases and expose collisions as typed receipts."""

    VERSION = "stage2m-entity-alias-policy.v1"

    def resolve(
        self,
        world: WorldRootDocument,
        mention: str,
        *,
        evidence_refs: tuple[EvidenceRef, ...] = (),
    ) -> EntityAliasResolutionReceipt:
        mention = mention.strip()
        if not mention:
            raise ValueError("entity mention must be non-empty")
        normalized = self._normalize(mention)
        label_matches = tuple(
            entity.entity_id
            for entity in world.entities
            if self._normalize(entity.internal_label) == normalized
        )
        alias_matches = tuple(
            entity.entity_id
            for entity in world.entities
            if any(self._normalize(alias) == normalized for alias in entity.aliases)
        )
        matches: tuple[StableId, ...]
        resolved: StableId | None
        reason: str | None
        if len(label_matches) == 1:
            status = EntityResolutionStatus.UNIQUE_LABEL
            matches = label_matches
            resolved = label_matches[0]
            basis = "exact_internal_label"
            reason = None
        elif len(label_matches) > 1:
            status = EntityResolutionStatus.AMBIGUOUS
            matches = label_matches
            resolved = None
            basis = "canonical_label_collision"
            reason = "exact canonical label collision"
        elif len(alias_matches) == 1:
            status = EntityResolutionStatus.UNIQUE_ALIAS
            matches = alias_matches
            resolved = alias_matches[0]
            basis = "exact_alias"
            reason = None
        elif alias_matches:
            status = EntityResolutionStatus.AMBIGUOUS
            matches = alias_matches
            resolved = None
            basis = "alias_collision"
            reason = "exact alias resolves to multiple entities"
        else:
            status = EntityResolutionStatus.MISSING
            matches = ()
            resolved = None
            basis = "no_exact_match"
            reason = "entity label or alias is missing"
        return EntityAliasResolutionReceipt(
            receipt_id=StableId(
                "alias-receipt."
                + self._digest(
                    world.root_hash.root,
                    mention,
                    *(evidence.evidence_id.root for evidence in evidence_refs),
                )
            ),
            mention=mention,
            status=status,
            matched_entity_ids=matches,
            resolved_entity_id=resolved,
            match_basis=basis,
            evidence_refs=evidence_refs,
            reason=reason,
        )

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.casefold().split())

    @staticmethod
    def _digest(*parts: str) -> str:
        return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class WorldGraphExtractionResult:
    repaired_world: WorldRootDocument
    receipt: WorldGraphExtractionReceipt
    change_set: ObservedChangeSet
    candidate_batches: tuple[WorldGraphCandidateBatch, ...]


class WorldGraphExtractionPass:
    """Admit bounded graph candidates through the existing World mutation contract."""

    POLICY_VERSION = "stage2m-world-graph-admission.v3"

    def __init__(
        self,
        registry: PredicateRegistry | None = None,
        alias_policy: EntityAliasRepairPolicy | None = None,
    ) -> None:
        self._registry = registry or PredicateRegistry()
        self._alias_policy = alias_policy or EntityAliasRepairPolicy()

    def run(
        self,
        world: WorldRootDocument,
        text: TextRootDocument,
        *,
        candidate_batches: tuple[WorldGraphCandidateBatch, ...] = (),
        base_commit: CommitId | None = None,
    ) -> WorldGraphExtractionResult:
        basis = base_commit or world.source_commit
        blocks = self._blocks(text)
        state_batch = self._state_candidate_batch(world, text, basis)
        batches = (*((state_batch,) if state_batch.relations else ()), *candidate_batches)
        for batch in batches:
            if batch.source_text_root != text.root_hash or batch.base_commit != basis:
                raise ValueError("graph candidate batch differs from the repair basis")

        entity_operations: list[ChangeOperation] = []
        relation_operations: list[ChangeOperation] = []
        entity_receipts: list[EntityAdmissionReceipt] = []
        relation_receipts: list[RelationBackfillReceipt] = []
        admitted_entities = list(world.entities)
        entity_candidate_map: dict[tuple[StableId, str], StableId] = {}

        for batch in batches:
            for candidate in batch.entities:
                entity_receipt, entity = self._admit_entity_candidate(
                    candidate,
                    world.model_copy(update={"entities": tuple(admitted_entities)}),
                    blocks,
                )
                entity_receipts.append(entity_receipt)
                if entity_receipt.entity_id is not None:
                    entity_candidate_map[(batch.batch_id, self._normalize(candidate.surface))] = (
                        entity_receipt.entity_id
                    )
                if entity is not None:
                    admitted_entities.append(entity)
                    entity_operations.append(self._entity_operation(entity, candidate, basis))

        augmented_world = world.model_copy(update={"entities": tuple(admitted_entities)})
        relations_by_key = {
            self._relation_key(
                relation.predicate,
                relation.subject_id,
                relation.object_id,
                relation.valid_time.worldline,
                relation.valid_time.start_ordinal,
                relation.valid_time.end_ordinal,
            ): relation
            for relation in world.relations
        }
        for batch in batches:
            for candidate in batch.relations:
                relation_receipt, relation = self._admit_relation_candidate(
                    candidate,
                    augmented_world,
                    blocks,
                    relations_by_key,
                    entity_candidate_map,
                )
                relation_receipts.append(relation_receipt)
                if relation is not None:
                    relations_by_key[
                        self._relation_key(
                            relation.predicate,
                            relation.subject_id,
                            relation.object_id,
                            relation.valid_time.worldline,
                            relation.valid_time.start_ordinal,
                            relation.valid_time.end_ordinal,
                        )
                    ] = relation
                    relation_operations.append(self._relation_operation(relation, candidate, basis))

        source_payload = canonical_json_bytes([batch.model_dump(mode="json") for batch in batches])
        operations = (*entity_operations, *relation_operations)
        change_set = ObservedChangeSet(
            change_set_id=StableId(
                "changes.world-graph."
                + self._digest(
                    basis.root,
                    text.root_hash.root,
                    *(operation.operation_id.root for operation in operations),
                )
            ),
            base_commit=basis,
            source_artifact=ArtifactRef(
                artifact_id=sha256_id(source_payload),
                media_type="application/vnd.novel-agent.world-graph-candidates+json",
                byte_length=len(source_payload),
                schema_version=SchemaVersion("1.0.0"),
            ),
            operations=operations,
        )
        repaired = WorldOverlay().apply(world, change_set, canonical_commit=basis)
        accepted_ids = tuple(
            candidate.relation_id
            for candidate in relation_receipts
            if candidate.status is RelationBackfillStatus.ACCEPTED
            and candidate.relation_id is not None
        )
        statuses = tuple(receipt.status.value for receipt in entity_receipts) + tuple(
            receipt.status.value for receipt in relation_receipts
        )
        extraction_receipt = WorldGraphExtractionReceipt(
            receipt_id=StableId(
                "world-graph-extraction."
                + self._digest(world.root_hash.root, repaired.root_hash.root, basis.root)
            ),
            source_world_root=world.root_hash,
            repaired_world_root=repaired.root_hash,
            predicate_registry_version=self._registry.VERSION,
            alias_policy_version=self._alias_policy.VERSION,
            source_batch_ids=tuple(batch.batch_id for batch in batches),
            entity_admissions=tuple(entity_receipts),
            candidates=tuple(relation_receipts),
            accepted_relation_ids=accepted_ids,
            retained_state_ids=tuple(state.state_id for state in world.states),
            accepted_count=sum(status in {"accepted", "created", "reused"} for status in statuses),
            rejected_count=statuses.count("rejected"),
            deduped_count=statuses.count("deduped"),
        )
        return WorldGraphExtractionResult(
            repaired_world=repaired,
            receipt=extraction_receipt,
            change_set=change_set,
            candidate_batches=tuple(batches),
        )

    def _state_candidate_batch(
        self,
        world: WorldRootDocument,
        text: TextRootDocument,
        basis: CommitId,
    ) -> WorldGraphCandidateBatch:
        batch_id = StableId(
            "graph-batch.state-audit."
            + self._digest(world.root_hash.root, text.root_hash.root, basis.root)
        )
        entities = {entity.entity_id: entity for entity in world.entities}
        blocks = self._blocks(text)
        candidates: list[WorldGraphRelationCandidate] = []
        for state in world.states:
            rule = self._registry.state_rule(state.predicate)
            if rule is None or not isinstance(state.value, str) or not state.value.strip():
                continue
            try:
                evidence_text = self._validate_evidence(state.evidence_refs, blocks)
            except ValueError:
                evidence_text = ()
            value_surface = state.value.strip()
            value_resolution = self._alias_policy.resolve(
                world,
                value_surface,
                evidence_refs=state.evidence_refs,
            )
            value_entity = (
                entities.get(value_resolution.resolved_entity_id)
                if value_resolution.resolved_entity_id is not None
                else None
            )
            state_surface = self._evidence_surface(
                world,
                entities[state.subject_id],
                evidence_text,
            )
            resolved_value_surface = (
                self._evidence_surface(world, value_entity, evidence_text)
                if value_entity is not None
                else value_surface
            )
            candidate_id = StableId(
                f"relation-candidate.{self._digest(world.root_hash.root, state.state_id.root)}"
            )
            if rule.direction is StateRelationDirection.SUBJECT_TO_OBJECT:
                subject_surface, object_surface = state_surface, resolved_value_surface
            else:
                subject_surface, object_surface = resolved_value_surface, state_surface
            candidates.append(
                WorldGraphRelationCandidate(
                    candidate_id=candidate_id,
                    source_batch_id=batch_id,
                    source_state_id=state.state_id,
                    subject_surface=subject_surface,
                    predicate=rule.relation_predicate,
                    object_surface=object_surface,
                    valid_time=state.valid_time,
                    evidence_refs=state.evidence_refs,
                    source_truth_class=state.truth_class,
                    support_status=GraphCandidateSupportStatus.SUPPORTED,
                    support_reason="canonical_relation_like_state",
                )
            )
        return WorldGraphCandidateBatch(
            batch_id=batch_id,
            source_text_root=text.root_hash,
            base_commit=basis,
            evidence_candidate_ids=tuple(
                dict.fromkeys(
                    evidence.evidence_id
                    for candidate in candidates
                    for evidence in candidate.evidence_refs
                )
            ),
            policy_version=self.POLICY_VERSION,
            relations=tuple(candidates),
        )

    def _evidence_surface(
        self,
        world: WorldRootDocument,
        entity: Entity,
        evidence_text: tuple[str, ...],
    ) -> str:
        for mention in (entity.internal_label, *entity.aliases):
            if not mention or not any(mention in selected for selected in evidence_text):
                continue
            resolution = self._alias_policy.resolve(world, mention)
            if resolution.resolved_entity_id == entity.entity_id:
                return mention
        return entity.internal_label

    def _admit_entity_candidate(
        self,
        candidate: WorldGraphEntityCandidate,
        world: WorldRootDocument,
        blocks: dict[StableId, TextBlock],
    ) -> tuple[EntityAdmissionReceipt, Entity | None]:
        resolution = self._alias_policy.resolve(
            world, candidate.surface, evidence_refs=candidate.evidence_refs
        )
        if candidate.support_status is GraphCandidateSupportStatus.REJECTED:
            return self._entity_rejected(candidate, resolution, candidate.support_reason), None
        try:
            evidence_text = self._validate_evidence(candidate.evidence_refs, blocks)
            if not any(candidate.surface in selected for selected in evidence_text):
                raise ValueError("entity surface is absent from its exact evidence")
        except ValueError as error:
            return self._entity_rejected(candidate, resolution, str(error)), None
        if resolution.resolved_entity_id is not None:
            existing = next(
                item for item in world.entities if item.entity_id == resolution.resolved_entity_id
            )
            if existing.entity_type != candidate.entity_type:
                return self._entity_rejected(
                    candidate, resolution, "entity_type_conflicts_with_canonical_entity"
                ), None
            return (
                EntityAdmissionReceipt(
                    candidate_id=candidate.candidate_id,
                    source_batch_id=candidate.source_batch_id,
                    surface=candidate.surface,
                    entity_type=candidate.entity_type,
                    status=EntityAdmissionStatus.REUSED,
                    entity_id=existing.entity_id,
                    evidence_refs=candidate.evidence_refs,
                    resolution=resolution,
                ),
                None,
            )
        if resolution.status is EntityResolutionStatus.AMBIGUOUS:
            return self._entity_rejected(
                candidate, resolution, resolution.reason or "ambiguous"
            ), None
        entity_id = self._entity_id(candidate.surface, candidate.entity_type)
        if any(entity.entity_id == entity_id for entity in world.entities):
            existing = next(entity for entity in world.entities if entity.entity_id == entity_id)
            if (
                self._normalize(existing.internal_label) != self._normalize(candidate.surface)
                or existing.entity_type != candidate.entity_type
            ):
                return self._entity_rejected(
                    candidate, resolution, "stable_entity_id_collision"
                ), None
            return (
                EntityAdmissionReceipt(
                    candidate_id=candidate.candidate_id,
                    source_batch_id=candidate.source_batch_id,
                    surface=candidate.surface,
                    entity_type=candidate.entity_type,
                    status=EntityAdmissionStatus.DEDUPED,
                    entity_id=entity_id,
                    evidence_refs=candidate.evidence_refs,
                    resolution=resolution,
                ),
                None,
            )
        entity = Entity(
            entity_id=entity_id,
            entity_type=candidate.entity_type,
            internal_label=candidate.surface,
        )
        return (
            EntityAdmissionReceipt(
                candidate_id=candidate.candidate_id,
                source_batch_id=candidate.source_batch_id,
                surface=candidate.surface,
                entity_type=candidate.entity_type,
                status=EntityAdmissionStatus.CREATED,
                entity_id=entity_id,
                evidence_refs=candidate.evidence_refs,
                resolution=resolution,
            ),
            entity,
        )

    def _admit_relation_candidate(
        self,
        candidate: WorldGraphRelationCandidate,
        world: WorldRootDocument,
        blocks: dict[StableId, TextBlock],
        relations_by_key: dict[tuple[str, str, str, str, str, str], RelationRecord],
        entity_candidate_map: dict[tuple[StableId, str], StableId],
    ) -> tuple[RelationBackfillReceipt, RelationRecord | None]:
        subject_resolution = self._alias_policy.resolve(
            world, candidate.subject_surface, evidence_refs=candidate.evidence_refs
        )
        object_resolution = self._alias_policy.resolve(
            world, candidate.object_surface, evidence_refs=candidate.evidence_refs
        )
        subject_id = subject_resolution.resolved_entity_id or entity_candidate_map.get(
            (candidate.source_batch_id, self._normalize(candidate.subject_surface))
        )
        object_id = object_resolution.resolved_entity_id or entity_candidate_map.get(
            (candidate.source_batch_id, self._normalize(candidate.object_surface))
        )
        reason: str | None = None
        if candidate.support_status is GraphCandidateSupportStatus.REJECTED:
            reason = candidate.support_reason
        elif candidate.source_truth_class not in {
            TruthClass.ACCEPTED_WORLD_FACT,
            TruthClass.ASSERTION,
        }:
            reason = f"truth_class_not_admitted:{candidate.source_truth_class.value}"
        elif subject_id is None:
            reason = f"subject_entity_{subject_resolution.status.value}"
        elif object_id is None:
            reason = f"object_entity_{object_resolution.status.value}"
        elif subject_id == object_id:
            reason = "self_relation_not_admitted"
        try:
            self._registry.require(candidate.predicate)
            evidence_text = self._validate_evidence(candidate.evidence_refs, blocks)
            if not any(candidate.subject_surface in selected for selected in evidence_text):
                raise ValueError("relation subject surface is absent from exact evidence")
            if not any(candidate.object_surface in selected for selected in evidence_text):
                raise ValueError("relation object surface is absent from exact evidence")
            if subject_id is not None and object_id is not None:
                entities = {entity.entity_id: entity for entity in world.entities}
                self._registry.validate_entity_types(
                    candidate.predicate,
                    entities[subject_id].entity_type,
                    entities[object_id].entity_type,
                )
        except (KeyError, ValueError) as error:
            reason = reason or str(error)
        if reason is not None:
            return (
                self._relation_receipt(
                    candidate,
                    RelationBackfillStatus.REJECTED,
                    subject_resolution,
                    object_resolution,
                    subject_id=subject_id,
                    object_id=object_id,
                    rejection_reason=reason,
                ),
                None,
            )
        assert subject_id is not None and object_id is not None
        key = self._relation_key(
            candidate.predicate,
            subject_id,
            object_id,
            candidate.valid_time.worldline,
            candidate.valid_time.start_ordinal,
            candidate.valid_time.end_ordinal,
        )
        existing = relations_by_key.get(key)
        if existing is not None:
            return (
                self._relation_receipt(
                    candidate,
                    RelationBackfillStatus.DEDUPED,
                    subject_resolution,
                    object_resolution,
                    subject_id=subject_id,
                    object_id=object_id,
                    relation_id=existing.relation_id,
                ),
                None,
            )
        definition = self._registry.require(candidate.predicate)
        if definition.multiplicity is PredicateMultiplicity.ONE_OBJECT_PER_SUBJECT and any(
            relation.predicate == candidate.predicate
            and relation.subject_id == subject_id
            and relation.object_id != object_id
            for relation in relations_by_key.values()
        ):
            return (
                self._relation_receipt(
                    candidate,
                    RelationBackfillStatus.REJECTED,
                    subject_resolution,
                    object_resolution,
                    subject_id=subject_id,
                    object_id=object_id,
                    rejection_reason="predicate_multiplicity_conflict",
                ),
                None,
            )
        relation_id = StableId("relation.graph." + self._digest(*key))
        relation = RelationRecord(
            relation_id=relation_id,
            predicate=candidate.predicate,
            subject_id=subject_id,
            object_id=object_id,
            valid_time=candidate.valid_time,
            evidence_refs=candidate.evidence_refs,
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        )
        return (
            self._relation_receipt(
                candidate,
                RelationBackfillStatus.ACCEPTED,
                subject_resolution,
                object_resolution,
                subject_id=subject_id,
                object_id=object_id,
                relation_id=relation_id,
            ),
            relation,
        )

    @staticmethod
    def _relation_receipt(
        candidate: WorldGraphRelationCandidate,
        status: RelationBackfillStatus,
        subject_resolution: EntityAliasResolutionReceipt,
        object_resolution: EntityAliasResolutionReceipt,
        *,
        subject_id: StableId | None = None,
        object_id: StableId | None = None,
        relation_id: StableId | None = None,
        rejection_reason: str | None = None,
    ) -> RelationBackfillReceipt:
        return RelationBackfillReceipt(
            candidate_id=candidate.candidate_id,
            source_batch_id=candidate.source_batch_id,
            source_state_id=candidate.source_state_id,
            source_truth_class=candidate.source_truth_class,
            status=status,
            predicate=candidate.predicate,
            subject_surface=candidate.subject_surface,
            object_surface=candidate.object_surface,
            subject_id=subject_id,
            object_id=object_id,
            relation_id=relation_id,
            evidence_refs=candidate.evidence_refs,
            subject_resolution=subject_resolution,
            object_resolution=object_resolution,
            rejection_reason=rejection_reason,
        )

    @staticmethod
    def _entity_rejected(
        candidate: WorldGraphEntityCandidate,
        resolution: EntityAliasResolutionReceipt,
        reason: str,
    ) -> EntityAdmissionReceipt:
        return EntityAdmissionReceipt(
            candidate_id=candidate.candidate_id,
            source_batch_id=candidate.source_batch_id,
            surface=candidate.surface,
            entity_type=candidate.entity_type,
            status=EntityAdmissionStatus.REJECTED,
            evidence_refs=candidate.evidence_refs,
            resolution=resolution,
            rejection_reason=reason,
        )

    @staticmethod
    def _entity_operation(
        entity: Entity,
        candidate: WorldGraphEntityCandidate,
        basis: CommitId,
    ) -> ChangeOperation:
        payload = entity.model_dump(mode="json")
        digest = WorldGraphExtractionPass._digest(
            basis.root, candidate.candidate_id.root, entity.entity_id.root
        )
        return ChangeOperation(
            operation_id=StableId(f"change.world-graph.entity.{digest}"),
            root_kind=RootKind.WORLD,
            operation=ChangeOperationType.CREATE,
            target_id=entity.entity_id,
            payload={"record_type": "entity", "record": payload},
            evidence_refs=candidate.evidence_refs,
        )

    @staticmethod
    def _relation_operation(
        relation: RelationRecord,
        candidate: WorldGraphRelationCandidate,
        basis: CommitId,
    ) -> ChangeOperation:
        payload = relation.model_dump(mode="json")
        digest = WorldGraphExtractionPass._digest(
            basis.root, candidate.candidate_id.root, relation.relation_id.root
        )
        return ChangeOperation(
            operation_id=StableId(f"change.world-graph.relation.{digest}"),
            root_kind=RootKind.WORLD,
            operation=ChangeOperationType.CREATE,
            target_id=relation.relation_id,
            payload={"record_type": "relation", "record": payload},
            evidence_refs=candidate.evidence_refs,
        )

    @staticmethod
    def _blocks(text: TextRootDocument) -> dict[StableId, TextBlock]:
        return {
            block.block_id: block
            for scene in (
                *(text.prelude.scenes if text.prelude is not None else ()),
                *(scene for chapter in text.chapters for scene in chapter.scenes),
            )
            for block in scene.blocks
        }

    @staticmethod
    def _validate_evidence(
        evidence_refs: tuple[EvidenceRef, ...],
        blocks: dict[StableId, TextBlock],
    ) -> tuple[str, ...]:
        if not evidence_refs:
            raise ValueError("relation-like state has no evidence")
        selected_text: list[str] = []
        for evidence in evidence_refs:
            if evidence.support_status not in {
                EvidenceSupportStatus.CURRENT,
                EvidenceSupportStatus.HISTORICAL,
            }:
                raise ValueError("relation-like state evidence is not current or historical")
            if evidence.span is None:
                raise ValueError("relation-like state evidence has no concrete span")
            block = blocks.get(evidence.span.block_id)
            if (
                block is None
                or evidence.span.end > len(block.text)
                or evidence.chapter_id != block.chapter_id
                or evidence.scene_id != block.scene_id
                or evidence.object_hash != sha256_id(block.text.encode("utf-8"))
            ):
                raise ValueError("relation-like state evidence does not resolve")
            selected = block.text[evidence.span.start : evidence.span.end]
            if evidence.quote_hash != quote_hash(selected):
                raise ValueError("relation-like state evidence quote hash does not match")
            selected_text.append(selected)
        return tuple(selected_text)

    @staticmethod
    def _entity_id(surface: str, entity_type: str) -> StableId:
        return StableId(
            "entity.graph."
            + WorldGraphExtractionPass._digest(
                WorldGraphExtractionPass._normalize(surface), entity_type
            )
        )

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.casefold().split())

    @staticmethod
    def _relation_key(
        predicate: str,
        subject_id: StableId,
        object_id: StableId,
        worldline: str,
        start: int | None,
        end: int | None,
    ) -> tuple[str, str, str, str, str, str]:
        return (
            predicate,
            subject_id.root,
            object_id.root,
            worldline,
            "" if start is None else str(start),
            "" if end is None else str(end),
        )

    @staticmethod
    def _digest(*parts: str) -> str:
        return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]


__all__ = [
    "EntityAliasRepairPolicy",
    "PredicateDefinition",
    "PredicateRegistry",
    "StateRelationDirection",
    "StateRelationRule",
    "WorldGraphExtractionPass",
    "WorldGraphExtractionResult",
]
