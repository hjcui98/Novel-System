from __future__ import annotations

from pathlib import Path

from novel_agent.agents.planner import planner_skill_ids_for_mode
from novel_agent.domain.generation import WritingLengthPolicy, WritingTaskContract
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.production_assembly import ProductionAssemblySpec, ProductionModelPolicy
from novel_agent.domain.stage2 import AgentMode
from novel_agent.services.editorial import (
    _ADMITTED_EDITOR_LENSES,
    _PLAN_REVIEWER_LENSES,
    _editor_lens_instructions,
    _selected_editor_lenses,
)
from novel_agent.skills.registry import SkillRegistry, SkillTemplate


def test_writer_selects_from_metadata_and_turn_loads_only_selected_bodies(
    tmp_path: Path,
) -> None:
    skill_path = tmp_path / "scene.md"
    skill_path.write_text("FULL BODY MUST NOT APPEAR IN WORK PLAN", encoding="utf-8")
    from novel_agent.prompts.registry import content_hash

    digest = content_hash(skill_path.read_bytes())
    registry = SkillRegistry(
        (
            SkillTemplate(
                StableId("skill.scene-composition"),
                SchemaVersion("1.0.0"),
                skill_path,
                digest,
                summary="Compose the current scene.",
                tags=("writer", "scene"),
                applicable_modes=("draft",),
            ),
        )
    )
    card = registry.describe(StableId("skill.scene-composition"), SchemaVersion("1.0.0"))
    assert "ID: skill.scene-composition" in card
    assert "Compose the current scene." in card
    assert "FULL BODY MUST NOT APPEAR IN WORK PLAN" not in card
    body, _ref = registry.resolve(StableId("skill.scene-composition"), SchemaVersion("1.0.0"))
    assert "FULL BODY MUST NOT APPEAR IN WORK PLAN" in body
    spec = ProductionAssemblySpec(
        spec_version=SchemaVersion("1.0.0"),
        factory_locator="novel_agent.runtime.creative_assembly:build_production_assembly",
        runtime_contract_version=SchemaVersion("1.0.0"),
        expected_migration_head="0010_model_call_ledger",
        expected_planner_adapter="planner",
        expected_writer_adapter="writer",
        expected_plan_materializer="plan",
        expected_draft_materializer="draft",
        expected_chapter_settlement="settle",
        expected_memory_maintenance="maintain",
        model_policy=ProductionModelPolicy(
            require_admission=True,
            sequence_limit=1024,
            default_output_limit=128,
            reasoning_billing_mode="unknown_not_applicable",
        ),
        expected_prompt_ids=(StableId("prompt.system-policy"),),
        expected_skill_ids=(StableId("skill.scene-composition"),),
        writer_skill_ids=(StableId("skill.scene-composition"),),
        planner_skill_ids=(StableId("skill.planning-inquiry"),),
    )
    assert spec.skills_for_writer() != spec.skills_for_planner()
    chapter_set_skills = planner_skill_ids_for_mode(AgentMode.CHAPTER_SET)
    story_skills = planner_skill_ids_for_mode(AgentMode.STORY)
    assert StableId("skill.planner.chapter_set") in chapter_set_skills
    assert StableId("skill.planner.story") in story_skills
    assert StableId("skill.scene-composition") not in chapter_set_skills


def test_review_paths_use_admitted_lenses_only() -> None:
    writing_task = WritingTaskContract(
        contract_id=StableId("writing-contract.lenses"),
        target_chapter=24,
        target_scenes=(StableId("scene.24"),),
        pov="Lin",
        narrative_person="third person limited",
        chapter_goal="Investigate.",
        forbidden_reveals=("不得兑现银铭",),
        length_policy=WritingLengthPolicy(
            minimum_characters=20,
            target_characters=40,
            maximum_characters=60,
        ),
    )
    review_input = type("Review", (), {"writing_task": writing_task})()
    lenses = _selected_editor_lenses(review_input, draft_length=10)  # type: ignore[arg-type]
    assert set(lenses) <= set(_ADMITTED_EDITOR_LENSES)
    assert len(lenses) <= 3
    assert StableId("skill.editor.chapter-length") in lenses
    assert StableId("skill.editor.plan-adherence-hook-payoff") in lenses
    instructions = _editor_lens_instructions(lenses)
    assert "trusted WritingTask length contract" in instructions
    assert "future-locked obligations" in instructions
    assert "skill.plan-review.temporal-obligation" not in instructions
    assert "skill.plan-review.parent-scope" not in instructions
    assert set(_PLAN_REVIEWER_LENSES).isdisjoint(set(_ADMITTED_EDITOR_LENSES))
    assert set(_PLAN_REVIEWER_LENSES).isdisjoint(set(lenses))
