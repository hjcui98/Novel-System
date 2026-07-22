#!/usr/bin/env python3
"""Secure user-owned lifecycle manager for the Stage 0 native services."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import posixpath
import re
import resource
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Any, Self, cast
from urllib.parse import urlparse

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPOSITORY_ROOT / "infra" / "native-services.lock"
NATIVE_ROOT = REPOSITORY_ROOT / "tmp" / "native"
DOWNLOAD_ROOT = NATIVE_ROOT / "downloads"
DIST_ROOT = NATIVE_ROOT / "dist"
RUN_ROOT = NATIVE_ROOT / "run"
LOG_ROOT = NATIVE_ROOT / "logs"
VOLUME_ROOT = REPOSITORY_ROOT / "volumes" / "native"
ENV_PATH = REPOSITORY_ROOT / ".env"
ENV_EXAMPLE_PATH = REPOSITORY_ROOT / ".env.example"
CONDA_BIN = REPOSITORY_ROOT / ".conda-env" / "bin"
LOOPBACK = "127.0.0.1"
SERVICE_ORDER = ("postgres", "opensearch", "minio", "otel")
PLACEHOLDER_MARKERS = (
    "change-me",
    "generate_at_bootstrap",
    "minioadmin",
    "example",
    "placeholder",
)
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class NativeInfraError(RuntimeError):
    """Fail-closed native infrastructure error."""


@dataclass(frozen=True, slots=True)
class ArtifactLock:
    name: str
    version: str
    url: str
    filename: str
    sha256: str
    archive: str
    install_dir: str
    executable: str
    allowed_hosts: tuple[str, ...]
    official_sha512: str | None = None


@dataclass(frozen=True, slots=True)
class NativeSettings:
    profile: str
    run_id: str
    postgres_port: int
    opensearch_port: int
    opensearch_transport_port: int
    minio_api_port: int
    minio_console_port: int
    otel_grpc_port: int
    otel_http_port: int
    otel_health_port: int
    postgres_user: str
    postgres_password: str
    postgres_database: str
    minio_user: str
    minio_password: str

    @property
    def ports(self) -> dict[str, tuple[int, ...]]:
        return {
            "postgres": (self.postgres_port,),
            "opensearch": (self.opensearch_port, self.opensearch_transport_port),
            "minio": (self.minio_api_port, self.minio_console_port),
            "otel": (self.otel_grpc_port, self.otel_http_port, self.otel_health_port),
        }


def sha256_file(path: Path) -> str:
    return hash_file(path, "sha256")


def hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def config_fingerprint(path: Path) -> str:
    return sha256_file(path)


def process_start_time(pid: int) -> int:
    stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    closing = stat.rfind(")")
    if closing < 0:
        raise NativeInfraError(f"cannot parse process stat for PID {pid}")
    fields = stat[closing + 2 :].split()
    return int(fields[19])


def process_owner(pid: int) -> int:
    status = (Path("/proc") / str(pid) / "status").read_text(encoding="utf-8")
    for line in status.splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    raise NativeInfraError(f"cannot determine owner for PID {pid}")


def process_command(pid: int) -> tuple[str, ...]:
    raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    return tuple(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        closing = stat.rfind(")")
        return closing >= 0 and stat[closing + 2 :].split()[0] != "Z"
    except (FileNotFoundError, IndexError):
        return False


def assert_owned_directory(path: Path, allowed_root: Path) -> Path:
    resolved = path.resolve()
    root = allowed_root.resolve()
    if not resolved.is_relative_to(root):
        raise NativeInfraError(f"path escapes allowed root: {resolved}")
    if resolved.exists() and resolved.stat().st_uid != os.getuid():
        raise NativeInfraError(f"path is not owned by current UID: {resolved}")
    return resolved


def ensure_private_directory(path: Path, allowed_root: Path) -> Path:
    resolved = assert_owned_directory(path, allowed_root)
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(resolved, 0o700)
    if resolved.stat().st_uid != os.getuid():
        raise NativeInfraError(f"directory owner changed unexpectedly: {resolved}")
    return resolved


def safe_remove_tree(path: Path, allowed_root: Path) -> None:
    resolved = assert_owned_directory(path, allowed_root)
    root = allowed_root.resolve()
    if resolved == root or resolved.parent == root.parent:
        raise NativeInfraError(f"refusing broad recursive removal: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def assert_safe_archive(archive: tarfile.TarFile) -> None:
    for member in archive.getmembers():
        candidate = PurePosixPath(member.name)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise NativeInfraError(f"unsafe archive member: {member.name}")
        if member.issym() or member.islnk():
            target = PurePosixPath(member.linkname)
            resolved_target = posixpath.normpath(
                posixpath.join(posixpath.dirname(member.name), member.linkname)
            )
            if target.is_absolute() or resolved_target == ".." or resolved_target.startswith("../"):
                raise NativeInfraError(f"unsafe archive link: {member.name}")


def load_lock(path: Path = LOCK_PATH) -> dict[str, ArtifactLock]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1 or document.get("architecture") != "x86_64":
        raise NativeInfraError("native service lock schema or architecture is unsupported")
    result: dict[str, ArtifactLock] = {}
    for name, value in document["artifacts"].items():
        result[name] = ArtifactLock(
            name=name,
            version=value["version"],
            url=value["url"],
            filename=value["filename"],
            sha256=value["sha256"],
            archive=value["archive"],
            install_dir=value["install_dir"],
            executable=value["executable"],
            allowed_hosts=tuple(value["allowed_hosts"]),
            official_sha512=value.get("official_sha512"),
        )
    return result


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()
    return values


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    source = path.read_text(encoding="utf-8") if path.exists() else ""
    seen: set[str] = set()
    lines: list[str] = []
    for line in source.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name = line.split("=", 1)[0].strip()
            if name in updates:
                lines.append(f"{name}={updates[name]}")
                seen.add(name)
                continue
        lines.append(line)
    for name in sorted(set(updates) - seen):
        lines.append(f"{name}={updates[name]}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def is_placeholder(value: str) -> bool:
    normalized = value.lower()
    return not value or any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind((LOOPBACK, 0))
        return int(server.getsockname()[1])


def assert_port_free(port: int) -> None:
    if not 1 <= port <= 65535:
        raise NativeInfraError(f"invalid TCP port: {port}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.2)
        if client.connect_ex((LOOPBACK, port)) == 0:
            raise NativeInfraError(f"loopback port {port} is occupied by an unknown process")


def wait_for(predicate: Any, description: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as error:
            last_error = error
        time.sleep(0.5)
    detail = f": {last_error}" if last_error else ""
    raise NativeInfraError(f"timed out waiting for {description}{detail}")


class RestrictedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: tuple[str, ...]) -> None:
        self._allowed_hosts = allowed_hosts
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
            raise NativeInfraError(f"download redirect escaped official hosts: {parsed.hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_locked_artifact(lock: ArtifactLock, target: Path) -> None:
    parsed = urlparse(lock.url)
    if parsed.scheme != "https" or parsed.hostname not in lock.allowed_hosts:
        raise NativeInfraError(f"artifact URL is not an allowed official HTTPS host: {lock.url}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_suffix(target.suffix + ".partial")
    opener = urllib.request.build_opener(RestrictedRedirectHandler(lock.allowed_hosts))
    request = urllib.request.Request(lock.url, headers={"User-Agent": "novel-agent-stage0/0.1"})
    context = ssl.create_default_context()
    opener.add_handler(urllib.request.HTTPSHandler(context=context))
    with opener.open(request, timeout=60) as response, temporary.open("wb") as output:
        final = urlparse(response.geturl())
        if final.scheme != "https" or final.hostname not in lock.allowed_hosts:
            raise NativeInfraError("artifact response escaped allowed official hosts")
        shutil.copyfileobj(response, output, length=1024 * 1024)
    if sha256_file(temporary) != lock.sha256:
        temporary.unlink(missing_ok=True)
        raise NativeInfraError(f"SHA-256 mismatch for {lock.name}")
    temporary.replace(target)


class NativeInfra:
    def __init__(self, settings: NativeSettings) -> None:
        self.settings = settings
        if settings.profile not in {"dev", "integration"}:
            raise NativeInfraError("native profile must be dev or integration")
        if settings.profile == "integration" and not settings.run_id.startswith("integration-"):
            raise NativeInfraError("integration run_id must use the integration- prefix")
        self.locks = load_lock()
        self.run_dir = ensure_private_directory(
            RUN_ROOT / settings.profile / settings.run_id, RUN_ROOT
        )
        self.config_dir = ensure_private_directory(self.run_dir / "config", RUN_ROOT)
        self.socket_dir = ensure_private_directory(self.run_dir / "socket", RUN_ROOT)
        self.log_dir = ensure_private_directory(
            LOG_ROOT / settings.profile / settings.run_id, LOG_ROOT
        )
        self.data_root = ensure_private_directory(
            VOLUME_ROOT / settings.profile / settings.run_id, VOLUME_ROOT
        )
        self._validate_settings()

    @classmethod
    def development(cls) -> Self:
        ensure_development_env()
        values = parse_env(ENV_PATH)
        return cls(
            NativeSettings(
                profile="dev",
                run_id="default",
                postgres_port=int(values.get("POSTGRES_PORT", "5432")),
                opensearch_port=int(values.get("OPENSEARCH_PORT", "9200")),
                opensearch_transport_port=int(values.get("OPENSEARCH_TRANSPORT_PORT", "9300")),
                minio_api_port=int(values.get("MINIO_API_PORT", "9000")),
                minio_console_port=int(values.get("MINIO_CONSOLE_PORT", "9001")),
                otel_grpc_port=int(values.get("OTEL_GRPC_PORT", "4317")),
                otel_http_port=int(values.get("OTEL_HTTP_PORT", "4318")),
                otel_health_port=int(values.get("OTEL_HEALTH_PORT", "13133")),
                postgres_user=values["POSTGRES_USER"],
                postgres_password=values["POSTGRES_PASSWORD"],
                postgres_database=values["POSTGRES_DB"],
                minio_user=values["MINIO_ROOT_USER"],
                minio_password=values["MINIO_ROOT_PASSWORD"],
            )
        )

    @classmethod
    def integration(cls, run_id: str | None = None) -> Self:
        identity = run_id or f"integration-{secrets.token_hex(8)}"
        ports: list[int] = []
        while len(ports) < 8:
            candidate = free_loopback_port()
            if candidate not in ports:
                ports.append(candidate)
        return cls(
            NativeSettings(
                profile="integration",
                run_id=identity,
                postgres_port=ports[0],
                opensearch_port=ports[1],
                opensearch_transport_port=ports[2],
                minio_api_port=ports[3],
                minio_console_port=ports[4],
                otel_grpc_port=ports[5],
                otel_http_port=ports[6],
                otel_health_port=ports[7],
                postgres_user="integration_user",
                postgres_password=secrets.token_urlsafe(36),
                postgres_database=f"integration_{secrets.token_hex(6)}",
                minio_user=f"integration_{secrets.token_hex(8)}",
                minio_password=secrets.token_urlsafe(36),
            )
        )

    def __enter__(self) -> Self:
        self.up()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.down(clean=exc_type is None and self.settings.profile == "integration")

    @property
    def database_url(self) -> str:
        from urllib.parse import quote

        user = quote(self.settings.postgres_user, safe="")
        password = quote(self.settings.postgres_password, safe="")
        database = quote(self.settings.postgres_database, safe="")
        return (
            f"postgresql+psycopg://{user}:{password}@{LOOPBACK}:"
            f"{self.settings.postgres_port}/{database}"
        )

    @property
    def checkpoint_url(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    def _validate_settings(self) -> None:
        values = self.settings
        if not IDENTIFIER.fullmatch(values.postgres_user):
            raise NativeInfraError("PostgreSQL user is not a safe identifier")
        if not IDENTIFIER.fullmatch(values.postgres_database):
            raise NativeInfraError("PostgreSQL database is not a safe identifier")
        if is_placeholder(values.postgres_password) or len(values.postgres_password) < 24:
            raise NativeInfraError("PostgreSQL password is missing, weak, or a placeholder")
        if is_placeholder(values.minio_user) or len(values.minio_user) < 12:
            raise NativeInfraError("MinIO user is missing, weak, or a placeholder")
        if is_placeholder(values.minio_password) or len(values.minio_password) < 32:
            raise NativeInfraError("MinIO password is missing, weak, or a placeholder")
        all_ports = [port for ports in values.ports.values() for port in ports]
        if len(all_ports) != len(set(all_ports)):
            raise NativeInfraError("native service ports must be unique")
        for port in all_ports:
            if not 1 <= port <= 65535:
                raise NativeInfraError(f"invalid native service port: {port}")

    def bootstrap(self) -> None:
        self._verify_postgres_installation()
        for lock in self.locks.values():
            self._install_artifact(lock)

    def _verify_postgres_installation(self) -> None:
        expected = {
            "initdb": CONDA_BIN / "initdb",
            "pg_ctl": CONDA_BIN / "pg_ctl",
            "postgres": CONDA_BIN / "postgres",
            "pg_isready": CONDA_BIN / "pg_isready",
        }
        for name, path in expected.items():
            if not path.is_file() or not os.access(path, os.X_OK):
                raise NativeInfraError(f"project Conda PostgreSQL binary is missing: {name}")
            if not path.resolve().is_relative_to((REPOSITORY_ROOT / ".conda-env").resolve()):
                raise NativeInfraError(f"PostgreSQL binary escaped .conda-env: {path}")
        result = subprocess.run(
            [str(expected["postgres"]), "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
        if "17.10" not in result.stdout:
            raise NativeInfraError("PostgreSQL binary is not the locked 17.10 version")

    def _install_artifact(self, lock: ArtifactLock) -> None:
        archive_path = DOWNLOAD_ROOT / lock.filename
        if not archive_path.exists() or sha256_file(archive_path) != lock.sha256:
            download_locked_artifact(lock, archive_path)
        if lock.official_sha512:
            sha512 = hash_file(archive_path, "sha512")
            if sha512 != lock.official_sha512:
                raise NativeInfraError(f"official SHA-512 mismatch for {lock.name}")
        destination = DIST_ROOT / lock.install_dir
        executable = DIST_ROOT / lock.executable
        if executable.exists():
            return
        ensure_private_directory(DIST_ROOT, NATIVE_ROOT)
        temporary = DIST_ROOT / f".extract-{lock.name}-{secrets.token_hex(6)}"
        ensure_private_directory(temporary, DIST_ROOT)
        try:
            if lock.archive == "binary":
                target = temporary / Path(lock.executable).name
                shutil.copyfile(archive_path, target)
                os.chmod(target, 0o700)
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary.replace(destination)
            elif lock.archive == "tar.gz":
                with tarfile.open(archive_path, "r:gz") as archive:
                    assert_safe_archive(archive)
                    archive.extractall(temporary, filter="data")
                extracted = temporary / lock.install_dir
                if not extracted.exists():
                    extracted = temporary
                    temporary = DIST_ROOT / f".installed-{lock.name}-{secrets.token_hex(6)}"
                    extracted.replace(temporary)
                if destination.exists():
                    safe_remove_tree(destination, DIST_ROOT)
                if temporary.name.startswith(".installed-"):
                    temporary.replace(destination)
                else:
                    extracted.replace(destination)
            else:
                raise NativeInfraError(f"unsupported archive type: {lock.archive}")
        finally:
            if temporary.exists():
                safe_remove_tree(temporary, DIST_ROOT)
        if not executable.exists() or not os.access(executable, os.X_OK):
            raise NativeInfraError(f"installed executable is missing: {executable}")

    def up(self) -> None:
        self.bootstrap()
        started: list[str] = []
        try:
            for service in SERVICE_ORDER:
                if self._lease_is_running(service):
                    continue
                for port in self.settings.ports[service]:
                    assert_port_free(port)
                self.start_service(service)
                started.append(service)
            self.health()
        except Exception:
            for service in reversed(started):
                with contextlib.suppress(Exception):
                    self.stop_service(service)
            raise

    def start_service(self, service: str) -> None:
        if service not in SERVICE_ORDER:
            raise NativeInfraError(f"unknown native service: {service}")
        if self._lease_is_running(service):
            return
        for port in self.settings.ports[service]:
            assert_port_free(port)
        if service == "postgres":
            self._start_postgres()
        elif service == "opensearch":
            self._start_opensearch()
        elif service == "minio":
            self._start_minio()
        else:
            self._start_otel()

    def _start_postgres(self) -> None:
        data = ensure_private_directory(self.data_root / "postgres", self.data_root)
        log = self.log_dir / "postgres.log"
        password_file = self.config_dir / "postgres.init-password"
        if not (data / "PG_VERSION").exists():
            password_file.write_text(self.settings.postgres_password + "\n", encoding="utf-8")
            os.chmod(password_file, 0o600)
            try:
                subprocess.run(
                    [
                        str(CONDA_BIN / "initdb"),
                        "-D",
                        str(data),
                        "--username",
                        self.settings.postgres_user,
                        "--pwfile",
                        str(password_file),
                        "--auth-host=scram-sha-256",
                        "--auth-local=trust",
                        "--encoding=UTF8",
                        "--no-locale",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            finally:
                password_file.unlink(missing_ok=True)
        os.chmod(data, 0o700)
        config = self.config_dir / "postgresql.conf"
        hba = self.config_dir / "pg_hba.conf"
        config.write_text(
            "\n".join(
                (
                    f"data_directory = '{_pg_quote(data)}'",
                    f"hba_file = '{_pg_quote(hba)}'",
                    f"ident_file = '{_pg_quote(self.config_dir / 'pg_ident.conf')}'",
                    "listen_addresses = '127.0.0.1'",
                    f"port = {self.settings.postgres_port}",
                    "unix_socket_directories = ''",
                    "password_encryption = 'scram-sha-256'",
                    "ssl = off",
                    "logging_collector = off",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        hba.write_text(
            "local all all trust\n"
            "host all all 127.0.0.1/32 scram-sha-256\n"
            "host all all ::1/128 reject\n",
            encoding="utf-8",
        )
        (self.config_dir / "pg_ident.conf").write_text("", encoding="utf-8")
        for path in (config, hba, self.config_dir / "pg_ident.conf"):
            os.chmod(path, 0o600)
        subprocess.run(
            [
                str(CONDA_BIN / "pg_ctl"),
                "-D",
                str(data),
                "-l",
                str(log),
                "-o",
                f"-c config_file={config}",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        pid = int((data / "postmaster.pid").read_text(encoding="utf-8").splitlines()[0])
        self._write_lease("postgres", pid, data, config)
        wait_for(self._postgres_ready, "PostgreSQL")
        self._ensure_database()

    def _ensure_database(self) -> None:
        import psycopg
        from psycopg import sql

        connection = psycopg.connect(
            host=LOOPBACK,
            port=self.settings.postgres_port,
            user=self.settings.postgres_user,
            password=self.settings.postgres_password,
            dbname="postgres",
            autocommit=True,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s",
                    (self.settings.postgres_database,),
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        sql.SQL("CREATE DATABASE {}").format(
                            sql.Identifier(self.settings.postgres_database)
                        )
                    )
        finally:
            connection.close()

    def _postgres_ready(self) -> bool:
        import psycopg

        try:
            with (
                psycopg.connect(
                    host=LOOPBACK,
                    port=self.settings.postgres_port,
                    user=self.settings.postgres_user,
                    password=self.settings.postgres_password,
                    dbname="postgres",
                    connect_timeout=2,
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute("SELECT current_setting('listen_addresses')")
                return cursor.fetchone() == (LOOPBACK,)
        except psycopg.Error:
            return False

    def _start_opensearch(self) -> None:
        lock = self.locks["opensearch"]
        home = DIST_ROOT / lock.install_dir
        data = ensure_private_directory(self.data_root / "opensearch", self.data_root)
        service_log = ensure_private_directory(self.log_dir / "opensearch", self.log_dir)
        config_home = self.config_dir / "opensearch"
        if not config_home.exists():
            shutil.copytree(home / "config", config_home)
        config = config_home / "opensearch.yml"
        config.write_text(
            "\n".join(
                (
                    f"cluster.name: novel-agent-{self.settings.run_id}",
                    f"node.name: node-{self.settings.run_id}",
                    f"path.data: {data}",
                    f"path.logs: {service_log}",
                    "network.host: 127.0.0.1",
                    f"http.port: {self.settings.opensearch_port}",
                    "transport.host: 127.0.0.1",
                    f"transport.port: {self.settings.opensearch_transport_port}",
                    "discovery.type: single-node",
                    "plugins.security.disabled: true",
                    "node.store.allow_mmap: false",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(config, 0o600)
        env = os.environ.copy()
        env.update(
            {
                "OPENSEARCH_HOME": str(home),
                "OPENSEARCH_PATH_CONF": str(config_home),
                "OPENSEARCH_JAVA_OPTS": "-Xms512m -Xmx512m",
            }
        )
        log = (self.log_dir / "opensearch-process.log").open("ab")
        process = subprocess.Popen(
            [str(home / "bin" / "opensearch")],
            cwd=home,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=_raise_nofile_limit,
        )
        log.close()
        self._write_lease("opensearch", process.pid, data, config)
        wait_for(self._opensearch_ready, "OpenSearch", timeout=180)

    def _opensearch_ready(self) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://{LOOPBACK}:{self.settings.opensearch_port}/",
                timeout=2,
            ) as response:
                document = cast(dict[str, Any], json.loads(response.read()))
                version = cast(dict[str, Any], document["version"])
                version_ready = int(response.status) == 200 and str(version["number"]) == "3.7.0"
            if not version_ready:
                return False
            health_url = (
                f"http://{LOOPBACK}:{self.settings.opensearch_port}/_cluster/health"
                "?wait_for_status=yellow&wait_for_no_relocating_shards=true&timeout=2s"
            )
            with urllib.request.urlopen(health_url, timeout=3) as response:
                health = cast(dict[str, Any], json.loads(response.read()))
                return int(response.status) == 200 and str(health["status"]) in {"yellow", "green"}
        except (OSError, ValueError, KeyError):
            return False

    def _start_minio(self) -> None:
        lock = self.locks["minio"]
        executable = DIST_ROOT / lock.executable
        data = ensure_private_directory(self.data_root / "minio", self.data_root)
        config = self.config_dir / "minio.json"
        config.write_text(
            json.dumps(
                {
                    "address": f"{LOOPBACK}:{self.settings.minio_api_port}",
                    "console_address": f"{LOOPBACK}:{self.settings.minio_console_port}",
                    "data": str(data),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(config, 0o600)
        env = os.environ.copy()
        env.update(
            {
                "MINIO_ROOT_USER": self.settings.minio_user,
                "MINIO_ROOT_PASSWORD": self.settings.minio_password,
            }
        )
        log = (self.log_dir / "minio.log").open("ab")
        process = subprocess.Popen(
            [
                str(executable),
                "server",
                str(data),
                "--address",
                f"{LOOPBACK}:{self.settings.minio_api_port}",
                "--console-address",
                f"{LOOPBACK}:{self.settings.minio_console_port}",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.close()
        self._write_lease("minio", process.pid, data, config)
        wait_for(self._minio_ready, "MinIO")

    def _minio_ready(self) -> bool:
        from minio import Minio

        try:
            client = Minio(
                f"{LOOPBACK}:{self.settings.minio_api_port}",
                access_key=self.settings.minio_user,
                secret_key=self.settings.minio_password,
                secure=False,
            )
            client.list_buckets()
            return True
        except Exception:
            return False

    def _start_otel(self) -> None:
        lock = self.locks["otel"]
        executable = DIST_ROOT / lock.executable
        data = ensure_private_directory(self.data_root / "otel", self.data_root)
        config = self.config_dir / "otel-collector.yaml"
        config.write_text(
            f"""extensions:
  health_check:
    endpoint: {LOOPBACK}:{self.settings.otel_health_port}
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: {LOOPBACK}:{self.settings.otel_grpc_port}
      http:
        endpoint: {LOOPBACK}:{self.settings.otel_http_port}
