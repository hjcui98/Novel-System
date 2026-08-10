#!/usr/bin/env python3
"""Validate frozen Stage 4 manifests and assemble deterministic fake-run reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.ids import SchemaVersion
from novel_agent.domain.planning import PlanningEvaluationManifest, PlanningLoopResult
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.planning_evaluation import (
    FakePlanningEvaluationAdapter,
    PlanningEvaluationRunner,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--fake-results", type=Path, required=True)
    parser.add_argument("--output-store", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = PlanningEvaluationManifest.model_validate_json(args.manifest.read_bytes())
    raw_results = json.loads(args.fake_results.read_text(encoding="utf-8"))
    if not isinstance(raw_results, dict):
        raise ValueError("fake Stage 4 result file must be an object")
    results: dict[tuple[str, str], PlanningLoopResult] = {}
    for identity, raw in raw_results.items():
        if not isinstance(identity, str) or "/" not in identity:
            raise ValueError("fake result key must be '<case-id>/<arm>'")
        case_id, arm = identity.rsplit("/", 1)
        results[(case_id, arm)] = PlanningLoopResult.model_validate(raw)
    artifacts = ArtifactRepository(FilesystemObjectStore(args.output_store))
    report, report_ref = PlanningEvaluationRunner(
        adapter=FakePlanningEvaluationAdapter(results),
        artifacts=artifacts,
        schema_version=SchemaVersion("1.0.0"),
    ).run(manifest)
    print(
        json.dumps(
            {
                "report_ref": report_ref.model_dump(mode="json"),
                "report": report.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
