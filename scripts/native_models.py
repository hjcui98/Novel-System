#!/usr/bin/env python3
"""Secure downloader and lifecycle manager for locked local retrieval models."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

try:
    from scripts.native_infra import (
        NativeInfraError,
        assert_port_free,
        ensure_private_directory,
        is_process_alive,
        process_command,
        process_owner,
        process_start_time,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from native_infra import (  # type: ignore[import-not-found,no-redef]
        NativeInfraError,
        assert_port_free,
        ensure_private_directory,
        is_process_alive,
        process_command,
        process_owner,
        process_start_time,
    )

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPOSITORY_ROOT / "infra" / "retrieval-models.lock"
MODEL_ROOT = REPOSITORY_ROOT / "models" / "retrieval"
RUN_ROOT = REPOSITORY_ROOT / "tmp" / "native-models" / "run"
LOG_ROOT = REPOSITORY_ROOT / "tmp" / "native-models" / "logs"
SERVICE_SCRIPT = REPOSITORY_ROOT / "scripts" / "retrieval_model_service.py"
PYTHON = REPOSITORY_ROOT / ".conda-env" / "bin" / "python"
LOOPBACK = "127.0.0.1"
MODEL_KEYS = ("embedding", "reranker")
DEFAULT_PORTS = {"embedding": 8081, "reranker": 8082}
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class LockedModelFile:
    path: PurePosixPath
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class LockedModel:
    key: str
    model_id: str
    revision: str
    task: str
    license: str
    max_input_tokens: int
    normalize: bool
    files: tuple[LockedModelFile, ...]
    dimension: int | None = None

    @property
    def directory_name(self) -> str:
        return f"{self.key}-{self.revision[:12]}"

    @property
    def profile(self) -> str:
        dimension = "" if self.dimension is None else f";dimension={self.dimension}"
        return (
            f"{self.model_id}@{self.revision};task={self.task};device=cpu;dtype=float32;"
            f"max_input_tokens={self.max_input_tokens};normalize={str(self.normalize).lower()}"
            f"{dimension}"
        )

    @property
    def runtime_fingerprint(self) -> str:
        return hashlib.sha256(self.profile.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RetrievalModelLock:
    architecture: str
    allowed_redirect_hosts: tuple[str, ...]
    models: dict[str, LockedModel]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_model_lock(path: Path = LOCK_PATH) -> RetrievalModelLock:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise NativeInfraError(f"cannot read retrieval model lock: {error}") from error
    if document.get("schema_version") != 1 or document.get("architecture") != "x86_64":
        raise NativeInfraError("retrieval model lock schema or architecture is unsupported")
    allowed_hosts = tuple(document.get("allowed_redirect_hosts", ()))
    if not allowed_hosts or any(not _safe_hostname(host) for host in allowed_hosts):
        raise NativeInfraError("retrieval model redirect host allowlist is invalid")
    raw_models = document.get("models")
    if not isinstance(raw_models, dict) or set(raw_models) != set(MODEL_KEYS):
        raise NativeInfraError("retrieval model lock must contain embedding and reranker")
    models: dict[str, LockedModel] = {}
    for key in MODEL_KEYS:
        raw = raw_models[key]
        model_id = raw.get("model_id")
        revision = raw.get("revision")
        if not isinstance(model_id, str) or not model_id.startswith("BAAI/"):
            raise NativeInfraError(f"{key} model must come from the BAAI publisher namespace")
        if not isinstance(revision, str) or HEX_40.fullmatch(revision) is None:
            raise NativeInfraError(f"{key} revision must be a full commit hash")
        files = _load_files(key, raw.get("files"))
        dimension = raw.get("dimension")
        if dimension is not None and (
            isinstance(dimension, bool) or not isinstance(dimension, int)
        ):
            raise NativeInfraError(f"{key} dimension must be an integer")
        model = LockedModel(
            key=key,
            model_id=model_id,
            revision=revision,
            task=str(raw.get("task")),
            license=str(raw.get("license")),
            max_input_tokens=int(raw.get("max_input_tokens", 0)),
            normalize=raw.get("normalize") is True,
            files=files,
            dimension=dimension,
        )
        _validate_model(model)
        models[key] = model
    return RetrievalModelLock(
        architecture="x86_64",
        allowed_redirect_hosts=allowed_hosts,
        models=models,
    )


def _load_files(key: str, raw_files: Any) -> tuple[LockedModelFile, ...]:
    if not isinstance(raw_files, list) or not raw_files:
        raise NativeInfraError(f"{key} file lock must be a non-empty list")
    files: list[LockedModelFile] = []
    seen: set[PurePosixPath] = set()
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise NativeInfraError(f"{key} file lock entry must be an object")
        path = PurePosixPath(str(raw.get("path", "")))
        size = raw.get("size")
        sha256 = raw.get("sha256")
        if path.is_absolute() or not path.parts or ".." in path.parts or path in seen:
            raise NativeInfraError(f"{key} contains an unsafe or duplicate file path: {path}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise NativeInfraError(f"{key}/{path} has an invalid size")
        if not isinstance(sha256, str) or HEX_64.fullmatch(sha256) is None:
            raise NativeInfraError(f"{key}/{path} has an invalid SHA-256")
        files.append(LockedModelFile(path=path, size=size, sha256=sha256))
        seen.add(path)
    return tuple(files)


def _validate_model(model: LockedModel) -> None:
    if model.task != model.key:
        raise NativeInfraError(f"{model.key} task does not match lock key")
    if not model.license or model.max_input_tokens < 1 or not model.normalize:
        raise NativeInfraError(f"{model.key} runtime metadata is incomplete")
    names = {str(item.path) for item in model.files}
    required = {"config.json", "tokenizer.json", "tokenizer_config.json"}
    if not required.issubset(names):
        raise NativeInfraError(f"{model.key} lock omits required runtime files")
    if model.key == "embedding":
        if model.dimension != 1024 or "pytorch_model.bin" not in names:
            raise NativeInfraError("embedding lock must pin the 1024d BGE-M3 weights")
    elif "model.safetensors" not in names:
        raise NativeInfraError("reranker lock must use safetensors weights")


def _safe_hostname(host: str) -> bool:
    return bool(host) and host == host.lower() and "/" not in host and ".." not in host


def model_directory(model: LockedModel) -> Path:
    return MODEL_ROOT / model.directory_name


def model_file_url(model: LockedModel, file: LockedModelFile) -> str:
    return (
        f"https://huggingface.co/{model.model_id}/resolve/{model.revision}/"
        f"{quote(str(file.path), safe='/')}"
    )


class RestrictedModelRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: tuple[str, ...]) -> None:
        self._allowed_hosts = frozenset(allowed_hosts)
        super().__init__()

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in self._allowed_hosts:
            raise NativeInfraError(
                f"model download redirect escaped locked HTTPS hosts: {parsed.hostname}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def bootstrap_model(lock: RetrievalModelLock, model: LockedModel) -> Path:
    root = ensure_private_directory(MODEL_ROOT, MODEL_ROOT.parent)
    target = ensure_private_directory(model_directory(model), root)
    opener = urllib.request.build_opener(
        RestrictedModelRedirectHandler(lock.allowed_redirect_hosts)
    )
    for file in model.files:
        destination = target.joinpath(*file.path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists():
            _verify_file(destination, file)
            print(f"verified {model.key}/{file.path}", file=sys.stderr, flush=True)
            continue
        print(
            f"downloading {model.key}/{file.path} ({file.size} bytes)",
            file=sys.stderr,
            flush=True,
        )
        _download_file(opener, model_file_url(model, file), destination, file)
        print(f"verified {model.key}/{file.path}", file=sys.stderr, flush=True)
    verify_model(model)
    return target


def _download_file(
    opener: urllib.request.OpenerDirector,
    url: str,
    destination: Path,
    locked: LockedModelFile,
) -> None:
    partial = destination.with_name(f".{destination.name}.part")
    for _ in range(20):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == locked.size:
            break
        if offset > locked.size:
            raise NativeInfraError(f"partial model file is larger than lock: {partial}")
        headers = {"User-Agent": "novel-agent-locked-model-bootstrap/1"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(request, timeout=60) as response:
                status = getattr(response, "status", 200)
                if offset and status not in {200, 206}:
                    raise NativeInfraError(f"model host refused safe resume for {locked.path}")
                mode = "ab" if offset and status == 206 else "wb"
                before = offset if mode == "ab" else 0
                with partial.open(mode) as stream:
                    while True:
                        chunk = response.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        stream.write(chunk)
                if partial.stat().st_size <= before:
                    raise NativeInfraError(f"model download made no progress for {locked.path}")
                if partial.stat().st_size == locked.size:
                    break
        except (OSError, urllib.error.URLError) as error:
            raise NativeInfraError(
                f"failed downloading locked model file {locked.path}: {error}"
            ) from error
    else:
        raise NativeInfraError(f"model download exceeded retry limit for {locked.path}")
    _verify_file(partial, locked)
    os.replace(partial, destination)
    os.chmod(destination, 0o600)


def _verify_file(path: Path, locked: LockedModelFile) -> None:
    actual_size = path.stat().st_size
    if actual_size != locked.size:
        raise NativeInfraError(
            f"model file size mismatch for {locked.path}: {actual_size} != {locked.size}"
        )
    actual_sha = sha256_file(path)
    if actual_sha != locked.sha256:
        raise NativeInfraError(f"model file SHA-256 mismatch for {locked.path}")


def verify_model(model: LockedModel) -> Path:
    directory = model_directory(model)
    if not directory.is_dir():
        raise NativeInfraError(f"locked model directory is missing: {directory}")
    for file in model.files:
        target = directory.joinpath(*file.path.parts)
        if not target.is_file():
            raise NativeInfraError(f"locked model file is missing: {target}")
        _verify_file(target, file)
    return directory


def configured_port(key: str) -> int:
    name = f"NOVEL_AGENT_{key.upper()}_MODEL_PORT"
    value = os.getenv(name, str(DEFAULT_PORTS[key]))
    try:
        port = int(value)
    except ValueError as error:
        raise NativeInfraError(f"{name} must be an integer") from error
    if not 1 <= port <= 65535:
        raise NativeInfraError(f"{name} is outside the TCP port range")
    return port


def _pid_path(key: str) -> Path:
    return RUN_ROOT / f"{key}.json"


def start_model(model: LockedModel) -> None:
    verify_model(model)
    if not PYTHON.is_file():
        raise NativeInfraError(f"project Python is missing: {PYTHON}")
    ensure_private_directory(RUN_ROOT, RUN_ROOT.parent)
    ensure_private_directory(LOG_ROOT, LOG_ROOT.parent)
    pid_path = _pid_path(model.key)
    if pid_path.exists():
        record = _load_pid_record(pid_path)
        if _record_matches_live_process(model, record):
            return
        raise NativeInfraError(f"stale or foreign PID record requires inspection: {pid_path}")
    port = configured_port(model.key)
    assert_port_free(port)
    command = (
        str(PYTHON),
        str(SERVICE_SCRIPT),
        "--kind",
        model.key,
        "--model-dir",
        str(model_directory(model)),
        "--model-id",
        model.model_id,
        "--revision",
        model.revision,
        "--max-input-tokens",
        str(model.max_input_tokens),
        "--host",
        LOOPBACK,
        "--port",
        str(port),
    )
    log_path = LOG_ROOT / f"{model.key}.log"
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env={
                **os.environ,
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "TOKENIZERS_PARALLELISM": "false",
            },
        )
    record = {
        "pid": process.pid,
        "start_time": process_start_time(process.pid),
        "uid": os.getuid(),
        "command": list(command),
        "model_id": model.model_id,
        "revision": model.revision,
        "port": port,
    }
    pid_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(pid_path, 0o600)
    try:
        _wait_for_health(model, timeout=300.0)
    except NativeInfraError:
        stop_model(model)
        raise


def stop_model(model: LockedModel) -> None:
    pid_path = _pid_path(model.key)
    if not pid_path.exists():
        return
    record = _load_pid_record(pid_path)
    if not _record_matches_live_process(model, record):
        if not is_process_alive(int(record["pid"])):
            pid_path.unlink()
            return
        raise NativeInfraError(f"refusing to signal unverified model PID {record['pid']}")
    pid = int(record["pid"])
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and is_process_alive(pid):
        time.sleep(0.2)
    if is_process_alive(pid):
        os.kill(pid, signal.SIGKILL)
    pid_path.unlink()


def _load_pid_record(path: Path) -> dict[str, Any]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise TypeError
        return record
    except (OSError, ValueError, TypeError) as error:
        raise NativeInfraError(f"invalid model PID record: {path}") from error


def _record_matches_live_process(model: LockedModel, record: dict[str, Any]) -> bool:
    try:
        pid = int(record["pid"])
        expected_start = int(record["start_time"])
        command = tuple(str(value) for value in record["command"])
    except (KeyError, TypeError, ValueError):
        return False
    if not is_process_alive(pid):
        return False
    try:
        return (
            process_owner(pid) == os.getuid()
            and process_start_time(pid) == expected_start
            and process_command(pid) == command
            and str(SERVICE_SCRIPT) in command
            and model.model_id in command
            and model.revision in command
        )
    except (FileNotFoundError, NativeInfraError):
        return False


def health_payload(model: LockedModel) -> dict[str, Any]:
    port = configured_port(model.key)
    url = f"http://{LOOPBACK}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise NativeInfraError(f"{model.key} model service is unavailable: {error}") from error
    if not isinstance(payload, dict):
        raise NativeInfraError(f"{model.key} health response is not an object")
    expected = {
        "status": "ok",
        "kind": model.key,
        "model": model.model_id,
        "revision": model.revision,
        "profile": model.profile,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise NativeInfraError(f"{model.key} health response does not match locked model")
    return payload


def assert_model_service(model: LockedModel) -> dict[str, Any]:
    pid_path = _pid_path(model.key)
    if not pid_path.exists():
        raise NativeInfraError(f"{model.key} model PID record is missing")
    record = _load_pid_record(pid_path)
    if not _record_matches_live_process(model, record):
        raise NativeInfraError(f"{model.key} model process identity does not match its PID record")
    return health_payload(model)


def _wait_for_health(model: LockedModel, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            health_payload(model)
            return
        except NativeInfraError as error:
            last_error = error
            pid_path = _pid_path(model.key)
            if pid_path.exists():
                record = _load_pid_record(pid_path)
                if not is_process_alive(int(record["pid"])):
                    break
        time.sleep(1)
    raise NativeInfraError(f"timed out waiting for {model.key} model service: {last_error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("verify-lock", "bootstrap", "verify", "up", "health", "down", "status")
    )
    parser.add_argument("--model", choices=MODEL_KEYS, action="append")
    return parser


def selected_models(
    lock: RetrievalModelLock, selected: list[str] | None
) -> tuple[LockedModel, ...]:
    keys = MODEL_KEYS if selected is None else tuple(dict.fromkeys(selected))
    return tuple(lock.models[key] for key in keys)


def main() -> int:
    args = build_parser().parse_args()
    try:
        lock = load_model_lock()
        models = selected_models(lock, args.model)
        if args.command == "verify-lock":
            payload: Any = {key: model.profile for key, model in lock.models.items()}
        elif args.command == "bootstrap":
            payload = {model.key: str(bootstrap_model(lock, model)) for model in models}
        elif args.command == "verify":
            payload = {model.key: str(verify_model(model)) for model in models}
        elif args.command == "up":
            for model in models:
                start_model(model)
            payload = {model.key: assert_model_service(model) for model in models}
        elif args.command == "health":
            payload = {model.key: assert_model_service(model) for model in models}
        elif args.command == "down":
            for model in reversed(models):
                stop_model(model)
            payload = {"status": "stopped", "models": [model.key for model in models]}
        else:
            payload = {}
            for model in models:
                pid_path = _pid_path(model.key)
                record = _load_pid_record(pid_path) if pid_path.exists() else None
                payload[model.key] = {
                    "downloaded": model_directory(model).is_dir(),
                    "running": record is not None and _record_matches_live_process(model, record),
                    "port": configured_port(model.key),
                    "profile": model.profile,
                }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except NativeInfraError as error:
        print(f"native model error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
