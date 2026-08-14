from __future__ import annotations

import asyncio

import pytest

from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.domain.changes import (
    ChangeOperationType,
    ChapterChangeDraft,
    ChapterChangeDraftV2,
    CuratedOperationDraft,
    CuratedOperationDraftV2,
    CuratorEntityRecord,
    CuratorEventRecord,
    CuratorEvidenceSelection,
    CuratorObligationRecord,
    CuratorRelationRecord,
    CuratorStateRecord,
    CuratorStoryTime,
    WorldRecordKind,
)
from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.text import EvidenceSupportStatus
from novel_agent.domain.world import TruthClass
from novel_agent.services.model_curation import (
    CuratorProposalSemanticRejected,
    ModelCurationContractError,
    ModelCurator,
)
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def _request() -> ModelRequest:
    return ModelRequest(
        request_id=StableId("request.model-curator"),
        run_id=RunId("run.model-curator"),
        task_id=TaskId("task.model-curator"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace-model-curator",
        prompt="must be replaced",
    )


def _gateway(draft: ChapterChangeDraft) -> tuple[ModelGateway, FakeModelEndpoint]:
    endpoint = FakeModelEndpoint(draft.model_dump_json())
    return (
        ModelGateway(
            (
                RegisteredModelEndpoint(
                    role=ModelRole.BATCH_TEST,
                    endpoint_name="batch-curator",
                    model_name="fake-curator",
                    adapter=endpoint,
                ),
            )
        ),
        endpoint,
    )


def _draft(chapter_index: int = 23) -> ChapterChangeDraft:
    bundle = make_synthetic_bundle()
    gold_evidence = bundle.replay_manifests[0].gold_changes[2].evidence_refs[0]
    assert gold_evidence.span is not None
    evidence = CuratorEvidenceSelection.model_validate(gold_evidence.span.model_dump())
    return ChapterChangeDraft(
        chapter_index=chapter_index,
        operations=(
            CuratedOperationDraft(
                operation=ChangeOperationType.REPLACE,
                record_kind=WorldRecordKind.OBLIGATION,
                target_id=StableId("obligation.synthetic.north-tower"),
                record=CuratorObligationRecord(
                    kind="objective",
                    description="林澈需要进入北塔。",
                    status="resolved",
                    owner_ids=(StableId("entity.synthetic.lin-che"),),
                    due_chapter=23,
                ),
                evidence_refs=(evidence,),
            ),
        ),
    )


def test_model_curator_binds_audited_draft_to_deterministic_changes() -> None:
    bundle = make_synthetic_bundle()
    future = bundle.text_roots[1]
    world = bundle.world_roots[0]
    gateway, endpoint = _gateway(_draft())

    changes, call = asyncio.run(
        ModelCurator(gateway).extract(future, 23, world.source_commit, world, _request())
    )

    assert call.model_role is ModelRole.BATCH_TEST
    assert changes.base_commit == world.source_commit
    assert changes.source_artifact.media_type.endswith("chapter+json")
    assert len(changes.operations) == 1
    operation = changes.operations[0]
    assert operation.operation_id.root.startswith("change.model.")
    assert operation.evidence_refs[0].span is not None
    assert operation.evidence_refs[0].span.model_dump() == (
        _draft().operations[0].evidence_refs[0].model_dump()
    )
    assert isinstance(operation.payload, dict)
    record = operation.payload["record"]
    assert isinstance(record, dict)
    assert record["evidence_refs"] == [operation.evidence_refs[0].model_dump(mode="json")]
    prompt = endpoint.requests[0].prompt
    assert "CHAPTER=" in prompt and "终于进入北塔" in prompt
    assert "重申旧誓言" not in prompt and "受伤仍未痊愈" not in prompt

    second, _ = asyncio.run(
        ModelCurator(_gateway(_draft())[0]).extract(
            future, 23, world.source_commit, world, _request()
        )
    )
    assert second == changes


def test_model_curator_preserves_entity_shape_without_record_evidence_field() -> None:
    bundle = make_synthetic_bundle()
    future = bundle.text_roots[1]
    world = bundle.world_roots[0]
    evidence = _draft().operations[0].evidence_refs[0]
    draft = ChapterChangeDraft(
        chapter_index=23,
        operations=(
            CuratedOperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.ENTITY,
                target_id=StableId("entity.synthetic.new"),
                record=CuratorEntityRecord(
                    entity_type="character",
                    internal_label="新角色",
                ),
                evidence_refs=(evidence,),
            ),
        ),
    )
    changes, _ = asyncio.run(
        ModelCurator(_gateway(draft)[0]).extract(future, 23, world.source_commit, world, _request())
    )
    payload = changes.operations[0].payload
    assert isinstance(payload, dict) and isinstance(payload["record"], dict)
    assert "evidence_refs" not in payload["record"]


