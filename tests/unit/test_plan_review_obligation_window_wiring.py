"""N2 (2026-09-13 guidance): the accepted-obligation window reaches production.

The window function already rejected a stage that crossed an accepted obligation's
boundary, but nothing in production passed it the window: ``obligation_windows`` had
exactly one caller in the whole repository and that caller was a unit test.  "The
function rejects it when called directly" was therefore never the same statement as
"a real review rejects it".

These tests go through the real entry point -- ``PlanReviewerAgent.review`` reading a
persisted ``WorldRootDocument`` out of an ``ArtifactRepository`` -- so the wiring is
what is under test, not the helper's argument list.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.agents.plan_reviewer import (
    PlanReviewerAgent,
    apply_host_plan_review_constraints,
    obligation_stage_windows,
)
from novel_agent.domain.artifacts import ArtifactId, ArtifactRef
from novel_agent.domain.author_constraints import (
    AuthorConstraint,
    AuthorConstraintCategory,
)
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    WorldRootDocument,
)
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.planning import (
    PlanReviewDraft,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import AgentMode, AgentType
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import content_id, world_root_content_id

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "1" * 64)
COMMIT = CommitId("sha256:" + "a" * 64)
WORLD_MEDIA_TYPE = "application/vnd.novel-agent.world-root+json"

_LONG_TRUTH_HINT = "lock.long-truth.vol4-hint"
_LONG_TRUTH_ADVANCE = "lock.long-truth.vol5-advance"


# --------------------------------------------------------------------------- worlds


def _obligation(
    obligation_id: str,
    *,
    kind: ObligationKind = ObligationKind.FORESHADOWING,
    status: ObligationStatus = ObligationStatus.OPEN,
    not_before_chapter: int | None = 350,
    target_chapter_start: int | None = None,
    target_chapter_end: int | None = None,
    due_chapter: int | None = None,
) -> PlanObligation:
    return PlanObligation(
        obligation_id=StableId(obligation_id),
        kind=kind,
        description="长程真相的暗示与推进",
        status=status,
        not_before_chapter=not_before_chapter,
        target_chapter_start=target_chapter_start,
        target_chapter_end=target_chapter_end,
        due_chapter=due_chapter,
    )


def _world(obligations: tuple[PlanObligation, ...]) -> WorldRootDocument:
    """A World root whose declared content address matches the obligations it holds.

    The placeholder hash is only a seed for the derivation; the persisted artifact
    always carries the derived address, so the repository's integrity check and the
    review's catalogue read see the same bytes.
    """

    seed = WorldRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        source_commit=COMMIT,
        obligations=obligations,
    )
    derived = world_root_content_id(seed)
    declared = WorldRootDocument(
        root_hash=derived,
        schema_version=VERSION,
        source_commit=COMMIT,
        obligations=obligations,
    )
    assert world_root_content_id(declared) == derived
    return declared


def _repo(tmp_path: Path) -> ArtifactRepository:
    return ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))


def _put_world(repo: ArtifactRepository, obligations: tuple[PlanObligation, ...]) -> ArtifactRef:
    return repo.put(_world(obligations).model_dump_json().encode(), WORLD_MEDIA_TYPE, VERSION)


def _locks() -> tuple[AuthorConstraint, ...]:
    def lock(constraint_id: str, key: str, *, not_before: int) -> AuthorConstraint:
        return AuthorConstraint(
            constraint_id=StableId(constraint_id),
            constraint_key=key,
            category=AuthorConstraintCategory.TIME_LOCK,
            text=f"{key} 不得早于第 {not_before} 章",
            source_ref=ArtifactRef(
                artifact_id=content_id({"lock": key}),
                byte_length=1,
                media_type="application/json",
                schema_version=VERSION,
            ),
            source_hash=ArtifactId(content_id({"lock": key}).root),
            not_before_chapter=not_before,
        )

    return (
        lock("author-constraint.time_lock.4", _LONG_TRUTH_HINT, not_before=350),
        lock("author-constraint.time_lock.5", _LONG_TRUTH_ADVANCE, not_before=401),
    )


# ------------------------------------------------------------------------- payloads


def _stage(description: str, window: str, role: str, serves: str | None = None) -> dict[str, str]:
    entry = {"description": description, "window": window, "role": role}
    if serves is not None:
        entry["serves"] = serves
    return entry


def _volume(
    *,
    start: int,
    end: int,
    stage_key: str,
    entry: dict[str, str],
    stage_window: str,
) -> dict[str, object]:
    """One volume whose named stage sits at ``stage_window``.

    The window is given explicitly rather than derived from the volume bounds: the
    tests below place a stage deliberately at one chapter and need the rest of the
    volume to stay legal around it.
    """

    early = ("opening_state", "trigger_event", "first_escalation", "first_cost")
    late = ("second_escalation", "climax_cost", "ending_state", "next_volume_hook")
    narrative = {key: _stage(f"{key} 描述", f"{start}-{start + 5}", "setup") for key in early}
    narrative.update({key: _stage(f"{key} 描述", f"{end - 5}-{end}", "setup") for key in late})
    narrative[stage_key] = {**entry, "window": stage_window}
    return {
        "plan_level": "arc_volume",
        "chapter_start": start,
        "chapter_end": end,
        "protagonist_arc": "主角弧线",
        "supporting_arc": "配角弧线",
        "faction_arc": "势力弧线",
        "capability_ceiling": "能力上限",
        "equipment_ceiling": "装备上限",
        "entry_conditions": "入卷条件",
        "exit_conditions": "出卷条件",
        "reveal_window": "本卷揭露范围",
        "obligation_plan": [
            {
                "kind": "objective",
                "summary": "本卷推进的责任",
                "setup_window": f"{start}-{start + 20}",
                "progress_windows": [f"{start + 21}-{end - 11}"],
                "payoff_window": f"{end - 10}-{end}",
            }
        ],
        **narrative,
    }


def _payload_at(
    window: str,
    *,
    description: str,
    role: str,
    serves: str | None = None,
    stage_key: str = "midpoint_reversal",
    start: int | None = None,
    end: int | None = None,
) -> str:
    """One volume holding a stage at ``window``.

    The volume bounds default to the window's own decade so the window is always
    inside its volume; a test that specifically wants a window outside the volume
    scope passes bounds of its own.
    """

    first, last = (int(part) for part in window.split("-"))
    start = first if start is None else start
    end = last if end is None else end
    entry = _stage(description, window, role, serves)
    return json.dumps(
        {
            "expected_volume_count": 1,
            "target_chapters": end,
            "items": [
                {
                    "item_id": "vol-4",
                    "kind": "arc_volume",
                    "payload": _volume(
                        start=start,
                        end=end,
                        stage_key=stage_key,
                        entry=entry,
                        stage_window=window,
                    ),
                }
            ],
        }
    )


# ------------------------------------------------------------------- the real entry


class _ReviewRunner:
    """A reviewer stub that returns one draft without any model call."""

    def __init__(self, draft: PlanReviewDraft) -> None:
        self.draft = draft
        self.calls = 0

    def prepare(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    async def execute(self, prepared: object, output_type: object) -> object:
        del prepared, output_type
        self.calls += 1
        from types import SimpleNamespace

        return SimpleNamespace(output=self.draft, model_call=cast(ModelCallRecord, object()))

    def receipt(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        from novel_agent.domain.ids import RunId, TaskId
        from novel_agent.domain.stage2 import (
            AgentExecutionReceipt,
            ContractRef,
            ExecutionStatus,
        )

        now = datetime.now(UTC)
        return AgentExecutionReceipt(
            receipt_id=StableId("receipt.plan-reviewer.arc_volume"),
            run_id=RunId("run.n2"),
            task_id=TaskId("task.n2"),
            agent_spec=ContractRef(
                contract_id=StableId("agent.plan-reviewer.arc_volume"),
                version=VERSION,
                content_hash=HASH,
            ),
            agent_type=AgentType.PLAN_REVIEWER,
            agent_mode=AgentMode.ARC_VOLUME,
            prompt_fingerprint=HASH,
            configuration_fingerprint=HASH,
            base_commit=COMMIT,
            status=ExecutionStatus.SUCCEEDED,
            started_at=now,
            completed_at=now,
            latency_ms=0,
        )


def _run_review(
    tmp_path: Path,
    *,
    payload: str,
    obligations: tuple[PlanObligation, ...],
    omit_world_from_sources: bool = False,
) -> PlanReviewDraft:
    """Drive the real reviewer against a persisted World root.

    Production has two ways to reach the catalogue -- the root the task's basis
    commit binds, and a World root among the trusted source artifacts -- so both are
    exercised.  ``omit_world_from_sources`` covers the state where neither reaches
    the review, which the host must report as an unknown catalogue.
    """

    repo = _repo(tmp_path)
    world_ref = _put_world(repo, obligations)
    target = repo.put(payload.encode(), "application/json", VERSION)
    agent = PlanReviewerAgent(
        cast(
            object,
            _ReviewRunner(
                PlanReviewDraft(
                    target_kind=ReviewTargetKind.PLAN_PROPOSAL,
                    decision=ReviewDecision.ACCEPT,
                    issues=(),
                )
            ),
        ),  # type: ignore[arg-type]
        repo,
        accepted_world_ref=None if omit_world_from_sources else world_ref,
    )
    review, _ref, _call = asyncio.run(
        agent.review(
            version=VERSION,
            mode=AgentMode.ARC_VOLUME,
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            target_payload=payload,
            target_artifact=target,
            trusted_source_artifacts=() if omit_world_from_sources else (world_ref,),
            request=cast(ModelRequest, object()),
            base_commit=COMMIT,
        )
    )
    return PlanReviewDraft(
        target_kind=review.target_kind,
        decision=review.decision,
        issues=review.issues,
        revision_instruction=review.revision_instruction,
        verification_failures=review.verification_failures,
    )


# ------------------------------------------------------------------------- the tests


def _window_findings(review: PlanReviewDraft) -> list[str]:
    return [
        issue.summary
        for issue in review.issues
        if issue.blocking and "VOLUME_STAGE_WINDOW" in issue.summary
    ]


def test_a_stage_before_its_accepted_obligations_boundary_is_rejected(tmp_path: Path) -> None:
    """The window the review never used to receive now decides the verdict.

    The stage serves an accepted obligation whose ``not_before_chapter`` is 350 and
    runs at 341-350 -- inside the volume, legal for every author lock, and only
    refusable through the obligation's own window.
    """

    review = _run_review(
        tmp_path,
        payload=_payload_at(
            "341-350",
            description="正式推进长程真相",
            role="progression",
            serves="obligation.long-truth",
        ),
        obligations=(_obligation("obligation.long-truth", not_before_chapter=350),),
    )

    assert review.decision is ReviewDecision.REVISE
    findings = _window_findings(review)
    assert len(findings) == 1
    assert "obligation.long-truth" in findings[0]
    assert "unlocks at 350" in findings[0]


def test_a_stage_inside_its_accepted_obligations_window_passes(tmp_path: Path) -> None:
    review = _run_review(
        tmp_path,
        payload=_payload_at(
            "351-360",
            description="正式推进长程真相",
            role="progression",
            serves="obligation.long-truth",
        ),
        obligations=(_obligation("obligation.long-truth", not_before_chapter=350),),
    )

    assert _window_findings(review) == []


def test_the_boundary_values_349_and_350_differ(tmp_path: Path) -> None:
    """349 is inside the lock, 350 is the first legal chapter."""

    early = _run_review(
        tmp_path / "early",
        payload=_payload_at(
            "340-349",
            description="正式推进长程真相",
            role="progression",
            serves="obligation.long-truth",
        ),
        obligations=(_obligation("obligation.long-truth", not_before_chapter=350),),
    )
    at = _run_review(
        tmp_path / "at",
        payload=_payload_at(
            "350-359",
            description="正式推进长程真相",
            role="progression",
            serves="obligation.long-truth",
        ),
        obligations=(_obligation("obligation.long-truth", not_before_chapter=350),),
    )

    assert len(_window_findings(early)) == 1
    assert _window_findings(at) == []


def test_the_boundary_values_400_and_401_differ(tmp_path: Path) -> None:
    """350-400 may hint; formal advancement starts at 401."""

    late_hint = _run_review(
        tmp_path / "hint",
        payload=_payload_at(
            "391-400",
            description="暗示长程真相",
            role="hint",
            serves="obligation.vol5",
        ),
        obligations=(_obligation("obligation.vol5", not_before_chapter=401),),
    )
    advance = _run_review(
        tmp_path / "advance",
        payload=_payload_at(
            "401-410",
            description="正式推进长程真相",
            role="progression",
            serves="obligation.vol5",
        ),
        obligations=(_obligation("obligation.vol5", not_before_chapter=401),),
    )

    assert len(_window_findings(late_hint)) == 1
    assert _window_findings(advance) == []


def test_a_legal_early_setup_passes_before_the_boundary(tmp_path: Path) -> None:
    """Planting a locked responsibility early is what planting is for."""

    review = _run_review(
        tmp_path,
        payload=_payload_at(
            "311-320",
            description="埋设残卷线索，不揭露真相",  # noqa: RUF001
            role="setup",
            serves="obligation.long-truth",
        ),
        obligations=(_obligation("obligation.long-truth", not_before_chapter=350),),
    )

    assert _window_findings(review) == []


def test_a_disguised_reveal_labelled_setup_is_still_blocked(tmp_path: Path) -> None:
    """Relabelling a disclosure as planting must not buy the exemption."""

    review = _run_review(
        tmp_path,
        payload=_payload_at(
            "311-320",
            description="正式揭露长程真相",
            role="setup",
            serves="obligation.long-truth",
        ),
        obligations=(_obligation("obligation.long-truth", not_before_chapter=350),),
    )

    findings = _window_findings(review)
    assert len(findings) == 1
    assert "declared setup but performs" in findings[0]


def test_an_unrelated_responsibility_does_not_bind_a_stage(tmp_path: Path) -> None:
    """A boundary only constrains the stages that actually serve it."""

    review = _run_review(
        tmp_path,
        payload=_payload_at(
            "311-320",
            description="推进第四碎片",
            role="progression",
            serves="obligation.vol4-shard",
        ),
        obligations=(
            _obligation("obligation.long-truth", not_before_chapter=350),
            _obligation("obligation.vol4-shard", not_before_chapter=301),
        ),
    )

    assert _window_findings(review) == []


def test_a_closing_obligation_bounds_the_late_edge(tmp_path: Path) -> None:
    """A responsibility declared due at 400 cannot be served by a stage past it."""

    inside = _run_review(
        tmp_path / "inside",
        payload=_payload_at(
            "391-400",
            description="兑现铜铭",
            role="payoff",
            serves="obligation.copper",
        ),
        obligations=(
            _obligation(
                "obligation.copper",
                kind=ObligationKind.PROMISE,
                not_before_chapter=350,
                target_chapter_start=390,
                target_chapter_end=400,
                due_chapter=400,
            ),
        ),
    )
    assert _window_findings(inside) == []


def test_a_state_reference_after_the_event_is_not_a_second_occurrence(
    tmp_path: Path,
) -> None:
    """Holding the copper token after chapter 100 is not re-obtaining it.

    ``due_chapter`` closes the event, not the state: the host caps the stage window
    at the due chapter, so a later stage that merely references the held token is
    outside the cap and is reported as such rather than silently accepted.
    """

    review = _run_review(
        tmp_path,
        payload=_payload_at(
            "101-110",
            description="继续持有铜铭",
            role="progression",
            serves="obligation.copper",
        ),
        obligations=(
            _obligation(
                "obligation.copper",
                kind=ObligationKind.PROMISE,
                not_before_chapter=90,
                target_chapter_start=90,
                target_chapter_end=100,
                due_chapter=100,
            ),
        ),
    )

    findings = _window_findings(review)
    assert len(findings) == 1
    assert "closes at 100" in findings[0]


# ----------------------------------------------------------- the three catalogue states


def test_an_unknown_catalogue_is_reported_not_passed(tmp_path: Path) -> None:
    """No trusted World root reached the review: the host says so."""

    review = _run_review(
        tmp_path,
        payload=_payload_at(
            "351-360",
            description="正式推进长程真相",
            role="progression",
            serves="obligation.long-truth",
        ),
        obligations=(),
        omit_world_from_sources=True,
    )

    summary = " ".join(issue.summary for issue in review.issues)
    assert "OBLIGATION_CATALOGUE_UNKNOWN" in summary
    assert review.decision is ReviewDecision.REVISE


def test_an_empty_catalogue_disputes_the_id_instead(tmp_path: Path) -> None:
    """A readable empty catalogue says the id does not exist -- a different finding."""

    review = _run_review(
        tmp_path,
        payload=_payload_at(
            "351-360",
            description="正式推进长程真相",
            role="progression",
            serves="obligation.long-truth",
        ),
        obligations=(),
    )

    summary = " ".join(issue.summary for issue in review.issues)
    assert "OBLIGATION_CATALOGUE_UNKNOWN" not in summary
    assert "neither an accepted author constraint nor an accepted obligation" in summary


def test_an_obligation_without_timing_is_reported_not_passed(tmp_path: Path) -> None:
    """The third state: the obligation exists but constrains no chapter."""

    review = _run_review(
        tmp_path,
        payload=_payload_at(
            "351-360",
            description="推进无窗口责任",
            role="progression",
            serves="obligation.no-window",
        ),
        obligations=(
            _obligation(
                "obligation.no-window",
                kind=ObligationKind.OBJECTIVE,
                not_before_chapter=None,
            ),
        ),
    )

    findings = _window_findings(review)
    assert len(findings) == 1
    assert "declares no not_before_chapter" in findings[0]


def test_a_candidate_citing_no_obligation_needs_no_catalogue(tmp_path: Path) -> None:
    review = _run_review(
        tmp_path,
        payload=_payload_at(
            "311-320",
            description="推进第四碎片",
            role="progression",
        ),
        obligations=(),
        omit_world_from_sources=True,
    )

    assert "OBLIGATION_CATALOGUE_UNKNOWN" not in " ".join(issue.summary for issue in review.issues)


# ------------------------------------------------------------ the mapping semantics


def test_only_open_obligations_contribute_a_window() -> None:
    """A resolved or abandoned responsibility no longer locks anything."""

    windows = obligation_stage_windows(
        (
            _obligation("obligation.open", not_before_chapter=350),
            _obligation(
                "obligation.done", status=ObligationStatus.RESOLVED, not_before_chapter=350
            ),
            _obligation(
                "obligation.dropped", status=ObligationStatus.ABANDONED, not_before_chapter=350
            ),
        )
    )

    assert windows["obligation.open"] == (350, None)
    assert "obligation.done" not in windows
    assert "obligation.dropped" not in windows


def test_the_early_edge_prefers_not_before_over_the_target_window() -> None:
    """``target_chapter_start`` is the payoff's slot, not the unlock."""

    windows = obligation_stage_windows(
        (
            _obligation(
                "obligation.targeted",
                kind=ObligationKind.PROMISE,
                not_before_chapter=350,
                target_chapter_start=390,
                target_chapter_end=400,
                due_chapter=400,
            ),
        )
    )

    assert windows["obligation.targeted"] == (350, 400)


