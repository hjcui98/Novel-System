from __future__ import annotations

from novel_agent.domain.benchmark import ChapterGoal, PlanRootDocument
from novel_agent.domain.generation import WritingLengthPolicy, WritingTaskContract
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    NeedGapPolicy,
    ObligationHistoryNeedKind,
    ObligationHistoryNeedReason,
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    RequirementLevel,
    WorldRootDocument,
    classify_obligation_history_need,
    setup_min_distinct_history_chapters,
)
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
)
from novel_agent.services.task_conditioned_need_generation import (
    NeedGenerationStatus,
    TaskPlanConditionedNeedGenerator,
)

HASH = ArtifactId("sha256:" + "1" * 64)
COMMIT = CommitId("sha256:" + "2" * 64)
VERSION = SchemaVersion("1.0.0")


def _obligation(
    *,
    status: ObligationStatus = ObligationStatus.OPEN,
    evidence_chapter: bool = False,
    not_before: int | None = None,
) -> PlanObligation:
    evidence = ()
    if evidence_chapter:
        evidence = (
            EvidenceRef(
                evidence_id=StableId("evidence.obligation.prior"),
                root_hash=HASH,
                object_hash=HASH,
                chapter_id=StableId("chapter.1"),
                support_status=EvidenceSupportStatus.HISTORICAL,
                resolved_at_commit=COMMIT,
            ),
        )
    elif evidence_chapter is False and status is ObligationStatus.OPEN:
        evidence = ()
    return PlanObligation(
        obligation_id=StableId("obligation.tower"),
        kind=ObligationKind.OBJECTIVE,
        description="进入北塔",
        status=status,
        not_before_chapter=not_before,
        evidence_refs=evidence,
    )


def _baseline_evidence_obligation() -> PlanObligation:
    return PlanObligation(
        obligation_id=StableId("obligation.tower"),
        kind=ObligationKind.OBJECTIVE,
        description="进入北塔",
        status=ObligationStatus.OPEN,
        evidence_refs=(
            EvidenceRef(
                evidence_id=StableId("evidence.obligation.baseline"),
                root_hash=HASH,
                object_hash=HASH,
                support_status=EvidenceSupportStatus.CURRENT,
                resolved_at_commit=COMMIT,
            ),
        ),
    )


def test_classifier_requires_obligation_facts_for_mandatory_progress_retrieval() -> None:
    open_obligation = _obligation()
    progressed = _obligation(status=ObligationStatus.PROGRESSED)
    chapter_bound = _obligation(evidence_chapter=True)
    baseline = _baseline_evidence_obligation()

    assert (
        classify_obligation_history_need(
            obligation=open_obligation,
            action="PROGRESS",
            target_chapter=2,
            committed_frontier=1,
            prior_prose_facts=True,
        ).kind
        is ObligationHistoryNeedKind.OPTIONAL
    )
    assert (
        classify_obligation_history_need(
            obligation=open_obligation,
            action="PROGRESS",
            target_chapter=2,
            committed_frontier=1,
            prior_prose_facts=True,
        ).reason_code
        is ObligationHistoryNeedReason.PROGRESS_AUXILIARY_RECALL
    )
    assert (
        classify_obligation_history_need(
            obligation=baseline,
            action="SETUP",
            target_chapter=2,
            committed_frontier=1,
            prior_prose_facts=True,
        ).kind
        is ObligationHistoryNeedKind.OPTIONAL
    )
    assert (
        classify_obligation_history_need(
            obligation=chapter_bound,
            action="SETUP",
            target_chapter=2,
            committed_frontier=1,
            prior_prose_facts=True,
        ).kind
        is ObligationHistoryNeedKind.MANDATORY
    )
    assert (
        classify_obligation_history_need(
            obligation=progressed,
            action="SETUP",
            target_chapter=2,
            committed_frontier=1,
            prior_prose_facts=True,
        ).kind
        is ObligationHistoryNeedKind.MANDATORY
    )
    assert (
        classify_obligation_history_need(
            obligation=progressed,
            action="PROGRESS",
            target_chapter=2,
            committed_frontier=1,
            prior_prose_facts=True,
        ).kind
        is ObligationHistoryNeedKind.MANDATORY
    )
    assert (
        classify_obligation_history_need(
            obligation=baseline,
            action="PROGRESS",
            target_chapter=2,
            committed_frontier=1,
            prior_prose_facts=True,
        ).kind
        is ObligationHistoryNeedKind.OPTIONAL
    )
    assert (
        classify_obligation_history_need(
            obligation=open_obligation,
            action="PAYOFF",
            target_chapter=2,
            committed_frontier=1,
            prior_prose_facts=True,
        ).reason_code
        is ObligationHistoryNeedReason.PAYOFF_AUXILIARY_RECALL
    )


