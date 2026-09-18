"""Versioned hierarchical plan payloads and their pure structural checks.

The Planner used to express a rolling window as N flat chapter-goal items with
no content of their own, so a chapter set could not say what the window as a
whole was about and a chapter could not say what happens inside it.  This
module owns the two V2 payloads that fix that, plus the reference shapes they
need.

Every check here is pure: no IO, no model call, no second store.  The host
supplies trusted values (the rolling horizon, the parent node identity, the
accepted obligation catalogue) and this module refuses anything that does not
follow from them.  Keeping it a leaf module avoids growing
``domain/planning.py`` and ``domain/stage2.py`` further without introducing a
second protocol stack.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, field_validator, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.services.content_addressing import content_id

CHAPTER_SET_CONTRACT_VERSION = "chapter-set.v2"
CHAPTER_CONTRACT_VERSION = "chapter.v2"

#: Detail levels a CHAPTER payload may declare.  This is a shape declaration
#: only; whether a chapter is actually writable is recomputed by the host from
#: the payload and the accepted parent, never read from a model's own flag.
DETAIL_LEVELS = ("outline", "execution")

#: Contract versions that only this module may interpret.  A payload carrying
#: one of these must satisfy the matching V2 validator; it is never silently
#: interpreted as V1.
V2_CONTRACT_VERSIONS = frozenset({CHAPTER_SET_CONTRACT_VERSION, CHAPTER_CONTRACT_VERSION})

NonEmptyText = Annotated[str, Field(min_length=1)]

#: Plan payloads travel through JSON artifacts, where a tuple is stored as a
#: list.  These models therefore read either shape and normalize to a tuple,
#: instead of rejecting a round-tripped payload the host just wrote.
_PayloadModel = ConfigDict(strict=False, extra="ignore")


def _as_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class ParentPlanBinding(DomainModel):
    """Host-filled binding from a child plan node to its parent's content.

    The host writes this after the parent node has a deterministic content
    hash.  The parent does not embed the child hash, so there is no
    parent-contains-child cycle.  Applicability is checked against the
    *current parent node* content hash, not against a whole-root hash that
    every later chapter refinement would change.
    """

    model_config = _PayloadModel

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_sequences(cls, value: object) -> object:
        return _as_tuple(value)

    parent_node_id: StableId
    parent_content_hash: ArtifactId
    parent_plan_level: str = Field(min_length=1)
    bound_at_commit: ArtifactId | None = None


class PlanParticipant(DomainModel):
    """One planned or already-known participant of a chapter set.

    A planned participant is not a Canon entity.  It carries a plan-local
    reference so the Writer can write about something that does not exist in
    the World yet, while the World stays free of a pre-written membership
    relation.  A known participant references a real entity id that the
    current World must be able to resolve.
    """

    model_config = _PayloadModel

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_sequences(cls, value: object) -> object:
        return _as_tuple(value)

    reference_kind: Literal["planned", "canon"]
    narrative_role: NonEmptyText
    # ``planned`` participants carry a plan-local reference and label.
    planned_ref: StableId | None = None
    label: NonEmptyText | None = None
    introduction_ref: StableId | None = None
    # ``canon`` participants carry a real entity identity.
    entity_id: StableId | None = None
    enter_chapter: int | None = Field(default=None, ge=1)
    exit_chapter: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_reference_shape(self) -> PlanParticipant:
        if self.reference_kind == "planned":
            missing = [
                name
                for name, value in (
                    ("planned_ref", self.planned_ref),
                    ("label", self.label),
                    ("introduction_ref", self.introduction_ref),
                )
                if value is None
            ]
            if missing:
                raise ValueError("a planned participant requires " + ", ".join(sorted(missing)))
            if self.entity_id is not None:
                raise ValueError("a planned participant must not claim a Canon entity identity")
        else:
            if self.entity_id is None:
                raise ValueError("a canon participant requires an entity_id")
            if self.planned_ref is not None or self.label is not None:
                raise ValueError("a canon participant must not carry a plan-local reference")
        if (
            self.enter_chapter is not None
            and self.exit_chapter is not None
            and self.exit_chapter < self.enter_chapter
        ):
            raise ValueError("participant exit precedes its entry")
        return self


class PlannedIntroduction(DomainModel):
    """One thing the window establishes that does not exist yet.

    A planned introduction is a plan responsibility, not a world fact.  It
    records which chapter performs the introduction and which later chapter
    depends on it, so a reader can check the dependency is discharged in the
    right order instead of assuming it already happened.
    """

    model_config = _PayloadModel

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_sequences(cls, value: object) -> object:
        return _as_tuple(value)

    introduction_id: StableId
    statement: NonEmptyText
    introduced_in_chapter: int = Field(ge=1)
    depends_from_chapters: tuple[int, ...] = ()
    created_by_item_ref: StableId | None = None

    @model_validator(mode="after")
    def validate_dependency_order(self) -> PlannedIntroduction:
        if self.introduced_in_chapter in self.depends_from_chapters:
            raise ValueError(
                "a planned introduction cannot depend on the chapter that introduces it"
            )
        return self


class PlotTurn(DomainModel):
    """One ordered key change inside a chapter set."""

    model_config = _PayloadModel

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_sequences(cls, value: object) -> object:
        return _as_tuple(value)

    turn_id: StableId
    summary: NonEmptyText
    responsibility: NonEmptyText
    chapter_index: int | None = Field(default=None, ge=1)


class ResponsibilityAction(StrEnum):
    SETUP = "SETUP"
    PROGRESS = "PROGRESS"
    PAYOFF = "PAYOFF"
    DEFER = "DEFER"


class ResponsibilityAssignment(DomainModel):
    """Which chapter carries which part of a parent obligation."""

    model_config = _PayloadModel

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_sequences(cls, value: object) -> object:
        return _as_tuple(value)

    obligation_id: StableId
    action: ResponsibilityAction
    chapter_indexes: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_chapters(self) -> ResponsibilityAssignment:
        if any(index < 1 for index in self.chapter_indexes):
            raise ValueError("responsibility chapters must be positive")
        if len(set(self.chapter_indexes)) != len(self.chapter_indexes):
            raise ValueError("responsibility chapters must be unique")
        return self


class ElementAction(DomainModel):
    """How a prop, clue, ability, location, or relation enters the plot."""

    model_config = _PayloadModel

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_sequences(cls, value: object) -> object:
        return _as_tuple(value)

    element_ref: NonEmptyText
    action: NonEmptyText
    chapter_index: int | None = Field(default=None, ge=1)
    obligation_id: StableId | None = None


class ChapterAssignment(DomainModel):
    """One chapter's job inside the window, referenced from the parent."""

    model_config = _PayloadModel

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_sequences(cls, value: object) -> object:
        return _as_tuple(value)

    chapter_index: int = Field(ge=1)
    chapter_node_id: StableId
    narrative_task: NonEmptyText
    turn_refs: tuple[StableId, ...] = ()
    expected_change: NonEmptyText
    next_chapter_interface: NonEmptyText


