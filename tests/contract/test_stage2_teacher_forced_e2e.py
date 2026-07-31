from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_agent.domain.ids import ProjectId, StableId
from novel_agent.domain.retrieval_routing import RetrievalBackendProfile
from novel_agent.domain.stage2 import BenchmarkInformationProfile, PublicCheckpointCase
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_benchmark_contract import build_public_checkpoint_case
from novel_agent.services.stage1_benchmark import Stage1NeedGenerator
from novel_agent.services.stage2_paired_pilot import Stage2PairedPilotRunner
from novel_agent.services.teacher_forced_benchmark_e2e import (
    TeacherForcedBenchmarkE2ERunner,
    TeacherForcedBenchmarkError,
)

ROOT = Path(__file__).parents[2]
PILOT = ROOT / "benchmarks/private/ztj_memory_pilot_v0.1"


def test_real_ztj_teacher_forced_flow_builds_genesis_and_five_frozen_cases(
    tmp_path: Path,
) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    output = tmp_path / "teacher-forced-e2e"

    summary = TeacherForcedBenchmarkE2ERunner(semantic_endpoint=None).run(
        PILOT,
        output,
        bundle,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )

    assert summary["status"] == "teacher_forced_contract_smoke_completed"
    assert summary["genesis_author_approved"] is True
    assert summary["teacher_forced_writer"] is True
    assert summary["chapter_commit_count"] == 96
    assert summary["curator_replay_agent_calls"] == 95
    assert summary["checkpoint_chapters"] == [20, 40, 60, 80, 95]
    assert summary["paired_results_count"] == 5
    # Scripted smoke does not carry trusted semantic support receipts for the
    # plan-conditioned mandatory facets.  It must now fail closed rather than
    # treating candidate presence as Writer-ready.
    assert summary["comparable_results_count"] == 0
    assert summary["future_isolation_failure_count"] == 0
    assert summary["future_leakage_count"] == 0
    assert summary["planner_agent_calls"] == 1
    assert summary["semantic_quality_eligible"] is False
    assert summary["generation_quality_eligible"] is False
    assert summary["retrieval_backend_profile"] == "scripted_smoke"
    assert summary["retrieval_quality_eligible"] is False
    assert summary["retrieval_attestation"]["capability"]["status"] == "test_only"
    assert (output / "project.sqlite3").is_file()
    assert (output / "scenario_run.json").is_file()
    scenario = json.loads((output / "scenario_run.json").read_text("utf-8"))
    checkpoint_plan_roots = {checkpoint["plan_root"] for checkpoint in scenario["checkpoints"]}
    assert len(checkpoint_plan_roots) == 1
    assert checkpoint_plan_roots.isdisjoint(
        {
            case.input_plan_root.root
            for case in bundle.case_manifests
            if case.input_plan_root is not None
        }
    )
    plan_hash = next(iter(checkpoint_plan_roots)).removeprefix("sha256:")
    plan_payload = json.loads(
        (output / "objects" / "sha256" / plan_hash[:2] / plan_hash).read_text("utf-8")
    )
    assert "plan.bootstrap.rough-story-outline.range.81-100" in {
        node["plan_node_id"] for node in plan_payload["nodes"]
    }
    report = json.loads((output / "e2e_paired_report.json").read_text("utf-8"))
    assert len(report["cases"]) == 5
    assert sum(case["comparable"] for case in report["cases"]) == 0
    assert all(case["blockers"] for case in report["cases"] if not case["comparable"])
    assert all(
        case["comparison_basis_fingerprint"] == report["configuration_fingerprint"]
        for case in report["cases"]
    )

    with pytest.raises(TeacherForcedBenchmarkError, match="completed teacher-forced run"):
        TeacherForcedBenchmarkE2ERunner().run(
            PILOT,
            output,
            bundle,
            information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        )


def test_real_hybrid_profile_fails_closed_before_it_can_create_a_smoke_run(tmp_path: Path) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    output = tmp_path / "real-hybrid"

    with pytest.raises(TeacherForcedBenchmarkError, match="scripted smoke fallback is disabled"):
        TeacherForcedBenchmarkE2ERunner(
            retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID
        ).run(
            PILOT,
            output,
            bundle,
            information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        )

    assert not output.exists()


