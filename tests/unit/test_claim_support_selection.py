from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    NeedRisk,
    RequirementLevel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
)
from novel_agent.domain.model_calls import (
    ModelRole,
    ModelUsage,
    ProviderModelResult,
)
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    ContextAssemblyStatus,
    EvidenceResolutionStatus,
    SemanticSupportStatus,
)
from novel_agent.services.claim_support import (
    ControllerSupportSelector,
    SemanticSupportBatch,
    SupportSelectionResult,
    TrustedClaimSupportProducer,
)
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.model_gateway import (
    ModelGateway,
    RegisteredModelEndpoint,
)
from novel_agent.services.task_conditioned_need_generation import (
    TaskPlanConditionedNeedGenerator,
)
from novel_agent.services.writer_context_assembler import WriterContextAssembler
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


class _SemanticSupportEndpoint:
    is_external = False
    model = "semantic-support-test"
    max_retries = 0

    def __init__(self, payloads: tuple[dict[str, object] | Exception, ...]) -> None:
        self.payloads = list(payloads)

    async def generate(self, _request: object) -> ProviderModelResult:
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return ProviderModelResult(
            text=json.dumps(payload, ensure_ascii=False),
            model_version=self.model,
            usage=ModelUsage(
                input_tokens=10,
                output_tokens=10,
                cost_usd=Decimal("0"),
            ),
        )


def _support_gateway(*payloads: dict[str, object] | Exception) -> ModelGateway:
    endpoint = _SemanticSupportEndpoint(payloads)
    return ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="semantic-support-test",
                model_name=endpoint.model,
                adapter=endpoint,
            ),
        )
    )


def _selection() -> tuple[
    BenchmarkTaskContract,
    Stage1MemoryNeed,
    RetrievalUnit,
    WriterContextAssembler,
    SupportSelectionResult,
]:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    case = bundle.case_manifests[0]
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    needs = TaskPlanConditionedNeedGenerator().generate(task, world, None)
    capability = next(item for item in needs if item.need_type == "capability_boundary").model_copy(
        update={
            "requirement": RequirementLevel.MANDATORY,
            "risk_level": NeedRisk.HIGH,
        }
    )
    evidence = world.states[0].evidence_refs[0]
    unit = RetrievalUnit(
        unit_id=StableId("anchor.test.capability"),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=world.source_commit,
        snapshot_id=StableId("snapshot.test.support"),
        text="林澈当前能力可用, 但受伤时无法持续, 存在明确限制。",
        entity_ids=capability.entity_ids,
        access_scope=capability.access_scope,
        evidence_refs=(evidence,),
        support_status="supported",
    )
    assembler = WriterContextAssembler()
    selection = ControllerSupportSelector().select(
        task=task,
        units=(unit,),
        needs=(capability,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (capability.need_id,)},
        writer_token_budget=4000,
        evidence_ledger_token_budget=12_000,
        token_counter=assembler.count_tokens,
    )
    return task, capability, unit, assembler, selection


def test_controller_outputs_receipt_bound_groups_and_closes_irreducible_facets() -> None:
    _task, capability, _unit, _assembler, selection = _selection()

    assert selection.support_groups
    assert selection.claim_variants
    assert all(
        group.semantic_support_status is SemanticSupportStatus.VERIFIED
        for group in selection.support_groups
    )
    assert capability.completion_spec is not None
    assert selection.context_assembly_spec.closed_need_facet_ids == (
        capability.completion_spec.required_need_facet_ids
    )
    assert selection.context_assembly_spec.unresolved_need_facet_ids == ()
    assert selection.context_assembly_spec.mandatory_support_group_ids


def test_assembler_rejects_missing_receipt_and_tampered_claim_variant() -> None:
    task, capability, unit, assembler, selection = _selection()
    common: Any = dict(
        task=task,
        assembly_spec=selection.context_assembly_spec,
        support_groups=selection.support_groups,
        cutoff_attestations=selection.cutoff_attestations,
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        arm="A",
    )
    missing = assembler.assemble_from_spec(
        **common,
        claim_variants=selection.claim_variants,
        support_receipts=(),
    )
    assert missing.status is ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
    assert any(code.startswith("SUPPORT_RECEIPT_INVALID") for code in missing.diagnostic_codes)

    tampered = selection.claim_variants[0].model_copy(
        update={"claim_text": selection.claim_variants[0].claim_text + " 篡改"}
    )
    invalid = assembler.assemble_from_spec(
        **common,
        claim_variants=(tampered, *selection.claim_variants[1:]),
        support_receipts=selection.support_receipts,
    )
    assert invalid.status is ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
    assert any(code.startswith("CLAIM_VARIANT_INVALID") for code in invalid.diagnostic_codes)