class ChapterSetPayloadV2(DomainModel):
    """The semantic content of one rolling chapter-set window."""

    model_config = _PayloadModel

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_sequences(cls, value: object) -> object:
        return _as_tuple(value)

    contract_version: Literal["chapter-set.v2"] = "chapter-set.v2"
    chapter_start: int = Field(ge=1)
    chapter_end: int = Field(ge=1)
    summary: NonEmptyText
    dramatic_question: NonEmptyText
    entry_requirements: tuple[NonEmptyText, ...] = ()
    plot_turns: tuple[PlotTurn, ...] = Field(min_length=1)
    cast: tuple[PlanParticipant, ...] = ()
    element_actions: tuple[ElementAction, ...] = ()
    responsibility_assignments: tuple[ResponsibilityAssignment, ...] = ()
    planned_introductions: tuple[PlannedIntroduction, ...] = ()
    chapter_assignments: tuple[ChapterAssignment, ...] = Field(min_length=1)
    exit_targets: tuple[NonEmptyText, ...] = Field(min_length=1)
    hold_for_later: tuple[NonEmptyText, ...] = ()
    roadmap_slot_refs: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def validate_window(self) -> ChapterSetPayloadV2:
        if self.chapter_end < self.chapter_start:
            raise ValueError("chapter set window end precedes its start")
        if len(self.summary.strip()) < 40:
            raise ValueError(
                "a chapter-set summary must state the window's course, not a placeholder"
            )
        turn_ids = [turn.turn_id for turn in self.plot_turns]
        if len(set(turn_ids)) != len(turn_ids):
            raise ValueError("chapter-set plot turn ids must be unique")
        introduction_ids = [item.introduction_id for item in self.planned_introductions]
        if len(set(introduction_ids)) != len(introduction_ids):
            raise ValueError("planned introduction ids must be unique")
        assigned = [item.chapter_index for item in self.chapter_assignments]
        if len(set(assigned)) != len(assigned):
            raise ValueError("a chapter set assigns each chapter at most once")
        for assignment in self.chapter_assignments:
            unknown = tuple(ref for ref in assignment.turn_refs if ref not in set(turn_ids))
            if unknown:
                raise ValueError(
                    "chapter assignment references an unknown plot turn: "
                    + ", ".join(item.root for item in unknown)
                )
        for introduction in self.planned_introductions:
            if not (self.chapter_start <= introduction.introduced_in_chapter <= self.chapter_end):
                raise ValueError("a planned introduction must be introduced inside its window")
        for responsibility in self.responsibility_assignments:
            outside = tuple(
                index
                for index in responsibility.chapter_indexes
                if not self.chapter_start <= index <= self.chapter_end
            )
            if outside:
                raise ValueError("responsibility assignment names a chapter outside the window")
        return self

    def chapter_indexes(self) -> tuple[int, ...]:
        return tuple(item.chapter_index for item in self.chapter_assignments)


