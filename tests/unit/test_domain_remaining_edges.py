from __future__ import annotations

from typing import Any

import pytest

from novel_agent.domain.benchmark import GoldItem, GoldKind, PreludeDocument
from novel_agent.domain.changes import (
    CuratedOperationDraft,
    CuratorEntityRecord,
    CuratorEventRecord,
    CuratorStoryTime,
    WorldRecordKind,
)
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import RetrievalUnit, Stage1MemoryNeed


def _construct(model: Any, **values: Any) -> Any:
    return model.model_construct(**values)


def _reject(model: Any, validator: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        getattr(model, validator)()


def test_prelude_rejects_foreign_blocks_and_duplicate_scene_order() -> None:
    foreign = _construct(
        PreludeDocument,
        prelude_id=StableId("prelude.one"),
        scenes=(),
    )
    foreign = foreign.model_copy(
        update={
            "scenes": (
                type(
                    "Scene",
                    (),
                    {
                        "scene_index": 1,
                        "blocks": (
                            type(
                                "Block",
                                (),
                                {"chapter_id": StableId("prelude.other")},
                            )(),
                        ),
                    },
                )(),
            )
        }
    )
    _reject(foreign, "validate_scenes", "containing prelude")

    duplicate = foreign.model_copy(
        update={
            "scenes": (
                type("Scene", (), {"scene_index": 1, "blocks": ()})(),
                type("Scene", (), {"scene_index": 1, "blocks": ()})(),
            )
        }
    )
    _reject(duplicate, "validate_scenes", "unique narrative order")


@pytest.mark.parametrize(
    ("kind", "evidence", "plan_evidence", "message"),
    (
        (GoldKind.PLAN_OBLIGATION, (), (), "requires plan or historical"),
        (GoldKind.OBSERVED_USE, (), (), "require historical"),
        (GoldKind.OBSERVED_USE, (object(),), (object(),), "only plan obligation"),
    ),
)
def test_gold_item_rejects_invalid_evidence_kinds(
    kind: GoldKind,
    evidence: tuple[object, ...],
    plan_evidence: tuple[object, ...],
    message: str,
) -> None:
    item = _construct(
        GoldItem,
        kind=kind,
        evidence_refs=evidence,
        plan_evidence_refs=plan_evidence,
    )
    _reject(item, "validate_evidence_kind", message)


def test_curator_story_time_and_typed_record_are_consistent() -> None:
    story_time = _construct(CuratorStoryTime, start_ordinal=2, end_ordinal=1)
    _reject(story_time, "validate_order", "end precedes start")

    operation = _construct(
        CuratedOperationDraft,
        record_kind=WorldRecordKind.EVENT,
        record=_construct(
            CuratorEntityRecord,
            entity_type="person",
            internal_label="hero",
        ),
    )
    _reject(operation, "validate_record_kind", "does not match")
    valid_operation = operation.model_copy(
        update={
            "record": _construct(
                CuratorEventRecord,
                event_type="arrival",
            )
        }
    )
    assert valid_operation.validate_record_kind() is valid_operation


def test_memory_need_and_retrieval_unit_reject_duplicate_lineage() -> None:
    need = _construct(
        Stage1MemoryNeed,
        chapter_target=1,
        horizon_target=None,
        hierarchy_parent_unit_ids=(StableId("unit.parent"), StableId("unit.parent")),
    )
    _reject(need, "validate_target", "parents must be unique")

    unit = _construct(
        RetrievalUnit,
        narrative_start=None,
        narrative_end=None,
        story_time_start=None,
        story_time_end=None,
        parent_unit_ids=(),
        source_refs=(StableId("source.one"), StableId("source.one")),
    )
    _reject(unit, "validate_projection_metadata", "source refs must be unique")
