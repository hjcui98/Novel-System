#!/usr/bin/env python3
"""Export deterministic public Stage 5 schemas and a golden manifest."""

from __future__ import annotations

import json
from pathlib import Path

from novel_agent.domain import (
    creative_runtime,
    evolution,
    production_assembly,
    recovery_reasoning,
    runtime,
    stage5_evaluation,
    stage5_manifest,
    u6b_production,
    u6c_fault_matrix,
    u6d_two_project,
    u6e_endurance,
)
from novel_agent.domain.base import DomainModel

OUTPUT_DIRECTORY = Path(__file__).parents[1] / "schemas" / "stage5"
DOMAIN_MODULES = (
    creative_runtime,
    evolution,
    production_assembly,
    recovery_reasoning,
    runtime,
    stage5_evaluation,
    stage5_manifest,
    u6b_production,
    u6c_fault_matrix,
    u6d_two_project,
    u6e_endurance,
)
MODELS = tuple(
    model
    for module in DOMAIN_MODULES
    for model in vars(module).values()
    if isinstance(model, type)
    and issubclass(model, DomainModel)
    and model is not DomainModel
    and model.__module__ == module.__name__
)


def main() -> None:
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    expected: set[str] = set()
    for model in sorted(MODELS, key=lambda item: item.__name__):
        expected.add(f"{model.__name__}.schema.json")
        target = OUTPUT_DIRECTORY / f"{model.__name__}.schema.json"
        target.write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    for stale in OUTPUT_DIRECTORY.glob("*.schema.json"):
        if stale.name not in expected:
            stale.unlink()


if __name__ == "__main__":
    main()
