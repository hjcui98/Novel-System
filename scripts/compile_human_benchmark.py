#!/usr/bin/env python3
"""Compile and validate a human-authored pilot workspace."""

from __future__ import annotations

import argparse
from pathlib import Path

from novel_agent.services.benchmark_importer import BenchmarkBundleImporter
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gate-output", type=Path)
    parser.add_argument("--plan-case-id")
    parser.add_argument("--plan-output", type=Path)
    args = parser.parse_args()
    if (args.plan_case_id is None) != (args.plan_output is None):
        parser.error("--plan-case-id and --plan-output must be provided together")
    bundle = HumanBenchmarkCompiler().compile(args.source)
    BenchmarkBundleImporter().validate(bundle)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    if args.gate_output is not None:
        gate = HumanBenchmarkCompiler().derive_gate_subset(bundle)
        BenchmarkBundleImporter().validate(gate)
        args.gate_output.parent.mkdir(parents=True, exist_ok=True)
        args.gate_output.write_text(gate.model_dump_json(indent=2), encoding="utf-8")
    if args.plan_case_id is not None and args.plan_output is not None:
        case = next(
            (item for item in bundle.case_manifests if item.case_id.root == args.plan_case_id),
            None,
        )
        if case is None:
            parser.error(f"benchmark case is missing: {args.plan_case_id}")
        plan = next(
            (item for item in bundle.plan_roots if item.root_hash == case.input_plan_root),
            None,
        )
        if plan is None:
            parser.error(f"benchmark case PlanRoot is missing: {args.plan_case_id}")
        args.plan_output.parent.mkdir(parents=True, exist_ok=True)
        args.plan_output.write_bytes(canonical_json_bytes(plan.model_dump(mode="json")))
    print(
        f"OK: {bundle.bundle_id.root} cases={len(bundle.case_manifests)} "
        f"hash={bundle.content_hash.root}"
    )


if __name__ == "__main__":
    main()
