from __future__ import annotations

from pathlib import Path

from yaml import safe_load

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_compose_declares_stage_zero_services_with_pinned_default_images() -> None:
    document = safe_load((REPOSITORY_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = document["services"]

    assert set(services) == {"postgres", "opensearch", "minio", "otel-collector"}
    for service in services.values():
        assert ":latest" not in service["image"]
        assert "healthcheck" in service
        assert service["restart"] == "unless-stopped"


def test_authoritative_and_derived_stores_use_separate_named_volumes() -> None:
    document = safe_load((REPOSITORY_ROOT / "compose.yaml").read_text(encoding="utf-8"))

    assert set(document["volumes"]) == {"postgres-data", "opensearch-data", "minio-data"}
    assert document["services"]["postgres"]["volumes"] == ["postgres-data:/var/lib/postgresql/data"]
    assert document["services"]["opensearch"]["volumes"] == [
        "opensearch-data:/usr/share/opensearch/data"
    ]
    assert document["services"]["minio"]["volumes"] == ["minio-data:/data"]


def test_opentelemetry_accepts_all_signal_types_and_exposes_health() -> None:
    document = safe_load(
        (REPOSITORY_ROOT / "infra" / "otel-collector.yaml").read_text(encoding="utf-8")
    )

    assert document["extensions"]["health_check"]["endpoint"] == "0.0.0.0:13133"
    assert set(document["service"]["pipelines"]) == {"traces", "metrics", "logs"}
    for pipeline in document["service"]["pipelines"].values():
        assert pipeline["receivers"] == ["otlp"]
        assert pipeline["exporters"] == ["debug"]