def test_evidence_resolution_does_not_imply_semantic_support() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    groups, _variants, _receipts, _attestations = TrustedClaimSupportProducer().produce(
        task=task,
        units=(unit.model_copy(update={"support_status": "unsupported"}),),
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (capability.need_id,)},
        token_counter=assembler.count_tokens,
    )

    assert groups
    assert groups[0].evidence_resolution_status is EvidenceResolutionStatus.RESOLVED
    assert groups[0].semantic_support_status is SemanticSupportStatus.UNVERIFIED


def test_historical_evidence_commit_is_valid_under_exact_current_snapshot() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    historical_evidence = unit.evidence_refs[0].model_copy(
        update={"resolved_at_commit": CommitId("sha256:" + "9" * 64)}
    )
    historical_unit = unit.model_copy(update={"evidence_refs": (historical_evidence,)})

    groups, _variants, _receipts, _attestations = TrustedClaimSupportProducer().produce(
        task=task,
        units=(historical_unit,),
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (capability.need_id,)},
        token_counter=assembler.count_tokens,
    )

    assert groups
    assert groups[0].evidence_resolution_status is EvidenceResolutionStatus.RESOLVED


def test_author_planning_need_can_consume_writer_safe_evidence() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    author_task = task.model_copy(
        update={"information_profile": BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED}
    )
    author_need = capability.model_copy(update={"access_scope": "author_planning"})

    groups, _variants, _receipts, _attestations = TrustedClaimSupportProducer().produce(
        task=author_task,
        units=(unit,),
        needs=(author_need,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (author_need.need_id,)},
        token_counter=assembler.count_tokens,
    )

    assert groups
    assert groups[0].semantic_support_status is SemanticSupportStatus.VERIFIED


