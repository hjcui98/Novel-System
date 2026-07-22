from __future__ import annotations

import math
import os

import pytest
from scripts.native_models import load_model_lock

from novel_agent.adapters.model import (
    HttpEmbeddingProvider,
    HttpPassageReranker,
    RetrievalModelRoute,
)
from novel_agent.domain.ids import RunId, TaskId
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRole

pytestmark = pytest.mark.model_required


def test_locked_bge_m3_executes_real_cpu_embedding() -> None:
    lock = load_model_lock()
    embedding = lock.models["embedding"]
    embedding_port = int(os.getenv("NOVEL_AGENT_EMBEDDING_MODEL_PORT", "8081"))
    embedding_provider = HttpEmbeddingProvider(
        RetrievalModelRoute(
            endpoint=f"http://127.0.0.1:{embedding_port}/v1/embeddings",
            model=embedding.model_id,
            revision=embedding.revision,
            runtime_fingerprint=embedding.runtime_fingerprint,
            run_id=RunId("run.model-smoke.embedding"),
            task_id=TaskId("task.model-smoke.embedding"),
            trace_id="trace.model-smoke.embedding",
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.BATCH_TEST,
            timeout_seconds=300,
        ),
        dimension=1024,
    )

    vectors = embedding_provider.embed(("林澈在北城疗伤。", "今天天气晴朗。"))
    assert len(vectors) == 2
    assert all(len(vector) == 1024 for vector in vectors)
    assert all(
        math.isclose(sum(value * value for value in vector), 1.0, rel_tol=1e-4)
        for vector in vectors
    )


def test_locked_bge_reranker_executes_real_cpu_ranking() -> None:
    reranker = load_model_lock().models["reranker"]
    reranker_port = int(os.getenv("NOVEL_AGENT_RERANKER_MODEL_PORT", "8082"))
    rerank_provider = HttpPassageReranker(
        RetrievalModelRoute(
            endpoint=f"http://127.0.0.1:{reranker_port}/rerank",
            model=reranker.model_id,
            revision=reranker.revision,
            runtime_fingerprint=reranker.runtime_fingerprint,
            run_id=RunId("run.model-smoke.reranker"),
            task_id=TaskId("task.model-smoke.reranker"),
            trace_id="trace.model-smoke.reranker",
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.BATCH_TEST,
            timeout_seconds=300,
        )
    )

    scores = rerank_provider.score(
        "林澈现在在哪里?",
        ("今天天气晴朗。", "林澈目前正在北城疗伤。"),
    )
    assert scores[1] > scores[0]
