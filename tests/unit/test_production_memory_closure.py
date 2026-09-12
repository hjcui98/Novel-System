"""Regression cases for complete chapter ingestion and durable provider replay."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.domain.changes import (
    ChangeOperationType,
    ChapterChangeDraftV2,
    CuratorStateRecord,
    CuratorStoryTime,
    CuratorV2EvidenceDraft,
    CuratorV2OperationDraft,
    PlannedObligationObservation,
    WorldRecordKind,
)
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import ObligationKind, ObligationStatus, PlanObligation
from novel_agent.domain.world import TruthClass
from novel_agent.services.model_curation import ModelCurationContractError, ModelCurator
from novel_agent.services.model_gateway import ModelCallSliceExhausted, ModelGateway
from novel_agent.services.ordinary_curation import source_batches, world_working_view
from tests.unit.test_curator_evidence_contract_v2 import _request, _root_with, _world
from tests.unit.test_model_gateway_raw_evidence import _endpoint, _repository


class PagesEndpoint(FakeModelEndpoint):
    def __init__(self, pages: tuple[CuratorV2EvidenceDraft, ...]) -> None:
        super().__init__("")
        self.pages = iter(pages)

    async def generate(self, request):
        self.response_text = next(self.pages).model_dump_json()
        return await super().generate(request)


def _pages(*, final_coverage: float = 1.0) -> tuple[CuratorV2EvidenceDraft, ...]:
    operations = tuple(
        CuratorV2OperationDraft(
            operation=ChangeOperationType.CREATE,
            record_kind=WorldRecordKind.STATE,
            target_id=StableId(f"state.chen.key.{index}"),
            record=CuratorStateRecord(
                subject_id=StableId("entity.chen"),
                predicate=f"key_{index}",
                value="acquired",
                valid_time=CuratorStoryTime(worldline="main"),
                truth_class=TruthClass.ACCEPTED_WORLD_FACT,
            ),
            evidence_quotes=(f"Chen acquired key {index}.",),
        )
        for index in range(6)
    )
    # Saturating a page must force a continuation even if the model forgot has_more.
    return (
        CuratorV2EvidenceDraft(chapter_index=21, operations=operations[:4]),
        CuratorV2EvidenceDraft(
            chapter_index=21, operations=operations[4:], coverage=final_coverage
        ),
    )


def test_six_changes_cross_response_limit_and_reach_bound_world_operations() -> None:
    endpoint = PagesEndpoint(_pages())
    gateway = ModelGateway((_endpoint(endpoint),))
    curator = ModelCurator(gateway, enforce_support_gate=False)
    text = " ".join(f"Chen acquired key {index}." for index in range(6))
    world = _world()
    changes, _, draft = asyncio.run(
        curator.extract_reported_v2(
            _root_with(text),
            21,
            world.source_commit,
            world,
            _request("curator.six"),
        )
    )
    assert len(changes.operations) == len(draft.operations) == 6
    assert len(ChapterChangeDraftV2.model_validate_json(draft.model_dump_json()).operations) == 6
    assert {operation.target_id.root for operation in changes.operations} == {
        f"state.chen.key.{index}" for index in range(6)
    }
    assert all(
        operation.evidence_refs and operation.evidence_refs[0].span
        for operation in changes.operations
    )
    assert len(endpoint.requests) == 2
    assert "ALREADY_EXTRACTED=" in endpoint.requests[1].prompt
    assert curator.last_ordinary_pages[-1].covered


def test_partial_final_page_cannot_settle_an_apparently_valid_first_page() -> None:
    endpoint = PagesEndpoint(_pages(final_coverage=0.5))
    curator = ModelCurator(ModelGateway((_endpoint(endpoint),)), enforce_support_gate=False)
    world = _world()
    with pytest.raises(ModelCurationContractError, match="incomplete coverage"):
        asyncio.run(
            curator.extract_reported_v2(
                _root_with(" ".join(f"Chen acquired key {i}." for i in range(6))),
                21,
                world.source_commit,
                world,
                _request("curator.incomplete"),
            )
        )


def test_source_windows_cover_every_character_and_keep_split_context() -> None:
    text = "A sentence with a persistent consequence. " * 220
    chapter = _root_with(text).chapters[0]
    batches = source_batches(chapter)
    covered: set[int] = set()
    for batch in batches:
        assert sum(len(unit.text) for unit in batch) <= 5000
        for unit in batch:
            start, end = (int(item) for item in unit.unit_id.rsplit(":", 2)[-2:])
            assert unit.text == text[start:end]
            covered.update(range(start, end))
    assert covered == set(range(len(text)))
    assert len(batches) > 1


def test_exact_lookup_recovers_an_entity_absent_from_current_prose() -> None:
    world = _world()
    assert world_working_view(world, "Someone waits.")["entities"] == []
    view = world_working_view(world, "Someone waits.", ("entity.chen",))
    assert json.loads(json.dumps(view))["entities"][0]["entity_id"] == "entity.chen"


@pytest.mark.parametrize("observed", ["resolved", "not_observed"])
def test_due_milestone_uses_planned_identity_and_requires_prose_evidence(observed) -> None:
    intent = PlanObligation(
        obligation_id=StableId("milestone.obtain-key"),
        kind=ObligationKind.OBJECTIVE,
        description="Chen obtains the key",
        status=ObligationStatus.OPEN,
        target_chapter_start=21,
        target_chapter_end=21,
        due_chapter=21,
    )
    quote = "Chen acquired key 0."
    page = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(),
        no_durable_delta_reason="No ordinary change",
        no_op_evidence_quotes=(quote,),
        plan_observations=(
            PlannedObligationObservation(
                obligation_id=intent.obligation_id,
                status=observed,
                rationale="Observe the action in the source",
                evidence_quotes=(quote,),
            ),
        ),
    )
    curator = ModelCurator(
        ModelGateway((_endpoint(PagesEndpoint((page,))),)), enforce_support_gate=False
    )
    world = _world()
    call = curator.extract_reported_v2(
        _root_with(quote),
        21,
        world.source_commit,
        world,
        _request("curator.milestone"),
        planned_obligations=(intent,),
    )
    if observed == "not_observed":
        with pytest.raises(ModelCurationContractError, match="lack completion evidence"):
            asyncio.run(call)
    else:
        changes, _, _ = asyncio.run(call)
        assert len(changes.operations) == 1
        assert changes.operations[0].target_id == intent.obligation_id
        assert changes.operations[0].evidence_refs


def test_completed_call_replays_after_gateway_restart_without_provider_cost(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    endpoint = FakeModelEndpoint("retained response")
    first = ModelGateway((_endpoint(endpoint),), raw_artifacts=repository)
    request = _request("gateway.replay")
    expected = asyncio.run(first.generate_text(request))
    resumed = ModelGateway(
        (_endpoint(endpoint),), raw_artifacts=repository, call_ledger=first.call_ledger
    )
    with resumed.model_call_budget(0):
        actual = asyncio.run(resumed.generate_text(request))
    assert actual == expected
    assert len(endpoint.requests) == 1


def test_provider_budget_applies_across_editor_and_writer_gateways() -> None:
    endpoint = FakeModelEndpoint("a result")
    writer = ModelGateway((_endpoint(endpoint),))
    editor = ModelGateway((_endpoint(endpoint),))
    with writer.model_call_budget(1) as budget:
        asyncio.run(writer.generate_text(_request("budget.writer")))
        with pytest.raises(ModelCallSliceExhausted):
            asyncio.run(editor.generate_text(_request("budget.editor")))
    assert budget.calls_started == 1
    assert len(endpoint.requests) == 1


def test_cancellation_after_lease_delivery_releases_capacity(monkeypatch) -> None:
    from types import SimpleNamespace

    from novel_agent.services import model_gateway as module

    released = []
    abandoned = []
    lease = SimpleNamespace(release=lambda: released.append(True))
    controller = SimpleNamespace(
        acquire=lambda *args, **kwargs: lease,
        abandon_request=lambda identity: abandoned.append(identity),
    )

    class ImmediateThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            # Deliver the lease, then cancel before shield resumes its caller.
            caller = asyncio.current_task()
            self.target()
            asyncio.get_running_loop().call_soon(caller.cancel)

    monkeypatch.setattr(module.threading, "Thread", ImmediateThread)
    gateway = ModelGateway(
        (_endpoint(FakeModelEndpoint("unused")),), admission_controller=controller
    )

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await gateway._acquire_scheduled_lease(
                SimpleNamespace(request_id="cancel-after-delivery", scheduling_timeout_seconds=1)
            )

    asyncio.run(scenario())
    assert released == [True]
    assert abandoned == ["cancel-after-delivery"]
