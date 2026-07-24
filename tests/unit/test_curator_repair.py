"""Audited Curator-repair scope and facade tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from novel_agent.agents.curator_repair import (
    CuratorRepairAgent,
    CuratorRepairContractError,
)
from novel_agent.domain.artifacts import RootKind
from novel_agent.domain.changes import (
    ChangeOperation,
    ChangeOperationType,
    ChapterChangeDraft,
    EvidenceRepairAction,
    EvidenceRepairDraft,
    ObservedChangeSet,
)
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory_write import (
    CandidateProducerKind,
    CandidateRevision,
    RepairAction,
    RepairDirective,
    RepairScope,
)
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.stage2 import AgentMode, AgentType, CuratorEvidenceContract
from novel_agent.ports.memory_write import CuratorRepairRequest, CuratorRepairResult
from novel_agent.services.model_curation import ModelCurationContractError
from tests.contract.test_stage2_contract import agent_receipt
from tests.factories import SCHEMA_VERSION, make_artifact
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

NOW = datetime(2026, 7, 23, tzinfo=UTC)
WORLD = make_synthetic_bundle().world_roots[0]
TEXT = make_synthetic_bundle().text_roots[1]


def _operation(
    operation_id: str = "operation.parent",
    target: str = "obligation.synthetic.north-tower",
) -> ChangeOperation:
    return ChangeOperation(
        operation_id=StableId(operation_id),
        root_kind=RootKind.WORLD,
        operation=ChangeOperationType.REPLACE,
        target_id=StableId(target),
        payload={
            "record_type": "obligation",
            "record": WORLD.obligations[0].model_dump(mode="json"),
        },
    )


def _changes(*operations: ChangeOperation) -> ObservedChangeSet:
    return ObservedChangeSet(
        change_set_id=StableId(
            "changes." + (operations[0].operation_id.root if operations else "empty")
        ),
        base_commit=WORLD.source_commit,
        source_artifact=make_artifact("9"),
        operations=operations,
    )


def _parent() -> CandidateRevision:
    return CandidateRevision(
        candidate_id=StableId("candidate.curator-repair"),
        revision_no=1,
        base_commit=WORLD.source_commit,
        basis_hash=ArtifactId("sha256:" + "a" * 64),
        candidate_artifact=make_artifact("b"),
        producer_kind=CandidateProducerKind.CURATOR_PROPOSE,
        content_hash=ArtifactId("sha256:" + "c" * 64),
        created_at=NOW,
    )


def _directive(
    *,
    action: RepairAction = RepairAction.CURATOR_REPAIR,
    operation_ids: tuple[StableId, ...] = (),
    identity_rebind: bool = False,
) -> RepairDirective:
    return RepairDirective(
        directive_id=StableId("directive.curator-repair"),
        action=action,
        operation_ids=operation_ids,
        allowed_scope=RepairScope(
            operation_ids=operation_ids,
            allow_identity_rebind=identity_rebind,
        ),
    )


def _request(
    parent: CandidateRevision | None = None,
    *,
    base: Any = None,
) -> CuratorRepairRequest:
    return cast(
        CuratorRepairRequest,
        SimpleNamespace(
            parent_candidate=parent or _parent(),
            basis=SimpleNamespace(commit_id=base or WORLD.source_commit),
        ),
    )


def test_parent_candidate_base_and_action_are_immutable() -> None:
    parent = _parent()
    with pytest.raises(CuratorRepairContractError, match="parent candidate"):
        CuratorRepairAgent._validate_parent(
            _request(),
            parent.model_copy(update={"content_hash": ArtifactId("sha256:" + "d" * 64)}),
            _directive(),
            WORLD.source_commit,
        )
    with pytest.raises(CuratorRepairContractError, match="base commit"):
        CuratorRepairAgent._validate_parent(
            _request(base=type(WORLD.source_commit)("sha256:" + "2" * 64)),
            parent,
            _directive(),
            WORLD.source_commit,
        )
    with pytest.raises(CuratorRepairContractError, match="curator-repair"):
        CuratorRepairAgent._validate_parent(
            _request(), parent, _directive(action=RepairAction.HUMAN), WORLD.source_commit
        )


def test_scoped_repair_rejects_targets_outside_selected_operations() -> None:
    parent = _parent()
    parent_operation = _operation()
    directive = _directive(operation_ids=(parent_operation.operation_id,))
    with pytest.raises(CuratorRepairContractError, match="immutable parent target"):
        CuratorRepairAgent._validate_scope(
            _changes(_operation("operation.new", "obligation.synthetic.other")),
            parent,
            _changes(parent_operation),
            directive,
        )


def test_scoped_repair_accepts_only_selected_operation_targets() -> None:
    parent = _parent()
    parent_operation = _operation()
    directive = _directive(operation_ids=(parent_operation.operation_id,))
    CuratorRepairAgent._validate_scope(
        _changes(_operation("operation.repaired", parent_operation.target_id.root)),
        parent,
        _changes(parent_operation),
        directive,
    )


def test_unscoped_repair_cannot_introduce_identity_without_authority() -> None:
    parent = _parent()
    with pytest.raises(CuratorRepairContractError, match="immutable parent target"):
        CuratorRepairAgent._validate_scope(
            _changes(_operation("operation.new", "obligation.synthetic.other")),
            parent,
            _changes(_operation()),
            _directive(),
        )
    CuratorRepairAgent._validate_scope(
        _changes(_operation("operation.new", "obligation.synthetic.other")),
        parent,
        _changes(_operation()),
        _directive(identity_rebind=True),
    )


def test_prompt_constraints_expose_exact_parent_targets_and_scope() -> None:
    operation = _operation()
    directive = RepairDirective(
        directive_id=StableId("directive.evidence-only"),
        action=RepairAction.CURATOR_REPAIR,
        operation_ids=(operation.operation_id,),
        allowed_scope=RepairScope(
            operation_ids=(operation.operation_id,),
            field_paths=("evidence_refs", "record.evidence_refs"),
        ),
    )

    constraints = CuratorRepairAgent._prompt_constraints(_changes(operation), directive)

    assert constraints["immutable_target_ids"] == (operation.target_id.root,)
    assert constraints["allowed_field_paths"] == (
        "evidence_refs",
        "record.evidence_refs",
    )
    assert constraints["operations"] == (
        {
            "operation_id": operation.operation_id.root,
            "target_id": operation.target_id.root,
            "operation_type": operation.operation.value,
            "selected_for_repair": True,
        },
    )
    identity_rebind = CuratorRepairAgent._prompt_constraints(
        _changes(operation),
        _directive(identity_rebind=True),
    )
    assert identity_rebind["immutable_target_ids"] == ()
    other = _operation("operation.other", "obligation.synthetic.other")
    partially_scoped = CuratorRepairAgent._prompt_constraints(
        _changes(operation, other),
        directive,
    )
    scoped_operations = cast(tuple[dict[str, Any], ...], partially_scoped["operations"])
    assert scoped_operations[1]["selected_for_repair"] is False
    assert CuratorRepairAgent._payload_without_evidence("raw") == "raw"
    assert CuratorRepairAgent._payload_without_evidence({"record": "raw"}) == {"record": "raw"}


def test_evidence_only_scope_rejects_record_or_operation_type_changes() -> None:
    parent_operation = _operation()
    parent_changes = _changes(parent_operation)
    evidence_only = RepairDirective(
        directive_id=StableId("directive.evidence-only"),
        action=RepairAction.CURATOR_REPAIR,
        allowed_scope=RepairScope(
            field_paths=("evidence_refs", "record.evidence_refs"),
        ),
    )
    parent_payload = cast(dict[str, Any], parent_operation.payload)
    parent_record = cast(dict[str, Any], parent_payload["record"])
    changed_record = parent_operation.model_copy(
        update={
            "operation_id": StableId("operation.changed-record"),
            "payload": {
                **parent_payload,
                "record": {
                    **parent_record,
                    "description": "unauthorized semantic rewrite",
                },
            },
        }
    )
    changed_type = parent_operation.model_copy(
        update={
            "operation_id": StableId("operation.changed-type"),
            "operation": ChangeOperationType.CREATE,
        }
    )

    with pytest.raises(CuratorRepairContractError, match="immutable record"):
        CuratorRepairAgent._validate_scope(
            _changes(changed_record),
            _parent(),
            parent_changes,
            evidence_only,
        )
    with pytest.raises(CuratorRepairContractError, match="operation type"):
        CuratorRepairAgent._validate_scope(
            _changes(changed_type),
            _parent(),
            parent_changes,
            evidence_only,
        )
    CuratorRepairAgent._validate_scope(
        parent_changes,
        _parent(),
        parent_changes,
        evidence_only,
    )
    CuratorRepairAgent._validate_scope(
        _changes(changed_type),
        _parent(),
        parent_changes,
        RepairDirective(
            directive_id=StableId("directive.operation-type-change"),
            action=RepairAction.CURATOR_REPAIR,
            allowed_scope=RepairScope(allow_operation_type_change=True),
        ),
    )
    CuratorRepairAgent._validate_scope(
        parent_changes,
        _parent(),
        parent_changes,
        RepairDirective(
            directive_id=StableId("directive.record-value"),
            action=RepairAction.CURATOR_REPAIR,
            allowed_scope=RepairScope(field_paths=("record.value",)),
        ),
    )


def test_identity_rebind_still_respects_selected_operation_scope() -> None:
    parent_operation = _operation()
    with pytest.raises(CuratorRepairContractError, match="outside"):
        CuratorRepairAgent._validate_scope(
            _changes(_operation("operation.other", "obligation.synthetic.other")),
            _parent(),
            _changes(parent_operation),
            RepairDirective(
                directive_id=StableId("directive.scoped-identity-rebind"),
                action=RepairAction.CURATOR_REPAIR,
                operation_ids=(parent_operation.operation_id,),
                allowed_scope=RepairScope(
                    operation_ids=(parent_operation.operation_id,),
                    allow_identity_rebind=True,
                ),
            ),
        )


def test_repair_output_cannot_change_base_commit() -> None:
    changed = _changes(_operation()).model_copy(
        update={"base_commit": type(WORLD.source_commit)("sha256:" + "2" * 64)}
    )
    with pytest.raises(CuratorRepairContractError, match="base commit"):
        CuratorRepairAgent._validate_scope(changed, _parent(), _changes(_operation()), _directive())


def test_successful_facade_binds_artifacts_and_directive() -> None:
    parent = _parent()
    parent_changes = _changes(_operation())
    directive = _directive()
    repaired = _changes(_operation("operation.repaired", "obligation.synthetic.north-tower"))

    class Runner:
        def prepare(self, *_: object, **__: object) -> Any:
            return SimpleNamespace(
                request=SimpleNamespace(),
                rendered_prompt="rendered",
            )

        def receipt(self, *_: object, **__: object) -> Any:
            return agent_receipt().model_copy(
                update={
                    "agent_type": AgentType.MEMORY_CURATOR,
                    "agent_mode": AgentMode.CURATOR_REPAIR,
                }
            )

    class Curator:
        async def extract_reported(self, *_: object, **__: object) -> tuple[Any, ...]:
            return repaired, SimpleNamespace(), SimpleNamespace(unresolved=())

    result = asyncio.run(
        CuratorRepairAgent(
            cast(Any, Curator()),
            cast(Any, Runner()),
            evidence_contract=CuratorEvidenceContract.LEGACY_OFFSET_V1,
        ).run(
            version=SCHEMA_VERSION,
            text_root=TEXT,
            chapter_index=23,
            base_commit=WORLD.source_commit,
            current_world=WORLD,
            parent_candidate=parent,
            parent_changes=parent_changes,
            validation=None,
            directive=directive,
            request=_request(parent),
            model_request=cast(ModelRequest, SimpleNamespace()),
        )
    )

    assert isinstance(result, CuratorRepairResult)
    assert result.observed_changes == repaired
    assert result.applied_directive_ids == (directive.directive_id,)
    assert result.candidate_artifact is not None
    assert result.producer_receipt is not None


@pytest.mark.parametrize(
    ("error_kind", "reason_code"),
    (
        ("schema", "CURATOR_REPAIR_SCHEMA_REJECTED"),
        ("domain", "CURATOR_REPAIR_DOMAIN_REJECTED"),
    ),
)
def test_facade_maps_model_contract_failures_to_retryable_rejection(
    error_kind: str,
    reason_code: str,
) -> None:
    parent = _parent()
    parent_changes = _changes(_operation())
    directive = _directive()
    if error_kind == "schema":
        try:
            ChapterChangeDraft.model_validate({"chapter_index": 0})
        except ValidationError as error:
            failure: Exception = error
        else:  # pragma: no cover - the invalid fixture must stay invalid
            raise AssertionError("invalid ChapterChangeDraft unexpectedly validated")
    else:
        failure = ModelCurationContractError("duplicate repair targets")

    class Runner:
        def prepare(self, *_: object, **__: object) -> Any:
            return SimpleNamespace(
                request=SimpleNamespace(),
                rendered_prompt="rendered",
            )

    class Curator:
        async def extract_reported(self, *_: object, **__: object) -> tuple[Any, ...]:
            raise failure

    with pytest.raises(CuratorRepairContractError) as caught:
        asyncio.run(
            CuratorRepairAgent(
                cast(Any, Curator()),
                cast(Any, Runner()),
                evidence_contract=CuratorEvidenceContract.LEGACY_OFFSET_V1,
            ).run(
                version=SCHEMA_VERSION,
                text_root=TEXT,
                chapter_index=23,
                base_commit=WORLD.source_commit,
                current_world=WORLD,
                parent_candidate=parent,
                parent_changes=parent_changes,
                validation=None,
                directive=directive,
                request=_request(parent),
                model_request=cast(ModelRequest, SimpleNamespace()),
            )
        )

    assert caught.value.reason_code == reason_code


def test_evidence_contract_property_returns_configured_contract() -> None:
    agent = CuratorRepairAgent(
        cast(Any, None), cast(Any, None), evidence_contract=CuratorEvidenceContract.CANDIDATE_ID_V2
    )
    assert agent.evidence_contract is CuratorEvidenceContract.CANDIDATE_ID_V2
    legacy = CuratorRepairAgent(
        cast(Any, None), cast(Any, None), evidence_contract=CuratorEvidenceContract.LEGACY_OFFSET_V1
    )
    assert legacy.evidence_contract is CuratorEvidenceContract.LEGACY_OFFSET_V1


def test_v2_facade_runs_evidence_only_repair_and_binds_artifacts() -> None:
    parent = _parent()
    parent_changes = _changes(_operation())
    directive = _directive()
    repaired = _changes(_operation("operation.repaired", "obligation.synthetic.north-tower"))
    repair_draft = EvidenceRepairDraft(
        operation_index=0,
        replacement_candidate_ids=(),
        action=EvidenceRepairAction.MARK_UNRESOLVED,
    )

    class Runner:
        def prepare(self, *_: object, **__: object) -> Any:
            return SimpleNamespace(
                request=SimpleNamespace(),
                rendered_prompt="rendered",
            )

        def receipt(self, *_: object, **__: object) -> Any:
            return agent_receipt().model_copy(
                update={
                    "agent_type": AgentType.MEMORY_CURATOR,
                    "agent_mode": AgentMode.CURATOR_REPAIR,
                }
            )

    class Curator:
        async def evidence_repair_v2(self, *_: object, **__: object) -> tuple[Any, ...]:
            return repaired, SimpleNamespace(), (repair_draft,)

    result = asyncio.run(
        CuratorRepairAgent(
            cast(Any, Curator()),
            cast(Any, Runner()),
            evidence_contract=CuratorEvidenceContract.CANDIDATE_ID_V2,
        ).run(
            version=SCHEMA_VERSION,
            text_root=TEXT,
            chapter_index=23,
            base_commit=WORLD.source_commit,
            current_world=WORLD,
            parent_candidate=parent,
            parent_changes=parent_changes,
            validation=None,
            directive=directive,
            request=_request(parent),
            model_request=cast(ModelRequest, SimpleNamespace()),
        )
    )

    assert isinstance(result, CuratorRepairResult)
    assert result.observed_changes == repaired
    assert result.applied_directive_ids == (directive.directive_id,)
    assert result.candidate_artifact is not None
    assert result.producer_receipt is not None


def test_v2_facade_scopes_repair_to_directed_operation_ids() -> None:
    parent = _parent()
    parent_operation = _operation()
    parent_changes = _changes(parent_operation)
    directive = _directive(operation_ids=(parent_operation.operation_id,))
    repaired = _changes(_operation("operation.repaired", parent_operation.target_id.root))

    class Runner:
        def prepare(self, *_: object, **__: object) -> Any:
            return SimpleNamespace(
                request=SimpleNamespace(),
                rendered_prompt="rendered",
            )

        def receipt(self, *_: object, **__: object) -> Any:
            return agent_receipt().model_copy(
                update={
                    "agent_type": AgentType.MEMORY_CURATOR,
                    "agent_mode": AgentMode.CURATOR_REPAIR,
                }
            )

    captured: dict[str, Any] = {}

    class Curator:
        async def evidence_repair_v2(self, *_args: object, **kwargs: object) -> tuple[Any, ...]:
            captured["repair_operation_indexes"] = kwargs.get("repair_operation_indexes")
            captured["contract_prompt"] = kwargs.get("contract_prompt")
            return repaired, SimpleNamespace(), ()

    result = asyncio.run(
        CuratorRepairAgent(
            cast(Any, Curator()),
            cast(Any, Runner()),
            evidence_contract=CuratorEvidenceContract.CANDIDATE_ID_V2,
        ).run(
            version=SCHEMA_VERSION,
            text_root=TEXT,
            chapter_index=23,
            base_commit=WORLD.source_commit,
            current_world=WORLD,
            parent_candidate=parent,
            parent_changes=parent_changes,
            validation=None,
            directive=directive,
            request=_request(parent),
            model_request=cast(ModelRequest, SimpleNamespace()),
        )
    )

    assert isinstance(result, CuratorRepairResult)
    assert captured["repair_operation_indexes"] == (0,)
    assert captured["contract_prompt"] == "rendered"


def test_v2_facade_maps_domain_rejection_to_retryable_rejection() -> None:
    parent = _parent()
    parent_changes = _changes(_operation())
    directive = _directive()

    class Runner:
        def prepare(self, *_: object, **__: object) -> Any:
            return SimpleNamespace(
                request=SimpleNamespace(),
                rendered_prompt="rendered",
            )

    class Curator:
        async def evidence_repair_v2(self, *_: object, **__: object) -> tuple[Any, ...]:
            raise ModelCurationContractError("unknown evidence candidate")

    with pytest.raises(CuratorRepairContractError) as caught:
        asyncio.run(
            CuratorRepairAgent(
                cast(Any, Curator()),
                cast(Any, Runner()),
                evidence_contract=CuratorEvidenceContract.CANDIDATE_ID_V2,
            ).run(
                version=SCHEMA_VERSION,
                text_root=TEXT,
                chapter_index=23,
                base_commit=WORLD.source_commit,
                current_world=WORLD,
                parent_candidate=parent,
                parent_changes=parent_changes,
                validation=None,
                directive=directive,
                request=_request(parent),
                model_request=cast(ModelRequest, SimpleNamespace()),
            )
        )

    assert caught.value.reason_code == "CURATOR_REPAIR_DOMAIN_REJECTED"


def test_validate_evidence_only_rejects_base_commit_change() -> None:
    parent = _parent()
    parent_changes = _changes(_operation())
    changed = parent_changes.model_copy(
        update={"base_commit": type(WORLD.source_commit)("sha256:" + "9" * 64)}
    )
    with pytest.raises(CuratorRepairContractError, match="base commit"):
        CuratorRepairAgent._validate_evidence_only(changed, parent_changes, parent, _directive())


def test_validate_evidence_only_rejects_new_target() -> None:
    parent = _parent()
    parent_changes = _changes(_operation())
    new_target = _changes(_operation("operation.new", "obligation.synthetic.other"))
    with pytest.raises(CuratorRepairContractError, match="new target"):
        CuratorRepairAgent._validate_evidence_only(new_target, parent_changes, parent, _directive())


def test_validate_evidence_only_rejects_changed_record_content() -> None:
    parent = _parent()
    parent_operation = _operation()
    parent_changes = _changes(parent_operation)
    parent_payload = cast(dict[str, Any], parent_operation.payload)
    parent_record = cast(dict[str, Any], parent_payload["record"])
    changed_operation = parent_operation.model_copy(
        update={
            "operation_id": StableId("operation.changed-record"),
            "payload": {
                **parent_payload,
                "record": {**parent_record, "description": "unauthorized rewrite"},
            },
        }
    )
    with pytest.raises(CuratorRepairContractError, match="immutable record"):
        CuratorRepairAgent._validate_evidence_only(
            _changes(changed_operation), parent_changes, parent, _directive()
        )


def test_validate_evidence_only_accepts_unchanged_payload() -> None:
    parent = _parent()
    parent_changes = _changes(_operation())
    CuratorRepairAgent._validate_evidence_only(parent_changes, parent_changes, parent, _directive())
