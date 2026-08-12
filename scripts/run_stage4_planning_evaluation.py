#!/usr/bin/env python3
"""Run a frozen seven-mode Stage 4 evaluation through a configured runtime."""

from __future__ import annotations

import argparse
import importlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.ids import SchemaVersion
from novel_agent.domain.planning import PlanningEvaluationManifest, PlanningEvaluationObservation
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.planning_evaluation import (
    BlindEvaluator,
    FakePlanningEvaluationAdapter,
    PlanningEvaluationAdapter,
    PlanningEvaluationArm,
    PlanningEvaluationRunner,
    load_frozen_planning_evaluation_gate,
    load_planning_evaluation_case,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-directory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--pilot", type=Path)
    parser.add_argument("--rubric", type=Path)
    parser.add_argument("--thresholds", type=Path)
    runtime = parser.add_mutually_exclusive_group(required=True)
    runtime.add_argument(
        "--runtime-factory",
        help="dotted callable returning (configured_adapter, post_freeze_blind_evaluator)",
    )
    runtime.add_argument(
        "--fake-results",
        type=Path,
        help=(
            "deterministic PlanningEvaluationObservation mapping only; "
            "the report is never Gate-eligible"
        ),
    )
    parser.add_argument("--output-store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--arms",
        default=",".join(arm.value for arm in PlanningEvaluationArm),
        help="comma-separated PlanningEvaluationArm values",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = PlanningEvaluationManifest.model_validate_json(args.manifest.read_bytes())
    loaded_cases = tuple(
        load_planning_evaluation_case(path)
        for path in sorted(args.case_directory.glob("*/case.json"))
    )
    if not loaded_cases:
        raise ValueError("formal Stage 4 evaluation requires seven case files")
    loaded_by_id = {case.case_id: case for case in loaded_cases}
    manifest_by_id = {case.case_id: case for case in manifest.cases}
    if len(loaded_by_id) != len(loaded_cases) or loaded_by_id != manifest_by_id:
        raise ValueError("Stage 4 case files differ from the frozen manifest")
    artifacts = ArtifactRepository(FilesystemObjectStore(args.output_store))
    frozen_gate = None
    if args.runtime_factory is not None:
        if args.pilot is None or args.rubric is None or args.thresholds is None:
            raise ValueError("formal Stage 4 runtime requires --pilot, --rubric, and --thresholds")
        frozen_gate = load_frozen_planning_evaluation_gate(
            pilot_path=args.pilot,
            rubric_path=args.rubric,
            threshold_path=args.thresholds,
            artifacts=artifacts,
            schema_version=SchemaVersion("1.0.0"),
        )
    adapter, evaluator = (
        _load_runtime(args.runtime_factory)
        if args.runtime_factory is not None
        else (_load_fake(args.fake_results), None)
    )
    arms = _parse_arms(args.arms)
    report, report_ref = PlanningEvaluationRunner(
        adapter=adapter,
        artifacts=artifacts,
        schema_version=SchemaVersion("1.0.0"),
        blind_evaluator=evaluator,
        frozen_gate=frozen_gate,
    ).run(manifest, arms=arms)
    payload = (
        json.dumps(
            {
                "report_ref": report_ref.model_dump(mode="json"),
                "report": report.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists() and args.output.read_text(encoding="utf-8") != payload:
        raise RuntimeError("refusing to overwrite different formal Stage 4 evidence")
    args.output.write_text(payload, encoding="utf-8")
    return 0


def _load_runtime(dotted: str) -> tuple[PlanningEvaluationAdapter, BlindEvaluator]:
    module_name, separator, attribute = dotted.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("--runtime-factory must use module.path:callable")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute)
    adapter, evaluator = factory()
    if evaluator is None:
        raise ValueError("formal Stage 4 runtime factory must return a blind evaluator")
    return cast(PlanningEvaluationAdapter, adapter), cast(BlindEvaluator, evaluator)


def _load_fake(path: Path) -> FakePlanningEvaluationAdapter:
    raw_results = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_results, dict):
        raise ValueError("fake Stage 4 result file must be an object")
    results: dict[tuple[str, str], PlanningEvaluationObservation] = {}
    for identity, raw in raw_results.items():
        if not isinstance(identity, str) or "/" not in identity:
            raise ValueError("fake result key must be '<case-id>/<arm>'")
        case_id, arm = identity.rsplit("/", 1)
        results[(case_id, arm)] = PlanningEvaluationObservation.model_validate(raw)
    return FakePlanningEvaluationAdapter(results)


def _parse_arms(raw: str) -> tuple[PlanningEvaluationArm, ...]:
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not values:
        raise ValueError("Stage 4 evaluation requires at least one arm")
    return tuple(PlanningEvaluationArm(value) for value in values)


if __name__ == "__main__":
    raise SystemExit(main())
