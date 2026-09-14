"""Independent inquiry and PlanProposal reviewer facade."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Container, Mapping, Sequence
from typing import Any

from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.author_constraints import AuthorConstraint
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId, bounded_stable_id
from novel_agent.domain.memory import (
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    WorldRootDocument,
    long_range_kind_requires_not_before,
)
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.obligation_contract import (
    compile_legacy_obligation_plan,
    compile_obligation_actions,
    parse_obligation_declarations,
)
from novel_agent.domain.planning import (
    VOLUME_NARRATIVE_STAGE_KEYS,
    PlannerContextPackage,
    PlanReview,
    PlanReviewDraft,
    PlanReviewIssue,
    ReviewCitationFailure,
    ReviewDecision,
    ReviewIssueKind,
    ReviewTargetKind,
    missing_volume_structure_keys,
    normalize_stage_handle,
    volume_stage_grid_defects,
    volume_stage_window_defects,
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
    PlanUnresolvedOperationRecord,
    hard_unresolved_kinds,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id

_RESOLVE_MARKERS = {"resolved", "payoff", "resolve"}
# The structured field each host gate checks, so a revision can be told exactly what
# to declare instead of inferring it from the reviewer's prose.
_HOST_ISSUE_REQUIRED_FIELDS: dict[ReviewIssueKind, tuple[str, ...]] = {
    ReviewIssueKind.LONG_RANGE_PAYOFF_WITHOUT_TIME_WINDOW: ("not_before_chapter",),
    ReviewIssueKind.UNRESOLVED_SCOPE_MISSING: ("affected_chapters",),
    ReviewIssueKind.EARLY_RESOLUTION_OF_FUTURE_LOCKED_OBLIGATION: ("target_chapter_start",),
    ReviewIssueKind.OBLIGATION_CONTRACT: ("kind",),
    ReviewIssueKind.VOLUME_STAGE_WINDOW_VIOLATION: (),
}
_HISTORY_NEED_KINDS = frozenset(
    {
        "causal_history",
        "knowledge_origin",
        "relationship_origin",
        "setup_evidence",
        "object_origin",
    }
)
_ARC_VOLUME_COMPARISON_KEYS = (
    "midpoint_reversal",
    "volume_climax",
    "ending_state",
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
    accepted_obligation_windows: Mapping[str, tuple[int | None, int | None]] | None = None,
    author_constraints: Sequence[AuthorConstraint] = (),
    trusted_window: tuple[int, int] | None = None,
    verified_citations: bool = False,
) -> PlanReviewDraft:
    """Overlay trusted temporal/parent-scope issues onto a model Plan review.

    Every return path goes through the same verified issue set: a finding the host
    cannot ground in the candidate never drives a revision, whether or not the host
    happened to add a finding of its own.  Previously the no-extra branch returned
    the raw draft, so an unverified citation survived exactly when the host had
    nothing else to say.

    ``verified_citations`` marks a draft whose citations the host has already
    checked, so an already-verified review is not re-verified into a different
    decision when it is replayed.
    """

    # `verification_failures` is a host verdict, not a reviewer field: the schema
    # is generated from the draft model, so it is offered to the model, but the
    # prompt never asks for it and no model value may stand as the host's own
    # finding about the review.  A model that self-reports an unrelated note there
    # used to decide the run: for a non-proposal target every guard below returns
    # early, so the value survived and `raise PlanReviewerInvocationError` parked
    # the task as blocked/leaf_review_required off text the host never verified.
    # Clearing it first makes "the host has not verified this draft yet" the only
    # reachable pre-verification state.
    draft = draft.model_copy(update={"verification_failures": ()})

    if target_kind is not ReviewTargetKind.PLAN_PROPOSAL:
        return draft
    try:
        payload = json.loads(target_payload)
    except json.JSONDecodeError:
        return draft
    if not isinstance(payload, Mapping):
        return draft
    document: dict[str, Any] = dict(payload)
    raw_items = document.get("items")
    if not isinstance(raw_items, list):
        return draft
    items = _item_payloads(document)
    constraint_ids = (
        frozenset(
            handle
            for constraint in author_constraints
            for handle in (constraint.constraint_id.root, constraint.constraint_key)
            if handle
        )
        if author_constraints
        else None
    )
    if verified_citations:
        issues = tuple(draft.issues)
        citation_failures: tuple[str, ...] = ()
    else:
        issues, citation_failures = _verified_model_issues(
            draft.issues,
            target_payload,
            items=items,
            constraint_ids=constraint_ids,
        )
    extra = (
        *_host_issues_for_items(
            raw_items,
            mode=mode,
            expected_volume_count=(
                expected_volume_count
                if expected_volume_count is not None
                else _expected_volume_count(document)
            ),
            expected_target_chapters=(
                expected_target_chapters
                if expected_target_chapters is not None
                else _expected_target_chapters(document)
            ),
            accepted_obligation_ids=accepted_obligation_ids,
            accepted_obligation_windows=accepted_obligation_windows,
            author_constraints=author_constraints,
        ),
        *_unresolved_host_issues(document),
    )
    coverage = _coverage_evidence(
        document,
        raw_items,
        mode=mode,
        constraints=author_constraints,
        trusted_window=trusted_window,
    )
    blocking = tuple(issue for issue in (*issues, *extra) if issue.blocking)
    missing_window = any(
        issue.kind is ReviewIssueKind.LONG_RANGE_PAYOFF_WITHOUT_TIME_WINDOW for issue in extra
    )
    if missing_window:
        return draft.model_copy(
            update={
                "issues": (*issues, *extra),
                "coverage_evidence": coverage,
                "decision": ReviewDecision.HUMAN_REQUIRED,
                "revision_instruction": None,
                "verification_failures": citation_failures,
            }
        )
    if not blocking:
        # Nothing verified blocks this candidate.  An advisory-only review (including
        # a model REVISE whose every citation the host refused) may not authorize a
        # rewrite, so it settles as ACCEPT with the refused findings kept as
        # advisories and their reasons recorded for the next review.
        return draft.model_copy(
            update={
                "issues": (*issues, *extra),
                "coverage_evidence": coverage,
                "decision": ReviewDecision.ACCEPT,
                "revision_instruction": None,
                "verification_failures": citation_failures,
            }
        )
    instruction = _bounded_revision_instruction(
        blocking, tuple(issue for issue in extra if issue.blocking)
    )
    return draft.model_copy(
        update={
            "issues": (*issues, *extra),
            "coverage_evidence": coverage,
            "decision": ReviewDecision.REVISE,
            "revision_instruction": instruction,
            "verification_failures": citation_failures,
        }
    )


def _model_blocking_findings(issues: Sequence[PlanReviewIssue]) -> tuple[PlanReviewIssue, ...]:
    """The blocking findings that came from the reviewer's model call."""

    return tuple(issue for issue in issues if issue.blocking and not issue.host_issued)


