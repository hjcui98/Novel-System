"""Small deterministic command-line entry point for repository diagnostics."""

from __future__ import annotations

import argparse
import json
import platform
from collections.abc import Sequence

from novel_agent.config import AppSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="print non-secret bootstrap diagnostics")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        settings = AppSettings()
        print(
            json.dumps(
                {
                    "environment": settings.environment,
                    "log_level": settings.log_level,
                    "python": platform.python_version(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
