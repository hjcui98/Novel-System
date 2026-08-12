from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus
from novel_agent.domain.world import (
    Entity,
    EntityAdmissionReceipt,
    EntityAdmissionStatus,
    EntityAliasResolutionReceipt,
    EntityResolutionStatus,
    GraphCandidatePageDraft,
    GraphCandidatePageStatus,
    GraphCandidateSupportStatus,
    GraphRelationCandidateDraft,
    RelationBackfillReceipt,
    RelationBackfillStatus,
    StoryTime,
    TruthClass,
    WorldGraphEntityCandidate,
    WorldGraphExtractionReceipt,
    WorldGraphRelationCandidate,
)
from novel_agent.services.content_addressing import quote_hash
from novel_agent.services.world_graph import (
    EntityAliasRepairPolicy,
    PredicateMultiplicity,
    PredicateRegistry,
    WorldGraphExtractionPass,
)
from tests.unit.test_world_graph_repair import _teacher_world


def _entity_candidate(
    surface: str,
    *,
    entity_type: str = "character",
    supported: bool = True,
) -> WorldGraphEntityCandidate:
    world, _, _, _ = _teacher_world()
    return WorldGraphEntityCandidate(
        candidate_id=StableId("candidate.entity.edges"),
        source_batch_id=StableId("batch.edges"),
        surface=surface,
        entity_type=entity_type,
        evidence_refs=world.states[0].evidence_refs,
        support_status=(
            GraphCandidateSupportStatus.SUPPORTED
            if supported
            else GraphCandidateSupportStatus.REJECTED
        ),
        support_reason="test_support",
    )


def _relation_candidate(**updates: object) -> WorldGraphRelationCandidate:
    world, _, _, _ = _teacher_world()
    values: dict[str, object] = {
        "candidate_id": StableId("candidate.relation.edges"),
        "source_batch_id": StableId("batch.edges"),
        "subject_surface": "旧誓言",
        "predicate": "teacher_of",
        "object_surface": "林澈",
        "valid_time": StoryTime(worldline="main", start_ordinal=5),
        "evidence_refs": world.states[0].evidence_refs,
        "source_truth_class": TruthClass.ASSERTION,
        "support_status": GraphCandidateSupportStatus.SUPPORTED,
        "support_reason": "test_support",
    }
    values.update(updates)
    return WorldGraphRelationCandidate.model_validate(values)


def test_alias_policy_covers_label_collision_alias_unique_and_missing() -> None:
    world, _, _, _ = _teacher_world()
    collision = Entity(
        entity_id=StableId("entity.collision"),
        entity_type="character",
        internal_label="林澈",
        aliases=("唯一别名",),
    )
    expanded = world.model_copy(update={"entities": (*world.entities, collision)})
    policy = EntityAliasRepairPolicy()

    assert policy.resolve(expanded, "林澈").status is EntityResolutionStatus.AMBIGUOUS
    assert policy.resolve(expanded, "唯一别名").status is EntityResolutionStatus.UNIQUE_ALIAS
    assert policy.resolve(expanded, "不存在").status is EntityResolutionStatus.MISSING
    with pytest.raises(ValueError, match="non-empty"):
        policy.resolve(expanded, "  ")


def test_evidence_surface_skips_colliding_label_for_unique_alias() -> None:
    world, _, _, _ = _teacher_world()
    target = Entity(
        entity_id=StableId("entity.surface-target"),
        entity_type="character",
        internal_label="共享名",
        aliases=("唯一表面",),
    )
    collision = Entity(
        entity_id=StableId("entity.surface-collision"),
        entity_type="character",
        internal_label="共享名",
    )
    expanded = world.model_copy(update={"entities": (*world.entities, target, collision)})

    surface = WorldGraphExtractionPass()._evidence_surface(
        expanded,
        target,
        ("共享名与唯一表面都出现在证据中",),
    )

    assert surface == "唯一表面"


