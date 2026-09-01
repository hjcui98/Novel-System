"""Schema contract for U6-E endurance evidence."""

from __future__ import annotations

import json
from pathlib import Path

from novel_agent.domain.u6e_endurance import (
    U6EEnduranceReport,
    U6EHealthProbe,
    U6EHistoryGrowth,
    U6EWorkerPhaseReport,
)

ROOT = Path(__file__).parents[2]


def test_u6e_schemas_match_domain_models() -> None:
    for model in (U6EEnduranceReport, U6EHealthProbe, U6EHistoryGrowth, U6EWorkerPhaseReport):
        path = ROOT / "schemas" / "stage5" / f"{model.__name__}.schema.json"
        assert json.loads(path.read_text(encoding="utf-8")) == model.model_json_schema()