class SceneBlueprint(DomainModel):
    """One scene of a chapter's execution detail."""

    model_config = _PayloadModel

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_sequences(cls, value: object) -> object:
        return _as_tuple(value)

    scene_id: StableId
    narrative_task: NonEmptyText
    location: NonEmptyText
    pov: NonEmptyText
    participants: tuple[NonEmptyText, ...] = ()
    entry_condition: NonEmptyText
    exit_condition: NonEmptyText
    beats: tuple[ExecutionBeatBlueprint, ...] = Field(min_length=1)
    budget_characters: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_beats(self) -> SceneBlueprint:
        beat_ids = [beat.beat_id for beat in self.beats]
        if len(set(beat_ids)) != len(beat_ids):
            raise ValueError("execution beat ids must be unique within a scene")
        return self


class ExecutionBeatBlueprint(DomainModel):
    """One few-hundred-character unit of writing work.

    A beat is the smallest unit the Writer contract carries: it says what
    happens, what opposes it, what the character chooses, what results, and how
    the prose should end.  A budget without those is a length request, not a
    plan.
    """

    model_config = _PayloadModel

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_sequences(cls, value: object) -> object:
        return _as_tuple(value)

    beat_id: StableId
    parent_beat_refs: tuple[StableId, ...] = Field(min_length=1)
    action: NonEmptyText
    resistance: NonEmptyText
    choice: NonEmptyText
    outcome: NonEmptyText
    information_revealed: NonEmptyText
    prose_focus: NonEmptyText
    budget_characters: int = Field(ge=1)
    close_point: NonEmptyText

    @model_validator(mode="after")
    def validate_budget(self) -> ExecutionBeatBlueprint:
        if self.budget_characters < 50:
            raise ValueError("an execution beat budget must be a real writing slice")
        return self