def test_classifier_returns_none_without_committed_history_or_on_future_plans() -> None:
    obligation = _obligation()
    future = _obligation(not_before=300)
    assert (
        classify_obligation_history_need(
            obligation=obligation,
            action="SETUP",
            target_chapter=1,
            committed_frontier=0,
            prior_prose_facts=False,
        ).reason_code
        is ObligationHistoryNeedReason.FIRST_CHAPTER
    )
    assert (
        classify_obligation_history_need(
            obligation=obligation,
            action="PROGRESS",
            target_chapter=2,
            committed_frontier=0,
            prior_prose_facts=False,
        ).reason_code
        is ObligationHistoryNeedReason.NO_COMMITTED_HISTORY
    )
    assert (
        classify_obligation_history_need(
            obligation=future,
            action="SETUP",
            target_chapter=21,
            committed_frontier=20,
            prior_prose_facts=True,
        ).reason_code
        is ObligationHistoryNeedReason.FUTURE_PLAN
    )
    assert (
        classify_obligation_history_need(
            obligation=future,
            action="PROGRESS",
            target_chapter=21,
            committed_frontier=20,
            prior_prose_facts=True,
        ).reason_code
        is ObligationHistoryNeedReason.PROGRESS_AUXILIARY_RECALL
    )
    assert (
        classify_obligation_history_need(
            obligation=future,
            action="PAYOFF",
            target_chapter=21,
            committed_frontier=20,
            prior_prose_facts=True,
        ).reason_code
        is ObligationHistoryNeedReason.FUTURE_PLAN
    )


def test_setup_history_chapter_requirement_grows_with_available_history() -> None:
    assert setup_min_distinct_history_chapters(1) == 1
    assert setup_min_distinct_history_chapters(2) == 1
    assert setup_min_distinct_history_chapters(3) == 2
    assert setup_min_distinct_history_chapters(21) == 2


def _writing_task(chapter: int) -> WritingTaskContract:
    return WritingTaskContract(
        contract_id=StableId(f"writing-contract.history.{chapter}"),
        target_chapter=chapter,
        target_scenes=(StableId(f"scene.chapter.{chapter}.0"),),
        pov="third_person_limited",
        narrative_person="third",
        chapter_goal="Enter the tower.",
        length_policy=WritingLengthPolicy(
            minimum_characters=100,
            target_characters=200,
            maximum_characters=400,
        ),
    )


def _task(chapter: int, *, checkpoint: int | None = None) -> BenchmarkTaskContract:
    return BenchmarkTaskContract(
        task_id=StableId(f"task.history.{chapter}"),
        task_text=f"Write chapter {chapter}.",
        checkpoint_chapter=chapter - 1 if checkpoint is None else checkpoint,
        target_chapter_start=chapter,
        target_chapter_end=chapter,
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        task_template_version="production-writing-task.v1",
        output_contract_version="writer_context.v2",
        task_intent="Enter the tower.",
    )


def _plan(chapter: int, *, action: str = "PROGRESS") -> PlanRootDocument:
    return PlanRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        chapter_goals=(
            ChapterGoal(
                goal_id=StableId(f"plan.chapter.{chapter}"),
                chapter_index=chapter,
                summary="Enter the tower.",
                obligation_ids=(StableId("obligation.tower"),),
                payload={
                    "obligation_actions": [
                        {
                            "obligation_id": "obligation.tower",
                            "action": action,
                        }
                    ],
                    "history_retrieval": {
                        "requirement": "NOT_REQUIRED",
                        "reason_code": "no_historical_dependency",
                        "waiver_ref": "waiver.review.history",
                    },
                },
            ),
        ),
    )


