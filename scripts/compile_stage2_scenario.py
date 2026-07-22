#!/usr/bin/env python3
"""Compile continuous Stage 2 scenarios and independent rebuild diagnostics."""

from __future__ import annotations

import argparse
from pathlib import Path

from novel_agent.domain.stage2 import BenchmarkInformationProfile
from novel_agent.services.benchmark_importer import BenchmarkBundleImporter
from novel_agent.services.benchmark_scenario_compiler import BenchmarkScenarioCompiler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output-directory", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    bundle = BenchmarkBundleImporter().load(args.bundle)
    compiler = BenchmarkScenarioCompiler()
    args.output_directory.mkdir(parents=True, exist_ok=True)
    for profile in BenchmarkInformationProfile:
        scenario = compiler.compile(bundle, profile)
        target = args.output_directory / f"scenario.{profile.value}.json"
        target.write_text(scenario.model_dump_json(indent=2) + "\n", encoding="utf-8")
    rebuild = compiler.independent_rebuild_report(bundle)
    (args.output_directory / "independent_rebuild.json").write_text(
        rebuild.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
