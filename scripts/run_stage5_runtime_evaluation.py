#!/usr/bin/env python3
"""Execute and freeze one production Planner -> Memory -> Writer -> Curator run."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from novel_agent.domain.creative_runtime import CreativeRunRequest
from novel_agent.domain.stage5_evaluation import VerticalRunStatus
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.runtime.creative_assembly import (
    ProductionAssemblyContext,
    load_production_runtime_assembly,
)
from novel_agent.runtime.vertical_runner import VerticalCreativeRunner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--object-store-root", type=Path, required=True)
    parser.add_argument("--assembly-factory", required=True)
    parser.add_argument("--max-tasks", type=int, required=True)
    parser.add_argument("--max-slices", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    request = CreativeRunRequest.model_validate_json(args.request.read_bytes())
    manifest = load_stage5_manifest(args.manifest)
    assembly = load_production_runtime_assembly(
        args.assembly_factory,
        ProductionAssemblyContext(
            database_url=args.database_url,
            object_store_root=args.object_store_root,
            project_id=request.project_id,
            run_id=request.run_id,
            policy=request.policy,
            manifest=manifest,
        ),
    )
    report = asyncio.run(
        VerticalCreativeRunner(
            runtime=assembly.runtime,
            dispatcher=assembly.dispatcher,
            tasks=assembly.task_reader,
        ).run(request, max_tasks=args.max_tasks, max_slices=args.max_slices)
    )
    args.output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(report.model_dump_json(indent=2))
    if report.status is VerticalRunStatus.BLOCKED:
        return 2
    if report.status is VerticalRunStatus.RECOVERY_PENDING:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