def test_open_obligation_facets_close_from_relevant_observed_state() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    case = bundle.case_manifests[0]
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    obligation = next(
        item
        for item in TaskPlanConditionedNeedGenerator().generate(task, world, None)
        if item.need_type == "unresolved_obligation"
    )
    unit = RetrievalUnit(
        unit_id=StableId("anchor.test.observed-obligation-state"),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=world.source_commit,
        snapshot_id=StableId("snapshot.test.observed-obligation"),
        text='林澈 destination "north_tower"',
        entity_ids=obligation.entity_ids,
        access_scope="writer_safe",
        evidence_refs=(world.obligations[0].evidence_refs[0],),
        information_label="observed",
        support_status="supported",
    )

    groups, _variants, _receipts, _attestations = TrustedClaimSupportProducer().produce(
        task=task,
        units=(unit,),
        needs=(obligation,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (obligation.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )

    assert groups
    assert groups[0].need_facet_ids == tuple(
        facet.need_facet_id for facet in obligation.need_facets
    )
    assert groups[0].plan_node_ids == ()


def test_grounded_claim_extraction_remains_deterministic_and_narrow() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    case = bundle.case_manifests[0]
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    history_need = next(
        item
        for item in TaskPlanConditionedNeedGenerator().generate(task, world, None)
        if item.need_type == "capability_history"
    )
    text = "林澈走入庭院。林澈负伤后仍挡在同伴身前。林澈随后休息。"
    evidence = world.states[0].evidence_refs[0]
    assert evidence.span is not None
    unit = RetrievalUnit(
        unit_id=StableId("grounded.test.semantic-passage"),
        unit_kind=RetrievalUnitKind.GROUNDED_BLOCK,
        source_commit=world.source_commit,
        snapshot_id=StableId("snapshot.test.semantic-passage"),
        text=text,
        entity_ids=history_need.entity_ids,
        access_scope="writer_safe",
        evidence_refs=(
            evidence.model_copy(
                update={"span": evidence.span.model_copy(update={"start": 0, "end": len(text)})}
            ),
        ),
        information_label="observed",
        support_status="supported",
    )

    groups, variants, _receipts, _attestations = TrustedClaimSupportProducer().produce(
        task=task,
        units=(unit,),
        needs=(history_need,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (history_need.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )

    assert groups
    assert any("挡在同伴身前" in item.claim_text for item in variants)
    assert all(len(item.claim_text) <= 240 for item in variants)


def test_semantic_support_is_bound_to_public_ids_and_model_receipt() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    facet_ids = tuple(
        item.need_facet_id
        for item in capability.need_facets
        if capability.completion_spec is not None
        and item.need_facet_id in capability.completion_spec.required_need_facet_ids
    )
    gateway = _support_gateway(
        {
            "claims": [
                {
                    "need_id": capability.need_id.root,
                    "need_facet_ids": [item.root for item in facet_ids],
                    "retrieval_unit_ids": [unit.unit_id.root],
                    "claim_text": "林澈当前能力受伤势限制, 无法稳定发挥完整战力。",
                }
            ],
            "insufficient_need_ids": [],
        },
        {
            "decisions": [
                {
                    "claim_index": 0,
                    "supports": True,
                    "counter_evidence_retrieval_unit_ids": [],
                }
            ]
        },
    )

    _groups, variants, receipts, _attestations = TrustedClaimSupportProducer(
        semantic_gateway=gateway
    ).produce(
        task=task,
        units=(unit,),
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (capability.need_id,)},
        token_counter=assembler.count_tokens,
    )

    semantic_receipts = tuple(item for item in receipts if item.model_call_record is not None)
    assert len(semantic_receipts) == 1
    receipt = semantic_receipts[0]
    assert receipt.producer_input_hash is not None
    assert receipt.producer_output_hash is not None
    assert receipt.verifier_input_hash is not None
    assert receipt.verifier_output_hash is not None
    assert receipt.verification_model_call_record is not None
    assert receipt.need_facet_ids == facet_ids
    with pytest.raises(ValidationError, match="independent verification call"):
        type(receipt).model_validate(
            receipt.model_dump()
            | {
                "verifier_input_hash": None,
                "verifier_output_hash": None,
                "verifier_input_ref": None,
                "verifier_output_ref": None,
                "verification_model_call_record": None,
            }
        )
    with pytest.raises(ValidationError, match="hashes must match retained artifacts"):
        type(receipt).model_validate(
            receipt.model_dump() | {"producer_input_hash": ArtifactId("sha256:" + "0" * 64)}
        )
    assert any(
        item.claim_text == "林澈当前能力受伤势限制, 无法稳定发挥完整战力。"
        and item.support_receipt_ref.artifact_id
        == TrustedClaimSupportProducer._artifact_ref(
            receipt,
            "application/vnd.novel-agent.claim-support-receipt+json",
        ).artifact_id
        for item in variants
    )


def test_semantic_support_rejects_ids_outside_public_need_binding() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    gateway = _support_gateway(
        {
            "claims": [
                {
                    "need_id": capability.need_id.root,
                    "need_facet_ids": [capability.need_facets[0].need_facet_id.root],
                    "retrieval_unit_ids": ["unit.not-in-public-input"],
                    "claim_text": "不得被接受的越界结论。",
                }
            ],
            "insufficient_need_ids": [],
        }
    )

    _groups, variants, receipts, _attestations = TrustedClaimSupportProducer(
        semantic_gateway=gateway
    ).produce(
        task=task,
        units=(unit,),
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (capability.need_id,)},
        token_counter=assembler.count_tokens,
    )

    assert not any(item.model_call_record is not None for item in receipts)
    assert all(item.claim_text != "不得被接受的越界结论。" for item in variants)


def test_mandatory_support_group_is_atomic_under_tiny_budget() -> None:
    task, capability, unit, assembler, selection = _selection()
    tiny_spec = selection.context_assembly_spec.model_copy(
        update={"token_budget": 1, "writer_token_budget": 1}
    )
    result = assembler.assemble_from_spec(
        task=task,
        assembly_spec=tiny_spec,
        support_groups=selection.support_groups,
        claim_variants=selection.claim_variants,
        support_receipts=selection.support_receipts,
        cutoff_attestations=selection.cutoff_attestations,
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        arm="A",
    )

    assert result.status is ContextAssemblyStatus.CONTEXT_BUDGET_INSUFFICIENT
    assert result.package.continuity_constraints
    assert all(item.mandatory for item in result.package.continuity_constraints)
    assert result.evidence_ledger.entries


def test_claim_support_static_resolution_and_callback_edges() -> None:
    task, capability, unit, _assembler, _selection_result = _selection()
    producer = TrustedClaimSupportProducer()
    assert producer._legal_for_need(
        task,
        capability,
        unit,
    )
    assert not producer._legal_for_need(
        task,
        capability.model_copy(update={"access_scope": "unknown"}),
        unit,
    )
    plan_unit = unit.model_copy(
        update={
            "unit_kind": RetrievalUnitKind.PLAN_ANCHOR,
            "information_label": "plan",
            "evidence_refs": (),
        }
    )
    assert not producer._legal_for_need(task, capability, plan_unit)
    assert producer._plan_node_ids(plan_unit)
    assert (
        producer._resolution_status(
            (),
            plan_unit.model_copy(update={"source_commit": CommitId("sha256:" + "7" * 64)}),
            basis_commit_id=unit.source_commit,
            checkpoint_chapter=task.checkpoint_chapter,
            plan_node_ids=producer._plan_node_ids(plan_unit),
        )
        is EvidenceResolutionStatus.BASIS_MISMATCH
    )

    grounded_without_span = unit.model_copy(
        update={
            "unit_kind": RetrievalUnitKind.GROUNDED_BLOCK,
            "evidence_refs": (unit.evidence_refs[0].model_copy(update={"span": None}),),
        }
    )
    assert producer._claim_candidates(grounded_without_span, capability) == ()
    whitespace_grounded = grounded_without_span.model_copy(
        update={
            "text": "   \n林澈受伤。",
            "evidence_refs": unit.evidence_refs,
        }
    )
    assert producer._claim_candidates(whitespace_grounded, capability)
    assert not producer._supported_facets(
        capability.model_copy(update={"entity_ids": ()}),
        unit.model_copy(update={"entity_ids": ()}),
        "完全无关",
    )
    assert (
        producer._resolution_status(
            (),
            unit,
            basis_commit_id=unit.source_commit,
            checkpoint_chapter=task.checkpoint_chapter,
            plan_node_ids=(),
        )
        is EvidenceResolutionStatus.UNRESOLVED
    )
    assert (
        producer._resolution_status(
            unit.evidence_refs,
            unit.model_copy(update={"source_commit": CommitId("sha256:" + "9" * 64)}),
            basis_commit_id=unit.source_commit,
            checkpoint_chapter=task.checkpoint_chapter,
            plan_node_ids=(),
        )
        is EvidenceResolutionStatus.BASIS_MISMATCH
    )
    future_ref = unit.evidence_refs[0].model_copy(update={"chapter_id": StableId("chapter.999")})
    assert (
        producer._resolution_status(
            (future_ref,),
            unit,
            basis_commit_id=unit.source_commit,
            checkpoint_chapter=task.checkpoint_chapter,
            plan_node_ids=(),
        )
        is EvidenceResolutionStatus.CUTOFF_VIOLATION
    )
    assert producer._chapter_number(StableId("chapter.prelude")) == 0
    assert producer._chapter_number(StableId("chapter.unknown")) is None
    assert "the" not in producer._query_terms("the gate and 林澈受伤")

    long_unit = unit.model_copy(update={"text": "无关句。" * 200 + "林澈受伤后能力受限。"})
    assert len(producer._semantic_excerpt(capability, long_unit)) <= 600

    retained: list[tuple[bytes, str]] = []
    progress: list[object] = []

    def write_artifact(payload: bytes, media_type: str) -> ArtifactRef:
        retained.append((payload, media_type))
        return ArtifactRef(
            artifact_id=ArtifactId("sha256:" + "8" * 64),
            media_type=media_type,
            byte_length=len(payload),
            schema_version=SchemaVersion("1.0.0"),
        )

    callbacks = TrustedClaimSupportProducer(
        artifact_writer=write_artifact,
        progress_writer=progress.append,
    )
    assert callbacks._retain_bytes(b"payload", "text/plain").byte_length == 7
    callbacks._record_progress(stage="test")
    assert retained and progress == [{"stage": "test"}]

    unsupported_evidence_free = unit.model_copy(update={"evidence_refs": ()})
    groups, *_rest = producer.produce(
        task=task,
        units=(unsupported_evidence_free,),
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (capability.need_id,)},
        token_counter=lambda _value: 1,
    )
    assert groups == ()


def test_semantic_producer_returns_empty_when_no_unit_is_legally_bound() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    producer = TrustedClaimSupportProducer(
        semantic_gateway=_support_gateway({"claims": [], "insufficient_need_ids": []})
    )
    result = producer.produce(
        task=task,
        units=(unit,),
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (StableId("need.unknown"),)},
        token_counter=assembler.count_tokens,
    )
    assert result == ((), (), (), ())


