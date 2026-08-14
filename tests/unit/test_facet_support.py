"""FacetSupportEvaluator: per-facet exact-evidence closure (2026-08-13 repair)."""

from __future__ import annotations

from novel_agent.domain.ids import CommitId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    ExpectedClaimScope,
    FacetClosureStatus,
    FacetEvidenceRequirement,
    FusedCandidate,
    NeedCompletionSpec,
    NeedFacet,
    NeedFacetKind,
    NeedGapPolicy,
    NeedRisk,
    NeedUncertaintyPolicy,
    RequirementLevel,
    ResolutionPath,
    RetrievalUnit,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.text import EvidenceRef, TextSpanRef
from novel_agent.services.facet_support import FacetSupportEvaluator

COMMIT = CommitId("sha256:" + "0" * 64)
ENTITY = StableId("entity.test.chen")
FACET_A = StableId("facet.test.a")
FACET_B = StableId("facet.test.b")


def _need(
    *,
    facets: tuple[NeedFacetKind, ...],
    mandatory: bool = True,
    predicates: tuple[str, ...] = ("location", "enrollment_status"),
    predicates_by_facet: dict[NeedFacetKind, tuple[str, ...]] | None = None,
) -> Stage1MemoryNeed:
    need_id = StableId("need.test")
    digest = "aabbccdd00112233"
    need_facets = tuple(
        NeedFacet(
            need_facet_id=StableId(f"need-facet.{digest}.{index}.{kind.value}"),
            need_id=need_id,
            facet_kind=kind,
            expected_claim_scope=ExpectedClaimScope.CURRENT,
            derivation_refs=(need_id,),
            producer="test",
            producer_version="v1",
            information_scope="cutoff_safe",
        )
        for index, kind in enumerate(facets)
    )
    facet_bindings = (
        {kind: predicates for kind in facets}
        if predicates_by_facet is None
        else {kind: predicates_by_facet.get(kind, ()) for kind in facets}
    )
    spec = NeedCompletionSpec(
        need_id=need_id,
        required_need_facet_ids=tuple(item.need_facet_id for item in need_facets),
        irreducible_need_facet_ids=(
            tuple(item.need_facet_id for item in need_facets) if mandatory else ()
        ),
        evidence_requirement_by_facet={
            item.need_facet_id.root: FacetEvidenceRequirement.TRACEABLE_CUTOFF_SOURCE
            for item in need_facets
        },
        min_distinct_evidence_sources=1,
        min_distinct_chapters=1,
        uncertainty_policy=NeedUncertaintyPolicy.ALLOW_GAP_ONLY,
        gap_policy=NeedGapPolicy.FAIL_MANDATORY,
        producer="test",
        producer_version="v1",
        predicates_by_facet={
            item.need_facet_id.root: facet_bindings[item.facet_kind] for item in need_facets
        },
    )
    return Stage1MemoryNeed(
        need_id=need_id,
        run_id=RunId("run.test"),
        task_id=TaskId("task.test"),
        base_commit=COMMIT,
        horizon_target=(21, 25),
        need_type="test",
        query_intent=Stage1QueryIntent.CURRENT_STATE,
        query_text="陈长生当前状态",
        entity_ids=(ENTITY,),
        predicates=predicates,
        access_scope="writer_safe",
        why_needed="test",
        risk_level=NeedRisk.MEDIUM,
        requirement=RequirementLevel.MANDATORY if mandatory else RequirementLevel.OPTIONAL,
        preferred_resolution_path=ResolutionPath.EXACT_TEMPORAL,
        allowed_candidate_pools=(CandidatePool.R1, CandidatePool.ANCHOR, CandidatePool.GROUNDED),
        stop_condition="served by cutoff-safe exact evidence slices or an explicit typed gap",
        need_facets=need_facets,
        completion_spec=spec,
    )


