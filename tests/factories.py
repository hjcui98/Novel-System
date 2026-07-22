from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    CommitRequest,
    ObservedChangeSet,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
)

SCHEMA_VERSION = SchemaVersion("0.1.0")


def content_hash(character: str) -> ArtifactId:
    return ArtifactId("sha256:" + character * 64)


def make_artifact(character: str = "f") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=content_hash(character),
        media_type="application/json",
        byte_length=10,
        schema_version=SCHEMA_VERSION,
    )


def make_manifest(
    project_id: ProjectId | None = None,
    parent_commit_ids: tuple[CommitId, ...] = (),
    *,
    root_offset: int = 0,
) -> RootManifest:
    project_id = project_id or ProjectId("project.test")
    characters = [format((root_offset + index) % 16, "x") for index in range(5)]
    common: dict[str, Any] = {
        "media_type": "application/json",
        "byte_length": 10,
        "schema_version": SCHEMA_VERSION,
    }
    return RootManifest(
        project_id=project_id,
        schema_version=SCHEMA_VERSION,
        text_root=TextRootRef(artifact_id=content_hash(characters[0]), **common),
        plan_root=PlanRootRef(artifact_id=content_hash(characters[1]), **common),
        world_root=WorldRootRef(artifact_id=content_hash(characters[2]), **common),
        reference_root=ReferenceRootRef(artifact_id=content_hash(characters[3]), **common),
        project_profile_root=ProjectProfileRootRef(
            artifact_id=content_hash(characters[4]), **common
        ),
        parent_commit_ids=parent_commit_ids,
    )


def make_commit_request(
    base_commit: CommitId,
    *,
    project_id: ProjectId | None = None,
    idempotency_key: str = "commit.key.1",
    root_offset: int = 5,
) -> CommitRequest:
    project_id = project_id or ProjectId("project.test")
    bundle_id = StableId(f"bundle.{idempotency_key}")
    observed = ObservedChangeSet(
        change_set_id=StableId(f"changes.{idempotency_key}"),
        base_commit=base_commit,
        source_artifact=make_artifact("f"),
    )
    bundle = CandidateChangeBundle(
        bundle_id=bundle_id,
        project_id=project_id,
        run_id=RunId("run.test"),
        base_commit=base_commit,
        observed_changes=observed,
        proposed_roots=make_manifest(project_id, (base_commit,), root_offset=root_offset),
    )
    report = ValidationReport(
        report_id=StableId(f"report.{idempotency_key}"),
        bundle_id=bundle_id,
        status=ValidationStatus.PASSED,
        schema_version=SCHEMA_VERSION,
        validated_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    return CommitRequest(
        request_id=StableId(f"request.{idempotency_key}"),
        project_id=project_id,
        base_commit=base_commit,
        idempotency_key=StableId(idempotency_key),
        bundle=bundle,
        validation_report=report,
    )