_REVISION_DEMAND_LIMIT = 12


def _host_required_fields(issues: Sequence[PlanReviewIssue]) -> tuple[str, ...]:
    """The structured fields the host gates require for these issues.

    A host gate can only be answered by a structured field, so the host names the
    exact item/field pairs it will check again.  Without this annex a revision that
    answers the reviewer's prose keeps re-triggering the same host rejection.
    """

    demands: list[str] = []
    for issue in issues:
        if issue.kind is ReviewIssueKind.VOLUME_STAGE_WINDOW_VIOLATION:
            demands.extend(_volume_window_field_paths(issue))
            continue
        if issue.field_path is not None:
            for item_id in issue.affected_item_ids:
                demands.append(f"{item_id.root}.{issue.field_path}")
            continue
        fields = _HOST_ISSUE_REQUIRED_FIELDS.get(issue.kind, ())
        for item_id in issue.affected_item_ids:
            for field in fields:
                if issue.kind is ReviewIssueKind.UNRESOLVED_SCOPE_MISSING:
                    # The field lives on the advisory itself, and a real candidate kept
                    # the summary window while leaving the list empty, so the demand
                    # names the advisory and the alternative of removing the conflict
                    # from the draft instead of filing it as an advisory.
                    demands.append(
                        f"unresolved[{item_id.root}].{field} 必须列出它质疑的每一章"
                        "（或直接在提案中修掉该冲突，不再以 advisory 形式保留）"  # noqa: RUF001
                    )
                else:
                    demands.append(f"{item_id.root}.{field}")
    return tuple(dict.fromkeys(demands))


_VOLUME_WINDOW_SUMMARY = re.compile(r"^VOLUME_STAGE_WINDOW: (?P<path>[A-Za-z0-9_.\-]+): ")


def _volume_window_field_paths(issue: PlanReviewIssue) -> tuple[str, ...]:
    """The exact volume field paths a window violation requires the planner to fix.

    The gate records ``<item_id>.<stage_key>.<field>`` as the head of its summary, so
    the revision demand points at ``vol4_arc.midpoint_reversal.window`` instead of a
    generic instruction that the planner cannot map back to a slot.
    """

    match = _VOLUME_WINDOW_SUMMARY.match(issue.summary)
    if match is None:
        return tuple(f"{item.root}.role|window|serves" for item in issue.affected_item_ids)
    path = match.group("path")
    if issue.affected_item_ids and not path.startswith(issue.affected_item_ids[0].root):
        path = f"{issue.affected_item_ids[0].root}.{path}"
    return (path,)


def _bounded_revision_instruction(
    blocking: Sequence[PlanReviewIssue],
    host_findings: Sequence[PlanReviewIssue] = (),
) -> str:
    """Name the exact item/field pairs a verified blocking set requires fixing.

    The instruction is derived from the verified findings themselves rather than
    forwarded from the reviewer's free prose, because that prose is exactly where a
    refuted demand used to reach the planner.  A host finding already carries its
    own precise demand text; a verified model finding carries the field path, the
    constraint it violates and the condition it fails.
    """

    demands: list[str] = []
    for issue in blocking:
        if issue.host_issued:
            demands.append(" ".join(issue.summary.split()))
            continue
        named = ", ".join(item.root for item in issue.affected_item_ids)
        located = f"{named}.{issue.field_path}" if issue.field_path else named
        condition = " ".join((issue.unmet_condition or issue.summary).split())
        constraint = f" (constraint {issue.constraint_id})" if issue.constraint_id else ""
        demands.append(f"{located}: {condition}{constraint}")
    unique = tuple(dict.fromkeys(demand for demand in demands if demand))
    shown = unique[:_REVISION_DEMAND_LIMIT]
    instruction = "; ".join(shown)
    if len(unique) > len(shown):
        instruction = f"{instruction} (+{len(unique) - len(shown)} more verified findings)"
    required = _host_required_fields(host_findings)
    if required:
        instruction = f"{instruction} HOST_REQUIRED_FIELDS: {'; '.join(required)}"
    return instruction


def host_only_plan_review(
    *,
    target_payload: str,
    mode: AgentMode,
    expected_volume_count: int | None = None,
    expected_target_chapters: int | None = None,
    accepted_obligation_ids: frozenset[str] | None = None,
    accepted_obligation_windows: Mapping[str, tuple[int | None, int | None]] | None = None,
    author_constraints: Sequence[AuthorConstraint] = (),
    trusted_window: tuple[int, int] | None = None,
) -> PlanReviewDraft | None:
    """The host gate's own verdict, computed without a model call.

    Mechanical defects (missing structure, an unusable stage entry, a window outside
    its scope) are decidable from the payload alone, so they must not cost a review
    call.  ``None`` means the host alone cannot decide and the model review runs.
    """

    probe = PlanReviewDraft(
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        decision=ReviewDecision.ACCEPT,
        issues=(),
    )
    overlaid = apply_host_plan_review_constraints(
        probe,
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=target_payload,
        mode=mode,
        expected_volume_count=expected_volume_count,
        expected_target_chapters=expected_target_chapters,
        accepted_obligation_ids=accepted_obligation_ids,
        accepted_obligation_windows=accepted_obligation_windows,
        author_constraints=author_constraints,
        trusted_window=trusted_window,
    )
    return None if overlaid.decision is ReviewDecision.ACCEPT else overlaid