def _world(obligation: PlanObligation) -> WorldRootDocument:
    return WorldRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        source_commit=COMMIT,
        obligations=(obligation,),
    )


def _first_chapter_plan() -> PlanRootDocument:
    return PlanRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        chapter_goals=(
            ChapterGoal(
                goal_id=StableId("plan.chapter.1"),
                chapter_index=1,
                summary="Enter the tower.",
                obligation_ids=(StableId("obligation.tower"),),
                payload={
                    "obligation_actions": [
                        {"obligation_id": "obligation.tower", "action": "SETUP"}
                    ],
                    "history_retrieval": {
                        "requirement": "NOT_REQUIRED",
                        "reason_code": "first_chapter",
                        "waiver_ref": "waiver.history.first_chapter",
                    },
                },
            ),
        ),
    )


def test_first_chapter_uses_the_real_waiver_and_creates_no_history_need() -> None:
    result = TaskPlanConditionedNeedGenerator().generate_for_writing_task(
        _task(1),
        _writing_task(1),
        _world(_obligation()),
        _first_chapter_plan(),
        None,
    )
    assert result.status is NeedGenerationStatus.READY
    assert result.needs == ()
    assert result.history_waiver_ref == "waiver.history.first_chapter"


def test_chapter_two_setup_need_requires_one_history_chapter() -> None:
    result = TaskPlanConditionedNeedGenerator().generate_for_writing_task(
        _task(2),
        _writing_task(2),
        _world(_obligation(status=ObligationStatus.PROGRESSED)),
        _plan(2, action="SETUP"),
        None,
    )
    assert result.needs
    spec = result.needs[0].completion_spec
    assert spec is not None
    assert spec.min_distinct_chapters == 1
    assert result.needs[0].requirement is RequirementLevel.MANDATORY
    assert result.needs[0].query_text
    assert result.needs[0].query_text != result.needs[0].need_id.root
    assert result.needs[0].need_id.root.startswith("need.production.history.")
    assert result.needs[0].completion_spec is not None
    need = result.needs[0]
    assert need.completion_spec.min_distinct_chapters == 1
    assert need.need_type == "setup_evidence"


def test_chapter_three_setup_may_require_two_history_chapters() -> None:
    result = TaskPlanConditionedNeedGenerator().generate_for_writing_task(
        _task(3),
        _writing_task(3),
        _world(_obligation(status=ObligationStatus.PROGRESSED)),
        _plan(3, action="SETUP"),
        None,
    )
    spec = result.needs[0].completion_spec
    assert spec is not None
    assert spec.min_distinct_chapters == 2
    assert spec.gap_policy is NeedGapPolicy.FAIL_MANDATORY


def test_insufficient_history_emits_a_typed_gap_instead_of_complete() -> None:
    result = TaskPlanConditionedNeedGenerator().generate_for_writing_task(
        _task(3, checkpoint=1),
        _writing_task(3),
        _world(_obligation(status=ObligationStatus.PROGRESSED)),
        _plan(3, action="SETUP"),
        None,
    )
    spec = result.needs[0].completion_spec
    assert spec is not None
    assert spec.min_distinct_chapters == 2
    assert spec.gap_policy is NeedGapPolicy.EMIT_TYPED_GAP


def test_host_need_source_chapter_end_precedes_the_target() -> None:
    result = TaskPlanConditionedNeedGenerator().generate_for_writing_task(
        _task(2),
        _writing_task(2),
        _world(_obligation()),
        _plan(2, action="PROGRESS"),
        None,
    )
    assert result.needs
    assert result.needs[0].requirement is RequirementLevel.OPTIONAL
    assert "进入北塔" in result.needs[0].query_text
    assert "progress_auxiliary_recall" in result.needs[0].why_needed
    assert result.needs[0].completion_spec is not None
    assert result.needs[0].completion_spec.min_distinct_chapters == 1


def test_baseline_evidence_refs_do_not_force_mandatory_setup_retrieval() -> None:
    result = TaskPlanConditionedNeedGenerator().generate_for_writing_task(
        _task(2),
        _writing_task(2),
        _world(_baseline_evidence_obligation()),
        _plan(2, action="SETUP"),
        None,
    )
    if result.needs:
        assert result.needs[0].requirement is RequirementLevel.OPTIONAL
