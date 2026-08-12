from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import R1RecordEntityRow, R1RecordRow
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    NeedRisk,
    R1RecordView,
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
                _need(
                    commit_id,
                    Stage1QueryIntent.MANDATORY_CONSTRAINT,
                    entities=(entity_id,),
                ),
                temporal=False,
                limit=10,
            )
        )
        == 2
    )
    with pytest.raises(ValueError, match="positive"):
        repository.exact(_need(commit_id, Stage1QueryIntent.KNOWN_ID), temporal=False, limit=0)
    with pytest.raises(ValueError, match="unfiltered factual exact"):
        repository.exact(_need(commit_id, Stage1QueryIntent.KNOWN_ID), temporal=False, limit=1)
    assert repository.exact(
        _need(
            commit_id,
            Stage1QueryIntent.KNOWN_ID,
            entities=(entity_id,),
        ).model_copy(update={"chapter_target": None}),
        temporal=False,
        limit=1,
    )


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
    assert temporal[0].unit.mandatory is False
    assert temporal[0].unit.text.startswith(world.states[0].subject_id.root)
    assert not temporal[0].unit.text.startswith("{")
    with pytest.raises(ValueError, match="unsupported"):
        backend.search(need, RetrievalChannel.ANCHOR_BM25, 5)
    with pytest.raises(ValueError, match="graph depth"):
        R1RetrievalBackend(repository, snapshot_id=StableId("snapshot.r1"), graph_depth=0)
    with pytest.raises(ValueError, match="non-empty access scope"):
        R1RetrievalBackend(
            repository,
            snapshot_id=StableId("snapshot.r1"),
            access_scopes=(),
        )
    with pytest.raises(ValueError, match="unique"):
        R1RetrievalBackend(
            repository,
            snapshot_id=StableId("snapshot.r1"),
            access_scopes=("writer_safe", "writer_safe"),
        )
    with pytest.raises(ValueError, match="unsupported retrieval access scope"):
        backend.search(
            need.model_copy(update={"access_scope": "unknown"}),
            RetrievalChannel.R1_EXACT,
            5,
        )
    author_only = R1RetrievalBackend(
        repository,
        snapshot_id=StableId("snapshot.r1"),
        access_scopes=("author_planning",),
    )
    with pytest.raises(ValueError, match="cannot satisfy"):
        author_only.search(need, RetrievalChannel.R1_EXACT, 5)
    with factory.begin() as session:
        state_row = session.scalar(select(R1RecordRow).where(R1RecordRow.record_kind == "state"))
        assert state_row is not None
        corrupt = dict(state_row.record_json)
        corrupt["evidence_refs"] = "invalid"
        state_row.record_json = corrupt
    with pytest.raises(ValueError, match="evidence_refs"):
        backend.search(need, RetrievalChannel.R1_EXACT, 5)


def test_r1_record_text_renders_each_canonical_kind() -> None:
    base = R1RecordView(
        row_id=StableId("r1.test"),
        source_commit=CommitId("sha256:" + "1" * 64),
        record_kind="event",
        record_id=StableId("record.test"),
        predicate="fallback",
        entity_ids=(),
        record={},
    )

    assert (
        R1RetrievalBackend._record_text(
            base.model_copy(
                update={
                    "record": {
                        "participant_ids": ["entity.a", "entity.b"],
                        "event_type": "arrival",
                    }
                }
            )
        )
        == "entity.a entity.b arrival"
    )
    assert (
        R1RetrievalBackend._record_text(
            base.model_copy(update={"record": {"participant_ids": "invalid"}})
        )
        == "fallback"
    )
    assert (
        R1RetrievalBackend._record_text(
            base.model_copy(
                update={
                    "record_kind": "obligation",
                    "record": {"description": "keep the promise"},
                }
            )
        )
        == "keep the promise"
    )
    assert (
        R1RetrievalBackend._record_text(
            base.model_copy(
                update={
                    "record_kind": "relation",
                    "record": {
                        "subject_id": "entity.a",
                        "predicate": "trusts",
                        "object_id": "entity.b",
                    },
                }
            )
        )
        == "entity.a trusts entity.b"
    )
    assert (
        R1RetrievalBackend._record_text(
            base.model_copy(
                update={
                    "record_kind": "plan_node",
                    "record": {"summary": "future intent"},
                }
            )
        )
        == "future intent"
    )


