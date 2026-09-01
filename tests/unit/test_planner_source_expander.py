"""U4-L0: Memory Planner P1 previews fail closed on cutoff and stale refs."""

from __future__ import annotations

from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.planner_source_expander import PlannerSourceExpander
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def test_p1_resolves_cutoff_safe_l0_and_excludes_future_or_stale_refs() -> None:
    bundle = make_synthetic_bundle()
    history, _future = bundle.text_roots
    world = bundle.world_roots[0]
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    expansion = PlannerSourceExpander().expand(
        task=task,
        world=world,
        text=history,
        snapshot_id=StableId("snapshot.p1"),
    )
    assert expansion.context_level == "P1"
    assert expansion.resolved_count >= 1
    assert any("旧誓言" in preview.text for preview in expansion.previews)
    assert "P1" in expansion.prompt_block()

    future_task = task.model_copy(update={"checkpoint_chapter": 4})
    cutoff = PlannerSourceExpander().expand(
        task=future_task,
        world=world,
        text=history,
        snapshot_id=StableId("snapshot.p1-cutoff"),
    )
    assert cutoff.resolved_count == 0
    assert cutoff.cutoff_excluded_count >= 1
    assert cutoff.prompt_block() == ""

    original_commit = world.source_commit
    historical = PlannerSourceExpander().expand(
        task=task,
        world=world,
        text=history,
        snapshot_id=StableId("snapshot.p1-historical"),
    )
    assert historical.resolved_count >= 1

    stale_world = world.model_copy(update={"source_commit": CommitId("sha256:" + "9" * 64)})
    stale = PlannerSourceExpander().expand(
        task=task,
        world=stale_world,
        text=history,
        snapshot_id=StableId("snapshot.p1-stale"),
        request_commit=original_commit,
    )
    assert stale.resolved_count == 0
    assert stale.stale_count >= 1
    assert stale.prompt_block() == ""


def test_p1_only_dereferences_the_p0_selected_record_sequence() -> None:
    bundle = make_synthetic_bundle()
    history, _future = bundle.text_roots
    world = bundle.world_roots[0]
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    selected = (next(event for event in world.events if event.evidence_refs).event_id,)

    expansion = PlannerSourceExpander().expand(
        task=task,
        world=world,
        text=history,
        snapshot_id=StableId("snapshot.p1-selected"),
        selected_record_ids=selected,
    )

    assert expansion.selected_record_ids == selected
    assert all(preview.record_id == selected[0] for preview in expansion.previews)