def _unit(
    unit_id: str,
    *,
    kind: str,
    entity_ids: tuple[StableId, ...] = (ENTITY,),
    evidence: bool = True,
    text: str = "陈长生伤势未愈。",
    predicate: str | None = "location",
    access_scope: str = "writer_safe",
) -> RetrievalUnit:
    from novel_agent.domain.memory import RetrievalUnitKind
    from novel_agent.domain.text import EvidenceSupportStatus

    refs = (
        (
            EvidenceRef(
                evidence_id=StableId("evidence.test"),
                root_hash="sha256:" + "0" * 64,
                object_hash="sha256:" + "0" * 64,
                span=TextSpanRef(
                    block_id=StableId("block.test"),
                    start=0,
                    end=len(text),
                ),
                support_status=EvidenceSupportStatus.CURRENT,
                resolved_at_commit=COMMIT,
            ),
        )
        if evidence
        else ()
    )
    return RetrievalUnit(
        unit_id=StableId(unit_id),
        unit_kind=RetrievalUnitKind(kind),
        source_commit=COMMIT,
        snapshot_id=StableId("snapshot.test"),
        text=text,
        entity_ids=entity_ids,
        predicate=predicate,
        access_scope=access_scope,
        evidence_refs=refs,
    )


def _candidate(unit: RetrievalUnit, *, selected: bool = True) -> FusedCandidate:
    from novel_agent.domain.memory import ChannelHit, RetrievalChannel

    hit = ChannelHit(
        unit=unit,
        channel=RetrievalChannel.R1_EXACT,
        channel_rank=1,
        raw_score=1.0,
        candidate_count=1,
        hit_reason="test",
    )
    return FusedCandidate(
        unit=unit,
        fused_rank=1,
        rrf_score=1.0,
        channel_hits=(hit,),
        selected=selected,
    )


def test_structured_anchor_closes_its_facet_only() -> None:
    need = _need(facets=(NeedFacetKind.CURRENT_STATE, NeedFacetKind.RELATION_STATE))
    state_unit = _unit("unit.state", kind="state_anchor", predicate="location")
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(state_unit),))
    by_kind = {receipt.facet_kind: receipt for receipt in receipts}
    assert by_kind[NeedFacetKind.CURRENT_STATE].status is FacetClosureStatus.SUPPORTED
    assert by_kind[NeedFacetKind.RELATION_STATE].status is FacetClosureStatus.UNSUPPORTED
    assert by_kind[NeedFacetKind.CURRENT_STATE].supporting_unit_ids == (state_unit.unit_id,)


def test_same_kind_matching_predicate_closes_facet() -> None:
    # Same kind, same predicate as the Need: closes the current_state facet.
    need = _need(facets=(NeedFacetKind.CURRENT_STATE,), predicates=("location",))
    state_unit = _unit("unit.state", kind="state_anchor", predicate="location")
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(state_unit),))
    assert receipts[0].status is FacetClosureStatus.SUPPORTED


def test_same_kind_different_predicate_does_not_close_facet() -> None:
    # Review follow-up P1: same unit kind, same entity, different predicate --
    # a possession/belief state record must NOT close knowledge_boundary or
    # capability_status, which is exactly the P001 false closure found.
    need = _need(
        facets=(NeedFacetKind.KNOWLEDGE_BOUNDARY, NeedFacetKind.CAPABILITY_STATUS),
        predicates=("location", "enrollment_status"),
    )
    state_unit = _unit("unit.state", kind="state_anchor", predicate="possession")
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(state_unit),))
    assert all(receipt.status is FacetClosureStatus.UNSUPPORTED for receipt in receipts)
    assert not FacetSupportEvaluator.mandatory_closed(need, receipts)


def test_anchor_with_absent_predicate_does_not_close_facet() -> None:
    # A same-kind anchor without a predicate cannot prove the semantic match.
    need = _need(facets=(NeedFacetKind.CURRENT_STATE,), predicates=("location",))
    state_unit = _unit("unit.state", kind="state_anchor", predicate=None)
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(state_unit),))
    assert receipts[0].status is FacetClosureStatus.UNSUPPORTED


def test_need_without_declared_predicates_does_not_close() -> None:
    # An empty need.predicates cannot prove predicate support; fail closed
    # rather than closing every same-kind anchor (review follow-up P1).
    need = _need(facets=(NeedFacetKind.CURRENT_STATE,), predicates=())
    state_unit = _unit("unit.state", kind="state_anchor", predicate="location")
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(state_unit),))
    assert receipts[0].status is FacetClosureStatus.UNSUPPORTED


