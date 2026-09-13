"""The plan-revision stop condition: only a *repeated* problem is no progress.

The v17 planner re-emitted the same proposal against the same finding set, so the
loop needs a stall guard.  The guard must not fire on a legacy ``REVISE`` that
carries a prose instruction instead of structured findings, and it must not treat a
partially repaired finding set as unchanged.  Its basis has to survive a work-slice
boundary, or every resumed slice would start the same revision again.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import StableId
from novel_agent.domain.model_calls import ModelCallRecord
from novel_agent.domain.planning import (
    PlanningLoopCheckpoint,
    PlanningLoopPhase,
    PlanningLoopResult,
    PlanningLoopTerminal,
    PlanReview,
    PlanReviewIssue,
    ReviewDecision,
    ReviewIssueKind,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import AgentMode, AgentType
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.planning_context_loop import (
    PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
    PlanningContextLoopService,
)
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.unit.test_stage4_planning_contracts import VERSION, _put, _receipt, _request
from tests.unit.test_stage4_planning_loop_and_evaluation import (
    _AcceptingReviewer,
    _model_request,
    _ModePlanner,
    _NoProgressPlanner,
    _post_genesis_service,
)


def _issue(index: int, summary: str) -> PlanReviewIssue:
    return PlanReviewIssue(
        issue_id=StableId(f"review-issue.{index}.{abs(hash(summary)) % 10_000}"),
        kind=ReviewIssueKind.VOLUME_STRUCTURE_INCOMPLETE,
        summary=summary,
        blocking=True,
        affected_item_ids=(StableId("plan-item.chapter_set.1"),),
    )


class _FindingsReviewer(_AcceptingReviewer):
    """Return one scripted blocking finding set per plan review, then accept."""

    def __init__(
        self,
        artifacts: ArtifactRepository,
        findings: list[tuple[PlanReviewIssue, ...]],
    ) -> None:
        super().__init__(artifacts)
        self._findings = findings
        self.plan_reviews = 0

    async def review(self, **kwargs: object) -> tuple[PlanReview, ArtifactRef, ModelCallRecord]:
        target_kind = cast(ReviewTargetKind, kwargs["target_kind"])
        target = cast(ArtifactRef, kwargs["target_artifact"])
        mode = cast(AgentMode, kwargs["mode"])
        issues: tuple[PlanReviewIssue, ...] = ()
        if target_kind is ReviewTargetKind.PLAN_PROPOSAL:
            index = self.plan_reviews
            issues = self._findings[index] if index < len(self._findings) else ()
            self.plan_reviews += 1
        decision = ReviewDecision.REVISE if issues else ReviewDecision.ACCEPT
        review = PlanReview(
            review_id=StableId(f"review.findings.{target_kind.value}.{self.plan_reviews}"),
            target_kind=target_kind,
            target_artifact_ref=target,
            decision=decision,
            issues=issues,
            revision_instruction="bounded repair" if decision is ReviewDecision.REVISE else None,
            receipt=_receipt(mode, AgentType.PLAN_REVIEWER),
        )
        ref = self._artifacts.put(review.model_dump_json().encode(), "application/json", VERSION)
        return review, ref, cast(ModelCallRecord, object())


def _run_to_terminal(
    service: PlanningContextLoopService,
    artifacts: ArtifactRepository,
    request: object,
    *,
    world: object,
    text_root: object,
    slices: int = 6,
) -> PlanningLoopTerminal:
    """Walk the loop across work slices, resuming from the last checkpoint."""

    checkpoint: ArtifactRef | None = None
    terminal: PlanningLoopTerminal | None = None
    for _ in range(slices):
        result = asyncio.run(
            service.run(
                request=request,
                model_request=_model_request,
                world=world,  # type: ignore[arg-type]
                text_root=text_root,  # type: ignore[arg-type]
                resume_checkpoint_ref=checkpoint,
            )
        )
        terminal = result.terminal
        if terminal is not PlanningLoopTerminal.YIELDED:
            return terminal
        checkpoint = next(
            ref for ref in reversed(result.event_artifacts) if "checkpoint" in ref.media_type
        )
    raise AssertionError(f"loop did not settle after {slices} slices: {terminal}")


def _service(
    tmp_path: Path,
    name: str,
    findings: list[tuple[PlanReviewIssue, ...]],
    *,
    planner: type[_ModePlanner] = _ModePlanner,
) -> tuple[PlanningContextLoopService, ArtifactRepository, object, object, object, _ModePlanner]:
    bundle = make_synthetic_bundle()
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / name))
    source = _put(artifacts, "author source")
    roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    request = _request(AgentMode.CHAPTER_SET, source, accepted=(roots[0], roots[1], roots[2]))
    selected = planner(artifacts, AgentMode.CHAPTER_SET)
    service, _planner, _memory = _post_genesis_service(
        artifacts,
        reviewer=_FindingsReviewer(artifacts, findings),
        planner=selected,
    )
    return (
        service,
        artifacts,
        request,
        bundle.world_roots[0],
        bundle.text_roots[0],
        selected,
    )


def test_an_empty_finding_set_never_stops_the_revision(tmp_path: Path) -> None:
    """A legacy REVISE with no structured findings is judged by content change."""

    service, artifacts, request, world, text_root, _planner = _service(
        tmp_path,
        "empty-signature",
        [(), ()],
    )

    terminal = _run_to_terminal(service, artifacts, request, world=world, text_root=text_root)

    assert terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY


def test_a_partially_repaired_finding_set_keeps_revising(tmp_path: Path) -> None:
    first = _issue(1, "vol_04.midpoint_reversal.window 越过 350")
    second = _issue(2, "vol_04.ending_state.role 缺失")
    service, artifacts, request, world, text_root, _planner = _service(
        tmp_path,
        "partial-repair",
        [(first, second), (second,)],
    )

    terminal = _run_to_terminal(service, artifacts, request, world=world, text_root=text_root)

    assert terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY


def test_identical_findings_stop_the_revision(tmp_path: Path) -> None:
    """The candidate changed, but the host-visible problem did not."""

    finding = _issue(1, "vol_04.midpoint_reversal.window 越过 350")
    service, artifacts, request, world, text_root, _planner = _service(
        tmp_path,
        "repeated-findings",
        [(finding,), (finding,)],
    )

    terminal = _run_to_terminal(service, artifacts, request, world=world, text_root=text_root)

    assert terminal is PlanningLoopTerminal.REVIEW_REVISION_REQUIRED


def test_an_unchanged_candidate_stops_the_revision(tmp_path: Path) -> None:
    """Only the proposal id changed, which is not progress on its own."""

    finding = _issue(1, "vol_04.midpoint_reversal.window 越过 350")
    service, artifacts, request, world, text_root, _planner = _service(
        tmp_path,
        "unchanged-candidate",
        [(finding,)],
        planner=_NoProgressPlanner,
    )

    terminal = _run_to_terminal(service, artifacts, request, world=world, text_root=text_root)

    assert terminal is PlanningLoopTerminal.REVIEW_REVISION_REQUIRED


def test_the_progress_basis_survives_a_work_slice_boundary(tmp_path: Path) -> None:
    """A restarted worker does not pay for a problem the last slice already rejected.

    Resuming from a pre-review frontier re-runs the review.  The basis recorded by the
    previous slice is what turns that repeat into an immediate stop; without it the
    loop revises the same rejected problem one more time.
    """

    finding = _issue(1, "vol_04.midpoint_reversal.window 越过 350")
    service, artifacts, request, world, text_root, planner = _service(
        tmp_path,
        "slice-basis",
        [(finding,)] * 5,
    )

    first = asyncio.run(
        service.run(
            request=request,  # type: ignore[arg-type]
            model_request=_model_request,
            world=world,  # type: ignore[arg-type]
            text_root=text_root,  # type: ignore[arg-type]
        )
    )
    assert first.terminal is PlanningLoopTerminal.REVIEW_REVISION_REQUIRED
    assert first.diagnostic_codes == ("PLAN_REVISION_NO_PROGRESS",)
    assert planner.plan_calls == 2
    checkpoint_ref = next(
        ref for ref in reversed(first.event_artifacts) if "checkpoint" in ref.media_type
    )
    checkpoint = PlanningLoopCheckpoint.model_validate_json(artifacts.read_verified(checkpoint_ref))
    assert checkpoint.plan_blocking_signature

    # A worker that restarts before it re-reviews resumes from a pre-review frontier.
    before_review = checkpoint.model_copy(
        update={"phase": PlanningLoopPhase.PLAN_PROPOSED, "plan_review_ref": None}
    )

    def resume_from(frontier: PlanningLoopCheckpoint) -> object:
        ref = artifacts.put(
            canonical_json_bytes(frontier.model_dump(mode="json")),
            PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
            VERSION,
        )
        return asyncio.run(
            service.run(
                request=request,  # type: ignore[arg-type]
                model_request=_model_request,
                world=world,  # type: ignore[arg-type]
                text_root=text_root,  # type: ignore[arg-type]
                resume_checkpoint_ref=ref,
            )
        )

    # The recorded basis recognises the repeat, so the problem buys no new revision.
    with_basis = cast("PlanningLoopResult", resume_from(before_review))
    assert with_basis.terminal is PlanningLoopTerminal.REVIEW_REVISION_REQUIRED
    assert with_basis.diagnostic_codes == ("PLAN_REVISION_NO_PROGRESS",)
    assert planner.plan_calls == 2

    # Without the recorded basis the same frontier pays for another revision before
    # it can recognise the repeat.
    without_basis = before_review.model_copy(update={"plan_blocking_signature": ()})
    repeated = cast("PlanningLoopResult", resume_from(without_basis))
    assert repeated.terminal is PlanningLoopTerminal.REVIEW_REVISION_REQUIRED
    assert planner.plan_calls == 3
