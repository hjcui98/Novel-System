"""Schema contract for the U6-D two-project evidence models."""

from __future__ import annotations

import json
from pathlib import Path

from novel_agent.domain.u6d_two_project import (
    U6DAdmissionEvidence,
    U6DProjectSmokeResult,
    U6DTwoProjectSmokeReport,
)

ROOT = Path(__file__).parents[2]


def test_u6d_schemas_match_domain_models() -> None:
    for model in (U6DAdmissionEvidence, U6DProjectSmokeResult, U6DTwoProjectSmokeReport):
        path = ROOT / "schemas" / "stage5" / f"{model.__name__}.schema.json"
        assert json.loads(path.read_text(encoding="utf-8")) == model.model_json_schema()