def _verified_model_issues(
    issues: Sequence[PlanReviewIssue],
    target_payload: str,
    *,
    items: Mapping[str, Mapping[str, object]] | None = None,
    constraint_ids: Container[str] | None = None,
) -> tuple[tuple[PlanReviewIssue, ...], tuple[str, ...]]:
    """Keep a model finding blocking only while its own citation is grounded.

    A reviewer that quotes text the candidate does not contain is describing a
    different artifact, so the finding is demoted to an advisory instead of being
    forwarded to the planner as a rewrite demand.  The citation must resolve the
    same way a reader would: the named item is located first, the declared field
    path is resolved *inside that item's payload*, and the quoted value is then
    matched against that one field.  Searching the whole payload let a quote from
    another volume justify a finding about this one, and a bare
    ``"350" in target_payload`` accepted a boundary the candidate never declared
    at the cited field.

    Returns the rewritten findings plus one reason per finding the host refused.
    """

    by_id = items if items is not None else _items_by_id(target_payload)
    verified: list[PlanReviewIssue] = []
    failures: list[str] = []
    for index, issue in enumerate(issues):
        # These fields are host observations/permissions.  A model may use the
        # same JSON keys, but its values cannot become authoritative merely by
        # being present in the reviewer response.
        normalized = issue.model_copy(
            update={
                "host_issued": False,
                "actual": None,
                "expected": None,
                "authorized_operations": (),
            }
        )
        if not normalized.blocking:
            verified.append(normalized)
            continue
        label = f"{normalized.kind.value}[{index}]"
        reason = _citation_failure(normalized, by_id, constraint_ids=constraint_ids)
        if reason is None:
            verified.append(normalized)
            continue
        failures.append(f"{label}: {reason}")
        detail = "the cited evidence does not resolve against this candidate"
        verified.append(
            normalized.model_copy(
                update={
                    "blocking": False,
                    "summary": f"REVIEW_EVIDENCE_UNVERIFIED: {issue.summary} ({detail})",
                }
            )
        )
    return tuple(verified), tuple(failures)


def _citation_failure(
    issue: PlanReviewIssue,
    by_id: Mapping[str, Mapping[str, object]],
    *,
    constraint_ids: Container[str] | None,
) -> str | None:
    """The reason this blocking finding's citation does not resolve, if any."""

    if not issue.affected_item_ids:
        return f"{ReviewCitationFailure.EVIDENCE_FIELDS_MISSING}: the finding names no item"
    if not issue.field_path:
        return (
            f"{ReviewCitationFailure.EVIDENCE_FIELDS_MISSING}: the finding declares no field path"
        )
    if not issue.quote:
        return f"{ReviewCitationFailure.EVIDENCE_FIELDS_MISSING}: the finding quotes nothing"
    if not issue.unmet_condition:
        return (
            f"{ReviewCitationFailure.EVIDENCE_FIELDS_MISSING}: the finding states no "
            "unmet condition"
        )
    if (
        constraint_ids is not None
        and issue.constraint_id is not None
        and issue.constraint_id not in constraint_ids
    ):
        return (
            f"{ReviewCitationFailure.CONSTRAINT_NOT_APPLICABLE}: {issue.constraint_id!r} is not "
            "part of the frozen catalogue this candidate was planned against"
        )
    resolved: list[tuple[str, object, object]] = []
    for item_id in issue.affected_item_ids:
        payload = by_id.get(item_id.root)
        if payload is None:
            return (
                f"{ReviewCitationFailure.ITEM_NOT_FOUND}: {item_id.root} is not an item of the "
                "reviewed candidate"
            )
        resolution = _resolve_field_path(payload, issue.field_path)
        if isinstance(resolution, str):
            return resolution
        resolved.append((item_id.root, resolution[0], resolution[1]))
    unmatched = [
        item_id
        for item_id, _parent, field_value in resolved
        if not _quote_matches(issue.quote, field_value)
    ]
    if unmatched:
        located = ", ".join(
            f"{item_id}.{issue.field_path}" for item_id, _parent, _value in resolved
        )
        return (
            f"{ReviewCitationFailure.VALUE_NOT_IN_FIELD}: {issue.quote!r} does not appear in "
            f"{', '.join(unmatched)}.{issue.field_path}; all named fields must contain the "
            f"citation (checked: {located})"
        )
    return None


def _quote_matches(quote: str, field_value: object) -> bool:
    """Whether the quoted evidence is really present in the cited field's value.

    Text is matched verbatim.  Numbers, windows and collections are matched by
    their own representation, because a field the candidate declared as ``350`` or
    ``"351-360"`` is legitimate evidence for the value the review reports; the
    candidate is not asked to word a structured field as prose first.
    """

    if isinstance(field_value, str):
        return quote in field_value
    if isinstance(field_value, bool) or field_value is None:
        return False
    if isinstance(field_value, (int, float)):
        return quote.strip() == str(field_value)
    if isinstance(field_value, Mapping):
        return any(
            _quote_matches(quote, item) for _key, item in field_value.items()
        ) or quote in json.dumps(field_value, ensure_ascii=False)
    if isinstance(field_value, (list, tuple)):
        return any(_quote_matches(quote, item) for item in field_value) or quote in json.dumps(
            list(field_value), ensure_ascii=False
        )
    return False


def _resolve_field_path(
    payload: Mapping[str, object], field_path: str
) -> tuple[object, object] | str:
    """Resolve a dotted field path inside one item payload.

    Returns ``(containing_value, field_value)`` or a failure string.  The grammar
    is deliberately small and explicit -- dotted keys with optional ``[n]`` list
    indices -- so a citation is checked the same way a reader would follow it and
    no expression from the model is ever evaluated.
    """

    segments = _field_path_segments(field_path)
    if segments is None:
        return (
            f"{ReviewCitationFailure.FIELD_PATH_INVALID}: {field_path!r} is not a dotted field "
            "path with optional [index] segments"
        )
    current: object = payload
    parent: object = payload
    for key, index in segments:
        if not isinstance(current, Mapping):
            return (
                f"{ReviewCitationFailure.FIELD_PATH_INVALID}: {field_path!r} descends into "
                f"{type(current).__name__}, which has no field {key!r}"
            )
        if key not in current:
            return (
                f"{ReviewCitationFailure.FIELD_NOT_FOUND}: {field_path!r} names no field in the "
                "reviewed item"
            )
        parent = current
        current = current[key]
        if index is None:
            continue
        if not isinstance(current, (list, tuple)):
            return (
                f"{ReviewCitationFailure.FIELD_PATH_INVALID}: {field_path!r} indexes a value "
                "that is not a list"
            )
        if index >= len(current):
            return (
                f"{ReviewCitationFailure.FIELD_NOT_FOUND}: {field_path!r} indexes element "
                f"{index} of a list with {len(current)} entries"
            )
        parent = current
        current = current[index]
    return parent, current


