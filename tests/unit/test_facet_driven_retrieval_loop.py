"""Facet-driven retrieval loop (2026-08-13 repair C): stop only on closure."""

from __future__ import annotations

from novel_agent.domain.ids import CommitId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    ExpectedClaimScope,
    FacetClosureStatus,
    FacetEvidenceRequirement,
    NeedCompletionSpec,
    NeedExecutionStatus,
    NeedFacet,
    NeedFacetKind,
    NeedGapPolicy,
    NeedRisk,
    NeedUncertaintyPolicy,
    RequirementLevel,
    ResolutionPath,
    RetrievalStopReason,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
from novel_agent.services.retrieval import FusionService, RetrievalOrchestrator

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.test")
ENTITY = StableId("entity.test.chen")


def _need(
    facets: tuple[NeedFacetKind, ...],
    predicates: tuple[str, ...] = ("location", "possesses"),
) -> Stage1MemoryNeed:
    need_id = StableId("need.test")
    digest = "1234567890abcdef"
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
    spec = NeedCompletionSpec(
        need_id=need_id,
        required_need_facet_ids=tuple(item.need_facet_id for item in need_facets),
        irreducible_need_facet_ids=tuple(item.need_facet_id for item in need_facets),
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
        predicates_by_facet={item.need_facet_id.root: predicates for item in need_facets},
    )
    return Stage1MemoryNeed(
        need_id=need_id,
        run_id=RunId("run.test"),
        task_id=TaskId("task.test"),
        base_commit=COMMIT,
        horizon_target=(21, 25),
        need_type="test",
        query_intent=Stage1QueryIntent.CURRENT_STATE,
        query_text="陈长生当前伤势",
        entity_ids=(ENTITY,),
        predicates=predicates,
        access_scope="writer_safe",
        why_needed="test",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.EXACT_TEMPORAL,
        allowed_candidate_pools=(CandidatePool.R1, CandidatePool.ANCHOR),
        stop_condition="served by cutoff-safe exact evidence slices or an explicit typed gap",
        need_facets=need_facets,
        completion_spec=spec,
    )


def _unit(
    identity: str,
    kind: RetrievalUnitKind,
    text: str,
    predicate: str = "location",
) -> RetrievalUnit:
    return RetrievalUnit(
        unit_id=StableId(identity),
        unit_kind=kind,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        text=text,
        entity_ids=(ENTITY,),
        predicate=predicate,
        evidence_refs=(
            EvidenceRef(
                evidence_id=StableId(f"evidence.{identity}"),
                root_hash="sha256:" + "b" * 64,
                object_hash="sha256:" + "b" * 64,
                span=TextSpanRef(block_id=StableId("block.test"), start=0, end=4),
                support_status=EvidenceSupportStatus.CURRENT,
                resolved_at_commit=COMMIT,
            ),
        ),
    )


def _orchestrator(
    units: tuple[RetrievalUnit, ...],
    *,
    window: int = 1,
    max_window: int = 2,
) -> RetrievalOrchestrator:
    from novel_agent.services.retrieval import InMemoryRetrievalBackend

    return RetrievalOrchestrator(
        InMemoryRetrievalBackend(units),
        FusionService(),
        per_channel_limit=window,
        fused_limit=window,
        window_step=window,
        max_window=max_window,
    )


def test_loop_stops_on_first_window_when_all_facets_served() -> None:
    units = (
        _unit("anchor.state", RetrievalUnitKind.STATE_ANCHOR, "陈长生 伤势 未愈"),
        _unit(
            "anchor.relation",
            RetrievalUnitKind.RELATION_ANCHOR,
            "陈长生 师徒 关系",
            predicate="possesses",
        ),
    )
    trace = _orchestrator(units, window=2).retrieve(_need((NeedFacetKind.CURRENT_STATE,)))
    assert trace.stop_reason is RetrievalStopReason.EXACT_SATISFIED
    assert trace.retrieval_pages == 1
    assert trace.closed_need_facet_ids == (trace.required_need_facet_ids[0],)
    assert trace.facet_receipts[0].status is FacetClosureStatus.SUPPORTED