def test_model_curator_canonically_binds_model_evidence_metadata() -> None:
    bundle = make_synthetic_bundle()
    future = bundle.text_roots[1]
    world = bundle.world_roots[0]
    draft = _draft()
    gold = bundle.replay_manifests[0].gold_changes[2].evidence_refs[0]

    changes, _ = asyncio.run(
        ModelCurator(_gateway(draft)[0]).extract(future, 23, world.source_commit, world, _request())
    )

    rebound = changes.operations[0].evidence_refs[0]
    assert rebound.chapter_id == gold.chapter_id
    assert rebound.scene_id == gold.scene_id
    assert rebound.root_hash == future.root_hash
    assert rebound.object_hash == gold.object_hash
    assert rebound.quote_hash == gold.quote_hash


def test_model_curator_filters_dangling_entity_references() -> None:
    bundle = make_synthetic_bundle()
    future = bundle.text_roots[1]
    world = bundle.world_roots[0]
    selection = _draft().operations[0].evidence_refs[0]
    draft = ChapterChangeDraft(
        chapter_index=23,
        operations=(
            CuratedOperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.synthetic.dangling"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.synthetic.unknown"),
                    predicate="location",
                    value="north tower",
                    valid_time=CuratorStoryTime(worldline="main", start_ordinal=23),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
                evidence_refs=(selection,),
            ),
        ),
    )

    changes, _, reported = asyncio.run(
        ModelCurator(_gateway(draft)[0]).extract_reported(
            future, 23, world.source_commit, world, _request()
        )
    )

    assert changes.operations == ()
    assert reported.coverage == 0
    assert "runtime filtered" in reported.unresolved[-1]


def test_model_curator_normalizes_replace_for_new_relation_to_create() -> None:
    bundle = make_synthetic_bundle()
    future = bundle.text_roots[1]
    world = bundle.world_roots[0]
    selection = _draft().operations[0].evidence_refs[0]
    draft = ChapterChangeDraft(
        chapter_index=23,
        operations=(
            CuratedOperationDraft(
                operation=ChangeOperationType.REPLACE,
                record_kind=WorldRecordKind.RELATION,
                target_id=StableId("relation.synthetic.new"),
                record=CuratorRelationRecord(
                    subject_id=world.entities[0].entity_id,
                    predicate="trusts",
                    object_id=world.entities[0].entity_id,
                    valid_time=CuratorStoryTime(worldline="main", start_ordinal=23),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
                evidence_refs=(selection,),
            ),
        ),
    )

    changes, _ = asyncio.run(
        ModelCurator(_gateway(draft)[0]).extract(future, 23, world.source_commit, world, _request())
    )

    assert changes.operations[0].operation is ChangeOperationType.CREATE


def test_model_curator_routes_retirement_out_of_chapter_replay() -> None:
    bundle = make_synthetic_bundle()
    future = bundle.text_roots[1]
    world = bundle.world_roots[0]
    operation = _draft().operations[0].model_copy(update={"operation": ChangeOperationType.RETIRE})
    draft = _draft().model_copy(update={"operations": (operation,)})

    changes, _, reported = asyncio.run(
        ModelCurator(_gateway(draft)[0]).extract_reported(
            future, 23, world.source_commit, world, _request()
        )
    )

    assert changes.operations == ()
    assert reported.coverage == 0
    assert operation.target_id.root in reported.unresolved[-1]


def test_model_curator_rejects_chapter_error_and_merges_equivalent_duplicate_target() -> None:
    bundle = make_synthetic_bundle()
    future = bundle.text_roots[1]
    world = bundle.world_roots[0]
    gateway, _ = _gateway(_draft())
    with pytest.raises(LookupError, match="chapter does not exist"):
        asyncio.run(
            ModelCurator(gateway).extract(future, 99, world.source_commit, world, _request())
        )

    wrong_gateway, _ = _gateway(_draft(22))
    with pytest.raises(ModelCurationContractError, match="draft chapter"):
        asyncio.run(
            ModelCurator(wrong_gateway).extract(future, 23, world.source_commit, world, _request())
        )
    duplicate = _draft().model_copy(
        update={"operations": (_draft().operations[0], _draft().operations[0])}
    )
    changes, _ = asyncio.run(
        ModelCurator(_gateway(duplicate)[0]).extract(
            future, 23, world.source_commit, world, _request()
        )
    )
    assert len(changes.operations) == 1


def test_model_curator_rejects_invalid_evidence_scope_and_binds_basis_and_status() -> None:
    bundle = make_synthetic_bundle()
    future = bundle.text_roots[1]
    world = bundle.world_roots[0]
    operation = _draft().operations[0]

    outside_evidence = bundle.replay_manifests[0].gold_changes[1].evidence_refs[0]
    assert outside_evidence.span is not None
    outside = CuratorEvidenceSelection.model_validate(outside_evidence.span.model_dump())
    outside_draft = _draft().model_copy(
        update={"operations": (operation.model_copy(update={"evidence_refs": (outside,)}),)}
    )
    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_INFORMATION_BOUNDARY",
    ):
        asyncio.run(
            ModelCurator(_gateway(outside_draft)[0]).extract_reported(
                future, 23, world.source_commit, world, _request()
            )
        )

    changes, _ = asyncio.run(
        ModelCurator(_gateway(_draft())[0]).extract(
            future, 23, world.source_commit, world, _request()
        )
    )
    evidence = changes.operations[0].evidence_refs[0]
    assert evidence.resolved_at_commit == world.source_commit
    assert evidence.support_status is EvidenceSupportStatus.CURRENT


