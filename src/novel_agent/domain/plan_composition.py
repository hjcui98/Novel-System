"""Host composition of a scoped Plan revision, and the proof that it happened.

A reviewer names items and fields; a planner returns a whole proposal.  Somewhere
between them the host has to decide which parts of the returned proposal the review
actually authorised, because a request is not an enforcement.  That composition used
to be an internal step of the planning loop, which left the formal materializer with
a proposal whose bytes match no planner execution: the persisted
``PlannerExecutionResult`` still held the model's raw output, so a legitimately
composed and re-reviewed candidate could be refused at commit with "requires one
matching Planner execution receipt".

This module owns the composition, its scope and its proof, so the loop that runs it
and the materializer that has to verify it share exactly one implementation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.creative_runtime import OperatorReviewEvidence
from novel_agent.domain.ids import StableId
from novel_agent.domain.planning import PlanReview, PlanReviewIssue
from novel_agent.domain.stage2 import (
    PlanProposal,
    PlanUnresolvedIssue,
    PlanUnresolvedOperation,
    PlanUnresolvedOperationRecord,
    ProposedItem,
)
from novel_agent.services.content_addressing import canonical_json_bytes, content_id

# Changing what the host does to a revision changes what a proof means.  A proof
# names the rule that produced it, so a candidate composed under one rule is never
# silently re-verified under another.
COMPOSITION_RULE_VERSION = "scoped-revision.v3"


class PlanRevisionOperation(StrEnum):
    """The structural edits a review can authorise on top of field writes.

    A field finding authorises writing that field, never adding or removing items.
    Structural repair of a real defect has to be named explicitly, or "fix volume
    four's field" would silently mean "you may delete volume four".
    """

    MODIFY = "modify"
    ADD = "add"
    REMOVE = "remove"


class PlanRevisionTarget(DomainModel):
    """One item a revision is allowed to touch, and how."""

    item_id: StableId
    # Constructing a target at all means "edit this item"; add/remove are named on
    # top of that, never instead of it.
    operations: tuple[PlanRevisionOperation, ...] = (PlanRevisionOperation.MODIFY,)
    # Empty means the whole item is in scope, which is what a finding with no field
    # path authorises.  Otherwise these are exact dotted payload paths; naming a
    # nested description cannot move its sibling window, role, or serves metadata.
    field_paths: tuple[str, ...] = ()
    # An operator review may explicitly authorize converting one cited chapter
    # window into the existing chapter-number set contract.
    chapter_window: tuple[int, int] | None = None


class PlanRevisionScope(DomainModel):
    """The host-derived boundary of one revision."""

    rule_version: str = COMPOSITION_RULE_VERSION
    targets: tuple[PlanRevisionTarget, ...]
    # The verified findings the scope came from, kept so the scope can be re-derived
    # and audited instead of trusted.
    finding_ids: tuple[StableId, ...] = ()
    # Item ids the parent does not contain but the review explicitly authorised.
    additions: tuple[StableId, ...] = ()
    # Item ids the review explicitly authorised removing.
    removals: tuple[StableId, ...] = ()
    # Advisory ids (``plan-issue.``) the review explicitly authorised restating.  The
    # host files its advisory findings against these ids, so they are the only way a
    # revision may touch ``unresolved``.  Without this the scope carried no advisory
    # information at all, the composed plan inherited the revision's own advisory list
    # while every other unauthorised field was frozen to the parent, and the invariant
    # check saw that leak as "the composed plan changed its unresolved issues" -- so a
    # revision that refreshed its advisories could never compose, however correct its
    # volumes were.
    advisory_ids: tuple[StableId, ...] = ()
    # Kept separate from item targets so an advisory identity can never be treated as
    # a proposal item during composition.
    advisory_targets: tuple[PlanRevisionTarget, ...] = ()

    @model_validator(mode="after")
    def validate_targets(self) -> PlanRevisionScope:
        ids = [target.item_id for target in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("a revision scope may not target one item twice")
        for target in self.targets:
            if PlanRevisionOperation.MODIFY not in target.operations and target.field_paths:
                raise ValueError("field paths only qualify a modify operation")
        advisory_ids = [target.item_id for target in self.advisory_targets]
        if len(advisory_ids) != len(set(advisory_ids)):
            raise ValueError("a revision scope may not target one advisory twice")
        if any(
            not target.item_id.root.startswith(_ADVISORY_ID_PREFIX)
            for target in self.advisory_targets
        ):
            raise ValueError("advisory revision targets require the plan-issue namespace")
        return self

    @property
    def targeted_item_ids(self) -> frozenset[str]:
        return frozenset(target.item_id.root for target in self.targets)

    def target_for(self, item_id: str) -> PlanRevisionTarget | None:
        for target in self.targets:
            if target.item_id.root == item_id:
                return target
        return None

    def advisory_target_for(self, issue_id: str) -> PlanRevisionTarget | None:
        for target in self.advisory_targets:
            if target.item_id.root == issue_id:
                return target
        return None


class PlanCompositionProof(DomainModel):
    """Why a composed candidate is the parent plus exactly the authorised changes.

    The proof does not carry the documents: the parent proposal, the raw planner
    execution and the review are all reachable artifacts, and its identity is their
    content addresses.  It carries the scope and the rule version, so verification
    is a re-derivation rather than a comparison against a stored copy.
    """

    proof_id: StableId
    rule_version: str = COMPOSITION_RULE_VERSION
    parent_proposal_ref: ArtifactRef
    raw_execution_ref: ArtifactRef
    review_ref: ArtifactRef
    scope: PlanRevisionScope
    # The composed document this proof produced, by content address.  This is what
    # makes "recompute and compare" possible without trusting the stored candidate.
    composed_digest: str = Field(min_length=1)
    # Every item the raw revision moved without being authorised to, recorded as
    # evidence rather than as a silent correction.
    out_of_scope_item_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def validate_rule(self) -> PlanCompositionProof:
        if self.rule_version not in {
            "scoped-revision.v1",
            "scoped-revision.v2",
            COMPOSITION_RULE_VERSION,
        }:
            raise ValueError(f"unknown composition rule {self.rule_version!r}")
        return self


def proposal_digest(proposal: PlanProposal) -> str:
    """The content address of a proposal, independent of any stored identity."""

    return content_id(proposal.model_dump(mode="json")).root


def revision_scope(review: PlanReview) -> PlanRevisionScope:
    """Derive the revision boundary from a review's *verified* blocking findings.

    Only findings that block grant authority.  An advisory never does, and a finding
    the host refused is no longer blocking, so a refuted demand cannot widen the
    scope it would have needed.
    """

    targets: list[PlanRevisionTarget] = []
    additions: list[StableId] = []
    removals: list[StableId] = []
    finding_ids: list[StableId] = []
    advisory_ids: list[StableId] = []
    advisory_targets: list[PlanRevisionTarget] = []
    for issue in review.issues:
        if not issue.blocking:
            continue
        # ``affected_item_ids`` are the evidence rows used for comparison and
        # citation.  Only host-authorized target ids grant write scope.  Host-issued
        # findings predate the split and are safe to interpret through their own
        # evidence ids; model findings without an explicit host authorization grant
        # no scope at all.
        target_item_ids = issue.authorized_target_item_ids
        if not target_item_ids and issue.host_issued:
            target_item_ids = issue.affected_item_ids
        if not target_item_ids:
            continue
        finding_ids.append(issue.issue_id)
        fields = _issue_field_paths(issue)
        operations = _issue_operations(issue)
        for item_id in target_item_ids:
            if item_id.root.startswith(_ADVISORY_ID_PREFIX):
                # The host files advisory findings against these ids, so naming one is
                # the revision's permission to restate that advisory.  It is not an
                # item target: the advisory is not an item of the proposal.
                advisory_ids.append(item_id)
                existing_advisory = next(
                    (target for target in advisory_targets if target.item_id.root == item_id.root),
                    None,
                )
                if existing_advisory is None:
                    advisory_targets.append(
                        PlanRevisionTarget(
                            item_id=item_id,
                            operations=operations,
                            field_paths=fields,
                        )
                    )
                else:
                    advisory_targets[advisory_targets.index(existing_advisory)] = (
                        existing_advisory.model_copy(
                            update={
                                "operations": tuple(
                                    dict.fromkeys((*existing_advisory.operations, *operations))
                                ),
                                "field_paths": tuple(
                                    dict.fromkeys((*existing_advisory.field_paths, *fields))
                                ),
                            }
                        )
                    )
                continue
            if PlanRevisionOperation.ADD in operations:
                additions.append(item_id)
            if PlanRevisionOperation.REMOVE in operations:
                removals.append(item_id)
            existing = next(
                (target for target in targets if target.item_id.root == item_id.root), None
            )
            if existing is not None:
                merged_ops = tuple(dict.fromkeys((*existing.operations, *operations)))
                merged_fields = tuple(dict.fromkeys((*existing.field_paths, *fields)))
                targets[targets.index(existing)] = existing.model_copy(
                    update={"operations": merged_ops, "field_paths": merged_fields}
                )
                continue
            targets.append(
                PlanRevisionTarget(
                    item_id=item_id,
                    operations=operations,
                    field_paths=fields,
                )
            )
    return PlanRevisionScope(
        targets=tuple(targets),
        finding_ids=tuple(dict.fromkeys(finding_ids)),
        additions=tuple(dict.fromkeys(additions)),
        removals=tuple(dict.fromkeys(removals)),
        advisory_ids=tuple(dict.fromkeys(advisory_ids)),
        advisory_targets=tuple(advisory_targets),
    )


def operator_revision_scope(review: OperatorReviewEvidence) -> PlanRevisionScope:
    """Derive a write boundary from an immutable host/operator review.

    Operator findings already are host observations, so they do not need (and must
    not be converted into) a model ``PlanReview`` receipt.  Their affected ids are
    the host-authorized targets; the comparison/target split is enforced at the
    operator-review creation boundary and the parent/composition checks below still
    verify that every target is a real parent identity.
    """

    targets: list[PlanRevisionTarget] = []
    advisory_targets: list[PlanRevisionTarget] = []
    finding_ids: list[StableId] = []
    for finding in review.issues:
        if not finding.blocking or not finding.affected_item_ids:
            continue
        finding_ids.append(finding.issue_id)
        fields = () if not finding.field_path else (finding.field_path,)
        chapter_window = None
        if finding.field_path in {"affected_chapters", "unresolved.affected_chapters"}:
            match = re.fullmatch(
                r"\s*(?:chapters\s+)?(\d{1,4})\s*[-~\uff5e]\s*(\d{1,4})\s*",
                finding.expected or "",
            )
            if match is not None:
                start, end = int(match.group(1)), int(match.group(2))
                if 1 <= start <= end:
                    chapter_window = (start, end)
        for item_id in finding.affected_item_ids:
            target = PlanRevisionTarget(
                item_id=item_id,
                operations=(PlanRevisionOperation.MODIFY,),
                field_paths=fields,
                chapter_window=chapter_window,
            )
            destination = (
                advisory_targets if item_id.root.startswith(_ADVISORY_ID_PREFIX) else targets
            )
            existing = next(
                (current for current in destination if current.item_id == item_id), None
            )
            if existing is None:
                destination.append(target)
            else:
                destination[destination.index(existing)] = existing.model_copy(
                    update={
                        "field_paths": tuple(dict.fromkeys((*existing.field_paths, *fields))),
                        "chapter_window": chapter_window or existing.chapter_window,
                    }
                )
    return PlanRevisionScope(
        targets=tuple(targets),
        finding_ids=tuple(dict.fromkeys(finding_ids)),
        advisory_ids=tuple(target.item_id for target in advisory_targets),
        advisory_targets=tuple(advisory_targets),
    )


def _scope_for_review(review: PlanReview | OperatorReviewEvidence) -> PlanRevisionScope:
    if isinstance(review, OperatorReviewEvidence):
        return operator_revision_scope(review)
    return revision_scope(review)


def _issue_field_paths(issue: PlanReviewIssue) -> tuple[str, ...]:
    """The payload keys one finding authorises writing.

    A citation names the exact value the reviewer verified.  Keep the dotted path
    intact so the composer can preserve sibling metadata inside a structured stage
    entry.  A finding with no field path remains item-scoped for backwards-compatible
    host findings that intentionally authorise the complete item.
    """

    return () if not issue.field_path else (issue.field_path,)


def _issue_operations(issue: PlanReviewIssue) -> tuple[PlanRevisionOperation, ...]:
    """The structural edits one finding authorises.

    Structural repair has to be asked for.  A host finding that says an item is
    missing or duplicated authorises the matching add or remove; nothing else does,
    and a model finding never authorises deleting an item the host did not name.

    ADD is additionally restricted to ids that can *be* plan items.  The host files
    its advisory findings against ``plan-issue.`` ids, and those are not items of the
    proposal, so no wording may ever authorise adding one.  The wording heuristic
    alone did exactly that: ``UNRESOLVED_SCOPE_MISSING`` is a host finding whose
    English summary contains the word "missing", so a blocking advisory was read as
    "an item is missing", the advisory id entered the authorised scope, and the
    composed plan could never contain it.  The whole composed candidate was then
    rejected as out of scope, deterministically, on correct output.
    """

    if issue.authorized_operations:
        parsed: list[PlanRevisionOperation] = []
        for operation in issue.authorized_operations:
            if operation == "close":
                parsed.append(PlanRevisionOperation.REMOVE)
                continue
            try:
                parsed.append(PlanRevisionOperation(operation))
            except ValueError:
                # Unknown host operations are not permission to widen a revision.
                continue
        if parsed:
            return tuple(dict.fromkeys(parsed))
    summary = issue.summary
    operations = [PlanRevisionOperation.MODIFY]
    if issue.host_issued and _mentions_missing(summary) and not _is_advisory_id(issue):
        operations.append(PlanRevisionOperation.ADD)
    if issue.host_issued and _mentions_duplicate(summary):
        operations.append(PlanRevisionOperation.REMOVE)
    return tuple(operations)


# The reserved namespace for unresolved-advisory identities.  A proposal item never
# carries it, so it is the reliable way to tell "this finding names an item" from
# "this finding names an advisory".
_ADVISORY_ID_PREFIX = "plan-issue."


def _is_advisory_id(issue: PlanReviewIssue) -> bool:
    target_ids = issue.authorized_target_item_ids or issue.affected_item_ids
    return any(item_id.root.startswith(_ADVISORY_ID_PREFIX) for item_id in target_ids)


_MISSING_MARKERS = ("missing", "MISSING", "缺少", "缺卷", "未提供")
_DUPLICATE_MARKERS = ("duplicate", "DUPLICATE", "重复", "duplicated")


def _mentions_missing(summary: str) -> bool:
    return any(marker in summary for marker in _MISSING_MARKERS)


def _mentions_duplicate(summary: str) -> bool:
    return any(marker in summary for marker in _DUPLICATE_MARKERS)


def item_body(item: ProposedItem) -> bytes:
    """The part of an item a field comparison is about."""

    document = item.model_dump(mode="json")
    document.pop("item_id", None)
    return canonical_json_bytes(document)


class PlanCompositionError(ValueError):
    """A composed candidate that is not the parent plus the authorised changes."""


def validate_composed_proposal(
    parent: PlanProposal,
    composed: PlanProposal,
    scope: PlanRevisionScope,
) -> None:
    """Check the composed candidate as a whole, not just its item list.

    Comparing items alone would accept a composition that silently repeated or
    dropped an item, or that moved coverage, unresolved issues and the proposal's own
    metadata.  Everything outside the authorised writes has to equal the parent.
    """

    seen: set[str] = set()
    duplicates: list[str] = []
    for item in composed.items:
        key = item.item_id.root
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    if duplicates:
        raise PlanCompositionError(
            "composed plan repeats an item id: " + ", ".join(sorted(set(duplicates)))
        )
    if not composed.items:
        raise PlanCompositionError("composed plan has no items")
    parent_ids = {item.item_id.root for item in parent.items}
    expected = (parent_ids - {item.root for item in scope.removals}) | {
        item.root for item in scope.additions
    }
    if seen != expected:
        raise PlanCompositionError(
            "composed plan item set does not match the authorised scope: "
            f"missing {sorted(expected - seen)}, unexpected {sorted(seen - expected)}"
        )
    if composed.mode is not parent.mode or composed.project_id != parent.project_id:
        raise PlanCompositionError("composed plan changed its mode or project")
    if composed.base_commit != parent.base_commit:
        raise PlanCompositionError("composed plan changed its basis commit")
    if composed.coverage != parent.coverage:
        raise PlanCompositionError("composed plan changed its coverage")
    authorised_advisories = {item.root for item in scope.advisory_ids}
    authorised_advisories.update(target.item_id.root for target in scope.advisory_targets)
    expected_unresolved = {
        issue.issue_id.root: issue
        for issue in parent.unresolved
        if issue.issue_id.root not in authorised_advisories
    }
    seen_unresolved = {issue.issue_id.root: issue for issue in composed.unresolved}
    for key, issue in expected_unresolved.items():
        if seen_unresolved.get(key) != issue:
            # An advisory or unresolved conflict is not something a scoped revision is
            # allowed to edit away; no authorised finding named it.
            raise PlanCompositionError("composed plan changed its unresolved issues")
    unauthorised_extras = set(seen_unresolved) - set(expected_unresolved) - authorised_advisories
    if unauthorised_extras:
        raise PlanCompositionError(
            "composed plan added unresolved issues no finding named: "
            + ", ".join(sorted(unauthorised_extras))
        )
    _validate_unresolved_operation_scope(parent, composed, scope, authorised_advisories)
    if composed.strategy is not parent.strategy:
        raise PlanCompositionError("composed plan changed its strategy")


def compose_scoped_revision(
    parent: PlanProposal,
    revised: PlanProposal,
    scope: PlanRevisionScope,
) -> PlanProposal:
    """The parent proposal with exactly the scope's authorised changes applied.

    Every rule here recovers a specific way a whole-plan rewrite used to leak
    through: an item nobody named keeps its parent bytes, an unauthorised addition
    never enters the result, an unauthorised removal is restored, and inside a named
    item only the named fields may differ.
    """

    _validate_revised_unresolved_operations(parent, revised, scope)
    composed = _compose_items(parent, revised, scope)
    validate_composed_proposal(parent, composed, scope)
    return composed


def _compose_items(
    parent: PlanProposal,
    revised: PlanProposal,
    scope: PlanRevisionScope,
) -> PlanProposal:
    if (
        not scope.targets
        and not scope.additions
        and not scope.removals
        and not scope.advisory_ids
        and not scope.advisory_targets
    ):
        # Nothing was authorised, so nothing may change.  Returning the revision here
        # is what let a review with no usable finding rewrite the plan.
        return parent
    parent_items = {item.item_id.root: item for item in parent.items}
    composed: list[ProposedItem] = []
    seen: set[str] = set()
    for item in revised.items:
        key = item.item_id.root
        seen.add(key)
        target = scope.target_for(key)
        if target is None:
            # An item the review did not name keeps its exact parent bytes; a new one
            # it did not authorise never enters the result at all.
            if key in scope.additions:
                composed.append(item)
            elif key in parent_items:
                composed.append(parent_items[key])
            continue
        if PlanRevisionOperation.REMOVE in target.operations:
            continue
        parent_item = parent_items.get(key)
        if parent_item is None:
            # The review named this item, but naming an id the parent does not have
            # is only an addition when the finding authorised adding one.  Otherwise
            # it is a write to an item that does not exist, and it does not enter the
            # result.
            if PlanRevisionOperation.ADD in target.operations:
                composed.append(item)
            continue
        composed.append(_compose_item(parent_item, item, target))
    # An item the revision dropped without authorisation stays.
    for key, item in parent_items.items():
        if key in seen:
            continue
        if key in scope.removals:
            continue
        target = scope.target_for(key)
        if target is not None and PlanRevisionOperation.REMOVE in target.operations:
            continue
        composed.append(item)
    return revised.model_copy(
        update={
            "items": tuple(composed),
            "unresolved": _compose_unresolved(parent, revised, scope),
            "unresolved_operations": _compose_unresolved_operations(parent, revised, scope),
        }
    )


def _compose_unresolved(
    parent: PlanProposal,
    revised: PlanProposal,
    scope: PlanRevisionScope,
) -> tuple[PlanUnresolvedIssue, ...]:
    """The parent's advisories with only the restatements the review authorised.

    Advisories are part of the plan, so they follow the same rule as item fields: what
    the review did not name keeps the parent's bytes.  A revision that refreshes its
    advisory list therefore no longer changes the composed plan by itself; only an
    advisory the review actually named can be restated, and an advisory the revision
    dropped without being asked to stays.
    """

    authorised = {item.root for item in scope.advisory_ids}
    authorised.update(target.item_id.root for target in scope.advisory_targets)
    if not authorised:
        return parent.unresolved
    revised_by_id = {issue.issue_id.root: issue for issue in revised.unresolved}
    revised_by_parent = {
        issue.parent_issue_id.root: issue
        for issue in revised.unresolved
        if issue.parent_issue_id is not None
    }
    composed: list[PlanUnresolvedIssue] = []
    kept: set[str] = set()
    for issue in parent.unresolved:
        key = issue.issue_id.root
        target = scope.advisory_target_for(key)
        if target is None and key in scope.advisory_ids:
            target = PlanRevisionTarget(item_id=issue.issue_id)
        replacement = revised_by_id.get(key) or revised_by_parent.get(key)
        revised_operation = _operation_for(revised, key)
        if (
            target is not None
            and revised_operation is not None
            and revised_operation.operation is PlanUnresolvedOperation.CLOSE
        ):
            if PlanRevisionOperation.REMOVE not in target.operations:
                raise PlanCompositionError(
                    f"authorized unresolved {key} does not permit the CLOSE operation"
                )
            kept.add(key)
            continue
        if replacement is not None and target is not None:
            required = (
                PlanRevisionOperation.REMOVE
                if revised_operation is not None
                and revised_operation.operation is PlanUnresolvedOperation.CLOSE
                else PlanRevisionOperation.MODIFY
            )
            if required not in target.operations:
                replacement = None
        elif (
            replacement is None
            and target is not None
            and PlanRevisionOperation.REMOVE in target.operations
        ):
            # A permission to close is not itself a close operation. Keep the
            # parent's advisory until the revision supplies an explicit closure.
            replacement = None
        if replacement is not None and target is not None:
            if replacement.issue_id.root != key:
                raise PlanCompositionError(
                    "revised unresolved issue must retain its parent host identity"
                )
            composed.append(_compose_unresolved_fields(issue, replacement, target))
        else:
            composed.append(issue)
        kept.add(key)
    # An advisory the review named but the parent never carried is a new statement the
    # host asked for, so it enters only with an explicit ADD permission; anything else
    # the revision invented does not.
    for key, issue in revised_by_id.items():
        if key not in kept:
            target = scope.advisory_target_for(key)
            if target is not None and PlanRevisionOperation.ADD in target.operations:
                composed.append(issue)
    return tuple(composed)


def _compose_unresolved_fields(
    parent: PlanUnresolvedIssue,
    revised: PlanUnresolvedIssue,
    target: PlanRevisionTarget,
) -> PlanUnresolvedIssue:
    """A field finding changes only that advisory field and its operation identity."""

    if not target.field_paths or "unresolved" in target.field_paths:
        return revised
    allowed = {
        field.removeprefix("unresolved.")
        for field in target.field_paths
        if field.startswith("unresolved.") or field in type(parent).model_fields
    }
    if not allowed:
        return parent
    values = {field: getattr(revised, field) for field in allowed}
    if "affected_chapters" in values and target.chapter_window is not None:
        values["affected_chapters"] = _normalize_cited_window(
            revised.affected_chapters, target.chapter_window
        )
    values.update(operation=revised.operation, parent_issue_id=revised.parent_issue_id)
    return parent.model_copy(update=values)


def _normalize_cited_window(
    chapters: tuple[int, ...], window: tuple[int, int]
) -> tuple[int, ...]:
    full = tuple(range(window[0], window[1] + 1))
    if chapters == full:
        return chapters
    if chapters == window:
        return full
    raise PlanCompositionError("revised unresolved range does not match the cited window")


def _operation_for(
    proposal: PlanProposal,
    issue_id: str,
) -> PlanUnresolvedOperationRecord | None:
    for operation in proposal.unresolved_operations:
        if operation.issue_id.root == issue_id:
            return operation
    return None


def _legacy_unresolved_operation(issue: PlanUnresolvedIssue) -> PlanUnresolvedOperationRecord:
    return PlanUnresolvedOperationRecord(
        operation=issue.operation,
        issue_id=issue.issue_id,
        parent_issue_id=issue.parent_issue_id,
        kind=issue.kind,
        summary=issue.summary,
        affected_chapters=issue.affected_chapters,
        blocking=issue.blocking,
        resolution_owner=issue.resolution_owner,
        allowed_assumptions=issue.allowed_assumptions,
        forbidden_assumptions=issue.forbidden_assumptions,
        source_ids=issue.source_ids,
        source_artifact_refs=issue.source_artifact_refs,
    )


def _proposal_unresolved_operations(
    proposal: PlanProposal,
) -> tuple[PlanUnresolvedOperationRecord, ...]:
    if proposal.unresolved_operations:
        return proposal.unresolved_operations
    return tuple(_legacy_unresolved_operation(issue) for issue in proposal.unresolved)


def _compose_unresolved_operations(
    parent: PlanProposal,
    revised: PlanProposal,
    scope: PlanRevisionScope,
) -> tuple[PlanUnresolvedOperationRecord, ...]:
    """Compose operation history while keeping CLOSE evidence auditable."""

    if not parent.unresolved_operations and not revised.unresolved_operations:
        return ()
    authorised = {item.root for item in scope.advisory_ids}
    authorised.update(target.item_id.root for target in scope.advisory_targets)
    targets = {target.item_id.root: target for target in scope.advisory_targets}
    for issue_id in scope.advisory_ids:
        targets.setdefault(issue_id.root, PlanRevisionTarget(item_id=issue_id))
    parent_records = {
        record.issue_id.root: record for record in _proposal_unresolved_operations(parent)
    }
    revised_records = {
        record.issue_id.root: record for record in _proposal_unresolved_operations(revised)
    }
    composed: list[PlanUnresolvedOperationRecord] = []
    for key, record in parent_records.items():
        target = targets.get(key)
        replacement = revised_records.get(key)
        if target is None or key not in authorised:
            composed.append(record)
            continue
        if replacement is None:
            composed.append(record)
            continue
        # ``authorized_operations`` is a set of alternatives, not a priority
        # ordering.  A host finding may permit either a field repair (MODIFY) or
        # an explicit resolution (CLOSE); the presence of REMOVE must not turn a
        # valid MODIFY response into a false "lacks CLOSE" rejection.
        required = (
            PlanRevisionOperation.REMOVE
            if replacement.operation is PlanUnresolvedOperation.CLOSE
            else PlanRevisionOperation.MODIFY
        )
        if required not in target.operations:
            expected = (
                "CLOSE"
                if replacement.operation is PlanUnresolvedOperation.CLOSE
                else replacement.operation.value.upper()
            )
            raise PlanCompositionError(
                f"authorized unresolved {key} does not permit the {expected} operation"
            )
        if replacement.operation is PlanUnresolvedOperation.CLOSE:
            composed.append(replacement)
        else:
            allowed = {
                field.removeprefix("unresolved.")
                for field in target.field_paths
                if field.startswith("unresolved.") or field in type(record).model_fields
            }
            if not target.field_paths or "unresolved" in target.field_paths:
                composed.append(replacement)
            else:
                values = {field: getattr(replacement, field) for field in allowed}
                if "affected_chapters" in values and target.chapter_window is not None:
                    values["affected_chapters"] = _normalize_cited_window(
                        replacement.affected_chapters, target.chapter_window
                    )
                values.update(
                    operation=replacement.operation,
                    parent_issue_id=replacement.parent_issue_id,
                )
                composed.append(record.model_copy(update=values))
    for key, record in revised_records.items():
        if key in parent_records:
            continue
        target = targets.get(key)
        if target is None or PlanRevisionOperation.ADD not in target.operations:
            continue
        if record.operation is not PlanUnresolvedOperation.ADD:
            raise PlanCompositionError("new unresolved identity must use an explicit ADD")
        composed.append(record)
    if len({record.issue_id.root for record in composed}) != len(composed):
        raise PlanCompositionError("composed unresolved operation history repeats an identity")
    return tuple(composed)


def _validate_revised_unresolved_operations(
    parent: PlanProposal,
    revised: PlanProposal,
    scope: PlanRevisionScope,
) -> None:
    """Reject an explicit operation that the review did not authorise.

    Composition is intentionally restorative for ordinary whole-proposal output: an
    unmentioned advisory is copied from the parent.  That containment rule must not
    turn a structured operation into a silent no-op, though.  An unknown identity,
    a close of a non-existent/closed issue, or a modify without the matching parent
    permission is an invalid revision and has to stop at the host boundary.
    """

    if not revised.unresolved_operations:
        return
    parent_records = {
        record.issue_id.root: record for record in _proposal_unresolved_operations(parent)
    }
    targets = {target.item_id.root: target for target in scope.advisory_targets}
    targets.update(
        {
            issue_id.root: PlanRevisionTarget(item_id=issue_id)
            for issue_id in scope.advisory_ids
            if issue_id.root not in targets
        }
    )
    for record in revised.unresolved_operations:
        key = record.issue_id.root
        target = targets.get(key)
        if target is None:
            raise PlanCompositionError(
                f"revised unresolved operation names unknown or unauthorized identity: {key}"
            )
        parent_record = parent_records.get(key)
        if record.operation is PlanUnresolvedOperation.ADD:
            if parent_record is not None:
                raise PlanCompositionError(
                    f"revised unresolved ADD repeats an existing identity: {key}"
                )
            if PlanRevisionOperation.ADD not in target.operations:
                raise PlanCompositionError(
                    f"revised unresolved ADD is not authorized for identity: {key}"
                )
            continue
        if parent_record is None:
            raise PlanCompositionError(
                f"revised unresolved operation names unknown parent identity: {key}"
            )
        if parent_record.operation is PlanUnresolvedOperation.CLOSE:
            raise PlanCompositionError(
                f"revised unresolved operation reopens a closed identity: {key}"
            )
        if record.parent_issue_id != record.issue_id:
            raise PlanCompositionError(
                f"revised unresolved operation must retain its host identity: {key}"
            )
        required = (
            PlanRevisionOperation.REMOVE
            if record.operation is PlanUnresolvedOperation.CLOSE
            else PlanRevisionOperation.MODIFY
        )
        if required not in target.operations:
            raise PlanCompositionError(
                f"revised unresolved {record.operation.value} is not authorized for identity: {key}"
            )


def _validate_unresolved_operation_scope(
    parent: PlanProposal,
    composed: PlanProposal,
    scope: PlanRevisionScope,
    authorised_advisories: set[str],
) -> None:
    if not parent.unresolved_operations and not composed.unresolved_operations:
        return
    parent_records = {
        record.issue_id.root: record for record in _proposal_unresolved_operations(parent)
    }
    composed_records = {
        record.issue_id.root: record for record in _proposal_unresolved_operations(composed)
    }
    allowed_changes = {
        target.item_id.root
        for target in scope.advisory_targets
        if target.item_id.root in authorised_advisories
    }
    allowed_changes.update(item.root for item in scope.advisory_ids)
    for key, record in parent_records.items():
        if key not in allowed_changes and composed_records.get(key) != record:
            raise PlanCompositionError("composed plan changed unresolved operation history")
    unexpected = set(composed_records) - set(parent_records) - allowed_changes
    if unexpected:
        raise PlanCompositionError(
            "composed plan added unresolved operation history no finding named: "
            + ", ".join(sorted(unexpected))
        )


def _compose_item(
    parent_item: ProposedItem,
    revised_item: ProposedItem,
    target: PlanRevisionTarget,
) -> ProposedItem:
    """One authorised item: identity from the parent, only named paths free."""

    if not target.field_paths:
        return revised_item
    payload: dict[str, Any] = deepcopy(parent_item.payload)
    for field_path in target.field_paths:
        segments = _field_path_segments(field_path)
        if segments is None:
            raise PlanCompositionError(f"invalid authorised field path: {field_path!r}")
        present, value = _read_field_path(revised_item.payload, segments)
        _write_field_path(payload, segments, value, present=present)
    return revised_item.model_copy(update={"payload": payload})


_FIELD_PATH_SEGMENT = re.compile(r"^(?P<key>[^\[\]]+)(?:\[(?P<index>\d+)\])?$")


def _field_path_segments(field_path: str) -> tuple[tuple[str, int | None], ...] | None:
    """Parse the same small dotted-path grammar used by review citation checks."""

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


def _read_field_path(
    payload: Mapping[str, object], segments: Sequence[tuple[str, int | None]]
) -> tuple[bool, object]:
    """Read one parsed path without evaluating any model-supplied expression."""

    current: object = payload
    for key, index in segments:
        if not isinstance(current, Mapping) or key not in current:
            return False, None
        current = current[key]
        if index is not None:
            if not isinstance(current, (list, tuple)) or index >= len(current):
                return False, None
            current = current[index]
    return True, current


def _write_field_path(
    payload: dict[str, Any],
    segments: Sequence[tuple[str, int | None]],
    value: object,
    *,
    present: bool,
) -> None:
    """Copy or remove exactly one path while retaining all sibling values."""

    current: object = payload
    for position, (key, index) in enumerate(segments):
        last = position == len(segments) - 1
        if not isinstance(current, dict):
            raise PlanCompositionError("authorised field path descends into a non-object")
        if key not in current:
            if not present:
                return
            current[key] = [] if index is not None else {}
        if index is None:
            if last:
                if present:
                    current[key] = deepcopy(value)
                else:
                    current.pop(key, None)
                return
            current = current[key]
            continue
        sequence = current[key]
        if not isinstance(sequence, list):
            raise PlanCompositionError("authorised field path indexes a non-list value")
        if index >= len(sequence):
            if not present:
                return
            sequence.extend({} for _ in range(index + 1 - len(sequence)))
        if last:
            if present:
                sequence[index] = deepcopy(value)
            else:
                del sequence[index]
            return
        current = sequence[index]


def out_of_scope_items(
    parent: PlanProposal,
    revised: PlanProposal,
    scope: PlanRevisionScope,
) -> tuple[str, ...]:
    """Item ids the revision moved without the review naming them."""

    parent_bodies = {item.item_id.root: item_body(item) for item in parent.items}
    moved = [
        item.item_id.root
        for item in revised.items
        if parent_bodies.get(item.item_id.root) != item_body(item)
    ]
    return tuple(sorted(set(moved) - scope.targeted_item_ids))


def build_composition_proof(
    *,
    parent_ref: ArtifactRef,
    raw_execution_ref: ArtifactRef,
    review_ref: ArtifactRef,
    scope: PlanRevisionScope,
    composed: PlanProposal,
    out_of_scope: Sequence[str],
) -> PlanCompositionProof:
    """Assemble the proof for one composed candidate."""

    digest = proposal_digest(composed)
    identity = content_id(
        {
            "rule": COMPOSITION_RULE_VERSION,
            "parent": parent_ref.artifact_id.root,
            "raw_execution": raw_execution_ref.artifact_id.root,
            "review": review_ref.artifact_id.root,
            "scope": scope.model_dump(mode="json"),
            "composed": digest,
        }
    ).root.removeprefix("sha256:")[:24]
    return PlanCompositionProof(
        proof_id=StableId(f"plan-composition.{identity}"),
        parent_proposal_ref=parent_ref,
        raw_execution_ref=raw_execution_ref,
        review_ref=review_ref,
        scope=scope,
        composed_digest=digest,
        out_of_scope_item_ids=tuple(StableId(item) for item in out_of_scope),
    )


def verify_composition(
    proof: PlanCompositionProof,
    *,
    parent: PlanProposal,
    revised: PlanProposal,
    review: PlanReview | OperatorReviewEvidence,
    composed: PlanProposal,
) -> tuple[bool, str]:
    """Re-derive the composed candidate and compare it with the one presented.

    Returns ``(ok, reason)``.  Every rejection names the specific mismatch, so a
    tampered or stale proof is refused with the same precision the rest of the
    materializer uses.
    """

    if proof.rule_version != COMPOSITION_RULE_VERSION:
        return False, f"unsupported composition rule {proof.rule_version!r}"
    derived_scope = _scope_for_review(review)
    if derived_scope != proof.scope:
        return False, "composition scope does not match the review's verified findings"
    recomposed = compose_scoped_revision(parent, revised, derived_scope)
    if proposal_digest(recomposed) != proof.composed_digest:
        return False, "composition proof digest does not match the re-derived candidate"
    if canonical_json_bytes(recomposed.model_dump(mode="json")) != canonical_json_bytes(
        composed.model_dump(mode="json")
    ):
        return False, "presented candidate is not the composition of parent and revision"
    return True, ""


def blocking_issue_identity(review: PlanReview) -> tuple[str, ...]:
    """The stable identity of a review's blocking findings.

    Identity is what the finding *is*: its type, the item and field it names, and
    the responsibility it cites.  That is what survives a partial repair and what
    makes "the same problem stated differently" recognisable as the same problem.
    The finding's wording does not belong here -- folding it in made a partially
    repaired finding look like a new one, while keeping only kind and item made a
    cosmetic rephrase look like progress.
    """

    return tuple(
        sorted(
            "|".join(
                (
                    issue.kind.value,
                    ",".join(sorted(item.root for item in issue.affected_item_ids)),
                    issue.field_path or "",
                    issue.constraint_id or "",
                )
            )
            for issue in review.issues
            if issue.blocking
        )
    )


def blocking_issue_details(review: PlanReview) -> tuple[str, ...]:
    """The value-level part of a blocking finding: which condition is unmet.

    Kept apart from the identity so a caller can tell "same problem, worded
    differently" from "same problem, actually narrowed" without either comparison
    being polluted by the other.
    """

    return tuple(
        sorted(
            " ".join((issue.unmet_condition or issue.summary).split())
            for issue in review.issues
            if issue.blocking
        )
    )


def issue_identity_seed(
    current: PlanReview,
    attempted: Sequence[Sequence[str]],
) -> tuple[str, ...]:
    """The frontier a later review is compared against.

    Two things have to travel: which problem is outstanding right now, and which
    problems an earlier revision has *already* been spent on.  Without the second,
    a revision that swings between two states looks like progress every time it
    moves, because each move changes the current problem.
    """

    attempted_ids = sorted({entry for group in attempted for entry in group})
    return tuple(
        [
            *(f"finding:{entry}" for entry in blocking_issue_identity(current)),
            *(f"detail:{entry}" for entry in blocking_issue_details(current)),
            *(f"attempted:{entry}" for entry in attempted_ids),
        ]
    )


def seed_identity(seed: Sequence[str]) -> frozenset[str]:
    """The outstanding problem identity a recorded frontier holds."""

    return frozenset(entry[len("finding:") :] for entry in seed if entry.startswith("finding:"))


def seed_details(seed: Sequence[str]) -> frozenset[str]:
    """The unmet conditions a recorded frontier holds."""

    return frozenset(entry[len("detail:") :] for entry in seed if entry.startswith("detail:"))


def seed_attempted(seed: Sequence[str]) -> frozenset[str]:
    """The problem identities an earlier revision has already been spent on."""

    return frozenset(entry[len("attempted:") :] for entry in seed if entry.startswith("attempted:"))


def progress_against(seed: Sequence[str], review: PlanReview) -> bool:
    """Whether this review is progress on the frontier the seed records.

    The question is whether the *problem* moved, not whether the reviewer found
    something to say.  Progress is any of: every problem closed, a problem set that
    shrank, a problem this run has not already paid a revision for, or the same
    problem now stated more narrowly.  What is not progress: the same problem
    restated, and a problem the run already attempted that has come back -- that is
    oscillation, and it must be visible as such instead of buying a fresh revision
    on every lap.
    """

    previous_identity = seed_identity(seed)
    # The frontier's own problem counts as attempted: a review that restates it has
    # not found anything new, which the branch below decides on the condition.
    attempted = seed_attempted(seed) | previous_identity
    if not attempted:
        # Nothing has settled yet, so there is no frontier to progress against.
        return False
    current_identity = frozenset(blocking_issue_identity(review))
    if not current_identity:
        # Every problem closed, which is the strongest progress there is.
        return True
    if current_identity - attempted:
        # A problem this run has not already spent a revision on.
        return True
    if current_identity < previous_identity:
        return True
    if current_identity == previous_identity:
        return _narrower_condition(blocking_issue_details(review), seed_details(seed))
    # Same problems as an earlier attempt, and nothing new: oscillation.
    return False


def _narrower_condition(current: Sequence[str], previous: frozenset[str]) -> bool:
    """Whether every current condition says strictly less than one it answers.

    Compared per condition rather than as a set, because "越过 350 和 401 两处边界"
    narrowed to "越过 350" is the same problem stated more narrowly, and string
    equality cannot see that.
    """

    if not previous:
        return False
    unmatched = list(previous)
    for condition in current:
        tokens = set(condition.split())
        for candidate in unmatched:
            wider = set(candidate.split())
            if tokens < wider:
                unmatched.remove(candidate)
                break
        else:
            return False
    return True


def assess_composition(
    parent: PlanProposal,
    revised: PlanProposal,
    scope: PlanRevisionScope,
) -> tuple[bool, str]:
    """Whether a composition is even attemptable, and why not if it is not.

    A caller that wants a reason rather than an exception uses this; the composing
    path itself raises, because a rejected composition must not be silently
    downgraded into a different candidate.
    """

    try:
        compose_scoped_revision(parent, revised, scope)
    except PlanCompositionError as error:
        return False, str(error)
    return True, ""
