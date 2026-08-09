"""Exact, cutoff-safe provenance matching for Stage 2M Gold alternatives."""

from __future__ import annotations

from pydantic import Field

from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import GoldItem
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory_benchmark import EvidenceLedger, EvidenceSet, EvidenceStageCoverage
from novel_agent.domain.text import EvidenceRef


class GoldEvidenceMatch(DomainModel):
    matched: bool
    partially_matched: bool = False
    matched_ledger_ids: tuple[StableId, ...] = ()
    matched_evidence_set_ids: tuple[StableId, ...] = ()
    supported_components: tuple[str, ...] = ()
    reason: str = Field(min_length=1)


class GoldEvidenceMatcher:
    """Require a content-addressed evidence identity/span match."""

    version = "gold_evidence_matcher.v4"

    def __init__(self, *, minimum_span_coverage: float = 0.5) -> None:
        if not 0.0 < minimum_span_coverage <= 1.0:
            raise ValueError("minimum span coverage must be in (0, 1]")
        self._minimum_span_coverage = minimum_span_coverage

    def match(self, gold: GoldItem, ledger: EvidenceLedger) -> GoldEvidenceMatch:
        alternatives = self.accepted_alternatives(gold)
        matched_entries: list[StableId] = []
        partial_entries: list[StableId] = []
        matched_sets: list[StableId] = []
        components: list[str] = []
        for alternative in alternatives:
            entry_ids, complete = self._match_alternative(alternative, ledger)
            partial_entries.extend(entry_ids)
            if not complete:
                continue
            matched_sets.append(alternative.evidence_set_id)
            matched_entries.extend(entry_ids)
            components.extend(alternative.component_ids)
        if not matched_sets:
            return GoldEvidenceMatch(
                matched=False,
                partially_matched=bool(partial_entries),
                matched_ledger_ids=tuple(dict.fromkeys(partial_entries)),
                reason=(
                    "only part of an accepted evidence alternative resolves in the frozen ledger"
                    if partial_entries
                    else "no accepted evidence alternative resolves in the frozen ledger"
                ),
            )
        return GoldEvidenceMatch(
            matched=True,
            partially_matched=True,
            matched_ledger_ids=tuple(dict.fromkeys(matched_entries)),
            matched_evidence_set_ids=tuple(dict.fromkeys(matched_sets)),
            supported_components=tuple(dict.fromkeys(components)),
            reason="an accepted evidence alternative resolves in the frozen ledger",
        )

    def coverage(self, gold: GoldItem, ledger: EvidenceLedger) -> EvidenceStageCoverage:
        """Count accepted references resolved at one evaluator-visible pipeline stage."""

        alternatives = self.accepted_alternatives(gold)
        accepted_count = 0
        matched_count = 0
        complete: list[StableId] = []
        partial: list[StableId] = []
        for alternative in alternatives:
            alternative_total = len(alternative.evidence_refs) + len(alternative.plan_node_ids)
            alternative_matched = 0
            for expected in alternative.evidence_refs:
                if any(
                    self._ref_matches(expected, actual)
                    for entry in ledger.entries
                    for actual in entry.evidence_refs
                ):
                    alternative_matched += 1
            for plan_node_id in alternative.plan_node_ids:
                if any(plan_node_id in entry.plan_node_ids for entry in ledger.entries):
                    alternative_matched += 1
            accepted_count += alternative_total
            matched_count += alternative_matched
            if alternative_total and alternative_matched == alternative_total:
                complete.append(alternative.evidence_set_id)
            elif alternative_matched:
                partial.append(alternative.evidence_set_id)
        return EvidenceStageCoverage(
            accepted_reference_count=accepted_count,
            matched_reference_count=matched_count,
            complete_alternative_ids=tuple(complete),
            partial_alternative_ids=tuple(partial),
        )

    def accepted_alternatives(self, gold: GoldItem) -> tuple[EvidenceSet, ...]:
        """Resolve the immutable accepted alternatives used by matcher v3."""

        return gold.accepted_evidence_sets or self._legacy_alternatives(gold)

    def text_reference_recall(self, alternative: EvidenceSet, ledger: EvidenceLedger) -> float:
        """Return exact matcher-v3 recall for one alternative's text references."""

        if not alternative.evidence_refs:
            raise ValueError("historical recall alternative has no text evidence references")
        matched = sum(
            any(
                self._ref_matches(expected, actual)
                for entry in ledger.entries
                for actual in entry.evidence_refs
            )
            for expected in alternative.evidence_refs
        )
        return matched / len(alternative.evidence_refs)

    def _match_alternative(
        self,
        alternative: EvidenceSet,
        ledger: EvidenceLedger,
    ) -> tuple[tuple[StableId, ...], bool]:
        matched_entry_ids: list[StableId] = []
        complete = True
        for expected in alternative.evidence_refs:
            matching = [
                entry.ledger_id
                for entry in ledger.entries
                if any(self._ref_matches(expected, actual) for actual in entry.evidence_refs)
            ]
            if not matching:
                complete = False
                continue
            matched_entry_ids.extend(matching)
        for plan_node_id in alternative.plan_node_ids:
            matching = [
                entry.ledger_id for entry in ledger.entries if plan_node_id in entry.plan_node_ids
            ]
            if not matching:
                complete = False
                continue
            matched_entry_ids.extend(matching)
        return tuple(dict.fromkeys(matched_entry_ids)), complete

    def _ref_matches(self, expected: EvidenceRef, actual: EvidenceRef) -> bool:
        # Evidence credit never crosses an unproven TextRoot ancestry.  Equal
        # object bytes/coordinates may be deduplicated only after the importer
        # has bound both references to the same canonical observed root.
        if expected.root_hash != actual.root_hash:
            return False
        if expected.evidence_id == actual.evidence_id:
            return True
        if expected.object_hash != actual.object_hash:
            return False
        # Object equality without exact evidence id is only sufficient when both
        # references have precise spans. This forbids whole-chapter coincidence.
        if expected.span is None or actual.span is None:
            return False
        # Block ids may be independently namespaced inside one proven root, so
        # exact object content plus coordinates remain the portable child key.
        overlap = max(
            0,
            min(expected.span.end, actual.span.end) - max(expected.span.start, actual.span.start),
        )
        # Human Gold may name a reviewed chapter/block while the memory curator
        # emits the precise sentence span that supports a frozen claim. Measure
        # overlap against the narrower span so a precise child span can resolve
        # inside that reviewed block. This is provenance candidate matching only:
        # the independent semantic verifier still has to establish HIT/PARTIAL.
        expected_width = max(1, expected.span.end - expected.span.start)
        actual_width = max(1, actual.span.end - actual.span.start)
        return overlap / min(expected_width, actual_width) >= self._minimum_span_coverage

    @staticmethod
    def _legacy_alternatives(gold: GoldItem) -> tuple[EvidenceSet, ...]:
        alternatives: list[EvidenceSet] = []
        for index, evidence in enumerate(gold.evidence_refs, start=1):
            alternatives.append(
                EvidenceSet(
                    evidence_set_id=StableId(f"accepted.legacy.{gold.gold_id.root}.{index}"[:128]),
                    evidence_refs=(evidence,),
                )
            )
        for index, plan in enumerate(gold.plan_evidence_refs, start=1):
            alternatives.append(
                EvidenceSet(
                    evidence_set_id=StableId(
                        f"accepted.legacy-plan.{gold.gold_id.root}.{index}"[:128]
                    ),
                    plan_node_ids=(plan.goal_id,),
                )
            )
        return tuple(alternatives)