def test_public_checkpoint_case_has_no_gold_fields() -> None:
    public = build_public_checkpoint_case(
        case_id=StableId("test"),
        project_id=ProjectId("test"),
        target_range=(21, 25),
        history_range=(1, 20),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    assert not hasattr(public, "observed_use_gold")
    assert not hasattr(public, "operational_constraint_gold")
    assert not hasattr(public, "plan_obligation_gold")
    assert not hasattr(public, "future_text_root_private")
    assert not hasattr(public, "input_plan_root")
    assert not hasattr(public, "input_world_root_verified")
    assert not hasattr(public, "gold_file_private")


def test_plan_needs_does_not_access_gold(tmp_path: Path) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    case = bundle.case_manifests[0]
    world_root = next(
        root for root in bundle.world_roots if root.root_hash == case.input_world_root_verified
    )

    needs = Stage1NeedGenerator().generate(world_root, case)

    corrupted = case.model_copy(update={"observed_use_gold": ()})
    corrupted_needs = Stage1NeedGenerator().generate(world_root, corrupted)

    assert len(needs) == len(corrupted_needs)
    assert tuple(n.query_text for n in needs) == tuple(n.query_text for n in corrupted_needs)


def test_plan_needs_uses_only_public_case_fields() -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    case = bundle.case_manifests[0]
    plan = next(root for root in bundle.plan_roots if root.root_hash == case.input_plan_root)
    world = next(
        root for root in bundle.world_roots if root.root_hash == case.input_world_root_verified
    )
    runner_plan_needs = Stage2PairedPilotRunner._plan_needs

    public = build_public_checkpoint_case(
        case_id=case.case_id,
        project_id=case.project_id,
        target_range=case.target_range,
        history_range=case.history_range,
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    needs_from_public = runner_plan_needs(public, plan, world.source_commit, ())

    needs_from_full = runner_plan_needs(case, plan, world.source_commit, ())

    assert len(needs_from_public) == len(needs_from_full)
    assert tuple(n.query_text for n in needs_from_public) == tuple(
        n.query_text for n in needs_from_full
    )


def test_public_checkpoint_case_cannot_receive_gold() -> None:
    fields = set(PublicCheckpointCase.model_fields.keys())
    gold_fields = {
        "observed_use_gold",
        "operational_constraint_gold",
        "plan_obligation_gold",
        "future_text_root_private",
        "input_plan_root",
        "input_world_root_verified",
        "gold_file_private",
    }
    overlap = fields & gold_fields
    assert not overlap, f"PublicCheckpointCase leaks Gold fields: {overlap}"


def test_resume_after_committed_prelude_does_not_append_prelude_twice(
    tmp_path: Path,
) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    output = tmp_path / "resume-after-prelude"
    runner = TeacherForcedBenchmarkE2ERunner(semantic_endpoint=None)

    runner.run(
        PILOT,
        output,
        bundle,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        max_chapter=0,
    )
    first_progress = json.loads((output / "progress_manifest.json").read_text("utf-8"))
    assert first_progress["last_accepted_chapter"] == 0
    assert first_progress["completed_chapters"] == [0]

    summary = runner.run(
        PILOT,
        output,
        bundle,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        max_chapter=1,
        resume=True,
    )

    assert summary["last_revealed_chapter"] == 1
    progress = json.loads((output / "progress_manifest.json").read_text("utf-8"))
    assert progress["completed_chapters"] == [0, 1]


def test_resume_after_genesis_counts_genesis_as_the_segment_preamble(
    tmp_path: Path,
) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    output = tmp_path / "resume-after-genesis"
    runner = TeacherForcedBenchmarkE2ERunner(semantic_endpoint=None)

    runner.run(
        PILOT,
        output,
        bundle,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        stop_after_genesis=True,
    )
    summary = runner.run(
        PILOT,
        output,
        bundle,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        max_chapter=1,
        resume=True,
    )

    assert summary["segment_commit_count"] == 1
    assert summary["segment_preamble_count"] == 1
    assert summary["total_commit_count"] == 2


def test_resume_rebuilds_checkpoint_without_recommitting_accepted_chapter(
    tmp_path: Path,
) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    output = tmp_path / "resume-checkpoint"
    runner = TeacherForcedBenchmarkE2ERunner(semantic_endpoint=None)
    first = runner.run(
        PILOT,
        output,
        bundle,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        max_chapter=20,
    )
    assert first["total_commit_count"] == 21

    recovered = runner.run(
        PILOT,
        output,
        bundle,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        max_chapter=20,
        resume=True,
    )

    assert recovered["last_revealed_chapter"] == 20
    assert recovered["segment_commit_count"] == 0
    assert recovered["segment_preamble_count"] == 21
    assert recovered["total_commit_count"] == 21
    assert recovered["paired_results_count"] == 1
    assert recovered["checkpoint_chain_consistent"] is True
    progress = json.loads((output / "progress_manifest.json").read_text("utf-8"))
    assert progress["completed_chapters"] == list(range(21))
