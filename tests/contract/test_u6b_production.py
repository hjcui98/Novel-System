from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from novel_agent.domain.ids import CommitId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.u6b_production import (
    U6BCompactionEvidence,
    U6BCompactionOutcome,
    U6BPhaseUsage,
    U6BProductionBaselineReport,
    U6BWorkerPhaseReport,
)

ROOT = Path(__file__).parents[2]
HASH = CommitId("sha256:" + "1" * 64)


def test_u6b_schemas_match_domain_models() -> None:
    for model in (
        U6BCompactionEvidence,
        U6BPhaseUsage,
        U6BProductionBaselineReport,
        U6BWorkerPhaseReport,
    ):
        path = ROOT / "schemas" / "stage5" / f"{model.__name__}.schema.json"
        assert json.loads(path.read_text(encoding="utf-8")) == model.model_json_schema()


def test_u6b_compaction_outcomes_keep_token_and_ratio_evidence() -> None:
    common = {
        "receipt_id": StableId("u6b.compaction.test"),
        "run_id": RunId("run.u6b.test"),
        "task_id": TaskId("task.u6b.test"),
        "chapter_index": 21,
        "min_reduction_ratio": 0.1,
        "covered_event_range": (1, 2),
        "protected_items_retained": True,
        "pending_effects_retained": True,
        "safe_cut": True,
        "semantic_retention_passed": True,
    }
    compacted = U6BCompactionEvidence(
        **common,
        outcome=U6BCompactionOutcome.COMPACTED,
        input_context_tokens=100,
        output_context_tokens=80,
        reduction_ratio=0.2,
        source_receipt_id=StableId("compaction.runtime.test"),
    )
    assert compacted.reduction_ratio == 0.2
    no_op = U6BCompactionEvidence(
        **{**common, "receipt_id": StableId("u6b.compaction.no-op")},
        outcome=U6BCompactionOutcome.NO_OP,
        input_context_tokens=40,
        output_context_tokens=40,
        reduction_ratio=0.0,
    )
    assert no_op.source_receipt_id is None
    with pytest.raises(ValidationError, match="ratio"):
        U6BCompactionEvidence(
            **{**common, "receipt_id": StableId("u6b.compaction.bad")},
            outcome=U6BCompactionOutcome.COMPACTED,
            input_context_tokens=100,
            output_context_tokens=95,
            reduction_ratio=0.2,
            source_receipt_id=StableId("compaction.runtime.bad"),
        )


def test_u6b_pass_requires_complete_clean_projection_evidence() -> None:
    worker = U6BWorkerPhaseReport(
        phase_index=1,
        report_path="/tmp/u6b-phase-1.json",
        status="yielded",
        completed_chapters_after=(21,),
        restarted_from_process=False,
    )
    phase = U6BPhaseUsage(
        chapter_index=21,
        phase="plan",
        wall_clock_ms=1,
        model_call_count=1,
        input_tokens=10,
        output_tokens=2,
        attempt_count=1,
    )
    with pytest.raises(ValidationError, match="PASS"):
        U6BProductionBaselineReport(
            status="PASS",
            run_id=RunId("run.u6b.test"),
            project_id=ProjectId("project.u6b.test"),
            basis_commit=HASH,
            final_commit=HASH,
            expected_chapters=(21, 22),
            completed_chapters=(21,),
            restart_boundary_chapter=21,
            worker_phases=(worker,),
            phase_usage=(phase,),
            compaction=(),
            model_call_count=1,
            input_tokens=10,
            output_tokens=2,
            event_count=1,
            task_count=1,
            attempt_count=1,
            commit_count=1,
            artifact_count=1,
            future_leakage_count=0,
            duplicate_effect_count=0,
            projection_rebuild_verified=True,
        )