_FIELD_PATH_SEGMENT = re.compile(r"^(?P<key>[^\[\]]+)(?:\[(?P<index>\d+)\])?$")


def _field_path_segments(field_path: str) -> tuple[tuple[str, int | None], ...] | None:
    """Parse the small citation grammar: dotted keys with optional list indices.

    A private or dunder segment is refused outright.  Payload keys never start with
    an underscore, so such a path is a defect of the review rather than a lookup the
    host should attempt, and keeping the grammar narrow means no model-supplied
    string can steer the walk anywhere but into the candidate's own JSON.
    """

    stripped = field_path.strip()
    if not stripped:
        return None
    segments: list[tuple[str, int | None]] = []
    for raw in stripped.split("."):
        match = _FIELD_PATH_SEGMENT.match(raw)
        if match is None:
            return None
        key = match.group("key")
        if key.startswith("_") or any(character.isspace() for character in key):
            return None
        index_raw = match.group("index")
        segments.append((key, int(index_raw) if index_raw is not None else None))
    return tuple(segments)


def _candidate_field_values_for_issue(
    target_payload: str, issue: PlanReviewIssue
) -> dict[str, object]:
    """Expose exact source values for one bounded citation repair.

    The values are copied from the same candidate the host will verify.  They are
    diagnostic evidence, not a model finding or an authorization, and are included
    only in the failure message that asks a reviewer to repair a rejected citation.
    """

    if not issue.field_path:
        return {}
    items = _items_by_id(target_payload)
    values: dict[str, object] = {}
    for item_id in issue.affected_item_ids:
        payload = items.get(item_id.root)
        if payload is None:
            continue
        resolved = _resolve_field_path(payload, issue.field_path)
        if isinstance(resolved, tuple):
            values[item_id.root] = resolved[1]
    return values


def _items_by_id(target_payload: str) -> dict[str, Mapping[str, object]]:
    """Index a candidate's items by item id so citations resolve inside one item."""

    try:
        payload = json.loads(target_payload)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    return _item_payloads(payload)


