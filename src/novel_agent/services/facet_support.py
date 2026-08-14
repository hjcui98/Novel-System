"""Deterministic per-facet evidence closure shared by route, support and package.

One evaluator computes, for each required ``NeedFacet`` of a public Need, whether
the selected retrieval candidates carry exact L0 evidence that can serve that
facet.  The receipts are attached to the ``RetrievalTrace`` and to every
``SliceSelectionTrace`` (as ``supported_facet_ids``), so route stop reasons,
support diagnostics and package gaps all speak the same facet language: a route
may only claim ``exact_satisfied``/``budget_satisfied`` for facets that the
package will also expose as exact evidence, and a package gap always has the
same typed reason the route recorded.

Deterministic rules only: structured anchors close the facets their record kind
stands for AND whose predicate the Need declared; grounded exact slices and
entity-identity FACT_ANCHOR units are raw evidence and never by themselves
close a semantic facet; obligation-projected PLAN_ANCHOR units (writer_safe)
close commitment facets while plan provenance closes only PLAN_NODE;
scene/chapter/arc anchors are navigation handles and never close a facet.
No model, no Gold and no claim synthesis is involved.
"""

from __future__ import annotations

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import (
    FacetClosureStatus,
    FacetEvidenceReceipt,
    FusedCandidate,
    NeedFacet,
    NeedFacetKind,
    RequirementLevel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
)

_NAVIGATION_KINDS = frozenset(
    {
        RetrievalUnitKind.SCENE_ANCHOR,
        RetrievalUnitKind.CHAPTER_ANCHOR,
        RetrievalUnitKind.ARC_ANCHOR,
    }
)

_STRUCTURED_KINDS_BY_FACET: dict[NeedFacetKind, frozenset[RetrievalUnitKind]] = {
    NeedFacetKind.CURRENT_STATE: frozenset({RetrievalUnitKind.STATE_ANCHOR}),
    NeedFacetKind.CAPABILITY_STATUS: frozenset({RetrievalUnitKind.STATE_ANCHOR}),
    NeedFacetKind.LIMITATION: frozenset({RetrievalUnitKind.STATE_ANCHOR}),
    NeedFacetKind.KNOWLEDGE_BOUNDARY: frozenset({RetrievalUnitKind.STATE_ANCHOR}),
    NeedFacetKind.RELATION_STATE: frozenset({RetrievalUnitKind.RELATION_ANCHOR}),
    NeedFacetKind.CAUSAL_HISTORY: frozenset({RetrievalUnitKind.EVENT_ANCHOR}),
    NeedFacetKind.SETUP: frozenset({RetrievalUnitKind.EVENT_ANCHOR}),
    # Durable open obligations project to PLAN_ANCHOR units with
    # access_scope="writer_safe" (see memory_pipeline/r1); plan provenance
    # (plan nodes, chapter goals) projects with access_scope="author_planning".
    # _facets_for_unit resolves that scope so obligations close commitment
    # facets while plan nodes close only PLAN_NODE.
    NeedFacetKind.COMMITMENT: frozenset(
        {RetrievalUnitKind.EVENT_ANCHOR, RetrievalUnitKind.PLAN_ANCHOR}
    ),
    NeedFacetKind.UNRESOLVED_STATUS: frozenset(
        {RetrievalUnitKind.EVENT_ANCHOR, RetrievalUnitKind.PLAN_ANCHOR}
    ),
    NeedFacetKind.PLAN_NODE: frozenset({RetrievalUnitKind.PLAN_ANCHOR}),
}

# FACT_ANCHOR units project entity-identity records (predicate
# "entity_identity"); they prove the entity exists, not that any semantic
# predicate holds, so they are not a semantic witness for any facet.
_PLAN_OBLIGATION_SCOPE = "writer_safe"


