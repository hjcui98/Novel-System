#!/usr/bin/env python3
"""Recover and aggregate audited Stage 2 checkpoint results from the object store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from novel_agent.domain.ids import StableId
from novel_agent.domain.stage2 import (
    BenchmarkInformationProfile,
    PairedPilotCaseResult,
)
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.teacher_forced_benchmark_e2e import TeacherForcedBenchmarkE2ERunner


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--source", type=Path, required=True)
    value.add_argument("--output-directory", type=Path, required=True)
    value.add_argument(
        "--information-profile",
        choices=tuple(item.value for item in BenchmarkInformationProfile),
        default=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED.value,
    )
    return value


def main() -> int:
    args = parser().parse_args()
    bundle = HumanBenchmarkCompiler().compile(args.source)
    expected = {case.case_id for case in bundle.case_manifests}
    found: dict[StableId, PairedPilotCaseResult] = {}
    object_root = args.output_directory / "objects" / "sha256"
    for path in object_root.glob("*/*"):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or "case_id" not in payload or "pair_id" not in payload:
            continue
        try:
            case = PairedPilotCaseResult.model_validate_json(path.read_text("utf-8"))
        except ValueError:
            continue
        if case.case_id not in expected:
            continue
        existing = found.get(case.case_id)
        if existing is not None and existing != case:
            raise SystemExit(f"conflicting audited results for {case.case_id.root}")
        found[case.case_id] = case
    missing = sorted((item.root for item in expected - set(found)), key=str)
    if missing:
        raise SystemExit(f"missing audited checkpoint results: {missing}")
    cases = tuple(sorted(found.values(), key=lambda item: item.checkpoint_chapter))
    profile = BenchmarkInformationProfile(args.information_profile)
    report = TeacherForcedBenchmarkE2ERunner._paired_report(bundle, profile, cases)
    args.output_directory.mkdir(parents=True, exist_ok=True)
    for case in cases:
        path = args.output_directory / f"paired_case_C{case.checkpoint_chapter}.json"
        path.write_text(case.model_dump_json(indent=2) + "\n", encoding="utf-8")
    aggregate = args.output_directory / "e2e_paired_report_all_checkpoints.json"
    aggregate.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "cases": [item.case_id.root for item in cases],
                "paired_results_count": report.paired_results_count,
                "comparable_results_count": report.comparable_results_count,
                "future_leakage_count": report.future_leakage_count,
                "output": str(aggregate),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
