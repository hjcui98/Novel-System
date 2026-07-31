from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from novel_agent.domain.ids import ArtifactId, ProjectId
from novel_agent.domain.stage2 import BenchmarkInformationProfile
from novel_agent.services.benchmark_scenario_compiler import BenchmarkScenarioCompiler
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler

PILOT = Path(__file__).parents[2] / "benchmarks/private/ztj_memory_pilot_v0.1"


def test_scenario_compiler_rejects_empty_cross_project_and_duplicate_checkpoints() -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    compiler = BenchmarkScenarioCompiler()
    with pytest.raises(ValueError, match="requires checkpoint cases"):
        compiler.compile(
            bundle.model_copy(update={"case_manifests": ()}),
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        )
    with pytest.raises(ValueError, match="requires checkpoint cases"):
        compiler.independent_rebuild_report(bundle.model_copy(update={"case_manifests": ()}))

    first, second, *remaining = bundle.case_manifests
    cross_project = second.model_copy(update={"project_id": ProjectId("project.other")})
    with pytest.raises(ValueError, match="one project"):
        compiler.compile(
            bundle.model_copy(update={"case_manifests": (first, cross_project, *remaining)}),
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        )

    duplicate = second.model_copy(update={"history_range": first.history_range})
    with pytest.raises(ValueError, match="checkpoint chapters must be unique"):
        compiler.compile(
            bundle.model_copy(update={"case_manifests": (first, duplicate, *remaining)}),
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        )


def test_scenario_compiler_keeps_case_target_plans_private() -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    compiler = BenchmarkScenarioCompiler()
    first = bundle.case_manifests[0]
    no_plan = first.model_copy(update={"input_plan_root": None})
    no_plan_bundle = bundle.model_copy(
        update={"case_manifests": (no_plan, *bundle.case_manifests[1:])}
    )
    assert compiler.compile(
        no_plan_bundle,
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    assert compiler.compile(
        no_plan_bundle,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )

    missing = ArtifactId("sha256:" + "f" * 64)
    with pytest.raises(ValueError, match="missing TextRoot"):
        compiler._text_root(bundle, missing)


def test_scenario_compiler_rejects_inconsistent_independent_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    compiler = BenchmarkScenarioCompiler()
    monkeypatch.setattr(
        compiler,
        "independent_rebuild_report",
        lambda _: SimpleNamespace(all_consistent=False),
    )
    with pytest.raises(ValueError, match="independently consistent"):
        compiler.compile(bundle, BenchmarkInformationProfile.VISIBLE_AT_CUTOFF)
