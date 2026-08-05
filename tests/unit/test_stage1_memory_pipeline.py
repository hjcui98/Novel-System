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
    COMPACT_BLOCK_EXCERPT_LIMIT,
    AnchorBuilder,
    ContextCompiler,
    EvidenceExpander,
    _compact_block_excerpt,
    _compact_block_unit,
    _estimate_tokens,
    _query_terms,
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


def _large_block_candidate(item: RetrievalUnit, rank: int, count: int) -> FusedCandidate:
    hit = ChannelHit(
        unit=item,
        channel=RetrievalChannel.GROUNDED_BM25,
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


def _large_block_trace(need: Stage1MemoryNeed, item: RetrievalUnit) -> RetrievalTrace:
    return RetrievalTrace(
        need_id=need.need_id,
        intent=need.query_intent,
        allowed_channels=(RetrievalChannel.GROUNDED_BM25,),
        channel_candidate_counts={RetrievalChannel.GROUNDED_BM25: 1},
        candidates=(_large_block_candidate(item, 1, 1),),
        fusion_applied=False,
        stop_reason=RetrievalStopReason.BUDGET_SATISFIED,
        need_execution_status=NeedExecutionStatus.EXECUTED_WITH_CANDIDATES,
        calls_allocated=1,
    )


def test_context_compiler_compacts_large_block_when_full_text_does_not_fit() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    snapshot_id = StableId("snapshot.synthetic.compact-block")
    reference = world.states[0].evidence_refs[0]
    block_text = "".join(f"落落说道 这是第{index}句。" for index in range(300))
    block = RetrievalUnit(
        unit_id=StableId("grounded.block.ZTJ-P005.56.0"),
        unit_kind=RetrievalUnitKind.GROUNDED_BLOCK,
        source_commit=world.source_commit,
        snapshot_id=snapshot_id,
        text=block_text,
        entity_ids=(StableId("entity.subject"),),
        evidence_refs=(reference,),
    )
    need = memory_need(
        "need.pipeline.compact-block",
        Stage1QueryIntent.CURRENT_STATE,
        "block query terms",
        (CandidatePool.GROUNDED,),
        mandatory=False,
    )
    full_cost = _estimate_tokens(block_text)
    compact_id = StableId("compact.grounded.block.ZTJ-P005.56.0")

    package = ContextCompiler(EvidenceExpander()).compile(
        ((need, _large_block_trace(need, block)),),
        history,
        context_id=StableId("context.synthetic.compact-block"),
        base_commit=world.source_commit,
        snapshot_id=snapshot_id,
        task_contract="represent a large block by a bounded excerpt",
        token_budget=_estimate_tokens("落" * 600) + 1,
    )

    style_ids = {unit.unit_id for unit in package.style_or_reference_optional}
    assert full_cost > package.budget_report.token_budget
    assert compact_id in style_ids
    assert block.unit_id in package.budget_report.dropped_optional_unit_ids
    compact = next(
        unit for unit in package.style_or_reference_optional if unit.unit_id == compact_id
    )
    assert compact.evidence_refs
    assert all(
        item.evidence_id.root.startswith("evidence.segment.") for item in compact.evidence_refs
    )
    assert all(item.span is not None for item in compact.evidence_refs)
    assert len(compact.text) < len(block_text)
    assert compact.parent_unit_id == block.unit_id
    assert package.budget_report.optional_tokens <= package.budget_report.token_budget


def test_context_compiler_packs_full_block_when_budget_allows() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    snapshot_id = StableId("snapshot.synthetic.full-block")
    block_text = "落落说道 这是第一句。"
    block = RetrievalUnit(
        unit_id=StableId("grounded.block.ZTJ-P005.56.0"),
        unit_kind=RetrievalUnitKind.GROUNDED_BLOCK,
        source_commit=world.source_commit,
        snapshot_id=snapshot_id,
        text=block_text,
        entity_ids=(StableId("entity.subject"),),
        evidence_refs=(world.states[0].evidence_refs[0],),
    )
    need = memory_need(
        "need.pipeline.full-block",
        Stage1QueryIntent.CURRENT_STATE,
        "block query terms",
        (CandidatePool.GROUNDED,),
        mandatory=False,
    )

    package = ContextCompiler(EvidenceExpander()).compile(
        ((need, _large_block_trace(need, block)),),
        history,
        context_id=StableId("context.synthetic.full-block"),
        base_commit=world.source_commit,
        snapshot_id=snapshot_id,
        task_contract="keep the full passage when it is small",
        token_budget=_estimate_tokens(block_text),
    )

    style_ids = {unit.unit_id for unit in package.style_or_reference_optional}
    assert block.unit_id in style_ids
    assert not any(
        unit.unit_id.root.startswith("compact.") for unit in package.style_or_reference_optional
    )
    assert block.unit_id not in package.budget_report.dropped_optional_unit_ids


def test_context_compiler_compacts_large_block_even_when_budget_allows() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    snapshot_id = StableId("snapshot.synthetic.compact-always")
    block_text = "".join(f"落落说道 这是第{index}句。" for index in range(300))
    block = RetrievalUnit(
        unit_id=StableId("grounded.block.ZTJ-P005.56.0"),
        unit_kind=RetrievalUnitKind.GROUNDED_BLOCK,
        source_commit=world.source_commit,
        snapshot_id=snapshot_id,
        text=block_text,
        entity_ids=(StableId("entity.subject"),),
        evidence_refs=(world.states[0].evidence_refs[0],),
    )
    need = memory_need(
        "need.pipeline.compact-always",
        Stage1QueryIntent.CURRENT_STATE,
        "block query terms",
        (CandidatePool.GROUNDED,),
        mandatory=False,
    )
    compact_id = StableId("compact.grounded.block.ZTJ-P005.56.0")

    package = ContextCompiler(EvidenceExpander()).compile(
        ((need, _large_block_trace(need, block)),),
        history,
        context_id=StableId("context.synthetic.compact-always"),
        base_commit=world.source_commit,
        snapshot_id=snapshot_id,
        task_contract="represent large passages by bounded excerpts",
        token_budget=_estimate_tokens(block_text),
    )

    style_ids = {unit.unit_id for unit in package.style_or_reference_optional}
    assert compact_id in style_ids
    assert block.unit_id not in style_ids
    compact = next(
        unit for unit in package.style_or_reference_optional if unit.unit_id == compact_id
    )
    assert compact.evidence_refs
    assert all(
        item.evidence_id.root.startswith("evidence.segment.") for item in compact.evidence_refs
    )
    assert compact.content_hash == block.content_hash


def test_context_compiler_never_compacts_non_grounded_units() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    snapshot_id = StableId("snapshot.synthetic.no-compact")
    anchor = RetrievalUnit(
        unit_id=StableId("anchor.state.subject"),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=world.source_commit,
        snapshot_id=snapshot_id,
        text="大" * 400,
        entity_ids=(StableId("entity.subject"),),
    )
    need = memory_need(
        "need.pipeline.no-compact",
        Stage1QueryIntent.CURRENT_STATE,
        "anchor query terms",
        (CandidatePool.ANCHOR,),
        mandatory=False,
    )

    package = ContextCompiler(EvidenceExpander()).compile(
        ((need, _large_block_trace(need, anchor)),),
        history,
        context_id=StableId("context.synthetic.no-compact"),
        base_commit=world.source_commit,
        snapshot_id=snapshot_id,
        task_contract="never compact non-grounded units",
        token_budget=_estimate_tokens("大" * 100),
    )

    assert anchor.unit_id in package.budget_report.dropped_optional_unit_ids
    assert not any(
        unit.unit_id.root.startswith("compact.") for unit in package.style_or_reference_optional
    )


def test_query_terms_skips_stopwords_keeps_ascii_and_bigrams_chinese() -> None:
    terms = _query_terms("the 落落 陈长生 says 的")
    assert "the" not in terms
    assert "的" not in terms
    assert "says" in terms
    assert "落落" in terms
    assert "陈长" in terms
    assert "长生" in terms


def test_compact_block_excerpt_short_text_is_not_compacted() -> None:
    assert (
        _compact_block_excerpt("短文本。" * 10, "短文本", limit=COMPACT_BLOCK_EXCERPT_LIMIT) is None
    )


def test_compact_block_excerpt_whitespace_only_text_returns_none() -> None:
    assert _compact_block_excerpt("\n" * 700, "句", limit=COMPACT_BLOCK_EXCERPT_LIMIT) is None


def test_compact_block_excerpt_single_oversized_sentence_returns_none() -> None:
    assert _compact_block_excerpt("长" * 700, "长", limit=COMPACT_BLOCK_EXCERPT_LIMIT) is None


def test_compact_block_excerpt_head_budget_covers_all_sentences() -> None:
    excerpt = _compact_block_excerpt(
        "长" + "\n" * 700 + "长", "长", limit=COMPACT_BLOCK_EXCERPT_LIMIT
    )
    assert excerpt == "长 长"


def test_context_compiler_rejects_compact_of_multi_unit_expansion() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    snapshot_id = StableId("snapshot.synthetic.compact-multi")
    reference = world.states[0].evidence_refs[0]
    anchor = RetrievalUnit(
        unit_id=StableId("anchor.state.subject"),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=world.source_commit,
        snapshot_id=snapshot_id,
        text="受伤仍未痊愈。",
        entity_ids=(StableId("entity.subject"),),
        evidence_refs=(
            reference,
            reference.model_copy(update={"evidence_id": StableId("evidence.synthetic.20.999")}),
        ),
    )
    need = memory_need(
        "need.pipeline.compact-multi",
        Stage1QueryIntent.CURRENT_STATE,
        "anchor query terms",
        (CandidatePool.ANCHOR,),
        mandatory=False,
    )

    package = ContextCompiler(EvidenceExpander()).compile(
        ((need, _large_block_trace(need, anchor)),),
        history,
        context_id=StableId("context.synthetic.compact-multi"),
        base_commit=world.source_commit,
        snapshot_id=snapshot_id,
        task_contract="never compact a multi-unit expansion group",
        token_budget=_estimate_tokens(anchor.text) + 1,
    )

    assert anchor.unit_id not in package.budget_report.dropped_optional_unit_ids
    assert any(
        unit_id.root.startswith("expanded.")
        for unit_id in package.budget_report.dropped_optional_unit_ids
    )


def test_context_compiler_rejects_compact_when_excerpt_still_exceeds_budget() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    snapshot_id = StableId("snapshot.synthetic.compact-too-big")
    block_text = "".join(f"落落说道 这是第{index}句。" for index in range(300))
    block = RetrievalUnit(
        unit_id=StableId("grounded.block.ZTJ-P005.56.0"),
        unit_kind=RetrievalUnitKind.GROUNDED_BLOCK,
        source_commit=world.source_commit,
        snapshot_id=snapshot_id,
        text=block_text,
        entity_ids=(StableId("entity.subject"),),
        evidence_refs=(world.states[0].evidence_refs[0],),
    )
    need = memory_need(
        "need.pipeline.compact-too-big",
        Stage1QueryIntent.CURRENT_STATE,
        "block query terms",
        (CandidatePool.GROUNDED,),
        mandatory=False,
    )

    package = ContextCompiler(EvidenceExpander()).compile(
        ((need, _large_block_trace(need, block)),),
        history,
        context_id=StableId("context.synthetic.compact-too-big"),
        base_commit=world.source_commit,
        snapshot_id=snapshot_id,
        task_contract="drop the block when even the excerpt does not fit",
        token_budget=_estimate_tokens("落" * 60),
    )

    assert block.unit_id in package.budget_report.dropped_optional_unit_ids
    assert not any(
        unit.unit_id.root.startswith("compact.") for unit in package.style_or_reference_optional
    )


def test_context_compiler_rejects_redundant_compact_representation() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    snapshot_id = StableId("snapshot.synthetic.compact-redundant")
    reference = world.states[0].evidence_refs[0]
    block_text = "".join(f"落落说道 这是第{index}句。" for index in range(300))
    block = RetrievalUnit(
        unit_id=StableId("grounded.block.ZTJ-P005.56.0"),
        unit_kind=RetrievalUnitKind.GROUNDED_BLOCK,
        source_commit=world.source_commit,
        snapshot_id=snapshot_id,
        text=block_text,
        entity_ids=(StableId("entity.subject"),),
        evidence_refs=(reference,),
    )
    first = memory_need(
        "need.pipeline.compact-redundant-first",
        Stage1QueryIntent.CURRENT_STATE,
        "block query terms",
        (CandidatePool.GROUNDED,),
        mandatory=False,
    )
    second = memory_need(
        "need.pipeline.compact-redundant-second",
        Stage1QueryIntent.CURRENT_STATE,
        "block query terms",
        (CandidatePool.GROUNDED,),
        mandatory=False,
    )

    package = ContextCompiler(EvidenceExpander()).compile(
        (
            (first, _large_block_trace(first, block)),
            (second, _large_block_trace(second, block)),
        ),
        history,
        context_id=StableId("context.synthetic.compact-redundant"),
        base_commit=world.source_commit,
        snapshot_id=snapshot_id,
        task_contract="never pack the same compact representation twice",
        token_budget=_estimate_tokens("落" * 600) + 1,
    )

    compact_units = [
        unit
        for unit in package.style_or_reference_optional
        if unit.unit_id.root.startswith("compact.")
    ]
    assert len(compact_units) == 1
    assert compact_units[0].parent_unit_id == block.unit_id
    assert block.unit_id in package.budget_report.dropped_optional_unit_ids


def test_context_compiler_drops_delimiter_only_block_without_compact() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    snapshot_id = StableId("snapshot.synthetic.compact-empty")
    block = RetrievalUnit(
        unit_id=StableId("grounded.block.ZTJ-P005.56.0"),
        unit_kind=RetrievalUnitKind.GROUNDED_BLOCK,
        source_commit=world.source_commit,
        snapshot_id=snapshot_id,
        text="\n" * 700,
        entity_ids=(StableId("entity.subject"),),
        evidence_refs=(world.states[0].evidence_refs[0],),
    )
    need = memory_need(
        "need.pipeline.compact-empty",
        Stage1QueryIntent.CURRENT_STATE,
        "block query terms",
        (CandidatePool.GROUNDED,),
        mandatory=False,
    )

    package = ContextCompiler(EvidenceExpander()).compile(
        ((need, _large_block_trace(need, block)),),
        history,
        context_id=StableId("context.synthetic.compact-empty"),
        base_commit=world.source_commit,
        snapshot_id=snapshot_id,
        task_contract="drop a large block with no extractable sentences",
        token_budget=_estimate_tokens("落" * 100),
    )

    assert block.unit_id in package.budget_report.dropped_optional_unit_ids
    assert not any(
        unit.unit_id.root.startswith("compact.") for unit in package.style_or_reference_optional
    )


def test_compact_block_unit_rejects_non_grounded_unit() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    anchor = RetrievalUnit(
        unit_id=StableId("anchor.state.subject"),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=world.source_commit,
        snapshot_id=StableId("snapshot.synthetic.compact-unit"),
        text="大" * 700,
        entity_ids=(StableId("entity.subject"),),
    )
    assert _compact_block_unit(anchor, query_text="anchor query") is None
    short_block = anchor.model_copy(
        update={
            "unit_id": StableId("grounded.block.ZTJ-P005.36.0"),
            "unit_kind": RetrievalUnitKind.GROUNDED_BLOCK,
            "text": "短文本。" * 5,
        }
    )
    assert _compact_block_unit(short_block, query_text="anchor query") is None


def test_context_compiler_deep_grounded_block_survives_budget_competition() -> None:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    snapshot_id = StableId("snapshot.synthetic.deep-grounded")
    reference = world.states[0].evidence_refs[0]

    def block(unit_id: str, seed: int) -> RetrievalUnit:
        return RetrievalUnit(
            unit_id=StableId(unit_id),
            unit_kind=RetrievalUnitKind.GROUNDED_BLOCK,
            source_commit=world.source_commit,
            snapshot_id=snapshot_id,
            text="".join(f"落落说道 这是第{index}句。" for index in range(seed * 40 + 80)),
            entity_ids=(StableId("entity.subject"),),
            evidence_refs=(reference,),
        )

    high_priority = block("grounded.block.ZTJ-P005.33.0", 5)
    deep_block = block("grounded.block.ZTJ-P005.56.0", 5)
    high_need = memory_need(
        "need.pipeline.deep-grounded-high",
        Stage1QueryIntent.CURRENT_STATE,
        "block query terms",
        (CandidatePool.GROUNDED,),
        mandatory=False,
    )
    high_need = high_need.model_copy(update={"priority": 97})
    deep_need = memory_need(
        "need.pipeline.deep-grounded-low",
        Stage1QueryIntent.CURRENT_STATE,
        "block query terms",
        (CandidatePool.GROUNDED,),
        mandatory=False,
    )
    deep_need = deep_need.model_copy(update={"priority": 60})
    high_candidates = tuple(_large_block_candidate(high_priority, rank, 5) for rank in range(1, 6))
    high_trace = RetrievalTrace(
        need_id=high_need.need_id,
        intent=high_need.query_intent,
        allowed_channels=(RetrievalChannel.GROUNDED_BM25,),
        channel_candidate_counts={RetrievalChannel.GROUNDED_BM25: 5},
        candidates=high_candidates,
        fusion_applied=False,
        stop_reason=RetrievalStopReason.BUDGET_SATISFIED,
        need_execution_status=NeedExecutionStatus.EXECUTED_WITH_CANDIDATES,
        calls_allocated=1,
    )
    deep_trace = _large_block_trace(deep_need, deep_block)

    package = ContextCompiler(EvidenceExpander()).compile(
        ((high_need, high_trace), (deep_need, deep_trace)),
        history,
        context_id=StableId("context.synthetic.deep-grounded"),
        base_commit=world.source_commit,
        snapshot_id=snapshot_id,
        task_contract="deep grounded evidence survives budget competition",
        token_budget=700,
    )

    style_ids = {unit.unit_id.root for unit in package.style_or_reference_optional}
    assert "compact.grounded.block.ZTJ-P005.56.0" in style_ids
    deep_compact = next(
        unit
        for unit in package.style_or_reference_optional
        if unit.unit_id.root == "compact.grounded.block.ZTJ-P005.56.0"
    )
    assert deep_compact.evidence_refs
    assert all(
        item.evidence_id.root.startswith("evidence.segment.") for item in deep_compact.evidence_refs
    )
    assert all(item.span is not None for item in deep_compact.evidence_refs)


def test_compact_segment_ref_requires_source_evidence() -> None:
    import pytest as _pytest

    from novel_agent.services.memory_pipeline import _segment_evidence_ref

    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    no_evidence = RetrievalUnit(
        unit_id=StableId("grounded.block.ZTJ-P005.56.0"),
        unit_kind=RetrievalUnitKind.GROUNDED_BLOCK,
        source_commit=world.source_commit,
        snapshot_id=StableId("snapshot.synthetic.segment-ref"),
        text="落落说道 这是第一句。",
        entity_ids=(StableId("entity.subject"),),
    )
    with _pytest.raises(ValueError, match="requires an evidence reference"):
        _segment_evidence_ref(no_evidence, "落落说道 这是第一句。", 0, 10)
    spanless = no_evidence.model_copy(
        update={
            "evidence_refs": (world.states[0].evidence_refs[0].model_copy(update={"span": None}),)
        }
    )
    with _pytest.raises(ValueError, match="requires a precise span"):
        _segment_evidence_ref(spanless, "落落说道 这是第一句。", 0, 10)
