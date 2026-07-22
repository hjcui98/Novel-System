"""Versioned PostgreSQL R1 materialization, exact/temporal lookup, and bounded graph traversal."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, and_, delete, exists, literal, or_, select
from sqlalchemy.orm import Session, aliased, sessionmaker

from novel_agent.adapters.postgres.models import R1RecordEntityRow, R1RecordRow
from novel_agent.domain.changes import WorldRecordKind
from novel_agent.domain.ids import CommitId, ProjectId, StableId
from novel_agent.domain.memory import (
    ChannelHit,
    R1RecordView,
    RetrievalChannel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    WorldRootDocument,
)
from novel_agent.domain.text import EvidenceRef


@dataclass(frozen=True, slots=True)
class _RecordSpec:
    kind: WorldRecordKind
    record_id: StableId
    predicate: str | None
    valid_start: int | None
    valid_end: int | None
    truth_class: str | None
    entities: tuple[tuple[StableId, str], ...]
    payload: dict[str, Any]


class R1WorldRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def materialize(
        self,
        project_id: ProjectId,
        canonical_commit: CommitId,
        world: WorldRootDocument,
    ) -> int:
        specs = self._specs(world)
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
                        record_kind=spec.kind.value,
                        record_id=spec.record_id.root,
                        predicate=spec.predicate,
                        valid_start=spec.valid_start,
                        valid_end=spec.valid_end,
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

    def exact(
        self,
        need: Stage1MemoryNeed,
        *,
        temporal: bool,
        limit: int,
    ) -> tuple[R1RecordView, ...]:
        if limit < 1:
            raise ValueError("R1 limit must be positive")
        statement: Select[tuple[R1RecordRow]] = select(R1RecordRow).where(
            R1RecordRow.source_commit == need.base_commit.root
        )
        kinds = self._kinds_for_need(need)
        if kinds:
            statement = statement.where(R1RecordRow.record_kind.in_(kinds))
        if need.predicates:
            statement = statement.where(R1RecordRow.predicate.in_(need.predicates))
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
            if ordinal is not None:
                statement = statement.where(
                    or_(R1RecordRow.valid_start.is_(None), R1RecordRow.valid_start <= ordinal),
                    or_(R1RecordRow.valid_end.is_(None), R1RecordRow.valid_end >= ordinal),
                )
        statement = statement.order_by(
            R1RecordRow.valid_start.desc().nullslast(),
            R1RecordRow.record_kind,
            R1RecordRow.record_id,
        ).limit(limit)
        with self._session_factory() as session:
            rows = tuple(session.scalars(statement))
            return self._views(session, rows)

    def typed_graph(
        self,
        source_commit: CommitId,
        entity_ids: tuple[StableId, ...],
        *,
        max_depth: int,
        limit: int,
    ) -> tuple[R1RecordView, ...]:
        if max_depth < 1 or limit < 1:
            raise ValueError("graph depth and limit must be positive")
        if not entity_ids:
            return ()
        subject = aliased(R1RecordEntityRow)
        object_ = aliased(R1RecordEntityRow)
        edges = (
            select(
                R1RecordRow.row_id.label("row_id"),
                subject.entity_id.label("src"),
                object_.entity_id.label("dst"),
            )
            .join(subject, and_(subject.row_id == R1RecordRow.row_id, subject.role == "subject"))
            .join(object_, and_(object_.row_id == R1RecordRow.row_id, object_.role == "object"))
            .where(
                R1RecordRow.source_commit == source_commit.root,
                R1RecordRow.record_kind == WorldRecordKind.RELATION.value,
            )
            .subquery()
        )
        starts = tuple(entity.root for entity in entity_ids)
        walk = select(
            edges.c.row_id,
            edges.c.src,
            edges.c.dst,
            literal(1).label("depth"),
        ).where(or_(edges.c.src.in_(starts), edges.c.dst.in_(starts)))
        graph = walk.cte("r1_graph", recursive=True)
        graph = graph.union_all(
            select(
                edges.c.row_id,
                edges.c.src,
                edges.c.dst,
                (graph.c.depth + 1).label("depth"),
            ).where(
                graph.c.depth < max_depth,
                or_(
                    edges.c.src == graph.c.dst,
                    edges.c.dst == graph.c.src,
                    edges.c.src == graph.c.src,
                    edges.c.dst == graph.c.dst,
                ),
            )
        )
        row_ids = select(graph.c.row_id).distinct().limit(limit)
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(R1RecordRow)
                    .where(R1RecordRow.row_id.in_(row_ids))
                    .order_by(R1RecordRow.record_id)
                )
            )
            return self._views(session, rows)

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
            return (WorldRecordKind.OBLIGATION.value,)
        if need.query_intent.value == "mandatory_constraint":
            return (WorldRecordKind.STATE.value, WorldRecordKind.OBLIGATION.value)
        return ()

    @staticmethod
    def _row_id(commit: CommitId, kind: WorldRecordKind, record_id: StableId) -> StableId:
        digest = hashlib.sha256(
            f"{commit.root}\0{kind.value}\0{record_id.root}".encode()
        ).hexdigest()
        return StableId(f"r1.{digest}")

    @staticmethod
    def _specs(world: WorldRootDocument) -> tuple[_RecordSpec, ...]:
        specs: list[_RecordSpec] = []
        for entity in world.entities:
            specs.append(
                _RecordSpec(
                    WorldRecordKind.ENTITY,
                    entity.entity_id,
                    None,
                    None,
                    None,
                    None,
                    ((entity.entity_id, "self"),),
                    entity.model_dump(mode="json"),
                )
            )
        for event in world.events:
            specs.append(
                _RecordSpec(
                    WorldRecordKind.EVENT,
                    event.event_id,
                    event.event_type,
                    None if event.story_time is None else event.story_time.start_ordinal,
                    None if event.story_time is None else event.story_time.end_ordinal,
                    event.truth_class.value,
                    tuple((entity, "participant") for entity in event.participant_ids),
                    event.model_dump(mode="json"),
                )
            )
        for state in world.states:
            specs.append(
                _RecordSpec(
                    WorldRecordKind.STATE,
                    state.state_id,
                    state.predicate,
                    state.valid_time.start_ordinal,
                    state.valid_time.end_ordinal,
                    state.truth_class.value,
                    ((state.subject_id, "subject"),),
                    state.model_dump(mode="json"),
                )
            )
        for relation in world.relations:
            specs.append(
                _RecordSpec(
                    WorldRecordKind.RELATION,
                    relation.relation_id,
                    relation.predicate,
                    relation.valid_time.start_ordinal,
                    relation.valid_time.end_ordinal,
                    relation.truth_class.value,
                    ((relation.subject_id, "subject"), (relation.object_id, "object")),
                    relation.model_dump(mode="json"),
                )
            )
        for obligation in world.obligations:
            specs.append(
                _RecordSpec(
                    WorldRecordKind.OBLIGATION,
                    obligation.obligation_id,
                    obligation.kind.value,
                    None,
                    obligation.due_chapter,
                    None,
                    tuple((entity, "owner") for entity in obligation.owner_ids),
                    obligation.model_dump(mode="json"),
                )
            )
        return tuple(specs)


class R1RetrievalBackend:
    def __init__(
        self,
        repository: R1WorldRepository,
        *,
        snapshot_id: StableId,
        graph_depth: int = 2,
    ) -> None:
        if graph_depth < 1:
            raise ValueError("graph depth must be positive")
        self._repository = repository
        self._snapshot_id = snapshot_id
        self._graph_depth = graph_depth

    def search(
        self, need: Stage1MemoryNeed, channel: RetrievalChannel, limit: int
    ) -> tuple[ChannelHit, ...]:
        if channel is RetrievalChannel.TYPED_GRAPH:
            records = self._repository.typed_graph(
                need.base_commit,
                need.entity_ids,
                max_depth=self._graph_depth,
                limit=limit,
            )
        elif channel in {RetrievalChannel.R1_EXACT, RetrievalChannel.R1_TEMPORAL}:
            records = self._repository.exact(
                need,
                temporal=channel is RetrievalChannel.R1_TEMPORAL,
                limit=limit,
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

    def _unit(self, record: R1RecordView) -> RetrievalUnit:
        kind = {
            WorldRecordKind.STATE.value: RetrievalUnitKind.STATE_ANCHOR,
            WorldRecordKind.EVENT.value: RetrievalUnitKind.EVENT_ANCHOR,
            WorldRecordKind.RELATION.value: RetrievalUnitKind.RELATION_ANCHOR,
            WorldRecordKind.OBLIGATION.value: RetrievalUnitKind.PLAN_ANCHOR,
            WorldRecordKind.ENTITY.value: RetrievalUnitKind.FACT_ANCHOR,
        }[record.record_kind]
        raw_evidence = record.record.get("evidence_refs", [])
        if not isinstance(raw_evidence, list):
            raise ValueError("R1 evidence_refs must be a list")
        evidence = tuple(
            EvidenceRef.model_validate_json(json.dumps(item))
            for item in raw_evidence
            if isinstance(item, dict)
        )
        return RetrievalUnit(
            unit_id=StableId(f"unit.{record.row_id.root}"),
            unit_kind=kind,
            source_commit=record.source_commit,
            snapshot_id=self._snapshot_id,
            text=json.dumps(record.record, ensure_ascii=False, sort_keys=True),
            entity_ids=record.entity_ids,
            evidence_refs=evidence,
            mandatory=record.record_kind
            in {
                WorldRecordKind.STATE.value,
                WorldRecordKind.OBLIGATION.value,
            },
        )
