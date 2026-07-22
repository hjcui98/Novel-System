#!/usr/bin/env python3
"""Loopback-only CPU service for locked BGE embedding or reranking inference."""

from __future__ import annotations

import argparse
import importlib
import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = REPOSITORY_ROOT / "models" / "retrieval"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class EmbeddingRuntime(Protocol):
    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


class RerankerRuntime(Protocol):
    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]: ...


@dataclass(frozen=True, slots=True)
class ModelServiceConfig:
    kind: Literal["embedding", "reranker"]
    model_id: str
    revision: str
    max_input_tokens: int
    dimension: int | None = None
    max_batch_size: int = 32

    def __post_init__(self) -> None:
        if not self.model_id.startswith("BAAI/"):
            raise ValueError("retrieval service only accepts locked BAAI model ids")
        if REVISION_PATTERN.fullmatch(self.revision) is None:
            raise ValueError("retrieval service revision must be a full commit hash")
        if self.max_input_tokens < 1 or self.max_batch_size < 1:
            raise ValueError("retrieval service limits must be positive")
        if self.kind == "embedding" and self.dimension != 1024:
            raise ValueError("BGE-M3 embedding service must expose 1024 dimensions")
        if self.kind == "reranker" and self.dimension is not None:
            raise ValueError("reranker service must not declare an embedding dimension")

    @property
    def profile(self) -> str:
        dimension = "" if self.dimension is None else f";dimension={self.dimension}"
        return (
            f"{self.model_id}@{self.revision};task={self.kind};device=cpu;dtype=float32;"
            f"max_input_tokens={self.max_input_tokens};normalize=true{dimension}"
        )


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: str
    input: str | list[str]
    encoding_format: Literal["float"] = "float"

    @field_validator("input")
    @classmethod
    def validate_input(cls, value: str | list[str]) -> str | list[str]:
        values = [value] if isinstance(value, str) else value
        if not values or any(not item.strip() for item in values):
            raise ValueError("embedding input must contain non-empty text")
        return value


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: str
    query: str = Field(min_length=1)
    documents: list[str]
    top_n: int | None = None
    return_documents: bool = False

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rerank query must not be blank")
        return value

    @field_validator("documents")
    @classmethod
    def validate_documents(cls, value: list[str]) -> list[str]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("rerank documents must contain non-empty text")
        return value


def create_app(
    config: ModelServiceConfig,
    runtime: EmbeddingRuntime | RerankerRuntime,
) -> FastAPI:
    app = FastAPI(title=f"Novel Agent locked {config.kind} model", version="1")

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "kind": config.kind,
            "model": config.model_id,
            "revision": config.revision,
            "profile": config.profile,
            "max_batch_size": config.max_batch_size,
        }

    async def embeddings(request: EmbeddingRequest) -> dict[str, object]:
        if config.kind != "embedding":
            raise HTTPException(status_code=404, detail="embedding route is disabled")
        _validate_requested_model(config, request.model)
        texts = (request.input,) if isinstance(request.input, str) else tuple(request.input)
        if len(texts) > config.max_batch_size:
            raise HTTPException(status_code=413, detail="embedding batch exceeds locked limit")
        vectors = runtime.embed(texts)  # type: ignore[union-attr]
        if len(vectors) != len(texts) or any(len(vector) != config.dimension for vector in vectors):
            raise HTTPException(
                status_code=500, detail="embedding runtime violated output contract"
            )
        if any(not math.isfinite(value) for vector in vectors for value in vector):
            raise HTTPException(
                status_code=500, detail="embedding runtime returned non-finite data"
            )
        return {
            "object": "list",
            "model": config.model_id,
            "data": [
                {"object": "embedding", "index": index, "embedding": vector}
                for index, vector in enumerate(vectors)
            ],
        }

    async def rerank(request: RerankRequest) -> dict[str, object]:
        if config.kind != "reranker":
            raise HTTPException(status_code=404, detail="rerank route is disabled")
        _validate_requested_model(config, request.model)
        if len(request.documents) > config.max_batch_size:
            raise HTTPException(status_code=413, detail="rerank batch exceeds locked limit")
        top_n = len(request.documents) if request.top_n is None else request.top_n
        if not 1 <= top_n <= len(request.documents):
            raise HTTPException(status_code=422, detail="top_n is outside the document range")
        passages = tuple(request.documents)
        scores = runtime.score(request.query, passages)  # type: ignore[union-attr]
        if len(scores) != len(passages) or any(not math.isfinite(score) for score in scores):
            raise HTTPException(status_code=500, detail="reranker runtime violated output contract")
        ordered = sorted(enumerate(scores), key=lambda item: (-item[1], item[0]))[:top_n]
        results: list[dict[str, object]] = []
        for index, score in ordered:
            item: dict[str, object] = {"index": index, "relevance_score": score}
            if request.return_documents:
                item["document"] = {"text": passages[index]}
            results.append(item)
        return {"id": f"{config.model_id}@{config.revision}", "results": results}

    app.post("/embeddings")(embeddings)
    app.post("/v1/embeddings")(embeddings)
    app.post("/rerank")(rerank)
    app.post("/v1/rerank")(rerank)
    return app


