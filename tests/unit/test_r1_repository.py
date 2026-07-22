from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import R1RecordEntityRow, R1RecordRow
from novel_agent.domain.ids import CommitId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    RetrievalChannel,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.world import Entity, RelationRecord, StoryTime, TruthClass
from novel_agent.services.commits import CommitService
from novel_agent.services.r1 import R1RetrievalBackend, R1WorldRepository
from tests.factories import make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


@pytest.fixture
def r1_database() -> Iterator[tuple[Engine, sessionmaker[Session], CommitId]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commit_id = CommitService(factory).initialize_project(make_manifest())
    yield engine, factory, commit_id
    engine.dispose()


def _need(
    commit_id: CommitId,
    intent: Stage1QueryIntent,
    *,
    entities: tuple[StableId, ...] = (),
    predicates: tuple[str, ...] = (),
    time: int | None = None,
) -> Stage1MemoryNeed:
    pool = (
        CandidatePool.GRAPH
        if intent in {Stage1QueryIntent.RELATION_CHAIN, Stage1QueryIntent.CAUSAL_MULTI_HOP}
        else CandidatePool.R1
    )
    return Stage1MemoryNeed(
        need_id=StableId(f"need.r1.{intent.value}"),
        run_id=RunId("run.r1"),
        task_id=TaskId("task.r1"),
        base_commit=commit_id,
        chapter_target=21,
        need_type=intent.value,
        query_intent=intent,
        query_text="林澈 injury",
        entity_ids=entities,
        predicates=predicates,
        time_scope=None if time is None else StoryTime(worldline="main", start_ordinal=time),
        why_needed="test versioned R1 lookup",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=(
            ResolutionPath.TYPED_GRAPH
            if pool is CandidatePool.GRAPH
            else ResolutionPath.EXACT_TEMPORAL
        ),
        allowed_candidate_pools=(pool,),
        stop_condition="bounded lookup complete",
    )


def test_r1_materialization_is_versioned_idempotent_and_queryable(
    r1_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    _, factory, commit_id = r1_database
    world = make_synthetic_bundle().world_roots[0]
    repository = R1WorldRepository(factory)

    assert repository.materialize(ProjectId("project.test"), commit_id, world) == 4
    assert repository.materialize(ProjectId("project.test"), commit_id, world) == 4
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(R1RecordRow)) == 4
        assert session.scalar(select(func.count()).select_from(R1RecordEntityRow)) == 4

    entity_id = world.entities[0].entity_id
    current = repository.exact(
        _need(
            commit_id,
            Stage1QueryIntent.CURRENT_STATE,
            entities=(entity_id,),
            predicates=("injury",),
            time=20,
        ),
        temporal=True,
        limit=10,
    )
    assert len(current) == 1
    assert current[0].record_id == world.states[0].state_id
    assert current[0].source_commit == commit_id
    assert current[0].entity_ids == (entity_id,)
    assert (
        repository.exact(
            _need(
                commit_id,
                Stage1QueryIntent.CURRENT_STATE,
                entities=(entity_id,),
                predicates=("injury",),
                time=19,
            ),
            temporal=True,
            limit=10,
        )
        == ()
    )
    unbounded_time = _need(
        commit_id,
        Stage1QueryIntent.CURRENT_STATE,
        entities=(entity_id,),
        predicates=("injury",),
    ).model_copy(update={"time_scope": StoryTime(worldline="main", label="unknown")})
    assert len(repository.exact(unbounded_time, temporal=True, limit=10)) == 1

    assert (
        len(
            repository.exact(
                _need(commit_id, Stage1QueryIntent.KNOWN_ID, entities=(entity_id,)),
                temporal=False,
                limit=10,
            )
        )
        == 4
    )
    assert (
        len(
            repository.exact(
                _need(commit_id, Stage1QueryIntent.PLAN_NODE), temporal=False, limit=10
            )
        )
        == 1
    )
    assert (
        len(
            repository.exact(
                _need(commit_id, Stage1QueryIntent.MANDATORY_CONSTRAINT),
                temporal=False,
                limit=10,
            )
        )
        == 2
    )
    with pytest.raises(ValueError, match="positive"):
        repository.exact(_need(commit_id, Stage1QueryIntent.KNOWN_ID), temporal=False, limit=0)


def test_r1_backend_returns_typed_traceable_units(
    r1_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    _, factory, commit_id = r1_database
    world = make_synthetic_bundle().world_roots[0]
    repository = R1WorldRepository(factory)
    repository.materialize(ProjectId("project.test"), commit_id, world)
    backend = R1RetrievalBackend(repository, snapshot_id=StableId("snapshot.r1"))
    need = _need(
        commit_id,
        Stage1QueryIntent.CURRENT_STATE,
        entities=(world.entities[0].entity_id,),
        predicates=("injury",),
        time=20,
    )

    exact = backend.search(need, RetrievalChannel.R1_EXACT, 5)
    temporal = backend.search(need, RetrievalChannel.R1_TEMPORAL, 5)
    assert exact[0].hit_reason == "postgresql_versioned_exact_match"
    assert temporal[0].hit_reason == "postgresql_versioned_temporal_match"
    assert temporal[0].unit.source_commit == commit_id
    assert temporal[0].unit.snapshot_id == StableId("snapshot.r1")
    assert temporal[0].unit.evidence_refs == world.states[0].evidence_refs
    with pytest.raises(ValueError, match="unsupported"):
        backend.search(need, RetrievalChannel.ANCHOR_BM25, 5)
    with pytest.raises(ValueError, match="graph depth"):
        R1RetrievalBackend(repository, snapshot_id=StableId("snapshot.r1"), graph_depth=0)
    with factory.begin() as session:
        state_row = session.scalar(select(R1RecordRow).where(R1RecordRow.record_kind == "state"))
        assert state_row is not None
        corrupt = dict(state_row.record_json)
        corrupt["evidence_refs"] = "invalid"
        state_row.record_json = corrupt
    with pytest.raises(ValueError, match="evidence_refs"):
        backend.search(need, RetrievalChannel.R1_EXACT, 5)


def test_bounded_typed_graph_uses_versioned_relation_edges(
    r1_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    _, factory, commit_id = r1_database
    base = make_synthetic_bundle().world_roots[0]
    tower = Entity(
        entity_id=StableId("entity.synthetic.north-tower"),
        entity_type="location",
        internal_label="北塔",
    )
    guild = Entity(
        entity_id=StableId("entity.synthetic.guild"),
        entity_type="organization",
        internal_label="守塔会",
    )
    relations = (
        RelationRecord(
            relation_id=StableId("relation.synthetic.located"),
            predicate="located_at",
            subject_id=base.entities[0].entity_id,
            object_id=tower.entity_id,
            valid_time=StoryTime(worldline="main", start_ordinal=21),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
        RelationRecord(
            relation_id=StableId("relation.synthetic.owned"),
            predicate="owned_by",
            subject_id=tower.entity_id,
            object_id=guild.entity_id,
            valid_time=StoryTime(worldline="main", start_ordinal=1),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
    )
    world = base.model_copy(
        update={"entities": (*base.entities, tower, guild), "relations": relations}
    )
    repository = R1WorldRepository(factory)
    repository.materialize(ProjectId("project.test"), commit_id, world)

    assert repository.typed_graph(commit_id, (), max_depth=2, limit=10) == ()
    graph = repository.typed_graph(commit_id, (base.entities[0].entity_id,), max_depth=2, limit=10)
    assert {record.record_id for record in graph} == {
        relation.relation_id for relation in relations
    }
    backend = R1RetrievalBackend(repository, snapshot_id=StableId("snapshot.graph"), graph_depth=2)
    hits = backend.search(
        _need(
            commit_id,
            Stage1QueryIntent.RELATION_CHAIN,
            entities=(base.entities[0].entity_id,),
        ),
        RetrievalChannel.TYPED_GRAPH,
        10,
    )
    assert len(hits) == 2
    assert {hit.hit_reason for hit in hits} == {"bounded_typed_graph_path"}
    paths = repository.typed_graph_paths(
        commit_id,
        (base.entities[0].entity_id,),
        max_depth=2,
        limit=10,
        time_scope=StoryTime(worldline="main", start_ordinal=21),
    )
    assert any(path.relation_ids == tuple(item.relation_id for item in relations) for path in paths)
    assert all(path.edge_semantics == ("canonical",) * len(path.relation_ids) for path in paths)
    assert (
        repository.typed_graph_paths(
            commit_id,
            (base.entities[0].entity_id,),
            max_depth=2,
            limit=10,
            time_scope=StoryTime(worldline="main", start_ordinal=20),
        )
        == ()
    )
    with pytest.raises(ValueError, match="canonical/evidence"):
        repository.typed_graph_paths(
            commit_id,
            (base.entities[0].entity_id,),
            max_depth=2,
            limit=10,
            allowed_edge_semantics=("inferred",),
        )
    with pytest.raises(ValueError, match="positive"):
        repository.typed_graph(commit_id, (base.entities[0].entity_id,), max_depth=0, limit=10)


def test_r1_materializes_plan_nodes_and_exposes_exact_alias_and_evidence_reverse_lookups(
    r1_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    _, factory, commit_id = r1_database
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    plan = bundle.plan_roots[0]
    repository = R1WorldRepository(factory)

    repository.materialize(ProjectId("project.test"), commit_id, world, plan)

    assert repository.resolve_entity_alias(commit_id, world.entities[0].internal_label) == (
        world.entities[0].entity_id,
    )
    assert repository.resolve_entity_alias(commit_id, "missing") == ()
    if world.states[0].evidence_refs:
        reverse = repository.records_for_evidence(
            commit_id, world.states[0].evidence_refs[0].evidence_id
        )
        assert world.states[0].state_id in {item.record_id for item in reverse}
    plan_records = repository.exact(
        _need(commit_id, Stage1QueryIntent.PLAN_NODE).model_copy(
            update={"access_scope": "author_planning", "allow_plan": True}
        ),
        temporal=False,
        limit=20,
        access_scopes=("writer_safe", "author_planning"),
    )
    assert {item.record_id for item in plan_records} >= {item.plan_node_id for item in plan.nodes}