def test_model_curator_rejects_invalid_evidence_coordinates() -> None:
    bundle = make_synthetic_bundle()
    future = bundle.text_roots[1]
    world = bundle.world_roots[0]
    operation = _draft().operations[0]
    selection = operation.evidence_refs[0]
    chapter = next(chapter for chapter in future.chapters if chapter.chapter_index == 23)
    block = next(
        block
        for scene in chapter.scenes
        for block in scene.blocks
        if block.block_id == selection.block_id
    )
    overflow = selection.model_copy(update={"end": len(block.text) + 100})
    draft = _draft().model_copy(
        update={"operations": (operation.model_copy(update={"evidence_refs": (overflow,)}),)}
    )

    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_INVALID_EVIDENCE",
    ) as overflow_rejection:
        asyncio.run(
            ModelCurator(_gateway(draft)[0]).extract_reported(
                future, 23, world.source_commit, world, _request()
            )
        )
    assert overflow_rejection.value.safe_feedback == (
        (
            f"{selection.block_id.root}: require 0 <= start < end <= {len(block.text)}; "
            f"received start={selection.start}, end={overflow.end}"
        ),
    )

    invalid_coordinates = selection.model_copy(
        update={"start": len(block.text) + 20, "end": len(block.text) + 40}
    )
    invalid_draft = _draft().model_copy(
        update={
            "operations": (operation.model_copy(update={"evidence_refs": (invalid_coordinates,)}),)
        }
    )
    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_INVALID_EVIDENCE",
    ) as coordinate_rejection:
        asyncio.run(
            ModelCurator(_gateway(invalid_draft)[0]).extract_reported(
                future, 23, world.source_commit, world, _request()
            )
        )
    assert "received start=" in coordinate_rejection.value.safe_feedback[0]


def test_model_curator_drops_unchanged_existing_state_replacements() -> None:
    bundle = make_synthetic_bundle()
    future = bundle.text_roots[1]
    world = bundle.world_roots[0]
    current = world.states[0]
    if not isinstance(current.value, (str, int, float, bool, type(None))):
        raise AssertionError("synthetic state fixture must contain a scalar value")
    selection = _draft().operations[0].evidence_refs[0]
    draft = ChapterChangeDraft(
        chapter_index=23,
        operations=(
            CuratedOperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=current.state_id,
                record=CuratorStateRecord(
                    subject_id=current.subject_id,
                    predicate=current.predicate,
                    value=current.value,
                    valid_time=CuratorStoryTime(
                        worldline=current.valid_time.worldline,
                        start_ordinal=23,
                    ),
                    truth_class=current.truth_class,
                ),
                evidence_refs=(selection,),
            ),
        ),
    )

    changes, _, reported = asyncio.run(
        ModelCurator(_gateway(draft)[0]).extract_reported(
            future, 23, world.source_commit, world, _request()
        )
    )

    assert changes.operations == ()
    assert current.state_id.root in reported.unresolved[-1]


