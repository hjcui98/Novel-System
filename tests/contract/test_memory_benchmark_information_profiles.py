from __future__ import annotations

from pathlib import Path

import pytest

from novel_agent.domain.artifacts import PlanRootRef
from novel_agent.domain.ids import ArtifactId, ProjectId, SchemaVersion, StableId
from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile
from novel_agent.domain.retrieval_routing import RetrievalBackendProfile
from novel_agent.domain.stage2 import PublicBenchmarkConfig
from novel_agent.services.benchmark_importer import content_id
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_benchmark_contract import (
    build_public_checkpoint_case,
    profile_namespace,
)
from novel_agent.services.stage2_paired_pilot import Stage2PairedPilotRunner

PILOT = Path(__file__).parents[2] / "benchmarks/private/ztj_memory_pilot_v0.1"


def _plan_ref() -> PlanRootRef:
    return PlanRootRef(
        artifact_id=ArtifactId("sha256:" + "a" * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )


def test_visible_profile_strips_plan_while_author_profile_keeps_bootstrap_ref() -> None:
    with pytest.raises(ValueError, match="rejects PlanRoot"):
        build_public_checkpoint_case(
            case_id=StableId("ZTJ-P001"),
            project_id=ProjectId("project.profile"),
            history_range=(1, 20),
            target_range=(21, 25),
            plan_root_ref=_plan_ref(),
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        )
    visible = build_public_checkpoint_case(
        case_id=StableId("ZTJ-P001"),
        project_id=ProjectId("project.profile"),
        history_range=(1, 20),
        target_range=(21, 25),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    planned = build_public_checkpoint_case(
        case_id=StableId("ZTJ-P001"),
        project_id=ProjectId("project.profile"),
        history_range=(1, 20),
        target_range=(21, 25),
        plan_root_ref=_plan_ref(),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="prepare history",
        planning_context_ref=ArtifactId("sha256:" + "b" * 64),
        planning_context_hash=ArtifactId("sha256:" + "c" * 64),
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


def test_apc_template_chain_keeps_all_memory_evidence_observed_only() -> None:
    """Phase 0B smoke: the template need chain with the real pilot data.

    Plan-labeled units may reach candidate sets only through explicit
    plan-channel needs; historical needs stay observed-only, and no claim or
    ledger entry outside the plan-obligation channel cites plan provenance.
    """

    bundle = HumanBenchmarkCompiler().compile(PILOT)
    case = next(item for item in bundle.case_manifests if item.case_id.root == "ZTJ-P003")
    context = next(
        item for item in bundle.planning_contexts if item.source_hash == case.planning_context_hash
    )
    assert context.task_intent == case.task_intent
    history = next(root for root in bundle.text_roots if root.root_hash == case.input_text_root)
    world = next(
        root for root in bundle.world_roots if root.root_hash == case.input_world_root_verified
    )
    plan = next(root for root in bundle.plan_roots if root.root_hash == case.input_plan_root)
    public = build_public_checkpoint_case(
        case_id=case.case_id,
        project_id=case.project_id,
        target_range=case.target_range,
        history_range=case.history_range,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent=case.task_intent,
        planning_context_ref=case.planning_context_ref,
        planning_context_hash=case.planning_context_hash,
        plan_root_ref=PlanRootRef(
            artifact_id=plan.root_hash,
            media_type="application/vnd.novel-agent.plan-root+json",
            byte_length=len(plan.model_dump_json().encode("utf-8")),
            schema_version=plan.schema_version,
        ),
    )
    config = PublicBenchmarkConfig(
        schema_version=bundle.bundle_schema_version,
        configuration_fingerprint=content_id({"profile": "apc-template-chain"}),
        expected_profiles=tuple(item.value for item in BenchmarkInformationProfile),
    )
    runner = Stage2PairedPilotRunner(
        arms=("A",),
        retrieval_backend_profile=RetrievalBackendProfile.SCRIPTED_SMOKE,
    )
    comparison = runner.resolve_state_case(
        config,
        public,
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        history=history,
        world=world,
        plan=plan,
        base_commit=world.source_commit,
        planning_context=context,
    )

    deterministic = comparison.deterministic
    assert deterministic.future_leakage_count == 0
    assert deterministic.assembly_status is not None
    plan_needs = {
        trace.need_id
        for trace in deterministic.context.retrieval_traces
        for candidate in trace.candidates
        if candidate.unit.information_label == "plan"
    }
    assert not plan_needs
    ledger = deterministic.evidence_ledger
    assert ledger is not None
    assert all(not entry.plan_node_ids for entry in ledger.entries)
    groups = deterministic.claim_support_groups or ()
    plan_groups = [group for group in groups if group.plan_node_ids]
    assert not plan_groups
