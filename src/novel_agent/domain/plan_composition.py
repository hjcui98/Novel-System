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

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import StableId
from novel_agent.domain.planning import PlanReview, PlanReviewIssue
from novel_agent.domain.stage2 import PlanProposal, ProposedItem
from novel_agent.services.content_addressing import canonical_json_bytes, content_id

# Changing what the host does to a revision changes what a proof means.  A proof
# names the rule that produced it, so a candidate composed under one rule is never
# silently re-verified under another.
COMPOSITION_RULE_VERSION = "scoped-revision.v1"


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
    # path authorises.  Otherwise only these payload fields may differ from the
    # parent, so naming one field cannot move the others.
    field_paths: tuple[str, ...] = ()


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

    @model_validator(mode="after")
    def validate_targets(self) -> PlanRevisionScope:
        ids = [target.item_id for target in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("a revision scope may not target one item twice")
        for target in self.targets:
            if PlanRevisionOperation.MODIFY not in target.operations and target.field_paths:
                raise ValueError("field paths only qualify a modify operation")
        return self

    @property
    def targeted_item_ids(self) -> frozenset[str]:
        return frozenset(target.item_id.root for target in self.targets)

    def target_for(self, item_id: str) -> PlanRevisionTarget | None:
        for target in self.targets:
            if target.item_id.root == item_id:
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
        if self.rule_version != COMPOSITION_RULE_VERSION:
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
    for issue in review.issues:
        if not issue.blocking:
            continue
        finding_ids.append(issue.issue_id)
        fields = _issue_field_paths(issue)
        operations = _issue_operations(issue)
        for item_id in issue.affected_item_ids:
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
    )


def _issue_field_paths(issue: PlanReviewIssue) -> tuple[str, ...]:
    """The payload keys one finding authorises writing.

    A citation names a value (``midpoint_reversal.window``); the unit of authorised
    change is the payload entry that value lives in, because a stage entry is edited
    as a whole and splitting it would leave a half-written entry.  Anything outside
    that entry stays the parent's.
    """

    if not issue.field_path:
        return ()
    key = _top_level_key(issue.field_path)
    return (key,) if key else ()


def _issue_operations(issue: PlanReviewIssue) -> tuple[PlanRevisionOperation, ...]:
    """The structural edits one finding authorises.

    Structural repair has to be asked for.  A host finding that says an item is
    missing or duplicated authorises the matching add or remove; nothing else does,
    and a model finding never authorises deleting an item the host did not name.
    """

    summary = issue.summary
    operations = [PlanRevisionOperation.MODIFY]
    if issue.host_issued and _mentions_missing(summary):
        operations.append(PlanRevisionOperation.ADD)
    if issue.host_issued and _mentions_duplicate(summary):
        operations.append(PlanRevisionOperation.REMOVE)
    return tuple(operations)


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
    if composed.unresolved != parent.unresolved:
        # An advisory or unresolved conflict is not something a scoped revision is
        # allowed to edit away; no authorised finding names it.
        raise PlanCompositionError("composed plan changed its unresolved issues")
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

    composed = _compose_items(parent, revised, scope)
    validate_composed_proposal(parent, composed, scope)
    return composed


def _compose_items(
    parent: PlanProposal,
    revised: PlanProposal,
    scope: PlanRevisionScope,
) -> PlanProposal:
    if not scope.targets and not scope.additions and not scope.removals:
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
    return revised.model_copy(update={"items": tuple(composed)})


def _compose_item(
    parent_item: ProposedItem,
    revised_item: ProposedItem,
    target: PlanRevisionTarget,
) -> ProposedItem:
    """One authorised item: identity from the parent, only named fields free."""

    if not target.field_paths:
        return revised_item
    payload: dict[str, Any] = dict(parent_item.payload)
    for field_path in target.field_paths:
        key = _top_level_key(field_path)
        if key in revised_item.payload:
            payload[key] = revised_item.payload[key]
        else:
            payload.pop(key, None)
    return revised_item.model_copy(update={"payload": payload})


def _top_level_key(field_path: str) -> str:
    """The payload key a dotted citation writes to.

    A citation names ``midpoint_reversal.window``; the unit of authorised change is
    that entry, so the whole entry is taken from the revision once any part of it was
    named.  Anything outside it stays the parent's.
    """

    return field_path.split(".", 1)[0].split("[", 1)[0].strip()


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
    review: PlanReview,
    composed: PlanProposal,
) -> tuple[bool, str]:
    """Re-derive the composed candidate and compare it with the one presented.

    Returns ``(ok, reason)``.  Every rejection names the specific mismatch, so a
    tampered or stale proof is refused with the same precision the rest of the
    materializer uses.
    """

    if proof.rule_version != COMPOSITION_RULE_VERSION:
        return False, f"unsupported composition rule {proof.rule_version!r}"
    derived_scope = revision_scope(review)
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

    return frozenset(
        entry[len("attempted:") :] for entry in seed if entry.startswith("attempted:")
    )


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