def test_semantic_producer_caches_valid_batches_and_reports_incomplete_needs() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    facets = tuple(facet.need_facet_id.root for facet in capability.need_facets)
    gateway = _support_gateway(
        {
            "claims": [
                {
                    "need_id": capability.need_id.root,
                    "need_facet_ids": facets,
                    "retrieval_unit_ids": [unit.unit_id.root],
                    "claim_text": "林澈当前能力受伤势限制。",
                }
            ],
            "insufficient_need_ids": [],
        },
        {"decisions": [{"claim_index": 0, "supports": True}]},
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=gateway)
    kwargs: Any = {
        "task": task,
        "units": (unit,),
        "needs": (capability,),
        "basis_commit_id": unit.source_commit,
        "basis_snapshot_id": unit.snapshot_id,
        "unit_need_ids": {unit.unit_id: (capability.need_id,)},
        "token_counter": assembler.count_tokens,
    }
    first = producer.produce(**kwargs)
    second = producer.produce(**kwargs)
    assert first == second

    incomplete = TrustedClaimSupportProducer(
        semantic_gateway=_support_gateway({"claims": [], "insufficient_need_ids": []})
    )
    incomplete.produce(**kwargs)
    assert "SEMANTIC_SUPPORT_INCOMPLETE_NEED_COVERAGE" in incomplete.last_diagnostic_codes


