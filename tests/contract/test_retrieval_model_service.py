from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient, Response
from scripts.retrieval_model_service import ModelServiceConfig, create_app

REVISION = "a" * 40


class _EmbeddingRuntime:
    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple((float(index),) * 1024 for index, _ in enumerate(texts))


class _RerankerRuntime:
    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        return tuple(float(len(set(query).intersection(passage))) for passage in passages)


def test_embedding_service_is_versioned_and_openai_compatible() -> None:
    config = ModelServiceConfig(
        kind="embedding",
        model_id="BAAI/bge-m3",
        revision=REVISION,
        max_input_tokens=8192,
        dimension=1024,
        max_batch_size=2,
    )

    async def exercise() -> tuple[Response, Response, Response, Response, Response]:
        transport = ASGITransport(app=create_app(config, _EmbeddingRuntime()))
        async with AsyncClient(transport=transport, base_url="http://embedding.test") as client:
            return (
                await client.get("/health"),
                await client.post(
                    "/v1/embeddings",
                    json={
                        "model": "BAAI/bge-m3",
                        "input": ["甲", "乙"],
                        "encoding_format": "float",
                    },
                ),
                await client.post("/v1/embeddings", json={"model": "wrong/model", "input": "甲"}),
                await client.post(
                    "/v1/embeddings",
                    json={"model": "BAAI/bge-m3", "input": ["一", "二", "三"]},
                ),
                await client.post(
                    "/rerank",
                    json={"model": "BAAI/bge-m3", "query": "甲", "documents": ["甲"]},
                ),
            )

    health, response, wrong_model, oversized, disabled = asyncio.run(exercise())
    assert health.status_code == 200
    assert health.json()["profile"] == config.profile
    assert response.status_code == 200
    assert [item["index"] for item in response.json()["data"]] == [0, 1]
    assert len(response.json()["data"][0]["embedding"]) == 1024
    assert wrong_model.status_code == 409
    assert oversized.status_code == 413
    assert disabled.status_code == 404


def test_reranker_service_returns_ranked_original_indexes() -> None:
    config = ModelServiceConfig(
        kind="reranker",
        model_id="BAAI/bge-reranker-v2-m3",
        revision=REVISION,
        max_input_tokens=8192,
        max_batch_size=3,
    )

    async def exercise() -> tuple[Response, Response, Response]:
        transport = ASGITransport(app=create_app(config, _RerankerRuntime()))
        async with AsyncClient(transport=transport, base_url="http://reranker.test") as client:
            return (
                await client.post(
                    "/rerank",
                    json={
                        "model": "BAAI/bge-reranker-v2-m3",
                        "query": "林澈位置",
                        "documents": ["天气晴朗", "林澈位于北城", "林澈"],
                        "top_n": 2,
                        "return_documents": True,
                    },
                ),
                await client.post(
                    "/rerank",
                    json={
                        "model": "BAAI/bge-reranker-v2-m3",
                        "query": "甲",
                        "documents": ["甲"],
                        "top_n": 2,
                    },
                ),
                await client.post(
                    "/embeddings",
                    json={"model": "BAAI/bge-reranker-v2-m3", "input": "甲"},
                ),
            )

    response, invalid_top_n, disabled = asyncio.run(exercise())
    assert response.status_code == 200
    results = response.json()["results"]
    assert [item["index"] for item in results] == [1, 2]
    assert results[0]["document"] == {"text": "林澈位于北城"}
    assert invalid_top_n.status_code == 422
    assert disabled.status_code == 404