def test_multi_facet_need_each_predicate_closes_only_its_own_facet() -> None:
    # Review second follow-up P1: two valid predicates in one multi-facet Need
    # must each close only the facet they are bound to.  A knowledge predicate
    # closes knowledge_boundary, never capability_status; a capability
    # predicate closes capability_status, never knowledge_boundary -- even
    # though both are state anchors of the same entity.
    need = _need(
        facets=(NeedFacetKind.KNOWLEDGE_BOUNDARY, NeedFacetKind.CAPABILITY_STATUS),
        predicates=("knowledge_secret", "skill_boundary"),
        predicates_by_facet={
            NeedFacetKind.KNOWLEDGE_BOUNDARY: ("knowledge_secret",),
            NeedFacetKind.CAPABILITY_STATUS: ("skill_boundary",),
        },
    )
    knowledge_unit = _unit("unit.knowledge", kind="state_anchor", predicate="knowledge_secret")
    capability_unit = _unit("unit.capability", kind="state_anchor", predicate="skill_boundary")

    knowledge_receipts = FacetSupportEvaluator.evaluate(need, (_candidate(knowledge_unit),))
    by_kind = {receipt.facet_kind: receipt for receipt in knowledge_receipts}
    assert by_kind[NeedFacetKind.KNOWLEDGE_BOUNDARY].status is FacetClosureStatus.SUPPORTED
    assert by_kind[NeedFacetKind.CAPABILITY_STATUS].status is FacetClosureStatus.UNSUPPORTED

    capability_receipts = FacetSupportEvaluator.evaluate(need, (_candidate(capability_unit),))
    by_kind = {receipt.facet_kind: receipt for receipt in capability_receipts}
    assert by_kind[NeedFacetKind.CAPABILITY_STATUS].status is FacetClosureStatus.SUPPORTED
    assert by_kind[NeedFacetKind.KNOWLEDGE_BOUNDARY].status is FacetClosureStatus.UNSUPPORTED

    # Both anchors together close both facets, each via its own predicate.
    both = FacetSupportEvaluator.evaluate(
        need, (_candidate(knowledge_unit), _candidate(capability_unit))
    )
    by_kind = {receipt.facet_kind: receipt for receipt in both}
    assert by_kind[NeedFacetKind.KNOWLEDGE_BOUNDARY].status is FacetClosureStatus.SUPPORTED
    assert by_kind[NeedFacetKind.CAPABILITY_STATUS].status is FacetClosureStatus.SUPPORTED


def test_fact_anchor_entity_identity_is_not_a_semantic_witness() -> None:
    # FACT_ANCHOR projects entity identity; it never closes a semantic facet.
    need = _need(facets=(NeedFacetKind.KNOWLEDGE_BOUNDARY,), predicates=("location",))
    fact_unit = _unit("unit.fact", kind="fact_anchor", predicate="entity_identity")
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(fact_unit),))
    assert receipts[0].status is FacetClosureStatus.UNSUPPORTED


def test_obligation_anchor_closes_commitment_facets() -> None:
    # Durable obligations project to PLAN_ANCHOR with access_scope
    # "writer_safe"; they must close COMMITMENT/UNRESOLVED_STATUS, not
    # PLAN_NODE (review follow-up P1: the old special case made the accepted
    # obligation mapping unreachable).
    need = _need(
        facets=(NeedFacetKind.COMMITMENT, NeedFacetKind.PLAN_NODE),
        predicates=("promise",),
    )
    obligation_unit = _unit(
        "unit.obligation",
        kind="plan_anchor",
        predicate="promise",
        access_scope="writer_safe",
    )
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(obligation_unit),))
    by_kind = {receipt.facet_kind: receipt for receipt in receipts}
    assert by_kind[NeedFacetKind.COMMITMENT].status is FacetClosureStatus.SUPPORTED
    assert by_kind[NeedFacetKind.PLAN_NODE].status is FacetClosureStatus.UNSUPPORTED


