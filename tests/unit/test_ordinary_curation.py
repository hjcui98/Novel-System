"""R4.1: bounded whole-chapter curation with continuation pages.

A Curator response may carry at most four operations.  That is a response limit, never a
chapter capacity: a chapter with more than four durable changes must still be fully
extracted, and the host aggregates the pages before validating against the full World.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from novel_agent.domain.benchmark import (
    ChapterDocument,
    SceneDocument,
    TextBlock,
)
from novel_agent.domain.changes import (
    ChangeOperationType,
    CuratorEventRecord,
    CuratorObligationRecord,
    CuratorV2EvidenceDraft,
    CuratorV2OperationDraft,
    WorldRecordKind,
)
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    WorldRootDocument,
)
from novel_agent.domain.world import Entity, TruthClass
from novel_agent.services.ordinary_curation import (
    OrdinaryCurationIncomplete,
    extract_source_batches,
    source_batches,
    world_working_view,
)
from tests.unit.test_curator_evidence_contract_v2 import _request

VERSION = SchemaVersion("0.1.0")
HASH = ArtifactId("sha256:" + "1" * 64)
COMMIT = CommitId("sha256:" + "2" * 64)
CHAPTER = ChapterDocument(
    chapter_id=StableId("chapter.v2.1"),
    chapter_index=21,
    scenes=(
        SceneDocument(
            scene_id=StableId("scene.v2.1"),
            scene_index=0,
            blocks=(
                TextBlock(
                    block_id=StableId("block.v2.1"),
                    chapter_id=StableId("chapter.v2.1"),
                    scene_id=StableId("scene.v2.1"),
                    narrative_index=0,
                    text="陈长生握紧铜铭。铜铭来自旧城。",
                ),
            ),
        ),
    ),
)


def _world() -> WorldRootDocument:
    return WorldRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        source_commit=COMMIT,
        entities=(),
    )


class _PageGateway:
    """Serve a scripted page sequence and record every request."""

    def __init__(self, pages: list[CuratorV2EvidenceDraft]) -> None:
        self._pages = pages
        self.requests: list[object] = []

    async def generate_structured(self, request, model_type, **kwargs):
        self.requests.append(request)
        assert model_type is CuratorV2EvidenceDraft
        page = self._pages[min(len(self.requests) - 1, len(self._pages) - 1)]
        call = type(
            "Call",
            (),
            {
                "request_id": request.request_id,
                "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})(),
            },
        )()
        return page, call


def _operation(target: str, *, kind: WorldRecordKind = WorldRecordKind.EVENT):
    record = (
        CuratorEventRecord(
            event_type="获得铜铭",
            participant_ids=(StableId("entity.chen"),),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        )
        if kind is WorldRecordKind.EVENT
        else CuratorObligationRecord(
            kind="objective",
            description="铜铭来历",
            status="progressed",
        )
    )
    return CuratorV2OperationDraft(
        operation=ChangeOperationType.CREATE,
        record_kind=kind,
        target_id=StableId(target),
        record=record,
        evidence_quotes=("陈长生握紧铜铭。",),
    )


def _draft(*, operations=(), has_more: bool, coverage: float = 1.0) -> CuratorV2EvidenceDraft:
    return CuratorV2EvidenceDraft.model_construct(
        chapter_index=21,
        operations=operations,
        coverage=coverage,
        has_more=has_more,
        world_lookup_terms=(),
        plan_observations=(),
        unresolved=(),
        declared_vs_observed_diff=(),
        no_durable_delta_reason=None,
        no_op_evidence_quotes=(),
    )


def _enveloped_request(request_id: str = "request.ordinary") -> object:
    request = _request(request_id)
    return request.model_copy(
        update={
            "prompt": (
                "curator instructions\n"
                '<CURATOR_INPUT trusted="false">\nstale\n</CURATOR_INPUT>\n'
                "tail instructions"
            )
        }
    )


def test_source_batches_never_exceed_the_source_budget() -> None:
    long_block = TextBlock(
        block_id=StableId("block.long"),
        chapter_id=StableId("chapter.long"),
        scene_id=StableId("scene.long"),
        narrative_index=0,
        text=("陈长生向前走去。" * 3_000),
    )
    chapter = ChapterDocument(
        chapter_id=StableId("chapter.long"),
        chapter_index=1,
        scenes=(
            SceneDocument(scene_id=StableId("scene.long"), scene_index=0, blocks=(long_block,)),
        ),
    )

    batches = source_batches(chapter)

    assert len(batches) > 1
    for batch in batches:
        assert sum(len(unit.text) for unit in batch) <= 5_000 + 2_400
    covered = "".join(unit.text for batch in batches for unit in batch)
    assert covered.startswith("陈长生向前走去。")
    assert len(covered) >= len(long_block.text)


def test_source_batches_are_empty_for_an_empty_chapter() -> None:
    chapter = ChapterDocument(chapter_id=StableId("chapter.empty"), chapter_index=1, scenes=())

    assert source_batches(chapter) == ()


def test_world_working_view_is_scoped_and_reports_omissions() -> None:
    """Only records whose owner appears in the source slice enter the working set."""

    world = _world().model_copy(
        update={
            "entities": (
                Entity(
                    entity_id=StableId("entity.chen"),
                    entity_type="character",
                    internal_label="陈长生",
                ),
            ),
            "obligations": tuple(
                PlanObligation(
                    obligation_id=StableId(f"obligation.test.{index}"),
                    kind=ObligationKind.OBJECTIVE,
                    description=f"目标{index}",
                    status=ObligationStatus.OPEN,
                    owner_ids=(
                        (StableId("entity.chen"),) if index == 0 else (StableId("entity.other"),)
                    ),
                )
                for index in range(5)
            ),
        }
    )

    view = world_working_view(world, "陈长生握紧铜铭。", ())

    kept = view["obligations"]
    assert [item["obligation_id"] for item in kept] == ["obligation.test.0"]
    assert view["omitted_record_counts"]["obligations"] == 4
    assert "working set, not the complete World" in view["lookup_contract"]
    assert [item["entity_id"] for item in view["entities"]] == ["entity.chen"]


def test_world_working_view_keeps_exact_lookup_matches() -> None:
    world = _world().model_copy(
        update={
            "obligations": tuple(
                PlanObligation(
                    obligation_id=StableId(f"obligation.test.{index}"),
                    kind=ObligationKind.OBJECTIVE,
                    description=f"目标{index}",
                    status=ObligationStatus.OPEN,
                )
                for index in range(200)
            )
        }
    )

    view = world_working_view(world, "无关正文", ("obligation.test.3",))

    kept = view["obligations"]
    assert any(item["obligation_id"] == "obligation.test.3" for item in kept)
    assert len(kept) <= 128
    assert view["omitted_record_counts"]["obligations"] == 200 - len(kept)


def test_continuation_pages_aggregate_more_than_four_operations() -> None:
    pages = [
        _draft(
            operations=tuple(_operation(f"event.page1.{index}") for index in range(4)),
            has_more=True,
            coverage=0.5,
        ),
        _draft(
            operations=tuple(_operation(f"event.page2.{index}") for index in range(3)),
            has_more=False,
        ),
    ]
    gateway = _PageGateway(pages)

    draft, calls, receipts = asyncio.run(
        extract_source_batches(
            gateway,
            _enveloped_request(),
            CHAPTER,
            _world(),
            (),
            base_commit=COMMIT,
            cumulative_token_budget=None,
            cumulative_tokens_used=0,
        )
    )
    assert len(draft.operations) == 7
    assert len(calls) == 2
    assert [receipt.has_more for receipt in receipts] == [True, False]
    assert receipts[-1].covered is True
    assert receipts[0].operation_count == 4
    assert draft.coverage == 1.0
    assert draft.has_more is False
    second_prompt = gateway.requests[1].prompt
    assert "BATCH_CONTINUATION_CONTRACT" in second_prompt


def test_a_stalled_continuation_fails_closed() -> None:
    """has_more with no new operation and no lookup is not progress."""

    stalled = _draft(operations=(), has_more=True, coverage=0.5)
    gateway = _PageGateway([stalled])

    with pytest.raises(OrdinaryCurationIncomplete, match="no progress"):
        asyncio.run(
            extract_source_batches(
                gateway,
                _enveloped_request(),
                CHAPTER,
                _world(),
                (),
                base_commit=COMMIT,
                cumulative_token_budget=None,
                cumulative_tokens_used=0,
            )
        )


def test_incomplete_coverage_without_more_fails_closed() -> None:
    partial = _draft(operations=(), has_more=False, coverage=0.5)
    gateway = _PageGateway([partial])

    with pytest.raises(OrdinaryCurationIncomplete, match="incomplete coverage"):
        asyncio.run(
            extract_source_batches(
                gateway,
                _enveloped_request(),
                CHAPTER,
                _world(),
                (),
                base_commit=COMMIT,
                cumulative_token_budget=None,
                cumulative_tokens_used=0,
            )
        )


def test_missing_source_envelope_fails_closed() -> None:
    gateway = _PageGateway([_draft(operations=(), has_more=False)])

    with pytest.raises(OrdinaryCurationIncomplete, match="source envelope"):
        asyncio.run(
            extract_source_batches(
                gateway,
                _request("request.no-envelope"),
                CHAPTER,
                _world(),
                (),
                base_commit=COMMIT,
                cumulative_token_budget=None,
                cumulative_tokens_used=0,
            )
        )


def test_page_receipts_name_their_source_units_and_request() -> None:
    gateway = _PageGateway([_draft(operations=(), has_more=False)])

    _draft_result, _calls, receipts = asyncio.run(
        extract_source_batches(
            gateway,
            _enveloped_request(),
            CHAPTER,
            _world(),
            (),
            base_commit=COMMIT,
            cumulative_token_budget=None,
            cumulative_tokens_used=0,
        )
    )

    receipt = receipts[0]
    assert receipt.source_unit_ids
    assert receipt.model_request_id.root
    assert "block.v2.1" in receipt.source_unit_ids[0]


def test_request_payload_carries_the_page_contract() -> None:
    gateway = _PageGateway([_draft(operations=(), has_more=False)])

    asyncio.run(
        extract_source_batches(
            gateway,
            _enveloped_request(),
            CHAPTER,
            _world(),
            (),
            base_commit=COMMIT,
            cumulative_token_budget=None,
            cumulative_tokens_used=0,
        )
    )

    prompt = gateway.requests[0].prompt
    payload = json.loads(prompt.split("EVIDENCE_CANDIDATES=", 1)[1].split("\n", 1)[0])
    assert payload[0]["block_id"] == "block.v2.1"
    assert "PLANNED_OBLIGATIONS" in prompt
    assert prompt.startswith("curator instructions")
    assert prompt.rstrip().endswith("tail instructions")
