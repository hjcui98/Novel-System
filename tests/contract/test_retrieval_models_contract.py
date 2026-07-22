from __future__ import annotations

import hashlib
import io
import json
import urllib.request
from pathlib import Path, PurePosixPath
from typing import cast

import pytest
from scripts.native_infra import NativeInfraError
from scripts.native_models import (
    LOCK_PATH,
    LockedModelFile,
    _download_file,
    load_model_lock,
    model_file_url,
    selected_models,
)


def test_model_lock_pins_publisher_revision_and_every_file_sha256() -> None:
    lock = load_model_lock()

    assert lock.architecture == "x86_64"
    assert set(lock.models) == {"embedding", "reranker"}
    assert "huggingface.co" in lock.allowed_redirect_hosts
    assert "us.aws.cdn.hf.co" in lock.allowed_redirect_hosts
    for model in lock.models.values():
        assert model.model_id.startswith("BAAI/")
        assert len(model.revision) == 40
        assert model.max_input_tokens == 8192
        assert model.normalize is True
        assert model.profile.startswith(f"{model.model_id}@{model.revision}")
        assert len(model.runtime_fingerprint) == 64
        for file in model.files:
            assert len(file.sha256) == 64
            assert file.size > 0
            assert model_file_url(model, file).startswith(
                f"https://huggingface.co/{model.model_id}/resolve/{model.revision}/"
            )
    assert lock.models["embedding"].dimension == 1024
    assert any(file.path.name == "pytorch_model.bin" for file in lock.models["embedding"].files)
    assert any(file.path.name == "model.safetensors" for file in lock.models["reranker"].files)


def test_model_lock_rejects_mutable_revision_and_unsafe_path(tmp_path: Path) -> None:
    document = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    document["models"]["embedding"]["revision"] = "main"
    target = tmp_path / "mutable.lock"
    target.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(NativeInfraError, match="full commit hash"):
        load_model_lock(target)

    document = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    document["models"]["embedding"]["files"][0]["path"] = "../escape"
    target.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(NativeInfraError, match="unsafe or duplicate"):
        load_model_lock(target)


def test_model_selection_is_explicit_and_deduplicated() -> None:
    lock = load_model_lock()
    assert [model.key for model in selected_models(lock, None)] == ["embedding", "reranker"]
    assert [model.key for model in selected_models(lock, ["reranker", "reranker"])] == ["reranker"]


def test_locked_downloader_resumes_an_early_eof_before_publish(tmp_path: Path) -> None:
    payload = b"locked-model-payload"
    locked = LockedModelFile(
        path=PurePosixPath("model.bin"),
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    class Response(io.BytesIO):
        def __init__(self, content: bytes, status: int) -> None:
            super().__init__(content)
            self.status = status

    class Opener:
        def __init__(self) -> None:
            self.requests: list[urllib.request.Request] = []

        def open(self, request: urllib.request.Request, timeout: int) -> Response:
            assert timeout == 60
            self.requests.append(request)
            if len(self.requests) == 1:
                return Response(payload[:7], 200)
            assert request.headers["Range"] == "bytes=7-"
            return Response(payload[7:], 206)

    opener = Opener()
    destination = tmp_path / "model.bin"
    _download_file(
        cast(urllib.request.OpenerDirector, opener),
        "https://huggingface.co/locked",
        destination,
        locked,
    )

    assert destination.read_bytes() == payload
    assert len(opener.requests) == 2
    assert not (tmp_path / ".model.bin.part").exists()
