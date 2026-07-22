from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from novel_agent import domain
from novel_agent.domain.artifacts import (
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.base import DomainModel
from novel_agent.domain.changes import CommitResult, CommitStatus
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, SchemaVersion, StableId
from novel_agent.domain.text import (
    EvidenceRef,
    EvidenceSupportStatus,
    QuoteHash,
    TextSpanRef,
)
from novel_agent.domain.world import StoryTime

REPOSITORY_ROOT = Path(__file__).parents[2]
DOMAIN_DIRECTORY = REPOSITORY_ROOT / "src" / "novel_agent" / "domain"
SCHEMA_DIRECTORY = REPOSITORY_ROOT / "schemas" / "stage0"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def root_manifest() -> RootManifest:
    version = SchemaVersion("0.1.0")
    return RootManifest(
        project_id=ProjectId("project.test"),
        schema_version=version,
        text_root=TextRootRef(
            artifact_id=ArtifactId(HASH_A),
            media_type="application/json",
            byte_length=10,
            schema_version=version,
        ),
        plan_root=PlanRootRef(
            artifact_id=ArtifactId(HASH_B),
            media_type="application/json",
            byte_length=10,
            schema_version=version,
        ),
        world_root=WorldRootRef(
            artifact_id=ArtifactId("sha256:" + "c" * 64),
            media_type="application/json",
            byte_length=10,
            schema_version=version,
        ),
        reference_root=ReferenceRootRef(
            artifact_id=ArtifactId("sha256:" + "d" * 64),
            media_type="application/json",
            byte_length=10,
            schema_version=version,
        ),
        project_profile_root=ProjectProfileRootRef(
            artifact_id=ArtifactId("sha256:" + "e" * 64),
            media_type="application/json",
            byte_length=10,
            schema_version=version,
        ),
    )


def test_domain_models_are_strict_frozen_and_reject_extra_fields() -> None:
    assert DomainModel.model_config["strict"] is True
    assert DomainModel.model_config["frozen"] is True
    assert DomainModel.model_config["extra"] == "forbid"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TextSpanRef(block_id=StableId("block.1"), start=0, end=1, unknown=True)  # type: ignore[call-arg]

    span = TextSpanRef(block_id=StableId("block.1"), start=0, end=1)
    with pytest.raises(ValidationError, match="Instance is frozen"):
        span.start = 1  # type: ignore[misc]


def test_ids_and_schema_versions_are_not_coerced_or_loosely_accepted() -> None:
    assert ProjectId("project.1").root == "project.1"
    assert ArtifactId(HASH_A).root == HASH_A
    assert SchemaVersion("0.1.0").root == "0.1.0"

    with pytest.raises(ValidationError):
        ProjectId(123)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ArtifactId("not-a-content-hash")
    with pytest.raises(ValidationError):
        SchemaVersion("v1")


def test_evidence_is_bound_to_immutable_content_and_valid_range() -> None:
    span = TextSpanRef(block_id=StableId("block.1"), start=2, end=7)
    evidence = EvidenceRef(
        evidence_id=StableId("evidence.1"),
        root_hash=ArtifactId(HASH_A),
        object_hash=ArtifactId(HASH_B),
        chapter_id=StableId("chapter.1"),
        span=span,
        quote_hash=QuoteHash(HASH_A),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=CommitId(HASH_B),
    )

    assert evidence.span == span
    assert evidence.quote_hash == QuoteHash(HASH_A)
    with pytest.raises(ValidationError, match="span end"):
        TextSpanRef(block_id=StableId("block.1"), start=7, end=2)


def test_story_time_rejects_reversed_intervals() -> None:
    assert StoryTime(worldline="main", start_ordinal=1, end_ordinal=2).end_ordinal == 2
    assert StoryTime(worldline="main", label="unknown").label == "unknown"
    with pytest.raises(ValidationError, match="story time end"):
        StoryTime(worldline="main", start_ordinal=2, end_ordinal=1)


def test_commit_result_enforces_outcome_specific_fields() -> None:
    accepted = CommitResult(
        request_id=StableId("request.1"),
        status=CommitStatus.ACCEPTED,
        commit_id=CommitId(HASH_A),
        manifest=root_manifest(),
        committed_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    rejected = CommitResult(
        request_id=StableId("request.2"),
        status=CommitStatus.REJECTED,
        reason="base commit changed",
    )

    assert accepted.commit_id == CommitId(HASH_A)
    assert rejected.reason == "base commit changed"
    with pytest.raises(ValidationError, match="accepted commit requires"):
        CommitResult(request_id=StableId("request.3"), status=CommitStatus.ACCEPTED)
    with pytest.raises(ValidationError, match="non-accepted commit requires"):
        CommitResult(request_id=StableId("request.4"), status=CommitStatus.CONFLICTED)


def test_checked_in_json_schemas_match_public_models() -> None:
    exported_names: set[str] = set()
    for name in domain.__all__:
        model_type = getattr(domain, name)
        if not isinstance(model_type, type) or not issubclass(model_type, BaseModel):
            continue
        expected = model_type.model_json_schema()
        actual = json.loads((SCHEMA_DIRECTORY / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert actual == expected
        exported_names.add(name)

    assert {path.name.removesuffix(".schema.json") for path in SCHEMA_DIRECTORY.iterdir()} == (
        exported_names
    )


def test_domain_layer_has_no_framework_or_adapter_imports() -> None:
    forbidden_roots = {
        "langchain",
        "langgraph",
        "sqlalchemy",
        "opensearchpy",
        "neo4j",
        "boto3",
        "fastapi",
    }
    imported_roots: set[str] = set()
    for path in DOMAIN_DIRECTORY.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

    assert imported_roots.isdisjoint(forbidden_roots)
