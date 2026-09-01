#!/usr/bin/env python3
"""Export the typed U6-D two-project smoke schemas."""

from __future__ import annotations

import json
from pathlib import Path

from novel_agent.domain.u6d_two_project import (
    U6DAdmissionEvidence,
    U6DProjectSmokeResult,
    U6DTwoProjectSmokeReport,
)

ROOT = Path(__file__).resolve().parents[1]
MODELS = (U6DAdmissionEvidence, U6DProjectSmokeResult, U6DTwoProjectSmokeReport)


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