def test_no_executable_query_preserves_facet_contract_diagnostics() -> None:
    need = _need((NeedFacetKind.CURRENT_STATE,)).model_copy(
        update={
            "query_intent": Stage1QueryIntent.RELATION_CHAIN,
            "entity_ids": (),
            "predicates": (),
            "allowed_candidate_pools": (CandidatePool.GRAPH,),
        }
    )

    trace = _orchestrator((), window=1).retrieve(need)

    assert trace.need_execution_status is NeedExecutionStatus.NOT_EXECUTED_NO_EXECUTABLE_QUERY
    assert trace.stop_reason is RetrievalStopReason.NO_EXECUTABLE_QUERY
    completion_spec = need.completion_spec
    assert completion_spec is not None
    assert trace.required_need_facet_ids == completion_spec.required_need_facet_ids
    assert trace.irreducible_need_facet_ids == completion_spec.irreducible_need_facet_ids
    assert tuple(item.need_facet_id for item in trace.facet_receipts) == (
        completion_spec.required_need_facet_ids
    )


def test_loop_expands_window_until_missing_facet_is_served() -> None:
    # Both units serve one facet each; a window of one finds the state anchor
    # first, so the relation facet stays open and the loop must widen.
    units = (
        _unit("anchor.state", RetrievalUnitKind.STATE_ANCHOR, "陈长生 伤势 未愈"),
        _unit(
            "anchor.relation",
            RetrievalUnitKind.RELATION_ANCHOR,
            "陈长生 师徒 关系",
            predicate="possesses",
        ),
    )
    trace = _orchestrator(units, window=1, max_window=2).retrieve(
        _need((NeedFacetKind.CURRENT_STATE, NeedFacetKind.RELATION_STATE))
    )
    assert trace.retrieval_pages == 2
    assert trace.stop_reason is RetrievalStopReason.EXACT_SATISFIED
    by_kind = {receipt.facet_kind: receipt for receipt in trace.facet_receipts}
    assert by_kind[NeedFacetKind.CURRENT_STATE].status is FacetClosureStatus.SUPPORTED
    assert by_kind[NeedFacetKind.RELATION_STATE].status is FacetClosureStatus.SUPPORTED
    assert set(trace.closed_need_facet_ids) == set(trace.required_need_facet_ids)


def test_loop_exhausts_windows_without_closure_and_reports_receipts() -> None:
    units = (_unit("anchor.state", RetrievalUnitKind.STATE_ANCHOR, "陈长生 伤势 未愈"),)
    trace = _orchestrator(units, window=1, max_window=2).retrieve(
        _need((NeedFacetKind.CURRENT_STATE, NeedFacetKind.RELATION_STATE))
    )
    # The CURRENT_STATE route legally runs its anchor fallback once windows are
    # exhausted; the relation facet stays open, so the stop reason is the
    # fallback-exhausted typed failure.
    assert trace.stop_reason is RetrievalStopReason.FALLBACK_EXHAUSTED
    by_kind = {receipt.facet_kind: receipt for receipt in trace.facet_receipts}
    assert by_kind[NeedFacetKind.CURRENT_STATE].status is FacetClosureStatus.SUPPORTED
    assert by_kind[NeedFacetKind.RELATION_STATE].status is FacetClosureStatus.UNSUPPORTED
    assert trace.closed_need_facet_ids == (trace.required_need_facet_ids[0],)


def test_route_never_claims_exact_when_only_candidates_exist() -> None:
    # A candidate without exact evidence must not close any facet (the P001
    # "any selected candidate counts as done" failure mode).
    no_evidence = _unit("anchor.no-evidence", RetrievalUnitKind.STATE_ANCHOR, "陈长生 伤势 未愈")
    no_evidence = no_evidence.model_copy(update={"evidence_refs": ()})
    trace = _orchestrator((no_evidence,), window=2).retrieve(_need((NeedFacetKind.CURRENT_STATE,)))
    assert trace.stop_reason is not RetrievalStopReason.EXACT_SATISFIED
    assert trace.facet_receipts[0].status is FacetClosureStatus.UNSUPPORTED
    assert trace.closed_need_facet_ids == ()


def test_orchestrator_rejects_bad_window_configuration() -> None:
    import pytest

    from novel_agent.services.retrieval import InMemoryRetrievalBackend

    with pytest.raises(ValueError, match="window step and ceiling"):
        RetrievalOrchestrator(
            InMemoryRetrievalBackend(()),
            FusionService(),
            per_channel_limit=5,
            fused_limit=5,
            window_step=1,
            max_window=3,
        )
