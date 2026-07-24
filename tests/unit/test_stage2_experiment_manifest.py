from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest
from scripts.run_stage2_teacher_forced_e2e import (
    _ensure_experiment_manifest,
    _load_quality_repair_flags,
)

from novel_agent.domain.stage2 import (
    ControllerMode,
    CuratorEvidenceContract,
    EvidenceSupportGateMode,
    QualityRepairFeatureFlags,
)
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler

ROOT = Path(__file__).parents[2]
PILOT = ROOT / "benchmarks/private/ztj_memory_pilot_v0.1"


def _args(experiment_id: str = "stage2r-run3") -> Namespace:
    return Namespace(
        experiment_id=experiment_id,
        source=PILOT,
        database_url=("postgresql+psycopg://user:secret@127.0.0.1:5432/novel_agent_stage2r_run3"),
        retrieval_backend="real_hybrid",
        opensearch_url="http://127.0.0.1:9200",
        embedding_url="http://127.0.0.1:8081/v1/embeddings",
        reranker_url="http://127.0.0.1:8082/rerank",
        model_base_url="http://127.0.0.1:8002/v1",
        model="qwen36-27b-nvfp4",
        memory_write_dry_run=False,
    )


def test_experiment_manifest_is_credential_safe_and_resume_stable(tmp_path: Path) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    flags = QualityRepairFeatureFlags()

    _ensure_experiment_manifest(_args(), bundle, tmp_path, flags)
    _ensure_experiment_manifest(_args(), bundle, tmp_path, flags)

    payload = json.loads((tmp_path / "experiment_manifest.json").read_text("utf-8"))
    assert payload["experiment_id"] == "stage2r-run3"
    assert payload["database"] == ("postgresql+psycopg://127.0.0.1:5432/novel_agent_stage2r_run3")
    assert "secret" not in json.dumps(payload)

    with pytest.raises(ValueError, match="manifest differs"):
        _ensure_experiment_manifest(_args("stage2r-run4"), bundle, tmp_path, flags)


def test_quality_repair_config_parses_json_enum_values_in_strict_mode(
    tmp_path: Path,
) -> None:
    config = tmp_path / "quality-repair.json"
    config.write_text(
        json.dumps(
            {
                "controller_mode": "deterministic_plus_agentic_delta",
                "curator_evidence_contract": "candidate_id_v2",
                "evidence_support_gate": "enforce_pre_candidate",
                "max_controller_decision_model_calls": 2,
                "max_agentic_actions": 8,
            }
        ),
        encoding="utf-8",
    )

    flags = _load_quality_repair_flags(Namespace(quality_repair_config=config))

    assert flags.controller_mode is ControllerMode.DETERMINISTIC_PLUS_AGENTIC_DELTA
    assert flags.curator_evidence_contract is CuratorEvidenceContract.CANDIDATE_ID_V2
    assert flags.evidence_support_gate is EvidenceSupportGateMode.ENFORCE_PRE_CANDIDATE
