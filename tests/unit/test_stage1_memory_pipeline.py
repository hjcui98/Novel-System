from __future__ import annotations

import pytest

from novel_agent.domain.ids import ArtifactId, CommitId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    ChannelHit,
    FusedCandidate,
    NeedExecutionStatus,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    RetrievalChannel,
    RetrievalStopReason,
    RetrievalTrace,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.services.memory_pipeline import (
    AnchorBuilder,
    ContextCompiler,
    EvidenceExpander,
    _estimate_tokens,
    _with_content_metadata,
)
from novel_agent.services.retrieval import (
    FusionService,
    InMemoryRetrievalBackend,
    RetrievalOrchestrator,
)
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def memory_need(
    identity: str,
    intent: Stage1QueryIntent,
    query: str,
    pools: tuple[CandidatePool, ...],
    *,
    mandatory: bool,
    entity_ids: tuple[StableId, ...] = (),
) -> Stage1MemoryNeed:
    world = make_synthetic_bundle().world_roots[0]
    return Stage1MemoryNeed(
        need_id=StableId(identity),
        run_id=RunId("run.stage1.pipeline"),
        task_id=TaskId("task.stage1.pipeline"),
        base_commit=world.source_commit,
        horizon_target=(21, 23),
        need_type="synthetic",
        query_intent=intent,
        query_text=query,
        entity_ids=entity_ids,
        why_needed="exercise the Stage 1 read side",
        risk_level=NeedRisk.HIGH if mandatory else NeedRisk.MEDIUM,
        requirement=(RequirementLevel.MANDATORY if mandatory else RequirementLevel.OPTIONAL),
        preferred_resolution_path=(
            ResolutionPath.EXACT_TEMPORAL
            if CandidatePool.R1 in pools
            else ResolutionPath.ANCHOR_FIRST
        ),
        allowed_candidate_pools=pools,
        expected_evidence_types=("text_span",),
        stop_condition="supported evidence found",
    )


def test_anchor_pipeline_keeps_anchor_and_grounded_pools_typed_and_separate() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    units = AnchorBuilder().build(
        bundle.world_roots[0],
        history,
        bundle.plan_roots[0],
        snapshot_id=StableId("snapshot.synthetic.20"),
    )

    anchor_units = tuple(
        item
        for item in units
        if item.unit_kind not in {RetrievalUnitKind.GROUNDED_BLOCK, RetrievalUnitKind.GROUNDED_SPAN}
    )
    grounded_units = tuple(
        item for item in units if item.unit_kind is RetrievalUnitKind.GROUNDED_BLOCK
    )
    assert anchor_units
    assert len(grounded_units) == 20
    assert not {unit.unit_id for unit in anchor_units}.intersection(
        unit.unit_id for unit in grounded_units
    )
    # Chapter 21 may appear as an author-visible Plan anchor, but never as
    # grounded/observed prose in the cutoff-20 TextRoot.
    assert all(".21" not in unit.unit_id.root for unit in grounded_units)
    assert any(
        unit.unit_id.root == "anchor.goal.synthetic.21"
        and unit.unit_kind is RetrievalUnitKind.PLAN_ANCHOR
        for unit in units
    )


def test_anchor_pipeline_resolves_evidence_from_an_older_append_only_text_root() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    state = world.states[0]
    old_root_evidence = tuple(
        evidence.model_copy(update={"root_hash": ArtifactId("sha256:" + "f" * 64)})
        for evidence in state.evidence_refs
    )
    historical_world = world.model_copy(
        update={"states": (state.model_copy(update={"evidence_refs": old_root_evidence}),)}
    )

    units = AnchorBuilder().build(
        historical_world,
        history,
        bundle.plan_roots[0],
        snapshot_id=StableId("snapshot.synthetic.append-only"),
    )

    assert any(unit.unit_id.root == f"anchor.{state.state_id.root}" for unit in units)
    need = memory_need(
        "need.synthetic.append-only",
        Stage1QueryIntent.CURRENT_STATE,
        state.predicate,
        (CandidatePool.R1,),
        mandatory=True,
        entity_ids=(state.subject_id,),
    )
    trace = RetrievalOrchestrator(InMemoryRetrievalBackend(units), FusionService()).retrieve(need)
    package = ContextCompiler(EvidenceExpander()).compile(
        ((need, trace),),
        history,
        context_id=StableId("context.synthetic.append-only"),
        base_commit=historical_world.source_commit,
        snapshot_id=StableId("snapshot.synthetic.append-only"),
        task_contract="resolve historical evidence against appended TextRoot",
        token_budget=1000,
    )
    assert package.raw_evidence_spans


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"span": None}, "does not resolve"),
        ({"quote_hash": ArtifactId("sha256:" + "f" * 64)}, "quote hash does not match"),
    ],
)
def test_anchor_pipeline_rejects_unbound_or_mismatched_evidence(
    mutation: dict[str, object],
    message: str,
) -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    state = world.states[0]
    invalid = state.evidence_refs[0].model_copy(update=mutation)
    invalid_world = world.model_copy(
        update={"states": (state.model_copy(update={"evidence_refs": (invalid,)}),)}
    )

    with pytest.raises(ValueError, match=message):
        AnchorBuilder().build(
            invalid_world,
            history,
            bundle.plan_roots[0],
            snapshot_id=StableId("snapshot.synthetic.invalid-evidence"),
        )