def _arc_volume_comparison_view(target_payload: str) -> str | None:
    """Build a read-only same-slot view from the candidate under review.

    The full proposal is still the only review target and the host verifies every
    citation against it.  This projection is only a compact display of the exact
    source strings, so a long ARC_VOLUME payload does not make a cross-item audit
    depend on the model finding distant sibling fields by accident.  It contains no
    host verdict, finding, authorization, or instruction authority.
    """

    try:
        document = json.loads(target_payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, Mapping):
        return None
    raw_items = document.get("items")
    if not isinstance(raw_items, list):
        return None
    rows: list[dict[str, object]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        item_id = raw_item.get("item_id")
        payload = raw_item.get("payload")
        if not isinstance(item_id, str) or not isinstance(payload, Mapping):
            continue
        row: dict[str, object] = {"item_id": item_id}
        for key in _ARC_VOLUME_COMPARISON_KEYS:
            if key in payload:
                row[key] = payload[key]
        if len(row) > 1:
            rows.append(row)
    if not rows:
        return None
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def _item_payloads(payload: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return {}
    by_id: dict[str, Mapping[str, object]] = {}
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        item_id = raw.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            continue
        item_payload = raw.get("payload")
        by_id[item_id] = item_payload if isinstance(item_payload, Mapping) else raw
    return by_id


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
    accepted_obligation_windows: Mapping[str, tuple[int | None, int | None]] | None = None,
    author_constraints: Sequence[AuthorConstraint] = (),
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
                        _optional_int(
                            item_payload.get("chapter_start") or raw.get("chapter_start")
                        ),
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
                for window_defect in volume_stage_window_defects(
                    item_payload,
                    constraints=author_constraints,
                    accepted_obligation_ids=accepted_obligation_ids or frozenset(),
                    obligation_windows=accepted_obligation_windows,
                ):
                    issues.append(
                        _host_issue(
                            ReviewIssueKind.VOLUME_STAGE_WINDOW_VIOLATION,
                            (
                                f"VOLUME_STAGE_WINDOW: {item_id}."
                                f"{window_defect.field}: {window_defect.message}"
                            ),
                            item_id,
                            blocking=True,
                            field_path=window_defect.field,
                            constraint_id="host.volume_stage_window",
                            actual=window_defect.message,
                            expected=(
                                "stage window and served responsibility satisfy the host catalogue"
                            ),
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
    _append_missing_catalogue_issues(raw_items, issues, accepted_obligation_ids)
    return issues


def _append_missing_catalogue_issues(
    raw_items: list[object],
    issues: list[PlanReviewIssue],
    accepted_obligation_ids: frozenset[str] | None,
) -> None:
    """Report a candidate that cites obligations the host had no catalogue for.

    Three states have to stay distinguishable.  A readable catalogue decides which
    ids exist; ``None`` means no trusted World root reached this review, so the host
    cannot say whether a cited id exists or what window it carries.  An empty
    catalogue is a readable *statement* that there are no obligations, which is a
    different finding and is left to the per-id check.  A candidate that cites
    nothing needs no catalogue at all, so the gap is only reported where it actually
    prevented a check.
    """

    if accepted_obligation_ids is not None:
        return
    cited = sorted(
        {
            handle
            for raw in raw_items
            if isinstance(raw, Mapping)
            for handle in _cited_obligation_handles(raw)
        }
    )
    if not cited:
        return
    issues.append(
        _host_issue(
            ReviewIssueKind.OBLIGATION_CONTRACT,
            "OBLIGATION_CATALOGUE_UNKNOWN: this candidate references accepted obligations ("
            + ", ".join(cited[:8])
            + ") but no trusted World root reached the review, so their identity and time "
            "windows could not be checked",
            cited[0],
            blocking=True,
        )
    )


def _cited_obligation_handles(raw: Mapping[str, object]) -> tuple[str, ...]:
    """Every accepted-obligation handle a plan item's *stage entries* serve.

    Only ``serves`` is collected.  A stage that serves an accepted obligation is
    bound by that obligation's own window, so the host needs a catalogue to check it.
    A lower-level ``obligation_actions`` reference asks a different question -- does
    this id exist -- and is answered by the id check the caller already runs, so it
    does not by itself require the window catalogue.
    """

    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        payload = raw
    handles: list[str] = []
    for key in VOLUME_NARRATIVE_STAGE_KEYS:
        entry = payload.get(key)
        if not isinstance(entry, Mapping):
            continue
        served = entry.get("serves")
        if isinstance(served, str) and served.strip():
            handles.append(normalize_stage_handle(served))
    return tuple(handles)


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
            chapter_range = _chapter_window_value(payload.get("chapter_range"))
            if chapter_range is not None:
                chapters.extend(chapter_range)
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


def _chapter_window_value(value: object) -> tuple[int, int] | None:
    """Read a candidate's explicit window as review evidence, never as issue scope."""

    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d{1,4})\s*[-~\uff5e至到]\s*(\d{1,4})\s*", value)
        if match is not None:
            start, end = int(match.group(1)), int(match.group(2))
            if start >= 1 and end >= start:
                return start, end
        return None
    if (
        isinstance(value, (list, tuple))
        and value
        and all(type(item) is int and item >= 1 for item in value)
    ):
        values = tuple(value)
        return min(values), max(values)
    if isinstance(value, Mapping):
        start = value.get("chapter_start")
        end = value.get("chapter_end")
        if type(start) is int and type(end) is int and start >= 1 and end >= start:
            return start, end
    return None


def _source_handles(value: object) -> frozenset[str]:
    """Extract stable source handles used to associate an unresolved item."""

    if not isinstance(value, (list, tuple)):
        return frozenset()
    handles: set[str] = set()
    for item in value:
        if isinstance(item, str) and item.strip():
            handles.add(item.strip())
        elif isinstance(item, Mapping):
            artifact_id = item.get("artifact_id")
            if isinstance(artifact_id, str) and artifact_id.strip():
                handles.add(artifact_id.strip())
    return frozenset(handles)


def _unresolved_scope_hint(issue: PlanUnresolvedIssue, raw_items: object) -> tuple[int, int] | None:
    """Find one source-bound item window that explains an empty issue scope.

    The returned span is only a host finding's expected-condition evidence.  It is
    deliberately not copied into ``affected_chapters``: a model or a reviewer must
    still provide the structured chapter set before the issue becomes executable.
    """

    if not isinstance(raw_items, list):
        return None
    issue_sources = frozenset(source.root for source in issue.source_ids) | frozenset(
        ref.artifact_id.root for ref in issue.source_artifact_refs
    )
    issue_text = issue.summary.strip()
    matches: set[tuple[int, int]] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        item_payload = raw.get("payload")
        if not isinstance(item_payload, Mapping):
            item_payload = raw
        source_values = frozenset()
        for key in ("source_ids", "source_references", "source_artifact_refs"):
            source_values |= _source_handles(item_payload.get(key))
        source_match = bool(issue_sources & source_values)
        text_match = any(
            isinstance(item_payload.get(key), str)
            and issue_text
            and issue_text in item_payload[key]
            for key in ("title", "description", "summary")
        )
        if not source_match and not text_match:
            continue
        for key in (
            "affected_chapters",
            "chapter_range",
            "chapter_start",
            "target_chapter_start",
            "chapter_end",
            "target_chapter_end",
        ):
            value = item_payload.get(key)
            if key in {"chapter_start", "target_chapter_start"}:
                end_key = "chapter_end" if key == "chapter_start" else "target_chapter_end"
                value = {"chapter_start": value, "chapter_end": item_payload.get(end_key)}
            span = _chapter_window_value(value)
            if span is not None:
                matches.add(span)
    return next(iter(matches)) if len(matches) == 1 else None


def _unresolved_host_issues(payload: dict[str, Any]) -> list[PlanReviewIssue]:
    """Block accepted proposals that still carry unresolved hard conflicts.

    A non-blocking advisory also has to state the chapters it actually questions.
    An issue that names a bounded window in its text while leaving
    ``affected_chapters`` empty cannot be checked at the affected chapter, so it
    must not be accepted as a plan-wide advisory.
    """

    raw_issues = payload.get("unresolved")
    window = _proposal_chapter_window(payload.get("items"))
    issues: list[PlanReviewIssue] = []
    hard_kinds = hard_unresolved_kinds()
    seen_issue_ids: set[str] = set()
    if "unresolved" in payload and not isinstance(raw_issues, list):
        issues.append(
            _host_issue(
                ReviewIssueKind.BLOCKING_UNRESOLVED,
                "PLAN_UNRESOLVED_INVALID: unresolved must be a structured issue list",
                "unresolved",
                blocking=True,
                field_path="unresolved",
                constraint_id="plan.unresolved.shape",
                actual=type(raw_issues).__name__,
                expected="structured unresolved issue list",
                authorized_operations=("modify", "close"),
            )
        )
        raw_issue_entries: list[object] = []
    else:
        raw_issue_entries = raw_issues if isinstance(raw_issues, list) else []
    for index, raw in enumerate(raw_issue_entries):
        if not isinstance(raw, dict):
            issues.append(
                _host_issue(
                    ReviewIssueKind.BLOCKING_UNRESOLVED,
                    "PLAN_UNRESOLVED_INVALID: unresolved entries must be objects",
                    f"unresolved.{index}",
                    blocking=True,
                    field_path=f"unresolved[{index}]",
                    constraint_id="plan.unresolved.shape",
                    actual=type(raw).__name__,
                    expected="structured unresolved issue object",
                    authorized_operations=("modify", "close"),
                )
            )
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
                    field_path=f"unresolved[{index}]",
                    constraint_id="plan.unresolved.shape",
                    actual=type(raw).__name__,
                    expected="structured unresolved issue object",
                    authorized_operations=("modify", "close"),
                )
            )
            continue
        if issue.issue_id.root in seen_issue_ids:
            issues.append(
                _host_issue(
                    ReviewIssueKind.BLOCKING_UNRESOLVED,
                    f"PLAN_UNRESOLVED_DUPLICATE: {issue.issue_id.root}",
                    issue.issue_id.root,
                    blocking=True,
                    field_path="unresolved.issue_id",
                    constraint_id="plan.unresolved.unique_id",
                    actual=issue.issue_id.root,
                    expected="one active unresolved issue per identity",
                    authorized_operations=("modify", "close"),
                )
            )
        seen_issue_ids.add(issue.issue_id.root)
        affects_window = window is not None and issue.affects_chapters(*window)
        if issue.blocking or (issue.kind in hard_kinds and (affects_window or window is None)):
            issues.append(
                _host_issue(
                    ReviewIssueKind.BLOCKING_UNRESOLVED,
                    f"BLOCKING_UNRESOLVED[{issue.kind.value}]: {issue.summary}",
                    issue.issue_id.root,
                    blocking=True,
                    field_path="unresolved",
                    constraint_id=f"plan.unresolved.{issue.kind.value}",
                    actual=issue.summary,
                    expected="no blocking unresolved issue in the requested window",
                    authorized_operations=("modify", "close"),
                )
            )
            continue
        if issue.affected_chapters:
            continue
        summary_window = _summary_chapter_window(issue.summary)
        questioned = summary_window or _unresolved_scope_hint(issue, payload.get("items"))
        if questioned is None:
            continue
        source_hint = (
            "the related candidate item questions chapters"
            if summary_window is None
            else "this advisory questions chapters"
        )
        issues.append(
            _host_issue(
                ReviewIssueKind.UNRESOLVED_SCOPE_MISSING,
                "UNRESOLVED_SCOPE_MISSING: "
                f"{source_hint} {questioned[0]}-{questioned[1]} but declares no "
                "affected_chapters, so the uncertainty cannot be checked at the affected "
                "chapter",
                issue.issue_id.root,
                blocking=True,
                field_path="unresolved.affected_chapters",
                constraint_id="plan.unresolved.scope",
                actual="[]",
                expected=f"chapters {questioned[0]}-{questioned[1]}",
                authorized_operations=("modify",),
            )
        )
    raw_operations = payload.get("unresolved_operations")
    if "unresolved_operations" in payload and not isinstance(raw_operations, list):
        issues.append(
            _host_issue(
                ReviewIssueKind.BLOCKING_UNRESOLVED,
                "PLAN_UNRESOLVED_OPERATION_INVALID: unresolved_operations must be a list",
                "unresolved_operations",
                blocking=True,
                field_path="unresolved_operations",
                constraint_id="plan.unresolved.operation.shape",
                actual=type(raw_operations).__name__,
                expected="unresolved operation record list",
                authorized_operations=("modify", "close"),
            )
        )
    if isinstance(raw_operations, list):
        operation_ids: set[str] = set()
        active_ids = set(seen_issue_ids)
        for index, raw_operation in enumerate(raw_operations):
            try:
                operation = PlanUnresolvedOperationRecord.model_validate(
                    raw_operation, strict=False
                )
            except ValueError as error:
                issues.append(
                    _host_issue(
                        ReviewIssueKind.BLOCKING_UNRESOLVED,
                        f"PLAN_UNRESOLVED_OPERATION_INVALID: {error}",
                        f"unresolved-operation.{index}",
                        blocking=True,
                        field_path=f"unresolved_operations[{index}]",
                        constraint_id="plan.unresolved.operation.shape",
                        actual=type(raw_operation).__name__,
                        expected="valid ADD/MODIFY/CLOSE operation record",
                        authorized_operations=("modify", "close"),
                    )
                )
                continue
            if operation.issue_id.root in operation_ids:
                issues.append(
                    _host_issue(
                        ReviewIssueKind.BLOCKING_UNRESOLVED,
                        f"PLAN_UNRESOLVED_OPERATION_DUPLICATE: {operation.issue_id.root}",
                        operation.issue_id.root,
                        blocking=True,
                        field_path="unresolved_operations.issue_id",
                        constraint_id="plan.unresolved.operation.unique_id",
                        actual=operation.issue_id.root,
                        expected="one operation per unresolved identity",
                        authorized_operations=("modify", "close"),
                    )
                )
            operation_ids.add(operation.issue_id.root)
            if operation.operation.value == "close" and operation.issue_id.root in active_ids:
                issues.append(
                    _host_issue(
                        ReviewIssueKind.BLOCKING_UNRESOLVED,
                        "PLAN_UNRESOLVED_CLOSE_ACTIVE: a CLOSE record must not retain "
                        "an active issue",
                        operation.issue_id.root,
                        blocking=True,
                        field_path="unresolved_operations.operation",
                        constraint_id="plan.unresolved.close",
                        actual="close + active issue",
                        expected="closed issue removed from active unresolved list",
                        authorized_operations=("close",),
                    )
                )
            if operation.operation.value != "close" and operation.issue_id.root not in active_ids:
                issues.append(
                    _host_issue(
                        ReviewIssueKind.BLOCKING_UNRESOLVED,
                        "PLAN_UNRESOLVED_OPERATION_ORPHAN: non-CLOSE record has no active issue",
                        operation.issue_id.root,
                        blocking=True,
                        field_path="unresolved_operations.issue_id",
                        constraint_id="plan.unresolved.operation.active_pair",
                        actual=operation.issue_id.root,
                        expected="operation identity present in active unresolved list",
                        authorized_operations=("modify",),
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
    # One declaration contract with the materializer.  Host review previously
    # had its own reading and both disagreed: a live STORY item was ACCEPTed here
    # and refused at commit, and two legal declaration shapes were refused here
    # while the materializer accepted them.
    parse = parse_obligation_declarations(payload, item_kind=item_kind, item_id=item_id)
    for discrepancy in parse.discrepancies:
        issues.append(
            _host_issue(
                ReviewIssueKind.OBLIGATION_CONTRACT,
                f"OBLIGATION_DECLARATION_UNREADABLE: {discrepancy}",
                item_id,
                blocking=True,
            )
        )
    declarations = payload.get("obligation_declarations")
    if declarations and mode in {
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
    field_path: str | None = None,
    constraint_id: str | None = None,
    actual: str | None = None,
    expected: str | None = None,
    authorized_operations: tuple[str, ...] = ("modify",),
) -> PlanReviewIssue:
    # A host identity is semantic: wording and observed values may change while
    # the same item/field/constraint remains the same finding.  Hashing this
    # tuple also prevents two same-kind findings on one item from colliding.
    resolved_field = field_path or {
        ReviewIssueKind.LONG_RANGE_PAYOFF_WITHOUT_TIME_WINDOW: "not_before_chapter",
        ReviewIssueKind.EARLY_RESOLUTION_OF_FUTURE_LOCKED_OBLIGATION: "target_chapter_start",
        ReviewIssueKind.TARGET_WINDOW_OUTSIDE_PARENT_SCOPE: "chapter_range",
        ReviewIssueKind.OBLIGATION_CONTRACT: "obligation_contract",
        ReviewIssueKind.VOLUME_STAGE_WINDOW_VIOLATION: "stage.window",
        ReviewIssueKind.VOLUME_STRUCTURE_INCOMPLETE: "volume.structure",
        ReviewIssueKind.UNRESOLVED_SCOPE_MISSING: "affected_chapters",
        ReviewIssueKind.BLOCKING_UNRESOLVED: "unresolved",
        ReviewIssueKind.COVERAGE: "coverage",
    }.get(kind, "host")
    resolved_constraint = constraint_id or f"host.{kind.value}"
    identity = content_id(
        {
            "kind": kind.value,
            "item": item_id,
            "field": resolved_field,
            "constraint": resolved_constraint,
        }
    ).root.removeprefix("sha256:")[:32]
    return PlanReviewIssue(
        issue_id=bounded_stable_id(
            f"issue.{kind.value}.{identity}",
            f"issue.{kind.value}.{item_id}.{resolved_field}",
            f"issue.{kind.value}",
        ),
        kind=kind,
        summary=summary,
        blocking=blocking,
        affected_item_ids=(StableId(item_id),) if _is_stable_id(item_id) else (),
        field_path=resolved_field,
        constraint_id=resolved_constraint,
        actual=actual or summary,
        expected=expected or f"{resolved_constraint} satisfied",
        authorized_operations=authorized_operations,
        host_issued=True,
    )


def _is_stable_id(value: str) -> bool:
    try:
        StableId(value)
    except ValueError:
        return False
    return True


class PlanReviewerAgent:
    def __init__(
        self,
        runner: StructuredAgentRunner,
        artifacts: ArtifactRepository,
        *,
        accepted_world_ref: ArtifactRef | None = None,
        world_root_for_commit: Callable[[CommitId], ArtifactRef | None] | None = None,
    ) -> None:
        self._runner = runner
        self._artifacts = artifacts
        # The accepted World root is the only authority for the obligation
        # catalogue.  It is the root the task's basis commit binds, so the
        # reviewer never has to guess it from the candidate or rebuild the ids
        # from a second source.
        self._accepted_world_ref = accepted_world_ref
        # A long-running run commits new World roots (an STORY or ARC_VOLUME commit
        # adds obligations).  A reference frozen when the run was assembled would
        # keep reviewing lower levels against the startup catalogue and refuse a
        # legitimately declared id, so the World is resolved per review from the
        # task's own basis commit.
        self._world_root_for_commit = world_root_for_commit

    def _world_ref_for(self, base_commit: CommitId | None) -> ArtifactRef | None:
        """The World root the reviewed task's basis commit binds."""

        if base_commit is not None and self._world_root_for_commit is not None:
            try:
                resolved = self._world_root_for_commit(base_commit)
            except (KeyError, RuntimeError, ValueError):
                resolved = None
            if resolved is not None:
                return resolved
        return self._accepted_world_ref

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
        review_feedback: str | None = None,
        review_focus: str | None = None,
    ) -> tuple[PlanReview, ArtifactRef, ModelCallRecord]:
        inputs = (*trusted_source_artifacts, target_artifact)
        review_context = self._review_context_data(trusted_source_artifacts)
        context_block = (
            '<REVIEW_CONTEXT_DATA instruction_authority="none">\n'
            f"{review_context or '(no additional context data)'}\n"
            "</REVIEW_CONTEXT_DATA>"
        )
        review_payload = (
            f"REVIEW_TARGET_KIND={target_kind.value}\n"
            f"{context_block}\n"
            f"<REVIEW_TARGET_DATA>\n{target_payload}\n</REVIEW_TARGET_DATA>\n"
            "PLANNER_HIDDEN_REASONING=not_supplied"
        )
        if mode is AgentMode.ARC_VOLUME and target_kind is ReviewTargetKind.PLAN_PROPOSAL:
            comparison_view = _arc_volume_comparison_view(target_payload)
            if comparison_view is not None:
                review_payload += (
                    '\n<ARC_VOLUME_COMPARISON_VIEW trusted="false" authority="none">\n'
                    "这是从上方 REVIEW_TARGET_DATA 机械抽取的只读横向显示投影, 不是新的输入, "
                    "结论或授权. 请用它比较同名槽位的原文; 任何 blocking 意见仍必须引用完整候选"
                    "中每个 affected_item_id 的同一 field_path, 并由宿主重新核验. 不要把此投影中的"
                    "条目、字符串或排序当作宿主字段, 也不要因投影存在就假定有缺陷。\n"
                    f"{comparison_view}\n</ARC_VOLUME_COMPARISON_VIEW>"
                )
        if review_focus is not None and review_focus.strip():
            review_payload += (
                '\n<REVIEW_FOCUS authority="none">\n'
                "这是本次审查的问题域提示, 不是 finding、结论或授权. 请先完成该焦点要求的"
                "独立比较, 仍只从候选原文提取证据; 不要把焦点文字当作候选内容或预置缺陷。\n"
                + review_focus.strip()
                + "\n</REVIEW_FOCUS>"
            )
        if review_feedback is not None and review_feedback.strip():
            review_payload += (
                '\n<REVIEW_REPAIR_FEEDBACK trusted="true">\n'
                "上一份同候选审校未通过宿主证据核验。仅按下面的宿主反馈修正审校输出; 对列出的"
                "model finding 逐个回到候选字段重查。跨条目问题应把 quote 缩短为每个列出字段"
                "都逐字包含的共同片段, 并删除不命中的条目和占位符引用; 如果没有至少两个共同"
                "命中则删除该 blocking 观察。不要因为宿主拒绝旧 quote 就无条件删除其语义观察。"
                "若 decision 为 revise, 必须同时填写非空 revision_instruction。反馈不授予任何"
                "写入权限, 也不改变候选或作者约束。\n"
                + review_feedback.strip()
                + "\n</REVIEW_REPAIR_FEEDBACK>"
            )
        prepared = self._runner.prepare(
            AgentType.PLAN_REVIEWER,
            mode,
            version.root,
            request,
            review_payload,
            source_hashes=tuple(item.artifact_id for item in trusted_source_artifacts),
            input_artifacts=inputs,
            base_commit=base_commit,
        )
        execution = await self._runner.execute(prepared, PlanReviewDraft)
        draft = execution.output
        context_package = _planner_context_package(self._artifacts, trusted_source_artifacts)
        if draft.target_kind is not target_kind:
            raise PlanReviewerInvocationError("Reviewer changed the trusted target kind")
        world = _accepted_obligations(
            self._artifacts,
            trusted_source_artifacts,
            accepted_world_ref=self._world_ref_for(base_commit),
        )
        draft = apply_host_plan_review_constraints(
            draft,
            mode=mode,
            target_kind=target_kind,
            target_payload=target_payload,
            expected_volume_count=_expected_volume_count_from_context(review_context),
            expected_target_chapters=_expected_target_chapters_from_context(review_context),
            accepted_obligation_ids=(
                None
                if world is None
                else frozenset(item.obligation_id.root for item in world.obligations)
            ),
            accepted_obligation_windows=(
                None if world is None else obligation_stage_windows(world.obligations)
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
        if draft.verification_failures:
            # The review is not usable evidence about the candidate: at least one
            # blocking finding did not resolve against it.  The original response and
            # the draft that records the failed citations are already durable, so the
            # run stops at "a review is required" instead of forwarding a refuted
            # demand to the planner.  Re-reviewing the *same* candidate is the repair.
            verified_model_findings = [
                {
                    "affected_item_ids": [item.root for item in issue.affected_item_ids],
                    "field_path": issue.field_path,
                    "quote": issue.quote,
                    "unmet_condition": issue.unmet_condition,
                }
                for issue in draft.issues
                if issue.blocking and not issue.host_issued
            ]
            preserved = json.dumps(
                verified_model_findings, ensure_ascii=False, separators=(",", ":")
            )
            model_findings_to_recheck = [
                {
                    "issue_id": issue.issue_id.root,
                    "kind": issue.kind.value,
                    "affected_item_ids": [item.root for item in issue.affected_item_ids],
                    "field_path": issue.field_path,
                    "quote": issue.quote,
                    "unmet_condition": issue.unmet_condition,
                    "blocking_after_host_check": issue.blocking,
                    "candidate_field_values": _candidate_field_values_for_issue(
                        target_payload, issue
                    ),
                }
                for issue in draft.issues
                if not issue.host_issued
            ]
            recheck = json.dumps(
                model_findings_to_recheck, ensure_ascii=False, separators=(",", ":")
            )
            raise PlanReviewerInvocationError(
                "Plan review citations did not resolve against the reviewed candidate: "
                + "; ".join(draft.verification_failures[:4])
                + "; VERIFIED_MODEL_FINDINGS_TO_PRESERVE="
                + preserved[:4000]
                + "; MODEL_FINDINGS_TO_RECHECK="
                + recheck[:6000]
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
            verification_failures=draft.verification_failures,
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
            dict.fromkeys(part for part in (*rendered, *source_text) if part.strip())
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
    for key in (
        "obligations",
        "obligation_declarations",
        "key_obligations",
        "obligation_declaration",
    ):
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


def _accepted_obligations(
    artifacts: ArtifactRepository,
    refs: tuple[ArtifactRef, ...],
    *,
    accepted_world_ref: ArtifactRef | None,
) -> WorldRootDocument | None:
    """Read the accepted World root a lower-level plan must reference.

    One reader, one truth source: the same catalogue decides which obligation ids a
    candidate may cite and which time window each of those obligations imposes on a
    stage that serves it.  ``None`` means no trusted World root reached this review,
    which the callers report instead of silently skipping the check.
    """

    from novel_agent.domain.memory import WorldRootDocument

    candidates = [
        ref
        for ref in (
            accepted_world_ref,
            *(
                item
                for item in refs
                if item.media_type == "application/vnd.novel-agent.world-root+json"
            ),
        )
        if ref is not None
    ]
    for ref in candidates:
        try:
            return WorldRootDocument.model_validate_json(artifacts.read_verified(ref), strict=True)
        except (UnicodeDecodeError, ValueError):
            continue
    return None


def _accepted_obligation_ids(
    artifacts: ArtifactRepository,
    refs: tuple[ArtifactRef, ...],
    *,
    accepted_world_ref: ArtifactRef | None,
) -> frozenset[str] | None:
    """Read the obligation catalogue a lower-level plan must reference.

    A chapter may point at an obligation the upper-level plan already declared; it
    may not invent one.  ``None`` means no trusted catalogue could be read, which the
    review reports instead of silently skipping the check.
    """

    world = _accepted_obligations(artifacts, refs, accepted_world_ref=accepted_world_ref)
    if world is None:
        return None
    return frozenset(item.obligation_id.root for item in world.obligations)


def obligation_stage_windows(
    obligations: Sequence[PlanObligation],
) -> dict[str, tuple[int | None, int | None]]:
    """Map each accepted obligation to the window its own timing declares.

    ``not_before_chapter`` is a *reveal* lock: it says when the responsibility's
    content may start reaching the reader, so it bounds the early edge of a stage
    that serves it.  ``due_chapter`` is the other end of the same commitment.  A
    ``target_chapter_start`` is the payoff's own slot, not an unlock, so it does not
    replace the early edge and is deliberately not folded into one shared pair of
    numbers for every action.  An obligation that declares no window at all is
    reported as an unknown window rather than an unbounded one, so an unchecked
    stage is never written up as passing.
    """

    windows: dict[str, tuple[int | None, int | None]] = {}
    for obligation in obligations:
        if obligation.status in {ObligationStatus.RESOLVED, ObligationStatus.ABANDONED}:
            continue
        earliest = obligation.not_before_chapter
        latest = obligation.due_chapter
        if earliest is None and obligation.target_chapter_end is not None:
            earliest = obligation.target_chapter_start
        windows[obligation.obligation_id.root] = (earliest, latest)
    return windows


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
