"""Regression coverage for Writer fidelity to an accepted V2 chapter execution.

The Writer factory used to flatten an accepted chapter into a summary plus a
few string beats.  These cases drive the real production factory over a V2
chapter goal and assert the structured plan actually reaches the Writer
contract, that the WorkPlan coverage gate refuses a dropped beat, and that a
planned participant never becomes a Canon entity claim.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from novel_agent.adapters.runtime.stage3_writer import (
    ProductionWritingRequestFactory,
    _volume_stage_constraints,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import ChapterGoal
from novel_agent.domain.generation import (
    WriterSceneExecutionBeat,
    WriterWorkPlan,
    WritingLengthPolicy,
    WritingTaskContract,
)
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.plan_detail import (
    ExecutionBeatBlueprint,
    SceneBlueprint,
    validate_execution_allocation,
)
from novel_agent.domain.world import PlanLevel, PlanNode
from novel_agent.services.writer_cognition import (
    WriterCognitionError,
    validate_work_plan_execution,
)
from tests.unit.test_stage5_production_factories import (
    VERSION,
)

CHAPTER = 21


def _beat(beat_id: str, *, budget: int = 400) -> ExecutionBeatBlueprint:
    return ExecutionBeatBlueprint(
        beat_id=StableId(beat_id),
        parent_beat_refs=(StableId("beat.parent.conflict"),),
        action="他开始行动。",
        resistance="对手挡住了第一步。",
        choice="他选择让出先手换取时间。",
        outcome="他失去了先机但保住了目标。",
        information_revealed="读者第一次看到他的真实上限。",
        prose_focus="动作与代价",
        budget_characters=budget,
        close_point="他停手并接受代价。",
    )


def _scene() -> SceneBlueprint:
    return SceneBlueprint(
        scene_id=StableId("scene.chapter.21.1"),
        narrative_task="建立冲突并暴露上限。",
        location="塔外",
        pov="林澈",
        participants=("林澈",),
        entry_condition="他刚刚抵达塔外。",
        exit_condition="他接受了代价。",
        beats=(_beat("beat.chapter.21.1"), _beat("beat.chapter.21.2", budget=500)),
        budget_characters=900,
    )


def _v2_payload() -> dict[str, object]:
    scene = _scene()
    return {
        "contract_version": "chapter.v2",
        "detail_level": "execution",
        "chapter_index": CHAPTER,
        "summary": "林澈在塔外接受代价，暴露了自己的上限。",  # noqa: RUF001
        "narrative_function": "把候选身份推进到实际参与安排。",
        "expected_exit_change": "他放弃了先手。",
        "next_chapter_interface": "把代价带到下一章的谈判里。",
        "cast": [
            {
                "reference_kind": "canon",
                "entity_id": "entity.lin-che",
                "narrative_role": "本章视角人物",
            },
            {
                "reference_kind": "planned",
                "planned_ref": "planned.entity.vanguard",
                "label": "先遣队",
                "narrative_role": "本窗口准备引入的行动组织",
                "introduction_ref": "intro.vanguard_membership",
            },
        ],
        "planned_introductions": [
            {
                "introduction_id": "intro.vanguard_membership",
                "statement": "先遣队的参与安排在本章第一次被正面提出。",
                "introduced_in_chapter": CHAPTER,
                "depends_from_chapters": [],
            }
        ],
        "planned_introduction_refs": ["intro.vanguard_membership"],
        "scenes": [scene.model_dump(mode="json")],
        "budget_characters": 900,
    }


def _v2_goal() -> ChapterGoal:
    return ChapterGoal(
        # The synthetic accepted root already owns this chapter's goal identity.
        goal_id=StableId("goal.synthetic.21"),
        chapter_index=CHAPTER,
        summary="林澈在塔外接受代价，暴露了自己的上限。",  # noqa: RUF001
        payload=_v2_payload(),
    )


def _compiled(goal: ChapterGoal | None = None) -> object:
    """Compile one accepted chapter goal through the production factory path."""

    return ProductionWritingRequestFactory._compile_chapter_execution(
        (goal if goal is not None else _v2_goal(),),
        chapter_index=CHAPTER,
    )


def _writing_task(
    *,
    scene_blueprints: tuple[SceneBlueprint, ...] = (),
    planned_participants: tuple[object, ...] = (),
    planned_introductions: tuple[object, ...] = (),
) -> WritingTaskContract:
    return WritingTaskContract(
        contract_id=StableId("writing-contract.v2"),
        target_chapter=CHAPTER,
        target_scenes=(StableId("scene.chapter.21.0"),),
        pov="Lin",
        narrative_person="third person limited",
        chapter_goal="V2 chapter.",
        scene_goals=("scene",),
        required_beats=("beat",),
        length_policy=WritingLengthPolicy(
            minimum_characters=500,
            target_characters=1_500,
            maximum_characters=3_000,
        ),
        scene_blueprints=scene_blueprints,
        planned_participants=planned_participants,
        planned_introductions=planned_introductions,
    )


def test_v2_execution_reaches_the_writer_contract_with_stable_ids() -> None:
    """W01: structure and IDs survive compilation into the Writer contract."""

    compiled = _compiled()
    task_contract = _writing_task(
        scene_blueprints=compiled.scene_blueprints,
        planned_participants=compiled.planned_participants,
        planned_introductions=compiled.planned_introductions,
    )

    assert [scene.scene_id.root for scene in task_contract.scene_blueprints] == [
        "scene.chapter.21.1"
    ]
    assert [item.root for item in task_contract.required_execution_beat_ids()] == [
        "beat.chapter.21.1",
        "beat.chapter.21.2",
    ]
    # The scene beat structure is visible, not flattened into a summary.
    serialized = task_contract.model_dump(mode="json")
    beats = serialized["scene_blueprints"][0]["beats"]
    assert beats[0]["beat_id"] == "beat.chapter.21.1"
    assert beats[0]["close_point"] == "他停手并接受代价。"
    assert beats[1]["budget_characters"] == 500
    assert "scene.chapter.21.1" in compiled.scene_goals[0]
    assert "beat.chapter.21.1" in compiled.required_beats[0]
    assert "阻力" in compiled.required_beats[0]


def test_planned_participant_is_not_a_canon_entity_claim() -> None:
    """W05: a planned participant stays a plan identity, not a World fact."""

    compiled = _compiled()

    planned = list(compiled.planned_participants)
    assert [item.planned_ref.root for item in planned] == ["planned.entity.vanguard"]
    assert planned[0].label == "先遣队"
    assert planned[0].entity_id is None
    assert [item.introduction_id.root for item in compiled.planned_introductions] == [
        "intro.vanguard_membership"
    ]

    task_contract = _writing_task(
        scene_blueprints=compiled.scene_blueprints,
        planned_participants=compiled.planned_participants,
        planned_introductions=compiled.planned_introductions,
    )
    # A plan-local reference never enters the Canon entity list.
    assert all(
        not item.root.startswith("planned.") for item in task_contract.participating_entity_ids
    )


def test_writer_contract_refuses_a_canon_participant_in_the_planned_list() -> None:
    """W05/W06: the planned list is a separate, plan-only channel."""

    from novel_agent.domain.plan_detail import PlannedIntroduction, PlanParticipant

    with pytest.raises(ValidationError, match="cannot claim a Canon entity"):
        _writing_task(
            scene_blueprints=_compiled().scene_blueprints,
            planned_participants=(
                PlanParticipant(
                    reference_kind="canon",
                    entity_id=StableId("entity.synthetic.lin-che"),
                    narrative_role="视角人物",
                ),
            ),
            planned_introductions=(
                PlannedIntroduction(
                    introduction_id=StableId("intro.vanguard_membership"),
                    statement="先遣队的参与安排在本章第一次被正面提出。",
                    introduced_in_chapter=CHAPTER,
                ),
            ),
        )


def test_writer_contract_requires_a_declared_introduction() -> None:
    from novel_agent.domain.plan_detail import PlannedIntroduction, PlanParticipant

    with pytest.raises(ValidationError, match="must name an introduction"):
        _writing_task(
            scene_blueprints=_compiled().scene_blueprints,
            planned_participants=(
                PlanParticipant(
                    reference_kind="planned",
                    planned_ref=StableId("planned.entity.other"),
                    label="别的组织",
                    narrative_role="本窗口准备引入",
                    introduction_ref=StableId("intro.never.declared"),
                ),
            ),
            planned_introductions=(
                PlannedIntroduction(
                    introduction_id=StableId("intro.vanguard_membership"),
                    statement="本窗口确实声明的一项引入。",
                    introduced_in_chapter=CHAPTER,
                ),
            ),
        )


def test_outline_only_chapter_cannot_reach_the_writer() -> None:
    """H10: an outline chapter is not writable before its execution refinement."""

    payload = _v2_payload()
    payload["detail_level"] = "outline"
    payload.pop("scenes")
    goal = ChapterGoal(
        goal_id=StableId("goal.synthetic.21"),
        chapter_index=CHAPTER,
        summary="Outline only.",
        payload=payload,
    )

    with pytest.raises(ValueError, match="refined to execution"):
        _compiled(goal)


def test_v2_payload_for_another_chapter_is_refused() -> None:
    payload = _v2_payload()
    payload["chapter_index"] = CHAPTER + 1
    goal = ChapterGoal(
        goal_id=StableId("goal.synthetic.22"),
        chapter_index=CHAPTER + 1,
        summary="Wrong chapter.",
        payload=payload,
    )

    with pytest.raises(ValueError, match="targets a different chapter"):
        _compiled(goal)


def test_work_plan_must_cover_every_required_beat() -> None:
    """W02: a dropped accepted beat is refused before the Writer is called."""

    compiled = _compiled()
    writing_task = _writing_task(scene_blueprints=compiled.scene_blueprints)
    half = 1_000

    def plan(beat_refs: tuple[str, ...]) -> WriterWorkPlan:
        beats = tuple(
            WriterSceneExecutionBeat(
                beat_ref=ref,
                scene_expansion="expanded",
                resistance="r",
                choice="c",
                outcome="o",
                expected_characters=half,
                close_point="x",
            )
            for ref in beat_refs
        )
        return WriterWorkPlan(
            work_plan_id=StableId("work-plan.v2"),
            writing_task_ref=_artifact_ref("1"),
            accepted_plan_ref=_artifact_ref("2"),
            writer_context_ref=_artifact_ref("3"),
            scene_beat_order=tuple(beat_refs),
            pov_boundary="bounded",
            reader_disclosure_boundary="bounded",
            selected_skill_ids=(StableId("skill.scene-composition"),),
            execution_beats=beats,
            expected_total_characters=half * len(beats),
        )

    validate_work_plan_execution(plan(("beat.chapter.21.1", "beat.chapter.21.2")), writing_task)

    with pytest.raises(WriterCognitionError, match="uncovered"):
        validate_work_plan_execution(plan(("beat.chapter.21.1",)), writing_task)

    with pytest.raises(WriterCognitionError, match="unknown accepted execution beat"):
        validate_work_plan_execution(
            plan(("beat.chapter.21.1", "beat.never.accepted")),
            writing_task,
        )


def test_work_plan_total_must_match_its_beat_allocation() -> None:
    compiled = _compiled()
    writing_task = _writing_task(scene_blueprints=compiled.scene_blueprints)
    total = 2_000
    work_plan = WriterWorkPlan(
        work_plan_id=StableId("work-plan.v2.mismatch"),
        writing_task_ref=_artifact_ref("1"),
        accepted_plan_ref=_artifact_ref("2"),
        writer_context_ref=_artifact_ref("3"),
        scene_beat_order=("beat.chapter.21.1", "beat.chapter.21.2"),
        pov_boundary="bounded",
        reader_disclosure_boundary="bounded",
        selected_skill_ids=(StableId("skill.scene-composition"),),
        execution_beats=(
            WriterSceneExecutionBeat(
                beat_ref="beat.chapter.21.1",
                scene_expansion="e",
                resistance="r",
                choice="c",
                outcome="o",
                expected_characters=total // 2,
                close_point="x",
            ),
            WriterSceneExecutionBeat(
                beat_ref="beat.chapter.21.2",
                scene_expansion="e",
                resistance="r",
                choice="c",
                outcome="o",
                expected_characters=total // 2,
                close_point="x",
            ),
        ),
        expected_total_characters=total + 1,
    )

    with pytest.raises(WriterCognitionError, match="does not match its beat allocation"):
        validate_work_plan_execution(work_plan, writing_task)


def test_work_plan_total_outside_the_length_policy_is_refused() -> None:
    """W03: an impossible budget is a plan defect, not a truncation problem."""

    accepted = {"beat.1", "beat.2"}
    with pytest.raises(ValueError, match="incompatible with the trusted length policy"):
        validate_execution_allocation(
            accepted_beat_ids=accepted,
            required_beat_ids=accepted,
            executed_refs=("beat.1", "beat.2"),
            expected_characters=(100, 100),
            declared_total=200,
            minimum_characters=3_000,
            maximum_characters=4_000,
        )


def test_work_plan_without_a_declared_total_is_refused() -> None:
    compiled = _compiled()
    writing_task = _writing_task(scene_blueprints=compiled.scene_blueprints)
    work_plan = WriterWorkPlan(
        work_plan_id=StableId("work-plan.v2.no-total"),
        writing_task_ref=_artifact_ref("1"),
        accepted_plan_ref=_artifact_ref("2"),
        writer_context_ref=_artifact_ref("3"),
        scene_beat_order=("beat.chapter.21.1",),
        pov_boundary="bounded",
        reader_disclosure_boundary="bounded",
        selected_skill_ids=(StableId("skill.scene-composition"),),
        execution_beats=(
            WriterSceneExecutionBeat(
                beat_ref="beat.chapter.21.1",
                scene_expansion="e",
                resistance="r",
                choice="c",
                outcome="o",
                expected_characters=1_000,
                close_point="x",
            ),
        ),
    )

    with pytest.raises(WriterCognitionError, match="must declare its expected total"):
        validate_work_plan_execution(work_plan, writing_task)


def _artifact_ref(digit: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + digit * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=VERSION,
    )


def test_legacy_chapter_without_blueprint_skips_the_coverage_gate() -> None:
    """A V1 chapter keeps its string-beat contract and is not newly blocked."""

    from novel_agent.domain.generation import WritingTaskContract

    legacy = WritingTaskContract(
        contract_id=StableId("writing-contract.legacy"),
        target_chapter=3,
        target_scenes=(StableId("scene.chapter.3.0"),),
        pov="Lin",
        narrative_person="third",
        chapter_goal="legacy",
        scene_goals=("beat",),
        required_beats=("beat",),
        length_policy=WritingLengthPolicy(
            minimum_characters=1,
            maximum_characters=10_000,
            target_characters=3_000,
        ),
    )
    work_plan = WriterWorkPlan(
        work_plan_id=StableId("work-plan.legacy"),
        writing_task_ref=_artifact_ref("1"),
        accepted_plan_ref=_artifact_ref("2"),
        writer_context_ref=_artifact_ref("3"),
        scene_beat_order=("beat",),
        pov_boundary="bounded",
        reader_disclosure_boundary="bounded",
        selected_skill_ids=(StableId("skill.scene-composition"),),
    )

    validate_work_plan_execution(work_plan, legacy)


def test_volume_stage_position_is_not_fact_evidence() -> None:
    """W04: being past a volume's opening does not make its entry state true."""

    node = PlanNode(
        plan_node_id=StableId("plan.volume.stage"),
        node_type="arc_volume",
        title="Volume",
        summary="Stage grid.",
        plan_level=PlanLevel.ARC_VOLUME,
        chapter_start=1,
        chapter_end=9,
        payload={"entry_conditions": "必须已取得铜铭"},
    )

    middle = _volume_stage_constraints([node], 5)

    assert any("入口待核实" in item for item in middle)
    assert not any("入口已成立" in item for item in middle)
    assert any("计划要求而非既成事实" in item for item in middle)


def test_volume_exit_lock_still_binds_mid_volume() -> None:
    node = PlanNode(
        plan_node_id=StableId("plan.volume.exit"),
        node_type="arc_volume",
        title="Volume",
        summary="Stage grid.",
        plan_level=PlanLevel.ARC_VOLUME,
        chapter_start=1,
        chapter_end=9,
        payload={"exit_conditions": "内府资格已获得"},
    )

    middle = _volume_stage_constraints([node], 5)

    assert any("出口未到期" in item for item in middle)

    closing = _volume_stage_constraints([node], 9)
    assert "当前卷阶段[卷尾:exit_conditions]：内府资格已获得" in closing  # noqa: RUF001
