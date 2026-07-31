"""Gold-free evaluation of public NeedCompletionSpec closure."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import FacetEvidenceRequirement, NeedCompletionSpec


class NeedCompletionStatus(StrEnum):
    UNSEEN = "UNSEEN"
    PARTIAL = "PARTIAL"
    REQUIRED_FACETS_CLOSED = "REQUIRED_FACETS_CLOSED"


class NeedFacetClosureState(DomainModel):
    need_id: StableId
    verified_need_facet_ids: tuple[StableId, ...] = ()
    evidence_source_ids_by_facet: dict[str, tuple[StableId, ...]] = Field(default_factory=dict)
    evidence_chapter_ids_by_facet: dict[str, tuple[StableId, ...]] = Field(default_factory=dict)
    plan_node_ids_by_facet: dict[str, tuple[StableId, ...]] = Field(default_factory=dict)
    current_claim_facet_ids: tuple[StableId, ...] = ()
    causal_history_facet_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def validate_ids(self) -> NeedFacetClosureState:
        for values in (
            self.verified_need_facet_ids,
            self.current_claim_facet_ids,
            self.causal_history_facet_ids,
        ):
            if len(values) != len(set(values)):
                raise ValueError("NeedFacet closure ids must be unique")
        return self


class NeedCompletionResult(DomainModel):
    need_id: StableId
    status: NeedCompletionStatus
    closed_need_facet_ids: tuple[StableId, ...]
    missing_required_need_facet_ids: tuple[StableId, ...]
    missing_irreducible_need_facet_ids: tuple[StableId, ...]
    diagnostic_codes: tuple[str, ...] = ()


class NeedCompletionEvaluator:
    version = "need_completion_evaluator.v1"

    def evaluate(
        self,
        spec: NeedCompletionSpec,
        state: NeedFacetClosureState,
    ) -> NeedCompletionResult:
        if state.need_id != spec.need_id:
            raise ValueError("NeedFacet closure state belongs to a different Need")
        required = set(spec.required_need_facet_ids)
        verified = set(state.verified_need_facet_ids)
        referenced = {
            StableId(item)
            for item in (
                *state.evidence_source_ids_by_facet.keys(),
                *state.evidence_chapter_ids_by_facet.keys(),
                *state.plan_node_ids_by_facet.keys(),
            )
        }
        if not verified.issubset(required) or not referenced.issubset(required):
            raise ValueError("NeedFacet closure state references an unknown facet")

        closed: list[StableId] = []
        diagnostics: list[str] = []
        for facet_id in spec.required_need_facet_ids:
            key = facet_id.root
            sources = set(state.evidence_source_ids_by_facet.get(key, ()))
            chapters = set(state.evidence_chapter_ids_by_facet.get(key, ()))
            plan_nodes = set(state.plan_node_ids_by_facet.get(key, ()))
            if facet_id not in verified:
                continue
            requirement = spec.evidence_requirement_by_facet[key]
            if requirement is FacetEvidenceRequirement.PLAN_PROVENANCE:
                if not plan_nodes:
                    diagnostics.append(f"FACET_PLAN_PROVENANCE_UNRESOLVED:{key}")
                    continue
            else:
                if not sources:
                    diagnostics.append(f"FACET_EVIDENCE_UNRESOLVED:{key}")
                    continue
                if not chapters:
                    diagnostics.append(f"FACET_CHAPTER_UNATTESTED:{key}")
                    continue
            closed.append(facet_id)

        closed_set = set(closed)
        all_sources = {
            source
            for facet_id in closed
            for source in state.evidence_source_ids_by_facet.get(facet_id.root, ())
        }
        all_sources.update(
            node
            for facet_id in closed
            for node in state.plan_node_ids_by_facet.get(facet_id.root, ())
        )
        all_chapters = {
            chapter
            for facet_id in closed
            for chapter in state.evidence_chapter_ids_by_facet.get(facet_id.root, ())
        }
        if len(all_sources) < spec.min_distinct_evidence_sources:
            diagnostics.append("MIN_DISTINCT_EVIDENCE_SOURCES_UNMET")
        non_plan_closed = tuple(
            facet_id
            for facet_id in closed
            if spec.evidence_requirement_by_facet[facet_id.root]
            is not FacetEvidenceRequirement.PLAN_PROVENANCE
        )
        if non_plan_closed and len(all_chapters) < spec.min_distinct_chapters:
            diagnostics.append("MIN_DISTINCT_CHAPTERS_UNMET")
        if spec.require_current_claim and not closed_set.intersection(
            state.current_claim_facet_ids
        ):
            diagnostics.append("CURRENT_CLAIM_REQUIRED")
        if spec.require_causal_history and not closed_set.intersection(
            state.causal_history_facet_ids
        ):
            diagnostics.append("CAUSAL_HISTORY_REQUIRED")

        missing = tuple(item for item in spec.required_need_facet_ids if item not in closed_set)
        missing_irreducible = tuple(
            item for item in spec.irreducible_need_facet_ids if item not in closed_set
        )
        complete = not missing and not missing_irreducible and not diagnostics
        return NeedCompletionResult(
            need_id=spec.need_id,
            status=(
                NeedCompletionStatus.REQUIRED_FACETS_CLOSED
                if complete
                else NeedCompletionStatus.PARTIAL
                if verified
                else NeedCompletionStatus.UNSEEN
            ),
            closed_need_facet_ids=tuple(closed),
            missing_required_need_facet_ids=missing,
            missing_irreducible_need_facet_ids=missing_irreducible,
            diagnostic_codes=tuple(diagnostics),
        )


__all__ = [
    "NeedCompletionEvaluator",
    "NeedCompletionResult",
    "NeedCompletionStatus",
    "NeedFacetClosureState",
]