class ChapterPayloadV2(DomainModel):
    """One chapter's outline or accepted execution detail."""

    model_config = _PayloadModel

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_sequences(cls, value: object) -> object:
        return _as_tuple(value)

    contract_version: Literal["chapter.v2"] = "chapter.v2"
    chapter_index: int = Field(ge=1)
    detail_level: Literal["outline", "execution"]
    summary: NonEmptyText
    narrative_function: NonEmptyText
    history_retrieval: dict[str, object] | None = None
    obligation_actions: tuple[str, ...] = ()
    parent_set_binding: ParentPlanBinding | None = None
    parent_turn_refs: tuple[StableId, ...] = ()
    beats: tuple[NonEmptyText, ...] = ()
    cast: tuple[PlanParticipant, ...] = ()
    pov: NonEmptyText | None = None
    entry_state_dependencies: tuple[NonEmptyText, ...] = ()
    expected_exit_change: NonEmptyText | None = None
    next_chapter_interface: NonEmptyText | None = None
    planned_introduction_refs: tuple[StableId, ...] = ()
    # The introductions this chapter itself performs.  The parent chapter set
    # owns the window-level list; a chapter that performs one repeats it here so
    # the Writer receives the responsibility, not just a reference.
    planned_introductions: tuple[PlannedIntroduction, ...] = ()
    plan_dependencies: tuple[StableId, ...] = ()
    scenes: tuple[SceneBlueprint, ...] = ()
    budget_characters: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_detail_level(self) -> ChapterPayloadV2:
        if self.detail_level == "execution":
            if not self.scenes:
                raise ValueError("an execution chapter requires ordered scenes")
            missing_interfaces = [
                name
                for name, value in (
                    ("expected_exit_change", self.expected_exit_change),
                    ("next_chapter_interface", self.next_chapter_interface),
                )
                if value is None
            ]
            if missing_interfaces:
                raise ValueError("an execution chapter requires " + ", ".join(missing_interfaces))
        elif self.scenes:
            raise ValueError("an outline chapter must not claim execution scenes")
        scene_ids = [scene.scene_id for scene in self.scenes]
        if len(set(scene_ids)) != len(scene_ids):
            raise ValueError("chapter scene ids must be unique")
        if self.chapter_index in self.plan_dependencies:
            raise ValueError("a chapter cannot depend on itself")
        declared_here = {item.introduction_id for item in self.planned_introductions}
        unknown = tuple(
            ref.root for ref in self.planned_introduction_refs if ref not in declared_here
        )
        if unknown:
            raise ValueError(
                "a chapter must state the introductions it performs: " + ", ".join(unknown)
            )
        return self

    def required_beat_ids(self) -> tuple[StableId, ...]:
        """Return every beat every scene of this chapter must cover."""

        return tuple(beat.beat_id for scene in self.scenes for beat in scene.beats)


