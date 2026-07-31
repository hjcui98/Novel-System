from __future__ import annotations

from typing import Any

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import RetrievalUnitKind
from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile, FreezeReceipt
from novel_agent.domain.stage2 import PublicBenchmarkConfig
from novel_agent.services.benchmark_importer import content_id
from novel_agent.services.memory_benchmark_contract import (
    build_public_checkpoint_case,
    build_safe_task_contract,
)
from novel_agent.services.memory_pipeline import AnchorBuilder
from novel_agent.services.stage2_paired_pilot import Stage2PairedPilotRunner
from novel_agent.services.task_conditioned_need_generation import TaskPlanConditionedNeedGenerator
from novel_agent.services.writer_context_assembler import WriterContextAssembler
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def writer_context_inputs() -> tuple[Any, ...]:
    bundle = make_synthetic_bundle()
    history, _future = bundle.text_roots
    world = bundle.world_roots[0]
    plan = bundle.plan_roots[0]
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    needs = TaskPlanConditionedNeedGenerator().generate(task, world, plan)
    units = AnchorBuilder().build(
        world,
        history,
        plan,
        snapshot_id=StableId("snapshot.stage2m"),
    )
    return task, needs, units, world.source_commit


def frozen_evaluation_inputs() -> tuple[Any, ...]:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    history, _future = bundle.text_roots
    world = bundle.world_roots[0]
    plan = bundle.plan_roots[0]
    gold = case.operational_constraint_gold[0]
    units = AnchorBuilder().build(
        world,
        history,
        plan,
        snapshot_id=StableId("snapshot.evaluator"),
    )
    unit = next(
        item
        for item in units
        if item.unit_kind is RetrievalUnitKind.STATE_ANCHOR
        and {ref.evidence_id for ref in item.evidence_refs}.intersection(
            ref.evidence_id for ref in gold.evidence_refs
        )
    )
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    assembled = WriterContextAssembler().assemble(
        task=task,
        units=(unit.model_copy(update={"mandatory": True}),),
        needs=(),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=StableId("snapshot.evaluator"),
        arm="A",
        writer_token_budget=1000,
    )
    package = assembled.package
    receipt = FreezeReceipt(
        receipt_id=StableId("freeze.evaluator"),
        public_input_hash=content_id({"public": True}),
        code_version="test",
        run_config_hash=content_id({"config": True}),
        arm_artifact_hashes={
            "A": content_id(package.model_dump(mode="json")),
            "B": content_id({"failure": "not-run"}),
            "C": content_id({"failure": "not-run"}),
        },
        frozen_before_reveal=True,
    )
    return gold, package, assembled.evidence_ledger, receipt


def resolved_public_comparison() -> tuple[Any, ...]:
    bundle = make_synthetic_bundle()
    private_case = bundle.case_manifests[0]
    history = next(
        root for root in bundle.text_roots if root.root_hash == private_case.input_text_root
    )
    world = bundle.world_roots[0]
    plan = bundle.plan_roots[0]
    public_case = build_public_checkpoint_case(
        case_id=private_case.case_id,
        project_id=private_case.project_id,
        target_range=private_case.target_range,
        history_range=private_case.history_range,
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    config = PublicBenchmarkConfig(
        schema_version=bundle.bundle_schema_version,
        configuration_fingerprint=content_id({"fixture": "freeze-reveal"}),
        expected_profiles=tuple(item.value for item in BenchmarkInformationProfile),
    )
    runner = Stage2PairedPilotRunner()
    comparison = runner.resolve_state_case(
        config,
        public_case,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        history=history,
        world=world,
        plan=plan,
        base_commit=world.source_commit,
    )
    return bundle, private_case, public_case, runner, comparison
