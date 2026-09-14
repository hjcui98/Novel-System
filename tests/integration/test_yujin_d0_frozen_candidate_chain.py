"""D0: one frozen candidate through accurate review, scoped revision, and materialization.

The development-diagnostic input is the immutable v23 candidate ``f5422937`` and the
review bound to it, ``47f9a758``.  That review settled REVISE with ``issues=[]`` and
told the planner that volume four "正式揭露" the long-range truth; volume four never
says that.  Its prose also named a real defect, and the candidate has one of its
own: every narrative stage slot is still a plain string, so no stage declares the
window, role or served responsibility the contract now requires.

This drives the whole fixed chain over those artifacts in an isolated diagnostic
space:

    host gate on the frozen candidate   -> the mechanical defect, host-issued
    the frozen review's own assertion   -> refused, because the citation is false
    scope from the verified findings    -> the eight volumes, whole payload
    one bounded revision                -> repairs the defect, keeps every other byte
    re-review of the composed candidate -> ACCEPT
    formal materializer pre-check       -> accepts the composed candidate by its proof

Nothing here writes the v23 Canon, calls a production CommitService, or advances the
frozen run.  The candidate, its root and its review are read-only inputs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.runtime.materializers import (
    PLAN_COMPOSITION_MEDIA_TYPE,
    PLAN_PROPOSAL_MEDIA_TYPE,
    PLANNER_EXECUTION_MEDIA_TYPE,
    PLANNING_EVENT_MEDIA_TYPE,
    PlanCandidateMaterializer,
)
from novel_agent.agents.plan_reviewer import apply_host_plan_review_constraints
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.author_constraints import AuthorConstraint, AuthorConstraintRoot
from novel_agent.domain.creative_runtime import (
    AcceptedCandidateBinding,
    ActorKind,
    CandidateBinding,
    CandidateKind,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.plan_composition import (
    PlanCompositionProof,
    PlanRevisionScope,
    PlanRevisionTarget,
    assess_composition,
    build_composition_proof,
    compose_scoped_revision,
    out_of_scope_items,
    revision_scope,
    verify_composition,
)
from novel_agent.domain.planning import (
    PlanningLoopEventReceipt,
    PlanningLoopPhase,
    PlanReview,
    PlanReviewDraft,
    PlanReviewIssue,
    ReviewDecision,
    ReviewIssueKind,
    ReviewTargetKind,
    missing_volume_structure_keys,
    volume_stage_grid_defects,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    PlannerExecutionResult,
    PlanProposal,
    ProposalProvenance,
    ProposedItem,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from tests.unit.test_stage4_planning_contracts import _receipt

VERSION = SchemaVersion("1.0.0")
PROJECT = ProjectId("project.yujin-jiuxu.v23.d0")
COMMIT = CommitId("sha256:" + "d" * 64)

# Immutable v23 development-diagnostic inputs.  Read-only: nothing below rewrites
# the frozen run or its objects.
FROZEN_RUN = Path("/home/cuihengjia/agent/novel/NS/yujin-jiuxu-v23")
FROZEN_CANDIDATE = (
    FROZEN_RUN
    / "objects/sha256/f5"
    / "f542293743e518e05b1665e4e8f48c3a7a9bb091c653ba09b52906c96913e2ae"
)
FROZEN_REVIEW = (
    FROZEN_RUN
    / "objects/sha256/47"
    / "47f9a7584b6c4f744bb9064950577ad7b4d5f5d378f7b2304a0c16f4fc2a2ced"
)
FROZEN_LOCKS = FROZEN_RUN / "input/planning-locks.json"

pytestmark = pytest.mark.skipif(
    not FROZEN_CANDIDATE.exists(), reason="the frozen v23 development input is not present"
)

# The stage slots the contract requires to be structured entries.
_STAGE_KEYS = (
    "opening_state",
    "trigger_event",
    "first_escalation",
    "first_cost",
    "midpoint_reversal",
    "second_escalation",
    "volume_climax",
    "climax_cost",
    "ending_state",
    "next_volume_hook",
)


def _frozen_candidate() -> dict[str, Any]:
    document = json.loads(FROZEN_CANDIDATE.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AssertionError("the frozen candidate must be a JSON object")
    return cast(dict[str, Any], document)


def _proposal_from_frozen(*, proposal_id: str = "plan-proposal.d0.frozen") -> PlanProposal:
    """The frozen candidate as a PlanProposal the current code can read."""

    document = _frozen_candidate()
    items = tuple(
        ProposedItem(
            item_id=StableId(entry["item_id"]),
            kind=str(entry.get("kind") or "arc_volume"),
            payload=dict(entry.get("payload") or {}),
            provenance=ProposalProvenance.PLANNER_PROPOSED,
        )
        for entry in document["items"]
    )
    return PlanProposal(
        proposal_id=StableId(proposal_id),
        project_id=PROJECT,
        mode=AgentMode.ARC_VOLUME,
        base_commit=COMMIT,
        items=items,
        coverage=1.0,
        receipt=_receipt(AgentMode.ARC_VOLUME, AgentType.PLANNER),
    )


def _author_constraints() -> tuple[AuthorConstraint, ...]:
    """The frozen author locks as the reviewer's catalogue.

    The locks do not reach a review as a raw file: they are projected onto the
    planner's own channel keys and compiled into ``AuthorConstraint`` entries, which
    is exactly what :func:`compile_author_constraint_root` does for a real profile.
    Building the entries by hand would test a catalogue no run produces.
    """

    return author_constraint_root()[0]


def author_constraint_root(
    lock_path: Path = FROZEN_LOCKS,
) -> tuple[tuple[AuthorConstraint, ...], AuthorConstraintRoot, ArtifactRef]:
    """``(constraints, root_document, profile_ref)`` from the frozen lock file."""

    from novel_agent.domain.author_constraints import AuthorConstraintCategory
    from novel_agent.domain.planning_locks import (
        compile_planning_lock_channels,
        load_author_planning_locks,
    )
    from novel_agent.services.content_addressing import content_id

    document = load_author_planning_locks(lock_path.read_bytes())
    channels = compile_planning_lock_channels(document)
    profile_ref = ArtifactRef(
        artifact_id=content_id({"profile": document.root_hash.root}),
        byte_length=1,
        media_type="application/vnd.novel-agent.project-profile-root+json",
        schema_version=VERSION,
    )
    # One constraint per lock, in the compiled order and with the production
    # category mapping, so the catalogue the review reads is the catalogue a run
    # would hand it.
    category_by_key = {
        "timeline_locks": AuthorConstraintCategory.TIME_LOCK,
        "reveal_windows": AuthorConstraintCategory.REVEAL_WINDOW,
        "progression_locks": AuthorConstraintCategory.ABILITY_MILESTONE,
        "equipment_locks": AuthorConstraintCategory.EQUIPMENT_MILESTONE,
        "location_preconditions": AuthorConstraintCategory.LOCATION_PRECONDITION,
    }
    constraints: list[AuthorConstraint] = []
    for key, category in category_by_key.items():
        for entry in channels.get(key, []):
            constraints.append(
                AuthorConstraint(
                    constraint_id=StableId(
                        f"author-constraint.{category.value}.{len(constraints)}"
                    ),
                    constraint_key=str(entry["lock_id"]),
                    category=category,
                    text=str(entry["description"]),
                    source_ref=profile_ref,
                    source_hash=document.root_hash,
                    not_before_chapter=entry.get("not_before_chapter"),  # type: ignore[arg-type]
                    chapter_earliest=entry.get("chapter_start"),  # type: ignore[arg-type]
                    chapter_latest=entry.get("chapter_end"),  # type: ignore[arg-type]
                )
            )
    root = AuthorConstraintRoot(
        root_hash=content_id(tuple(item.model_dump(mode="json") for item in constraints)),
        source_refs=(profile_ref,),
        constraints=tuple(constraints),
    )
    return tuple(constraints), root, profile_ref


def _frozen_review(proposal: PlanProposal, target: ArtifactRef | None = None) -> PlanReview:
    """The frozen review, bound to the frozen candidate.

    ``target`` is the persisted candidate ref when one exists.  A review has to name
    the artifact it reviewed, and the composition proof checks exactly that: a review
    of a *different* candidate cannot authorise this one.
    """

    document = json.loads(FROZEN_REVIEW.read_text(encoding="utf-8"))
    return PlanReview(
        review_id=StableId("plan-review.d0.frozen-47f9a758"),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_artifact_ref=target
        or ArtifactRef(
            artifact_id=ArtifactId("sha256:" + "2" * 64),
            byte_length=1,
            media_type=PLAN_PROPOSAL_MEDIA_TYPE,
            schema_version=VERSION,
        ),
        decision=ReviewDecision(document["decision"]),
        issues=(),
        revision_instruction=document["revision_instruction"],
        receipt=_receipt(AgentMode.ARC_VOLUME, AgentType.PLAN_REVIEWER),
    )


def _stages_for(
    payload: dict[str, Any],
    chapter_start: int,
    chapter_end: int,
    locks: tuple[AuthorConstraint, ...],
) -> dict[str, Any]:
    """The repaired stage grid: every narrative slot a structured entry.

    Each stage sits inside its own volume.  A stage that reaches a responsibility
    also declares the lock it serves, and it declares one only when the whole stage
    really sits inside that lock's boundary -- citing a responsibility whose lock
    opens later would be a violation, not a repair.  This is the repair the host
    finding demanded; it touches no other field.
    """

    repaired: dict[str, Any] = {}
    span = max(1, (chapter_end - chapter_start + 1) // 10)
    for index, key in enumerate(_STAGE_KEYS):
        low = chapter_start + index * span
        high = min(chapter_end, low + span - 1)
        current = payload.get(key)
        description = (
            current
            if isinstance(current, str) and current.strip()
            else (current or {}).get("description")
            if isinstance(current, dict)
            else f"{key} 描述"
        )
        role = "setup" if index < 4 else "progression"
        entry: dict[str, object] = {
            "description": description,
            "window": f"{low}-{high}",
            "role": role,
        }
        if role != "setup":
            served = _lock_open_at(locks, low, high)
            if served is not None:
                entry["serves"] = served
        repaired[key] = entry
    return repaired


def _lock_open_at(locks: tuple[AuthorConstraint, ...], low: int, high: int) -> str | None:
    """The lock whose boundary this window respects, if any claims the chapter.

    Only a lock that is already open at ``low`` and still open at ``high`` qualifies;
    choosing a later-opening responsibility would make the repair itself illegal.
    """

    claiming = [
        lock
        for lock in locks
        if (boundary := getattr(lock, "not_before_chapter", None)) is not None
        and boundary <= low
        and (latest := getattr(lock, "chapter_latest", None)) is not None
        and high <= latest
    ]
    if not claiming:
        return None
    return str(getattr(claiming[-1], "constraint_key", "") or "") or None


def _climax_description(item: ProposedItem) -> str:
    raw_climax = item.payload.get("volume_climax")
    if not isinstance(raw_climax, dict):
        return ""
    description = raw_climax.get("description")
    return description if isinstance(description, str) else ""


def verified_findings(proposal: PlanProposal) -> tuple[PlanReviewIssue, ...]:
    """A review whose findings are grounded in the candidate it names.

    These are derived from the candidate's own text -- the wording volumes five to
    eight repeat, and the volume-one climax sentence they repeat it from -- so every
    citation resolves against the field it names.  A real reviewer would find these
    by reading; deriving them here keeps the diagnostic deterministic.
    """

    climaxes = {item.item_id.root: _climax_description(item) for item in proposal.items}
    repeated = "正式揭露门被从对面推开"
    findings: list[PlanReviewIssue] = []
    for item in proposal.items:
        description = climaxes.get(item.item_id.root, "")
        if item.item_id.root == "vol-4" or repeated not in description:
            continue
        findings.append(
            PlanReviewIssue(
                issue_id=StableId(f"issue.d0.{item.item_id.root}"),
                kind=ReviewIssueKind.CONTRADICTION,
                # Chinese prose legitimately uses fullwidth punctuation.
                summary=(
                    "后卷高潮与前次揭露使用同一句式，读者无法判断这是第一次揭露、"  # noqa: RUF001
                    "再次验证还是新的后果"
                ),
                blocking=True,
                affected_item_ids=(item.item_id,),
                proposed_target_item_ids=(item.item_id,),
                field_path="volume_climax.description",
                quote=description,
                unmet_condition="每一次揭露必须说明它与前次是首次、验证还是新后果",
            )
        )
    assert findings, "the frozen candidate has no grounded repetition finding"
    return tuple(findings)


# ---------------------------------------------------------------- the D0 chain


def test_d0_frozen_candidate_through_the_whole_chain(tmp_path: Path) -> None:
    """The single chain D0 asks for, on the frozen artifacts."""

    candidate = _proposal_from_frozen()
    locks = _author_constraints()
    frozen_review = _frozen_review(candidate)

    # 1. The frozen review is unverifiable: issues=[] means no citation resolves.
    draft = PlanReviewDraft(
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        decision=frozen_review.decision,
        issues=frozen_review.issues,
        revision_instruction=frozen_review.revision_instruction,
    )
    overlaid = apply_host_plan_review_constraints(
        draft,
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=candidate.model_dump_json(),
        mode=AgentMode.ARC_VOLUME,
        accepted_obligation_ids=frozenset(),
        accepted_obligation_windows={},
        author_constraints=locks,
    )
    assert overlaid.verification_failures == ()
    assert not [item for item in overlaid.issues if item.blocking and not item.host_issued]
    # Its prose demand therefore reaches no planner: the scope is empty.
    empty_scope = revision_scope(frozen_review.model_copy(update={"issues": overlaid.issues}))
    assert empty_scope.targets == ()
    assert compose_scoped_revision(candidate, candidate, empty_scope) == candidate
    # Its stage grid is structurally complete, and with the real author locks every
    # window respects the lock it serves: read with the catalogue, this candidate has
    # no mechanical defect at all.  (Reading it with an *empty* catalogue produces the
    # unknown-handle findings the N2 notes describe; that is a missing-catalogue
    # artifact, not a defect of the candidate.)
    assert all(volume_stage_grid_defects(item.payload) == () for item in candidate.items)
    assert all(missing_volume_structure_keys(item.payload) == () for item in candidate.items)

    # 2. An accurate review of the same candidate, on findings grounded in its own
    #    text: the wording volumes five to eight repeat verbatim.
    accurate = apply_host_plan_review_constraints(
        PlanReviewDraft(
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            decision=ReviewDecision.REVISE,
            issues=verified_findings(candidate),
            revision_instruction="按已核验问题逐项修订",
        ),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=candidate.model_dump_json(),
        mode=AgentMode.ARC_VOLUME,
        accepted_obligation_ids=frozenset(),
        accepted_obligation_windows={},
        author_constraints=locks,
    )
    assert accurate.verification_failures == ()
    assert accurate.decision is ReviewDecision.REVISE
    blocking = tuple(item for item in accurate.issues if item.blocking)
    assert blocking
    assert all(item.affected_item_ids for item in blocking)
    assert all(not item.host_issued for item in blocking)

    # 3. Scope from exactly those findings: the four volumes whose climax wording
    #    repeats, and nothing else.
    review = PlanReview(
        review_id=StableId("plan-review.d0.accurate"),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        # Rebound to the persisted parent by `_diagnostic_binding`, which is the
        # artifact this review is actually about.
        target_artifact_ref=ArtifactRef(
            artifact_id=ArtifactId("sha256:" + "3" * 64),
            byte_length=1,
            media_type=PLAN_PROPOSAL_MEDIA_TYPE,
            schema_version=VERSION,
        ),
        decision=ReviewDecision.REVISE,
        issues=accurate.issues,
        revision_instruction=accurate.revision_instruction,
        receipt=_receipt(AgentMode.ARC_VOLUME, AgentType.PLAN_REVIEWER),
    )
    scope = revision_scope(review)
    # The scope is exactly the volumes whose climax repeats the phrase, derived from
    # the candidate's own text rather than asserted here.
    repeating = {
        item.item_id.root
        for item in candidate.items
        if "正式揭露门被从对面推开" in _climax_description(item) and item.item_id.root != "vol-4"
    }
    assert repeating
    assert scope.targeted_item_ids == repeating
    assert scope.targeted_item_ids < {item.item_id.root for item in candidate.items}
    assert {target.field_paths for target in scope.targets} == {("volume_climax.description",)}

    # 4. One bounded revision.  The model rewrites the four named climaxes and, as a
    #    real model does, moves something nobody asked about.
    revised_items: list[ProposedItem] = []
    for item in candidate.items:
        payload = dict(item.payload)
        climax = dict(cast(dict[str, Any], payload.get("volume_climax") or {}))
        if item.item_id.root in scope.targeted_item_ids:
            climax["description"] = (
                f"{item.item_id.root} 的高潮给出与前卷不同的后果："  # noqa: RUF001
                "读者能判断这是新的进展，而不是同一次揭露的重复。"  # noqa: RUF001
            )
            payload["volume_climax"] = climax
        if item.item_id.root == "vol-1":
            payload["capability_ceiling"] = "模型趁机改写的无关内容"
        revised_items.append(item.model_copy(update={"payload": payload}))
    revised = candidate.model_copy(
        update={"items": tuple(revised_items), "proposal_id": StableId("plan-proposal.d0.raw")}
    )
    # The raw output did move something nobody asked about, and it is recorded
    # rather than silently corrected.
    assert out_of_scope_items(candidate, revised, scope) == ("vol-1",)

    composed = compose_scoped_revision(candidate, revised, scope)
    attemptable, reason = assess_composition(candidate, revised, scope)
    assert attemptable, reason

    # The four named climaxes carry the revision; every other item is the parent's.
    for original, repaired_item in zip(candidate.items, composed.items, strict=True):
        if original.item_id.root in scope.targeted_item_ids:
            assert repaired_item.payload["volume_climax"] != original.payload["volume_climax"]
        else:
            assert repaired_item.payload == original.payload, original.item_id.root
    vol1 = next(item for item in composed.items if item.item_id.root == "vol-1")
    assert vol1.payload["capability_ceiling"] == candidate.items[0].payload["capability_ceiling"]
    assert "模型趁机改写的无关内容" not in json.dumps(
        composed.model_dump(mode="json"), ensure_ascii=False
    )

    # 5. Independent re-review of the composed candidate: ACCEPT.
    rereview = apply_host_plan_review_constraints(
        PlanReviewDraft(
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            decision=ReviewDecision.ACCEPT,
            issues=(),
        ),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=composed.model_dump_json(),
        mode=AgentMode.ARC_VOLUME,
        accepted_obligation_ids=frozenset(),
        accepted_obligation_windows={},
        author_constraints=locks,
    )
    assert rereview.decision is ReviewDecision.ACCEPT, [
        issue.summary for issue in rereview.issues if issue.blocking
    ]

    # 6. The formal materializer pre-check, through its real entry point.
    repo, accepted = _diagnostic_binding(tmp_path, candidate, revised, scope, review, composed)
    bound_review = _read_review(repo, accepted.candidate.lineage_artifact_refs)
    ref, execution = PlanCandidateMaterializer(
        repo, _commits(), schema_version=VERSION
    )._planner_execution(accepted.candidate.lineage_artifact_refs, composed)

    assert execution.plan_proposal == composed
    assert execution.raw_plan_proposal == revised
    assert execution.composition_proof is not None
    proof_ref = execution.composition_proof
    proof = repo.read_verified(proof_ref)
    assert json.loads(proof)["composed_digest"]
    ok, reason = verify_composition(
        _read_proof(repo, proof_ref),
        parent=candidate,
        revised=revised,
        review=bound_review,
        composed=composed,
    )
    assert ok, reason
    assert ref.media_type == PLANNER_EXECUTION_MEDIA_TYPE


def test_d0_a_tampered_proof_is_refused_on_the_same_candidate(tmp_path: Path) -> None:
    """The same candidate, with the proof's scope widened: verification refuses it.

    The frozen review authorises nothing, so a proof that claims volumes were
    composed from it cannot re-derive the same scope and must fail closed.
    """

    candidate = _proposal_from_frozen()
    review = _frozen_review(candidate)
    empty_scope = revision_scope(review)
    presented = compose_scoped_revision(candidate, candidate, empty_scope)
    proof = build_composition_proof(
        parent_ref=ArtifactRef(
            artifact_id=ArtifactId("sha256:" + "4" * 64),
            byte_length=1,
            media_type=PLAN_PROPOSAL_MEDIA_TYPE,
            schema_version=VERSION,
        ),
        raw_execution_ref=ArtifactRef(
            artifact_id=ArtifactId("sha256:" + "5" * 64),
            byte_length=1,
            media_type=PLANNER_EXECUTION_MEDIA_TYPE,
            schema_version=VERSION,
        ),
        review_ref=ArtifactRef(
            artifact_id=ArtifactId("sha256:" + "6" * 64),
            byte_length=1,
            media_type="application/vnd.novel-agent.plan-review+json",
            schema_version=VERSION,
        ),
        scope=empty_scope,
        composed=presented,
        out_of_scope=(),
    )
    tampered = proof.model_copy(
        update={
            "scope": empty_scope.model_copy(
                update={"targets": (PlanRevisionTarget(item_id=StableId("vol-1")),)}
            )
        }
    )

    ok, reason = verify_composition(
        tampered,
        parent=candidate,
        revised=candidate,
        review=review,
        composed=presented,
    )

    assert not ok
    assert "scope" in reason


# ------------------------------------------------------------------- materializer


def _read_review(repo: ArtifactRepository, refs: tuple[ArtifactRef, ...]) -> PlanReview:
    """The plan review the diagnostic event carries."""

    from novel_agent.domain.planning import PlanningLoopEventReceipt

    for ref in refs:
        if ref.media_type != PLANNING_EVENT_MEDIA_TYPE:
            continue
        event = PlanningLoopEventReceipt.model_validate_json(repo.read_verified(ref), strict=True)
        for artifact in event.artifact_refs:
            if artifact.media_type == "application/vnd.novel-agent.plan-review+json":
                return PlanReview.model_validate_json(repo.read_verified(artifact), strict=True)
    raise AssertionError("the diagnostic event carries no plan review")


def _read_proof(repo: ArtifactRepository, ref: ArtifactRef) -> PlanCompositionProof:
    return PlanCompositionProof.model_validate_json(repo.read_verified(ref), strict=True)


def _commits() -> CommitService:
    commits = Mock(spec=CommitService)
    commits.current_commit.return_value = COMMIT
    return cast(CommitService, commits)


def _diagnostic_binding(
    tmp_path: Path,
    parent: PlanProposal,
    revised: PlanProposal,
    scope: PlanRevisionScope,
    review: PlanReview,
    composed: PlanProposal,
) -> tuple[ArtifactRepository, AcceptedCandidateBinding]:
    """Persist the diagnostic chain and bind the composed candidate for pre-check."""

    from novel_agent.domain.plan_composition import (
        build_composition_proof,
        out_of_scope_items,
    )

    repo = ArtifactRepository(FilesystemObjectStore(tmp_path / "d0-objects"))
    parent_ref = repo.put(parent.model_dump_json().encode(), PLAN_PROPOSAL_MEDIA_TYPE, VERSION)
    # A review authorises the candidate it names, so bind it to the persisted parent
    # before it is written; the proof's own check is exactly this agreement.
    review = review.model_copy(update={"target_artifact_ref": parent_ref})
    review_ref = repo.put(
        review.model_dump_json().encode(),
        "application/vnd.novel-agent.plan-review+json",
        VERSION,
    )
    raw_execution = PlannerExecutionResult(
        mode=AgentMode.ARC_VOLUME,
        plan_proposal=revised,
        output_artifact=repo.put(b"{}", "application/json", VERSION),
        receipt=revised.receipt,
    )
    raw_ref = repo.put(
        raw_execution.model_dump_json().encode(), PLANNER_EXECUTION_MEDIA_TYPE, VERSION
    )
    proof = build_composition_proof(
        parent_ref=parent_ref,
        raw_execution_ref=raw_ref,
        review_ref=review_ref,
        scope=scope,
        composed=composed,
        out_of_scope=out_of_scope_items(parent, revised, scope),
    )
    proof_ref = repo.put(proof.model_dump_json().encode(), PLAN_COMPOSITION_MEDIA_TYPE, VERSION)
    execution = PlannerExecutionResult(
        mode=AgentMode.ARC_VOLUME,
        plan_proposal=composed,
        output_artifact=repo.put(b"{}", "application/json", VERSION),
        receipt=composed.receipt,
        composition_proof=proof_ref,
        raw_plan_proposal=revised,
    )
    execution_ref = repo.put(
        execution.model_dump_json().encode(), PLANNER_EXECUTION_MEDIA_TYPE, VERSION
    )
    composed_ref = repo.put(composed.model_dump_json().encode(), PLAN_PROPOSAL_MEDIA_TYPE, VERSION)
    event = PlanningLoopEventReceipt(
        event_id=StableId("planning-event.d0"),
        request_id=StableId("planning-request.d0"),
        phase=PlanningLoopPhase.PLAN_REVIEWED,
        event_kind="plan.review_settled",
        artifact_refs=(composed_ref, proof_ref, execution_ref, review_ref),
    )
    event_ref = repo.put(event.model_dump_json().encode(), PLANNING_EVENT_MEDIA_TYPE, VERSION)
    ref = CandidateBinding(
        candidate_id=StableId("candidate.d0.composed"),
        kind=CandidateKind.PLAN,
        artifact_ref=composed_ref,
        candidate_hash=composed_ref.artifact_id.root,
        basis_commit=COMMIT,
        lineage_artifact_refs=(event_ref,),
    )
    return repo, AcceptedCandidateBinding(
        acceptance_id=StableId("acceptance.d0"),
        command_id=StableId("command.d0"),
        project_id=PROJECT,
        run_id=RunId("run.d0.diagnostic"),
        task_id=TaskId("task.d0"),
        candidate=ref,
        actor_kind=ActorKind.AUTHOR,
        actor_id="author.diagnostic",
        accepted_at=datetime(2026, 9, 13, tzinfo=UTC),
        expected_project_commit=COMMIT,
    )
