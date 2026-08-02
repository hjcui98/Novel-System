from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.aggregate_stage2_checkpoint_reports import _validate_formal_run_lifecycle

from novel_agent.domain.ids import ProjectId, StableId
from novel_agent.domain.stage2 import (
    BenchmarkInformationProfile,
    ScenarioBuildMode,
    ScenarioRunResult,
)


def _write_manifest(path: Path, **updates: object) -> None:
    payload: dict[str, object] = {
        "code_commit": "abc123",
        "code_source_fingerprint": "sha256:" + "a" * 64,
        "experiment_id": "stage2m-test",
        "information_profile": BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED.value,
        "code_source_dirty": False,
        "code_version": "stage2-paired-pilot-v0.4",
        "run_config_hash": "sha256:" + "b" * 64,
        "benchmark_contract_hash": "sha256:" + "c" * 64,
        "matcher_version": "gold-evidence-matcher-v3",
        "writer_token_budget": 4000,
        "evidence_ledger_token_budget": 12_000,
    }
    payload.update(updates)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_formal_lifecycle_rejects_dirty_source_manifest(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "experiment_manifest.json", code_source_dirty=True)

    with pytest.raises(SystemExit, match="dirty or unverified source"):
        _validate_formal_run_lifecycle(
            tmp_path,
            profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            cases=(),
        )


def test_formal_lifecycle_rejects_unclosed_scenario(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "experiment_manifest.json")
    scenario = ScenarioRunResult(
        scenario_id=StableId("scenario.formal.lifecycle"),
        project_id=ProjectId("project.formal.lifecycle"),
        build_mode=ScenarioBuildMode.CONTINUOUS_REPLAY,
        chapter_receipts=(),
        checkpoints=(),
        completed=False,
    )
    (tmp_path / "scenario_run.json").write_text(
        scenario.model_dump_json() + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="scenario lifecycle is not closed"):
        _validate_formal_run_lifecycle(
            tmp_path,
            profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            cases=(),
        )


def test_formal_lifecycle_rejects_missing_manifest_identity(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "experiment_manifest.json", run_config_hash=None)

    with pytest.raises(SystemExit, match="missing run_config_hash"):
        _validate_formal_run_lifecycle(
            tmp_path,
            profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            cases=(),
        )
