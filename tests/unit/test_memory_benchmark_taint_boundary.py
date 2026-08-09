from __future__ import annotations

import pytest
from pydantic import ValidationError

from novel_agent.domain.artifacts import PlanRootRef
from novel_agent.domain.ids import ArtifactId, ProjectId, SchemaVersion, StableId
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    ContextAssemblyStatus,
    EvidenceLedgerEntry,
    EvidenceSet,
    FreezeReceipt,
    WriterContextBudgetReport,
    WriterContextItem,
    WriterContextPackage,
    WriterContextSection,
)
from novel_agent.services.benchmark_importer import content_id
from novel_agent.services.memory_benchmark_contract import (
    PublicBenchmarkTaintError,
    assert_safe_public_payload,
    build_public_checkpoint_case,
    build_safe_task_contract,
    profile_namespace,
    verify_public_checkpoint_case,
)
from tests.fixtures.stage2_memory_benchmark import frozen_evaluation_inputs


def test_task_contract_rejects_backwards_or_nonfuture_target_ranges() -> None:
    base = {
        "task_id": StableId("task.invalid"),
        "task_text": "safe",
        "checkpoint_chapter": 20,
        "target_chapter_start": 21,
        "target_chapter_end": 23,
        "information_profile": BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        "task_template_version": "v1",
        "output_contract_version": "v1",
    }
    with pytest.raises(ValidationError, match="range is invalid"):
        BenchmarkTaskContract.model_validate(base | {"target_chapter_end": 19})
    with pytest.raises(ValidationError, match="must follow"):
        BenchmarkTaskContract.model_validate(base | {"target_chapter_start": 20})


def test_grounded_contracts_fail_closed_on_missing_or_duplicate_provenance() -> None:
    _gold, package, ledger, _receipt = frozen_evaluation_inputs()
    item = package.current_world_state[0]
    with pytest.raises(ValidationError, match="requires evidence"):
        WriterContextItem.model_validate(item.model_dump() | {"evidence_ledger_ids": ()})
    with pytest.raises(ValidationError, match="must be unique"):
        WriterContextItem.model_validate(
            item.model_dump() | {"retrieval_unit_ids": (item.retrieval_unit_ids[0],) * 2}
        )
    with pytest.raises(ValidationError, match="source evidence"):
        EvidenceLedgerEntry.model_validate(
            ledger.entries[0].model_dump() | {"evidence_refs": (), "plan_node_ids": ()}
        )
    with pytest.raises(ValidationError, match="cannot be empty"):
        EvidenceSet(
            evidence_set_id=StableId("accepted.empty"),
            evidence_refs=(),
            plan_node_ids=(),
        )


def test_ready_budget_and_package_section_invariants_are_validated() -> None:
    _gold, package, _ledger, _receipt = frozen_evaluation_inputs()
    budget = package.budget_report
    with pytest.raises(ValidationError, match="cannot exceed"):
        WriterContextBudgetReport.model_validate(
            budget.model_dump()
            | {
                "final_status": ContextAssemblyStatus.READY,
                "actual_rendered_writer_tokens": budget.configured_writer_token_budget + 1,
            }
        )
    wrong_item = package.current_world_state[0].model_copy(
        update={"section": WriterContextSection.CAUSAL_HISTORY}
    )
    with pytest.raises(ValidationError, match="wrong section"):
        WriterContextPackage.model_validate(
            package.model_dump() | {"current_world_state": (wrong_item.model_dump(),)}
        )


def test_freeze_receipt_requires_pre_reveal_and_all_three_arms() -> None:
    base = {
        "receipt_id": StableId("freeze.invalid"),
        "public_input_hash": content_id({"public": True}),
        "code_version": "test",
        "run_config_hash": content_id({"config": True}),
        "arm_artifact_hashes": {
            "A": content_id({"arm": "A"}),
            "B": content_id({"arm": "B"}),
            "C": content_id({"arm": "C"}),
        },
        "frozen_before_reveal": True,
    }
    with pytest.raises(ValidationError, match="before Gold reveal"):
        FreezeReceipt.model_validate(base | {"frozen_before_reveal": False})
    with pytest.raises(ValidationError, match="A, B, and C"):
        FreezeReceipt.model_validate(
            base | {"arm_artifact_hashes": {"A": content_id({"arm": "A"})}}
        )