def test_v2_entity_alias_normalization_covers_event_and_relation_records() -> None:
    world = make_synthetic_bundle().world_roots[0]
    canonical = world.entities[0].entity_id
    shortened = StableId(f"entity.short.{canonical.root.rsplit('.', 1)[-1]}")
    candidate = StableId("candidate.alias-normalization")
    event = CuratedOperationDraftV2(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.EVENT,
        target_id=StableId("event.alias-normalization"),
        record=CuratorEventRecord(
            event_type="arrives",
            participant_ids=(shortened,),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
        evidence_candidate_ids=(candidate,),
    )
    relation = CuratedOperationDraftV2(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.RELATION,
        target_id=StableId("relation.alias-normalization"),
        record=CuratorRelationRecord(
            subject_id=shortened,
            predicate="trusts",
            object_id=shortened,
            valid_time=CuratorStoryTime(worldline="main", start_ordinal=23),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
        evidence_candidate_ids=(candidate,),
    )

    normalized = ModelCurator._normalize_entity_reference_aliases(
        ChapterChangeDraftV2(chapter_index=23, operations=(event, relation)),
        world,
    )
    event_record = normalized.operations[0].record
    relation_record = normalized.operations[1].record

    assert isinstance(event_record, CuratorEventRecord)
    assert event_record.participant_ids == (canonical,)
    assert isinstance(relation_record, CuratorRelationRecord)
    assert relation_record.subject_id == canonical
    assert relation_record.object_id == canonical


def test_v2_normalized_collisions_merge_evidence_and_reject_conflicts() -> None:
    base_commit = make_synthetic_bundle().world_roots[0].source_commit
    record = CuratorEventRecord(
        event_type="arrives",
        truth_class=TruthClass.ACCEPTED_WORLD_FACT,
    )
    first = CuratedOperationDraftV2(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.EVENT,
        target_id=StableId("event.v2-collision"),
        record=record,
        evidence_candidate_ids=(StableId("candidate.one"),),
    )
    second = first.model_copy(update={"evidence_candidate_ids": (StableId("candidate.two"),)})
    singleton = first.model_copy(update={"target_id": StableId("event.v2-singleton")})
    draft = ChapterChangeDraftV2(
        chapter_index=23,
        operations=(first, second, singleton),
    )

    merged, receipts = ModelCurator._merge_normalized_collisions_v2(draft, base_commit)

    assert len(merged.operations) == 2
    assert merged.operations[0].evidence_candidate_ids == (
        StableId("candidate.one"),
        StableId("candidate.two"),
    )
    assert len(receipts) == 1

    conflict = draft.model_copy(
        update={
            "operations": (
                first,
                second.model_copy(
                    update={
                        "record": record.model_copy(update={"event_type": "leaves"}),
                    }
                ),
            )
        }
    )
    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_NORMALIZED_TARGET_COLLISION",
    ) as collision:
        ModelCurator._merge_normalized_collisions_v2(conflict, base_commit)
    assert collision.value.operation_indexes == (0, 1)
    assert collision.value.violation_rule == "normalized_target_must_be_unique"

    overflow = ChapterChangeDraftV2(
        chapter_index=23,
        operations=(
            first,
            first.model_copy(update={"evidence_candidate_ids": (StableId("candidate.two"),)}),
            first.model_copy(
                update={
                    "evidence_candidate_ids": (
                        StableId("candidate.three"),
                        StableId("candidate.four"),
                        StableId("candidate.five"),
                    )
                }
            ),
        ),
    )
    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_NORMALIZED_TARGET_COLLISION",
    ) as overflow_rejection:
        ModelCurator._merge_normalized_collisions_v2(overflow, base_commit)
    assert overflow_rejection.value.violation_rule == "normalized_target_evidence_must_be_bounded"