class _DiscardingResponses(dict[str, str]):
    def __init__(self, *, discard_verification_only: bool = False) -> None:
        super().__init__()
        self.discard_verification_only = discard_verification_only

    def __setitem__(self, key: str, value: str) -> None:
        if not self.discard_verification_only or key.startswith("support-verification."):
            return
        super().__setitem__(key, value)


def test_semantic_producer_reports_missing_raw_outputs() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    facets = [facet.need_facet_id.root for facet in capability.need_facets]
    proposal: dict[str, object] = {
        "claims": [
            {
                "need_id": capability.need_id.root,
                "need_facet_ids": facets,
                "retrieval_unit_ids": [unit.unit_id.root],
                "claim_text": "林澈当前能力受伤势限制。",
            }
        ],
        "insufficient_need_ids": [],
    }
    kwargs: Any = {
        "task": task,
        "units": (unit,),
        "needs": (capability,),
        "basis_commit_id": unit.source_commit,
        "basis_snapshot_id": unit.snapshot_id,
        "unit_need_ids": {unit.unit_id: (capability.need_id,)},
        "token_counter": assembler.count_tokens,
    }
    proposal_gateway = _support_gateway(proposal)
    proposal_gateway.raw_responses = _DiscardingResponses()
    proposal_producer = TrustedClaimSupportProducer(semantic_gateway=proposal_gateway)
    proposal_producer.produce(**kwargs)
    assert "SEMANTIC_SUPPORT_RAW_OUTPUT_MISSING" in proposal_producer.last_diagnostic_codes

    verification_gateway = _support_gateway(
        proposal,
        {"decisions": [{"claim_index": 0, "supports": True}]},
    )
    verification_gateway.raw_responses = _DiscardingResponses(discard_verification_only=True)
    verification_producer = TrustedClaimSupportProducer(semantic_gateway=verification_gateway)
    verification_producer.produce(**kwargs)
    assert (
        "SEMANTIC_SUPPORT_VERIFIER_RAW_OUTPUT_MISSING"
        in verification_producer.last_diagnostic_codes
    )


