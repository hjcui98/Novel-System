"""Fail-closed admission manifest for the isolated Stage 5 runtime kernel."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import SchemaVersion


class Stage5FeatureAdmission(DomainModel):
    real_stage4_adapter: bool = False
    multi_worker_lease: bool = False
    scheduled_fire: bool = False
    external_hook_ingress: bool = False
    skill_evolution: bool = False
    temporal_adapter: bool = False


class Stage5DevelopmentManifest(DomainModel):
    runtime_contract_version: SchemaVersion
    stage2_base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    stage2_schema_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stage3_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    stage3_contract_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stage3_gate: Literal["CONDITIONAL"] = "CONDITIONAL"
    stage4_port_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stage4_implementation_status: Literal["DEFERRED", "INTEGRATED"] = "DEFERRED"
    commit_projection_contract_version: SchemaVersion
    commit_projection_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifact_runtime_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    configuration_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model_admission_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    skill_registry_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    projection_contract_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    feature_admission: Stage5FeatureAdmission

    @model_validator(mode="after")
    def validate_isolated_kernel(self) -> Stage5DevelopmentManifest:
        deferred = self.feature_admission.model_dump(exclude={"real_stage4_adapter"})
        if any(deferred.values()):
            raise ValueError("deferred Stage 5 features cannot be admitted by the A-layer manifest")
        if self.stage4_implementation_status == "DEFERRED" and (
            self.feature_admission.real_stage4_adapter
        ):
            raise ValueError("a deferred Stage 4 adapter cannot be admitted")
        if self.stage4_implementation_status == "INTEGRATED" and not (
            self.feature_admission.real_stage4_adapter
        ):
            raise ValueError("an integrated Stage 4 adapter must be admitted")
        return self


def load_stage5_manifest(path: Path) -> Stage5DevelopmentManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Stage 5 manifest is missing or invalid") from error
    manifest = Stage5DevelopmentManifest.model_validate(payload, strict=True)
    # Source fingerprints remain historical provenance only. Development admission is
    # decided by typed versions/features and the final behavioural test, not by repeatedly
    # hashing mutable source files during integration.
    return manifest


__all__ = ["Stage5DevelopmentManifest", "Stage5FeatureAdmission", "load_stage5_manifest"]
