"""SQLite integration proof for the U6-C fault harness and immutable report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.run_u6c_fault_matrix import run_matrix


@pytest.mark.integration
def test_u6c_fault_matrix_runs_all_cases_without_second_runtime(tmp_path: Path) -> None:
    database = tmp_path / "u6c.db"
    output_root = tmp_path / "u6c-output"
    report = run_matrix(
        f"sqlite+pysqlite:///{database}",
        output_root,
        "u6c.integration",
    )

    assert report.status.value == "PASS"
    assert len(report.cases) == 18
    assert report.projection_rebuild_verified is True
    assert report.duplicate_effect_count == 0
    assert report.forbidden_result_count == 0
    report_path = output_root / "u6c-fault-matrix-report.json"
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["report_schema"] == "u6c-fault-matrix.v1"
    assert len(payload["cases"]) == 18

    with pytest.raises(RuntimeError, match="refuses to reuse output root"):
        run_matrix(
            f"sqlite+pysqlite:///{database}",
            output_root,
            "u6c.integration.retry",
        )