def test_graph_domain_contracts_reject_invalid_shapes_and_accounting() -> None:
    world, _, _, _ = _teacher_world()
    resolution = EntityAliasRepairPolicy().resolve(world, "不存在")
    evidence = world.states[0].evidence_refs
    with pytest.raises(ValidationError, match="at most 12"):
        GraphCandidatePageDraft(
            status=GraphCandidatePageStatus.COMPLETE,
            candidates=tuple(
                GraphRelationCandidateDraft(
                    subject_surface="subject",
                    predicate="teacher_of",
                    object_surface="object",
                    valid_time=StoryTime(worldline="main", start_ordinal=1),
                    source_truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                    evidence_quotes=("evidence quote",),
                )
                for _ in range(13)
            ),
        )
    with pytest.raises(ValidationError, match="cannot carry a no-op"):
        GraphCandidatePageDraft(
            status=GraphCandidatePageStatus.COMPLETE,
            candidates=(
                GraphRelationCandidateDraft(
                    subject_surface="subject",
                    predicate="teacher_of",
                    object_surface="object",
                    valid_time=StoryTime(worldline="main", start_ordinal=1),
                    source_truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                    evidence_quotes=("quote text",),
                ),
            ),
            no_graph_candidate_reason="not empty",
        )
    with pytest.raises(ValidationError, match="must be complete and carry a reason"):
        GraphCandidatePageDraft(status=GraphCandidatePageStatus.COMPLETE)
    for update, message in (
        ({"status": EntityAdmissionStatus.CREATED}, "requires an entity id"),
        (
            {
                "status": EntityAdmissionStatus.REUSED,
                "entity_id": world.entities[0].entity_id,
                "rejection_reason": "bad",
            },
            "cannot have a rejection reason",
        ),
        ({"status": EntityAdmissionStatus.REJECTED}, "requires a reason"),
    ):
        with pytest.raises(ValidationError, match=message):
            EntityAdmissionReceipt(
                candidate_id=StableId("candidate.entity.contract"),
                source_batch_id=StableId("batch.contract"),
                surface="不存在",
                entity_type="character",
                evidence_refs=evidence,
                resolution=resolution,
                **update,
            )
    deduped_values = {
        "candidate_id": StableId("candidate.relation.contract"),
        "source_batch_id": StableId("batch.contract"),
        "source_truth_class": TruthClass.ASSERTION,
        "status": RelationBackfillStatus.DEDUPED,
        "predicate": "teacher_of",
        "subject_surface": "旧誓言",
        "object_surface": "林澈",
        "evidence_refs": evidence,
    }
    with pytest.raises(ValidationError, match="requires canonical relation identity"):
        RelationBackfillReceipt.model_validate(deduped_values)
    with pytest.raises(ValidationError, match="cannot have a rejection reason"):
        RelationBackfillReceipt.model_validate(
            {
                **deduped_values,
                "subject_id": world.entities[-1].entity_id,
                "object_id": world.entities[0].entity_id,
                "relation_id": StableId("relation.contract"),
                "rejection_reason": "bad",
            }
        )
    base_receipt = {
        "receipt_id": StableId("receipt.contract"),
        "source_world_root": world.root_hash,
        "repaired_world_root": world.root_hash,
        "predicate_registry_version": "v1",
        "alias_policy_version": "v1",
        "accepted_count": 0,
        "rejected_count": 0,
        "deduped_count": 0,
    }
    for field in ("accepted_count", "rejected_count", "deduped_count"):
        with pytest.raises(ValidationError, match="accounting mismatch"):
            WorldGraphExtractionReceipt.model_validate({**base_receipt, field: 1})


