"""Restricted HTTP adapters for versioned embedding and passage-rerank services."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from urllib.parse import urlparse

import httpx

from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRole,
    RetrievalInferenceCallRecord,
    RetrievalInferenceOperation,
    RetrievalInferenceStatus,
    RetrievalInferenceUsage,
)


class RetrievalInferenceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RetrievalModelRoute:
    endpoint: str
    model: str
    revision: str
    runtime_fingerprint: str
    run_id: RunId
    task_id: TaskId
    trace_id: str
    span_id: str | None
    model_role: ModelRole
    purpose: ModelCallPurpose
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("retrieval model endpoint must be an absolute HTTP(S) URL")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("non-loopback retrieval model endpoints must use HTTPS")
        if not self.model or not self.revision:
            raise ValueError("retrieval model and revision must not be empty")
        if re.fullmatch(r"[0-9a-f]{64}", self.runtime_fingerprint) is None:
            raise ValueError("retrieval model runtime fingerprint must be a SHA-256 hex digest")
        if not self.trace_id:
            raise ValueError("retrieval model trace id must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("retrieval model timeout must be positive")
        if (
            self.purpose in {ModelCallPurpose.BATCH_TEST, ModelCallPurpose.EVALUATION}
            and self.model_role is not ModelRole.BATCH_TEST
        ):
            raise ValueError("batch retrieval inference must use batch_test_model")

    @property
    def profile(self) -> str:
        host = urlparse(self.endpoint).hostname or "unknown"
        return (
            f"{self.model}@{self.revision};role={self.model_role.value};"
            f"purpose={self.purpose.value};host={host};runtime={self.runtime_fingerprint}"
        )


class _RetrievalCallAuditor:
    def __init__(
        self,
        route: RetrievalModelRoute,
        audit_sink: Callable[[RetrievalInferenceCallRecord], None] | None = None,
    ) -> None:
        self._audit_route = route
        self._audit_sink = audit_sink
        self._records: list[RetrievalInferenceCallRecord] = []
        self._sequence = 0

    @property
    def call_records(self) -> tuple[RetrievalInferenceCallRecord, ...]:
        return tuple(self._records)

    def record(
        self,
        operation: RetrievalInferenceOperation,
        *,
        input_items: int,
        input_characters: int,
        output_items: int,
        started_at: datetime,
        started_clock: float,
        status: RetrievalInferenceStatus,
        error_type: str | None = None,
    ) -> None:
        completed_at = datetime.now(UTC)
        self._sequence += 1
        identity = (
            f"{self._audit_route.run_id.root}\0{self._audit_route.task_id.root}\0"
            f"{operation.value}\0{self._sequence}\0{started_at.isoformat()}"
        )
        record = RetrievalInferenceCallRecord(
            call_id=StableId(f"retrieval-call.{hashlib.sha256(identity.encode()).hexdigest()}"),
            run_id=self._audit_route.run_id,
            task_id=self._audit_route.task_id,
            model_role=self._audit_route.model_role,
            purpose=self._audit_route.purpose,
            trace_id=self._audit_route.trace_id,
            span_id=self._audit_route.span_id,
            endpoint=self._audit_route.endpoint,
            model=self._audit_route.model,
            revision=self._audit_route.revision,
            runtime_fingerprint=self._audit_route.runtime_fingerprint,
            operation=operation,
            usage=RetrievalInferenceUsage(
                input_items=input_items,
                input_characters=input_characters,
                output_items=output_items,
                cost_usd=Decimal("0"),
            ),
            latency_ms=max(0, round((monotonic() - started_clock) * 1000)),
            started_at=started_at,
            completed_at=completed_at,
            status=status,
            error_type=error_type,
        )
        self._records.append(record)
        if self._audit_sink is not None:
            self._audit_sink(record)


class HttpEmbeddingProvider(_RetrievalCallAuditor):
    """OpenAI-compatible `/embeddings` client with strict response validation."""

    def __init__(
        self,
        route: RetrievalModelRoute,
        *,
        dimension: int,
        batch_size: int = 32,
        client: httpx.Client | None = None,
        bearer_token: str | None = None,
        audit_sink: Callable[[RetrievalInferenceCallRecord], None] | None = None,
    ) -> None:
        if dimension < 1:
            raise ValueError("embedding dimension must be positive")
        if batch_size < 1:
            raise ValueError("embedding batch size must be positive")
        super().__init__(route, audit_sink)
        self._route = route
        self._batch_size = batch_size
        self.dimension = dimension
        self.profile = f"{route.profile};dimension={dimension};batch_size={batch_size}"
        self._headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else None
        self._client = client or httpx.Client()

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        vectors: list[tuple[float, ...]] = []
        for offset in range(0, len(texts), self._batch_size):
            vectors.extend(self._embed_batch(texts[offset : offset + self._batch_size]))
        return tuple(vectors)

    def _embed_batch(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        started_at = datetime.now(UTC)
        started_clock = monotonic()
        try:
            response = self._client.post(
                self._route.endpoint,
                json={
                    "model": self._route.model,
                    "input": list(texts),
                    "encoding_format": "float",
                },
                headers=self._headers,
                timeout=self._route.timeout_seconds,
            )
            self._raise_for_status(response)
            payload = self._json_object(response)
            data = payload.get("data")
            if not isinstance(data, list) or len(data) != len(texts):
                raise RetrievalInferenceError("embedding response count differs from input count")
            indexed: dict[int, tuple[float, ...]] = {}
            for item in data:
                if not isinstance(item, dict):
                    raise RetrievalInferenceError("embedding response item is not an object")
                index = item.get("index")
                vector = item.get("embedding")
                if not isinstance(index, int) or isinstance(index, bool) or index in indexed:
                    raise RetrievalInferenceError(
                        "embedding response index is invalid or duplicated"
                    )
                if not isinstance(vector, list) or len(vector) != self.dimension:
                    raise RetrievalInferenceError("embedding response dimension mismatch")
                if any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    for value in vector
                ):
                    raise RetrievalInferenceError("embedding vector contains a non-numeric value")
                indexed[index] = tuple(float(value) for value in vector)
            if set(indexed) != set(range(len(texts))):
                raise RetrievalInferenceError("embedding response indexes are incomplete")
            result = tuple(indexed[index] for index in range(len(texts)))
        except Exception as error:
            self.record(
                RetrievalInferenceOperation.EMBEDDING,
                input_items=len(texts),
                input_characters=sum(map(len, texts)),
                output_items=0,
                started_at=started_at,
                started_clock=started_clock,
                status=RetrievalInferenceStatus.FAILED,
                error_type=type(error).__name__,
            )
            if isinstance(error, RetrievalInferenceError):
                raise
            raise RetrievalInferenceError("embedding transport failed") from error
        self.record(
            RetrievalInferenceOperation.EMBEDDING,
            input_items=len(texts),
            input_characters=sum(map(len, texts)),
            output_items=len(result),
            started_at=started_at,
            started_clock=started_clock,
            status=RetrievalInferenceStatus.SUCCEEDED,
        )
        return result

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise RetrievalInferenceError(
                f"retrieval inference returned HTTP {response.status_code}"
            ) from error

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as error:
            raise RetrievalInferenceError("retrieval inference returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise RetrievalInferenceError("retrieval inference response must be an object")
        return payload


class HttpPassageReranker(_RetrievalCallAuditor):
    """Cohere/Jina-style `/rerank` client returning scores in input order."""

    def __init__(
        self,
        route: RetrievalModelRoute,
        *,
        client: httpx.Client | None = None,
        bearer_token: str | None = None,
        audit_sink: Callable[[RetrievalInferenceCallRecord], None] | None = None,
    ) -> None:
        super().__init__(route, audit_sink)
        self._route = route
        self.profile = route.profile
        self._headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else None
        self._client = client or httpx.Client()

    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        if not passages:
            return ()
        started_at = datetime.now(UTC)
        started_clock = monotonic()
        try:
            response = self._client.post(
                self._route.endpoint,
                json={
                    "model": self._route.model,
                    "query": query,
                    "documents": list(passages),
                    "top_n": len(passages),
                    "return_documents": False,
                },
                headers=self._headers,
                timeout=self._route.timeout_seconds,
            )
            HttpEmbeddingProvider._raise_for_status(response)
            payload = HttpEmbeddingProvider._json_object(response)
            results = payload.get("results")
            if not isinstance(results, list) or len(results) != len(passages):
                raise RetrievalInferenceError("rerank response count differs from passage count")
            scores: dict[int, float] = {}
            for item in results:
                if not isinstance(item, dict):
                    raise RetrievalInferenceError("rerank response item is not an object")
                index = item.get("index")
                score = item.get("relevance_score")
                if not isinstance(index, int) or isinstance(index, bool) or index in scores:
                    raise RetrievalInferenceError("rerank response index is invalid or duplicated")
                if isinstance(score, bool) or not isinstance(score, (int, float)):
                    raise RetrievalInferenceError("rerank score is not numeric")
                scores[index] = float(score)
            if set(scores) != set(range(len(passages))):
                raise RetrievalInferenceError("rerank response indexes are incomplete")
            result = tuple(scores[index] for index in range(len(passages)))
        except Exception as error:
            self.record(
                RetrievalInferenceOperation.RERANK,
                input_items=1 + len(passages),
                input_characters=len(query) + sum(map(len, passages)),
                output_items=0,
                started_at=started_at,
                started_clock=started_clock,
                status=RetrievalInferenceStatus.FAILED,
                error_type=type(error).__name__,
            )
            if isinstance(error, RetrievalInferenceError):
                raise
            raise RetrievalInferenceError("reranker transport failed") from error
        self.record(
            RetrievalInferenceOperation.RERANK,
            input_items=1 + len(passages),
            input_characters=len(query) + sum(map(len, passages)),
            output_items=len(result),
            started_at=started_at,
            started_clock=started_clock,
            status=RetrievalInferenceStatus.SUCCEEDED,
        )
        return result
