#!/usr/bin/env python3
"""Export deterministic JSON Schemas for the public Stage 0 domain contract."""

from __future__ import annotations

import json
from pathlib import Path

from novel_agent import domain

OUTPUT_DIRECTORY = Path(__file__).parents[1] / "schemas" / "stage0"
MODEL_NAMES = domain.__all__


def main() -> None:
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for name in MODEL_NAMES:
        model_type = getattr(domain, name)
        if not hasattr(model_type, "model_json_schema"):
            continue
        schema = model_type.model_json_schema()
        target = OUTPUT_DIRECTORY / f"{name}.schema.json"
        target.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