def test_content_metadata_promotes_legacy_parent_id() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    source = AnchorBuilder().build(
        bundle.world_roots[0],
        history,
        bundle.plan_roots[0],
        snapshot_id=StableId("snapshot.synthetic.legacy-parent"),
    )[0]
    parent = StableId("unit.legacy.parent")
    legacy = source.model_copy(update={"parent_unit_id": parent, "parent_unit_ids": ()})

    rebound = _with_content_metadata(legacy)

    assert rebound.parent_unit_ids == (parent,)


def test_evidence_expander_does_not_duplicate_grounded_l0_units() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    grounded = next(
        unit
        for unit in AnchorBuilder().build(
            bundle.world_roots[0],
            history,
            bundle.plan_roots[0],
            snapshot_id=StableId("snapshot.synthetic.grounded"),
        )
        if unit.unit_kind is RetrievalUnitKind.GROUNDED_BLOCK
    )
    hit = ChannelHit(
        unit=grounded,
        channel=RetrievalChannel.GROUNDED_BM25,
        channel_rank=1,
        raw_score=1.0,
        candidate_count=1,
        hit_reason="test",
    )
    candidate = FusedCandidate(
        unit=grounded,
        fused_rank=1,
        rrf_score=1.0,
        channel_hits=(hit,),
    )

    assert EvidenceExpander().expand((candidate,), history) == ()


def test_context_compiler_gives_an_uncovered_need_a_slot_before_extra_alternatives() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    snapshot_id = StableId("snapshot.synthetic.fair-context")
    mandatory_need = memory_need(
        "need.pipeline.mandatory-wide",
        Stage1QueryIntent.CURRENT_STATE,
        "wide mandatory query",
        (CandidatePool.R1,),
        mandatory=True,
    )
    optional_need = memory_need(
        "need.pipeline.optional-history",
        Stage1QueryIntent.SEMANTIC_HISTORY,
        "specific historical evidence",
        (CandidatePool.ANCHOR,),
        mandatory=False,
    ).model_copy(update={"priority": 100})

    def unit(identity: str, text: str) -> RetrievalUnit:
        return RetrievalUnit(
            unit_id=StableId(identity),
            unit_kind=RetrievalUnitKind.STATE_ANCHOR,
            source_commit=world.source_commit,
            snapshot_id=snapshot_id,
            text=text,
        )

    first = unit("unit.context.required-first", "A" * 40)
    redundant = unit("unit.context.redundant-second", "B" * 40)
    target = unit("unit.context.optional-target", "C" * 40)

    def candidate(item: RetrievalUnit, rank: int, count: int) -> FusedCandidate:
        hit = ChannelHit(
            unit=item,
            channel=RetrievalChannel.R1_EXACT,
            channel_rank=rank,
            raw_score=float(count - rank + 1),
            candidate_count=count,
            hit_reason="test",
        )
        return FusedCandidate(
            unit=item,
            fused_rank=rank,
            rrf_score=1.0 / rank,
            channel_hits=(hit,),
        )

    def trace(need: Stage1MemoryNeed, items: tuple[RetrievalUnit, ...]) -> RetrievalTrace:
        candidates = tuple(
            candidate(item, index, len(items)) for index, item in enumerate(items, 1)
        )
        return RetrievalTrace(
            need_id=need.need_id,
            intent=need.query_intent,
            allowed_channels=(RetrievalChannel.R1_EXACT,),
            channel_candidate_counts={RetrievalChannel.R1_EXACT: len(candidates)},
            candidates=candidates,
            fusion_applied=False,
            stop_reason=RetrievalStopReason.BUDGET_SATISFIED,
            need_execution_status=NeedExecutionStatus.EXECUTED_WITH_CANDIDATES,
            calls_allocated=1,
        )

    package = ContextCompiler(EvidenceExpander()).compile(
        (
            (mandatory_need, trace(mandatory_need, (first, redundant))),
            (optional_need, trace(optional_need, (target,))),
        ),
        history,
        context_id=StableId("context.synthetic.fair-context"),
        base_commit=world.source_commit,
        snapshot_id=snapshot_id,
        task_contract="preserve one bounded evidence group per Need",
        token_budget=_estimate_tokens(first.text) + _estimate_tokens(target.text),
    )

    assert tuple(unit.unit_id for unit in package.mandatory_constraints) == (first.unit_id,)
    assert target.unit_id in {unit.unit_id for unit in package.current_world_state}
    assert redundant.unit_id in package.budget_report.dropped_optional_unit_ids
    assert package.budget_report.mandatory_tokens + package.budget_report.optional_tokens == (
        package.budget_report.token_budget
    )


