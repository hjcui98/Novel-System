from __future__ import annotations

import pytest
from scripts.run_stage1_benchmark import (
    _database_url_from_environment,
    _validate_database_url,
    _validate_opensearch_url,
    build_parser,
)


def test_native_benchmark_parser_defaults_to_non_model_backend() -> None:
    args = build_parser().parse_args(["bundle.json", "--case-id", "case.example"])

    assert args.retrieval_backend == "in-memory"
    assert args.database_url is None
    assert args.opensearch_url.startswith("http://127.0.0.1:")


def test_native_benchmark_network_targets_are_loopback_only() -> None:
    _validate_database_url("postgresql+psycopg://user:secret@127.0.0.1:5432/database")
    target = _validate_opensearch_url("http://localhost:9200")
    assert target.hostname == "localhost"
    assert target.port == 9200

    with pytest.raises(ValueError, match="loopback PostgreSQL"):
        _validate_database_url("postgresql+psycopg://user:secret@db.example:5432/database")
    with pytest.raises(ValueError, match="bare loopback"):
        _validate_opensearch_url("http://search.example:9200")
    with pytest.raises(ValueError, match="bare loopback"):
        _validate_opensearch_url("http://127.0.0.1:9200/index")
    with pytest.raises(ValueError, match="bare loopback"):
        _validate_opensearch_url("http://user:secret@127.0.0.1:9200")


def test_database_url_is_built_from_required_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_PORT", "POSTGRES_DB"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="environment is incomplete"):
        _database_url_from_environment()

    monkeypatch.setenv("POSTGRES_USER", "novel user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret/value")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "novel agent")
    assert _database_url_from_environment() == (
        "postgresql+psycopg://novel+user:secret%2Fvalue@127.0.0.1:5432/novel+agent"
    )