def test_plan_provenance_anchor_closes_only_plan_node() -> None:
    # Plan provenance (author_planning scope) closes PLAN_NODE only.
    need = _need(
        facets=(NeedFacetKind.COMMITMENT, NeedFacetKind.PLAN_NODE),
        predicates=("bootstrap_author_intent",),
    )
    plan_unit = _unit(
        "unit.plan-node",
        kind="plan_anchor",
        predicate="bootstrap_author_intent",
        access_scope="author_planning",
    )
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(plan_unit),))
    by_kind = {receipt.facet_kind: receipt for receipt in receipts}
    assert by_kind[NeedFacetKind.PLAN_NODE].status is FacetClosureStatus.SUPPORTED
    assert by_kind[NeedFacetKind.COMMITMENT].status is FacetClosureStatus.UNSUPPORTED


def test_grounded_exact_slice_does_not_close_semantic_facets() -> None:
    # 2026-08-14 review P1-1: raw grounded text evidence carries no world-record
    # predicate, so retrieval relevance must not be confused with semantic
    # support.  A grounded slice about the same entity must not close any
    # semantic facet (e.g. causal_history or setup in a 0-event world).
    need = _need(facets=(NeedFacetKind.SETUP, NeedFacetKind.UNRESOLVED_STATUS))
    grounded = _unit("unit.grounded", kind="grounded_span")
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(grounded),))
    assert all(receipt.status is FacetClosureStatus.UNSUPPORTED for receipt in receipts)
    assert not FacetSupportEvaluator.mandatory_closed(need, receipts)


def test_grounded_block_does_not_close_facets_either() -> None:
    need = _need(facets=(NeedFacetKind.CAUSAL_HISTORY, NeedFacetKind.KNOWLEDGE_BOUNDARY))
    grounded = _unit("unit.grounded-block", kind="grounded_block")
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(grounded),))
    assert all(receipt.status is FacetClosureStatus.UNSUPPORTED for receipt in receipts)


def test_same_entity_different_predicate_does_not_close_facet() -> None:
    # Negative regression from the review: an exact slice about the same entity
    # but a different predicate (a state record, not a relation record) must not
    # close relation_state.  Predicates are set on both sides so this proves
    # predicate mismatch, not merely kind mismatch.
    need = _need(
        facets=(NeedFacetKind.CURRENT_STATE, NeedFacetKind.RELATION_STATE),
        predicates=("location", "possesses"),
    )
    state_unit = _unit("unit.state", kind="state_anchor", predicate="location")
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(state_unit),))
    by_kind = {receipt.facet_kind: receipt for receipt in receipts}
    assert by_kind[NeedFacetKind.CURRENT_STATE].status is FacetClosureStatus.SUPPORTED
    assert by_kind[NeedFacetKind.RELATION_STATE].status is FacetClosureStatus.UNSUPPORTED
    assert by_kind[NeedFacetKind.CURRENT_STATE].supporting_unit_ids == (state_unit.unit_id,)


def test_navigation_anchor_never_closes_a_facet() -> None:
    need = _need(facets=(NeedFacetKind.CURRENT_STATE,))
    scene = _unit("unit.scene", kind="scene_anchor")
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(scene),))
    assert receipts[0].status is FacetClosureStatus.UNSUPPORTED
    assert not FacetSupportEvaluator.mandatory_closed(need, receipts)


def test_no_candidates_is_exhausted_and_empty_candidates_stay_unclosed() -> None:
    need = _need(facets=(NeedFacetKind.CURRENT_STATE,))
    receipts = FacetSupportEvaluator.evaluate(need, ())
    assert receipts[0].status is FacetClosureStatus.EXHAUSTED
    assert not FacetSupportEvaluator.mandatory_closed(need, receipts)


def test_unit_without_exact_evidence_does_not_close() -> None:
    need = _need(facets=(NeedFacetKind.CURRENT_STATE,))
    unit = _unit("unit.no-evidence", kind="state_anchor", evidence=False)
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(unit),))
    assert receipts[0].status is FacetClosureStatus.UNSUPPORTED


def test_entity_mismatch_does_not_close() -> None:
    need = _need(facets=(NeedFacetKind.CURRENT_STATE,))
    other = StableId("entity.test.other")
    unit = _unit("unit.other", kind="state_anchor", entity_ids=(other,))
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(unit),))
    assert receipts[0].status is FacetClosureStatus.UNSUPPORTED


