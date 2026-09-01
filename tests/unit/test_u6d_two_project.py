"""Unit contracts for the U6-D two-project admission evidence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from scripts.run_u6d_two_project_smoke import _task_belongs_to_run

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, SchemaVersion
from novel_agent.domain.u6d_two_project import (
    U6DAdmissionEvidence,
    U6DProjectSmokeResult,
    U6DTwoProjectSmokeReport,
)


def _ref(digit: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + digit * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )


def _project(suffix: str) -> U6DProjectSmokeResult:
    commit = CommitId("sha256:" + suffix * 64)
    return U6DProjectSmokeResult(
        project_id=ProjectId(f"project.u6d.{suffix}"),
        run_id=RunId(f"run.u6d.{suffix}"),
        basis_commit=commit,
        final_commit=commit,
        status="PASS",
        completed_chapters=(21,),
        chain_task_kinds=("plan_commit@C20:succeeded", "projection_freshness@C21:succeeded"),
        event_count=10,
        task_count=7,
        attempt_count=7,
        effect_count=2,
        commit_count=2,
        model_call_count=4,
        object_store_root=f"/tmp/u6d-{suffix}",
        evidence_artifact_refs=(_ref(suffix),),
    )


def _report(**updates: object) -> U6DTwoProjectSmokeReport:
    payload: dict[str, object] = {
        "experiment_id": "u6d.unit",
        "database_descriptor": "sqlite:///u6d.db",
        "status": "PASS",
        "projects": (_project("a"), _project("b")),
        "admission": U6DAdmissionEvidence(
            endpoint_request_limit=2,
            acquired_requests=4,
            released_requests=4,
            max_inflight_requests=2,
            inflight_requests_after_run=0,
            endpoint_admission_shared=True,
        ),
        "worker_stop_project_id": ProjectId("project.u6d.a"),
        "worker_stop_status": "natural-boundary:pending",
        "other_project_unaffected": True,
        "shared_composition_verified": True,
        "cross_project_leakage_count": 0,
        "duplicate_effect_count": 0,
    }
    payload.update(updates)
    return U6DTwoProjectSmokeReport(**payload)


def test_u6d_pass_requires_two_clean_isolated_projects() -> None:
    report = _report()
    assert report.status == "PASS"
    assert report.admission.endpoint_request_limit == 2
    assert {item.project_id for item in report.projects} == {
        ProjectId("project.u6d.a"),
        ProjectId("project.u6d.b"),
    }

    with pytest.raises(ValidationError, match="two isolated project/run identities"):
        _report(projects=(_project("a"), _project("a")))


def test_u6d_pass_cannot_hide_admission_or_isolation_failure() -> None:
    with pytest.raises(ValidationError, match="PASS contradicts"):
        _report(other_project_unaffected=False)
    with pytest.raises(ValidationError, match="PASS contradicts"):
        _report(shared_composition_verified=False)
    with pytest.raises(ValidationError, match="PASS contradicts"):
        _report(cross_project_leakage_count=1)


def test_u6d_admission_requires_release_balance_and_empty_tail() -> None:
    with pytest.raises(ValidationError, match="unreleased"):
        U6DAdmissionEvidence(
            endpoint_request_limit=2,
            acquired_requests=2,
            released_requests=1,
            max_inflight_requests=2,
            inflight_requests_after_run=0,
            endpoint_admission_shared=True,
        )
    with pytest.raises(ValidationError, match="retained"):
        U6DAdmissionEvidence(
            endpoint_request_limit=2,
            acquired_requests=2,
            released_requests=2,
            max_inflight_requests=2,
            inflight_requests_after_run=1,
            endpoint_admission_shared=True,
        )


def test_u6d_run_scoped_settlement_owner_is_not_cross_project_leakage() -> None:
    run_id = RunId("run.u6d.audit")
    expected = {"run.u6d.audit.plan"}
    assert _task_belongs_to_run("run.u6d.audit.plan", run_id, expected)
    assert _task_belongs_to_run("run.u6d.audit.settlement", run_id, expected)
    assert not _task_belongs_to_run("run.other.audit.settlement", run_id, expected)