def test_r1_current_state_keeps_canonical_assertions_retrievable(
    r1_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    _, factory, commit_id = r1_database
    world = make_synthetic_bundle().world_roots[0]
    assertion = world.states[0].model_copy(update={"truth_class": TruthClass.ASSERTION})
    world = world.model_copy(update={"states": (assertion,)})
    repository = R1WorldRepository(factory)
    repository.materialize(ProjectId("project.test"), commit_id, world)

    current = repository.exact(
        _need(
            commit_id,
            Stage1QueryIntent.CURRENT_STATE,
            entities=(assertion.subject_id,),
            predicates=(assertion.predicate,),
        ),
        temporal=False,
        limit=5,
    )

    assert tuple(item.record_id for item in current) == (assertion.state_id,)


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
            evidence_refs=base.obligations[0].evidence_refs,
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
        RelationRecord(
            relation_id=StableId("relation.synthetic.owned"),
            predicate="owned_by",
            subject_id=tower.entity_id,
            object_id=guild.entity_id,
            valid_time=StoryTime(worldline="main", start_ordinal=1),
            evidence_refs=base.obligations[0].evidence_refs,
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
        RelationRecord(
            relation_id=StableId("relation.synthetic.asserted"),
            predicate="suspects",
            subject_id=base.entities[0].entity_id,
            object_id=guild.entity_id,
            valid_time=StoryTime(worldline="main", start_ordinal=1),
            truth_class=TruthClass.ASSERTION,
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
        relation.relation_id
        for relation in relations
        if relation.truth_class is TruthClass.ACCEPTED_WORLD_FACT
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
    accepted_relation_ids = tuple(
        item.relation_id for item in relations if item.truth_class is TruthClass.ACCEPTED_WORLD_FACT
    )
    assert any(path.relation_ids == accepted_relation_ids for path in paths)
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
    assert (
        repository.typed_graph_paths(
            commit_id,
            (base.entities[0].entity_id,),
            max_depth=2,
            limit=10,
            allowed_edge_semantics=("evidence",),
        )
        == ()
    )
    assert (
        repository.typed_graph_paths(
            commit_id,
            (base.entities[0].entity_id,),
            max_depth=2,
            limit=10,
            allowed_predicates=("not-present",),
        )
        == ()
    )
    assert (
        len(
            repository.typed_graph_paths(
                commit_id,
                (base.entities[0].entity_id,),
                max_depth=2,
                limit=1,
            )
        )
        == 1
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
    with factory.begin() as session:
        relation_row = session.scalar(
            select(R1RecordRow).where(R1RecordRow.record_id == relations[0].relation_id.root)
        )
        assert relation_row is not None
        association = session.scalar(
            select(R1RecordEntityRow).where(
                R1RecordEntityRow.row_id == relation_row.row_id,
                R1RecordEntityRow.role == "object",
            )
        )
        assert association is not None
        session.delete(association)
    assert (
        repository.typed_graph_paths(
            commit_id,
            (base.entities[0].entity_id,),
            max_depth=2,
            limit=10,
        )
        == ()
    )


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
    assert repository.resolve_entity_alias(
        commit_id, world.entities[0].internal_label, limit=1
    ) == (world.entities[0].entity_id,)
    assert repository.resolve_entity_alias(commit_id, "missing") == ()
    with pytest.raises(ValueError, match="non-empty alias"):
        repository.resolve_entity_alias(commit_id, " ")
    with pytest.raises(ValueError, match="must be positive"):
        repository.records_for_evidence(commit_id, StableId("evidence.any"), limit=0)
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


def test_r1_evidence_and_graph_time_helpers_fail_closed(
    r1_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    _, factory, commit_id = r1_database
    world = make_synthetic_bundle().world_roots[0]
    repository = R1WorldRepository(factory)
    repository.materialize(ProjectId("project.test"), commit_id, world)
    assert repository._evidence_refs({"evidence_refs": "invalid"}) == ()
    with factory.begin() as session:
        row = session.scalar(select(R1RecordRow).where(R1RecordRow.record_kind == "state"))
        assert row is not None
        payload = dict(row.record_json)
        payload["evidence_refs"] = "invalid"
        row.record_json = payload

    assert repository.records_for_evidence(commit_id, StableId("evidence.any")) == ()
    with factory() as session:
        row = session.scalar(select(R1RecordRow).where(R1RecordRow.record_kind == "state"))
        assert row is not None
        row.worldline = "alternate"
        assert (
            repository._edge_matches_time(
                row,
                StoryTime(worldline="main", start_ordinal=20),
            )
            is False
        )
        row.worldline = "main"
        assert (
            repository._edge_matches_time(
                row,
                StoryTime(worldline="main", label="unknown"),
            )
            is True
        )


def test_validate_graph_path_receipts_typed_dereference_failures(
    r1_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    """Graph path receipts fail closed on every dereference branch (Round 1/2)."""
    from novel_agent.domain.memory import GraphPathDereferenceStatus
    from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
    from novel_agent.services.artifacts import sha256_id
    from novel_agent.services.content_addressing import quote_hash
    from novel_agent.services.r1 import R1WorldRepository as R

    _, factory, commit_id = r1_database
    bundle = make_synthetic_bundle()
    base = bundle.world_roots[0]
    text = bundle.text_roots[0]
    block = text.chapters[0].scenes[0].blocks[0]
    span = TextSpanRef(block_id=block.block_id, start=0, end=8)
    evidence = EvidenceRef(
        evidence_id=StableId("evidence.path-1"),
        root_hash=text.root_hash,
        object_hash=sha256_id(block.text.encode("utf-8")),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=span,
        quote_hash=quote_hash(block.text[0:8]),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=commit_id,
    )
    tower = Entity(
        entity_id=StableId("entity.synthetic.tower"),
        entity_type="location",
        internal_label="北塔",
    )
    relation = RelationRecord(
        relation_id=StableId("relation.synthetic.path"),
        predicate="located_at",
        subject_id=base.entities[0].entity_id,
        object_id=tower.entity_id,
        valid_time=StoryTime(worldline="main", start_ordinal=21),
        evidence_refs=(evidence,),
        truth_class=TruthClass.ACCEPTED_WORLD_FACT,
    )
    world = base.model_copy(
        update={
            "entities": (*base.entities, tower),
            "relations": (*base.relations, relation),
        }
    )
    repository = R1WorldRepository(factory)
    repository.materialize(ProjectId("project.test"), commit_id, world)
    paths = repository.typed_graph_paths(
        commit_id, (base.entities[0].entity_id,), max_depth=2, limit=10
    )
    assert any(relation.relation_id in path.relation_ids for path in paths)
    # Empty receipts short-circuit.
    assert repository.validate_graph_path_receipts((), text) == ()
    # A consistent receipt validates to L0_VERIFIED.
    single = next(path for path in paths if path.relation_ids == (relation.relation_id,))
    verified = repository.validate_graph_path_receipts((single,), text)
    assert verified[0].dereference_status is GraphPathDereferenceStatus.L0_VERIFIED
    # Mutating fields outside the path identity (relation ids) fails dereference.
    forged_row_ids = (StableId("row.missing"),)
    forged = single.model_copy(
        update={
            "relation_row_ids": forged_row_ids,
            "path_id": repository._graph_path_id(
                single.source_commit,
                single.snapshot_id,
                forged_row_ids,
                single.entity_path,
                single.directions,
            ),
        }
    )
    with pytest.raises(ValueError, match="does not dereference to its relation row"):
        repository.validate_graph_path_receipts((forged,), text)
    # Swapping the entity path keeps identity but breaks endpoint roles.
    reversed_path = single.model_copy(
        update={
            "entity_path": tuple(reversed(single.entity_path)),
            "path_id": repository._graph_path_id(
                single.source_commit,
                single.snapshot_id,
                single.relation_row_ids,
                tuple(reversed(single.entity_path)),
                single.directions,
            ),
        }
    )
    with pytest.raises(ValueError, match="endpoint roles do not match"):
        repository.validate_graph_path_receipts((reversed_path,), text)
    reverse_entities = tuple(reversed(single.entity_path))
    reverse_directions = ("reverse",)
    valid_reverse = single.model_copy(
        update={
            "entity_path": reverse_entities,
            "directions": reverse_directions,
            "path_id": repository._graph_path_id(
                single.source_commit,
                single.snapshot_id,
                single.relation_row_ids,
                reverse_entities,
                reverse_directions,
            ),
        }
    )
    reverse_verified = repository.validate_graph_path_receipts((valid_reverse,), text)
    assert reverse_verified[0].dereference_status is GraphPathDereferenceStatus.L0_VERIFIED
    # An identity mismatch is caught before any row work.
    with pytest.raises(ValueError, match="identity does not match"):
        repository.validate_graph_path_receipts(
            (single.model_copy(update={"directions": ("reverse",)}),), text
        )
    # Stripping evidence from the relation row makes paths skip the edge (366)
    # and receipt validation fail with a typed reason.
    with factory.begin() as session:
        row = session.scalar(
            select(R1RecordRow).where(R1RecordRow.record_id == relation.relation_id.root)
        )
        assert row is not None
        payload = dict(row.record_json)
        payload.pop("evidence_refs", None)
        row.record_json = payload
    assert (
        repository.typed_graph_paths(
            commit_id, (base.entities[0].entity_id,), max_depth=2, limit=10
        )
        == ()
    )
    with pytest.raises(ValueError, match="relation row has no evidence"):
        repository.validate_graph_path_receipts((single,), text)
    # Evidence refs on the receipt that disagree with the row fail closed.
    repository.materialize(ProjectId("project.test"), commit_id, world)
    restored = next(
        path
        for path in repository.typed_graph_paths(
            commit_id, (base.entities[0].entity_id,), max_depth=2, limit=10
        )
        if path.relation_ids == (relation.relation_id,)
    )
    with pytest.raises(ValueError, match="evidence does not match relation rows"):
        repository.validate_graph_path_receipts(
            (restored.model_copy(update={"evidence_refs": ()}),), text
        )
    # L0 evidence validation branches fail closed per mechanism.
    blocks = repository._text_blocks(text)
    R._validate_l0_evidence(evidence, blocks)
    with pytest.raises(ValueError, match="no concrete span"):
        R._validate_l0_evidence(evidence.model_copy(update={"span": None}), blocks)
    with pytest.raises(ValueError, match="does not resolve to L0"):
        R._validate_l0_evidence(
            evidence.model_copy(update={"object_hash": ArtifactId("sha256:" + "c" * 64)}),
            blocks,
        )
    with pytest.raises(ValueError, match="does not resolve to L0"):
        R._validate_l0_evidence(
            evidence.model_copy(
                update={"span": TextSpanRef(block_id=StableId("block.ghost"), start=0, end=8)}
            ),
            blocks,
        )
    with pytest.raises(ValueError, match="quote hash does not match L0"):
        R._validate_l0_evidence(
            evidence.model_copy(update={"quote_hash": quote_hash("其他")}), blocks
        )
