#!/usr/bin/env python3
"""Create a profile-separated Stage 2M comparison without merging denominators."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from novel_agent.domain.memory_benchmark import MemoryBenchmarkUnifiedReport
from novel_agent.services.memory_benchmark_reporting import MemoryBenchmarkReporter


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--visible-report", type=Path, required=True)
    value.add_argument("--planned-report", type=Path, required=True)
    value.add_argument("--output-directory", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    visible = MemoryBenchmarkUnifiedReport.model_validate_json(
        args.visible_report.read_text("utf-8")
    )
    planned = MemoryBenchmarkUnifiedReport.model_validate_json(
        args.planned_report.read_text("utf-8")
    )
    delta = MemoryBenchmarkReporter.cross_profile_delta(visible, planned)
    args.output_directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "comparison_version": "stage2m_cross_profile.v1",
        "visible_at_cutoff_report": str(args.visible_report.resolve()),
        "author_plan_conditioned_report": str(args.planned_report.resolve()),
    }
    (args.output_directory / "comparison_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.output_directory / "profile_delta_report.json").write_text(
        json.dumps(delta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    weighted_delta = cast(float, delta["weighted_coverage_delta"])
    mandatory_delta = cast(float, delta["mandatory_hit_rate_delta"])
    contradiction_delta = cast(float, delta["contradiction_rate_delta"])
    untraceable_delta = cast(float, delta["untraceable_rate_delta"])
    markdown = (
        "# Stage 2M Profile Delta\n\n"
        "The profiles remain independent experiments; deltas are shown side by side "
        "and are not pooled into one score.\n\n"
        f"- Weighted coverage delta: {weighted_delta:.4f}\n"
        f"- Mandatory hit-rate delta: {mandatory_delta:.4f}\n"
        f"- Contradiction-rate delta: {contradiction_delta:.4f}\n"
        f"- Untraceable-rate delta: {untraceable_delta:.4f}\n"
    )
    (args.output_directory / "profile_delta_report.md").write_text(
        markdown,
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
