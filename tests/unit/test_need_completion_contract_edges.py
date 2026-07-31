from __future__ import annotations

import pytest

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import (
    FacetEvidenceRequirement,
    NeedCompletionSpec,
    NeedGapPolicy,
    NeedUncertaintyPolicy,
)
from novel_agent.services.need_completion import (
    NeedCompletionEvaluator,
    NeedCompletionStatus,
    NeedFacetClosureState,
)


def _spec() -> NeedCompletionSpec:
    need_id = StableId("need.completion.edges")
    evidence_facet = StableId("need-facet.completion.evidence")
    plan_facet = StableId("need-facet.completion.plan")
    return NeedCompletionSpec(
        need_id=need_id,
        required_need_facet_ids=(evidence_facet, plan_facet),
        irreducible_need_facet_ids=(evidence_facet,),
        evidence_requirement_by_facet={
            evidence_facet.root: FacetEvidenceRequirement.TRACEABLE_CUTOFF_SOURCE,
            plan_facet.root: FacetEvidenceRequirement.PLAN_PROVENANCE,
        },
        min_distinct_evidence_sources=3,
        min_distinct_chapters=2,
        require_current_claim=True,
        require_causal_history=True,
        uncertainty_policy=NeedUncertaintyPolicy.REJECT_UNVERIFIED_CLAIM,
        gap_policy=NeedGapPolicy.FAIL_MANDATORY,
        producer="test",
        producer_version="test.v1",
    )


def test_need_completion_rejects_duplicate_mismatched_and_unknown_facets() -> None:
    spec = _spec()
    facet = spec.required_need_facet_ids[0]
    with pytest.raises(ValueError, match="must be unique"):
        NeedFacetClosureState(
            need_id=spec.need_id,
            verified_need_facet_ids=(facet, facet),
        )
    evaluator = NeedCompletionEvaluator()
    with pytest.raises(ValueError, match="different Need"):
        evaluator.evaluate(
            spec,
            NeedFacetClosureState(need_id=StableId("need.completion.other")),
        )
    with pytest.raises(ValueError, match="unknown facet"):
        evaluator.evaluate(
            spec,
            NeedFacetClosureState(
                need_id=spec.need_id,
                verified_need_facet_ids=(StableId("need-facet.completion.unknown"),),
            ),
        )


def test_need_completion_emits_typed_evidence_and_global_closure_diagnostics() -> None:
    spec = _spec()
    evidence_facet, plan_facet = spec.required_need_facet_ids
    evaluator = NeedCompletionEvaluator()

    unresolved = evaluator.evaluate(
        spec,
        NeedFacetClosureState(
            need_id=spec.need_id,
            verified_need_facet_ids=(evidence_facet, plan_facet),
        ),
    )
    assert f"FACET_EVIDENCE_UNRESOLVED:{evidence_facet.root}" in unresolved.diagnostic_codes
    assert f"FACET_PLAN_PROVENANCE_UNRESOLVED:{plan_facet.root}" in (unresolved.diagnostic_codes)

    unattested = evaluator.evaluate(
        spec,
        NeedFacetClosureState(
            need_id=spec.need_id,
            verified_need_facet_ids=(evidence_facet,),
            evidence_source_ids_by_facet={
                evidence_facet.root: (StableId("evidence.completion.one"),)
            },
        ),
    )
    assert f"FACET_CHAPTER_UNATTESTED:{evidence_facet.root}" in unattested.diagnostic_codes

    globally_incomplete = evaluator.evaluate(
        spec,
        NeedFacetClosureState(
            need_id=spec.need_id,
            verified_need_facet_ids=(evidence_facet, plan_facet),
            evidence_source_ids_by_facet={
                evidence_facet.root: (StableId("evidence.completion.one"),)
            },
            evidence_chapter_ids_by_facet={evidence_facet.root: (StableId("chapter.1"),)},
            plan_node_ids_by_facet={plan_facet.root: (StableId("plan-node.completion.one"),)},
        ),
    )
    assert globally_incomplete.status is NeedCompletionStatus.PARTIAL
    assert "MIN_DISTINCT_EVIDENCE_SOURCES_UNMET" in globally_incomplete.diagnostic_codes
    assert "MIN_DISTINCT_CHAPTERS_UNMET" in globally_incomplete.diagnostic_codes
    assert "CURRENT_CLAIM_REQUIRED" in globally_incomplete.diagnostic_codes
    assert "CAUSAL_HISTORY_REQUIRED" in globally_incomplete.diagnostic_codes

    complete = evaluator.evaluate(
        spec,
        NeedFacetClosureState(
            need_id=spec.need_id,
            verified_need_facet_ids=(evidence_facet, plan_facet),
            evidence_source_ids_by_facet={
                evidence_facet.root: (
                    StableId("evidence.completion.one"),
                    StableId("evidence.completion.two"),
                )
            },
            evidence_chapter_ids_by_facet={
                evidence_facet.root: (StableId("chapter.1"), StableId("chapter.2"))
            },
            plan_node_ids_by_facet={plan_facet.root: (StableId("plan-node.completion.one"),)},
            current_claim_facet_ids=(evidence_facet,),
            causal_history_facet_ids=(evidence_facet,),
        ),
    )
    assert complete.status is NeedCompletionStatus.REQUIRED_FACETS_CLOSED
