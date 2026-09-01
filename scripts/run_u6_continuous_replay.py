#!/usr/bin/env python3
"""Freeze the U6-A V0.5 basis after one sequential C0..C300 ingest.

This command deliberately stops at ``BASIS_FROZEN``.  Writer readout,
evaluator reveal, and evaluation-namespace discard are a later phase and are
not silently counted as completed by basis preparation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from novel_agent.domain.ids import ProjectId
from novel_agent.services.u6_continuous_replay import U6ContinuousReplayService

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle-root",
        type=Path,
        default=ROOT / "benchmarks/private/ztj_novelmem_v0.5",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--project-id")
    return parser


def main() -> int:
    args = _parser().parse_args()
    service = U6ContinuousReplayService(
        bundle_root=args.bundle_root,
        output_root=args.output_root,
        experiment_id=args.experiment_id,
        project_id=ProjectId(args.project_id) if args.project_id else None,
    )
    result = service.run()
    print(
        json.dumps(
            {
                "status": result.report.status,
                "campaign_id": result.report.campaign_id.root,
                "basis_manifest": str(result.manifest_path),
                "report": str(result.report_path),
                "object_root": str(result.object_root),
                "chapters_ingested": result.report.chapters_ingested,
                "ingest_passes": result.report.ingest_passes,
                "basis_count": result.report.basis_count,
                "canary_job_count": result.report.canary_job_count,
                "expected_readout_task_count": result.report.expected_readout_task_count,
                "completed_readout_task_count": result.report.completed_readout_task_count,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
