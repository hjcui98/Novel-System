"""Fail-closed admission manifest for the isolated Stage 5 runtime kernel."""

from __future__ import annotations

import hashlib
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
    stage4_implementation_status: Literal["DEFERRED"] = "DEFERRED"
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
        if any(self.feature_admission.model_dump().values()):
            raise ValueError("deferred Stage 5 features cannot be admitted by the A-layer manifest")
        return self


def load_stage5_manifest(path: Path) -> Stage5DevelopmentManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Stage 5 manifest is missing or invalid") from error
    manifest = Stage5DevelopmentManifest.model_validate(payload, strict=True)
    repository_root = path.parents[3]
    expected = {
        "stage2_schema_fingerprint": repository_root / "schemas/stage2/PlanningTask.schema.json",
        "stage3_contract_fingerprint": repository_root
        / "tests/golden/stage3_writer/schema_manifest.json",
        "stage4_port_fingerprint": repository_root / "src/novel_agent/domain/creative_runtime.py",
        "commit_projection_fingerprint": repository_root / "src/novel_agent/services/commits.py",
        "artifact_runtime_fingerprint": repository_root / "src/novel_agent/domain/runtime.py",
        "configuration_fingerprint": repository_root / "src/novel_agent/config.py",
        "model_admission_fingerprint": repository_root
        / "src/novel_agent/services/model_request_admission.py",
        "skill_registry_fingerprint": repository_root / "src/novel_agent/skills/registry.py",
        "projection_contract_fingerprint": repository_root
        / "src/novel_agent/services/projection.py",
    }
    for field, source in expected.items():
        try:
            actual = f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}"
        except OSError as error:
            raise RuntimeError(f"Stage 5 manifest source is missing: {source}") from error
        if getattr(manifest, field) != actual:
            raise RuntimeError(f"Stage 5 manifest fingerprint mismatch: {field}")
    return manifest


__all__ = ["Stage5DevelopmentManifest", "Stage5FeatureAdmission", "load_stage5_manifest"]