def test_record_kind_coverage_receipt_accounts_proposed_accepted_rejected() -> None:
    """2026-08-13 repair E: per-source-unit durable record coverage receipt."""
    base_commit = make_synthetic_bundle().world_roots[0].source_commit
    event = CuratedOperationDraftV2(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.EVENT,
        target_id=StableId("event.coverage"),
        record=CuratorEventRecord(
            event_type="arrives",
            participant_ids=(),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
        evidence_candidate_ids=(StableId("candidate.one"),),
    )
    state = CuratedOperationDraftV2(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.STATE,
        target_id=StableId("state.coverage"),
        record=CuratorStateRecord(
            predicate="injury",
            subject_id=StableId("entity.coverage"),
            value="healing",
            valid_time=CuratorStoryTime(worldline="main", start_ordinal=23),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
        evidence_candidate_ids=(StableId("candidate.two"),),
    )
    curator = ModelCurator(
        _gateway(ChapterChangeDraft(chapter_index=23, operations=(), coverage=0.0))[0]
    )
    # Two events and one state proposed; one event and the state accepted.
    curator._pending_record_kind_proposed = {
        WorldRecordKind.EVENT: 2,
        WorldRecordKind.STATE: 1,
    }
    receipt = curator._build_record_kind_coverage(
        chapter_id=StableId("chapter.coverage"),
        base_commit=base_commit,
        request_id=StableId("request.coverage"),
        draft=ChapterChangeDraftV2(chapter_index=23, operations=(event, state)),
        accepted_kinds=(WorldRecordKind.EVENT, WorldRecordKind.STATE),
    )
    by_kind = {item.record_kind: item for item in receipt.counts}
    assert by_kind[WorldRecordKind.EVENT].proposed == 2
    assert by_kind[WorldRecordKind.EVENT].accepted == 1
    assert by_kind[WorldRecordKind.EVENT].rejected == 1
    assert by_kind[WorldRecordKind.STATE].proposed == 1
    assert by_kind[WorldRecordKind.STATE].accepted == 1
    assert by_kind[WorldRecordKind.STATE].rejected == 0
    assert receipt.no_durable_delta is False
    assert receipt.receipt_id.root.startswith("record-kind-coverage.")

    curator._pending_record_kind_proposed = {}
    empty = curator._build_record_kind_coverage(
        chapter_id=StableId("chapter.no-op"),
        base_commit=base_commit,
        request_id=StableId("request.no-op"),
        draft=ChapterChangeDraftV2(
            chapter_index=23,
            operations=(),
            no_durable_delta_reason="chapter has no durable change",
        ),
        accepted_kinds=(),
    )
    assert empty.no_durable_delta is True
    assert empty.counts == ()
    assert empty.no_durable_delta_reason == "chapter has no durable change"


def test_record_kind_coverage_receipt_validates_counts_and_uniqueness() -> None:
    import pytest
    from pydantic import ValidationError

    from novel_agent.domain.memory_write import (
        CuratorRecordKindCounts,
        CuratorRecordKindCoverageReceipt,
    )

    with pytest.raises(ValidationError, match="cannot exceed proposed"):
        CuratorRecordKindCounts(
            record_kind=WorldRecordKind.EVENT,
            proposed=1,
            accepted=2,
            rejected=0,
        )
    with pytest.raises(ValidationError, match="unique per kind"):
        base = make_synthetic_bundle().world_roots[0].source_commit
        counts = (
            CuratorRecordKindCounts(
                record_kind=WorldRecordKind.EVENT, proposed=1, accepted=1, rejected=0
            ),
            CuratorRecordKindCounts(
                record_kind=WorldRecordKind.EVENT, proposed=1, accepted=0, rejected=1
            ),
        )
        CuratorRecordKindCoverageReceipt(
            receipt_id=StableId("receipt.dup"),
            workflow_request_id=StableId("request.dup"),
            base_commit=base,
            chapter_id=StableId("chapter.dup"),
            source_unit_id=StableId("chapter.dup"),
            no_durable_delta=False,
            counts=counts,
            producer_version="test",
        )


def test_model_curator_constructor_validates_graph_parameters() -> None:
    import pytest

    with pytest.raises(ValueError, match="max_concurrent_graph_units"):
        ModelCurator(_gateway(_draft())[0], max_concurrent_graph_units=0)
    with pytest.raises(ValueError, match="max_pages_per_graph_unit"):
        ModelCurator(_gateway(_draft())[0], max_pages_per_graph_unit=0)
    with pytest.raises(ValueError, match="graph_source_unit_tokens"):
        ModelCurator(_gateway(_draft())[0], graph_source_unit_tokens=0)
