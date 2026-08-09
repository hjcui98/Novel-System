"""Deterministic grounding of planner entity/relation mentions."""

from __future__ import annotations

import re

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.planning_memory import (
    GroundedEntityMention,
    GroundedNeedDraft,
    GroundedRelationMention,
    GroundingStatus,
    PlannedNeedDraft,
)


class NeedDraftGrounder:
    """Bind natural-language mentions to canonical world records.

    The LLM never guesses graph ids; this layer resolves mentions through
    exact label/alias matches first, then a bounded fuzzy match, and marks
    ambiguous or unresolved mentions explicitly.  Same-label ambiguity is
    resolved with relation context when the draft names the other endpoint;
    otherwise the mention stays ``AMBIGUOUS``/``UNRESOLVED`` and the
    validator decides.
    """

    version = "need_draft_grounder.v1"

    @staticmethod
    def _normalize(label: str) -> str:
        return " ".join(re.sub(r"[\s\u3000]+", " ", label).strip().casefold().split())

    def ground(
        self,
        draft: PlannedNeedDraft,
        world: WorldRootDocument,
    ) -> GroundedNeedDraft:
        aliases_to_entities: dict[str, tuple[tuple[StableId, str], ...]] = {}
        for entity in world.entities:
            labels = tuple(
                dict.fromkeys(
                    label for label in (entity.internal_label, *entity.aliases) if label.strip()
                )
            )
            for label in labels:
                normalized = self._normalize(label)
                previous = aliases_to_entities.get(normalized, ())
                if any(entity_id == entity.entity_id for entity_id, _canonical in previous):
                    continue
                aliases_to_entities[normalized] = (
                    *previous,
                    (entity.entity_id, entity.internal_label),
                )
        mention_by_normalized = {
            self._normalize(mention.label): mention for mention in draft.entity_mentions
        }
        resolved: dict[str, tuple[StableId, str]] = {}

        def exact_candidates(label: str) -> tuple[tuple[StableId, str], ...]:
            return aliases_to_entities.get(self._normalize(label), ())

        def resolve_entity(label: str) -> GroundedEntityMention:
            normalized = self._normalize(label)
            if normalized in resolved:
                entity_id, canonical = resolved[normalized]
                return GroundedEntityMention(
                    mention=label,
                    canonical_label=canonical,
                    entity_id=entity_id,
                    confidence=1.0,
                    grounding_method="exact_label_match",
                    grounding_status=GroundingStatus.GROUNDED,
                )
            exact = exact_candidates(label)
            if len(exact) == 1:
                entity_id, canonical = exact[0]
                resolved[normalized] = (entity_id, canonical)
                return GroundedEntityMention(
                    mention=label,
                    canonical_label=canonical,
                    entity_id=entity_id,
                    confidence=1.0,
                    grounding_method="exact_label_match",
                    grounding_status=GroundingStatus.GROUNDED,
                )
            if exact:
                context = relation_context_candidates(label, exact)
                if len(context) == 1:
                    entity_id, canonical = context[0]
                    resolved[normalized] = (entity_id, canonical)
                    return GroundedEntityMention(
                        mention=label,
                        canonical_label=canonical,
                        entity_id=entity_id,
                        confidence=0.9,
                        grounding_method="relation_context_match",
                        grounding_status=GroundingStatus.GROUNDED,
                    )
                return GroundedEntityMention(
                    mention=label,
                    canonical_label=label,
                    entity_id=None,
                    confidence=0.4,
                    grounding_method="ambiguous_label_match",
                    grounding_status=GroundingStatus.AMBIGUOUS,
                )
            fuzzy: list[tuple[StableId, str]] = []
            for alias, entries in aliases_to_entities.items():
                if len(alias) >= 2 and (normalized in alias or alias in normalized):
                    fuzzy.extend(entries)
            if len(fuzzy) == 1:
                hit_entity_id, hit_canonical = fuzzy[0]
                resolved[normalized] = (hit_entity_id, hit_canonical)
                return GroundedEntityMention(
                    mention=label,
                    canonical_label=hit_canonical,
                    entity_id=hit_entity_id,
                    confidence=0.8,
                    grounding_method="fuzzy_label_match",
                    grounding_status=GroundingStatus.GROUNDED,
                )
            if fuzzy:
                return GroundedEntityMention(
                    mention=label,
                    canonical_label=label,
                    entity_id=None,
                    confidence=0.4,
                    grounding_method="ambiguous_fuzzy_match",
                    grounding_status=GroundingStatus.AMBIGUOUS,
                )
            return GroundedEntityMention(
                mention=label,
                canonical_label=label,
                entity_id=None,
                confidence=0.0,
                grounding_method="no_label_match",
                grounding_status=GroundingStatus.UNRESOLVED,
            )

        def relation_context_candidates(
            label: str,
            exact: tuple[tuple[StableId, str], ...],
        ) -> tuple[tuple[StableId, str], ...]:
            """Disambiguate same-label entities through draft relation endpoints."""

            normalized = self._normalize(label)
            candidates: list[tuple[StableId, str]] = []
            for relation_mention in draft.relation_mentions:
                subject_norm = self._normalize(relation_mention.subject_label)
                object_norm = self._normalize(relation_mention.object_label)
                if normalized not in {subject_norm, object_norm}:
                    continue
                other_label = (
                    relation_mention.object_label
                    if subject_norm == normalized
                    else relation_mention.subject_label
                )
                other_mention = mention_by_normalized.get(self._normalize(other_label))
                if other_mention is None:
                    continue
                other_resolved = (
                    resolved.get(self._normalize(other_label)) or exact_candidates(other_label)[0]
                    if len(exact_candidates(other_label)) == 1
                    else None
                )
                if other_resolved is None:
                    continue
                for candidate_entity_id, candidate_canonical in exact:
                    if any(
                        relation.predicate == relation_mention.relation_label
                        and other_resolved[0] in {relation.subject_id, relation.object_id}
                        and candidate_entity_id in {relation.subject_id, relation.object_id}
                        for relation in world.relations
                    ):
                        candidates.append((candidate_entity_id, candidate_canonical))
            return tuple(dict.fromkeys(candidates))

        grounded_entities = tuple(
            resolve_entity(mention.label) for mention in draft.entity_mentions
        )
        grounded_by_normalized = {self._normalize(item.mention): item for item in grounded_entities}
        grounded_relations: list[GroundedRelationMention] = []
        for mention in draft.relation_mentions:
            subject = grounded_by_normalized.get(self._normalize(mention.subject_label))
            object_ = grounded_by_normalized.get(self._normalize(mention.object_label))
            if subject is None or object_ is None:
                grounded_relations.append(
                    GroundedRelationMention(
                        subject_label=mention.subject_label,
                        relation_label=mention.relation_label,
                        object_label=mention.object_label,
                        relation_id=None,
                        grounding_status=GroundingStatus.UNRESOLVED,
                        confidence=0.0,
                        grounding_method="unresolved_endpoint",
                    )
                )
                continue
            if subject.entity_id is None or object_.entity_id is None:
                grounded_relations.append(
                    GroundedRelationMention(
                        subject_label=mention.subject_label,
                        relation_label=mention.relation_label,
                        object_label=mention.object_label,
                        relation_id=None,
                        grounding_status=GroundingStatus.AMBIGUOUS,
                        confidence=0.3,
                        grounding_method="ambiguous_endpoint",
                    )
                )
                continue
            matches = tuple(
                relation
                for relation in world.relations
                if relation.predicate == mention.relation_label
                and {relation.subject_id, relation.object_id}
                == {subject.entity_id, object_.entity_id}
            )
            if len(matches) == 1:
                relation_id = matches[0].relation_id
                status = GroundingStatus.GROUNDED
                confidence = 0.9
                method = "context_relation_match"
            elif matches:
                relation_id = None
                status = GroundingStatus.AMBIGUOUS
                confidence = 0.5
                method = "ambiguous_relation_match"
            else:
                relation_id = None
                status = GroundingStatus.UNRESOLVED
                confidence = 0.2
                method = "no_relation_match"
            grounded_relations.append(
                GroundedRelationMention(
                    subject_label=mention.subject_label,
                    relation_label=mention.relation_label,
                    object_label=mention.object_label,
                    relation_id=relation_id,
                    grounding_status=status,
                    confidence=confidence,
                    grounding_method=method,
                )
            )
        return GroundedNeedDraft(
            draft_id=draft.draft_id,
            semantic_question=draft.semantic_question,
            entity_mentions=grounded_entities,
            relation_mentions=tuple(grounded_relations),
            trigger_plan_chapters=draft.trigger_plan_chapters,
            trigger_plan_goal=draft.trigger_plan_goal,
            why_needed=draft.why_needed,
            required_claim_scopes=draft.required_claim_scopes,
            suggested_facets=draft.suggested_facets,
            historical_time_scope=draft.historical_time_scope,
            query_hints=draft.query_hints,
        )

    def grounded_entity_ids(
        self,
        draft: GroundedNeedDraft,
    ) -> tuple[StableId, ...]:
        return tuple(
            dict.fromkeys(
                mention.entity_id
                for mention in draft.entity_mentions
                if mention.entity_id is not None
            )
        )


__all__ = ["NeedDraftGrounder"]