def test_entity_admission_covers_fail_closed_and_identity_paths() -> None:
    world, text, _, _ = _teacher_world()
    service = WorldGraphExtractionPass()
    blocks = service._blocks(text)

    rejected, _ = service._admit_entity_candidate(
        _entity_candidate("林澈", supported=False), world, blocks
    )
    absent, _ = service._admit_entity_candidate(_entity_candidate("不存在"), world, blocks)
    conflict, _ = service._admit_entity_candidate(
        _entity_candidate("林澈", entity_type="location"), world, blocks
    )
    reused, _ = service._admit_entity_candidate(_entity_candidate("林澈"), world, blocks)
    duplicate_label = Entity(
        entity_id=StableId("entity.duplicate-label"),
        entity_type="character",
        internal_label="旧誓言",
    )
    ambiguous, _ = service._admit_entity_candidate(
        _entity_candidate("旧誓言"),
        world.model_copy(update={"entities": (*world.entities, duplicate_label)}),
        blocks,
    )

    assert rejected.status is EntityAdmissionStatus.REJECTED
    assert absent.status is EntityAdmissionStatus.REJECTED
    assert conflict.rejection_reason == "entity_type_conflicts_with_canonical_entity"
    assert reused.status is EntityAdmissionStatus.REUSED
    assert ambiguous.status is EntityAdmissionStatus.REJECTED

    collision_id = service._entity_id("北塔", "location")
    collision_world = world.model_copy(
        update={
            "entities": (
                *world.entities,
                Entity(
                    entity_id=collision_id,
                    entity_type="character",
                    internal_label="别名",
                ),
            )
        }
    )
    collision, _ = service._admit_entity_candidate(
        _entity_candidate("北塔", entity_type="location"), collision_world, blocks
    )
    assert collision.rejection_reason == "stable_entity_id_collision"

    class MissingPolicy(EntityAliasRepairPolicy):
        def resolve(
            self,
            world_arg: WorldRootDocument,
            mention: str,
            *,
            evidence_refs: tuple[EvidenceRef, ...] = (),
        ) -> EntityAliasResolutionReceipt:
            receipt = super().resolve(world_arg, mention, evidence_refs=evidence_refs)
            return receipt.model_copy(
                update={
                    "status": EntityResolutionStatus.MISSING,
                    "matched_entity_ids": (),
                    "resolved_entity_id": None,
                    "match_basis": "forced_missing",
                }
            )

    dedupe_service = WorldGraphExtractionPass(alias_policy=MissingPolicy())
    canonical = Entity(
        entity_id=collision_id,
        entity_type="location",
        internal_label="北塔",
    )
    deduped, _ = dedupe_service._admit_entity_candidate(
        _entity_candidate("北塔", entity_type="location"),
        world.model_copy(update={"entities": (*world.entities, canonical)}),
        blocks,
    )
    assert deduped.status is EntityAdmissionStatus.DEDUPED


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"support_status": GraphCandidateSupportStatus.REJECTED}, "test_support"),
        ({"source_truth_class": TruthClass.UNKNOWN}, "truth_class_not_admitted:unknown"),
        ({"subject_surface": "不存在"}, "subject_entity_missing"),
        ({"object_surface": "不存在"}, "object_entity_missing"),
        ({"object_surface": "旧誓言"}, "self_relation_not_admitted"),
        ({"predicate": "unknown"}, "predicate is not registered"),
        ({"predicate": "resides_at"}, "rejects object type"),
    ],
)
def test_relation_admission_rejection_reasons(updates: dict[str, object], reason: str) -> None:
    world, text, _, _ = _teacher_world()
    service = WorldGraphExtractionPass()
    receipt, relation = service._admit_relation_candidate(
        _relation_candidate(**updates), world, service._blocks(text), {}, {}
    )

    assert receipt.status is RelationBackfillStatus.REJECTED
    assert receipt.rejection_reason is not None and reason in receipt.rejection_reason
    assert relation is None


@pytest.mark.parametrize(
    ("start", "end", "reason"),
    [(0, 2, "subject surface is absent"), (4, 7, "object surface is absent")],
)
def test_relation_admission_requires_both_surfaces_in_exact_evidence(
    start: int, end: int, reason: str
) -> None:
    world, text, _, _ = _teacher_world()
    service = WorldGraphExtractionPass()
    block = text.chapters[4].scenes[0].blocks[0]
    evidence = world.states[0].evidence_refs[0]
    assert evidence.span is not None
    narrowed = evidence.model_copy(
        update={
            "span": evidence.span.model_copy(update={"start": start, "end": end}),
            "quote_hash": quote_hash(block.text[start:end]),
        }
    )
    receipt, relation = service._admit_relation_candidate(
        _relation_candidate(evidence_refs=(narrowed,)),
        world,
        service._blocks(text),
        {},
        {},
    )
    assert receipt.rejection_reason is not None and reason in receipt.rejection_reason
    assert relation is None