def require_exact_chapter_coverage(*, start: int, end: int, actual: Sequence[int]) -> None:
    """Require the chapter indexes to be exactly the trusted horizon, once each."""

    for name, value in (("start", start), ("end", end)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer, not {type(value).__name__}")
    if start < 1 or end < start:
        raise ValueError("invalid trusted horizon")
    for index in actual:
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("chapter indexes must be integers")
    expected = list(range(start, end + 1))
    if sorted(actual) != expected:
        raise ValueError("chapter indexes must cover the horizon exactly once")


def validate_chapter_set_window(
    payload: ChapterSetPayloadV2, *, horizon_start: int, horizon_end: int
) -> None:
    """Require the window to be the task's trusted horizon and fully assigned."""

    if (payload.chapter_start, payload.chapter_end) != (horizon_start, horizon_end):
        raise ValueError("a chapter-set payload must use the task's exact rolling horizon")
    require_exact_chapter_coverage(
        start=horizon_start,
        end=horizon_end,
        actual=payload.chapter_indexes(),
    )


def validate_execution_allocation(
    *,
    accepted_beat_ids: set[str],
    required_beat_ids: set[str],
    executed_refs: Sequence[str],
    expected_characters: Sequence[int],
    declared_total: int,
    minimum_characters: int,
    maximum_characters: int,
) -> None:
    """Require one work plan to cover every required beat inside the length policy."""

    if minimum_characters < 1 or maximum_characters < minimum_characters:
        raise ValueError("invalid trusted length policy")
    if not required_beat_ids.issubset(accepted_beat_ids):
        raise ValueError("required beats must come from the accepted blueprint")
    if not executed_refs or len(executed_refs) != len(expected_characters):
        raise ValueError("execution references and budgets must align")
    if any(ref not in accepted_beat_ids for ref in executed_refs):
        raise ValueError("unknown accepted execution beat reference")
    if not required_beat_ids.issubset(set(executed_refs)):
        raise ValueError("a required execution beat is uncovered")
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count <= 0
        for count in expected_characters
    ):
        raise ValueError("execution budgets must be positive integers")
    if sum(expected_characters) != declared_total:
        raise ValueError("declared work-plan total does not match its beat allocation")
    if not minimum_characters <= declared_total <= maximum_characters:
        raise ValueError("execution allocation is incompatible with the trusted length policy")


def require_known_facet_references(
    payload: ChapterSetPayloadV2,
    *,
    trusted_obligation_ids: Iterable[StableId],
) -> None:
    """Refuse an obligation reference the accepted plan never declared.

    A chapter set may only commit to long-range responsibilities that already
    exist.  Inventing a new obligation id here would silently create a promise
    no author or accepted plan ever made.
    """

    trusted = set(trusted_obligation_ids)
    referenced = {assignment.obligation_id for assignment in payload.responsibility_assignments} | {
        action.obligation_id
        for action in payload.element_actions
        if action.obligation_id is not None
    }
    unknown = tuple(sorted(item.root for item in referenced - trusted))
    if unknown:
        raise ValueError(
            "chapter set references an obligation no accepted plan declared: " + ", ".join(unknown)
        )


@runtime_checkable
class _Dumpable(Protocol):
    def model_dump(self, *, mode: str) -> dict[str, object]: ...


def plan_node_content_id(node: _Dumpable) -> ArtifactId:
    """Return a plan node's content identity, independent of its parent/children.

    A parent binds a child to *this* hash rather than to a whole-root hash, so
    refining an unrelated chapter cannot invalidate a still-correct child and
    no parent-contains-child hash cycle can form.
    """

    if not isinstance(node, _Dumpable):
        raise ValueError("plan node content identity requires a plan node")
    payload = dict(node.model_dump(mode="json"))
    # These keys are host bindings *from* the node to its own parent and must
    # never affect the node's own identity.
    payload.pop("parent_content_hash", None)
    payload.pop("parent_set_binding", None)
    return content_id(payload)


def payload_contract_version(payload: Mapping[str, object]) -> str | None:
    """Return the declared contract version, refusing a non-string value."""

    raw = payload.get("contract_version")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("plan payload contract_version must be a non-empty string")
    return raw


__all__ = [
    "CHAPTER_CONTRACT_VERSION",
    "CHAPTER_SET_CONTRACT_VERSION",
    "DETAIL_LEVELS",
    "V2_CONTRACT_VERSIONS",
    "ChapterAssignment",
    "ChapterPayloadV2",
    "ChapterSetPayloadV2",
    "ElementAction",
    "ExecutionBeatBlueprint",
    "ParentPlanBinding",
    "PlanParticipant",
    "PlannedIntroduction",
    "PlotTurn",
    "ResponsibilityAction",
    "ResponsibilityAssignment",
    "SceneBlueprint",
    "payload_contract_version",
    "plan_node_content_id",
    "require_exact_chapter_coverage",
    "require_known_facet_references",
    "validate_chapter_set_window",
    "validate_execution_allocation",
]
