#!/usr/bin/env python3
"""Compile the U6-A readout and Track C/D lifecycle plan without model calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from novel_agent.services.u6a_readout_plan import U6AReadoutPlanCompiler


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basis-manifest", type=Path, required=True)
    parser.add_argument("--source-readout-manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = U6AReadoutPlanCompiler(
        basis_manifest_path=args.basis_manifest,
        source_readout_manifest_path=args.source_readout_manifest,
        output_root=args.output_root,
        experiment_id=args.experiment_id,
    ).run()
    print(
        json.dumps(
            {
                "status": result.plan.status,
                "plan": str(result.plan_path),
                "plan_ref": result.plan_ref.artifact_id.root,
                "qa_task_count": result.plan.qa_task_count,
                "context_task_count": result.plan.context_task_count,
                "canary_job_count": result.plan.canary_job_count,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
