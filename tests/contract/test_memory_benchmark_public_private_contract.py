from __future__ import annotations

import pytest
from pydantic import ValidationError

from novel_agent.domain.ids import ProjectId, StableId
from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile
from novel_agent.domain.stage2 import PublicCheckpointCase
from novel_agent.services.memory_benchmark_contract import (
    PublicBenchmarkTaintError,
    assert_safe_public_payload,
    build_public_checkpoint_case,
)


def test_safe_public_task_is_versioned_hash_bound_and_has_no_answer_count() -> None:
    first = build_public_checkpoint_case(
        case_id=StableId("ZTJ-P001"),
        project_id=ProjectId("ztj"),
        history_range=(1, 20),
        target_range=(21, 25),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    second = build_public_checkpoint_case(
        case_id=StableId("ZTJ-P001"),
        project_id=ProjectId("ztj"),
        history_range=(1, 20),
        target_range=(21, 25),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )

    assert first == second
    assert first.task_contract.task_template_version == "memory_context_task.v1"
    assert "最多" not in first.task_contract.task_text
    assert "不超过" not in first.task_contract.task_text
    assert first.plan_root_ref is None


@pytest.mark.parametrize(
    "private_field",
    (
        "gold_items",
        "gold_weight",
        "accepted_evidence_sets",
        "future_evidence_refs",
        "forbidden_future_facts",
        "target_plan",
        "preparation_refs",
    ),
)
def test_public_contract_rejects_all_evaluator_only_fields(private_field: str) -> None:
    public = build_public_checkpoint_case(
        case_id=StableId("ZTJ-P001"),
        project_id=ProjectId("ztj"),
        history_range=(1, 20),
        target_range=(21, 25),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    payload = public.model_dump(mode="json")
    payload[private_field] = []

    with pytest.raises((ValidationError, PublicBenchmarkTaintError)):
        assert_safe_public_payload(payload)
        PublicCheckpointCase.model_validate(payload)
