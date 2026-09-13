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
    PlannedObligationObservation,
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
from novel_agent.services.model_gateway import ModelOutputBudgetExhausted
from novel_agent.services.ordinary_curation import (
    OrdinaryCurationIncomplete,
    _bill_page_attempts,
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


class _PageLedgerEntry:
    def __init__(self, request_id, call_record) -> None:
        self.request_id = request_id
        self.call_record = call_record


class _PageLedger:
    """Minimal durable-ledger double: settled attempts keyed by request identity."""

    def __init__(self) -> None:
        self.entries: list[_PageLedgerEntry] = []

    def list_for_prefix(self, request_id_prefix: str):
        return tuple(
            entry
            for entry in self.entries
            if entry.request_id.root == request_id_prefix
            or entry.request_id.root.startswith(f"{request_id_prefix}.")
        )


def _call(request_id, *, input_tokens: int = 1, output_tokens: int = 1):
    return type(
        "Call",
        (),
        {
            "request_id": request_id,
            "usage": type(
                "U",
                (),
                {"input_tokens": input_tokens, "output_tokens": output_tokens},
            )(),
        },
    )()


class _PageGateway:
    """Serve a scripted page sequence, replaying settled pages like the real gateway."""

    def __init__(self, pages: list[CuratorV2EvidenceDraft]) -> None:
        self._pages = pages
        self.requests: list[object] = []
        self.call_ledger = _PageLedger()
        self._answers: dict[str, tuple[object, object]] = {}
        self._served = 0

    async def generate_structured(self, request, model_type, **kwargs):
        assert model_type is CuratorV2EvidenceDraft
        settled = self._answers.get(request.request_id.root)
        if settled is not None:
            return settled
        self.requests.append(request)
        page = self._pages[min(self._served, len(self._pages) - 1)]
        self._served += 1
        call = _call(request.request_id)
        self.call_ledger.entries.append(_PageLedgerEntry(request.request_id, call))
        self._answers[request.request_id.root] = (page, call)
        return page, call

    def attempts_for(self, request_id_root: str):
        return tuple(
            entry.call_record
            for entry in self.call_ledger.list_for_prefix(request_id_root)
        )


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


def _draft(
    *,
    operations=(),
    has_more: bool,
    coverage: float = 1.0,
    no_durable_delta_reason: str | None = None,
    world_lookup_terms=(),
    plan_observations=(),
) -> CuratorV2EvidenceDraft:
    return CuratorV2EvidenceDraft.model_construct(
        chapter_index=21,
        operations=operations,
        coverage=coverage,
        has_more=has_more,
        world_lookup_terms=world_lookup_terms,
        plan_observations=plan_observations,
        unresolved=(),
        declared_vs_observed_diff=(),
        no_durable_delta_reason=no_durable_delta_reason,
        no_op_evidence_quotes=(),
    )


def _extract(gateway, *, page_quota=None, cumulative_token_budget=None, planned=()):
    return extract_source_batches(
        gateway,
        _enveloped_request(),
        CHAPTER,
        _world(),
        planned,
        base_commit=COMMIT,
        cumulative_token_budget=cumulative_token_budget,
        cumulative_tokens_used=0,
        page_quota=page_quota,
    )


def _observation(status: str) -> PlannedObligationObservation:
    return PlannedObligationObservation(
        obligation_id=StableId("obligation.test.0"),
        status=status,
        rationale="铜铭在陈长生手中。",
        evidence_quotes=("陈长生握紧铜铭。",),
    )


def _planned_obligation() -> PlanObligation:
    return PlanObligation(
        obligation_id=StableId("obligation.test.0"),
        kind=ObligationKind.OBJECTIVE,
        description="铜铭来历",
        status=ObligationStatus.OPEN,
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


def test_incomplete_coverage_without_more_is_reported_not_silently_covered() -> None:
    """A contradictory coverage claim is a contract defect for the support gate.

    Page exhaustion proves the *source* was read to the end; the model's own coverage
    self-report is a separate, less trustworthy signal.  This layer records the page as
    not covered and leaves the typed refusal to the no-durable-delta support gate,
    which already rejects `coverage != 1` with actionable feedback.
    """

    partial = _draft(operations=(), has_more=False, coverage=0.5, no_durable_delta_reason="无变化")
    gateway = _PageGateway([partial])

    draft, _calls, receipts = asyncio.run(
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

    assert receipts[0].covered is False
    assert receipts[0].has_more is False
    # The page is exhausted, so the aggregate must not claim more work either.  The
    # model's own coverage self-report survives aggregation: overwriting it with 1.0
    # would hide exactly the incomplete chapter the support gate exists to refuse.
    assert draft.has_more is False
    assert draft.coverage == 0.5


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


def test_empty_page_is_allowed_when_it_continues_or_looks_up() -> None:
    """The draft contract must not demand no-op proof from a working page."""

    continuation = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(),
        coverage=0.5,
        has_more=True,
    )
    assert continuation.operations == ()

    lookup = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(),
        coverage=0.5,
        world_lookup_terms=("陆沉舟",),
    )
    assert lookup.world_lookup_terms == ("陆沉舟",)

    with pytest.raises(ValueError, match="no-durable-delta reason"):
        CuratorV2EvidenceDraft(chapter_index=21, operations=(), coverage=1.0)


def test_page_quota_is_configurable_and_the_retry_resumes_from_the_ledger() -> None:
    """A bounded attempt stops at its quota; the next attempt continues past it."""

    pages = [
        _draft(operations=(_operation("event.quota.1"),), has_more=True, coverage=0.5),
        _draft(operations=(_operation("event.quota.2"),), has_more=True, coverage=0.8),
        _draft(operations=(_operation("event.quota.3"),), has_more=False),
    ]
    gateway = _PageGateway(pages)

    with pytest.raises(OrdinaryCurationIncomplete, match="configured quota of 1 new pages"):
        asyncio.run(_extract(gateway, page_quota=1))
    assert len(gateway.requests) == 1

    draft, calls, _receipts = asyncio.run(_extract(gateway, page_quota=2))

    assert len(draft.operations) == 3
    # The settled page replays from the ledger, so the retry pays for two pages only.
    assert len(gateway.requests) == 3
    assert [record.request_id.root for record in calls] == [
        "request.ordinary.ordinary.b0.p1",
        "request.ordinary.ordinary.b0.p2",
    ]


def test_every_attempt_of_a_page_is_billed_exactly_once() -> None:
    gateway = _PageGateway([_draft(operations=(), has_more=False)])
    primary = StableId("request.ordinary")
    compact = StableId("request.ordinary.compact")
    gateway.call_ledger.entries.append(
        _PageLedgerEntry(primary, _call(primary, input_tokens=3, output_tokens=2))
    )
    gateway.call_ledger.entries.append(
        _PageLedgerEntry(compact, _call(compact, input_tokens=4, output_tokens=1))
    )

    calls: list[object] = []
    billed: set[str] = set()
    total = _bill_page_attempts(gateway, primary, calls=calls, billed=billed, skip=set())

    assert total == 10
    assert [record.request_id.root for record in calls] == [
        "request.ordinary",
        "request.ordinary.compact",
    ]
    # Walking the same page twice in one pass charges it once.
    assert _bill_page_attempts(gateway, primary, calls=calls, billed=billed, skip=set()) == 0
    # An attempt settled before this pass was already charged by its owner.
    assert _bill_page_attempts(gateway, primary, calls=[], billed=set(), skip={primary.root}) == 5


class _ExhaustedThenCompactGateway(_PageGateway):
    """The primary page exhausts its legal output budget; the compact retry answers."""

    async def generate_structured(self, request, model_type, **kwargs):
        if kwargs.get("json_object_framing"):
            return await super().generate_structured(request, model_type, **kwargs)
        self.requests.append(request)
        call = _call(request.request_id, input_tokens=7, output_tokens=0)
        self.call_ledger.entries.append(_PageLedgerEntry(request.request_id, call))
        raise ModelOutputBudgetExhausted(
            "request.ordinary", request.request_id.root, 8_000, 131_072
        )


def test_an_exhausted_output_budget_takes_the_compact_page_and_keeps_both_attempts() -> None:
    page = _draft(operations=(_operation("event.compact"),), has_more=False)
    gateway = _ExhaustedThenCompactGateway([page])

    draft, calls, receipts = asyncio.run(_extract(gateway))

    assert [record.request_id.root for record in calls] == [
        "request.ordinary",
        "request.ordinary.compact",
    ]
    assert len(draft.operations) == 1
    assert receipts[0].model_request_id.root == "request.ordinary.compact"


def test_a_repeated_operation_is_not_progress() -> None:
    repeated = _operation("event.duplicate")
    pages = [
        _draft(operations=(repeated,), has_more=True, coverage=0.5),
        _draft(operations=(repeated,), has_more=True, coverage=0.6),
    ]
    gateway = _PageGateway(pages)

    with pytest.raises(OrdinaryCurationIncomplete, match="no progress"):
        asyncio.run(_extract(gateway))


def test_a_repeated_lookup_is_not_progress() -> None:
    pages = [
        _draft(operations=(), has_more=True, coverage=0.5, world_lookup_terms=("陆沉舟",)),
        _draft(operations=(), has_more=True, coverage=0.6, world_lookup_terms=("陆沉舟",)),
    ]
    gateway = _PageGateway(pages)

    with pytest.raises(OrdinaryCurationIncomplete, match="no progress"):
        asyncio.run(_extract(gateway))


def test_a_fresh_obligation_observation_is_progress() -> None:
    """The ask is re-issued for every unobserved identity, so a new one continues."""

    second = _planned_obligation().model_copy(
        update={"obligation_id": StableId("obligation.test.1")}
    )
    second_observation = _observation("progressed").model_copy(
        update={"obligation_id": StableId("obligation.test.1")}
    )
    pages = [
        _draft(
            operations=(_operation("event.rank.1"),),
            has_more=True,
            coverage=0.5,
            plan_observations=(_observation("progressed"),),
        ),
        _draft(
            operations=(),
            has_more=True,
            coverage=0.8,
            plan_observations=(second_observation,),
        ),
        _draft(operations=(_operation("event.rank.2"),), has_more=False),
    ]
    gateway = _PageGateway(pages)

    draft, _calls, receipts = asyncio.run(
        _extract(gateway, planned=(_planned_obligation(), second))
    )

    assert sum(item.record_kind is WorldRecordKind.EVENT for item in draft.operations) == 2
    assert sum(item.record_kind is WorldRecordKind.OBLIGATION for item in draft.operations) == 2
    assert [receipt.covered for receipt in receipts] == [False, False, True]
    assert {item.obligation_id.root for item in draft.plan_observations} == {
        "obligation.test.0",
        "obligation.test.1",
    }