def test_relation_admission_covers_dedupe_and_multiplicity() -> None:
    world, text, _, _ = _teacher_world()
    service = WorldGraphExtractionPass()
    candidate = _relation_candidate()
    accepted, relation = service._admit_relation_candidate(
        candidate, world, service._blocks(text), {}, {}
    )
    assert relation is not None
    key = service._relation_key(
        relation.predicate,
        relation.subject_id,
        relation.object_id,
        relation.valid_time.worldline,
        relation.valid_time.start_ordinal,
        relation.valid_time.end_ordinal,
    )
    deduped, duplicate = service._admit_relation_candidate(
        candidate, world, service._blocks(text), {key: relation}, {}
    )
    assert accepted.status is RelationBackfillStatus.ACCEPTED
    assert deduped.status is RelationBackfillStatus.DEDUPED
    assert duplicate is None

    registry = PredicateRegistry()
    registry._definitions["teacher_of"] = replace(
        registry.require("teacher_of"),
        multiplicity=PredicateMultiplicity.ONE_OBJECT_PER_SUBJECT,
    )
    other = relation.model_copy(
        update={
            "relation_id": StableId("relation.other"),
            "object_id": world.entities[-1].entity_id,
        }
    )
    multiplicity_service = WorldGraphExtractionPass(registry=registry)
    conflict, new_relation = multiplicity_service._admit_relation_candidate(
        candidate,
        world,
        multiplicity_service._blocks(text),
        {("other", "key", "for", "coverage", "only", "x"): other},
        {},
    )
    assert conflict.rejection_reason == "predicate_multiplicity_conflict"
    assert new_relation is None


@pytest.mark.parametrize(
    "update",
    [
        None,
        {"support_status": EvidenceSupportStatus.SUPERSEDED},
        {"span": None},
        {"object_hash": ArtifactId("sha256:" + "f" * 64)},
        {"quote_hash": quote_hash("wrong")},
    ],
)
def test_exact_evidence_validation_rejects_every_invalid_shape(
    update: dict[str, object] | None,
) -> None:
    world, text, _, _ = _teacher_world()
    service = WorldGraphExtractionPass()
    evidence = world.states[0].evidence_refs[0]
    evidence_refs = () if update is None else (evidence.model_copy(update=update),)

    with pytest.raises(ValueError):
        service._validate_evidence(evidence_refs, service._blocks(text))


def test_graph_candidate_batch_basis_must_match() -> None:
    world, text, _, _ = _teacher_world()
    result = WorldGraphExtractionPass()._state_candidate_batch(world, text, world.source_commit)
    mismatched = result.model_copy(update={"source_text_root": ArtifactId("sha256:" + "f" * 64)})
    with pytest.raises(ValueError, match="repair basis"):
        WorldGraphExtractionPass().run(world, text, candidate_batches=(mismatched,))


def test_registry_and_state_scan_cover_subject_direction_and_ignored_states() -> None:
    world, text, _, student_id = _teacher_world()
    registry = PredicateRegistry()
    with pytest.raises(ValueError, match="rejects subject type"):
        registry.validate_entity_types("member_of", "location", "location")
    state = world.states[0]
    subject_direction = state.model_copy(
        update={
            "state_id": StableId("state.location.edge"),
            "subject_id": student_id,
            "predicate": "location",
            "value": "北塔",
        }
    )
    ignored = state.model_copy(
        update={"state_id": StableId("state.ignored.edge"), "predicate": "unregistered_state"}
    )
    result = WorldGraphExtractionPass().run(
        world.model_copy(update={"states": (subject_direction, ignored)}), text
    )
    assert result.receipt.candidates[0].predicate == "located_at"


def test_run_loop_accounts_for_reused_entity_without_create_operation() -> None:
    world, text, _, _ = _teacher_world()
    service = WorldGraphExtractionPass()
    batch_id = StableId("batch.reused.edge")
    batch = service._state_candidate_batch(world, text, world.source_commit).model_copy(
        update={
            "batch_id": batch_id,
            "relations": (),
            "entities": (
                _entity_candidate("林澈").model_copy(update={"source_batch_id": batch_id}),
                _entity_candidate("旧誓言", supported=False).model_copy(
                    update={"source_batch_id": batch_id}
                ),
            ),
        }
    )
    result = service.run(world, text, candidate_batches=(batch,))
    assert result.receipt.entity_admissions[0].status is EntityAdmissionStatus.REUSED
    assert result.receipt.entity_admissions[1].status is EntityAdmissionStatus.REJECTED
