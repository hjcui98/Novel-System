from __future__ import annotations

from typing import Any

import pytest

from novel_agent.domain.benchmark import GoldItem, GoldKind, PreludeDocument
from novel_agent.domain.changes import (
    CuratedOperationDraft,
    CuratedOperationDraftV2,
    CuratorEntityRecord,
    CuratorEventRecord,
    CuratorStoryTime,
    EvidenceCandidate,
    WorldRecordKind,
)
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory import RetrievalUnit, Stage1MemoryNeed
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


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


def test_evidence_candidate_rejects_start_not_less_than_end() -> None:
    candidate = _construct(
        EvidenceCandidate,
        candidate_id=StableId("evidence-candidate.bad"),
        block_id=StableId("block.1"),
        chapter_index=1,
        scene_index=0,
        text="evidence text",
        start=5,
        end=5,
        content_hash=ArtifactId("sha256:" + "e" * 64),
    )
    _reject(candidate, "validate_span", "start < end")


def test_curated_operation_draft_v2_rejects_mismatched_record_kind() -> None:
    operation = _construct(
        CuratedOperationDraftV2,
        record_kind=WorldRecordKind.EVENT,
        record=_construct(
            CuratorEntityRecord,
            entity_type="person",
            internal_label="hero",
        ),
    )
    _reject(operation, "validate_record_kind", "does not match")


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


def test_memory_need_enforces_layered_plan_policies() -> None:
    base = dict(
        chapter_target=1,
        horizon_target=None,
        predicates=(),
        entity_ids=(),
        hierarchy_parent_unit_ids=(),
        need_facets=(),
        completion_spec=None,
    )

    legacy_mismatch = _construct(
        Stage1MemoryNeed,
        **base,
        legacy_allow_plan=True,
        retrieval_may_return_plan=False,
    )
    _reject(legacy_mismatch, "validate_target", "legacy allow_plan")

    deprecated_mismatch = _construct(
        Stage1MemoryNeed,
        **base,
        allow_plan=True,
        retrieval_may_return_plan=False,
    )
    _reject(deprecated_mismatch, "validate_target", "deprecated allow_plan")

    retrieval_without_planner = _construct(
        Stage1MemoryNeed,
        **base,
        planner_may_read_plan=False,
        retrieval_may_return_plan=True,
        claim_may_cite_plan=True,
        legacy_allow_plan=True,
        allow_plan=True,
    )
    _reject(retrieval_without_planner, "validate_target", "requires planner plan access")

    consistent = _construct(
        Stage1MemoryNeed,
        **base,
        planner_may_read_plan=True,
        retrieval_may_return_plan=True,
        claim_may_cite_plan=True,
        legacy_allow_plan=True,
        allow_plan=True,
    )
    assert consistent.validate_target() is consistent

    blind = _construct(
        Stage1MemoryNeed,
        **base,
        planner_may_read_plan=False,
        retrieval_may_return_plan=False,
        claim_may_cite_plan=False,
        legacy_allow_plan=False,
        allow_plan=False,
    )
    assert blind.validate_target() is blind

    incomplete_lineage = _construct(
        Stage1MemoryNeed,
        **base,
        semantic_question="问题?",
    )
    _reject(incomplete_lineage, "validate_target", "complete planner lineage")

    partial_lineage = _construct(
        Stage1MemoryNeed,
        **base,
        semantic_question="问题?",
        planner_artifact_ref=ArtifactId("sha256:" + "d" * 64),
    )
    _reject(partial_lineage, "validate_target", "complete planner lineage")

    missing_goal = _construct(
        Stage1MemoryNeed,
        **base,
        semantic_question="问题?",
        planner_artifact_ref=ArtifactId("sha256:" + "d" * 64),
        planned_draft_id="draft",
        validated_need_set_hash=ArtifactId("sha256:" + "e" * 64),
        trigger_plan_chapters=(21,),
        trigger_plan_goal="",
    )
    _reject(missing_goal, "validate_target", "canonical goal text")


def test_benchmark_case_planning_context_ref_hash_pair_is_atomic() -> None:
    case = (
        make_synthetic_bundle()
        .case_manifests[0]
        .model_copy(update={"planning_context_ref": ArtifactId("sha256:" + "d" * 64)})
    )
    _reject(case, "validate_case_ranges_and_gold", "must appear as a pair")
