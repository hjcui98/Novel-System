from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import pytest
from scripts.native_infra import (
    NativeInfraError,
    assert_safe_archive,
    load_lock,
    update_env_file,
)

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_native_lock_pins_official_https_artifacts() -> None:
    raw = json.loads((REPOSITORY_ROOT / "infra/native-services.lock").read_text())
    assert raw["postgresql"] == {
        "version": "17.10",
        "conda_channel": "conda-forge",
        "conda_build": "he06dc6e_0",
    }
    locks = load_lock()
    assert set(locks) == {"opensearch", "minio", "otel"}
    for lock in locks.values():
        assert lock.url.startswith("https://")
        assert len(lock.sha256) == 64
        assert all(character in "0123456789abcdef" for character in lock.sha256)
    assert locks["opensearch"].official_sha512 is not None
    assert len(locks["opensearch"].official_sha512 or "") == 128


def test_native_template_contains_no_development_secret() -> None:
    template = (REPOSITORY_ROOT / ".env.example").read_text().lower()
    assert "generate_at_bootstrap" in template
    assert "change-me" not in template
    assert "minioadmin" not in template


def test_env_writer_enforces_private_mode(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("KEEP=value\nSECRET=old\n")
    os.chmod(target, 0o644)

    update_env_file(target, {"SECRET": "new", "ADDED": "value"})

    assert target.read_text().splitlines() == ["KEEP=value", "SECRET=new", "ADDED=value"]
    assert target.stat().st_mode & 0o777 == 0o600


def test_archive_preflight_rejects_parent_traversal() -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        member = tarfile.TarInfo("../escape")
        member.size = 0
        archive.addfile(member)
    payload.seek(0)

    with (
        tarfile.open(fileobj=payload, mode="r") as archive,
        pytest.raises(NativeInfraError, match="unsafe archive member"),
    ):
        assert_safe_archive(archive)


def test_makefile_defaults_to_native_with_explicit_docker_route() -> None:
    makefile = (REPOSITORY_ROOT / "Makefile").read_text()
    assert "INFRA_BACKEND ?= native" in makefile
    assert '"$(PYTHON)" scripts/native_infra.py up' in makefile
    assert 'test "$(INFRA_BACKEND)" = "docker"' in makefile


def test_native_otel_disables_the_implicit_shared_metrics_listener() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "native_infra.py").read_text()

    assert "telemetry:\n    metrics:\n      level: none\n      readers: []" in script