def _scoped_unit(
    identity: str,
    scope: str,
    *,
    commit: CommitId,
    snapshot: StableId,
) -> RetrievalUnit:
    return RetrievalUnit(
        unit_id=StableId(identity),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=commit,
        snapshot_id=snapshot,
        text="scope visible state",
        entity_ids=(StableId("entity.scope.subject"),),
        access_scope=scope,
        evidence_refs=(),
        support_status="supported",
    )


def test_in_memory_backend_applies_scope_lattice_and_rejects_unknown_scope() -> None:
    world = make_synthetic_bundle().world_roots[0]
    snapshot = StableId("snapshot.synthetic.scope-lattice")
    safe = _scoped_unit(
        "anchor.scope.safe", "writer_safe", commit=world.source_commit, snapshot=snapshot
    )
    plan = _scoped_unit(
        "anchor.scope.plan", "author_planning", commit=world.source_commit, snapshot=snapshot
    )
    evaluator = _scoped_unit(
        "anchor.scope.evaluator", "evaluator", commit=world.source_commit, snapshot=snapshot
    )
    backend = InMemoryRetrievalBackend((safe, plan, evaluator))

    def visible(scope: str) -> set[StableId]:
        scoped_need = memory_need(
            f"need.scope.{scope}",
            Stage1QueryIntent.CURRENT_STATE,
            "scope visible state",
            (CandidatePool.R1,),
            mandatory=True,
            entity_ids=(StableId("entity.scope.subject"),),
        ).model_copy(
            update={
                "access_scope": scope,
                "allow_plan": scope in {"author_planning", "evaluator"},
            }
        )
        return {
            hit.unit.unit_id for hit in backend.search(scoped_need, RetrievalChannel.R1_EXACT, 10)
        }

    assert visible("writer_safe") == {safe.unit_id}
    assert visible("author_planning") == {safe.unit_id, plan.unit_id}
    assert visible("evaluator") == {safe.unit_id, plan.unit_id, evaluator.unit_id}
    assert visible("unknown") == set()


def test_context_compiler_preserves_mandatory_closure_and_expands_l0_evidence() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    snapshot_id = StableId("snapshot.synthetic.20")
    units = AnchorBuilder().build(
        world,
        history,
        bundle.plan_roots[0],
        snapshot_id=snapshot_id,
    )
    orchestrator = RetrievalOrchestrator(InMemoryRetrievalBackend(units), FusionService())
    character_id = world.entities[0].entity_id
    injury = memory_need(
        "need.pipeline.injury",
        Stage1QueryIntent.CURRENT_STATE,
        "林澈 injury not_healed",
        (CandidatePool.R1,),
        mandatory=True,
        entity_ids=(character_id,),
    )
    promise = memory_need(
        "need.pipeline.promise",
        Stage1QueryIntent.SEMANTIC_HISTORY,
        "旧誓言",
        (CandidatePool.ANCHOR, CandidatePool.GROUNDED),
        mandatory=False,
    )
    promise = promise.model_copy(update={"access_scope": "author_planning", "allow_plan": True})
    traces = ((injury, orchestrator.retrieve(injury)), (promise, orchestrator.retrieve(promise)))

    package = ContextCompiler(EvidenceExpander()).compile(
        traces,
        history,
        context_id=StableId("context.synthetic.20-to-3"),
        base_commit=world.source_commit,
        snapshot_id=snapshot_id,
        task_contract="prepare chapters 21 through 23",
        token_budget=1,
    )

    assert package.mandatory_constraints
    assert package.budget_report.mandatory_tokens > package.budget_report.token_budget
    assert any(
        unit.unit_kind is RetrievalUnitKind.STATE_ANCHOR for unit in package.current_world_state
    )
    assert any(unit.text == "受伤仍未痊愈" for unit in package.raw_evidence_spans)
    assert package.budget_report.dropped_optional_unit_ids
    assert package.unresolved_gaps == ()
    assert all(unit.source_commit == world.source_commit for unit in package.mandatory_constraints)
    assert all(unit.snapshot_id == snapshot_id for unit in package.mandatory_constraints)


def test_context_compiler_rejects_stale_snapshot_units() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    built_snapshot = StableId("snapshot.synthetic.old")
    units = AnchorBuilder().build(
        world,
        history,
        bundle.plan_roots[0],
        snapshot_id=built_snapshot,
    )
    orchestrator = RetrievalOrchestrator(InMemoryRetrievalBackend(units), FusionService())
    need = memory_need(
        "need.pipeline.stale",
        Stage1QueryIntent.CURRENT_STATE,
        "林澈 injury",
        (CandidatePool.R1,),
        mandatory=True,
        entity_ids=(world.entities[0].entity_id,),
    )

    with pytest.raises(ValueError, match="snapshot basis mismatch"):
        ContextCompiler(EvidenceExpander()).compile(
            ((need, orchestrator.retrieve(need)),),
            history,
            context_id=StableId("context.synthetic.stale"),
            base_commit=world.source_commit,
            snapshot_id=StableId("snapshot.synthetic.current"),
            task_contract="must not read a stale snapshot",
            token_budget=100,
        )
