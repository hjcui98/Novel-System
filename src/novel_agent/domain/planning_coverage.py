"""Four-way planning coverage, reported as separate traceable ratios.

A single ``coverage`` float cannot say *what* was covered.  A planner proposal now
reports chapter coverage, author hard-constraint coverage, durable responsibility
coverage and history-decision coverage separately, each with the trusted denominator
it was measured against and the concrete items that are missing.

Every ratio is ``covered / total`` over a denominator that comes from a trusted input
(the declared horizon, the compiled author-constraint root, the declared
responsibility table, the chapter goals), so a smaller proposal cannot raise its own
score by simply declaring less.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from novel_agent.domain.author_constraints import AuthorConstraint
from novel_agent.domain.obligation_contract import compile_legacy_obligation_plan


class PlanningCoverageKind(StrEnum):
    """The four independent coverage questions a plan proposal must answer."""

    CHAPTER = "chapter_coverage"
    AUTHOR_CONSTRAINT = "author_constraint_coverage"
    OBLIGATION = "obligation_coverage"
    HISTORY_DECISION = "history_decision_coverage"


@dataclass(frozen=True, slots=True)
class PlanningCoverageRatio:
    """One coverage ratio with its trusted denominator and missing items."""

    kind: PlanningCoverageKind
    covered: int
    total: int
    missing: tuple[str, ...]
    denominator_source: str
    applicable: bool = True

    @property
    def complete(self) -> bool:
        return not self.applicable or self.total == 0 or not self.missing

    @property
    def ratio(self) -> float:
        return 1.0 if self.total == 0 else self.covered / self.total

    def as_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "applicable": self.applicable,
            "covered": self.covered,
            "total": self.total,
            "ratio": self.ratio,
            "denominator_source": self.denominator_source,
            "missing": list(self.missing),
        }


@dataclass(frozen=True, slots=True)
class PlanningCoverageReport:
    """All four coverage ratios for one proposal."""

    ratios: tuple[PlanningCoverageRatio, ...]

    @property
    def complete(self) -> bool:
        return all(ratio.complete for ratio in self.ratios)

    def ratio_for(self, kind: PlanningCoverageKind) -> PlanningCoverageRatio:
        for ratio in self.ratios:
            if ratio.kind is kind:
                return ratio
        raise KeyError(kind.value)

    def as_payload(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "ratios": [ratio.as_payload() for ratio in self.ratios],
        }


def _chapter_indexes(payload: Mapping[str, object]) -> int | None:
    raw = payload.get("chapter_index")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    return None


def _declared_responsibilities(
    items: Sequence[Mapping[str, object]],
) -> tuple[list[tuple[int, str]], list[str]]:
    """Return compiled responsibilities and every unreadable declaration.

    A declaration counts as coverage only when the host can compile it into a durable
    obligation.  An entry this module cannot read is reported by index instead of
    being dropped or silently counted as covered.
    """

    compiled: list[tuple[int, str]] = []
    unreadable: list[str] = []
    for item in items:
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if payload.get("obligation_plan") is not None:
            compilation = compile_legacy_obligation_plan(payload["obligation_plan"])
            compiled.extend(
                (declaration.source_ordinal, declaration.description)
                for declaration in compilation.declarations
            )
            unreadable.extend(compilation.discrepancies)
        raw_declarations = payload.get("obligation_declarations")
        if isinstance(raw_declarations, (list, tuple)):
            for index, entry in enumerate(raw_declarations):
                if not isinstance(entry, Mapping):
                    unreadable.append(f"obligation_declarations[{index}] is not an object")
                    continue
                description = next(
                    (
                        value.strip()
                        for key in ("summary", "description", "goal", "text")
                        for value in (entry.get(key),)
                        if isinstance(value, str) and value.strip()
                    ),
                    None,
                )
                kind = entry.get("kind") or entry.get("obligation_kind")
                if description is None or not isinstance(kind, str):
                    unreadable.append(
                        f"obligation_declarations[{index}] needs a kind and a description"
                    )
                    continue
                compiled.append((index, description))
    return compiled, unreadable


def _constraint_is_covered(constraint: AuthorConstraint, text: str) -> bool:
    """Report whether proposal text carries this constraint's own wording.

    Matching uses the constraint text itself (or its first clause) so a proposal is
    credited only when the constraint is actually represented, never by category.
    """

    body = constraint.text.strip()
    if not body:
        return False
    head = body.split("，")[0].split(",")[0].strip()  # noqa: RUF001 - author text
    return body in text or (len(head) >= 4 and head in text)


def compile_planning_coverage_report(
    *,
    items: Sequence[Mapping[str, object]],
    target_chapter_start: int,
    target_chapter_end: int,
    constraints: Sequence[AuthorConstraint] = (),
    mode: str | None = None,
) -> PlanningCoverageReport:
    """Measure the four coverage questions over one planning proposal.

    ``mode`` decides which questions are meaningful.  An ARC_VOLUME proposal owns
    ranges rather than individual chapters, so asking it for per-chapter coverage
    would report a real-looking zero that says nothing about the volume plan.
    """

    chapter_indexes: set[int] = set()
    for item in items:
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            continue
        index = _chapter_indexes(payload)
        if index is not None:
            chapter_indexes.add(index)
    level = (mode or "").lower()
    chapter_level = level in {"chapter_set", "chapter", "scene"}
    if not chapter_level and level:
        chapter_ratio = PlanningCoverageRatio(
            kind=PlanningCoverageKind.CHAPTER,
            covered=0,
            total=0,
            missing=(),
            denominator_source="not applicable to this planning level",
            applicable=False,
        )
    else:
        expected_chapters = tuple(range(target_chapter_start, target_chapter_end + 1))
        missing_chapters = tuple(
            f"chapter.{index}" for index in expected_chapters if index not in chapter_indexes
        )
        chapter_ratio = PlanningCoverageRatio(
            kind=PlanningCoverageKind.CHAPTER,
            covered=len(expected_chapters) - len(missing_chapters),
            total=len(expected_chapters),
            missing=missing_chapters,
            denominator_source="declared planning horizon",
        )

    proposal_text_parts: list[str] = []
    for item in items:
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            continue
        for key in ("summary", "goal", "title", "chapter_goal"):
            value = payload.get(key)
            if isinstance(value, str):
                proposal_text_parts.append(value)
    proposal_text = " ".join(proposal_text_parts)
    payload_texts = [
        str(item["payload"]) for item in items if isinstance(item.get("payload"), Mapping)
    ]
    combined = proposal_text + " " + " ".join(payload_texts)
    missing_constraints = tuple(
        constraint.constraint_id.root
        for constraint in constraints
        if not _constraint_is_covered(constraint, combined)
    )
    constraint_ratio = PlanningCoverageRatio(
        kind=PlanningCoverageKind.AUTHOR_CONSTRAINT,
        covered=len(constraints) - len(missing_constraints),
        total=len(constraints),
        missing=missing_constraints,
        denominator_source="compiled author constraint root",
    )

    declared, unreadable = _declared_responsibilities(items)
    total_declared = len(declared) + len(unreadable)
    obligation_ratio = PlanningCoverageRatio(
        kind=PlanningCoverageKind.OBLIGATION,
        covered=len(declared),
        total=total_declared,
        missing=tuple(unreadable),
        denominator_source="declared responsibility table",
    )

    if not chapter_indexes or not chapter_level:
        history_ratio = PlanningCoverageRatio(
            kind=PlanningCoverageKind.HISTORY_DECISION,
            covered=0,
            total=0,
            missing=(),
            denominator_source="not applicable to this planning level",
            applicable=chapter_level,
        )
    else:
        with_decision = set()
        for item in items:
            payload = item.get("payload")
            if not isinstance(payload, Mapping):
                continue
            index = _chapter_indexes(payload)
            if index is None:
                continue
            if isinstance(payload.get("history_retrieval"), Mapping):
                with_decision.add(index)
        missing_decisions = tuple(
            f"chapter.{index}" for index in sorted(chapter_indexes - with_decision)
        )
        history_ratio = PlanningCoverageRatio(
            kind=PlanningCoverageKind.HISTORY_DECISION,
            covered=len(chapter_indexes) - len(missing_decisions),
            total=len(chapter_indexes),
            missing=missing_decisions,
            denominator_source="chapter goals in this proposal",
        )

    return PlanningCoverageReport(
        ratios=(chapter_ratio, constraint_ratio, obligation_ratio, history_ratio)
    )
