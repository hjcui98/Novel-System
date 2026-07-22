from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select

from novel_agent.adapters.postgres.database import build_engine
from novel_agent.adapters.postgres.models import EvaluationEntryRow
from novel_agent.services.stage2_benchmark_flow import Stage2BenchmarkFlowRunner
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def test_stage2_benchmark_flow_persists_current_read_pilot_without_overclaiming(
    tmp_path: Path,
) -> None:
    summary = Stage2BenchmarkFlowRunner().run(make_synthetic_bundle(), tmp_path)

    assert summary["status"] == "read_pilot_completed"
    assert summary["paired_results_count"] == 2
    assert summary["future_leakage_count"] == 0
    assert summary["bounded_controller_executed"] is True
    assert summary["planner_bootstrap_executed"] is False
    assert summary["curator_continuous_replay_executed"] is False
    assert summary["project_commit_database_written"] is False
    assert summary["stage2_gate_ready"] is False
    assert (tmp_path / "canonical.bundle.json").is_file()
    assert (tmp_path / "scenarios/scenario.visible_at_cutoff.json").is_file()
    assert (tmp_path / "scenarios/scenario.author_plan_conditioned.json").is_file()
    assert (tmp_path / "paired_controller_report.json").is_file()
    assert (tmp_path / "evaluation/paired_controller.parquet").is_file()
    persisted = json.loads((tmp_path / "flow_summary.json").read_text("utf-8"))
    assert persisted["bundle_hash"] == summary["bundle_hash"]

    engine = build_engine(f"sqlite:///{(tmp_path / 'evaluation/ledger.sqlite3').resolve()}")
    try:
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(EvaluationEntryRow)) == 2
    finally:
        engine.dispose()

    assert Stage2BenchmarkFlowRunner().run(make_synthetic_bundle(), tmp_path) == summary
