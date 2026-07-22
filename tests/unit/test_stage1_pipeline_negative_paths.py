from __future__ import annotations

import pytest

from novel_agent.domain.benchmark import BenchmarkBundle, TextRootDocument
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory import (
    CandidatePool,
    RequirementLevel,
    RetrievalTrace,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.world import Entity, PlanNode, RelationRecord, StoryTime, TruthClass
from novel_agent.services.memory_pipeline import (
    AnchorBuilder,
    ContextCompiler,
    EvidenceExpander,
    _evidence_snippets,
)
from novel_agent.services.retrieval import (
    FusionService,
    InMemoryRetrievalBackend,
    RetrievalOrchestrator,
)
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.unit.test_stage1_memory_pipeline import memory_need


def built_components() -> tuple[
    BenchmarkBundle,
    TextRootDocument,
    WorldRootDocument,
    StableId,
    tuple[RetrievalUnit, ...],
    Stage1MemoryNeed,
    RetrievalTrace,
]:
    bundle = make_synthetic_bundle()
    history = bundle.text_roots[0]
    world = bundle.world_roots[0]
    snapshot = StableId("snapshot.negative")
    units = AnchorBuilder().build(world, history, bundle.plan_roots[0], snapshot_id=snapshot)
    orchestrator = RetrievalOrchestrator(InMemoryRetrievalBackend(units), FusionService())
    need = memory_need(
        "need.negative.promise",
        Stage1QueryIntent.SEMANTIC_HISTORY,
        "旧誓言",
        (CandidatePool.ANCHOR, CandidatePool.GROUNDED),
        mandatory=False,
    )
    trace = orchestrator.retrieve(need)
    return bundle, history, world, snapshot, units, need, trace


def test_anchor_builder_covers_relations_hierarchy_and_duplicate_guard() -> None:
    bundle = make_synthetic_bundle()
    history = bundle.text_roots[0]
    world = bundle.world_roots[0]
    first = world.entities[0]
    second = Entity(
        entity_id=StableId("entity.synthetic.tower"),
        entity_type="location",
        internal_label="北塔",
        aliases=("北塔",),
    )
    relation = RelationRecord(
        relation_id=StableId("relation.synthetic.destination"),
        predicate="travels_to",
        subject_id=first.entity_id,
        object_id=second.entity_id,
        valid_time=StoryTime(worldline="main", start_ordinal=20),
        evidence_refs=(world.obligations[0].evidence_refs[0],),
        truth_class=TruthClass.ACCEPTED_WORLD_FACT,
    )
    related_world = world.model_copy(update={"entities": (first, second), "relations": (relation,)})
    root_node = bundle.plan_roots[0].nodes[0]
    child = PlanNode(
        plan_node_id=StableId("plan.synthetic.child"),
        node_type="chapter_arc",
        title="北塔入口",
        summary="进入北塔",
        parent_id=root_node.plan_node_id,
    )
    nested_plan = bundle.plan_roots[0].model_copy(update={"nodes": (root_node, child)})

    units = AnchorBuilder().build(
        related_world,
        history,
        nested_plan,
        snapshot_id=StableId("snapshot.relation"),
    )

    assert any(unit.unit_kind is RetrievalUnitKind.RELATION_ANCHOR for unit in units)
    assert any(unit.parent_unit_id is not None for unit in units)

    colliding_state = world.states[0].model_copy(
        update={"state_id": history.chapters[0].chapter_id}
    )
    colliding_world = world.model_copy(update={"states": (colliding_state,)})
    with pytest.raises(ValueError, match="duplicate retrieval unit"):
        AnchorBuilder().build(
            colliding_world,
            history,
            None,
            snapshot_id=StableId("snapshot.collision"),
        )


def test_evidence_expander_rejects_bad_refs_skips_rejected_and_dedupes() -> None:
    bundle, history, _, _, _, _, trace = built_components()
    candidate = trace.candidates[0]
    expander = EvidenceExpander()

    rejected = candidate.model_copy(update={"selected": False})
    assert expander.expand((rejected,), history) == ()
    expanded = expander.expand((candidate, candidate), history)
    assert len({unit.unit_id for unit in expanded}) == len(expanded)

    evidence = candidate.unit.evidence_refs[0]
    wrong_root = evidence.model_copy(update={"root_hash": bundle.text_roots[1].root_hash})
    wrong_candidate = candidate.model_copy(
        update={"unit": candidate.unit.model_copy(update={"evidence_refs": (wrong_root,)})}
    )
    assert expander.expand((wrong_candidate,), history)
    wrong_object = evidence.model_copy(update={"object_hash": ArtifactId("sha256:" + "d" * 64)})
    bad_candidate = candidate.model_copy(
        update={"unit": candidate.unit.model_copy(update={"evidence_refs": (wrong_object,)})}
    )
    with pytest.raises(ValueError, match="span cannot be resolved"):
        expander.expand((bad_candidate,), history)

    assert evidence.span is not None
    missing_span = evidence.span.model_copy(update={"block_id": StableId("block.missing")})
    missing = evidence.model_copy(update={"span": missing_span})
    missing_candidate = candidate.model_copy(
        update={"unit": candidate.unit.model_copy(update={"evidence_refs": (missing,)})}
    )
    with pytest.raises(ValueError, match="span cannot be resolved"):
        expander.expand((missing_candidate,), history)


def test_context_compiler_covers_invalid_trace_unresolved_and_optional_budget() -> None:
    _, history, world, snapshot, units, need, trace = built_components()
    compiler = ContextCompiler(EvidenceExpander())
    with pytest.raises(ValueError, match="token budget"):
        compiler.compile(
            (),
            history,
            context_id=StableId("context.invalid-budget"),
            base_commit=world.source_commit,
            snapshot_id=snapshot,
            task_contract="invalid",
            token_budget=0,
        )
    with pytest.raises(ValueError, match="different memory need"):
        compiler.compile(
            ((need, trace.model_copy(update={"need_id": StableId("need.other")})),),
            history,
            context_id=StableId("context.wrong-trace"),
            base_commit=world.source_commit,
            snapshot_id=snapshot,
            task_contract="wrong trace",
            token_budget=100,
        )

    empty = RetrievalOrchestrator(InMemoryRetrievalBackend(()), FusionService()).retrieve(need)
    unresolved = compiler.compile(
        ((need, empty),),
        history,
        context_id=StableId("context.unresolved"),
        base_commit=world.source_commit,
        snapshot_id=snapshot,
        task_contract="unresolved",
        token_budget=100,
    )
    assert unresolved.unresolved_gaps == (need.query_text,)

    optional = need.model_copy(update={"requirement": RequirementLevel.OPTIONAL})
    roomy = compiler.compile(
        ((optional, trace),),
        history,
        context_id=StableId("context.roomy"),
        base_commit=world.source_commit,
        snapshot_id=snapshot,
        task_contract="roomy optional context",
        token_budget=10_000,
    )
    assert roomy.budget_report.optional_tokens > 0
    assert roomy.budget_report.dropped_optional_unit_ids == ()
    assert units


def test_canonical_evidence_snippet_guard_accepts_old_root_but_rejects_bad_content() -> None:
    bundle = make_synthetic_bundle()
    history = bundle.text_roots[0]
    evidence = bundle.world_roots[0].events[0].evidence_refs[0]
    blocks = {
        block.block_id: block
        for chapter in history.chapters
        for scene in chapter.scenes
        for block in scene.blocks
    }
    wrong_root = evidence.model_copy(update={"root_hash": bundle.text_roots[1].root_hash})
    assert _evidence_snippets((wrong_root,), history, blocks)
    wrong_object = evidence.model_copy(update={"object_hash": ArtifactId("sha256:" + "e" * 64)})
    with pytest.raises(ValueError, match="span cannot be resolved"):
        _evidence_snippets((wrong_object,), history, blocks)
    assert evidence.span is not None
    outside = evidence.model_copy(update={"span": evidence.span.model_copy(update={"end": 9999})})
    with pytest.raises(ValueError, match="span cannot be resolved"):
        _evidence_snippets((outside,), history, blocks)
