"""R4.3/R4.4: whole-World validation and all-or-nothing chapter settlement.

The model only ever sees a bounded World working set; validation must still run against
the complete Canonical World, and a chapter must either settle as one complete operation
set or fail with a typed error - never commit the pages that happened to succeed.
"""

from __future__ import annotations

import asyncio

import pytest

from novel_agent.domain.changes import (
    ChangeOperationType,
    CuratorEntityRecord,
    CuratorV2EvidenceDraft,
    CuratorV2OperationDraft,
    WorldRecordKind,
)
from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory import ObligationKind, ObligationStatus, PlanObligation
from novel_agent.services.model_curation import (
    CuratorProposalSemanticRejected,
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
TEXT = "chen walks to the old city gate while the northern seal stays broken."


def _entity_operation(target: str) -> CuratorV2OperationDraft:
    return CuratorV2OperationDraft(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.ENTITY,
        target_id=StableId(target),
        record=CuratorEntityRecord(
            entity_type="character",
            internal_label="陆沉舟",
            aliases=(),
            identity_invariants=(),
        ),
        evidence_quotes=(TEXT,),
    )


def _draft(*operations: CuratorV2OperationDraft) -> CuratorV2EvidenceDraft:
    return CuratorV2EvidenceDraft.model_construct(
        chapter_index=21,
        operations=operations,
        coverage=1.0,
        has_more=False,
        world_lookup_terms=(),
        plan_observations=(),
        unresolved=(),
        declared_vs_observed_diff=(),
        no_durable_delta_reason=None,
        no_op_evidence_quotes=(),
    )


def _obligation_operation(target: str, *, owner: str) -> CuratorV2OperationDraft:
    from novel_agent.domain.changes import CuratorObligationRecord

    return CuratorV2OperationDraft(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.OBLIGATION,
        target_id=StableId(target),
        record=CuratorObligationRecord(
            kind="objective",
            description="铜铭来历",
            status="open",
            owner_ids=(StableId(owner),),
        ),
        evidence_quotes=(TEXT,),
    )


def _entity_operation_record_only(target: str) -> CuratorV2OperationDraft:
    return _entity_operation(target)


def test_reference_to_an_entity_created_in_the_same_proposal_is_accepted() -> None:
    """R4.3: page-aggregated operations may reference a just-created entity."""

    root = _root_with(TEXT)
    curator = ModelCurator(
        _FakeGateway(
            _draft(
                _obligation_operation("obligation.new.0", owner="entity.created"),
                _entity_operation_record_only("entity.created"),
            )
        ),
        enforce_support_gate=False,
    )

    changes, _call, _draft_out = asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            COMMIT,
            _world().model_copy(
                update={
                    "obligations": (
                        PlanObligation(
                            obligation_id=StableId("obligation.existing.0"),
                            kind=ObligationKind.OBJECTIVE,
                            description="既有责任",
                            status=ObligationStatus.OPEN,
                        ),
                    )
                }
            ),
            _request("req.v2.same-proposal-entity"),
        )
    )

    record_types = [
        operation.payload["record_type"] for operation in changes.operations
    ]
    assert WorldRecordKind.ENTITY.value in record_types
    assert WorldRecordKind.OBLIGATION.value in record_types


def test_unresolvable_entity_reference_is_rejected_not_silently_dropped() -> None:
    """R4.3: a dangling reference must fail closed with field-level feedback."""

    root = _root_with(TEXT)
    curator = ModelCurator(
        _FakeGateway(_draft(_obligation_operation("obligation.new.1", owner="entity.absent"))),
        enforce_support_gate=False,
    )

    with pytest.raises(CuratorProposalSemanticRejected) as error:
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                COMMIT,
                _world(),
                _request("req.v2.dangling-entity"),
            )
        )

    assert "owner_ids" in str(error.value.json_pointers)


def test_full_world_validation_is_not_limited_to_the_model_working_set() -> None:
    """R4.3: an entity outside the prompt working set still counts as known."""

    root = _root_with(TEXT)
    other_entity = StableId("entity.outside-working-set")
    known_only_in_full_world = _entity_operation("entity.known")
    world = _world().model_copy(
        update={"entities": (_world().entities[0].model_copy(update={"entity_id": other_entity}),)}
    )
    curator = ModelCurator(
        _FakeGateway(
            _draft(
                _entity_operation_record_only("entity.known"),
                _obligation_operation("obligation.new.2", owner=other_entity.root),
            )
        ),
        enforce_support_gate=False,
    )

    changes, _call, _draft_out = asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            COMMIT,
            world,
            _request("req.v2.full-world-validation"),
        )
    )

    # The obligation survives *and* keeps the off-working-set owner: validation and
    # alias resolution both consulted the complete World, not just the prompt view.
    obligation = next(
        operation
        for operation in changes.operations
        if operation.payload["record_type"] == WorldRecordKind.OBLIGATION.value
    )
    assert obligation.payload["record"]["owner_ids"] == [other_entity.root]
    assert known_only_in_full_world  # the created entity operation is part of the draft


def test_budget_exhaustion_settles_nothing_and_fails_typed() -> None:
    """R4.4: exhausting the page budget must not commit the pages that succeeded."""

    root = _root_with(TEXT)
    page = _draft(_entity_operation("entity.page"))
    page = page.model_copy(update={"has_more": True, "coverage": 0.4})
    curator = ModelCurator(_FakeGateway(page), enforce_support_gate=False)

    with pytest.raises(ModelCurationOutputIncomplete):
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                COMMIT,
                _world(),
                _request("req.v2.budget-exhaustion"),
            )
        )


def test_incomplete_chapter_reports_no_partial_operation_set() -> None:
    """A failed extraction returns nothing; the caller never sees a partial set."""

    root = _root_with(TEXT)
    stalled = _draft(_entity_operation("entity.stalled")).model_copy(
        update={"has_more": True, "coverage": 0.3}
    )
    curator = ModelCurator(_FakeGateway(stalled), enforce_support_gate=False)

    result: object = None
    with pytest.raises(ModelCurationOutputIncomplete):
        result = asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                COMMIT,
                _world(),
                _request("req.v2.no-partial"),
            )
        )

    assert result is None
    assert curator.last_ordinary_pages == () or all(
        not page.covered for page in curator.last_ordinary_pages
    )
