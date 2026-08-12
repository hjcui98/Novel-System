#!/usr/bin/env python3
"""Run Stage 3 formal evaluation through an application-provided real full-chain runtime."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from novel_agent.domain.stage3_loop_evaluation import Stage3FormalManifest
from novel_agent.services.stage3_evaluation import load_case
from novel_agent.services.stage3_loop_evaluation import (
    Stage3FullChainEvaluationService,
    Stage3FullChainRuntimeFactory,
    Stage3PostFreezeEvaluator,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-directory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--runtime-factory",
        required=True,
        help="dotted callable returning (runtime_factory, post_freeze_evaluator)",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases = tuple(load_case(path) for path in sorted(args.case_directory.glob("*/case.json")))
    if not cases:
        raise ValueError("formal Stage 3 evaluation requires at least one case")
    manifest = Stage3FormalManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    runtime_factory, evaluator = _load_runtime(args.runtime_factory)
    report = asyncio.run(
        Stage3FullChainEvaluationService().run(
            cases,
            manifest,
            runtime_factory,
            evaluator,
        )
    )
    payload = (
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists() and args.output.read_text(encoding="utf-8") != payload:
        raise RuntimeError("refusing to overwrite different formal Stage 3 evidence")
    args.output.write_text(payload, encoding="utf-8")
    return 0


def _load_runtime(
    dotted: str,
) -> tuple[Stage3FullChainRuntimeFactory, Stage3PostFreezeEvaluator]:
    module_name, separator, attribute = dotted.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("--runtime-factory must use module.path:callable")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute)
    runtime, evaluator = factory()
    return (
        cast(Stage3FullChainRuntimeFactory, runtime),
        cast(Stage3PostFreezeEvaluator, evaluator),
    )


if __name__ == "__main__":
    raise SystemExit(main())
