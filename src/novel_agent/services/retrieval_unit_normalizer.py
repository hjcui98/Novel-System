"""Deterministic identity, evidence, and freshness normalization."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import Field

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.canonical import CanonicalAliasReceipt
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import RetrievalUnit, RetrievalUnitKind
from novel_agent.services.canonical_alias_registry import CanonicalAliasRegistry


@dataclass(frozen=True)
class _CanonicalMerge:
    canonical_value_id: StableId
    canonicalizer_version: str
    receipts: tuple[CanonicalAliasReceipt, ...]
    receipt_refs: tuple[ArtifactRef, ...]


class NormalizedRetrievalSet(DomainModel):
    units: tuple[RetrievalUnit, ...]
    superseded_unit_ids: tuple[StableId, ...] = ()
    duplicate_unit_ids: tuple[StableId, ...] = ()
    conflicts: tuple[str, ...] = ()
    canonical_alias_receipts: tuple[CanonicalAliasReceipt, ...] = ()
    input_unit_count: int = Field(ge=0)


class RetrievalUnitNormalizer:
    version = "retrieval_unit_normalizer.v4"

    def __init__(self, alias_registry: CanonicalAliasRegistry | None = None) -> None:
        self._alias_registry = alias_registry or CanonicalAliasRegistry()

    def normalize(self, units: tuple[RetrievalUnit, ...]) -> NormalizedRetrievalSet:
        by_unit_id: dict[StableId, RetrievalUnit] = {}
        input_position: dict[StableId, int] = {}
        duplicates: list[StableId] = []
        for index, unit in enumerate(units):
            prior = by_unit_id.get(unit.unit_id)
            if prior is None:
                by_unit_id[unit.unit_id] = unit
                input_position[unit.unit_id] = index
            elif prior == unit:
                duplicates.append(unit.unit_id)
            else:
                raise ValueError(f"retrieval unit id has conflicting payload: {unit.unit_id.root}")

        grouped: dict[tuple[object, ...], list[RetrievalUnit]] = {}
        for unit in by_unit_id.values():
            grouped.setdefault(self._canonical_identity(unit), []).append(unit)

        retained: list[RetrievalUnit] = []
        superseded: list[StableId] = []
        conflicts: list[str] = []
        alias_receipts: list[CanonicalAliasReceipt] = []
        for identity, group in grouped.items():
            ordered = sorted(
                group,
                key=lambda item: (*self._freshness_position(item), item.unit_id.root),
                reverse=True,
            )
            current = ordered[0]
            tied = [
                item
                for item in ordered
                if self._freshness_position(item) == self._freshness_position(current)
            ]
            if len({item.text for item in tied}) > 1:
                canonical_merge = self._canonical_merge(tied)
                if canonical_merge is not None:
                    winner = min(tied, key=lambda item: (len(item.text), item.unit_id.root))
                    retained.append(self._merge_alias_group(winner, tied, canonical_merge))
                    alias_receipts.extend(canonical_merge.receipts)
                    superseded.extend(item.unit_id for item in ordered if item != winner)
                else:
                    conflicts.append(
                        "conflicting current records for "
                        + "|".join(str(value) for value in identity)
                    )
                    # Do not silently pick a winner. All tied records remain audit-visible.
                    retained.extend(sorted(tied, key=lambda item: item.unit_id.root))
                    superseded.extend(item.unit_id for item in ordered[len(tied) :])
            else:
                retained.append(self._dedupe_evidence(current))
                superseded.extend(item.unit_id for item in ordered[1:])

        retained.sort(
            key=lambda item: (
                self._section_order(item),
                input_position[item.unit_id],
                item.unit_id.root,
            )
        )
        return NormalizedRetrievalSet(
            units=tuple(retained),
            superseded_unit_ids=tuple(dict.fromkeys(superseded)),
            duplicate_unit_ids=tuple(dict.fromkeys(duplicates)),
            conflicts=tuple(conflicts),
            canonical_alias_receipts=tuple(
                {item.receipt_id: item for item in alias_receipts}.values()
            ),
            input_unit_count=len(units),
        )

    @staticmethod
    def _canonical_identity(unit: RetrievalUnit) -> tuple[object, ...]:
        if unit.unit_kind in {
            RetrievalUnitKind.STATE_ANCHOR,
            RetrievalUnitKind.RELATION_ANCHOR,
            RetrievalUnitKind.FACT_ANCHOR,
        }:
            predicate = unit.predicate or unit.unit_id.root
            # Result records describe repeatable events (for example, separate
            # entrance-exam outcomes) rather than one mutually exclusive
            # current-state slot. Preserve each result identity independently.
            if unit.unit_kind is RetrievalUnitKind.STATE_ANCHOR and re.search(
                r"(?:^|_)result(?:$|_)", predicate.casefold()
            ):
                return (
                    unit.unit_kind.value,
                    tuple(sorted(item.root for item in unit.entity_ids)),
                    predicate,
                    unit.unit_id.root,
                )
            return (
                unit.unit_kind.value,
                tuple(sorted(item.root for item in unit.entity_ids)),
                predicate,
            )
        if unit.content_hash is not None:
            return ("content", unit.content_hash.root)
        return ("unit", unit.unit_id.root)

    @staticmethod
    def _freshness_position(unit: RetrievalUnit) -> tuple[int, int]:
        evidence_chapter = max(
            (
                chapter
                for item in unit.evidence_refs
                if item.chapter_id is not None
                and (chapter := RetrievalUnitNormalizer._chapter_number(item.chapter_id.root))
                is not None
            ),
            default=-1,
        )
        return (
            unit.narrative_end if unit.narrative_end is not None else evidence_chapter,
            unit.story_time_end if unit.story_time_end is not None else -1,
        )

    @staticmethod
    def _chapter_number(chapter_id: str) -> int | None:
        if chapter_id.endswith(".prelude") or chapter_id.startswith("prelude."):
            return 0
        match = re.search(r"(?:^|[._:-])(\d+)$", chapter_id)
        return int(match.group(1)) if match is not None else None

    @classmethod
    def _are_semantic_aliases(cls, units: list[RetrievalUnit]) -> bool:
        """Compatibility probe backed only by canonical identity or the trusted registry."""

        return cls()._canonical_merge(units) is not None

    def _canonical_merge(self, units: list[RetrievalUnit]) -> _CanonicalMerge | None:
        if (
            len(units) < 2
            or any(unit.predicate is None for unit in units)
            or any(not unit.evidence_refs for unit in units)
        ):
            return None
        predicates = {unit.predicate for unit in units}
        if len(predicates) != 1:
            return None
        predicate = next(iter(predicates))
        assert predicate is not None
        parsed = [self._state_value_and_tail(unit) for unit in units]
        if any(item is None for item in parsed):
            return None
        raw_values = [item[0] for item in parsed if item is not None]
        explicit = [
            (unit.canonical_value_id, unit.canonicalizer_version)
            for unit in units
            if unit.canonical_value_id is not None
        ]
        if len(explicit) == len(units) and len(set(explicit)) == 1:
            canonical_value_id, canonicalizer_version = explicit[0]
            assert canonical_value_id is not None and canonicalizer_version is not None
            return _CanonicalMerge(canonical_value_id, canonicalizer_version, (), ())

        resolutions = [self._alias_registry.resolve(predicate, value) for value in raw_values]
        if any(
            unit.canonical_value_id is not None
            and (
                unit.canonical_value_id != resolution.canonical_value_id
                or unit.canonicalizer_version != resolution.canonicalizer_version
            )
            for unit, resolution in zip(units, resolutions, strict=True)
        ):
            return None
        identities = {(item.canonical_value_id, item.canonicalizer_version) for item in resolutions}
        if len(identities) != 1:
            return None
        receipts: list[CanonicalAliasReceipt] = []
        receipt_refs: list[ArtifactRef] = []
        first = raw_values[0]
        for other in raw_values[1:]:
            resolution = self._alias_registry.equivalent(predicate, first, other)
            if resolution is None:
                return None
            if resolution.receipt is not None and resolution.receipt_ref is not None:
                receipts.append(resolution.receipt)
                receipt_refs.append(resolution.receipt_ref)
        canonical_value_id, canonicalizer_version = next(iter(identities))
        return _CanonicalMerge(
            canonical_value_id,
            canonicalizer_version,
            tuple(receipts),
            tuple(receipt_refs),
        )

    @staticmethod
    def _evidence_identity(unit: RetrievalUnit) -> frozenset[tuple[str, str, int, int]]:
        return frozenset(
            (
                item.evidence_id.root,
                item.span.block_id.root if item.span is not None else "",
                item.span.start if item.span is not None else -1,
                item.span.end if item.span is not None else -1,
            )
            for item in unit.evidence_refs
        )

    @staticmethod
    def _state_value_and_tail(unit: RetrievalUnit) -> tuple[str, str] | None:
        if not unit.predicate:
            return None
        match = re.search(
            rf"\s{re.escape(unit.predicate)}\s+\"([^\"]+)\"(.*)$",
            unit.text,
            flags=re.DOTALL,
        )
        if match is None:
            return None
        return match.group(1), " ".join(match.group(2).split())

    @staticmethod
    def _dedupe_evidence(unit: RetrievalUnit) -> RetrievalUnit:
        evidence = tuple(
            {
                (
                    item.object_hash.root,
                    item.span.block_id.root if item.span else "",
                    item.span.start if item.span else -1,
                    item.span.end if item.span else -1,
                ): item
                for item in unit.evidence_refs
            }.values()
        )
        return unit.model_copy(update={"evidence_refs": evidence})

    @classmethod
    def _merge_alias_group(
        cls,
        winner: RetrievalUnit,
        units: list[RetrievalUnit],
        merge: _CanonicalMerge,
    ) -> RetrievalUnit:
        merged = winner.model_copy(
            update={"evidence_refs": tuple(ref for unit in units for ref in unit.evidence_refs)}
        )
        deduplicated = cls._dedupe_evidence(merged)
        return deduplicated.model_copy(
            update={
                "canonical_value_id": merge.canonical_value_id,
                "canonicalizer_version": merge.canonicalizer_version,
                "canonical_alias_receipt_ref": (
                    merge.receipt_refs[0] if merge.receipt_refs else None
                ),
            }
        )

    @staticmethod
    def _section_order(unit: RetrievalUnit) -> int:
        order = {
            RetrievalUnitKind.FACT_ANCHOR: 0,
            RetrievalUnitKind.STATE_ANCHOR: 1,
            RetrievalUnitKind.RELATION_ANCHOR: 2,
            RetrievalUnitKind.EVENT_ANCHOR: 3,
            RetrievalUnitKind.PLAN_ANCHOR: 4,
        }
        return order.get(unit.unit_kind, 5)