def test_semantic_producer_records_proposal_and_verifier_failures() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    proposal: dict[str, object] = {
        "claims": [
            {
                "need_id": capability.need_id.root,
                "need_facet_ids": [facet.need_facet_id.root for facet in capability.need_facets],
                "retrieval_unit_ids": [unit.unit_id.root],
                "claim_text": "林澈当前能力受伤势限制。",
            }
        ],
        "insufficient_need_ids": [],
    }
    kwargs: Any = {
        "task": task,
        "units": (unit,),
        "needs": (capability,),
        "basis_commit_id": unit.source_commit,
        "basis_snapshot_id": unit.snapshot_id,
        "unit_need_ids": {unit.unit_id: (capability.need_id,)},
        "token_counter": assembler.count_tokens,
    }
    progress: list[object] = []
    proposal_failure = TrustedClaimSupportProducer(
        semantic_gateway=_support_gateway(RuntimeError("proposal failed")),
        progress_writer=progress.append,
    )
    proposal_failure.produce(**kwargs)
    assert "SEMANTIC_SUPPORT_PRODUCER_RUNTIMEERROR" in (proposal_failure.last_diagnostic_codes)
    assert progress[-1] == {
        "stage": "proposal",
        "batch_index": 1,
        "status": "failed",
        "error_type": "RuntimeError",
    }

    verifier_failure = TrustedClaimSupportProducer(
        semantic_gateway=_support_gateway(proposal, RuntimeError("verification failed"))
    )
    verifier_failure.produce(**kwargs)
    assert "SEMANTIC_SUPPORT_VERIFIER_RUNTIMEERROR" in (verifier_failure.last_diagnostic_codes)


def test_semantic_producer_rejects_invalid_verifier_decision_shapes() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    proposal: dict[str, object] = {
        "claims": [
            {
                "need_id": capability.need_id.root,
                "need_facet_ids": [facet.need_facet_id.root for facet in capability.need_facets],
                "retrieval_unit_ids": [unit.unit_id.root],
                "claim_text": "林澈当前能力受伤势限制。",
            }
        ],
        "insufficient_need_ids": [],
    }
    kwargs: Any = {
        "task": task,
        "units": (unit,),
        "needs": (capability,),
        "basis_commit_id": unit.source_commit,
        "basis_snapshot_id": unit.snapshot_id,
        "unit_need_ids": {unit.unit_id: (capability.need_id,)},
        "token_counter": assembler.count_tokens,
    }
    decisions: tuple[dict[str, object], ...] = (
        {"decisions": []},
        {"decisions": [{"claim_index": 7, "supports": True}]},
        {
            "decisions": [
                {
                    "claim_index": 0,
                    "supports": False,
                    "counter_evidence_retrieval_unit_ids": ["unit.outside"],
                }
            ]
        },
        {"decisions": [{"claim_index": 0, "supports": False}]},
    )
    diagnostic_sets: list[tuple[str, ...]] = []
    for decision in decisions:
        producer = TrustedClaimSupportProducer(
            semantic_gateway=_support_gateway(proposal, decision)
        )
        _groups, variants, receipts, _attestations = producer.produce(**kwargs)
        diagnostic_sets.append(producer.last_diagnostic_codes)
        assert not any(item.model_call_record is not None for item in receipts)
        assert all(item.claim_text != "林澈当前能力受伤势限制。" for item in variants)
    assert all(
        "SEMANTIC_SUPPORT_VERIFIER_INCOMPLETE_DECISIONS" in codes for codes in diagnostic_sets[:3]
    )
    assert diagnostic_sets[3] == ()


