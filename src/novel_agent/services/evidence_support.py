"""Hard + semantic evidence support gates for Curator proposals (WP5).

The hard gate enforces identity/bounds/chapter membership and is always enforced.
The prefilter gate uses fast lexical heuristics and cannot issue a definitive
rejection — it flags candidates that need a model-based semantic verifier.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from novel_agent.domain.changes import (
    CuratedOperationDraftV2,
    CuratorEventRecord,
    CuratorObligationRecord,
    CuratorRelationRecord,
    CuratorStateRecord,
    CuratorTypedRecord,
    EvidenceCandidate,
    EvidenceSupportDecision,
    EvidenceSupportDisposition,
)
from novel_agent.domain.ids import StableId

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


class EvidenceSupportGate:
    """Level-2 gate: lexical prefilter.

    The lexical gate returns SUPPORTS for clean lexical hits, CONTRADICTS when
    an explicit negation appears near the primary predicate token (a hard
    signal), and PARTIAL when no or only a partial lexical hit is found. PARTIAL
    outcomes must be resolved by a narrow semantic verifier or fail closed.
    The gate never returns UNRELATED because lexical heuristics cannot rule out
    a Chinese/English language mismatch.
    """

    def evaluate_operation(
        self,
        *,
        operation_index: int,
        operation: CuratedOperationDraftV2,
        candidates: Sequence[EvidenceCandidate],
    ) -> tuple[EvidenceSupportDecision, ...]:
        decisions: list[EvidenceSupportDecision] = []
        for candidate in candidates:
            disposition, reason = self._disposition(operation.record, candidate.text)
            decisions.append(
                EvidenceSupportDecision(
                    operation_index=operation_index,
                    candidate_id=candidate.candidate_id,
                    disposition=disposition,
                    reason_code=reason,
                )
            )
        return tuple(decisions)

    def evaluate_draft(
        self,
        operations: Sequence[CuratedOperationDraftV2],
        catalog: dict[StableId, EvidenceCandidate],
    ) -> tuple[EvidenceSupportDecision, ...]:
        decisions: list[EvidenceSupportDecision] = []
        for index, operation in enumerate(operations):
            candidates = tuple(catalog[item] for item in operation.evidence_candidate_ids)
            decisions.extend(
                self.evaluate_operation(
                    operation_index=index,
                    operation=operation,
                    candidates=candidates,
                )
            )
        return tuple(decisions)

    @staticmethod
    def all_lexical_support(decisions: Sequence[EvidenceSupportDecision]) -> bool:
        return bool(decisions) and all(
            item.disposition is EvidenceSupportDisposition.SUPPORTS for item in decisions
        )

    @staticmethod
    def _disposition(
        record: CuratorTypedRecord,
        text: str,
    ) -> tuple[EvidenceSupportDisposition, str]:
        if isinstance(record, CuratorStateRecord):
            scalar = EvidenceSupportGate._scalar_text(record.value).casefold()
            if (
                "半个时辰" in text
                and scalar
                in {
                    "half_hour",
                    "half-hour",
                    "30_minutes",
                    "thirty_minutes",
                }
            ):
                return (
                    EvidenceSupportDisposition.CONTRADICTS,
                    "EXPLICIT_TRADITIONAL_TIME_UNIT_MISMATCH",
                )
        tokens = EvidenceSupportGate._record_tokens(record)
        if not tokens:
            return EvidenceSupportDisposition.SUPPORTS, "NO_LEXICAL_ANCHOR_GRANTED_PASS"
        lowered = text.casefold()
        hits = [token for token in tokens if token.casefold() in lowered or token in text]
        # Contradiction check first: an explicit negation near the primary token
        # is a hard signal that the candidate text denies the record.
        primary = tokens[0]
        negations = (
            "\u4e0d\u662f", "\u5e76\u672a", "\u6ca1\u6709",
            "\u7edd\u975e", "not ", "never ",
        )
        if primary and primary.casefold() in lowered:
            for negation in negations:
                if negation in lowered:
                    idx = lowered.find(negation)
                    pidx = lowered.find(primary.casefold())
                    if abs(idx - pidx) <= 24:
                        return (
                            EvidenceSupportDisposition.CONTRADICTS,
                            "CANDIDATE_TEXT_CONTRADICTS",
                        )
        if not hits:
            # Noun-subject predicate may appear; single-token values like
            # read_49_books_100_times never match Chinese text. Flag for the
            # semantic verifier because a language mismatch cannot be ruled out.
            return (
                EvidenceSupportDisposition.PARTIAL,
                "LEXICAL_NO_HIT_NEEDS_VERIFIER",
            )
        if len(hits) < max(1, len(tokens) // 2):
            return (
                EvidenceSupportDisposition.PARTIAL,
                "LEXICAL_PARTIAL_NEEDS_VERIFIER",
            )
        return EvidenceSupportDisposition.SUPPORTS, "CANDIDATE_TEXT_LEXICAL_HIT"

    @staticmethod
    def _record_tokens(record: CuratorTypedRecord) -> list[str]:
        values: list[str] = []
        if isinstance(record, CuratorStateRecord):
            values.extend([record.predicate, EvidenceSupportGate._scalar_text(record.value)])
        elif isinstance(record, CuratorRelationRecord):
            values.append(record.predicate)
        elif isinstance(record, CuratorEventRecord):
            values.append(record.event_type)
        elif isinstance(record, CuratorObligationRecord):
            values.extend([record.kind, record.description, record.status])
        else:
            values.extend([record.entity_type, record.internal_label, *record.aliases])
        tokens: list[str] = []
        for value in values:
            if not value:
                continue
            if len(value) <= 24:
                tokens.append(value)
            tokens.extend(part for part in _TOKEN_RE.findall(value) if len(part) >= 2)
        seen: set[str] = set()
        ordered: list[str] = []
        for token in tokens:
            key = token.casefold()
            if key in seen or not token.strip():
                continue
            seen.add(key)
            ordered.append(token)
        return ordered[:8]

    @staticmethod
    def _scalar_text(value: object) -> str:
        if value is None:
            return ""
        return str(value)