def _validate_requested_model(config: ModelServiceConfig, requested: str) -> None:
    if requested != config.model_id:
        raise HTTPException(status_code=409, detail="requested model differs from locked service")


class SentenceTransformerEmbeddingRuntime:
    def __init__(self, model_dir: Path, *, max_input_tokens: int) -> None:
        module = importlib.import_module("sentence_transformers")
        sentence_transformer = module.SentenceTransformer
        self._model = sentence_transformer(
            str(model_dir),
            device="cpu",
            trust_remote_code=False,
            local_files_only=True,
        )
        self._model.max_seq_length = max_input_tokens
        self._lock = threading.Lock()

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        with self._lock:
            vectors = self._model.encode(
                list(texts),
                batch_size=len(texts),
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
        return tuple(tuple(float(value) for value in vector) for vector in vectors)


class TransformersRerankerRuntime:
    def __init__(self, model_dir: Path, *, max_input_tokens: int) -> None:
        transformers = importlib.import_module("transformers")
        self._torch = importlib.import_module("torch")
        tokenizer_type = transformers.AutoTokenizer
        model_type = transformers.AutoModelForSequenceClassification
        self._tokenizer = tokenizer_type.from_pretrained(
            str(model_dir), trust_remote_code=False, local_files_only=True
        )
        self._model = model_type.from_pretrained(
            str(model_dir),
            trust_remote_code=False,
            local_files_only=True,
            use_safetensors=True,
        )
        self._model.eval()
        self._max_input_tokens = max_input_tokens
        self._lock = threading.Lock()

    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        pairs = [[query, passage] for passage in passages]
        with self._lock:
            inputs = self._tokenizer(
                pairs,
                padding=True,
                truncation=True,
                max_length=self._max_input_tokens,
                return_tensors="pt",
            )
            with self._torch.inference_mode():
                logits = self._model(**inputs, return_dict=True).logits.reshape(-1).float()
                scores = self._torch.sigmoid(logits).cpu().tolist()
        return tuple(float(value) for value in scores)


def _validated_model_directory(value: str) -> Path:
    path = Path(value).resolve(strict=True)
    root = MODEL_ROOT.resolve()
    if not path.is_dir() or not path.is_relative_to(root):
        raise ValueError("model directory must be inside the repository model root")
    if path.stat().st_uid != os.getuid():
        raise ValueError("model directory is not owned by the current user")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=("embedding", "reranker"))
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--max-input-tokens", type=int, required=True)
    parser.add_argument("--max-batch-size", type=int, default=32)
    parser.add_argument("--dimension", type=int)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.host not in LOOPBACK_HOSTS:
        raise SystemExit("retrieval model service may only bind to loopback")
    if not 1 <= args.port <= 65535:
        raise SystemExit("retrieval model service port is invalid")
    dimension = 1024 if args.kind == "embedding" and args.dimension is None else args.dimension
    config = ModelServiceConfig(
        kind=args.kind,
        model_id=args.model_id,
        revision=args.revision,
        max_input_tokens=args.max_input_tokens,
        dimension=dimension,
        max_batch_size=args.max_batch_size,
    )
    model_dir = _validated_model_directory(args.model_dir)
    threads = int(os.getenv("NOVEL_AGENT_MODEL_THREADS", str(min(os.cpu_count() or 1, 32))))
    if threads < 1:
        raise SystemExit("NOVEL_AGENT_MODEL_THREADS must be positive")
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(threads))
    if args.kind == "embedding":
        runtime: EmbeddingRuntime | RerankerRuntime = SentenceTransformerEmbeddingRuntime(
            model_dir, max_input_tokens=args.max_input_tokens
        )
    else:
        runtime = TransformersRerankerRuntime(model_dir, max_input_tokens=args.max_input_tokens)
    uvicorn = importlib.import_module("uvicorn")
    uvicorn.run(create_app(config, runtime), host=args.host, port=args.port, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