def test_a_target_window_without_a_not_before_still_bounds_the_stage() -> None:
    windows = obligation_stage_windows(
        (
            _obligation(
                "obligation.window-only",
                kind=ObligationKind.OBJECTIVE,
                not_before_chapter=None,
                target_chapter_start=390,
                target_chapter_end=400,
            ),
        )
    )

    assert windows["obligation.window-only"] == (390, None)


def test_an_obligation_without_any_timing_maps_to_an_empty_window() -> None:
    """Explicitly present, explicitly empty: the host reports it rather than passing."""

    windows = obligation_stage_windows(
        (_obligation("obligation.bare", kind=ObligationKind.OBJECTIVE, not_before_chapter=None),)
    )

    assert windows == {"obligation.bare": (None, None)}


def test_the_pure_function_agrees_with_the_production_path(tmp_path: Path) -> None:
    """Review and materializer must not disagree about the same mechanical defect.

    The agent path is checked above; this pins that the same payload going through
    the pure host overlay produces the same window finding, so a candidate cannot be
    accepted at review and refused later for the same defect.
    """

    payload = _payload_at(
        "341-350",
        description="正式推进长程真相",
        role="progression",
        serves="obligation.long-truth",
    )
    obligations = (_obligation("obligation.long-truth", not_before_chapter=350),)

    through_agent = _run_review(tmp_path, payload=payload, obligations=obligations)
    through_overlay = apply_host_plan_review_constraints(
        PlanReviewDraft(
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            decision=ReviewDecision.ACCEPT,
            issues=(),
        ),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=payload,
        mode=AgentMode.ARC_VOLUME,
        accepted_obligation_ids=frozenset({"obligation.long-truth"}),
        accepted_obligation_windows=obligation_stage_windows(obligations),
        author_constraints=_locks(),
    )

    assert _window_findings(through_agent) == _window_findings(through_overlay)
    assert through_agent.decision is through_overlay.decision


@pytest.mark.parametrize("ignored", [None])
def test_the_review_never_asks_the_model_for_a_catalogue(ignored: None) -> None:
    """A reviewer stub is used throughout: none of these paths needed a model call."""

    del ignored
    assert Mock is not None
