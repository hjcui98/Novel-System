from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from novel_agent.adapters.model import (
    HttpEmbeddingProvider,
    HttpPassageReranker,
    RetrievalInferenceError,
    RetrievalModelRoute,
)
from novel_agent.domain.ids import RunId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRole,
    RetrievalInferenceCallRecord,
    RetrievalInferenceOperation,
    RetrievalInferenceStatus,
)


def _route(endpoint: str = "http://127.0.0.1:8080/v1/embeddings") -> RetrievalModelRoute:
    return RetrievalModelRoute(
        endpoint=endpoint,
        model="BAAI/bge-m3",
        revision="locked-revision",
        runtime_fingerprint="a" * 64,
        run_id=RunId("run.retrieval-test"),
        task_id=TaskId("task.retrieval-test"),
        trace_id="trace-retrieval-test",
        span_id=None,
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        timeout_seconds=3,
    )


def _client(handler: object) -> httpx.Client:
    assert callable(handler)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_route_enforces_transport_version_and_batch_role() -> None:
    assert "BAAI/bge-m3@locked-revision" in _route().profile
    assert _route("http://localhost:8080/embed").endpoint.startswith("http://localhost")
    assert _route("http://[::1]:8080/embed").endpoint.startswith("http://[")
    assert _route("https://models.example.test/embed").endpoint.startswith("https://")
    with pytest.raises(ValueError, match="absolute"):
        _route("/relative")
    with pytest.raises(ValueError, match="HTTPS"):
        _route("http://models.example.test/embed")


def test_route_rejects_empty_revision_timeout_and_wrong_batch_role() -> None:
    route = _route()
    with pytest.raises(ValueError, match="must not be empty"):
        replace(route, model="")
    with pytest.raises(ValueError, match="must not be empty"):
        replace(route, revision="")
    with pytest.raises(ValueError, match="runtime fingerprint"):
        replace(route, runtime_fingerprint="mutable")
    with pytest.raises(ValueError, match="trace id"):
        replace(route, trace_id="")
    with pytest.raises(ValueError, match="positive"):
        replace(route, timeout_seconds=0)
    with pytest.raises(ValueError, match="batch_test_model"):
        replace(route, model_role=ModelRole.IMPLEMENTATION)


def test_embedding_provider_preserves_input_order_and_profile() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        assert request.url.path == "/v1/embeddings"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [3, 4.0]},
                    {"index": 0, "embedding": [1.0, 2]},
                ]
            },
        )

    audited: list[RetrievalInferenceCallRecord] = []
    provider = HttpEmbeddingProvider(
        _route(),
        dimension=2,
        client=_client(handler),
        bearer_token="secret",
        audit_sink=audited.append,
    )
    assert provider.embed(("a", "b")) == ((1.0, 2.0), (3.0, 4.0))
    assert provider.embed(()) == ()
    assert provider.dimension == 2
    assert "dimension=2" in provider.profile
    assert "batch_size=32" in provider.profile
    assert len(provider.call_records) == 1
    assert provider.call_records[0].operation is RetrievalInferenceOperation.EMBEDDING
    assert provider.call_records[0].status is RetrievalInferenceStatus.SUCCEEDED
    assert provider.call_records[0].usage.input_items == 2
    assert provider.call_records[0].usage.output_items == 2
    assert audited == list(provider.call_records)
    with pytest.raises(ValueError, match="dimension"):
        HttpEmbeddingProvider(_route(), dimension=0)
    with pytest.raises(ValueError, match="batch size"):
        HttpEmbeddingProvider(_route(), dimension=2, batch_size=0)


def test_embedding_provider_batches_without_changing_global_order() -> None:
    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode()
        inputs = json.loads(payload)["input"]
        requests.append(inputs)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(value)]}
                    for index, value in enumerate(inputs)
                ]
            },
        )

    provider = HttpEmbeddingProvider(_route(), dimension=1, batch_size=2, client=_client(handler))

    assert provider.embed(("1", "2", "3")) == ((1.0,), (2.0,), (3.0,))
    assert requests == [["1", "2"], ["3"]]
    assert len(provider.call_records) == 2


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"data": []}, "count"),
        ({"data": ["bad"]}, "not an object"),
        ({"data": [{"index": True, "embedding": [1, 2]}]}, "index"),
        ({"data": [{"index": 0, "embedding": [1]}]}, "dimension"),
        ({"data": [{"index": 0, "embedding": [True, 2]}]}, "non-numeric"),
        (
            {"data": [{"index": 1, "embedding": [1, 2]}]},
            "incomplete",
        ),
    ],
)
def test_embedding_provider_rejects_malformed_payload(payload: object, message: str) -> None:
    provider = HttpEmbeddingProvider(
        _route(),
        dimension=2,
        client=_client(lambda request: httpx.Response(200, json=payload)),
    )
    with pytest.raises(RetrievalInferenceError, match=message):
        provider.embed(("one",))


