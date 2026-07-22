#!/usr/bin/env python3
"""Compile and validate a human-authored pilot workspace."""

from __future__ import annotations

import argparse
from pathlib import Path

from novel_agent.services.benchmark_importer import BenchmarkBundleImporter
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gate-output", type=Path)
    args = parser.parse_args()
    bundle = HumanBenchmarkCompiler().compile(args.source)
    BenchmarkBundleImporter().validate(bundle)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    if args.gate_output is not None:
        gate = HumanBenchmarkCompiler().derive_gate_subset(bundle)
        BenchmarkBundleImporter().validate(gate)
        args.gate_output.parent.mkdir(parents=True, exist_ok=True)
        args.gate_output.write_text(gate.model_dump_json(indent=2), encoding="utf-8")
    print(
        f"OK: {bundle.bundle_id.root} cases={len(bundle.case_manifests)} "
        f"hash={bundle.content_hash.root}"
    )


if __name__ == "__main__":
    main()
