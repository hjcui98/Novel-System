"""Versioned PostgreSQL R1 materialization, exact/temporal lookup, and bounded graph traversal."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, delete, exists, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import R1RecordEntityRow, R1RecordRow
from novel_agent.domain.benchmark import PlanRootDocument
from novel_agent.domain.changes import WorldRecordKind
from novel_agent.domain.ids import CommitId, ProjectId, StableId
from novel_agent.domain.memory import (
    ChannelHit,
    R1RecordView,
    RetrievalChannel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.text import EvidenceRef
from novel_agent.domain.world import StoryTime, TruthClass
from novel_agent.services.canonical_alias_registry import CanonicalAliasRegistry
from novel_agent.services.need_query_compiler import compile_need_query


@dataclass(frozen=True, slots=True)
class _RecordSpec:
    kind: str
    record_id: StableId
    predicate: str | None
    valid_start: int | None
    valid_end: int | None
    worldline: str | None
    narrative_start: int | None
    narrative_end: int | None
    access_scope: str
    truth_class: str | None
    entities: tuple[tuple[StableId, str], ...]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GraphPath:
    """Bounded canonical-relation path with evidence and direction receipts."""

    relation_row_ids: tuple[StableId, ...]
    relation_ids: tuple[StableId, ...]
    entity_path: tuple[StableId, ...]
    directions: tuple[str, ...]
    edge_semantics: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...]


class R1WorldRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def materialize(
        self,
        project_id: ProjectId,
        canonical_commit: CommitId,
        world: WorldRootDocument,
        plan: PlanRootDocument | None = None,
    ) -> int:
        specs = self._specs(world, plan)
        with self._session_factory() as session, session.begin():
            old_ids = tuple(
                session.scalars(
                    select(R1RecordRow.row_id).where(
                        R1RecordRow.source_commit == canonical_commit.root
                    )
                )
            )
            if old_ids:
                session.execute(
                    delete(R1RecordEntityRow).where(R1RecordEntityRow.row_id.in_(old_ids))
                )
            session.execute(
                delete(R1RecordRow).where(R1RecordRow.source_commit == canonical_commit.root)
            )
            for spec in specs:
                row_id = self._row_id(canonical_commit, spec.kind, spec.record_id)
                session.add(
                    R1RecordRow(
                        row_id=row_id.root,
                        project_id=project_id.root,
                        source_commit=canonical_commit.root,
                        record_kind=spec.kind,
                        record_id=spec.record_id.root,
                        predicate=spec.predicate,
                        valid_start=spec.valid_start,
                        valid_end=spec.valid_end,
                        worldline=spec.worldline,
                        narrative_start=spec.narrative_start,
                        narrative_end=spec.narrative_end,
                        access_scope=spec.access_scope,
                        truth_class=spec.truth_class,
                        record_json=spec.payload,
                    )
                )
                session.flush()
                for entity_id, role in spec.entities:
                    session.add(
                        R1RecordEntityRow(
                            row_id=row_id.root,
                            entity_id=entity_id.root,
                            role=role,
                        )
                    )
        return len(specs)

    def counts(self, source_commit: CommitId) -> tuple[int, int, int]:
        """Return record, association, and relation-edge receipts for one basis."""

        with self._session_factory() as session:
            record_count = session.scalar(
                select(func.count())
                .select_from(R1RecordRow)
                .where(R1RecordRow.source_commit == source_commit.root)
            )
            association_count = session.scalar(
                select(func.count())
                .select_from(R1RecordEntityRow)
                .join(R1RecordRow, R1RecordEntityRow.row_id == R1RecordRow.row_id)
                .where(R1RecordRow.source_commit == source_commit.root)
            )
            relation_count = session.scalar(
                select(func.count())
                .select_from(R1RecordRow)
                .where(
                    R1RecordRow.source_commit == source_commit.root,
                    R1RecordRow.record_kind == WorldRecordKind.RELATION.value,
                )
            )
        return int(record_count or 0), int(association_count or 0), int(relation_count or 0)

    def exact(
        self,
        need: Stage1MemoryNeed,
        *,
        temporal: bool,
        limit: int,
        access_scopes: tuple[str, ...] = ("writer_safe",),
    ) -> tuple[R1RecordView, ...]:
        if limit < 1:
            raise ValueError("R1 limit must be positive")
        if need.query_intent in {
            Stage1QueryIntent.CURRENT_STATE,
            Stage1QueryIntent.KNOWN_ID,
            Stage1QueryIntent.MANDATORY_CONSTRAINT,
        } and not (need.entity_ids or need.predicates):
            raise ValueError("unfiltered factual exact retrieval is forbidden")
        self._validate_access_scopes(access_scopes)
        statement: Select[tuple[R1RecordRow]] = select(R1RecordRow).where(
            R1RecordRow.source_commit == need.base_commit.root,
            R1RecordRow.access_scope.in_(access_scopes),
        )
        kinds = self._kinds_for_need(need)
        if kinds:
            statement = statement.where(R1RecordRow.record_kind.in_(kinds))
        if need.predicates:
            statement = statement.where(R1RecordRow.predicate.in_(need.predicates))
        if need.query_intent in {
            Stage1QueryIntent.CURRENT_STATE,
            Stage1QueryIntent.KNOWN_ID,
            Stage1QueryIntent.MANDATORY_CONSTRAINT,
        }:
            statement = statement.where(
                or_(
                    R1RecordRow.truth_class.is_(None),
                    R1RecordRow.truth_class == TruthClass.ACCEPTED_WORLD_FACT.value,
                    R1RecordRow.truth_class == TruthClass.ASSERTION.value,
                )
            )
        if need.entity_ids:
            statement = statement.where(
                exists().where(
                    R1RecordEntityRow.row_id == R1RecordRow.row_id,
                    R1RecordEntityRow.entity_id.in_(
                        tuple(entity.root for entity in need.entity_ids)
                    ),
                )
            )
        if temporal and need.time_scope is not None:
            ordinal = need.time_scope.start_ordinal
            statement = statement.where(
                or_(
                    R1RecordRow.worldline.is_(None),
                    R1RecordRow.worldline == need.time_scope.worldline,
                )
            )
            if ordinal is not None:
                statement = statement.where(
                    or_(R1RecordRow.valid_start.is_(None), R1RecordRow.valid_start <= ordinal),
                    or_(R1RecordRow.valid_end.is_(None), R1RecordRow.valid_end >= ordinal),
                )
        if need.chapter_target is not None:
            statement = statement.where(
                or_(
                    R1RecordRow.narrative_start.is_(None),
                    R1RecordRow.narrative_start <= need.chapter_target,
                ),
                or_(
                    R1RecordRow.narrative_end.is_(None),
                    R1RecordRow.narrative_end >= need.chapter_target,
                ),
            )
        statement = statement.order_by(
            R1RecordRow.valid_start.desc().nullslast(),
            R1RecordRow.narrative_start.desc().nullslast(),
            R1RecordRow.record_kind,
            R1RecordRow.record_id,
        ).limit(limit)
        with self._session_factory() as session:
            rows = tuple(session.scalars(statement))
            return self._views(session, rows)

    def resolve_entity_alias(
        self,
        source_commit: CommitId,
        alias: str,
        *,
        limit: int = 8,
    ) -> tuple[StableId, ...]:
        """Resolve only exact normalized entity labels/aliases in one commit basis."""

        if not alias.strip() or limit < 1:
            raise ValueError("alias resolution requires a non-empty alias and positive limit")
        normalized = self._normalize_alias(alias)
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(R1RecordRow)
                    .where(
                        R1RecordRow.source_commit == source_commit.root,
                        R1RecordRow.record_kind == WorldRecordKind.ENTITY.value,
                    )
                    .order_by(R1RecordRow.record_id)
                )
            )
        resolved: list[StableId] = []
        for row in rows:
            aliases = row.record_json.get("aliases", [])
            labels = [row.record_json.get("internal_label"), *aliases]
            if any(
                isinstance(label, str) and self._normalize_alias(label) == normalized
                for label in labels
            ):
                resolved.append(StableId(row.record_id))
                if len(resolved) == limit:
                    break
        return tuple(resolved)

    def records_for_evidence(
        self,
        source_commit: CommitId,
        evidence_id: StableId,
        *,
        limit: int = 100,
    ) -> tuple[R1RecordView, ...]:
        """Find canonical records with a direct evidence reverse reference."""

        if limit < 1:
            raise ValueError("evidence reverse lookup limit must be positive")
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(R1RecordRow)
                    .where(R1RecordRow.source_commit == source_commit.root)
                    .order_by(R1RecordRow.record_kind, R1RecordRow.record_id)
                )
            )
            matched = tuple(
                row
                for row in rows
                if any(
                    isinstance(item, dict) and item.get("evidence_id") == evidence_id.root
                    for item in row.record_json.get("evidence_refs", [])
                )
            )[:limit]
            return self._views(session, matched)

    def typed_graph(
        self,
        source_commit: CommitId,
        entity_ids: tuple[StableId, ...],
        *,
        max_depth: int,
        limit: int,
        allowed_predicates: tuple[str, ...] = (),
        time_scope: StoryTime | None = None,
        allowed_edge_semantics: tuple[str, ...] = ("canonical", "evidence"),
        access_scopes: tuple[str, ...] = ("writer_safe",),
    ) -> tuple[R1RecordView, ...]:
        paths = self.typed_graph_paths(
            source_commit,
            entity_ids,
            max_depth=max_depth,
            limit=limit,
            allowed_predicates=allowed_predicates,
            time_scope=time_scope,
            allowed_edge_semantics=allowed_edge_semantics,
            access_scopes=access_scopes,
        )
        row_ids = tuple(
            dict.fromkeys(row_id for path in paths for row_id in path.relation_row_ids)
        )[:limit]
        if not row_ids:
            return ()
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(R1RecordRow)
                    .where(R1RecordRow.row_id.in_(tuple(item.root for item in row_ids)))
                    .order_by(R1RecordRow.record_id)
                )
            )
            return self._views(session, rows)

    def typed_graph_paths(
        self,
        source_commit: CommitId,
        entity_ids: tuple[StableId, ...],
        *,
        max_depth: int,
        limit: int,
        allowed_predicates: tuple[str, ...] = (),
        time_scope: StoryTime | None = None,
        allowed_edge_semantics: tuple[str, ...] = ("canonical", "evidence"),
        access_scopes: tuple[str, ...] = ("writer_safe",),
    ) -> tuple[GraphPath, ...]:
        """Traverse accepted relation edges with fixed depth and explicit receipts."""

        if max_depth < 1 or limit < 1:
            raise ValueError("graph depth and limit must be positive")
        if not entity_ids:
            return ()
        self._validate_access_scopes(access_scopes)
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(R1RecordRow).where(
                        R1RecordRow.source_commit == source_commit.root,
                        R1RecordRow.record_kind == WorldRecordKind.RELATION.value,
                        R1RecordRow.access_scope.in_(access_scopes),
                    )
                )
            )
            associations = tuple(
                session.scalars(
                    select(R1RecordEntityRow).where(
                        R1RecordEntityRow.row_id.in_(tuple(row.row_id for row in rows))
                    )
                )
            )
        roles: dict[str, dict[str, str]] = {}
        for association in associations:
            roles.setdefault(association.row_id, {})[association.role] = association.entity_id
        edges: list[tuple[R1RecordRow, str, str]] = []
        for row in rows:
            endpoints = roles.get(row.row_id, {})
            source = endpoints.get("subject")
            target = endpoints.get("object")
            if source is None or target is None:
                continue
            if row.truth_class != TruthClass.ACCEPTED_WORLD_FACT.value:
                continue
            if allowed_predicates and row.predicate not in allowed_predicates:
                continue
            if not self._edge_matches_time(row, time_scope):
                continue
            edges.append((row, source, target))
        allowed = set(allowed_edge_semantics)
        if not allowed or {"inferred", "similarity"} & allowed:
            raise ValueError("graph traversal only permits explicit canonical/evidence semantics")
        if "canonical" not in allowed:
            return ()
        paths: list[GraphPath] = []
        frontier: list[tuple[str, tuple[R1RecordRow, ...], tuple[str, ...], tuple[str, ...]]] = [
            (entity.root, (), (entity.root,), ()) for entity in entity_ids
        ]
        while frontier and len(paths) < limit:
            node, path_rows, path_entities, directions = frontier.pop(0)
            if len(path_rows) == max_depth:
                continue
            for row, source, target in edges:
                if source == node:
                    next_node, direction = target, "forward"
                elif target == node:
                    next_node, direction = source, "reverse"
                else:
                    continue
                if next_node in path_entities:
                    continue
                next_rows = (*path_rows, row)
                next_entities = (*path_entities, next_node)
                next_directions = (*directions, direction)
                evidence = tuple(
                    item for edge in next_rows for item in self._evidence_refs(edge.record_json)
                )
                paths.append(
                    GraphPath(
                        relation_row_ids=tuple(StableId(edge.row_id) for edge in next_rows),
                        relation_ids=tuple(StableId(edge.record_id) for edge in next_rows),
                        entity_path=tuple(StableId(item) for item in next_entities),
                        directions=next_directions,
                        edge_semantics=("canonical",) * len(next_rows),
                        evidence_refs=evidence,
                    )
                )
                if len(paths) == limit:
                    break
                frontier.append((next_node, next_rows, next_entities, next_directions))
        return tuple(paths)

    @staticmethod
    def _views(session: Session, rows: tuple[R1RecordRow, ...]) -> tuple[R1RecordView, ...]:
        if not rows:
            return ()
        associations = tuple(
            session.scalars(
                select(R1RecordEntityRow)
                .where(R1RecordEntityRow.row_id.in_(tuple(row.row_id for row in rows)))
                .order_by(R1RecordEntityRow.association_id)
            )
        )
        entities: dict[str, list[StableId]] = {}
        for item in associations:
            entities.setdefault(item.row_id, []).append(StableId(item.entity_id))
        return tuple(
            R1RecordView(
                row_id=StableId(row.row_id),
                source_commit=CommitId(row.source_commit),
                record_kind=row.record_kind,
                record_id=StableId(row.record_id),
                predicate=row.predicate,
                valid_start=row.valid_start,
                valid_end=row.valid_end,
                worldline=row.worldline,
                narrative_start=row.narrative_start,
                narrative_end=row.narrative_end,
                access_scope=row.access_scope,
                truth_class=row.truth_class,
                entity_ids=tuple(entities.get(row.row_id, ())),
                record=row.record_json,
            )
            for row in rows
        )

    @staticmethod
    def _kinds_for_need(need: Stage1MemoryNeed) -> tuple[str, ...]:
        if need.query_intent.value == "current_state":
            return (WorldRecordKind.STATE.value,)
        if need.query_intent.value == "plan_node":
            return (
                WorldRecordKind.OBLIGATION.value,
                "plan_node",
                "chapter_goal",
            )
        if need.query_intent.value == "mandatory_constraint":
            return (WorldRecordKind.STATE.value, WorldRecordKind.OBLIGATION.value)
        return ()

    @staticmethod
    def _row_id(commit: CommitId, kind: str, record_id: StableId) -> StableId:
        digest = hashlib.sha256(f"{commit.root}\0{kind}\0{record_id.root}".encode()).hexdigest()
        return StableId(f"r1.{digest}")

    @staticmethod
    def _specs(world: WorldRootDocument, plan: PlanRootDocument | None) -> tuple[_RecordSpec, ...]:
        specs: list[_RecordSpec] = []
        for entity in world.entities:
            specs.append(
                _RecordSpec(
                    kind=WorldRecordKind.ENTITY.value,
                    record_id=entity.entity_id,
                    predicate=None,
                    valid_start=None,
                    valid_end=None,
                    worldline=None,
                    narrative_start=None,
                    narrative_end=None,
                    access_scope="writer_safe",
                    truth_class=None,
                    entities=((entity.entity_id, "self"),),
                    payload=entity.model_dump(mode="json"),
                )
            )
        for event in world.events:
            specs.append(
                _RecordSpec(
                    kind=WorldRecordKind.EVENT.value,
                    record_id=event.event_id,
                    predicate=event.event_type,
                    valid_start=None
                    if event.story_time is None
                    else event.story_time.start_ordinal,
                    valid_end=None if event.story_time is None else event.story_time.end_ordinal,
                    worldline=None if event.story_time is None else event.story_time.worldline,
                    narrative_start=(
                        None
                        if event.narrative_order is None
                        else event.narrative_order.chapter_index
                    ),
                    narrative_end=(
                        None
                        if event.narrative_order is None
                        else event.narrative_order.chapter_index
                    ),
                    access_scope="writer_safe",
                    truth_class=event.truth_class.value,
                    entities=tuple((entity, "participant") for entity in event.participant_ids),
                    payload=event.model_dump(mode="json"),
                )
            )
        for state in world.states:
            specs.append(
                _RecordSpec(
                    kind=WorldRecordKind.STATE.value,
                    record_id=state.state_id,
                    predicate=state.predicate,
                    valid_start=state.valid_time.start_ordinal,
                    valid_end=state.valid_time.end_ordinal,
                    worldline=state.valid_time.worldline,
                    narrative_start=None,
                    narrative_end=None,
                    access_scope="writer_safe",
                    truth_class=state.truth_class.value,
                    entities=((state.subject_id, "subject"),),
                    payload=state.model_dump(mode="json"),
                )
            )
        for relation in world.relations:
            specs.append(
                _RecordSpec(
                    kind=WorldRecordKind.RELATION.value,
                    record_id=relation.relation_id,
                    predicate=relation.predicate,
                    valid_start=relation.valid_time.start_ordinal,
                    valid_end=relation.valid_time.end_ordinal,
                    worldline=relation.valid_time.worldline,
                    narrative_start=None,
                    narrative_end=None,
                    access_scope="writer_safe",
                    truth_class=relation.truth_class.value,
                    entities=((relation.subject_id, "subject"), (relation.object_id, "object")),
                    payload=relation.model_dump(mode="json"),
                )
            )
        for obligation in world.obligations:
            specs.append(
                _RecordSpec(
                    kind=WorldRecordKind.OBLIGATION.value,
                    record_id=obligation.obligation_id,
                    predicate=obligation.kind.value,
                    valid_start=None,
                    valid_end=None,
                    worldline="main",
                    narrative_start=None,
                    narrative_end=obligation.due_chapter,
                    access_scope="writer_safe",
                    truth_class=None,
                    entities=tuple((entity, "owner") for entity in obligation.owner_ids),
                    payload=obligation.model_dump(mode="json"),
                )
            )
        if plan is not None:
            for node in plan.nodes:
                specs.append(
                    _RecordSpec(
                        kind="plan_node",
                        record_id=node.plan_node_id,
                        predicate=node.node_type,
                        valid_start=None,
                        valid_end=None,
                        worldline="main",
                        narrative_start=None,
                        narrative_end=None,
                        access_scope="author_planning",
                        truth_class=None,
                        entities=(),
                        payload=node.model_dump(mode="json"),
                    )
                )
            for goal in plan.chapter_goals:
                specs.append(
                    _RecordSpec(
                        kind="chapter_goal",
                        record_id=goal.goal_id,
                        predicate="chapter_goal",
                        valid_start=None,
                        valid_end=None,
                        worldline="main",
                        narrative_start=goal.chapter_index,
                        narrative_end=goal.chapter_index,
                        access_scope="author_planning",
                        truth_class=None,
                        entities=(),
                        payload=goal.model_dump(mode="json"),
                    )
                )
        return tuple(specs)

    @staticmethod
    def _normalize_alias(value: str) -> str:
        return " ".join(value.casefold().split())

    @staticmethod
    def _evidence_refs(payload: dict[str, Any]) -> tuple[EvidenceRef, ...]:
        raw = payload.get("evidence_refs", [])
        if not isinstance(raw, list):
            return ()
        return tuple(EvidenceRef.model_validate(item) for item in raw if isinstance(item, dict))

    @staticmethod
    def _edge_matches_time(row: R1RecordRow, time_scope: StoryTime | None) -> bool:
        if time_scope is None:
            return True
        if row.worldline is not None and row.worldline != time_scope.worldline:
            return False
        ordinal = time_scope.start_ordinal
        if ordinal is None:
            return True
        return (row.valid_start is None or row.valid_start <= ordinal) and (
            row.valid_end is None or row.valid_end >= ordinal
        )

    @staticmethod
    def _validate_access_scopes(access_scopes: tuple[str, ...]) -> None:
        if not access_scopes or any(not scope for scope in access_scopes):
            raise ValueError("R1 retrieval requires at least one non-empty access scope")
        if len(access_scopes) != len(set(access_scopes)):
            raise ValueError("R1 retrieval access scopes must be unique")


class R1RetrievalBackend:
    def __init__(
        self,
        repository: R1WorldRepository,
        *,
        snapshot_id: StableId,
        graph_depth: int = 2,
        access_scopes: tuple[str, ...] = ("writer_safe",),
    ) -> None:
        if graph_depth < 1:
            raise ValueError("graph depth must be positive")
        R1WorldRepository._validate_access_scopes(access_scopes)
        self._repository = repository
        self._snapshot_id = snapshot_id
        self._graph_depth = graph_depth
        self._access_scopes = access_scopes

    def search(
        self, need: Stage1MemoryNeed, channel: RetrievalChannel, limit: int
    ) -> tuple[ChannelHit, ...]:
        bundle = compile_need_query(need)
        if channel is RetrievalChannel.TYPED_GRAPH:
            records = self._repository.typed_graph(
                need.base_commit,
                bundle.graph_seeds,
                max_depth=self._graph_depth,
                limit=limit,
                allowed_predicates=bundle.exact_predicates,
                time_scope=bundle.time_scope,
                access_scopes=self._visible_access_scopes(need),
            )
        elif channel in {RetrievalChannel.R1_EXACT, RetrievalChannel.R1_TEMPORAL}:
            # The R1 repository derives its exact query from the same fields
            # the bundle compiles (exact_entity_ids / exact_predicates), so
            # the two stay identical by construction.
            records = self._repository.exact(
                need,
                temporal=channel is RetrievalChannel.R1_TEMPORAL,
                limit=limit,
                access_scopes=self._visible_access_scopes(need),
            )
        else:
            raise ValueError(f"unsupported R1 retrieval channel: {channel.value}")
        count = len(records)
        return tuple(
            ChannelHit(
                unit=self._unit(record),
                channel=channel,
                channel_rank=rank,
                raw_score=float(count - rank + 1),
                candidate_count=count,
                hit_reason=(
                    "bounded_typed_graph_path"
                    if channel is RetrievalChannel.TYPED_GRAPH
                    else "postgresql_versioned_temporal_match"
                    if channel is RetrievalChannel.R1_TEMPORAL
                    else "postgresql_versioned_exact_match"
                ),
            )
            for rank, record in enumerate(records, start=1)
        )

    def _visible_access_scopes(self, need: Stage1MemoryNeed) -> tuple[str, ...]:
        required = {
            "writer_safe": ("writer_safe",),
            "author_planning": ("writer_safe", "author_planning"),
            "evaluator": ("writer_safe", "author_planning", "evaluator"),
        }.get(need.access_scope)
        if required is None:
            raise ValueError(f"unsupported retrieval access scope: {need.access_scope}")
        visible = tuple(scope for scope in required if scope in self._access_scopes)
        if not visible:
            raise ValueError("R1 backend cannot satisfy the need access scope")
        return visible

    def _unit(self, record: R1RecordView) -> RetrievalUnit:
        kind = {
            WorldRecordKind.STATE.value: RetrievalUnitKind.STATE_ANCHOR,
            WorldRecordKind.EVENT.value: RetrievalUnitKind.EVENT_ANCHOR,
            WorldRecordKind.RELATION.value: RetrievalUnitKind.RELATION_ANCHOR,
            WorldRecordKind.OBLIGATION.value: RetrievalUnitKind.PLAN_ANCHOR,
            WorldRecordKind.ENTITY.value: RetrievalUnitKind.FACT_ANCHOR,
            "plan_node": RetrievalUnitKind.PLAN_ANCHOR,
            "chapter_goal": RetrievalUnitKind.PLAN_ANCHOR,
        }[record.record_kind]
        raw_evidence = record.record.get("evidence_refs", [])
        if not isinstance(raw_evidence, list):
            raise ValueError("R1 evidence_refs must be a list")
        evidence = tuple(
            EvidenceRef.model_validate_json(json.dumps(item))
            for item in raw_evidence
            if isinstance(item, dict)
        )
        raw_value = record.record.get("value")
        canonical_value = (
            CanonicalAliasRegistry().resolve(record.predicate, raw_value)
            if kind is RetrievalUnitKind.STATE_ANCHOR
            and record.predicate is not None
            and isinstance(raw_value, str)
            else None
        )
        return RetrievalUnit(
            unit_id=StableId(f"unit.{record.row_id.root}"),
            unit_kind=kind,
            source_commit=record.source_commit,
            snapshot_id=self._snapshot_id,
            text=self._record_text(record),
            entity_ids=record.entity_ids,
            predicate=record.predicate,
            canonical_value_id=(
                None if canonical_value is None else canonical_value.canonical_value_id
            ),
            canonicalizer_version=(
                None if canonical_value is None else canonical_value.canonicalizer_version
            ),
            worldline=record.worldline or "main",
            narrative_start=record.narrative_start,
            narrative_end=record.narrative_end,
            story_time_start=record.valid_start,
            story_time_end=record.valid_end,
            truth_class=(None if record.truth_class is None else TruthClass(record.truth_class)),
            access_scope=record.access_scope,
            information_label=(
                "plan" if record.record_kind in {"plan_node", "chapter_goal"} else "observed"
            ),
            evidence_refs=evidence,
            mandatory=record.record_kind == WorldRecordKind.OBLIGATION.value,
        )

    @staticmethod
    def _record_text(record: R1RecordView) -> str:
        payload = record.record
        if record.record_kind == WorldRecordKind.STATE.value:
            subject = str(payload.get("subject_id", "unknown-subject"))
            predicate = str(payload.get("predicate", record.predicate or "state"))
            value = json.dumps(payload.get("value"), ensure_ascii=False, sort_keys=True)
            return f"{subject} {predicate} {value}"
        if record.record_kind == WorldRecordKind.RELATION.value:
            return " ".join(
                (
                    str(payload.get("subject_id", "unknown-subject")),
                    str(payload.get("predicate", record.predicate or "relation")),
                    str(payload.get("object_id", "unknown-object")),
                )
            )
        if record.record_kind == WorldRecordKind.EVENT.value:
            participants = payload.get("participant_ids", ())
            prefix = (
                " ".join(str(value) for value in participants)
                if isinstance(participants, list)
                else ""
            )
            return " ".join(
                value
                for value in (
                    prefix,
                    str(payload.get("event_type", record.predicate or "event")),
                )
                if value
            )
        if record.record_kind == WorldRecordKind.OBLIGATION.value:
            return str(payload.get("description", record.record_id.root))
        return str(
            payload.get(
                "summary",
                payload.get("internal_label", payload.get("title", record.record_id.root)),
            )
        )
