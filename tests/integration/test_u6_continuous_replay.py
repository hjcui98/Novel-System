"""One real-bundle, no-model U6-A basis integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_agent.domain.u6_continuous_replay import U6BasisStatus
from novel_agent.services.u6_continuous_replay import U6ContinuousReplayService

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "benchmarks/private/ztj_novelmem_v0.5"


@pytest.mark.integration
def test_u6a_freezes_one_pass_basis_without_claiming_writer_completion(tmp_path: Path) -> None:
    output = tmp_path / "u6a-basis"
    result = U6ContinuousReplayService(
        bundle_root=BUNDLE,
        output_root=output,
        experiment_id="integration",
    ).run()

    assert result.manifest.status is U6BasisStatus.FROZEN
    assert result.report.status == "BASIS_FROZEN"
    assert result.report.chapters_declared == 300
    assert result.report.chapters_ingested == 300
    assert result.report.ingest_passes == 1
    assert result.report.basis_count == 34
    assert result.report.canary_job_count == 45
    assert result.report.expected_readout_task_count == 81
    assert result.report.completed_readout_task_count == 0
    assert result.report.evaluation_discard_count == 0
    assert result.report.future_leakage_count == 0
    assert result.report.duplicate_checkpoint_declarations == 0
    assert result.manifest_path.is_absolute()
    assert result.report_path.is_absolute()
    persisted = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "FROZEN"
    assert all(node["status"] == "FROZEN" for node in persisted["basis_nodes"])
