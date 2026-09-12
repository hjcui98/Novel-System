"""Author-declared planning locks compiled without model interpretation.

The brief may carry explicit progress and reveal locks ("最早第三卷",
``not_before_chapter = 201``).  A model-mediated profile extraction can drop
them silently, which leaves the Planner and the Writer without the very
constraints the author declared.  This module gives the author a frozen,
machine-readable channel: the locks are validated here and merged verbatim
into the project profile, so ``compile_author_constraint_root`` and the Stage 3
writer lock projection both see them.

Anchors are alternated on purpose.  ``anchor`` carries the human-facing
wording; ``not_before_chapter`` and ``chapter_earliest`` are absolute chapter
boundaries and are never derived from the anchor, so a downstream horizon
cannot be used to satisfy a future-dated lock.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum

from pydantic import Field, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.services.content_addressing import content_id

AUTHOR_PLANNING_LOCKS_MEDIA_TYPE = "application/vnd.novel-agent.author-planning-locks+json"
ROOT_HASH_PLACEHOLDER = ArtifactId("sha256:" + "0" * 64)


class PlanningLockCategory(StrEnum):
    TIMELINE = "timeline"
    REVEAL = "reveal"
    PROGRESSION = "progression"
    EQUIPMENT = "equipment"
    LOCATION = "location"


class AuthorPlanningLock(DomainModel):
    """One author-declared window; independent of any planning horizon."""

    lock_id: StableId
    category: PlanningLockCategory
    description: str = Field(min_length=1)
    anchor: str | None = None
    not_before_chapter: int | None = Field(default=None, ge=1)
    chapter_earliest: int | None = Field(default=None, ge=1)
    chapter_latest: int | None = Field(default=None, ge=1)
    satisfies: str | None = None

    @model_validator(mode="after")
    def validate_window(self) -> AuthorPlanningLock:
        if (
            self.chapter_earliest is not None
            and self.chapter_latest is not None
            and self.chapter_latest < self.chapter_earliest
        ):
            raise ValueError("author planning lock chapter window is reversed")
        if self.category is not PlanningLockCategory.TIMELINE:
            return self
        if self.not_before_chapter is None or self.chapter_latest is None:
            raise ValueError(
                "timeline lock requires an explicit not_before_chapter and a chapter_latest "
                "bound so a short planning horizon cannot silently satisfy it"
            )
        if self.not_before_chapter > self.chapter_latest:
            raise ValueError("author planning lock not_before_chapter exceeds chapter_latest")
        return self


class AuthorPlanningLocksDocument(DomainModel):
    """Frozen author channel for progress and reveal locks.

    The authoring shape omits ``root_hash``; use
    :func:`load_author_planning_locks` so the stored document carries the
    content address of exactly the locks it declares.
    """

    root_hash: ArtifactId = ROOT_HASH_PLACEHOLDER
    schema_version: SchemaVersion
    project_id: str | None = None
    story_title: str | None = None
    locks: tuple[AuthorPlanningLock, ...] = ()

    @model_validator(mode="after")
    def validate_locks(self) -> AuthorPlanningLocksDocument:
        if len({item.lock_id for item in self.locks}) != len(self.locks):
            raise ValueError("author planning lock ids must be unique")
        return self


def author_planning_locks_content_id(document: AuthorPlanningLocksDocument) -> ArtifactId:
    """Content address of the declared locks, independent of ``root_hash``."""

    return content_id(
        {
            "schema_version": document.schema_version.root,
            "project_id": document.project_id,
            "locks": tuple(item.model_dump(mode="json") for item in document.locks),
        }
    )


def load_author_planning_locks(
    raw: bytes | str | Mapping[str, object],
    *,
    schema_version: SchemaVersion | None = None,
) -> AuthorPlanningLocksDocument:
    """Validate an author lock document and stamp its derived content address."""

    if isinstance(raw, Mapping):
        # Strict domain models keep tuples as tuples; go through the JSON shape
        # so an author file and an in-memory payload validate identically.
        document = AuthorPlanningLocksDocument.model_validate_json(
            json.dumps(dict(raw), ensure_ascii=False)
        )
    else:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        document = AuthorPlanningLocksDocument.model_validate_json(text)
    if schema_version is not None and document.schema_version != schema_version:
        raise ValueError(
            "author planning locks schema version mismatch: "
            f"{document.schema_version.root} != {schema_version.root}"
        )
    declared = document.root_hash
    derived = author_planning_locks_content_id(document)
    if declared.root != ROOT_HASH_PLACEHOLDER.root and declared != derived:
        raise ValueError(
            "author planning locks root_hash does not match the declared locks; "
            "edit the locks and drop root_hash instead of restating a stale hash"
        )
    return document.model_copy(update={"root_hash": derived})


def compile_planning_lock_channels(
    document: AuthorPlanningLocksDocument,
) -> dict[str, list[dict[str, object]]]:
    """Project validated locks onto the keys the Planner and Writer already read.

    The channel keys are the frozen contract consumed by
    :mod:`novel_agent.domain.author_constraints` and by the Stage 3 writer lock
    projection, so a lock reaches both surfaces through the profile alone.
    """

    channels: dict[str, list[dict[str, object]]] = {
        "timeline_locks": [],
        "reveal_windows": [],
        "progression_locks": [],
        "equipment_locks": [],
        "location_preconditions": [],
    }
    channel_by_category = {
        PlanningLockCategory.TIMELINE: "timeline_locks",
        PlanningLockCategory.REVEAL: "reveal_windows",
        PlanningLockCategory.PROGRESSION: "progression_locks",
        PlanningLockCategory.EQUIPMENT: "equipment_locks",
        PlanningLockCategory.LOCATION: "location_preconditions",
    }
    for lock in document.locks:
        entry: dict[str, object] = {
            "lock_id": lock.lock_id.root,
            "category": lock.category.value,
            "description": lock.description,
        }
        if lock.not_before_chapter is not None:
            entry["not_before_chapter"] = lock.not_before_chapter
        if lock.chapter_earliest is not None:
            entry["chapter_start"] = lock.chapter_earliest
        if lock.chapter_latest is not None:
            entry["chapter_end"] = lock.chapter_latest
        channels[channel_by_category[lock.category]].append(entry)
    return channels
