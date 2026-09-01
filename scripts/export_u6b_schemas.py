#!/usr/bin/env python3
"""Export the isolated U6-B production evidence schemas."""

from __future__ import annotations

import json
from pathlib import Path

from novel_agent.domain.u6b_production import (
    U6BCompactionEvidence,
    U6BPhaseUsage,
    U6BProductionBaselineReport,
    U6BWorkerPhaseReport,
)

ROOT = Path(__file__).resolve().parents[1]
MODELS = (
    U6BCompactionEvidence,
    U6BPhaseUsage,
    U6BProductionBaselineReport,
    U6BWorkerPhaseReport,
)


def main() -> int:
    output = ROOT / "schemas" / "stage5"
    output.mkdir(parents=True, exist_ok=True)
    for model in MODELS:
        (output / f"{model.__name__}.schema.json").write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
