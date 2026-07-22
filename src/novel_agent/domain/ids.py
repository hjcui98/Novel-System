"""Stable business identities and content-addressed identities."""

from typing import Annotated

from pydantic import ConfigDict, RootModel, StringConstraints

StableIdValue = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
]
ContentHashValue = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
SchemaVersionValue = Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]


class StableId(RootModel[StableIdValue]):
    model_config = ConfigDict(strict=True, frozen=True)


class ProjectId(StableId):
    pass


class RunId(StableId):
    pass


class TaskId(StableId):
    pass


class CommitId(RootModel[ContentHashValue]):
    model_config = ConfigDict(strict=True, frozen=True)


class ArtifactId(RootModel[ContentHashValue]):
    model_config = ConfigDict(strict=True, frozen=True)


class SchemaVersion(RootModel[SchemaVersionValue]):
    model_config = ConfigDict(strict=True, frozen=True)
