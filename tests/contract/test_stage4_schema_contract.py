import json
from pathlib import Path

from novel_agent.domain import planning
from novel_agent.domain.base import DomainModel

ROOT = Path(__file__).parents[2]
SCHEMA_ROOT = ROOT / "schemas" / "stage4"


def test_stage4_schema_exports_match_domain_contracts() -> None:
    models = tuple(
        model
        for model in vars(planning).values()
        if isinstance(model, type)
        and issubclass(model, DomainModel)
        and model is not DomainModel
        and model.__module__ == planning.__name__
    )
    expected = {f"{model.__name__}.schema.json" for model in models}
    assert {path.name for path in SCHEMA_ROOT.glob("*.schema.json")} == expected
    for model in models:
        exported = json.loads((SCHEMA_ROOT / f"{model.__name__}.schema.json").read_text())
        assert exported == model.model_json_schema()
