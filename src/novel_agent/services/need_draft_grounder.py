"""Deterministic grounding of planner entity/relation mentions."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

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

    Resolution uses one deterministic rule set (evidence-first Need contract):

    1. a normalized mention that uniquely exact-matches an ``internal_label``
       resolves directly to that runtime entity; another entity's same-named
       alias must not turn the canonical exact match into ambiguity;
    2. without an internal-label exact match, a unique alias exact match
       grounds the mention;
    3. multiple internal-label exact matches, multiple alias matches and
       unknown labels all fail closed (``AMBIGUOUS``/``UNRESOLVED``) -- no
       fuzzy, substring or dense inference is used to guess an id;
    4. a bounded mention closure scans only the same public draft's semantic
       question, query hints, why-needed and trigger goal, never Gold/future
       or whole-World text;
    5. an exact, uniquely owned scalar StateRecord predicate/value in those
       fields may close its subject entity; ambiguous literals stay unresolved
       and no Canon alias is written;
    6. every mention records explicit/derived source fields, match kind, and
       the chosen id or typed rejection for the grounding audit.
    """

    version = "need_draft_grounder.v5"

    @staticmethod
    def _normalize(label: str) -> str:
        return " ".join(re.sub(r"[\s\u3000]+", " ", label).strip().casefold().split())

    @classmethod
    def _resolvable_label_map(cls, world: WorldRootDocument) -> dict[str, StableId]:
        """Normalized label -> entity id under internal-label priority.

        A label is resolvable when exactly one entity carries it as
        ``internal_label`` (even if other entities use it as an alias), or,
        when no entity carries it as an internal label, exactly one entity
        uses it as an alias.  Labels that are ambiguous under these rules are
        excluded so closure and coverage postconditions never guess an ID.
        """

        internal_ids: dict[str, list[StableId]] = {}
        alias_ids: dict[str, list[StableId]] = {}
        for entity in world.entities:
            if entity.internal_label.strip():
                internal_ids.setdefault(cls._normalize(entity.internal_label), []).append(
                    entity.entity_id
                )
            for label in entity.aliases:
                if label.strip():
                    alias_ids.setdefault(cls._normalize(label), []).append(entity.entity_id)
        resolvable: dict[str, StableId] = {}
        for normalized, ids in internal_ids.items():
            if len(ids) == 1:
                resolvable[normalized] = ids[0]
        for normalized, ids in alias_ids.items():
            if normalized not in internal_ids and len(ids) == 1:
                resolvable[normalized] = ids[0]
        return resolvable

    @classmethod
    def _world_label_map(cls, world: WorldRootDocument) -> dict[str, StableId]:
        """Backward-compatible label map with internal-label priority."""
        return cls._resolvable_label_map(world)

    def ground(
        self,
        draft: PlannedNeedDraft,
        world: WorldRootDocument,
    ) -> GroundedNeedDraft:
        internal_label_entities: dict[str, tuple[StableId, ...]] = {}
        alias_entities: dict[str, tuple[StableId, ...]] = {}
        label_by_id = {entity.entity_id: entity.internal_label for entity in world.entities}
        for entity in world.entities:
            if entity.internal_label.strip():
                normalized = self._normalize(entity.internal_label)
                if entity.entity_id not in internal_label_entities.get(
                    normalized, ()
                ):  # pragma: no branch - world entities are unique
                    internal_label_entities[normalized] = (
                        *internal_label_entities.get(normalized, ()),
                        entity.entity_id,
                    )
            for label in dict.fromkeys(entity.aliases):
                if not label.strip():
                    continue
                normalized = self._normalize(label)
                if entity.entity_id not in alias_entities.get(
                    normalized, ()
                ):  # pragma: no branch - world entities are unique
                    alias_entities[normalized] = (
                        *alias_entities.get(normalized, ()),
                        entity.entity_id,
                    )
        resolved: dict[str, tuple[StableId, str]] = {}

        def resolve_entity(
            label: str,
            *,
            mention_source: str = "explicit",
            source_fields: tuple[str, ...] = (),
            derived_entity_id: StableId | None = None,
            derived_method: str = "exact_state_literal_subject_match",
        ) -> GroundedEntityMention:
            if derived_entity_id is not None:
                return GroundedEntityMention(
                    mention=label,
                    canonical_label=label_by_id[derived_entity_id],
                    entity_id=derived_entity_id,
                    confidence=1.0,
                    grounding_method=derived_method,
                    grounding_status=GroundingStatus.GROUNDED,
                    mention_source=mention_source,
                    mention_source_fields=source_fields,
                )
            normalized = self._normalize(label)
            if normalized in resolved:
                entity_id, canonical = resolved[normalized]
                return GroundedEntityMention(
                    mention=label,
                    canonical_label=canonical,
                    entity_id=entity_id,
                    confidence=1.0,
                    grounding_method="exact_internal_label_match"
                    if normalized in internal_label_entities
                    else "exact_alias_match",
                    grounding_status=GroundingStatus.GROUNDED,
                    mention_source=mention_source,
                    mention_source_fields=source_fields,
                )
            internal = internal_label_entities.get(normalized, ())
            if len(internal) == 1:
                entity_id = internal[0]
                canonical = label_by_id[entity_id]
                resolved[normalized] = (entity_id, canonical)
                return GroundedEntityMention(
                    mention=label,
                    canonical_label=canonical,
                    entity_id=entity_id,
                    confidence=1.0,
                    grounding_method="exact_internal_label_match",
                    grounding_status=GroundingStatus.GROUNDED,
                    mention_source=mention_source,
                    mention_source_fields=source_fields,
                )
            if internal:
                return GroundedEntityMention(
                    mention=label,
                    canonical_label=label,
                    entity_id=None,
                    confidence=0.4,
                    grounding_method="ambiguous_label_match",
                    grounding_status=GroundingStatus.AMBIGUOUS,
                    mention_source=mention_source,
                    mention_source_fields=source_fields,
                )
            aliases = alias_entities.get(normalized, ())
            if len(aliases) == 1:
                entity_id = aliases[0]
                canonical = label_by_id[entity_id]
                resolved[normalized] = (entity_id, canonical)
                return GroundedEntityMention(
                    mention=label,
                    canonical_label=canonical,
                    entity_id=entity_id,
                    confidence=1.0,
                    grounding_method="exact_alias_match",
                    grounding_status=GroundingStatus.GROUNDED,
                    mention_source=mention_source,
                    mention_source_fields=source_fields,
                )
            if aliases:
                return GroundedEntityMention(
                    mention=label,
                    canonical_label=label,
                    entity_id=None,
                    confidence=0.4,
                    grounding_method="ambiguous_alias_match",
                    grounding_status=GroundingStatus.AMBIGUOUS,
                    mention_source=mention_source,
                    mention_source_fields=source_fields,
                )
            return GroundedEntityMention(
                mention=label,
                canonical_label=label,
                entity_id=None,
                confidence=0.0,
                grounding_method="no_label_match",
                grounding_status=GroundingStatus.UNRESOLVED,
                mention_source=mention_source,
                mention_source_fields=source_fields,
            )

        grounded_entities = tuple(
            resolve_entity(mention.label) for mention in draft.entity_mentions
        )
        grounded_by_normalized = {self._normalize(item.mention): item for item in grounded_entities}
        closure_texts: tuple[tuple[str, tuple[str, ...]], ...] = (
            (draft.semantic_question, ("semantic_question",)),
            *((hint, ("query_hints",)) for hint in draft.query_hints),
            (draft.why_needed, ("why_needed",)),
            (draft.trigger_plan_goal, ("trigger_plan_goal",)),
        )
        closed_mentions = self._mention_closure(
            closure_texts=closure_texts,
            unique_label_entities=self._resolvable_label_map(world),
            state_literal_entities=self._unique_state_literal_entities(world),
            grounded_by_normalized=grounded_by_normalized,
            resolve=resolve_entity,
        )
        grounded_entities = (*grounded_entities, *closed_mentions)
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
                and relation.subject_id == subject.entity_id
                and relation.object_id == object_.entity_id
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
                    subject_id=subject.entity_id,
                    object_id=object_.entity_id,
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

    @classmethod
    def _mention_closure(
        cls,
        *,
        closure_texts: tuple[tuple[str, tuple[str, ...]], ...],
        unique_label_entities: dict[str, StableId],
        state_literal_entities: Mapping[str, StableId],
        grounded_by_normalized: dict[str, GroundedEntityMention],
        resolve: Callable[..., GroundedEntityMention],
    ) -> tuple[GroundedEntityMention, ...]:
        """Bounded literal entity-mention closure over a draft's own text.

        Finds every resolvable label or uniquely owned StateRecord literal
        that occurs verbatim in the draft-authorized text fields (semantic
        question, query hints, why-needed, trigger goal), longest label first
        so a longer alias wins over a shorter substring of it.  Entity labels
        take precedence over state literals.  Only literals that uniquely
        resolve to one runtime entity are closed in; ambiguous, empty, absent
        or unresolved literals stay fail-closed.  Explicit mentions already
        resolved are skipped.
        """

        literal_entities: dict[str, tuple[StableId, bool]] = {
            normalized: (entity_id, False)
            for normalized, entity_id in unique_label_entities.items()
        }
        literal_entities.update(
            {
                normalized: (entity_id, True)
                for normalized, entity_id in state_literal_entities.items()
                if normalized not in literal_entities
            }
        )
        normalized_by_length = tuple(sorted(literal_entities, key=lambda item: (-len(item), item)))
        combined = "\n".join(text for text, _fields in closure_texts if text)
        closed: list[GroundedEntityMention] = []
        closed_ids: set[StableId] = set()
        occupied: list[tuple[int, int]] = []
        for normalized in normalized_by_length:
            text = combined
            start = 0
            while True:
                found = text.find(normalized, start)
                if found < 0:
                    break
                span = (found, found + len(normalized))
                if any(
                    span[0] < other_end and other_start < span[1]
                    for other_start, other_end in occupied
                ):
                    start = found + 1
                    continue
                occupied.append(span)
                entity_id, is_state_literal = literal_entities[normalized]
                if entity_id in closed_ids:
                    start = found + 1
                    continue
                mention = grounded_by_normalized.get(normalized)
                if (
                    mention is not None
                    and mention.entity_id is not None
                    and mention.grounding_status is GroundingStatus.GROUNDED
                ):
                    start = found + 1
                    continue
                fields = tuple(
                    field
                    for text_value, field_names in closure_texts
                    if text_value and normalized in text_value
                    for field in field_names
                )
                resolved_mention = resolve(
                    normalized,
                    mention_source=(
                        "exact_state_literal_subject_closure"
                        if is_state_literal
                        else "exact_text_mention_closure"
                    ),
                    source_fields=fields,
                    **({"derived_entity_id": entity_id} if is_state_literal else {}),
                )
                if (  # pragma: no branch - closure labels uniquely resolve
                    resolved_mention.entity_id is not None
                    and resolved_mention.grounding_status is GroundingStatus.GROUNDED
                ):
                    closed.append(resolved_mention)
                    closed_ids.add(entity_id)
                start = found + 1
        return tuple(closed)

    @classmethod
    def _unique_state_literal_entities(
        cls,
        world: WorldRootDocument,
    ) -> dict[str, StableId]:
        """Map exact, uniquely owned scalar state literals to their subjects.

        This is only a grounding aid for a public draft.  It does not expose
        state text to the draft, create an alias, or infer a relation: a
        literal is usable only when every matching StateRecord belongs to the
        same subject entity.
        """

        owners: dict[str, set[StableId]] = {}
        for state in world.states:
            literals: tuple[str, ...] = (state.predicate,)
            if isinstance(state.value, str) and state.value.strip():
                literals = (*literals, state.value)
            for literal in literals:
                normalized = cls._normalize(literal)
                if normalized:
                    owners.setdefault(normalized, set()).add(state.subject_id)
        return {
            literal: next(iter(entity_ids))
            for literal, entity_ids in owners.items()
            if len(entity_ids) == 1
        }


__all__ = ["NeedDraftGrounder"]
