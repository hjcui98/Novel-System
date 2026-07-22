from __future__ import annotations

import os
from argparse import Namespace

import pytest
from scripts.run_stage1_benchmark import run_native_bge
from sqlalchemy import func, select

from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.models import EvaluationEntryRow
from novel_agent.domain.benchmark import BenchmarkTrack
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRole,
    RetrievalInferenceOperation,
    RetrievalInferenceStatus,
)
from novel_agent.domain.runtime import RunEventType
from novel_agent.services.event_log import RunEventLogRepository
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

pytestmark = pytest.mark.model_required


def _database_url() -> str:
    return (
        f"postgresql+psycopg://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@127.0.0.1:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
    )


def test_native_bge_oracle_matrix_crosses_real_r1_opensearch_and_models() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    result = run_native_bge(
        bundle,
        case.case_id,
        BenchmarkTrack.ORACLE,
        Namespace(
            database_url=_database_url(),
            opensearch_url=f"http://127.0.0.1:{os.environ['OPENSEARCH_PORT']}",
            token_budget=4000,
        ),
    )

    assert result.run_id is not None
    assert result.context_frozen is True
    assert len(result.profile_results) == 16
    assert "BAAI/bge-m3@5617a9f" in result.config.embedding_profile
    assert "BAAI/bge-reranker-v2-m3@953dc6f" in result.config.reranker_profile
    assert result.retrieval_model_calls
    assert {call.operation for call in result.retrieval_model_calls} == {
        RetrievalInferenceOperation.EMBEDDING,
        RetrievalInferenceOperation.RERANK,
    }
    assert all(
        call.model_role is ModelRole.BATCH_TEST
        and call.purpose is ModelCallPurpose.EVALUATION
        and call.status is RetrievalInferenceStatus.SUCCEEDED
        and call.usage.cost_usd == 0
        for call in result.retrieval_model_calls
    )
    leaked_profiles = tuple(
        (profile.profile, profile.metrics.future_leakage_rate)
        for profile in result.profile_results
        if profile.metrics.future_leakage_rate != 0
    )
    assert leaked_profiles == ()

    engine = build_engine(_database_url())
    try:
        session_factory = build_session_factory(engine)
        events = RunEventLogRepository(session_factory).replay(result.run_id)
        with session_factory() as session:
            evaluation_count = session.scalar(
                select(func.count())
                .select_from(EvaluationEntryRow)
                .where(EvaluationEntryRow.run_id == result.run_id.root)
            )
    finally:
        engine.dispose()
    assert events[0].event_type is RunEventType.RUN_CREATED
    assert events[-1].event_type is RunEventType.RUN_COMPLETED
    assert sum(event.event_type is RunEventType.MODEL_COMPLETED for event in events) == len(
        result.retrieval_model_calls
    )
    assert evaluation_count == len(result.profile_results)
