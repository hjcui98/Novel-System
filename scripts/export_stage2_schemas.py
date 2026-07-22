#!/usr/bin/env python3
"""Export deterministic Stage 2 agent harness schemas."""

from __future__ import annotations

import json
from pathlib import Path

from novel_agent.domain import stage2
from novel_agent.domain.base import DomainModel

OUTPUT_DIRECTORY = Path(__file__).parents[1] / "schemas" / "stage2"
MODELS = tuple(
    model
    for model in vars(stage2).values()
    if isinstance(model, type)
    and issubclass(model, DomainModel)
    and model is not DomainModel
    and model.__module__ == stage2.__name__
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