def test_optional_facet_gap_does_not_block_mandatory_closure() -> None:
    need = _need(
        facets=(NeedFacetKind.CURRENT_STATE, NeedFacetKind.RELATION_STATE),
        mandatory=False,
    )
    state_unit = _unit("unit.state", kind="state_anchor")
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(state_unit),))
    assert FacetSupportEvaluator.mandatory_closed(need, receipts)


def test_not_executed_receipts_are_typed() -> None:
    need = _need(facets=(NeedFacetKind.CURRENT_STATE,))
    receipts = FacetSupportEvaluator.not_executed(need)
    assert receipts[0].status is FacetClosureStatus.NOT_EXECUTED
    assert receipts[0].stop_reason == "no_executable_query"


def test_facet_outside_completion_spec_is_skipped() -> None:
    need = _need(facets=(NeedFacetKind.CURRENT_STATE,))
    extra = NeedFacet(
        need_facet_id=StableId("facet.extra.relation"),
        need_id=need.need_id,
        facet_kind=NeedFacetKind.RELATION_STATE,
        expected_claim_scope=ExpectedClaimScope.CURRENT,
        derivation_refs=(need.need_id,),
        producer="test",
        producer_version="v1",
        information_scope="cutoff_safe",
    )
    need = need.model_copy(update={"need_facets": (*need.need_facets, extra)})
    unit = _unit("unit.state", kind="state_anchor")
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(unit),))
    assert len(receipts) == 1
    assert receipts[0].need_facet_id != extra.need_facet_id


def test_need_without_completion_spec_cannot_close() -> None:
    # Facet-level predicate binding lives on NeedCompletionSpec; without it
    # there is no binding to consult, so no facet can close (fail-closed).
    need = _need(facets=(NeedFacetKind.CURRENT_STATE,))
    unit = _unit("unit.state", kind="state_anchor")
    need = need.model_copy(update={"completion_spec": None})
    receipts = FacetSupportEvaluator.evaluate(need, (_candidate(unit),))
    assert len(receipts) == 1
    assert receipts[0].status is FacetClosureStatus.UNSUPPORTED


def test_domain_validates_canonical_goal_binding_and_facet_receipts() -> None:
    import pytest
    from pydantic import ValidationError

    from novel_agent.domain.memory import (
        FacetEvidenceReceipt,
        RetrievalStopReason,
        RetrievalTrace,
    )

    need = _need(facets=(NeedFacetKind.CURRENT_STATE,))
    need = need.model_copy(update={"trigger_plan_chapters": (21,)})
    with pytest.raises(ValidationError, match="canonical goal binding"):
        type(need).model_validate(
            {
                **need.model_dump(),
                "canonical_goal_by_chapter": {22: "other chapter"},
            }
        )

    # Facet receipts must belong to the Need's required facets.
    foreign = FacetEvidenceReceipt(
        need_id=need.need_id,
        need_facet_id=StableId("facet.foreign"),
        facet_kind=NeedFacetKind.CURRENT_STATE,
        mandatory=True,
        status=FacetClosureStatus.SUPPORTED,
    )
    with pytest.raises(ValidationError, match="facet receipts must belong"):
        RetrievalTrace(
            need_id=need.need_id,
            intent=need.query_intent,
            allowed_channels=(),
            channel_candidate_counts={},
            candidates=(),
            fusion_applied=False,
            stop_reason=RetrievalStopReason.CANDIDATES_EXHAUSTED,
            facet_receipts=(foreign,),
        )

    # closed_need_facet_ids must equal the supported receipts.
    receipt = FacetEvidenceReceipt(
        need_id=need.need_id,
        need_facet_id=need.need_facets[0].need_facet_id,
        facet_kind=NeedFacetKind.CURRENT_STATE,
        mandatory=True,
        status=FacetClosureStatus.SUPPORTED,
        supporting_unit_ids=(StableId("unit.x"),),
    )
    with pytest.raises(ValidationError, match="closed Need facets must match"):
        RetrievalTrace(
            need_id=need.need_id,
            intent=need.query_intent,
            allowed_channels=(),
            channel_candidate_counts={},
            candidates=(),
            fusion_applied=False,
            stop_reason=RetrievalStopReason.EXACT_SATISFIED,
            required_need_facet_ids=(need.need_facets[0].need_facet_id,),
            facet_receipts=(receipt,),
            closed_need_facet_ids=(),
        )
