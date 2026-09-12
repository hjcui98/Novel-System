"""R4.5: obligation observation through the real Curator path.

A chapter that advances an accepted durable obligation must write that progress back to
Canon under the *same* obligation identity.  The model reports the observation; the host
binds it to a REPLACE/CREATE obligation operation with the source quotes, and refuses an
observation that resolves a still time-locked obligation.
"""

from __future__ import annotations

import asyncio

import pytest

from novel_agent.domain.changes import (
    ChangeOperationType,
    CuratorV2EvidenceDraft,
    PlannedObligationObservation,
    WorldRecordKind,
)
from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory import (
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    WorldRootDocument,
)
from novel_agent.services.model_curation import (
    ModelCurationOutputIncomplete,
    ModelCurator,
)
from tests.unit.test_curator_evidence_contract_v2 import (
    _FakeGateway,
    _request,
    _root_with,
    _world,
)

COMMIT = CommitId("sha256:" + "3" * 64)
OBLIGATION = StableId("obligation.vol_01.0.objective")
TEXT = "chen obtains the bronze token from the old city."


def _planned(*, not_before: int | None = None, due: int | None = None) -> PlanObligation:
    return PlanObligation(
        obligation_id=OBLIGATION,
        kind=ObligationKind.OBJECTIVE,
        description="chen obtains the bronze token",
        status=ObligationStatus.OPEN,
        not_before_chapter=not_before,
        due_chapter=due,
    )


def _world_with(*obligations: PlanObligation) -> WorldRootDocument:
    return _world().model_copy(update={"obligations": obligations})


def _observation(
    *,
    status: str = "progressed",
    quote: str = TEXT,
    obligation_id: StableId = OBLIGATION,
) -> PlannedObligationObservation:
    return PlannedObligationObservation.model_construct(
        obligation_id=obligation_id,
        status=status,
        rationale="chapter shows the acquisition",
        evidence_quotes=(quote,),
    )


def _draft(*observations: PlannedObligationObservation) -> CuratorV2EvidenceDraft:
    return CuratorV2EvidenceDraft.model_construct(
        chapter_index=21,
        operations=(),
        coverage=1.0,
        has_more=False,
        world_lookup_terms=(),
        plan_observations=observations,
        unresolved=(),
        declared_vs_observed_diff=(),
        no_durable_delta_reason=None,
        no_op_evidence_quotes=(),
    )


def test_plan_observation_becomes_an_obligation_operation() -> None:
    root = _root_with(TEXT)
    curator = ModelCurator(_FakeGateway(_draft(_observation())), enforce_support_gate=False)

    changes, _call, _draft_out = asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            COMMIT,
            _world_with(_planned()),
            _request("req.v2.plan-observation"),
            planned_obligations=(_planned(),),
        )
    )

    obligation_operations = [
        operation
        for operation in changes.operations
        if operation.payload["record_type"] == WorldRecordKind.OBLIGATION.value
    ]
    assert len(obligation_operations) == 1
    operation = obligation_operations[0]
    assert operation.target_id == OBLIGATION
    assert operation.payload["record"]["status"] == "progressed"
    assert operation.evidence_refs


def test_progressed_observation_creates_when_the_world_has_no_record_yet() -> None:
    root = _root_with(TEXT)
    curator = ModelCurator(_FakeGateway(_draft(_observation())), enforce_support_gate=False)

    changes, _call, _draft_out = asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            COMMIT,
            _world_with(),
            _request("req.v2.plan-observation-create"),
            planned_obligations=(_planned(),),
        )
    )

    obligations = [
        operation
        for operation in changes.operations
        if operation.payload["record_type"] == WorldRecordKind.OBLIGATION.value
    ]
    assert len(obligations) == 1
    assert obligations[0].operation is ChangeOperationType.CREATE
    assert obligations[0].target_id == OBLIGATION


def test_observation_for_an_unrequested_obligation_fails_closed() -> None:
    root = _root_with(TEXT)
    other = StableId("obligation.not-requested")
    curator = ModelCurator(
        _FakeGateway(_draft(_observation(obligation_id=other))),
        enforce_support_gate=False,
    )

    with pytest.raises(ModelCurationOutputIncomplete):
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                COMMIT,
                _world_with(_planned()),
                _request("req.v2.plan-observation-unknown"),
                planned_obligations=(_planned(),),
            )
        )


def test_resolving_a_time_locked_obligation_fails_closed() -> None:
    """A reveal-locked obligation may be set up but never paid off early."""

    root = _root_with(TEXT)
    curator = ModelCurator(
        _FakeGateway(_draft(_observation(status="resolved"))),
        enforce_support_gate=False,
    )

    with pytest.raises(ModelCurationOutputIncomplete) as error:
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                COMMIT,
                _world_with(_planned(not_before=101)),
                _request("req.v2.plan-observation-locked"),
                planned_obligations=(_planned(not_before=101),),
            )
        )
    # The time-lock refusal is a typed content failure for the repair corridor.
    assert "before its time lock" in str(error.value.__cause__)


def test_due_milestone_without_completion_evidence_fails_closed() -> None:
    root = _root_with(TEXT)
    milestone = _planned(due=21).model_copy(update={"obligation_id": StableId("milestone.vol_01")})
    curator = ModelCurator(_FakeGateway(_draft()), enforce_support_gate=False)

    with pytest.raises(ModelCurationOutputIncomplete):
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                COMMIT,
                _world_with(milestone),
                _request("req.v2.plan-milestone-due"),
                planned_obligations=(milestone,),
            )
        )
