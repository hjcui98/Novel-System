"""Content-addressed artifacts and the five canonical root references."""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, SchemaVersion


class RootKind(StrEnum):
    TEXT = "text"
    PLAN = "plan"
    WORLD = "world"
    REFERENCE = "reference"
    PROJECT_PROFILE = "project_profile"


class ArtifactRef(DomainModel):
    artifact_id: ArtifactId
    media_type: str = Field(min_length=1, max_length=255)
    byte_length: int = Field(ge=0)
    schema_version: SchemaVersion


class TextRootRef(ArtifactRef):
    root_kind: Literal[RootKind.TEXT] = RootKind.TEXT


class PlanRootRef(ArtifactRef):
    root_kind: Literal[RootKind.PLAN] = RootKind.PLAN


class WorldRootRef(ArtifactRef):
    root_kind: Literal[RootKind.WORLD] = RootKind.WORLD


class ReferenceRootRef(ArtifactRef):
    root_kind: Literal[RootKind.REFERENCE] = RootKind.REFERENCE


class ProjectProfileRootRef(ArtifactRef):
    root_kind: Literal[RootKind.PROJECT_PROFILE] = RootKind.PROJECT_PROFILE


class RootManifest(DomainModel):
    project_id: ProjectId
    schema_version: SchemaVersion
    text_root: TextRootRef
    plan_root: PlanRootRef
    world_root: WorldRootRef
    reference_root: ReferenceRootRef
    project_profile_root: ProjectProfileRootRef
    parent_commit_ids: tuple[CommitId, ...] = ()