def test_recursive_taint_hash_and_namespace_checks_fail_closed() -> None:
    with pytest.raises(PublicBenchmarkTaintError, match="fixed answer-count"):
        assert_safe_public_payload(["safe", {"nested": "最多 20 项"}])
    with pytest.raises(ValueError, match="experiment id"):
        profile_namespace(
            ProjectId("project.test"),
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            " ",
        )
    _gold, package, _ledger, _receipt = frozen_evaluation_inputs()
    case = package.task_contract
    fake = type(
        "FakeCase",
        (),
        {
            "public_input_hash": content_id({"wrong": True}),
            "model_dump": lambda self, mode: {
                "task_contract": case.model_dump(mode="json"),
                "public_input_hash": content_id({"wrong": True}).root,
            },
        },
    )()
    with pytest.raises(PublicBenchmarkTaintError, match="hash mismatch"):
        verify_public_checkpoint_case(fake)


def test_task_contract_composes_safe_contract_with_normalized_task_intent() -> None:
    planning_context_hash = content_id({"planning": "source"})
    planning_context_ref = content_id({"planning": "document"})
    contract = build_safe_task_contract(
        case_id=StableId("case.intent"),
        checkpoint_chapter=20,
        target_range=(21, 25),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="为写 21-25 章准备历史上下文 不续写",
        planning_context_ref=planning_context_ref,
        planning_context_hash=planning_context_hash,
    )
    assert contract.task_text.startswith("为目标章节 21-25 准备必要的历史记忆 ContextPackage。")
    assert "评测 Gold" in contract.task_text
    assert contract.task_intent == "为写 21-25 章准备历史上下文 不续写"
    assert contract.planning_context_ref == planning_context_ref
    assert contract.planning_context_hash == planning_context_hash
    assert contract.task_template_version == "memory_context_task.v1"

    with pytest.raises(PublicBenchmarkTaintError, match="blind profile"):
        build_safe_task_contract(
            case_id=StableId("case.intent"),
            checkpoint_chapter=20,
            target_range=(21, 25),
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            task_intent="任何任务意图都不该进入盲式档位",
        )
    with pytest.raises(PublicBenchmarkTaintError, match="fixed answer-count"):
        build_safe_task_contract(
            case_id=StableId("case.intent"),
            checkpoint_chapter=20,
            target_range=(21, 25),
            information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            task_intent="任务要求最多 20 项历史条目。",
        )


def test_public_checkpoint_case_forwards_planning_context_refs() -> None:
    planning_context_hash = content_id({"planning": "source"})
    planning_context_ref = content_id({"planning": "document"})
    public = build_public_checkpoint_case(
        case_id=StableId("case.public"),
        project_id=ProjectId("project.public"),
        history_range=(1, 20),
        target_range=(21, 25),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="为写 21-25 章准备历史上下文。",
        planning_context_ref=planning_context_ref,
        planning_context_hash=planning_context_hash,
        plan_root_ref=PlanRootRef(
            artifact_id=ArtifactId("sha256:" + "d" * 64),
            media_type="application/json",
            byte_length=1,
            schema_version=SchemaVersion("1.0.0"),
        ),
    )
    assert public.task_contract.task_intent == "为写 21-25 章准备历史上下文。"
    assert public.task_contract.planning_context_ref == planning_context_ref
    assert public.task_contract.planning_context_hash == planning_context_hash
    verify_public_checkpoint_case(public)

    vac = build_public_checkpoint_case(
        case_id=StableId("case.public"),
        project_id=ProjectId("project.public"),
        history_range=(1, 20),
        target_range=(21, 25),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    assert vac.task_contract.task_intent == ""
    assert vac.plan_root_ref is None
    verify_public_checkpoint_case(vac)