def test_embedding_provider_rejects_duplicate_index_http_and_invalid_json() -> None:
    duplicate = HttpEmbeddingProvider(
        _route(),
        dimension=1,
        client=_client(
            lambda request: httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1]}, {"index": 0, "embedding": [2]}]},
            )
        ),
    )
    with pytest.raises(RetrievalInferenceError, match="duplicated"):
        duplicate.embed(("one", "two"))
    http_error = HttpEmbeddingProvider(
        _route(), dimension=1, client=_client(lambda request: httpx.Response(503))
    )
    with pytest.raises(RetrievalInferenceError, match="HTTP 503"):
        http_error.embed(("one",))
    invalid_json = HttpEmbeddingProvider(
        _route(),
        dimension=1,
        client=_client(lambda request: httpx.Response(200, content=b"not-json")),
    )
    with pytest.raises(RetrievalInferenceError, match="invalid JSON"):
        invalid_json.embed(("one",))
    wrong_root = HttpEmbeddingProvider(
        _route(), dimension=1, client=_client(lambda request: httpx.Response(200, json=[]))
    )
    with pytest.raises(RetrievalInferenceError, match="must be an object"):
        wrong_root.embed(("one",))

    def transport_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    transport_error = HttpEmbeddingProvider(
        _route(), dimension=1, client=_client(transport_failure)
    )
    with pytest.raises(RetrievalInferenceError, match="transport failed"):
        transport_error.embed(("one",))
    assert transport_error.call_records[0].status is RetrievalInferenceStatus.FAILED
    assert transport_error.call_records[0].error_type == "ConnectError"


def test_reranker_returns_scores_in_passage_order() -> None:
    reranker = HttpPassageReranker(
        _route("http://127.0.0.1:8080/rerank"),
        client=_client(
            lambda request: httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 1, "relevance_score": 0.2},
                        {"index": 0, "relevance_score": 0.9},
                    ]
                },
            )
        ),
    )
    assert reranker.score("query", ("first", "second")) == (0.9, 0.2)
    assert reranker.score("query", ()) == ()
    assert len(reranker.call_records) == 1
    assert reranker.call_records[0].operation is RetrievalInferenceOperation.RERANK
    assert reranker.call_records[0].status is RetrievalInferenceStatus.SUCCEEDED

    duplicate = HttpPassageReranker(
        _route("http://127.0.0.1:8080/rerank"),
        client=_client(
            lambda request: httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 0, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.2},
                    ]
                },
            )
        ),
    )
    with pytest.raises(RetrievalInferenceError, match="duplicated"):
        duplicate.score("query", ("first", "second"))

    def transport_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    transport_error = HttpPassageReranker(
        _route("http://127.0.0.1:8080/rerank"), client=_client(transport_failure)
    )
    with pytest.raises(RetrievalInferenceError, match="transport failed"):
        transport_error.score("query", ("passage",))
    assert transport_error.call_records[0].status is RetrievalInferenceStatus.FAILED


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"results": []}, "count"),
        ({"results": ["bad"]}, "not an object"),
        ({"results": [{"index": True, "relevance_score": 1}]}, "index"),
        ({"results": [{"index": 0, "relevance_score": True}]}, "not numeric"),
        ({"results": [{"index": 1, "relevance_score": 1}]}, "incomplete"),
    ],
)
def test_reranker_rejects_malformed_payload(payload: object, message: str) -> None:
    reranker = HttpPassageReranker(
        _route("http://127.0.0.1:8080/rerank"),
        client=_client(lambda request: httpx.Response(200, json=payload)),
    )
    with pytest.raises(RetrievalInferenceError, match=message):
        reranker.score("query", ("one",))