def test_semantic_producer_deduplicates_claims_and_rejects_unresolved_sources() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    draft = {
        "need_id": capability.need_id.root,
        "need_facet_ids": [facet.need_facet_id.root for facet in capability.need_facets],
        "retrieval_unit_ids": [unit.unit_id.root],
        "claim_text": "林澈当前能力受伤势限制。",
    }
    duplicate_proposal: dict[str, object] = {
        "claims": [draft, draft],
        "insufficient_need_ids": [],
    }
    duplicate_verification: dict[str, object] = {
        "decisions": [
            {"claim_index": 0, "supports": True},
            {"claim_index": 1, "supports": True},
        ]
    }
    kwargs: Any = {
        "task": task,
        "units": (unit,),
        "needs": (capability,),
        "basis_commit_id": unit.source_commit,
        "basis_snapshot_id": unit.snapshot_id,
        "unit_need_ids": {unit.unit_id: (capability.need_id,)},
        "token_counter": assembler.count_tokens,
    }
    _groups, variants, receipts, _attestations = TrustedClaimSupportProducer(
        semantic_gateway=_support_gateway(duplicate_proposal, duplicate_verification)
    ).produce(**kwargs)
    assert sum(item.model_call_record is not None for item in receipts) == 1
    assert sum(item.claim_text == draft["claim_text"] for item in variants) == 1

    future_ref = unit.evidence_refs[0].model_copy(update={"chapter_id": StableId("chapter.999")})
    unresolved_unit = unit.model_copy(update={"evidence_refs": (future_ref,)})
    unresolved_kwargs = kwargs | {
        "units": (unresolved_unit,),
        "unit_need_ids": {unresolved_unit.unit_id: (capability.need_id,)},
    }
    _groups, variants, receipts, _attestations = TrustedClaimSupportProducer(
        semantic_gateway=_support_gateway(
            {
                "claims": [draft],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    ).produce(**unresolved_kwargs)
    assert not any(item.model_call_record is not None for item in receipts)
    assert all(item.claim_text != draft["claim_text"] for item in variants)


def test_selector_records_no_completion_and_mandatory_unclosed_need() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    no_completion = capability.model_copy(update={"completion_spec": None})
    selector = ControllerSupportSelector()
    result = selector.select(
        task=task,
        units=(unit.model_copy(update={"support_status": "contradicted"}),),
        needs=(no_completion, capability),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (no_completion.need_id, capability.need_id)},
        writer_token_budget=4000,
        evidence_ledger_token_budget=12_000,
        token_counter=assembler.count_tokens,
    )
    assert result.context_assembly_spec.unresolved_need_facet_ids
    assert any(code.startswith("MANDATORY_FACET_UNCLOSED:") for code in result.diagnostic_codes)


def test_selector_skips_group_with_no_marginal_facet_progress() -> None:
    task, capability, unit, assembler, baseline = _selection()
    assert capability.completion_spec is not None
    first_group = baseline.support_groups[0]
    first_variant = baseline.claim_variants[0]
    second_group_id = StableId("support-group.no-marginal-progress")
    second_group = first_group.model_copy(update={"support_group_id": second_group_id})
    second_variant = first_variant.model_copy(
        update={
            "claim_variant_id": StableId("claim-variant.no-marginal-progress"),
            "support_group_id": second_group_id,
        }
    )

    class FixedProducer(TrustedClaimSupportProducer):
        def produce(self, **_kwargs: Any) -> Any:
            return (
                (first_group, second_group),
                (first_variant, second_variant),
                (),
                (),
            )

    impossible_facet = StableId("need-facet.required-but-unavailable")
    incomplete_need = capability.model_copy(
        update={
            "completion_spec": capability.completion_spec.model_copy(
                update={
                    "required_need_facet_ids": (
                        *capability.completion_spec.required_need_facet_ids,
                        impossible_facet,
                    )
                }
            )
        }
    )
    result = ControllerSupportSelector(FixedProducer()).select(
        task=task,
        units=(unit,),
        needs=(incomplete_need,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (incomplete_need.need_id,)},
        writer_token_budget=4000,
        evidence_ledger_token_budget=12_000,
        token_counter=assembler.count_tokens,
    )
    assert result.context_assembly_spec.ordered_optional_support_group_ids == (second_group_id,)
    assert impossible_facet in result.context_assembly_spec.unresolved_need_facet_ids


def test_semantic_batch_contract_allows_explicit_insufficient_need() -> None:
    batch = SemanticSupportBatch(
        claims=(),
        insufficient_need_ids=(StableId("need.explicitly-insufficient"),),
    )
    assert batch.insufficient_need_ids
