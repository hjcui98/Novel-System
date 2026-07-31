from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from scripts.aggregate_stage2_checkpoint_reports import _load_stage2m_case
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.fixtures.stage2_memory_benchmark_baseline import LEGACY_OVERSIZED_CASES
from tests.unit.test_stage1_memory_pipeline import memory_need

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import CandidatePool, Stage1QueryIntent
from novel_agent.domain.memory_benchmark import MemoryBenchmarkCaseArmReport
from novel_agent.services.controller_legal_actions import LegalActionProvider
from novel_agent.services.memory_pipeline import AnchorBuilder, ContextCompiler, EvidenceExpander
from novel_agent.services.retrieval import (
    FusionService,
    InMemoryRetrievalBackend,
    RetrievalOrchestrator,
)


def test_scrubbed_r35_baseline_records_mandatory_overflow_without_private_data() -> None:
    assert all(item.mandatory_tokens > item.configured_tokens for item in LEGACY_OVERSIZED_CASES)
    assert {item.case_id for item in LEGACY_OVERSIZED_CASES} == {"ZTJ-P004", "ZTJ-P005"}


def test_legacy_stage2m_case_without_formal_identity_fields_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stage2m_case_C20_A.json"
    path.write_text(
        '{"case_id":"ZTJ-P001","checkpoint_chapter":20,"arm":"A"}\n',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="invalid Stage 2M case artifact"):
        _load_stage2m_case(path)

    with pytest.raises(ValidationError):
        MemoryBenchmarkCaseArmReport.model_validate_json(path.read_text("utf-8"))


def test_legacy_context_is_explicitly_ineligible_when_it_overflows() -> None:
    bundle = make_synthetic_bundle()
    history, _future = bundle.text_roots
    world = bundle.world_roots[0]
    snapshot = StableId("snapshot.legacy-overflow")
    units = AnchorBuilder().build(world, history, bundle.plan_roots[0], snapshot_id=snapshot)
    state = world.states[0]
    need = memory_need(
        "need.legacy-overflow",
        Stage1QueryIntent.CURRENT_STATE,
        state.predicate,
        (CandidatePool.R1,),
        mandatory=True,
        entity_ids=(state.subject_id,),
    )
    trace = RetrievalOrchestrator(
        InMemoryRetrievalBackend(units),
        FusionService(),
    ).retrieve(need)
    package = ContextCompiler(EvidenceExpander()).compile(
        ((need, trace),),
        history,
        context_id=StableId("context.legacy-overflow"),
        base_commit=world.source_commit,
        snapshot_id=snapshot,
        task_contract="legacy regression only",
        token_budget=1,
    )

    assert package.budget_report.mandatory_tokens > package.budget_report.token_budget
    assert package.contract_version == "stage1_context.legacy"
    assert package.benchmark_quality_eligible is False


def test_controller_action_id_is_stable_and_bounded_for_long_need_and_step_ids() -> None:
    need_id = StableId("need." + "n" * 122)
    step_id = StableId("step." + "s" * 122)

    first = LegalActionProvider._action_id(need_id, step_id)
    second = LegalActionProvider._action_id(need_id, step_id)

    assert first == second
    assert first.startswith("action.")
    assert len(first) <= 128
    assert StableId(first).root == first
