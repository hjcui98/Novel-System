#!/usr/bin/env python3
"""Validate and publish a formal isolated Stage 5 evaluation report."""

from __future__ import annotations

import argparse
from pathlib import Path

from novel_agent.domain.stage5_evaluation import (
    IsolatedKernelStatus,
    Stage5IsolatedKernelReport,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = Stage5IsolatedKernelReport.model_validate_json(args.report.read_bytes())
    print(report.model_dump_json(indent=2))
    return 0 if report.status is IsolatedKernelStatus.PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
