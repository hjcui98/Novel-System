from __future__ import annotations

from novel_agent.domain.artifacts import PlanRootRef
from novel_agent.domain.ids import ArtifactId, ProjectId, SchemaVersion, StableId
from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile
from novel_agent.services.memory_benchmark_contract import (
    build_public_checkpoint_case,
    profile_namespace,
)


def _plan_ref() -> PlanRootRef:
    return PlanRootRef(
        artifact_id=ArtifactId("sha256:" + "a" * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )


def test_visible_profile_strips_plan_while_author_profile_keeps_bootstrap_ref() -> None:
    visible = build_public_checkpoint_case(
        case_id=StableId("ZTJ-P001"),
        project_id=ProjectId("project.profile"),
        history_range=(1, 20),
        target_range=(21, 25),
        plan_root_ref=_plan_ref(),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    planned = build_public_checkpoint_case(
        case_id=StableId("ZTJ-P001"),
        project_id=ProjectId("project.profile"),
        history_range=(1, 20),
        target_range=(21, 25),
        plan_root_ref=_plan_ref(),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )

    assert visible.plan_root_ref is None
    assert planned.plan_root_ref == _plan_ref()
    assert visible.public_input_hash != planned.public_input_hash
    assert "不得使用截止点之后" in visible.task_contract.task_text
    assert "作者粗粒度计划" in planned.task_contract.task_text


def test_profile_namespaces_are_disjoint() -> None:
    project = ProjectId("project.profile")
    visible = profile_namespace(
        project, BenchmarkInformationProfile.VISIBLE_AT_CUTOFF, "experiment"
    )
    planned = profile_namespace(
        project, BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED, "experiment"
    )
    assert visible != planned