class FacetSupportEvaluator:
    """Compute per-facet exact-evidence closure over selected candidates."""

    version = "facet_support_evaluator.v1"

    @classmethod
    def evaluate(
        cls,
        need: Stage1MemoryNeed,
        candidates: tuple[FusedCandidate, ...],
    ) -> tuple[FacetEvidenceReceipt, ...]:
        """Return one receipt per required facet of the Need.

        ``status`` is ``SUPPORTED`` when at least one selected candidate
        carries exact evidence for the facet, ``UNSUPPORTED`` when candidates
        exist but none serves the facet, and ``EXHAUSTED`` when no candidates
        were selected at all.
        """
        units = tuple(candidate.unit for candidate in candidates)
        unit_facets = {unit.unit_id: cls._facets_for_unit(need, unit) for unit in units}
        required = (
            set(need.completion_spec.required_need_facet_ids)
            if need.completion_spec is not None
            else {facet.need_facet_id for facet in need.need_facets}
        )
        receipts: list[FacetEvidenceReceipt] = []
        for facet in need.need_facets:
            if facet.need_facet_id not in required:
                continue
            supporting = tuple(
                unit_id for unit_id, facets in unit_facets.items() if facet.need_facet_id in facets
            )
            if supporting:
                status = FacetClosureStatus.SUPPORTED
                stop_reason = "exact_evidence_served"
            elif not units:
                status = FacetClosureStatus.EXHAUSTED
                stop_reason = "no_candidates_selected"
            else:
                status = FacetClosureStatus.UNSUPPORTED
                stop_reason = "no_exact_evidence_for_facet"
            receipts.append(
                FacetEvidenceReceipt(
                    need_id=need.need_id,
                    need_facet_id=facet.need_facet_id,
                    facet_kind=facet.facet_kind,
                    mandatory=need.requirement is RequirementLevel.MANDATORY,
                    status=status,
                    supporting_unit_ids=supporting,
                    stop_reason=stop_reason,
                )
            )
        return tuple(receipts)

    @classmethod
    def supporting_facet_ids(
        cls,
        need: Stage1MemoryNeed,
        unit: RetrievalUnit,
    ) -> tuple[StableId, ...]:
        """Facet ids of the Need served by one unit (empty when none)."""
        return cls._facets_for_unit(need, unit)

    @classmethod
    def not_executed(
        cls,
        need: Stage1MemoryNeed,
    ) -> tuple[FacetEvidenceReceipt, ...]:
        """Receipts for a Need whose route had no executable query."""
        required = (
            set(need.completion_spec.required_need_facet_ids)
            if need.completion_spec is not None
            else {facet.need_facet_id for facet in need.need_facets}
        )
        return tuple(
            FacetEvidenceReceipt(
                need_id=need.need_id,
                need_facet_id=facet.need_facet_id,
                facet_kind=facet.facet_kind,
                mandatory=need.requirement is RequirementLevel.MANDATORY,
                status=FacetClosureStatus.NOT_EXECUTED,
                stop_reason="no_executable_query",
            )
            for facet in need.need_facets
            if facet.need_facet_id in required
        )

    @classmethod
    def mandatory_closed(
        cls,
        need: Stage1MemoryNeed,
        receipts: tuple[FacetEvidenceReceipt, ...],
    ) -> bool:
        """True when every mandatory required facet is supported."""
        del need
        if not receipts:
            return True
        return all(
            receipt.status is FacetClosureStatus.SUPPORTED
            for receipt in receipts
            if receipt.mandatory
        )

    @classmethod
    def closed_facet_ids(
        cls,
        receipts: tuple[FacetEvidenceReceipt, ...],
    ) -> tuple[StableId, ...]:
        return tuple(
            dict.fromkeys(
                receipt.need_facet_id
                for receipt in receipts
                if receipt.status is FacetClosureStatus.SUPPORTED
            )
        )

    @classmethod
    def _facets_for_unit(
        cls,
        need: Stage1MemoryNeed,
        unit: RetrievalUnit,
    ) -> tuple[StableId, ...]:
        """Deterministic unit -> facet binding for one Need.

        Structured anchors close only the facets whose record kind stands for
        the semantics AND whose predicate is in that facet's own binding
        (``NeedCompletionSpec.predicates_by_facet``, 2026-08-14 review second
        follow-up P1): a same-kind anchor with an absent or mismatched
        predicate does not close the facet, and a declared predicate closes
        only its bound facet.  An unbound facet cannot prove predicate
        support, so no semantic facet closes on it (fail-closed).
        FACT_ANCHOR (entity identity) is never a semantic witness; PLAN_ANCHOR
        resolves to obligation vs plan provenance by access_scope.
        """
        if not unit.evidence_refs:
            return ()
        facets = tuple(
            facet
            for facet in need.need_facets
            if need.completion_spec is None
            or facet.need_facet_id in need.completion_spec.required_need_facet_ids
        )
        if not facets:
            return ()
        if need.entity_ids and not (set(need.entity_ids) & set(unit.entity_ids)):
            return ()
        if unit.unit_kind in _NAVIGATION_KINDS:
            return ()
        # Grounded exact slices are raw text evidence, not world records: they
        # carry no predicate that identifies which facet semantics they serve.
        # Binding them to every non-plan facet would claim semantic support
        # from mere retrieval relevance (2026-08-14 review P1-1).
        if unit.unit_kind in (RetrievalUnitKind.GROUNDED_BLOCK, RetrievalUnitKind.GROUNDED_SPAN):
            return ()
        if unit.unit_kind is RetrievalUnitKind.FACT_ANCHOR:
            return ()
        if unit.unit_kind is RetrievalUnitKind.PLAN_ANCHOR:
            # Durable obligations (writer_safe) close commitment facets; plan
            # provenance (author_planning) closes only PLAN_NODE.
            if unit.access_scope == _PLAN_OBLIGATION_SCOPE:
                kind_facets: tuple[NeedFacetKind, ...] = (
                    NeedFacetKind.COMMITMENT,
                    NeedFacetKind.UNRESOLVED_STATUS,
                )
            else:
                kind_facets = (NeedFacetKind.PLAN_NODE,)
            return cls._matching_facets(need, unit, facets, kind_facets)
        kind_facets = tuple(
            facet_kind
            for facet_kind, kinds in _STRUCTURED_KINDS_BY_FACET.items()
            if unit.unit_kind in kinds
        )
        return cls._matching_facets(need, unit, facets, kind_facets)

    @classmethod
    def _matching_facets(
        cls,
        need: Stage1MemoryNeed,
        unit: RetrievalUnit,
        facets: tuple[NeedFacet, ...],
        kind_facets: tuple[NeedFacetKind, ...],
    ) -> tuple[StableId, ...]:
        """Facets whose kind the unit stands for AND whose binding matches.

        Facet-level predicate binding (2026-08-14 review second follow-up P1):
        each facet carries its own allowed predicates in
        ``NeedCompletionSpec.predicates_by_facet``, so a unit predicate closes
        only the facets whose binding contains it -- never every same-kind
        facet of the Need.  A facet without a binding, or a unit without a
        predicate, cannot be closed (fail-closed).
        """
        if unit.predicate is None:
            return ()
        spec = need.completion_spec
        if spec is None or not spec.predicates_by_facet:
            return ()
        return tuple(
            facet.need_facet_id
            for facet in facets
            if facet.facet_kind in kind_facets
            and unit.predicate in spec.predicates_by_facet.get(facet.need_facet_id.root, ())
        )


__all__ = ["FacetSupportEvaluator"]