processors:
  batch: {{}}
  memory_limiter:
    check_interval: 1s
    limit_mib: 256
    spike_limit_mib: 64
exporters:
  debug:
    verbosity: normal
service:
  extensions: [health_check]
  telemetry:
    metrics:
      level: none
      readers: []
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [debug]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [debug]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [debug]
""",
            encoding="utf-8",
        )
        os.chmod(config, 0o600)
        log = (self.log_dir / "otel.log").open("ab")
        process = subprocess.Popen(
            [str(executable), "--config", str(config)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.close()
        self._write_lease("otel", process.pid, data, config)
        wait_for(self._otel_ready, "OpenTelemetry Collector")

    def _otel_ready(self) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://{LOOPBACK}:{self.settings.otel_health_port}/",
                timeout=2,
            ) as response:
                return int(response.status) == 200
        except OSError:
            return False

    def _lease_path(self, service: str) -> Path:
        return self.run_dir / f"{service}.lease.json"

    def _write_lease(self, service: str, pid: int, data: Path, config: Path) -> None:
        wait_for(lambda: is_process_alive(pid), f"{service} process")
        if service == "opensearch":
            wait_for(
                lambda: (Path("/proc") / str(pid) / "exe").resolve().name == "java",
                "OpenSearch launcher to exec bundled Java",
                timeout=30,
            )
        exe = (Path("/proc") / str(pid) / "exe").resolve()
        artifact = self.locks.get(service)
        lease = {
            "schema_version": 1,
            "service": service,
            "profile": self.settings.profile,
            "run_id": self.settings.run_id,
            "pid": pid,
            "uid": os.getuid(),
            "start_time": process_start_time(pid),
            "executable": str(exe),
            "binary_sha256": sha256_file(exe),
            "version": artifact.version if artifact else "17.10",
            "artifact_sha256": artifact.sha256 if artifact else None,
            "data_dir": str(data.resolve()),
            "config": str(config.resolve()),
            "config_sha256": config_fingerprint(config),
            "ports": list(self.settings.ports[service]),
            "created_at_epoch": time.time(),
        }
        path = self._lease_path(service)
        path.write_text(json.dumps(lease, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

    def _read_and_validate_lease(self, service: str) -> dict[str, Any]:
        path = self._lease_path(service)
        if not path.exists():
            raise NativeInfraError(f"no lease exists for {service}")
        lease = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        pid = int(lease["pid"])
        if not is_process_alive(pid):
            raise NativeInfraError(f"leased {service} process is not alive")
        if int(lease["uid"]) != os.getuid() or process_owner(pid) != os.getuid():
            raise NativeInfraError(f"leased {service} process owner mismatch")
        if process_start_time(pid) != int(lease["start_time"]):
            raise NativeInfraError(f"leased {service} process start time mismatch")
        exe = (Path("/proc") / str(pid) / "exe").resolve()
        if exe != Path(lease["executable"]).resolve():
            raise NativeInfraError(f"leased {service} executable mismatch")
        if sha256_file(exe) != lease["binary_sha256"]:
            raise NativeInfraError(f"leased {service} executable digest mismatch")
        data = assert_owned_directory(Path(lease["data_dir"]), self.data_root)
        command = process_command(pid)
        if service in {"postgres", "minio"} and str(data) not in command:
            raise NativeInfraError(f"leased {service} command does not reference its data dir")
        if service == "opensearch" and not any("opensearch" in item for item in command):
            raise NativeInfraError("leased OpenSearch command identity mismatch")
        if service == "otel" and lease["config"] not in command:
            raise NativeInfraError("leased OTel command does not reference its config")
        if tuple(int(port) for port in lease["ports"]) != self.settings.ports[service]:
            raise NativeInfraError(f"leased {service} port mismatch")
        return lease

    def _lease_is_running(self, service: str) -> bool:
        path = self._lease_path(service)
        if not path.exists():
            return False
        try:
            self._read_and_validate_lease(service)
            return True
        except NativeInfraError:
            return False

    def stop_service(self, service: str, timeout: float = 30.0) -> None:
        lease_path = self._lease_path(service)
        if not lease_path.exists():
            return
        lease = self._read_and_validate_lease(service)
        pid = int(lease["pid"])
        if service == "postgres":
            subprocess.run(
                [
                    str(CONDA_BIN / "pg_ctl"),
                    "-D",
                    lease["data_dir"],
                    "-m",
                    "fast",
                    "-w",
                    "stop",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + timeout
            while is_process_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.2)
            if is_process_alive(pid):
                self._read_and_validate_lease(service)
                os.kill(pid, signal.SIGKILL)
                wait_for(lambda: not is_process_alive(pid), f"{service} forced stop", 10)
        lease_path.unlink(missing_ok=True)

    def restart_service(self, service: str) -> None:
        self.stop_service(service)
        self.start_service(service)
        self.health_service(service)

    def restart(self) -> None:
        self.down(clean=False)
        self.up()

    def health_service(self, service: str) -> dict[str, Any]:
        lease = self._read_and_validate_lease(service)
        probes = {
            "postgres": self._postgres_ready,
            "opensearch": self._opensearch_ready,
            "minio": self._minio_ready,
            "otel": self._otel_ready,
        }
        if not probes[service]():
            raise NativeInfraError(f"{service} health probe failed")
        for port in self.settings.ports[service]:
            wait_for(
                lambda port=port: _port_is_loopback_only(port),
                f"{service} port {port} to become a loopback-only listener",
                timeout=5,
            )
        return lease

    def health(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "backend": "native",
            "profile": self.settings.profile,
            "run_id": self.settings.run_id,
            "mmap_disabled": True,
            "services": {},
        }
        for service in SERVICE_ORDER:
            evidence["services"][service] = self.health_service(service)
        path = self.run_dir / "health-evidence.json"
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return evidence

    def status(self) -> dict[str, Any]:
        return {
            "backend": "native",
            "profile": self.settings.profile,
            "run_id": self.settings.run_id,
            "services": {
                service: {"running": self._lease_is_running(service)} for service in SERVICE_ORDER
            },
        }

    def down(self, *, clean: bool = False) -> None:
        errors: list[str] = []
        for service in reversed(SERVICE_ORDER):
            try:
                self.stop_service(service)
            except Exception as error:
                errors.append(f"{service}: {error}")
        if clean:
            if self.settings.profile != "integration":
                raise NativeInfraError("automatic cleanup is only allowed for integration data")
            safe_remove_tree(self.data_root, VOLUME_ROOT / "integration")
            safe_remove_tree(self.run_dir, RUN_ROOT / "integration")
        if errors:
            raise NativeInfraError("; ".join(errors))


def _pg_quote(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def _raise_nofile_limit() -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(max(soft, 65535), hard), hard))


def _port_is_loopback_only(port: int) -> bool:
    command = shutil.which("ss")
    if command is None:
        raise NativeInfraError("ss is required to verify loopback listener ownership")
    result = subprocess.run(
        [command, "-ltnH", f"sport = :{port}"],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return False
    for line in lines:
        fields = line.split()
        local = fields[3] if len(fields) > 3 else ""
        if not (
            local.startswith("127.0.0.1:")
            or local.startswith("[::1]:")
            or local.startswith("::1:")
            or local.startswith("[::ffff:127.0.0.1]:")
        ):
            return False
    return True


def ensure_development_env() -> None:
    if not ENV_PATH.exists():
        shutil.copyfile(ENV_EXAMPLE_PATH, ENV_PATH)
    values = parse_env(ENV_PATH)
    updates: dict[str, str] = {}
    if is_placeholder(values.get("POSTGRES_PASSWORD", "")):
        updates["POSTGRES_PASSWORD"] = secrets.token_urlsafe(36)
    if (
        is_placeholder(values.get("MINIO_ROOT_USER", ""))
        or len(values.get("MINIO_ROOT_USER", "")) < 12
    ):
        updates["MINIO_ROOT_USER"] = f"novel_{secrets.token_hex(8)}"
    if is_placeholder(values.get("MINIO_ROOT_PASSWORD", "")):
        updates["MINIO_ROOT_PASSWORD"] = secrets.token_urlsafe(36)
    if updates:
        update_env_file(ENV_PATH, updates)
    else:
        os.chmod(ENV_PATH, 0o600)
    mode = ENV_PATH.stat().st_mode & 0o777
    if mode != 0o600 or ENV_PATH.stat().st_uid != os.getuid():
        raise NativeInfraError(".env must be owned by current UID with mode 0600")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("bootstrap", "up", "health", "stop", "restart", "down", "status"),
    )
    parser.add_argument("--service", choices=SERVICE_ORDER)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        infra = NativeInfra.development()
        if args.command == "bootstrap":
            infra.bootstrap()
            result: Any = {"backend": "native", "status": "bootstrapped"}
        elif args.command == "up":
            infra.up()
            result = infra.status()
        elif args.command == "health":
            result = infra.health()
        elif args.command == "status":
            result = infra.status()
        elif args.command in {"stop", "down"}:
            if args.service:
                infra.stop_service(args.service)
            else:
                infra.down(clean=False)
            result = infra.status()
        else:
            if args.service:
                infra.restart_service(args.service)
            else:
                infra.restart()
            result = infra.status()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except NativeInfraError as error:
        print(f"native infrastructure error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
