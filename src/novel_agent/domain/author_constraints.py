"""Traceable author constraints compiled once for every Planner invocation.

The raw brief stays immutable; this root is the compiled, source-addressed
projection the Planner may consume.  Required constraint sections are never
silently demoted to optional budget drops.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.stage2 import ProjectProfileRootDocument


class AuthorConstraintCategory(StrEnum):
    WORLD_HARD_FACT = "world_hard_fact"
    TIME_LOCK = "time_lock"
    ABILITY_MILESTONE = "ability_milestone"
    EQUIPMENT_MILESTONE = "equipment_milestone"
    LOCATION_PRECONDITION = "location_precondition"
    REVEAL_WINDOW = "reveal_window"
    LANGUAGE = "language"


class AuthorConstraint(DomainModel):
    constraint_id: StableId
    # The channel entry's own id (for example an author planning lock id).  A plan
    # stage binds to the responsibility it serves through this key, so the key must
    # travel with the constraint instead of only living in the compiled profile.
    constraint_key: str | None = Field(default=None, min_length=1)
    category: AuthorConstraintCategory
    text: str = Field(min_length=1)
    source_ref: ArtifactRef
    source_hash: ArtifactId
    source_span: str | None = None
    not_before_chapter: int | None = Field(default=None, ge=1)
    chapter_earliest: int | None = Field(default=None, ge=1)
    chapter_latest: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_window(self) -> AuthorConstraint:
        if (
            self.chapter_earliest is not None
            and self.chapter_latest is not None
            and self.chapter_latest < self.chapter_earliest
        ):
            raise ValueError("author constraint chapter window is reversed")
        return self


class AuthorConstraintRoot(DomainModel):
    root_hash: ArtifactId
    source_refs: tuple[ArtifactRef, ...] = ()
    constraints: tuple[AuthorConstraint, ...] = ()

    @model_validator(mode="after")
    def validate_constraints(self) -> AuthorConstraintRoot:
        if len({item.constraint_id for item in self.constraints}) != len(self.constraints):
            raise ValueError("author constraint ids must be unique")
        return self


def compile_author_constraint_root(
    *,
    profile: ProjectProfileRootDocument,
    profile_ref: ArtifactRef,
    source_refs: tuple[ArtifactRef, ...] = (),
) -> AuthorConstraintRoot:
    """Deterministically compile the structured profile into author constraints."""

    constraints: list[AuthorConstraint] = []
    profile_hash = profile.root_hash

    def add(
        category: AuthorConstraintCategory,
        text: str,
        *,
        not_before: int | None = None,
        earliest: int | None = None,
        latest: int | None = None,
        constraint_key: str | None = None,
    ) -> None:
        key = f"{category.value}.{len(constraints)}"
        constraints.append(
            AuthorConstraint(
                constraint_id=StableId(f"author-constraint.{key}"),
                constraint_key=constraint_key,
                category=category,
                text=text,
                source_ref=profile_ref,
                source_hash=profile_hash,
                not_before_chapter=not_before,
                chapter_earliest=earliest,
                chapter_latest=latest,
            )
        )

    style = profile.style_profile
    language = style.get("language")
    if isinstance(language, str) and language.strip():
        add(AuthorConstraintCategory.LANGUAGE, f"正文语言必须为 {language.strip()}")
    allowlist = style.get("language_allowlist")
    if isinstance(allowlist, list):
        tokens = [item.strip() for item in allowlist if isinstance(item, str) and item.strip()]
        if tokens:
            add(
                AuthorConstraintCategory.LANGUAGE,
                "允许保留的英文代号: " + ", ".join(tokens),
            )

    planning: dict[str, object] = dict(profile.capability_profile)
    raw_planning = profile.capability_profile.get("planning_constraints")
    if isinstance(raw_planning, dict):
        planning.update(raw_planning)

    category_by_key = {
        "timeline_locks": AuthorConstraintCategory.TIME_LOCK,
        "progression_locks": AuthorConstraintCategory.ABILITY_MILESTONE,
        "reveal_windows": AuthorConstraintCategory.REVEAL_WINDOW,
        "equipment_locks": AuthorConstraintCategory.EQUIPMENT_MILESTONE,
        "location_preconditions": AuthorConstraintCategory.LOCATION_PRECONDITION,
    }
    for key, category in category_by_key.items():
        raw_items = planning.get(key)
        if not isinstance(raw_items, list):
            continue
        for raw in raw_items:
            if isinstance(raw, dict):
                raw_key = raw.get("lock_id") or raw.get("id") or raw.get("constraint_id")
                constraint_key = (
                    str(raw_key).strip()
                    if isinstance(raw_key, str) and str(raw_key).strip()
                    else None
                )
                text = next(
                    (
                        str(raw[field]).strip()
                        for field in ("description", "reveal", "lock", "value", "summary")
                        if isinstance(raw.get(field), str) and str(raw[field]).strip()
                    ),
                    None,
                )
                not_before = next(
                    (
                        int(raw[field])
                        for field in ("not_before_chapter", "chapter_start", "start")
                        if type(raw.get(field)) is int and int(raw[field]) >= 1
                    ),
                    None,
                )
                latest = next(
                    (
                        int(raw[field])
                        for field in ("chapter_end", "end", "deadline_chapter")
                        if type(raw.get(field)) is int and int(raw[field]) >= 1
                    ),
                    None,
                )
            elif isinstance(raw, str) and raw.strip():
                text = raw.strip()
                not_before = None
                latest = None
            else:
                continue
            if text is None:
                continue
            add(
                category,
                f"{key}: {text}",
                not_before=not_before,
                latest=latest,
                constraint_key=constraint_key,
            )

    from novel_agent.services.content_addressing import content_id

    root_hash = content_id(
        {
            "profile": profile_hash.root,
            "constraints": tuple(item.model_dump(mode="json") for item in constraints),
        }
    )
    return AuthorConstraintRoot(
        root_hash=root_hash,
        source_refs=tuple(dict.fromkeys((*source_refs, profile_ref))),
        constraints=tuple(constraints),
    )


def render_author_constraint_context(
    root: AuthorConstraintRoot,
    *,
    chapter_start: int | None,
    chapter_end: int | None,
) -> str:
    """Render the required constraint sections for one Planner invocation."""

    del chapter_start, chapter_end
    if not root.constraints:
        return "AUTHOR_CONSTRAINTS: none"
    lines = ["AUTHOR_CONSTRAINTS (required; never treat unknown values as facts):"]
    for constraint in root.constraints:
        scope = ""
        if constraint.not_before_chapter is not None:
            scope = f" [not_before_chapter={constraint.not_before_chapter}]"
        if constraint.chapter_latest is not None:
            scope += f" [latest_chapter={constraint.chapter_latest}]"
        handle = constraint.constraint_key or constraint.constraint_id.root
        lines.append(f"- [{handle}] {constraint.category.value}: {constraint.text}{scope}")
    return "\n".join(lines)
