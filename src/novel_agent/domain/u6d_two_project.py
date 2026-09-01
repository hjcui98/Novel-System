"""Typed evidence for the U6-D two-project production smoke."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import CommitId, ProjectId, RunId


class U6DProjectSmokeResult(DomainModel):
    project_id: ProjectId
    run_id: RunId
    basis_commit: CommitId
    final_commit: CommitId
    status: Literal["PASS", "REVIEW_REQUIRED"]
    completed_chapters: tuple[int, ...]
    chain_task_kinds: tuple[str, ...]
    event_count: int = Field(ge=0)
    task_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    effect_count: int = Field(ge=0)
    commit_count: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    object_store_root: str = Field(min_length=1)
    evidence_artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    notes: tuple[str, ...] = ()


class U6DAdmissionEvidence(DomainModel):
    endpoint_request_limit: int = Field(ge=1)
    acquired_requests: int = Field(ge=0)
    released_requests: int = Field(ge=0)
    max_inflight_requests: int = Field(ge=0)
    inflight_requests_after_run: int = Field(ge=0)
    endpoint_admission_shared: bool

    @model_validator(mode="after")
    def validate_release_balance(self) -> U6DAdmissionEvidence:
        if self.acquired_requests != self.released_requests:
            raise ValueError("U6-D admission has unreleased endpoint reservations")
        if self.max_inflight_requests > self.endpoint_request_limit:
            raise ValueError("U6-D admission exceeded the endpoint request limit")
        if self.inflight_requests_after_run != 0:
            raise ValueError("U6-D admission retained an inflight request")
        return self


class U6DTwoProjectSmokeReport(DomainModel):
    report_schema: Literal["u6d-two-project-smoke.v1"] = "u6d-two-project-smoke.v1"
    experiment_id: str = Field(min_length=1, max_length=160)
    database_descriptor: str = Field(min_length=1, max_length=240)
    status: Literal["PASS", "REVIEW_REQUIRED"]
    projects: tuple[U6DProjectSmokeResult, U6DProjectSmokeResult]
    admission: U6DAdmissionEvidence
    worker_stop_project_id: ProjectId
    worker_stop_status: str = Field(min_length=1, max_length=160)
    other_project_unaffected: bool
    shared_composition_verified: bool
    cross_project_leakage_count: int = Field(ge=0)
    duplicate_effect_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_smoke(self) -> U6DTwoProjectSmokeReport:
        project_ids = tuple(item.project_id for item in self.projects)
        run_ids = tuple(item.run_id for item in self.projects)
        if len(set(project_ids)) != 2 or len(set(run_ids)) != 2:
            raise ValueError("U6-D requires two isolated project/run identities")
        if self.status == "PASS" and (
            any(item.status != "PASS" for item in self.projects)
            or not self.admission.endpoint_admission_shared
            or not self.other_project_unaffected
            or not self.shared_composition_verified
            or self.cross_project_leakage_count
            or self.duplicate_effect_count
        ):
            raise ValueError("U6-D PASS contradicts isolation or admission evidence")
        return self


__all__ = [
    "U6DAdmissionEvidence",
    "U6DProjectSmokeResult",
    "U6DTwoProjectSmokeReport",
]
