#!/usr/bin/env python3
"""Compile the human Pilot, run the current Stage 2 read flow, and persist evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.stage2_benchmark_flow import Stage2BenchmarkFlowRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--token-budget", type=int, default=4000)
    parser.add_argument("--max-candidates", type=int, default=20)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    compiler = HumanBenchmarkCompiler()
    bundle = compiler.compile(args.source)
    gate = compiler.derive_gate_subset(bundle)
    summary = Stage2BenchmarkFlowRunner(
        token_budget=args.token_budget,
        max_candidates=args.max_candidates,
    ).run(bundle, args.output_directory, gate_bundle=gate)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
