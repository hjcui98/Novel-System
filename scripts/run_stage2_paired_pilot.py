#!/usr/bin/env python3
"""Run the Stage 2 paired Controller Pilot against a canonical benchmark bundle."""

from __future__ import annotations

import argparse
from pathlib import Path

from novel_agent.services.benchmark_importer import BenchmarkBundleImporter
from novel_agent.services.stage2_paired_pilot import Stage2PairedPilotRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--token-budget", type=int, default=4000)
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    bundle = BenchmarkBundleImporter().load(args.bundle)
    report = Stage2PairedPilotRunner(
        token_budget=args.token_budget,
        max_candidates=args.max_candidates,
    ).run(bundle)
    payload = report.model_dump_json(indent=2) + "\n"
    if args.output is None:
        print(payload, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
