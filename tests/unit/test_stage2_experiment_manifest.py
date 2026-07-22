from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest
from scripts.run_stage2_teacher_forced_e2e import _ensure_experiment_manifest

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
    )


def test_experiment_manifest_is_credential_safe_and_resume_stable(tmp_path: Path) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)

    _ensure_experiment_manifest(_args(), bundle, tmp_path)
    _ensure_experiment_manifest(_args(), bundle, tmp_path)

    payload = json.loads((tmp_path / "experiment_manifest.json").read_text("utf-8"))
    assert payload["experiment_id"] == "stage2r-run3"
    assert payload["database"] == ("postgresql+psycopg://127.0.0.1:5432/novel_agent_stage2r_run3")
    assert "secret" not in json.dumps(payload)

    with pytest.raises(ValueError, match="manifest differs"):
        _ensure_experiment_manifest(_args("stage2r-run4"), bundle, tmp_path)
