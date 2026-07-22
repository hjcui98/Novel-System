#!/usr/bin/env python3
"""Check the externally visible endpoints of the Stage 0 local infrastructure."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from dataclasses import asdict, dataclass
from urllib.error import URLError
from urllib.request import urlopen


@dataclass(frozen=True)
class CheckResult:
    service: str
    healthy: bool
    detail: str


def check_tcp(service: str, host: str, port: int, timeout: float) -> CheckResult:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return CheckResult(service, True, f"tcp://{host}:{port}")
    except OSError as error:
        return CheckResult(service, False, str(error))


def check_http(service: str, url: str, timeout: float) -> CheckResult:
    try:
        with urlopen(url, timeout=timeout) as response:
            if 200 <= response.status < 300:
                return CheckResult(service, True, f"HTTP {response.status}")
            return CheckResult(service, False, f"HTTP {response.status}")
    except (OSError, URLError) as error:
        return CheckResult(service, False, str(error))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--postgres-port", type=int, default=5432)
    parser.add_argument("--opensearch-port", type=int, default=9200)
    parser.add_argument("--minio-port", type=int, default=9000)
    parser.add_argument("--otel-health-port", type=int, default=13133)
    parser.add_argument("--timeout", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    checks = [
        check_tcp("postgres", args.host, args.postgres_port, args.timeout),
        check_http(
            "opensearch",
            f"http://{args.host}:{args.opensearch_port}/_cluster/health",
            args.timeout,
        ),
        check_http(
            "minio",
            f"http://{args.host}:{args.minio_port}/minio/health/live",
            args.timeout,
        ),
        check_http(
            "otel-collector",
            f"http://{args.host}:{args.otel_health_port}/",
            args.timeout,
        ),
    ]
    print(json.dumps([asdict(result) for result in checks], ensure_ascii=False, indent=2))
    return 0 if all(result.healthy for result in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
