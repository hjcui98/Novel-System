"""Independent inquiry and PlanProposal reviewer facade."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.author_constraints import AuthorConstraint
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId, bounded_stable_id
from novel_agent.domain.memory import ObligationKind, long_range_kind_requires_not_before
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.obligation_contract import (
    compile_legacy_obligation_plan,
    compile_obligation_actions,
)
from novel_agent.domain.planning import (
    PlannerContextPackage,
    PlanReview,
    PlanReviewDraft,
    PlanReviewIssue,
    ReviewDecision,
    ReviewIssueKind,
    ReviewTargetKind,
    missing_volume_structure_keys,
    volume_stage_grid_defects,
)
from novel_agent.domain.planning_coverage import (
    compile_planning_coverage_report,
)
from novel_agent.domain.retrieval_decision import (
    FIRST_CHAPTER_WAIVER_REF,
    HOST_ISSUED_WAIVER_REFS,
    HistoryRetrievalDecision,
    HistoryRetrievalRequirement,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    PlanUnresolvedIssue,
    ProjectProfileRootDocument,
    hard_unresolved_kinds,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id

_RESOLVE_MARKERS = {"resolved", "payoff", "resolve"}
_HISTORY_NEED_KINDS = frozenset(
    {
        "causal_history",
        "knowledge_origin",
        "relationship_origin",
        "setup_evidence",
        "object_origin",
    }
)


class PlanReviewerInvocationError(ValueError):
    pass


def apply_host_plan_review_constraints(
    draft: PlanReviewDraft,
    *,
    target_kind: ReviewTargetKind,
    target_payload: str,
    mode: AgentMode = AgentMode.ARC_VOLUME,
    expected_volume_count: int | None = None,
    expected_target_chapters: int | None = None,
    accepted_obligation_ids: frozenset[str] | None = None,
    author_constraints: Sequence[AuthorConstraint] = (),
    trusted_window: tuple[int, int] | None = None,
) -> PlanReviewDraft:
    """Overlay trusted temporal/parent-scope issues onto a model Plan review."""

    if target_kind is not ReviewTargetKind.PLAN_PROPOSAL:
        return draft
    try:
        payload = json.loads(target_payload)
    except json.JSONDecodeError:
        return draft
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return draft
    extra = (
        *_host_issues_for_items(
            raw_items,
            mode=mode,
            expected_volume_count=(
                expected_volume_count
                if expected_volume_count is not None
                else _expected_volume_count(payload)
            ),
            expected_target_chapters=(
                expected_target_chapters
                if expected_target_chapters is not None
                else _expected_target_chapters(payload)
            ),
            accepted_obligation_ids=accepted_obligation_ids,
        ),
        *_unresolved_host_issues(payload),
    )
    coverage = _coverage_evidence(
        payload,
        raw_items,
        mode=mode,
        constraints=author_constraints,
        trusted_window=trusted_window,
    )
    if not extra and not coverage:
        return draft
    issues = (*draft.issues, *extra)
    missing_window = any(
        issue.kind is ReviewIssueKind.LONG_RANGE_PAYOFF_WITHOUT_TIME_WINDOW for issue in extra
    )
    if missing_window:
        return draft.model_copy(
            update={
                "issues": issues,
                "coverage_evidence": coverage,
                "decision": ReviewDecision.HUMAN_REQUIRED,
                "revision_instruction": None,
            }
        )
    if not extra:
        return draft.model_copy(update={"coverage_evidence": coverage})
    instruction = (
        draft.revision_instruction
        or "Revise blocking unresolved conflicts, incomplete volume structure, "
        "future-locked payoff, and parent-scope violations; keep SETUP/PROGRESS only."
    )
    return draft.model_copy(
        update={
            "issues": issues,
            "coverage_evidence": coverage,
            "decision": ReviewDecision.REVISE,
            "revision_instruction": instruction,
        }
    )


def _coverage_evidence(
    payload: dict[str, Any],
    raw_items: list[object],
    *,
    mode: AgentMode,
    constraints: Sequence[AuthorConstraint] = (),
    trusted_window: tuple[int, int] | None = None,
) -> tuple[str, ...]:
    """Return one host-computed coverage line per coverage question.

    The denominators come from trusted inputs: the task horizon when the host
    supplies one, otherwise the candidate's own declared window, plus the frozen
    author-constraint catalogue.  A candidate therefore cannot raise its own
    score by declaring less.
    """

    items = [item for item in raw_items if isinstance(item, dict)]
    window = trusted_window or _proposal_chapter_window(items)
    if window is None:
        return ()
    start, end = window
    report = compile_planning_coverage_report(
        items=items,
        target_chapter_start=start,
        target_chapter_end=end,
        constraints=constraints,
        mode=mode.value,
    )
    lines: list[str] = []
    for ratio in report.ratios:
        if not ratio.applicable:
            lines.append(f"{ratio.kind.value}: not applicable at this planning level")
            continue
        missing = ", ".join(ratio.missing[:10]) or "none"
        lines.append(
            f"{ratio.kind.value}: {ratio.covered}/{ratio.total} "
            f"(denominator: {ratio.denominator_source}; missing: {missing})"
        )
    return tuple(lines)


def _host_issues_for_items(
    raw_items: list[object],
    *,
    mode: AgentMode,
    expected_volume_count: int | None = None,
    expected_target_chapters: int | None = None,
    accepted_obligation_ids: frozenset[str] | None = None,
) -> list[PlanReviewIssue]:
    issues: list[PlanReviewIssue] = []
    by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if isinstance(raw, dict) and isinstance(raw.get("item_id"), str):
            by_id[raw["item_id"]] = raw
    volume_items = 0
    volume_ranges: list[tuple[str, int | None, int | None]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        raw_item_id = raw.get("item_id")
        item_id = raw_item_id if isinstance(raw_item_id, str) else "item"
        item_payload = raw.get("payload")
        if not isinstance(item_payload, dict):
            item_payload = {}
        kind_raw = str(item_payload.get("obligation_kind") or raw.get("kind") or "")
        level_raw = str(item_payload.get("plan_level") or raw.get("kind") or "").lower()
        if level_raw in {"arc_volume", "volume", "volume_scope", "volume_arc", "arc"}:
            volume_items += 1
            if mode is AgentMode.ARC_VOLUME:
                volume_ranges.append(
                    (
                        item_id,
                        _optional_int(item_payload.get("chapter_start") or raw.get("chapter_start")),
                        _optional_int(item_payload.get("chapter_end") or raw.get("chapter_end")),
                    )
                )
                missing_slots = missing_volume_structure_keys(item_payload)
                if missing_slots:
                    issues.append(
                        _host_issue(
                            ReviewIssueKind.VOLUME_STRUCTURE_INCOMPLETE,
                            "VOLUME_STRUCTURE_INCOMPLETE: missing required volume slots: "
                            + ", ".join(missing_slots),
                            item_id,
                            blocking=True,
                        )
                    )
                for defect in volume_stage_grid_defects(item_payload):
                    issues.append(
                        _host_issue(
                            ReviewIssueKind.VOLUME_STRUCTURE_INCOMPLETE,
                            f"VOLUME_STAGE_UNUSABLE: {defect}",
                            item_id,
                            blocking=True,
                        )
                    )
        try:
            kind = ObligationKind(kind_raw)
        except ValueError:
            kind = None
        not_before = item_payload.get("not_before_chapter")
        not_before_chapter = not_before if isinstance(not_before, int) else None
        if (
            kind is not None
            and long_range_kind_requires_not_before(kind)
            and not_before_chapter is None
        ):
            issues.append(
                _host_issue(
                    ReviewIssueKind.LONG_RANGE_PAYOFF_WITHOUT_TIME_WINDOW,
                    "long-range PROMISE/FORESHADOWING requires not_before_chapter",
                    item_id,
                    blocking=True,
                )
            )
        if _item_resolves(raw, item_payload) and not_before_chapter is not None:
            chapter = item_payload.get("chapter_index")
            if not isinstance(chapter, int):
                chapter = item_payload.get("chapter")
            if not isinstance(chapter, int):
                chapter = item_payload.get("target_chapter_start")
            if isinstance(chapter, int) and chapter < not_before_chapter:
                issues.append(
                    _host_issue(
                        ReviewIssueKind.EARLY_RESOLUTION_OF_FUTURE_LOCKED_OBLIGATION,
                        "future-locked obligation cannot be resolved in this planning scope",
                        item_id,
                        blocking=True,
                    )
                )
        parent_id = item_payload.get("parent_id")
        if isinstance(parent_id, str) and parent_id in by_id:
            parent_payload = by_id[parent_id].get("payload")
            if not isinstance(parent_payload, dict):
                parent_payload = {}
            child_start = _optional_int(item_payload.get("chapter_start"))
            child_end = _optional_int(item_payload.get("chapter_end"))
            parent_start = _optional_int(parent_payload.get("chapter_start"))
            parent_end = _optional_int(parent_payload.get("chapter_end"))
            if (
                child_start is not None
                and child_end is not None
                and parent_start is not None
                and parent_end is not None
                and (child_start < parent_start or child_end > parent_end)
            ):
                issues.append(
                    _host_issue(
                        ReviewIssueKind.TARGET_WINDOW_OUTSIDE_PARENT_SCOPE,
                        "child plan scope exceeds parent scope",
                        item_id,
                        blocking=True,
                    )
                )
        if mode is AgentMode.CHAPTER_SET and _is_chapter_item(raw, item_payload):
            _append_history_need_issues(issues, item_payload, item_id)
        _append_obligation_contract_issues(
            issues,
            item_payload,
            item_id,
            mode=mode,
            item_kind=str(raw.get("kind") or "").lower(),
            accepted_obligation_ids=accepted_obligation_ids,
        )
    if (
        mode is AgentMode.ARC_VOLUME
        and expected_volume_count is not None
        and volume_items != expected_volume_count
    ):
        issues.append(
            _host_issue(
                ReviewIssueKind.COVERAGE,
                f"expected {expected_volume_count} arc volumes but received {volume_items}",
                "volume-coverage",
                blocking=True,
            )
        )
    if mode is AgentMode.ARC_VOLUME and volume_ranges:
        range_issue = _volume_range_issue(volume_ranges, expected_target_chapters)
        if range_issue is not None:
            issues.append(range_issue)
    return issues


def _volume_range_issue(
    ranges: list[tuple[str, int | None, int | None]],
    expected_target_chapters: int | None,
) -> PlanReviewIssue | None:
    incomplete = tuple(
        item_id for item_id, start, end in ranges if start is None or end is None or end < start
    )
    if incomplete:
        return _host_issue(
            ReviewIssueKind.VOLUME_STRUCTURE_INCOMPLETE,
            "VOLUME_STRUCTURE_INCOMPLETE: volume chapter_start/chapter_end missing or reversed",
            incomplete[0],
            blocking=True,
        )
    ordered = sorted(
        ((start, end, item_id) for item_id, start, end in ranges if start and end),
        key=lambda item: (item[0], item[1]),
    )
    expected_start = 1
    for start, end, item_id in ordered:
        if start != expected_start:
            return _host_issue(
                ReviewIssueKind.COVERAGE,
                "VOLUME_RANGE_GAP_OR_OVERLAP: volume ranges must be contiguous from "
                f"chapter 1; expected {expected_start}, got {start}",
                item_id,
                blocking=True,
            )
        expected_start = end + 1
    if expected_target_chapters is not None and expected_start - 1 != expected_target_chapters:
        return _host_issue(
            ReviewIssueKind.COVERAGE,
            "VOLUME_RANGE_COVERAGE: volumes must cover chapters 1.."
            f"{expected_target_chapters}, got 1..{expected_start - 1}",
            "volume-range-coverage",
            blocking=True,
        )
    return None


def _proposal_chapter_window(raw_items: object) -> tuple[int, int] | None:
    chapters: list[int] = []
    if isinstance(raw_items, list):
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            payload = raw.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            for key in (
                "chapter_index",
                "chapter",
                "target_chapter_start",
                "chapter_start",
                "target_chapter_end",
                "chapter_end",
            ):
                value = payload.get(key)
                if type(value) is int and value >= 1:
                    chapters.append(value)
    if not chapters:
        return None
    return min(chapters), max(chapters)


def _summary_chapter_window(summary: str) -> tuple[int, int] | None:
    """Return the chapter window a summary text is actually asking about."""

    numbers: list[int] = []
    for start, end in re.findall(r"(?<!\d)(\d{1,4})\s*-\s*(\d{1,4})(?!\d)", summary):
        numbers.extend((int(start), int(end)))
    for single in re.findall(r"第\s*(\d{1,4})\s*章", summary):
        numbers.append(int(single))
    if not numbers:
        return None
    return min(numbers), max(numbers)


def _unresolved_host_issues(payload: dict[str, Any]) -> list[PlanReviewIssue]:
    """Block accepted proposals that still carry unresolved hard conflicts.

    A non-blocking advisory also has to state the chapters it actually questions.
    An issue that names a bounded window in its text while leaving
    ``affected_chapters`` empty cannot be checked at the affected chapter, so it
    must not be accepted as a plan-wide advisory.
    """

    raw_issues = payload.get("unresolved")
    if not isinstance(raw_issues, list) or not raw_issues:
        return []
    window = _proposal_chapter_window(payload.get("items"))
    issues: list[PlanReviewIssue] = []
    hard_kinds = hard_unresolved_kinds()
    for index, raw in enumerate(raw_issues):
        if not isinstance(raw, dict):
            continue
        try:
            issue = PlanUnresolvedIssue.model_validate(raw, strict=False)
        except ValueError as error:
            issues.append(
                _host_issue(
                    ReviewIssueKind.BLOCKING_UNRESOLVED,
                    f"PLAN_UNRESOLVED_INVALID: {error}",
                    f"unresolved.{index}",
                    blocking=True,
                )
            )
            continue
        affects_window = window is not None and issue.affects_chapters(*window)
        if issue.blocking or (issue.kind in hard_kinds and (affects_window or window is None)):
            issues.append(
                _host_issue(
                    ReviewIssueKind.BLOCKING_UNRESOLVED,
                    f"BLOCKING_UNRESOLVED[{issue.kind.value}]: {issue.summary}",
                    issue.issue_id.root,
                    blocking=True,
                )
            )
            continue
        if issue.affected_chapters:
            continue
        questioned = _summary_chapter_window(issue.summary)
        if questioned is None:
            continue
        issues.append(
            _host_issue(
                ReviewIssueKind.UNRESOLVED_SCOPE_MISSING,
                "UNRESOLVED_SCOPE_MISSING: this advisory questions chapters "
                f"{questioned[0]}-{questioned[1]} but declares no affected_chapters, so the "
                "uncertainty cannot be checked at the affected chapter",
                issue.issue_id.root,
                blocking=True,
            )
        )
    return issues


def _is_chapter_item(raw: dict[str, Any], payload: dict[str, Any]) -> bool:
    level = str(payload.get("plan_level") or raw.get("plan_level") or "").lower()
    kind = str(raw.get("kind") or "").lower()
    return (
        level in {"chapter", "chapter_goal"}
        or "chapter" in kind
        or isinstance(payload.get("chapter_index"), int)
        or isinstance(payload.get("chapter"), int)
    )


def _is_known_obligation_kind(value: object) -> bool:
    try:
        ObligationKind(str(value))
    except ValueError:
        return False
    return True


def _claims_direct_obligation(payload: dict[str, Any], item_kind: str) -> bool:
    """Mirror the materializer's direct-declaration surface.

    The materializer reads a direct declaration from ``obligation_kind`` /
    ``obligation_type``, from a nested ``obligation`` object, or from an item whose
    own kind is an obligation kind.  A legacy ``obligation_plan`` table and an
    ``obligation_declarations`` list are separate surfaces with their own checks,
    so they must not be treated as direct declarations here.
    """

    # "obligation" itself is not an ObligationKind value but the materializer
    # treats it as a declaration surface, which is exactly how the live STORY
    # item slipped through.
    if item_kind == "obligation" or item_kind in {kind.value for kind in ObligationKind}:
        return True
    return payload.get("obligation") is not None or payload.get("obligations") is not None


def _append_obligation_contract_issues(
    issues: list[PlanReviewIssue],
    payload: dict[str, Any],
    item_id: str,
    *,
    mode: AgentMode,
    item_kind: str = "",
    accepted_obligation_ids: frozenset[str] | None = None,
) -> None:
    """Surface unreadable obligation shapes before the candidate is accepted.

    Host review is the boundary that must catch a legacy free-text chapter action,
    an unreadable responsibility table, a lower-level plan that creates a durable
    obligation, or an action that points at an obligation nobody declared.  The
    materializer keeps the same checks as the last line of defence, but a
    candidate must not reach acceptance with any of these defects.
    """

    actions = payload.get("obligation_actions")
    if actions is not None:
        compilation = compile_obligation_actions(actions)
        for discrepancy in compilation.discrepancies:
            issues.append(
                _host_issue(
                    ReviewIssueKind.OBLIGATION_CONTRACT,
                    f"OBLIGATION_ACTION_UNREADABLE: {discrepancy}",
                    item_id,
                    blocking=True,
                )
            )
        if accepted_obligation_ids is not None:
            for action in compilation.actions:
                if action.obligation_id in accepted_obligation_ids:
                    continue
                issues.append(
                    _host_issue(
                        ReviewIssueKind.OBLIGATION_CONTRACT,
                        "OBLIGATION_ACTION_UNDECLARED: "
                        f"{action.obligation_id} is not a declared obligation; this level "
                        "may reference accepted obligation ids but may not invent one",
                        item_id,
                        blocking=True,
                    )
                )
    # A direct declaration has to name a readable obligation kind.  The
    # materializer rejects an unknown kind, so accepting it here would produce
    # exactly the "accepted by review, refused at commit" failure the remediation
    # set out to close.  Reproduced live: a STORY item with kind="obligation" and
    # no obligation_kind was ACCEPTed and then blocked the commit.
    direct_kind = payload.get("obligation_kind") or payload.get("obligation_type")
    if direct_kind is None and _claims_direct_obligation(payload, item_kind):
        issues.append(
            _host_issue(
                ReviewIssueKind.OBLIGATION_CONTRACT,
                "OBLIGATION_KIND_MISSING: an item that declares an obligation must name an "
                "obligation_kind of "
                + ", ".join(kind.value for kind in ObligationKind),
                item_id,
                blocking=True,
            )
        )
    elif direct_kind is not None and not _is_known_obligation_kind(direct_kind):
        issues.append(
            _host_issue(
                ReviewIssueKind.OBLIGATION_CONTRACT,
                f"OBLIGATION_KIND_UNKNOWN: {direct_kind!r} is not one of "
                + ", ".join(kind.value for kind in ObligationKind),
                item_id,
                blocking=True,
            )
        )
    declarations = payload.get("obligation_declarations")
    if declarations is not None and (
        not isinstance(declarations, (list, tuple))
        or not all(isinstance(entry, dict) for entry in declarations)
    ):
        issues.append(
            _host_issue(
                ReviewIssueKind.OBLIGATION_CONTRACT,
                "OBLIGATION_DECLARATION_UNREADABLE: obligation_declarations must be "
                "a list of declaration objects",
                item_id,
                blocking=True,
            )
        )
    elif declarations and mode in {
        AgentMode.CHAPTER_SET,
        AgentMode.CHAPTER,
        AgentMode.SCENE,
    }:
        # The same level rule the materializer enforces: a lower-level plan may
        # project or reference accepted obligations, never create one.  Without
        # this the candidate was accepted here and only rejected later.
        issues.append(
            _host_issue(
                ReviewIssueKind.OBLIGATION_CONTRACT,
                "OBLIGATION_DECLARATION_FORBIDDEN: this planning level may reference "
                "accepted obligation ids but may not declare a durable obligation",
                item_id,
                blocking=True,
            )
        )
    if payload.get("obligation_plan") is None:
        return
    legacy = compile_legacy_obligation_plan(payload["obligation_plan"])
    if mode in {AgentMode.CHAPTER_SET, AgentMode.CHAPTER, AgentMode.SCENE}:
        # Lower planning levels reference accepted obligations; a durable
        # responsibility may only be created by the upper-level plan.
        issues.append(
            _host_issue(
                ReviewIssueKind.OBLIGATION_CONTRACT,
                "OBLIGATION_PLAN_FORBIDDEN: this planning level may reference accepted "
                "obligation ids but may not declare a durable responsibility table",
                item_id,
                blocking=True,
            )
        )
    for discrepancy in legacy.discrepancies:
        issues.append(
            _host_issue(
                ReviewIssueKind.OBLIGATION_CONTRACT,
                f"OBLIGATION_PLAN_UNREADABLE: {discrepancy}",
                item_id,
                blocking=True,
            )
        )


def _append_history_waiver_issues(
    issues: list[PlanReviewIssue],
    decision: HistoryRetrievalDecision,
    chapter: int | None,
    item_id: str,
) -> None:
    """Reject a waiver reference the host never issued.

    The first-chapter waiver is host-owned and only legitimate for chapter 1.  A
    model may propose a reason, but it cannot approve itself by writing an arbitrary
    waiver string; any other NOT_REQUIRED waiver needs a real approval receipt.
    """

    if decision.requirement is not HistoryRetrievalRequirement.NOT_REQUIRED:
        return
    if decision.waiver_ref == FIRST_CHAPTER_WAIVER_REF:
        if chapter != 1:
            issues.append(
                _host_issue(
                    ReviewIssueKind.COVERAGE,
                    "HISTORY_WAIVER_INAPPLICABLE: the host first-chapter history waiver "
                    f"only applies to chapter 1, not chapter {chapter}",
                    item_id,
                    blocking=True,
                )
            )
        return
    if decision.waiver_ref not in HOST_ISSUED_WAIVER_REFS:
        issues.append(
            _host_issue(
                ReviewIssueKind.COVERAGE,
                "HISTORY_WAIVER_UNVERIFIED: waiver_ref "
                f"{decision.waiver_ref!r} was not issued by the host; NOT_REQUIRED needs a "
                "host-generated waiver or a real approval receipt",
                item_id,
                blocking=True,
            )
        )


def _append_history_need_issues(
    issues: list[PlanReviewIssue], payload: dict[str, Any], item_id: str
) -> None:
    chapter = payload.get("chapter_index")
    if not isinstance(chapter, int):
        chapter = payload.get("chapter")
    raw_decision = payload.get("history_retrieval")
    declared = payload.get("history_needs")
    if raw_decision is None and (declared is None or declared == []):
        # A bare empty list never proves the question was decided.  Only the
        # first chapter may omit the explicit decision.
        if chapter != 1:
            issues.append(
                _host_issue(
                    ReviewIssueKind.COVERAGE,
                    "HISTORY_DECISION_MISSING: chapters after chapter 1 require an "
                    "explicit history_retrieval decision",
                    item_id,
                    blocking=True,
                )
            )
        return
    if raw_decision is not None:
        if not isinstance(raw_decision, dict):
            issues.append(
                _host_issue(
                    ReviewIssueKind.COVERAGE,
                    "HISTORY_RETRIEVAL_INVALID: history_retrieval must be an object",
                    item_id,
                    blocking=True,
                )
            )
            return
        try:
            decision = HistoryRetrievalDecision.model_validate(raw_decision, strict=False)
        except ValueError as error:
            issues.append(
                _host_issue(
                    ReviewIssueKind.COVERAGE,
                    f"HISTORY_RETRIEVAL_INVALID: {error}",
                    item_id,
                    blocking=True,
                )
            )
            return
        _append_history_waiver_issues(issues, decision, chapter, item_id)
    if declared is None:
        return
    if not isinstance(declared, list):
        issues.append(
            _host_issue(
                ReviewIssueKind.COVERAGE,
                "HISTORY_NEEDS_INVALID: history_needs must be a list",
                item_id,
                blocking=True,
            )
        )
        return
    if len(declared) > 3:
        issues.append(
            _host_issue(
                ReviewIssueKind.COVERAGE,
                f"HISTORY_NEEDS_LIMIT: expected at most 3, received {len(declared)}",
                item_id,
                blocking=True,
            )
        )
    seen: set[tuple[str, str]] = set()
    for index, need in enumerate(declared):
        if not isinstance(need, dict):
            issues.append(
                _host_issue(
                    ReviewIssueKind.COVERAGE,
                    f"HISTORY_NEEDS_INVALID: item {index} must be an object",
                    item_id,
                    blocking=True,
                )
            )
            continue
        kind = need.get("kind")
        query = need.get("query")
        if not isinstance(kind, str) or kind not in _HISTORY_NEED_KINDS:
            issues.append(
                _host_issue(
                    ReviewIssueKind.COVERAGE,
                    f"HISTORY_NEEDS_INVALID: item {index} has unsupported kind",
                    item_id,
                    blocking=True,
                )
            )
        if not isinstance(query, str) or not query.strip():
            issues.append(
                _host_issue(
                    ReviewIssueKind.COVERAGE,
                    f"HISTORY_NEEDS_INVALID: item {index} requires a non-empty query",
                    item_id,
                    blocking=True,
                )
            )
        if isinstance(kind, str) and isinstance(query, str) and query.strip():
            key = (kind, query.strip())
            if key in seen:
                issues.append(
                    _host_issue(
                        ReviewIssueKind.COVERAGE,
                        "HISTORY_NEEDS_INVALID: duplicate (kind, query)",
                        item_id,
                        blocking=True,
                    )
                )
            seen.add(key)


def _expected_target_chapters(payload: dict[str, Any]) -> int | None:
    candidates: list[object] = [payload.get("target_chapters")]
    for raw in payload.get("items", ()):
        if isinstance(raw, dict) and isinstance(raw.get("payload"), dict):
            candidates.append(raw["payload"].get("target_chapters"))
    for candidate in candidates:
        if type(candidate) is int and candidate > 0:
            return candidate
    return None


def _expected_volume_count(payload: dict[str, Any]) -> int | None:
    candidates: list[object] = [
        payload.get("expected_volume_count"),
        payload.get("volume_count"),
    ]
    for raw in payload.get("items", ()):
        if isinstance(raw, dict) and isinstance(raw.get("payload"), dict):
            candidates.extend(
                (
                    raw["payload"].get("expected_volume_count"),
                    raw["payload"].get("volume_count"),
                )
            )
    for candidate in candidates:
        if type(candidate) is int and candidate > 0:
            return candidate
    return None


def _item_resolves(raw: dict[str, Any], payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("obligation_status") or "").lower()
    operation = str(payload.get("operation") or "").lower()
    kind = str(raw.get("kind") or "").lower()
    return status in _RESOLVE_MARKERS or operation in _RESOLVE_MARKERS or kind in _RESOLVE_MARKERS


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _host_issue(
    kind: ReviewIssueKind,
    summary: str,
    item_id: str,
    *,
    blocking: bool,
) -> PlanReviewIssue:
    return PlanReviewIssue(
        issue_id=bounded_stable_id(f"issue.{kind.value}.{item_id}", f"issue.{kind.value}"),
        kind=kind,
        summary=summary,
        blocking=blocking,
        affected_item_ids=(StableId(item_id),) if _is_stable_id(item_id) else (),
    )


def _is_stable_id(value: str) -> bool:
    try:
        StableId(value)
    except ValueError:
        return False
    return True


class PlanReviewerAgent:
    def __init__(self, runner: StructuredAgentRunner, artifacts: ArtifactRepository) -> None:
        self._runner = runner
        self._artifacts = artifacts

    async def review(
        self,
        *,
        version: SchemaVersion,
        mode: AgentMode,
        target_kind: ReviewTargetKind,
        target_payload: str,
        target_artifact: ArtifactRef,
        trusted_source_artifacts: tuple[ArtifactRef, ...],
        request: ModelRequest,
        base_commit: CommitId | None,
    ) -> tuple[PlanReview, ArtifactRef, ModelCallRecord]:
        inputs = (*trusted_source_artifacts, target_artifact)
        review_context = self._review_context_data(trusted_source_artifacts)
        context_block = (
            '<REVIEW_CONTEXT_DATA instruction_authority="none">\n'
            f"{review_context or '(no additional context data)'}\n"
            "</REVIEW_CONTEXT_DATA>"
        )
        prepared = self._runner.prepare(
            AgentType.PLAN_REVIEWER,
            mode,
            version.root,
            request,
            (
                f"REVIEW_TARGET_KIND={target_kind.value}\n"
                f"{context_block}\n"
                f"<REVIEW_TARGET_DATA>\n{target_payload}\n</REVIEW_TARGET_DATA>\n"
                "PLANNER_HIDDEN_REASONING=not_supplied"
            ),
            source_hashes=tuple(item.artifact_id for item in trusted_source_artifacts),
            input_artifacts=inputs,
            base_commit=base_commit,
        )
        execution = await self._runner.execute(prepared, PlanReviewDraft)
        draft = execution.output
        context_package = _planner_context_package(self._artifacts, trusted_source_artifacts)
        if draft.target_kind is not target_kind:
            raise PlanReviewerInvocationError("Reviewer changed the trusted target kind")
        draft = apply_host_plan_review_constraints(
            draft,
            mode=mode,
            target_kind=target_kind,
            target_payload=target_payload,
            expected_volume_count=_expected_volume_count_from_context(review_context),
            expected_target_chapters=_expected_target_chapters_from_context(review_context),
            accepted_obligation_ids=_accepted_obligation_ids(
                self._artifacts, trusted_source_artifacts, context_package
            ),
            author_constraints=_author_constraint_catalogue(
                self._artifacts, trusted_source_artifacts, context_package
            ),
        )
        draft_artifact = self._artifacts.put(
            canonical_json_bytes(draft.model_dump(mode="json")),
            "application/vnd.novel-agent.plan-review-draft+json",
            version,
        )
        receipt = self._runner.receipt(
            prepared,
            execution.model_call,
            output_artifacts=(draft_artifact,),
            unresolved=tuple(issue.summary for issue in draft.issues if issue.blocking),
        )
        identity = content_id(
            {
                "target": target_artifact.artifact_id.root,
                "draft": draft.model_dump(mode="json"),
                "receipt": receipt.receipt_id.root,
            }
        ).root.removeprefix("sha256:")[:24]
        review = PlanReview(
            review_id=StableId(f"plan-review.{identity}"),
            target_kind=draft.target_kind,
            target_artifact_ref=target_artifact,
            decision=draft.decision,
            issues=draft.issues,
            preserve_item_ids=draft.preserve_item_ids,
            revision_instruction=draft.revision_instruction,
            memory_gap_questions=draft.memory_gap_questions,
            receipt=receipt,
        )
        review_artifact = self._artifacts.put(
            canonical_json_bytes(review.model_dump(mode="json")),
            "application/vnd.novel-agent.plan-review+json",
            version,
        )
        return review, review_artifact, execution.model_call

    def _review_context_data(self, refs: tuple[ArtifactRef, ...]) -> str:
        rendered: list[str] = []
        source_text: list[str] = []
        for ref in refs:
            try:
                raw = self._artifacts.read_verified(ref)
                if ref.media_type == "application/vnd.novel-agent.planner-context-package+json":
                    package = PlannerContextPackage.model_validate_json(raw, strict=True)
                    rendered.append(package.rendered_context)
                elif ref.media_type.startswith("text/"):
                    source_text.append(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise PlanReviewerInvocationError("Reviewer context artifact is invalid") from error
        return "\n\n".join(
            dict.fromkeys(
                part
                for part in (*rendered, *source_text)
                if part.strip()
            )
        )


def _planner_context_package(
    artifacts: ArtifactRepository,
    refs: tuple[ArtifactRef, ...],
) -> PlannerContextPackage | None:
    """Read the trusted Planner context package the host assembled.

    The package is where the host publishes the references a review needs
    (``profile_ref`` and ``author_constraint_root_ref``).  A review that only
    looked at the artefact list therefore never found them and silently skipped
    both the obligation catalogue and the author-constraint denominator.
    """

    for ref in refs:
        if ref.media_type != "application/vnd.novel-agent.planner-context-package+json":
            continue
        try:
            return PlannerContextPackage.model_validate_json(
                artifacts.read_verified(ref), strict=True
            )
        except (UnicodeDecodeError, ValueError):
            return None
    return None


def _author_constraint_catalogue(
    artifacts: ArtifactRepository,
    refs: tuple[ArtifactRef, ...],
    context_package: PlannerContextPackage | None,
) -> tuple[AuthorConstraint, ...]:
    """Read the frozen author-constraint root the host compiled for this plan.

    Coverage must be measured against what the author declared, not against what
    the candidate chose to restate: with no catalogue the denominator collapsed to
    0/0 and a proposal that dropped every hard constraint scored perfectly.
    """

    from novel_agent.domain.author_constraints import AuthorConstraintRoot

    candidates: list[ArtifactRef] = []
    if context_package is not None and context_package.author_constraint_root_ref is not None:
        candidates.append(context_package.author_constraint_root_ref)
    candidates.extend(
        ref
        for ref in refs
        if ref.media_type == "application/vnd.novel-agent.author-constraint-root+json"
    )
    for ref in candidates:
        try:
            root = AuthorConstraintRoot.model_validate_json(
                artifacts.read_verified(ref), strict=True
            )
        except (UnicodeDecodeError, ValueError):
            continue
        return root.constraints
    return ()


def _declared_obligation_ids(payload: Mapping[str, object], item_id: str) -> set[str]:
    """Reproduce the host-derived ids a plan item's own declarations will bind.

    The identity convention lives in the materializer, so a review that wants to
    recognise the accepted catalogue has to derive the same ids: one per readable
    declaration ordinal and kind, in the order the materializer binds them.
    """

    from novel_agent.domain.ids import bounded_stable_id

    declared: list[tuple[int, str]] = []
    for key in ("obligations", "obligation_declarations", "key_obligations",
                "obligation_declaration"):
        values = payload.get(key)
        if isinstance(values, Mapping):
            values = [values]
        if not isinstance(values, (list, tuple)):
            continue
        for entry in values:
            if not isinstance(entry, Mapping):
                continue
            kind_raw = entry.get("obligation_kind") or entry.get("kind") or entry.get("type")
            declared.append((0, str(kind_raw).strip().lower()))
    nested = payload.get("obligation")
    if isinstance(nested, Mapping):
        kind_raw = nested.get("obligation_kind") or nested.get("kind") or nested.get("type")
        declared.append((0, str(kind_raw).strip().lower()))
    legacy = payload.get("obligation_plan")
    if legacy is not None:
        compilation = compile_legacy_obligation_plan(legacy)
        declared.extend(
            (declaration.source_ordinal, declaration.kind.value)
            for declaration in compilation.declarations
        )
    ids: set[str] = set()
    for ordinal, kind in enumerate(kind for _source, kind in declared):
        ids.add(
            bounded_stable_id(
                f"obligation.{item_id}.{ordinal}.{kind}",
                "obligation."
                + content_id(
                    {"plan_item_id": item_id, "ordinal": ordinal, "kind": kind}
                ).root.removeprefix("sha256:")[:48],
            ).root
        )
    return ids


def _accepted_obligation_ids(
    artifacts: ArtifactRepository,
    refs: tuple[ArtifactRef, ...],
    context_package: PlannerContextPackage | None,
) -> frozenset[str] | None:
    """Read the obligation catalogue a lower-level plan must reference.

    A chapter may point at an obligation the upper-level plan already declared; it
    may not invent one.  The catalogue comes from the trusted World root when the
    host passed one, and otherwise from the declarations the trusted project
    profile carries.  ``None`` means no trusted catalogue could be read, which the
    review reports instead of silently skipping the check.
    """

    from novel_agent.domain.memory import WorldRootDocument

    for ref in refs:
        if ref.media_type != "application/vnd.novel-agent.world-root+json":
            continue
        try:
            world = WorldRootDocument.model_validate_json(
                artifacts.read_verified(ref), strict=True
            )
        except (UnicodeDecodeError, ValueError):
            continue
        return frozenset(item.obligation_id.root for item in world.obligations)
    profile_ref = None if context_package is None else context_package.profile_ref
    if profile_ref is None:
        return None
    try:
        profile = ProjectProfileRootDocument.model_validate_json(
            artifacts.read_verified(profile_ref), strict=True
        )
    except (UnicodeDecodeError, ValueError):
        return None
    catalogue: set[str] = set()
    raw_declarations = profile.capability_profile.get("obligation_declarations")
    if isinstance(raw_declarations, (list, tuple)):
        for item in raw_declarations:
            if not isinstance(item, Mapping):
                continue
            item_id = item.get("item_id") or item.get("plan_item_id")
            payload = item.get("payload")
            if isinstance(item_id, str) and isinstance(payload, Mapping):
                catalogue |= _declared_obligation_ids(payload, item_id)
    return frozenset(catalogue)


def _expected_target_chapters_from_context(context: str) -> int | None:
    """Read the trusted project chapter target used by host range coverage checks."""

    match = re.search(r'"target_chapters"\s*:\s*(\d+)', context)
    if match is None:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def _expected_volume_count_from_context(context: str) -> int | None:
    """Read only the trusted Profile/constraint value used by host coverage checks."""

    for key in ("expected_volume_count", "volume_count"):
        match = re.search(rf'"{re.escape(key)}"\s*:\s*(\d+)', context)
        if match is not None:
            value = int(match.group(1))
            if value > 0:
                return value
    return None
