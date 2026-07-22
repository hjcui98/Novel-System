"""Reproducible benchmark run configuration."""

from __future__ import annotations

from pydantic import JsonValue, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, RunId, StableId
from novel_agent.domain.model_calls import ModelRole


class EvaluationParameter(DomainModel):
    name: str
    value: JsonValue


class BenchmarkRunConfig(DomainModel):
    config_id: StableId
    benchmark_id: str
    dataset_hash: ArtifactId
    run_id: RunId
    code_version: str
    random_seed: int
    parameters: tuple[EvaluationParameter, ...] = ()
    model_required: bool = False
    model_role: ModelRole | None = None

    @model_validator(mode="after")
    def validate_model_isolation(self) -> BenchmarkRunConfig:
        if self.model_required and self.model_role is not ModelRole.BATCH_TEST:
            raise ValueError("model-required benchmark must use batch_test_model")
        if not self.model_required and self.model_role is not None:
            raise ValueError("deterministic benchmark cannot declare a model role")
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("benchmark parameter names must be unique")
        return self
