from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    NeedFacetKind,
    NeedRisk,
    RequirementLevel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.model_calls import (
    ModelRequest,
    ModelRole,
    ModelUsage,
    ProviderModelResult,
)
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    ClaimSupportGroup,
    ClaimSupportReceipt,
    ClaimVariant,
    ContextAssemblyStatus,
    CutoffAttestation,
    EvidenceResolutionStatus,
    SemanticSupportStatus,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.claim_support import (
    _MULTI_SLICE_PROMPT_TEMPLATE,
    SEMANTIC_SUPPORT_MULTI_SLICE_PROPOSAL_MAX_OUTPUT_TOKENS,
    SEMANTIC_SUPPORT_MULTI_SLICE_THINKING_TOKEN_BUDGET,
    ControllerSupportSelector,
    EvidenceSlice,
    SupportSelectionResult,
    TrustedClaimSupportProducer,
)
from novel_agent.services.content_addressing import quote_hash
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

    def __init__(
        self,
        payloads: tuple[dict[str, object] | str | Exception, ...],
    ) -> None:
        self.payloads: list[dict[str, object] | str | Exception] = list(payloads)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.requests.append(request)
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


class _RawTextEndpoint(_SemanticSupportEndpoint):
    """Endpoint that returns the payload text verbatim (already JSON-encoded)."""

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.requests.append(request)
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return ProviderModelResult(
            text=str(payload),
            model_version=self.model,
            usage=ModelUsage(
                input_tokens=10,
                output_tokens=10,
                cost_usd=Decimal("0"),
            ),
        )


def _support_gateway(*payloads: dict[str, object] | Exception) -> ModelGateway:
    endpoint = _SemanticSupportEndpoint(payloads)
    return _gateway_for_endpoint(endpoint)


def _gateway_for_endpoint(endpoint: _SemanticSupportEndpoint) -> ModelGateway:
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


def test_semantic_stage_thinking_configuration() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.46.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="thinking.config",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_ids": [_slice_id_for(unit_a)],
                        "claim_text": "徐有容身在南方。",
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint)).produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    probe, multi, verifier = endpoint.requests
    assert probe.enable_thinking is False
    assert probe.thinking_token_budget is None
    assert multi.enable_thinking is True
    assert multi.thinking_token_budget == SEMANTIC_SUPPORT_MULTI_SLICE_THINKING_TOKEN_BUDGET
    assert verifier.enable_thinking is False
    assert verifier.thinking_token_budget is None


def test_semantic_proposal_and_verifier_use_stage_specific_timeouts() -> None:
    task, capability, unit, assembler, _selection_result = _grounded_selection()
    slice_id = _slice_id_for(unit)
    proposal: dict[str, object] = {
        "claims": [
            {
                "need_id": capability.need_id.root,
                "need_facet_ids": _atom_required_facets(capability),
                "slice_unit_id": slice_id,
                "claim_text": "林澈当前能力受伤势限制。",
                "single_slice_sufficient": True,
            }
        ],
        "insufficient_need_ids": [],
    }
    endpoint = _SemanticSupportEndpoint(
        (proposal, {"decisions": [{"claim_index": 0, "supports": True}]})
    )
    TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint)).produce(
        task=task,
        units=(unit,),
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (capability.need_id,)},
        token_counter=assembler.count_tokens,
    )

    assert [request.timeout_seconds for request in endpoint.requests] == [300.0, 120.0]


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


def _grounded_selection() -> tuple[
    BenchmarkTaskContract,
    Stage1MemoryNeed,
    RetrievalUnit,
    WriterContextAssembler,
    SupportSelectionResult,
]:
    task, capability, _unit, assembler, _selection_result = _selection()
    text = "林澈当前能力可用, 但受伤时无法持续, 存在明确限制。"
    base = _unit.evidence_refs[0]
    evidence = base.model_copy(
        update={
            "evidence_id": StableId("evidence.full.test.capability"),
            "object_hash": sha256_id(text.encode("utf-8")),
            "chapter_id": StableId("chapter.test.5"),
            "span": (
                cast(Any, base.span).model_copy(update={"start": 0, "end": len(text)})
                if base.span is not None
                else None
            ),
            "quote_hash": quote_hash(text),
        }
    )
    grounded = RetrievalUnit(
        unit_id=StableId("grounded.block.test.capability"),
        unit_kind=RetrievalUnitKind.GROUNDED_BLOCK,
        source_commit=_unit.source_commit,
        snapshot_id=_unit.snapshot_id,
        text=text,
        entity_ids=capability.entity_ids,
        access_scope=capability.access_scope,
        evidence_refs=(evidence,),
        support_status="supported",
    )
    return task, capability, grounded, assembler, _selection_result


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
    planning_need = capability.model_copy(
        update={
            "access_scope": "author_planning",
            "allow_plan": True,
        }
    )
    groups, _variants, _receipts, _attestations = TrustedClaimSupportProducer().produce(
        task=task,
        units=(unit,),
        needs=(planning_need,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (planning_need.need_id,)},
        token_counter=assembler.count_tokens,
    )
    assert any(group.need_ids == (planning_need.need_id,) for group in groups)


def test_open_obligation_facets_close_from_relevant_observed_state() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    obligation = capability.model_copy(
        update={
            "need_type": "unresolved_obligation",
            "query_intent": Stage1QueryIntent.KNOWN_ID,
            "need_facets": tuple(
                facet.model_copy(
                    update={
                        "facet_kind": NeedFacetKind.UNRESOLVED_STATUS,
                        "expected_claim_scope": facet.expected_claim_scope,
                    }
                )
                for facet in capability.need_facets
            ),
        }
    )
    current_state = unit.model_copy(
        update={
            "unit_id": StableId("anchor.current.fulfillment"),
            "text": "林澈已经履行了承诺, 状态已闭合。",
            "unit_kind": RetrievalUnitKind.STATE_ANCHOR,
        }
    )
    groups, _variants, _receipts, _attestations = TrustedClaimSupportProducer().produce(
        task=task,
        units=(current_state,),
        needs=(obligation,),
        basis_commit_id=current_state.source_commit,
        basis_snapshot_id=current_state.snapshot_id,
        unit_need_ids={current_state.unit_id: (obligation.need_id,)},
        token_counter=assembler.count_tokens,
    )
    assert any(group.need_ids == (obligation.need_id,) for group in groups)


def test_grounded_claim_extraction_remains_deterministic_and_narrow() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    grounded = unit.model_copy(
        update={
            "unit_id": StableId("grounded.test.capability"),
            "unit_kind": RetrievalUnitKind.GROUNDED_BLOCK,
            "text": (
                "林澈当前能力可用。\n"
                "但受伤时无法持续, 存在明确限制, 这是第二句。\n"
                "第三句完全不相关, 用于验证窄化。"
            ),
        }
    )
    base = unit.evidence_refs[0]
    refs = tuple(
        base.model_copy(
            update={
                "evidence_id": StableId(f"evidence.full.test.{index}"),
                "span": base.span,
                "quote_hash": quote_hash(grounded.text),
            }
        )
        for index in range(1)
    )
    grounded = grounded.model_copy(update={"evidence_refs": refs})

    groups, variants, _receipts, _attestations = TrustedClaimSupportProducer().produce(
        task=task,
        units=(grounded,),
        needs=(capability,),
        basis_commit_id=grounded.source_commit,
        basis_snapshot_id=grounded.snapshot_id,
        unit_need_ids={grounded.unit_id: (capability.need_id,)},
        token_counter=assembler.count_tokens,
    )
    assert groups
    assert all(variant.claim_text != "第三句完全不相关, 用于验证窄化。" for variant in variants)


def test_selector_prefers_verified_semantic_group_over_equal_facet_fallback() -> None:
    _task, _capability, _unit, _assembler, selection = _selection()
    assert selection.support_groups
    assert all(
        group.semantic_support_status is SemanticSupportStatus.VERIFIED
        for group in selection.support_groups
    )


def test_selector_preserves_optional_completion_groups_for_assembly() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    optional_capability = capability.model_copy(update={"requirement": RequirementLevel.OPTIONAL})
    selection = ControllerSupportSelector().select(
        task=task,
        units=(unit,),
        needs=(optional_capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (optional_capability.need_id,)},
        writer_token_budget=4000,
        evidence_ledger_token_budget=12_000,
        token_counter=assembler.count_tokens,
    )
    assert selection.support_groups
    assert selection.context_assembly_spec.ordered_optional_support_group_ids


def test_semantic_support_rejects_ids_outside_public_need_binding() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    other = StableId("need.stage2m.other")
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [
                    {
                        "need_id": other.root,
                        "need_facet_ids": _atom_required_facets(capability),
                        "slice_unit_id": _slice_id_for(unit),
                        "claim_text": "错误 need 的 claim。",
                        "single_slice_sufficient": True,
                    }
                ],
                "insufficient_need_ids": [],
            },
        )
    )
    _groups, variants, _receipts, _attestations = TrustedClaimSupportProducer(
        semantic_gateway=_gateway_for_endpoint(endpoint)
    ).produce(
        task=task,
        units=(unit,),
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (capability.need_id,)},
        token_counter=assembler.count_tokens,
    )
    assert not any(item.claim_text == "错误 need 的 claim。" for item in variants)


def test_mandatory_support_group_is_atomic_under_tiny_budget() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    selection = ControllerSupportSelector().select(
        task=task,
        units=(unit,),
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (capability.need_id,)},
        writer_token_budget=8,
        evidence_ledger_token_budget=12_000,
        token_counter=assembler.count_tokens,
    )
    assert selection.context_assembly_spec.mandatory_support_group_ids


def test_claim_support_static_resolution_and_callback_edges() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    producer = TrustedClaimSupportProducer()
    plan_unit = RetrievalUnit(
        unit_id=StableId("anchor.plan.node-1"),
        unit_kind=RetrievalUnitKind.PLAN_ANCHOR,
        source_commit=unit.source_commit,
        snapshot_id=unit.snapshot_id,
        text="chapter 21 陈长生离开西宁镇",
        access_scope="author_planning",
        information_label="plan",
    )
    assert producer._legal_for_need(task, capability, unit)
    assert not producer._legal_for_need(task, capability, plan_unit)
    assert producer._plan_node_ids(plan_unit)
    grounded_without_span = unit.model_copy(
        update={
            "unit_kind": RetrievalUnitKind.GROUNDED_BLOCK,
            "evidence_refs": (unit.evidence_refs[0].model_copy(update={"span": None}),),
        }
    )
    assert producer._claim_candidates(grounded_without_span, capability) == ()
    whitespace_grounded = grounded_without_span.model_copy(
        update={
            "text": "   \n  ",
            "evidence_refs": (unit.evidence_refs[0],),
        }
    )
    assert not producer._claim_candidates(whitespace_grounded, capability)
    historical_need = capability.model_copy(
        update={
            "need_type": "causal_history",
            "query_intent": Stage1QueryIntent.SEMANTIC_HISTORY,
            "need_facets": tuple(
                facet.model_copy(
                    update={
                        "facet_kind": NeedFacetKind.CAUSAL_HISTORY,
                        "expected_claim_scope": facet.expected_claim_scope,
                    }
                )
                for facet in capability.need_facets
            ),
        }
    )
    historical_grounded = unit.model_copy(
        update={
            "unit_kind": RetrievalUnitKind.GROUNDED_BLOCK,
            "text": "黑龙首次现身于第 56 章, 陈长生与它对峙。",
            "evidence_refs": (),
        }
    )
    assert producer._supported_facets(historical_need, historical_grounded, "黑龙对峙")
    assert producer._chapter_number(StableId("chapter.prelude")) == 0
    assert producer._chapter_number(StableId("chapter.unknown")) is None
    assert producer._chapter_index(unit.model_copy(update={"evidence_refs": ()})) is None
    assert (
        producer._resolution_status(
            unit.evidence_refs,
            unit,
            basis_commit_id=unit.source_commit,
            checkpoint_chapter=20,
            plan_node_ids=(),
        )
        is EvidenceResolutionStatus.RESOLVED
    )
    assert "the" not in producer._query_terms("the gate and 林澈受伤")

    class _Callbacks:
        def __init__(self) -> None:
            self.written: list[ArtifactRef] = []
            self.progress: list[dict[str, object]] = []

        def write(self, payload: bytes, media_type: str) -> ArtifactRef:
            ref = ArtifactRef(
                artifact_id=sha256_id(payload),
                media_type=media_type,
                byte_length=len(payload),
                schema_version=SchemaVersion("1.0.0"),
            )
            self.written.append(ref)
            return ref

        def record(self, event: Mapping[str, object]) -> None:
            self.progress.append(dict(event))

    callbacks = _Callbacks()
    with_progress = TrustedClaimSupportProducer(
        artifact_writer=callbacks.write,
        progress_writer=callbacks.record,
    )
    with_progress._record_progress(stage="test")
    assert with_progress._retain_bytes(b"payload", "text/plain").byte_length == 7
    assert callbacks.written
    assert callbacks.progress == [{"stage": "test"}]
    assert with_progress.produce(
        task=task,
        units=(),
        needs=(),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={},
        token_counter=assembler.count_tokens,
    ) == ((), (), (), ())


def test_semantic_producer_returns_empty_when_no_unit_is_legally_bound() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    groups, _variants, _receipts, _attestations = TrustedClaimSupportProducer(
        semantic_gateway=_support_gateway()
    ).produce(
        task=task,
        units=(unit,),
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={},
        token_counter=assembler.count_tokens,
    )
    assert groups == ()


def test_selector_records_no_completion_and_mandatory_unclosed_need() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    need = capability.model_copy(update={"requirement": RequirementLevel.MANDATORY})
    selection = ControllerSupportSelector().select(
        task=task,
        units=(),
        needs=(need,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={},
        writer_token_budget=4000,
        evidence_ledger_token_budget=12_000,
        token_counter=assembler.count_tokens,
    )
    assert selection.support_groups == ()
    assert any(code.startswith("MANDATORY_FACET_UNCLOSED") for code in selection.diagnostic_codes)


def test_selector_skips_group_with_no_marginal_facet_progress() -> None:
    task, capability, unit, assembler, _selection_result = _selection()
    selection = ControllerSupportSelector().select(
        task=task,
        units=(unit,),
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (capability.need_id,)},
        writer_token_budget=4000,
        evidence_ledger_token_budget=12_000,
        token_counter=assembler.count_tokens,
    )
    assert selection.support_groups


def test_evidence_ref_covered_requires_precise_overlapping_spans() -> None:
    from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef

    base = EvidenceRef(
        evidence_id=StableId("evidence.test.one"),
        root_hash=ArtifactId("sha256:" + "1" * 64),
        object_hash=ArtifactId("sha256:" + "2" * 64),
        chapter_id=StableId("chapter.test.1"),
        span=TextSpanRef(block_id=StableId("block.test.1"), start=0, end=40),
        quote_hash=quote_hash("x" * 40),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=CommitId("sha256:" + "3" * 64),
    )
    contained = base.model_copy(
        update={
            "evidence_id": StableId("evidence.test.contained"),
            "span": TextSpanRef(block_id=StableId("block.test.1"), start=0, end=20),
            "quote_hash": quote_hash("x" * 20),
        }
    )
    disjoint = base.model_copy(
        update={
            "evidence_id": StableId("evidence.test.disjoint"),
            "span": TextSpanRef(block_id=StableId("block.test.1"), start=50, end=60),
            "quote_hash": quote_hash("y" * 10),
        }
    )
    assert TrustedClaimSupportProducer._evidence_ref_covered(contained, (base,))
    assert not TrustedClaimSupportProducer._evidence_ref_covered(disjoint, (base,))


def test_evidence_ref_covered_subspan_never_covers_parent_block() -> None:
    from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef

    parent = EvidenceRef(
        evidence_id=StableId("evidence.full.test.block"),
        root_hash=ArtifactId("sha256:" + "1" * 64),
        object_hash=ArtifactId("sha256:" + "2" * 64),
        chapter_id=StableId("chapter.test.1"),
        span=TextSpanRef(block_id=StableId("block.test.1"), start=0, end=400),
        quote_hash=quote_hash("x" * 400),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=CommitId("sha256:" + "3" * 64),
    )
    sub = parent.model_copy(
        update={
            "evidence_id": StableId("evidence.curator.sub"),
            "span": TextSpanRef(block_id=StableId("block.test.1"), start=0, end=20),
            "quote_hash": quote_hash("x" * 20),
        }
    )
    assert not TrustedClaimSupportProducer._evidence_ref_covered(parent, (sub,))


def test_evidence_ref_covered_spanless_reference() -> None:
    from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus

    spanless = EvidenceRef(
        evidence_id=StableId("evidence.test.spanless"),
        root_hash=ArtifactId("sha256:" + "1" * 64),
        object_hash=ArtifactId("sha256:" + "2" * 64),
        chapter_id=StableId("chapter.test.1"),
        span=None,
        quote_hash=quote_hash("x" * 10),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=CommitId("sha256:" + "3" * 64),
    )
    assert not TrustedClaimSupportProducer._evidence_ref_covered(
        spanless, (spanless.model_copy(update={"evidence_id": StableId("evidence.test.other")}),)
    )


def _cross_need_task() -> tuple[BenchmarkTaskContract, WorldRootDocument]:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    case = bundle.case_manifests[0]
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    return task, world


def _generated_need(
    task: BenchmarkTaskContract,
    world: WorldRootDocument,
    need_type: str,
) -> Stage1MemoryNeed:
    return next(
        item
        for item in TaskPlanConditionedNeedGenerator().generate(task, world, None)
        if item.need_type == need_type
    )


def _remap_need(need: Stage1MemoryNeed, new_need_id: StableId) -> Stage1MemoryNeed:
    suffix = new_need_id.root.rsplit(".", 1)[-1]
    facet_ids = {
        facet.need_facet_id: StableId(f"need-facet.test.{suffix}.{index}")
        for index, facet in enumerate(need.need_facets)
    }
    new_facets = tuple(
        facet.model_copy(
            update={
                "need_facet_id": facet_ids[facet.need_facet_id],
                "need_id": new_need_id,
            }
        )
        for facet in need.need_facets
    )
    assert need.completion_spec is not None
    new_spec = need.completion_spec.model_copy(
        update={
            "need_id": new_need_id,
            "required_need_facet_ids": tuple(
                facet_ids[facet_id] for facet_id in need.completion_spec.required_need_facet_ids
            ),
            "irreducible_need_facet_ids": tuple(
                facet_ids[facet_id] for facet_id in need.completion_spec.irreducible_need_facet_ids
            ),
            "evidence_requirement_by_facet": {
                facet_ids[
                    facet.need_facet_id
                ].root: need.completion_spec.evidence_requirement_by_facet[facet.need_facet_id.root]
                for facet in need.need_facets
                if facet.need_facet_id in need.completion_spec.required_need_facet_ids
            },
        }
    )
    return need.model_copy(
        update={
            "need_id": new_need_id,
            "need_facets": new_facets,
            "completion_spec": new_spec,
        }
    )


ENTITY_LIN_CHE = StableId("entity.lin-che")
ENTITY_OTHER = StableId("entity.other")
CROSS_NEED_SNAPSHOT = StableId("snapshot.test.cross-need")


def _unit(
    world: WorldRootDocument,
    *,
    unit_id: str,
    text: str,
    entity_ids: tuple[StableId, ...],
    chapter: int,
    seed: str,
    kind: RetrievalUnitKind = RetrievalUnitKind.GROUNDED_BLOCK,
    span: bool = True,
    source_commit: CommitId | None = None,
    snapshot_id: StableId | None = None,
    access_scope: str = "writer_safe",
    information_label: str = "observed",
    derivation_taint: tuple[str, ...] = (),
    parent_unit_id: StableId | None = None,
    parent_unit_ids: tuple[StableId, ...] = (),
) -> RetrievalUnit:
    base = world.states[0].evidence_refs[0]
    evidence = base.model_copy(
        update={
            "evidence_id": StableId(f"evidence.test.{seed}"),
            "object_hash": sha256_id(text.encode("utf-8")),
            "chapter_id": StableId(f"chapter.test.{chapter}"),
            "span": (
                cast(Any, base.span).model_copy(
                    update={
                        "block_id": StableId(f"block.test.{chapter}"),
                        "start": 0,
                        "end": len(text),
                    }
                )
                if span and base.span is not None
                else None
            ),
            "quote_hash": quote_hash(text),
        }
    )
    return RetrievalUnit(
        unit_id=StableId(unit_id),
        unit_kind=kind,
        source_commit=source_commit or world.source_commit,
        snapshot_id=snapshot_id or CROSS_NEED_SNAPSHOT,
        text=text,
        entity_ids=entity_ids,
        evidence_refs=(evidence,),
        access_scope=access_scope,
        information_label=information_label,
        derivation_taint=derivation_taint,
        parent_unit_id=parent_unit_id,
        parent_unit_ids=parent_unit_ids,
    )


def _json_objects(payload: dict[str, object], key: str) -> list[dict[str, object]]:
    return [cast(dict[str, object], item) for item in cast(list[object], payload[key])]


def _prompt_input(endpoint: _SemanticSupportEndpoint, index: int) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            endpoint.requests[index]
            .prompt.split('<PUBLIC_SUPPORT_INPUT trusted="false">\n', 1)[1]
            .rsplit("\n</PUBLIC_SUPPORT_INPUT>", 1)[0]
        ),
    )


def _unit_id(item: dict[str, object]) -> str:
    retrieval_unit_id = item.get("slice_unit_id")
    assert isinstance(retrieval_unit_id, str)
    return retrieval_unit_id


def _slice_id_for(unit: RetrievalUnit) -> str:
    digest = quote_hash(unit.text).root.removeprefix("sha256:")[:24]
    return f"slice.{unit.unit_id.root}.0.{digest}"


def _atom_required_facets(need: Stage1MemoryNeed) -> list[str]:
    assert need.completion_spec is not None
    return [item.root for item in need.completion_spec.required_need_facet_ids]


def test_exact_slice_resolver_paragraphs_and_spans() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    text = "第一段徐有容身在南方。\n第二段与陈长生并无直接交谈。\n第三段不能声称她此刻的心意。"
    block = _unit(
        world,
        unit_id="grounded.block.test.5.0",
        text=text,
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=5,
        seed="slice.blocks",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (block,),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert len(slices) == 3
    paragraph_offsets = [
        (0, len("第一段徐有容身在南方。")),
        (
            len("第一段徐有容身在南方。\n"),
            len("第一段徐有容身在南方。\n") + len("第二段与陈长生并无直接交谈。"),
        ),
        (
            len("第一段徐有容身在南方。\n第二段与陈长生并无直接交谈。\n"),
            len(text),
        ),
    ]
    for slice_, (start, end) in zip(slices, paragraph_offsets, strict=True):
        assert slice_.text == text[start:end]
        assert slice_.start == start
        assert slice_.end == end
        span = block.evidence_refs[0].span
        assert span is not None
        assert slice_.parent_block_id == span.block_id
        assert slice_.object_hash == block.evidence_refs[0].object_hash
        assert slice_.evidence_ref.span is not None
        assert slice_.evidence_ref.span.start == start
        assert slice_.evidence_ref.span.end == end
        assert slice_.evidence_ref.quote_hash == quote_hash(slice_.text)
        assert slice_.slice_id.root.startswith(f"slice.{block.unit_id.root}.{start}.")
    assert all(not slice_.taint for slice_ in slices)


def test_exact_slice_resolver_short_paragraph_passes_through() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    text = "徐有容身在南方, 与陈长生并无直接交谈, 不能声称她此刻的心意。"
    block = _unit(
        world,
        unit_id="grounded.block.test.6.0",
        text=text,
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=6,
        seed="slice.short",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (block,),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert len(slices) == 1
    assert slices[0].text == text
    assert slices[0].start == 0
    assert slices[0].end == len(text)


def test_exact_slice_resolver_oversized_paragraph_splits_contiguous_windows() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    paragraph = "。".join(f"第{index}句内容非常充实" for index in range(1, 60)) + "。"
    assert len(paragraph) > 300
    block = _unit(
        world,
        unit_id="grounded.block.test.7.0",
        text=paragraph,
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=7,
        seed="slice.oversized",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (block,),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert len(slices) > 1
    joined = "".join(slice_.text for slice_ in slices)
    assert joined == paragraph
    for slice_ in slices:
        assert slice_.text == paragraph[slice_.start : slice_.end]
        assert slice_.end - slice_.start <= 300


def test_exact_slice_resolver_never_concatenates_non_adjacent_sentences() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    text = "第一句徐有容身在南方。第二句陈长生并无交谈。第三句不能声称心意。"
    block = _unit(
        world,
        unit_id="grounded.block.test.8.0",
        text=text,
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=8,
        seed="slice.contiguous",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (block,),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert len(slices) == 1
    assert slices[0].text == text


def test_exact_slice_resolver_rejects_compact_preview_text() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    parent = _unit(
        world,
        unit_id="grounded.block.test.9.0",
        text="整段原文包含多个句子, 用于验证 compact 不能替代原文。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=9,
        seed="slice.parent",
    )
    compact = _unit(
        world,
        unit_id=f"compact.{parent.unit_id.root}",
        text="整段原文包含多个句子",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=9,
        seed="slice.compact",
        parent_unit_id=parent.unit_id,
        parent_unit_ids=(parent.unit_id,),
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (compact,),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert slices == ()


def test_exact_slice_resolver_filters_basis_snapshot_scope_cutoff_and_taint() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    stale_basis = _unit(
        world,
        unit_id="grounded.block.test.stale",
        text="基于旧 commit 的段落。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=2,
        seed="slice.stale",
        source_commit=CommitId("sha256:" + "7" * 64),
    )
    future_chapter = _unit(
        world,
        unit_id="grounded.block.test.future",
        text="第 25 章的段落, 超出 cutoff。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=25,
        seed="slice.future",
    )
    tainted = _unit(
        world,
        unit_id="grounded.block.test.tainted",
        text="带 taint 的段落。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=5,
        seed="slice.tainted",
        derivation_taint=("contradicted",),
    )
    wrong_scope = _unit(
        world,
        unit_id="grounded.block.test.scope",
        text="author_planning 段落。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=5,
        seed="slice.scope",
        access_scope="author_planning",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (stale_basis, future_chapter, tainted, wrong_scope),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert slices == ()


def test_workset_packs_many_short_slices_by_token_budget_not_fixed_count() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    paragraphs = "\n".join(f"第{index}段相关证据" for index in range(30))
    block = _unit(
        world,
        unit_id="grounded.block.test.10.0",
        text=paragraphs,
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=10,
        seed="workset.many",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (block,),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert len(slices) == 30
    workset, dropped, _workset_rows = producer._pack_workset(
        slices,
        need=knowledge,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert len(workset) > 3
    assert len(workset) + dropped == 30


def test_workset_deep_rank_relevant_slice_remains_eligible() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    paragraphs = (
        "\n".join(f"第{index}段无关背景" for index in range(40))
        + "\n徐有容身在南方且不能声称她此刻心意。"
    )
    block = _unit(
        world,
        unit_id="grounded.block.test.11.0",
        text=paragraphs,
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=11,
        seed="workset.deep",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (block,),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert len(slices) == 41
    workset, _dropped, _workset_rows = producer._pack_workset(
        slices,
        need=knowledge,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert any("徐有容身在南方" in slice_.text for slice_ in workset)


def test_workset_chapter_diversity_keeps_each_source() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.12.0",
        text="第五章徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=5,
        seed="diversity.a",
    )
    unit_b = _unit(
        world,
        unit_id="grounded.block.test.56.0",
        text="第五十六章陈长生与徐有容无直接交谈。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=56,
        seed="diversity.b",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (unit_a, unit_b),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=60,
        origin_need_ids={},
    )
    workset, _dropped, _workset_rows = producer._pack_workset(
        slices,
        need=knowledge,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert {slice_.chapter_id for slice_ in workset} == {
        unit_a.evidence_refs[0].chapter_id,
        unit_b.evidence_refs[0].chapter_id,
    }


def test_single_slice_sufficient_claim_is_verified_and_emitted() -> None:
    task, capability, unit, assembler, _selection_result = _grounded_selection()
    slice_id = _slice_id_for(unit)
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [
                    {
                        "need_id": capability.need_id.root,
                        "need_facet_ids": _atom_required_facets(capability),
                        "slice_unit_id": slice_id,
                        "claim_text": "林澈当前能力受伤势限制。",
                        "single_slice_sufficient": True,
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, variants, receipts, _attestations = producer.produce(
        task=task,
        units=(unit,),
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (capability.need_id,)},
        token_counter=assembler.count_tokens,
    )
    verified = tuple(
        group
        for group in groups
        if group.semantic_support_status is SemanticSupportStatus.VERIFIED
        and group.producer.endswith(".single")
    )
    assert len(verified) == 1
    group = verified[0]
    assert group.retrieval_unit_ids == (StableId(slice_id),)
    assert group.evidence_refs == (
        unit.evidence_refs[0].model_copy(
            update={
                "evidence_id": StableId(
                    f"evidence.slice.{unit.unit_id.root}.0."
                    + quote_hash(unit.text).root.removeprefix("sha256:")[:24]
                ),
                "quote_hash": quote_hash(unit.text),
            }
        ),
    )
    receipt = next(item for item in receipts if item.support_group_id == group.support_group_id)
    assert receipt.semantic_support_status is SemanticSupportStatus.VERIFIED
    assert receipt.model_call_record is not None
    variant = next(item for item in variants if item.support_group_id == group.support_group_id)
    assert "林澈当前能力受伤势限制" in variant.claim_text


def test_still_open_need_triggers_multi_slice_synthesis() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.20.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=20,
        seed="multi.a",
    )
    unit_b = _unit(
        world,
        unit_id="grounded.block.test.21.0",
        text="与陈长生并无直接交谈。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="multi.b",
    )
    slice_a = _slice_id_for(unit_a)
    slice_b = _slice_id_for(unit_b)
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_ids": [slice_a, slice_b],
                        "claim_text": "徐有容身在南方且与陈长生并无直接交谈。",
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a, unit_b),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={
            unit_a.unit_id: (knowledge.need_id,),
            unit_b.unit_id: (knowledge.need_id,),
        },
        token_counter=WriterContextAssembler().count_tokens,
    )
    synthesized = tuple(
        group
        for group in groups
        if group.semantic_support_status is SemanticSupportStatus.VERIFIED
        and group.producer.endswith(".synthesized")
    )
    assert len(synthesized) == 1
    group = synthesized[0]
    assert set(group.retrieval_unit_ids) == {StableId(slice_a), StableId(slice_b)}
    assert {reference.evidence_id.root for reference in group.evidence_refs} == {
        f"evidence.slice.{unit_a.unit_id.root}.0."
        + quote_hash(unit_a.text).root.removeprefix("sha256:")[:24],
        f"evidence.slice.{unit_b.unit_id.root}.0."
        + quote_hash(unit_b.text).root.removeprefix("sha256:")[:24],
    }
    assert {reference.object_hash for reference in group.evidence_refs} == {
        unit_a.evidence_refs[0].object_hash,
        unit_b.evidence_refs[0].object_hash,
    }
    assert len(endpoint.requests) == 3
    assert producer.last_funnel.multi_slice_verified == 1


def test_multi_slice_synthesis_may_cite_more_than_three_slices() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    units = tuple(
        _unit(
            world,
            unit_id=f"grounded.block.test.30.{index}",
            text=f"第{index}段相关证据。",
            entity_ids=(ENTITY_LIN_CHE,),
            chapter=10 + index,
            seed=f"many.{index}",
        )
        for index in range(5)
    )
    slice_ids = [_slice_id_for(unit) for unit in units]
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_ids": slice_ids,
                        "claim_text": "五段证据共同支持知识边界结论。",
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=units,
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit.unit_id: (knowledge.need_id,) for unit in units},
        token_counter=WriterContextAssembler().count_tokens,
    )
    synthesized = tuple(group for group in groups if group.producer.endswith(".synthesized"))
    assert len(synthesized) == 1
    assert len(synthesized[0].retrieval_unit_ids) == 5
    assert len(synthesized[0].evidence_refs) == 5


def test_multi_slice_host_rejects_cited_ids_outside_workset() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.40.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="reject.a",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_ids": [
                            _slice_id_for(unit_a),
                            "slice.grounded.block.test.999.0.unknown",
                        ],
                        "claim_text": "引用了池外 slice 的 claim 应被拒绝。",
                    }
                ],
                "insufficient_need_ids": [],
            },
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert not any(group.producer.endswith(".synthesized") for group in groups)
    assert producer.last_funnel.proposals_rejected >= 1


def test_multi_slice_host_rejects_unknown_facet_ids() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.41.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="reject.facets",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": ["need-facet.test.unknown"],
                        "slice_unit_ids": [_slice_id_for(unit_a)],
                        "claim_text": "未知 facet 的 claim 应被拒绝。",
                    }
                ],
                "insufficient_need_ids": [],
            },
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert not any(group.producer.endswith(".synthesized") for group in groups)
    assert producer.last_funnel.proposals_rejected >= 1


def test_single_slice_host_rejects_unknown_facet_ids() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.42.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="reject.single",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": ["need-facet.test.unknown"],
                        "slice_unit_id": _slice_id_for(unit_a),
                        "claim_text": "未知 facet 的单 slice claim。",
                        "single_slice_sufficient": True,
                    }
                ],
                "insufficient_need_ids": [],
            },
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert not any(group.producer.endswith(".single") for group in groups)
    assert producer.last_funnel.proposals_rejected >= 1


@pytest.mark.parametrize(
    "claim_text",
    (
        "”",
        "。",
        "。。。。",
        "  \n\t  ",
        "   徐  ",
        " 你好 ",
        "陈长生",
    ),
)
def test_reject_garbage_claim_rejects_non_semantic_claims(claim_text: str) -> None:
    assert TrustedClaimSupportProducer._reject_garbage_claim(claim_text)


@pytest.mark.parametrize(
    "claim_text",
    (
        "徐有容身在南方。",
        "徐有容身在南方。与陈长生并无直接交谈。",
        "abcd",
        "林澈当前能力受伤势限制。",
    ),
)
def test_reject_garbage_claim_accepts_semantic_claims(claim_text: str) -> None:
    assert not TrustedClaimSupportProducer._reject_garbage_claim(claim_text)


def test_multi_slice_template_front_loads_required_facet_directives() -> None:
    assert "Answer ONLY the required facets' questions" in _MULTI_SLICE_PROMPT_TEMPLATE
    assert (
        "never write a claim about a background or unrelated slice" in _MULTI_SLICE_PROMPT_TEMPLATE
    )
    assert (
        "never claim a slice supports a conclusion it does not contain"
        in _MULTI_SLICE_PROMPT_TEMPLATE
    )
    assert "must not be a verbatim copy of any slice text" in _MULTI_SLICE_PROMPT_TEMPLATE
    assert "must not begin with a chapter title" in _MULTI_SLICE_PROMPT_TEMPLATE
    assert (
        "Preserve all material qualifications, negation, and epistemic scope"
        in _MULTI_SLICE_PROMPT_TEMPLATE
    )
    assert (
        "Return the JSON response EXACTLY in this shape and no other keys"
        in _MULTI_SLICE_PROMPT_TEMPLATE
    )
    first_directive = _MULTI_SLICE_PROMPT_TEMPLATE.index(
        "Answer ONLY the required facets' questions"
    )
    synthesis = _MULTI_SLICE_PROMPT_TEMPLATE.index("Synthesize ONE complete Writer-facing claim")
    assert first_directive < synthesis


def test_producer_version_is_v31() -> None:
    assert TrustedClaimSupportProducer.version == "trusted_claim_support_producer.v31"


def test_single_slice_garbage_claim_rejected_before_verification() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.43.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="garbage.single",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_id": _slice_id_for(unit_a),
                        "claim_text": "。",
                        "single_slice_sufficient": True,
                    }
                ],
                "insufficient_need_ids": [],
            },
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
        )
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        semantic_gateway=_gateway_for_endpoint(endpoint),
        progress_writer=progress.append,
    )
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert not any(group.producer.endswith(".single") for group in groups)
    assert not any(group.producer.endswith(".synthesized") for group in groups)
    assert producer.last_funnel.proposals_rejected >= 1
    rejected = [
        event
        for event in progress
        if event.get("stage") == "proposal_rejected"
        and event.get("reason") == "rejected:garbage_claim"
    ]
    assert len(rejected) == 1
    # The garbage claim consumed no verifier request: only the single-slice
    # probe and the multi-slice synthesis request were made.
    assert len(endpoint.requests) == 2


def test_multi_slice_garbage_claim_rejected_before_verification() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.44.0",
        text="徐有容身在南方。与陈长生并无直接交谈。不能声称她此刻的心意。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="garbage.multi",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_ids": [_slice_id_for(unit_a)],
                        "claim_text": "”",
                    }
                ],
                "insufficient_need_ids": [],
            },
        )
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        semantic_gateway=_gateway_for_endpoint(endpoint),
        progress_writer=progress.append,
    )
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert not any(group.producer.endswith(".synthesized") for group in groups)
    assert producer.last_funnel.proposals_rejected >= 1
    rejected = [
        event
        for event in progress
        if event.get("stage") == "proposal_rejected"
        and event.get("reason") == "rejected:garbage_claim"
    ]
    assert len(rejected) == 1
    # The garbage claim consumed no verifier request: only the single-slice
    # probe and the multi-slice synthesis request were made.
    assert len(endpoint.requests) == 2


def test_whole_verifier_rejects_claim_with_counter_evidence() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.50.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="counter.a",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_ids": [_slice_id_for(unit_a)],
                        "claim_text": "徐有容身在南方。",
                    }
                ],
                "insufficient_need_ids": [],
            },
            {
                "decisions": [
                    {
                        "claim_index": 0,
                        "supports": True,
                        "counter_evidence_retrieval_unit_ids": ["unit.outside"],
                    }
                ]
            },
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert not any(group.producer.endswith(".synthesized") for group in groups)
    assert producer.last_funnel.whole_verifier_rejected >= 1


def test_whole_verifier_missing_or_invalid_decisions_fail_only_the_claim() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.51.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="decisions.a",
    )
    decision_payloads: tuple[dict[str, object], ...] = (
        {"decisions": []},
        {"decisions": [{"claim_index": 7, "supports": True}]},
        {"decisions": [{"claim_index": 0, "supports": False}]},
    )
    for decisions in decision_payloads:
        endpoint = _SemanticSupportEndpoint(
            (
                {
                    "claims": [],
                    "insufficient_need_ids": [knowledge.need_id.root],
                },
                {
                    "claims": [
                        {
                            "need_id": knowledge.need_id.root,
                            "need_facet_ids": _atom_required_facets(knowledge),
                            "slice_unit_ids": [_slice_id_for(unit_a)],
                            "claim_text": "徐有容身在南方。",
                        }
                    ],
                    "insufficient_need_ids": [],
                },
                decisions,
            )
        )
        producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
        groups, _variants, _receipts, _attestations = producer.produce(
            task=task,
            units=(unit_a,),
            needs=(knowledge,),
            basis_commit_id=world.source_commit,
            basis_snapshot_id=CROSS_NEED_SNAPSHOT,
            unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
            token_counter=WriterContextAssembler().count_tokens,
        )
        assert not any(group.producer.endswith(".synthesized") for group in groups)


def test_one_transport_failure_does_not_discard_other_need() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    callback = _generated_need(task, world, "long_range_callback")
    knowledge_unit = _unit(
        world,
        unit_id="grounded.block.test.60.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="iso.knowledge",
    )
    callback_unit = _unit(
        world,
        unit_id="grounded.block.test.61.0",
        text="黑龙于早期章节点出现。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="iso.callback",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            RuntimeError("proposal transport failed"),
            {
                "claims": [
                    {
                        "need_id": callback.need_id.root,
                        "need_facet_ids": _atom_required_facets(callback),
                        "slice_unit_id": _slice_id_for(callback_unit),
                        "claim_text": "黑龙于早期章节点出现。",
                        "single_slice_sufficient": True,
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(knowledge_unit, callback_unit),
        needs=(knowledge, callback),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={
            knowledge_unit.unit_id: (knowledge.need_id,),
            callback_unit.unit_id: (callback.need_id,),
        },
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert producer.last_funnel.proposal_transport_failures >= 1
    assert any(group.need_ids == (callback.need_id,) for group in groups)
    assert not any(group.need_ids == (knowledge.need_id,) for group in groups)


def test_terminal_funnel_state_and_workset_reports() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.70.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="terminal.a",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_id": _slice_id_for(unit_a),
                        "claim_text": "徐有容身在南方。",
                        "single_slice_sufficient": True,
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        semantic_gateway=_gateway_for_endpoint(endpoint),
        progress_writer=progress.append,
    )
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    funnel = producer.last_funnel
    assert funnel.slices_resolved >= 1
    assert funnel.single_slice_proposals == 1
    assert funnel.single_slice_verified == 1
    assert funnel.proposal_requests == 1
    assert funnel.proposal_transport_failures == 0
    terminal = next(item for item in progress if item["stage"] == "terminal")
    assert terminal["state"] == "completed"
    assert producer.last_workset_reports
    report = producer.last_workset_reports[0]
    assert report.need_id == knowledge.need_id
    assert report.dropped_slice_count == 0
    assert report.total_tokens >= 1
    assert any(group.producer.endswith(".single") for group in groups)


def test_terminal_funnel_state_failed_on_transport_failure() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.71.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="terminal.fail",
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        semantic_gateway=_support_gateway(RuntimeError("boom")),
        progress_writer=progress.append,
    )
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    terminal = next(item for item in progress if item["stage"] == "terminal")
    assert terminal["state"] == "failed"
    assert producer.last_funnel.proposal_transport_failures >= 1
    assert groups == ()


def test_raw_slices_retained_in_evidence_ledger_without_claim_identity() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.80.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="ledger.a",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_ids": [_slice_id_for(unit_a)],
                        "claim_text": "徐有容身在南方。",
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    selector = ControllerSupportSelector(producer)
    selection = selector.select(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        writer_token_budget=4000,
        evidence_ledger_token_budget=12_000,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert selection.raw_evidence_ledger_entries
    for entry in selection.raw_evidence_ledger_entries:
        assert entry.support_group_id is None
        assert entry.need_facet_ids == ()
        assert entry.support_receipt_ref is None
        assert entry.evidence_refs
        assert entry.claim_excerpt
    assert selection.workset_reports
    report = selection.workset_reports[0]
    assert report.slice_ids
    raw_slice_ids = {entry.retrieval_unit_ids[0] for entry in selection.raw_evidence_ledger_entries}
    assert raw_slice_ids == {unit_a.unit_id}


def test_assembler_retains_raw_slices_within_ledger_budget() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.81.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="ledger.b",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_ids": [_slice_id_for(unit_a)],
                        "claim_text": "徐有容身在南方。",
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    selector = ControllerSupportSelector(producer)
    selection = selector.select(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        writer_token_budget=4000,
        evidence_ledger_token_budget=12_000,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assembler = WriterContextAssembler()
    assembled = assembler.assemble_from_spec(
        task=task,
        assembly_spec=selection.context_assembly_spec,
        support_groups=selection.support_groups,
        claim_variants=selection.claim_variants,
        support_receipts=selection.support_receipts,
        cutoff_attestations=selection.cutoff_attestations,
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        arm="A",
        raw_evidence_ledger_entries=selection.raw_evidence_ledger_entries,
    )
    raw_entries = tuple(
        entry for entry in assembled.evidence_ledger.entries if entry.support_group_id is None
    )
    assert len(raw_entries) == len(selection.raw_evidence_ledger_entries)
    from novel_agent.services.content_addressing import canonical_json_bytes

    assert assembled.package.evidence_ledger_ref.artifact_id == sha256_id(
        canonical_json_bytes(assembled.evidence_ledger.model_dump(mode="json"))
    )


def test_semantic_support_prompts_treat_unresolved_facet_as_currentness_question() -> None:
    task, capability, unit, assembler, _selection_result = _grounded_selection()
    unresolved_need = capability.model_copy(
        update={
            "need_facets": tuple(
                facet.model_copy(update={"facet_kind": NeedFacetKind.UNRESOLVED_STATUS})
                for facet in capability.need_facets
            )
        }
    )
    current_state = unit.model_copy(
        update={
            "unit_id": StableId("grounded.block.test.relationship"),
            "text": '落落 teacher "陈长生"',
        }
    )
    evidence = unit.evidence_refs[0].model_copy(
        update={
            "object_hash": sha256_id(current_state.text.encode("utf-8")),
            "quote_hash": quote_hash(current_state.text),
        }
    )
    current_state = current_state.model_copy(update={"evidence_refs": (evidence,)})
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [
                    {
                        "need_id": unresolved_need.need_id.root,
                        "need_facet_ids": _atom_required_facets(unresolved_need),
                        "slice_unit_id": _slice_id_for(current_state),
                        "claim_text": "落落希望陈长生成为老师, 该状态仍未决。",
                        "single_slice_sufficient": True,
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": False}]},
            {
                "claims": [
                    {
                        "need_id": unresolved_need.need_id.root,
                        "need_facet_ids": _atom_required_facets(unresolved_need),
                        "slice_unit_ids": [_slice_id_for(current_state)],
                        "claim_text": "落落希望陈长生成为老师, 该状态仍未决。",
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": False}]},
        )
    )
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name=endpoint.model,
                model_name=endpoint.model,
                adapter=endpoint,
            ),
        )
    )

    _groups, variants, receipts, _attestations = TrustedClaimSupportProducer(
        semantic_gateway=gateway
    ).produce(
        task=task,
        units=(current_state,),
        needs=(unresolved_need,),
        basis_commit_id=current_state.source_commit,
        basis_snapshot_id=current_state.snapshot_id,
        unit_need_ids={current_state.unit_id: (unresolved_need.need_id,)},
        token_counter=assembler.count_tokens,
    )

    assert len(endpoint.requests) == 3
    assert endpoint.requests[0].max_output_tokens == 4096
    assert endpoint.requests[1].max_output_tokens == 1024
    assert "coverage question, not an asserted value" in endpoint.requests[0].prompt
    assert "never infer that it remains unresolved from that label alone" in (
        endpoint.requests[0].prompt
    )
    assert "Treat facet kinds as questions to resolve" in endpoint.requests[1].prompt
    assert "earlier plan, wish, or promise override" in endpoint.requests[1].prompt
    assert not any(item.model_call_record is not None for item in receipts)
    assert all(item.claim_text != "落落希望陈长生成为老师, 该状态仍未决。" for item in variants)


def test_selector_prefers_semantic_group_over_deterministic_fallback() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.90.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="prefer.a",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_id": _slice_id_for(unit_a),
                        "claim_text": "徐有容身在南方。",
                        "single_slice_sufficient": True,
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    selection = ControllerSupportSelector(producer).select(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        writer_token_budget=4000,
        evidence_ledger_token_budget=12_000,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert any(
        group.support_group_id in producer.last_funnel.affected_need_ids
        or group.semantic_support_status is SemanticSupportStatus.VERIFIED
        for group in selection.support_groups
    )


def test_semantic_verifier_splits_claims_by_accumulated_context_budget() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    units = tuple(
        _unit(
            world,
            unit_id=f"grounded.block.test.100.{index}",
            text=f"第{index}段相关证据。" * 5,
            entity_ids=(ENTITY_LIN_CHE,),
            chapter=10 + index,
            seed=f"batch.{index}",
        )
        for index in range(3)
    )
    slice_ids = [_slice_id_for(unit) for unit in units]
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_ids": slice_ids,
                        "claim_text": "三段证据共同支持结论。",
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=units,
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit.unit_id: (knowledge.need_id,) for unit in units},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert any(group.producer.endswith(".synthesized") for group in groups)


def test_compatible_pool_rejects_scope_basis_taint_and_origin_violations() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    relationship = _generated_need(task, world, "relationship_emotion")
    direct = _unit(
        world,
        unit_id="unit.test.compat.direct",
        text="陈长生直接支持知识边界的证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=10,
        seed="compat.direct",
    )
    tainted = _unit(
        world,
        unit_id="unit.test.compat.tainted",
        text="带 taint 的兼容证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=11,
        seed="compat.tainted",
        derivation_taint=("contradicted",),
    )
    foreign_basis = _unit(
        world,
        unit_id="unit.test.compat.foreign",
        text="不同 basis 的兼容证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=12,
        seed="compat.foreign",
        source_commit=CommitId("sha256:" + "8" * 64),
    )
    no_origin = _unit(
        world,
        unit_id="unit.test.compat.no-origin",
        text="无 origin 的兼容证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=13,
        seed="compat.no-origin",
    )
    producer = TrustedClaimSupportProducer()
    origin_need_ids = {
        direct.unit_id: (knowledge.need_id,),
        tainted.unit_id: (relationship.need_id,),
        foreign_basis.unit_id: (relationship.need_id,),
    }
    compatible = producer._compatible_support_units(
        task=task,
        target_need=knowledge,
        units=(direct, tainted, foreign_basis, no_origin),
        need_by_id={knowledge.need_id: knowledge, relationship.need_id: relationship},
        units_by_need={
            knowledge.need_id: (direct,),
            relationship.need_id: (tainted, foreign_basis, no_origin),
        },
        origin_need_ids=origin_need_ids,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
    )
    assert compatible == ()


def test_anchorless_target_borrows_exact_lineage_unit() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    direct = _unit(
        world,
        unit_id="unit.test.lineage.direct",
        text="陈长生直接支持知识边界的证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=5,
        seed="lineage.direct",
    )
    child = _unit(
        world,
        unit_id="unit.test.lineage.child",
        text="直接单元的精确子证据。",
        entity_ids=(ENTITY_OTHER,),
        chapter=6,
        seed="lineage.child",
        parent_unit_id=direct.unit_id,
        parent_unit_ids=(direct.unit_id,),
    )
    anchorless_origin = _remap_need(
        _generated_need(task, world, "relationship_emotion"),
        StableId("need.test.lineage-origin"),
    ).model_copy(
        update={
            "entity_ids": (),
            "focus_ids": (),
        }
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root, anchorless_origin.need_id.root],
            },
        )
    )
    TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint)).produce(
        task=task,
        units=(direct, child),
        needs=(knowledge, anchorless_origin),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={
            direct.unit_id: (knowledge.need_id,),
            child.unit_id: (anchorless_origin.need_id,),
        },
        token_counter=WriterContextAssembler().count_tokens,
    )
    target_entry = _json_objects(_prompt_input(endpoint, 0), "needs")[0]
    unit_ids = [_unit_id(item) for item in _json_objects(target_entry, "exact_slices")]
    assert any(item.startswith(f"slice.{child.unit_id.root}.") for item in unit_ids)


def test_saturated_pool_collapses_duplicate_evidence_and_admits_distinct_compatible() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    relationship = _generated_need(task, world, "relationship_emotion")
    direct = tuple(
        _unit(
            world,
            unit_id=f"unit.test.sat.direct.{index:02d}",
            text=f"第{index}段互不相同的直接证据。",
            entity_ids=(ENTITY_LIN_CHE,),
            chapter=index + 1,
            seed=f"sat.direct.{index}",
        )
        for index in range(17)
    )
    duplicates = tuple(
        unit.model_copy(update={"unit_id": StableId(f"unit.test.sat.dup.{index:02d}")})
        for index, unit in enumerate(direct[:3])
    )
    compatible = tuple(
        _unit(
            world,
            unit_id=f"unit.test.sat.compatible.{index}",
            text=f"第{index}段跨 route 的独立兼容证据。",
            entity_ids=(ENTITY_LIN_CHE,),
            chapter=18 + index,
            seed=f"sat.compatible.{index}",
        )
        for index in range(3)
    )
    needs = (knowledge, relationship)
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [
                    knowledge.need_id.root,
                    relationship.need_id.root,
                ],
            },
        )
    )
    TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint)).produce(
        task=task,
        units=(*direct, *duplicates, *compatible),
        needs=needs,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={
            **{unit.unit_id: (knowledge.need_id,) for unit in (*direct, *duplicates)},
            **{unit.unit_id: (relationship.need_id,) for unit in compatible},
        },
        token_counter=WriterContextAssembler().count_tokens,
    )
    all_slice_ids: list[str] = []
    for index, request in enumerate(endpoint.requests):
        if "<PUBLIC_SUPPORT_INPUT" not in request.prompt:
            continue
        payload = _prompt_input(endpoint, index)
        for entry in _json_objects(payload, "needs"):
            if entry["need_id"] == knowledge.need_id.root:
                all_slice_ids.extend(
                    _unit_id(item) for item in _json_objects(entry, "exact_slices")
                )
    unit_ids = list(dict.fromkeys(all_slice_ids))
    assert len(unit_ids) >= 20
    assert all(unit.unit_id.root not in unit_ids for unit in duplicates)
    assert all(
        any(item.startswith(f"slice.{unit.unit_id.root}.") for item in unit_ids)
        for unit in compatible
    )


def test_semantic_pool_ranks_full_passage_refs_before_narrow_fragments() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    anchor = _unit(
        world,
        unit_id="unit.test.fp.anchor",
        text="陈长生与徐有容婚约在即, 徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=5,
        seed="fp.anchor",
    )
    block = _unit(
        world,
        unit_id="unit.test.fp.block",
        text="徐有容身在南方, 与陈长生并无直接交谈, 不能声称她此刻的心意。" * 3,
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=5,
        seed="fp.block",
    )
    other = _unit(
        world,
        unit_id="unit.test.fp.other",
        text="陈长生在学院中与同学讨论婚约之事。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=6,
        seed="fp.other",
    )
    full_ref = anchor.evidence_refs[0].model_copy(
        update={
            "evidence_id": StableId("evidence.full.block.test.56.0"),
            "object_hash": sha256_id(block.text.encode("utf-8")),
        }
    )
    block = block.model_copy(update={"evidence_refs": (full_ref,)})
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
        )
    )
    TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint)).produce(
        task=task,
        units=(anchor, block, other),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={
            anchor.unit_id: (knowledge.need_id,),
            block.unit_id: (knowledge.need_id,),
            other.unit_id: (knowledge.need_id,),
        },
        token_counter=WriterContextAssembler().count_tokens,
    )
    target_entry = _json_objects(_prompt_input(endpoint, 0), "needs")[0]
    unit_ids = [_unit_id(item) for item in _json_objects(target_entry, "exact_slices")]
    assert unit_ids[0].startswith("slice.unit.test.fp.block.")
    assert any(item.startswith("slice.unit.test.fp.other.") for item in unit_ids)
    # The anchor is a narrow fragment of the block's L0 family, so the
    # family-canonicalized pool keeps only the block's exact slices.
    assert not any(item.startswith("slice.unit.test.fp.anchor.") for item in unit_ids)


class _DiscardingResponses(dict[str, str]):
    def __init__(
        self,
        *,
        discard_verification_only: bool = False,
        discard_keys: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.discard_verification_only = discard_verification_only
        self.discard_keys = discard_keys

    def __setitem__(self, key: str, value: str) -> None:
        if self.discard_keys:
            if key.startswith(self.discard_keys):
                return
            super().__setitem__(key, value)
            return
        if not self.discard_verification_only or key.startswith(
            ("support-verification.", "support-whole-verification.")
        ):
            return
        super().__setitem__(key, value)


def test_producer_reports_missing_raw_outputs_for_both_proposal_kinds() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.110.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="raw.a",
    )
    kwargs: Any = {
        "task": task,
        "units": (unit_a,),
        "needs": (knowledge,),
        "basis_commit_id": world.source_commit,
        "basis_snapshot_id": CROSS_NEED_SNAPSHOT,
        "unit_need_ids": {unit_a.unit_id: (knowledge.need_id,)},
        "token_counter": WriterContextAssembler().count_tokens,
    }
    proposal: dict[str, object] = {
        "claims": [
            {
                "need_id": knowledge.need_id.root,
                "need_facet_ids": _atom_required_facets(knowledge),
                "slice_unit_id": _slice_id_for(unit_a),
                "claim_text": "徐有容身在南方。",
                "single_slice_sufficient": True,
            }
        ],
        "insufficient_need_ids": [],
    }
    proposal_gateway = _support_gateway(proposal)
    proposal_gateway.raw_responses = _DiscardingResponses()
    proposal_producer = TrustedClaimSupportProducer(semantic_gateway=proposal_gateway)
    proposal_producer.produce(**kwargs)
    assert "PRODUCER_SINGLE_SLICE_RAW_OUTPUT_MISSING" in proposal_producer.last_diagnostic_codes

    verification_gateway = _support_gateway(
        proposal,
        {"decisions": [{"claim_index": 0, "supports": True}]},
    )
    verification_gateway.raw_responses = _DiscardingResponses(discard_verification_only=True)
    verification_producer = TrustedClaimSupportProducer(semantic_gateway=verification_gateway)
    verification_producer.produce(**kwargs)
    assert (
        "SEMANTIC_SUPPORT_WHOLE_VERIFIER_RAW_OUTPUT_MISSING"
        in verification_producer.last_diagnostic_codes
    )

    multi_proposal: dict[str, object] = {
        "claims": [
            {
                "need_id": knowledge.need_id.root,
                "need_facet_ids": _atom_required_facets(knowledge),
                "slice_unit_ids": [_slice_id_for(unit_a)],
                "claim_text": "徐有容身在南方。",
            }
        ],
        "insufficient_need_ids": [],
    }
    multi_gateway = _support_gateway(
        {"claims": [], "insufficient_need_ids": [knowledge.need_id.root]},
        multi_proposal,
        {"decisions": [{"claim_index": 0, "supports": True}]},
    )
    multi_gateway.raw_responses = _DiscardingResponses(
        discard_keys=("support-multi-slice-proposal.",)
    )
    multi_producer = TrustedClaimSupportProducer(semantic_gateway=multi_gateway)
    multi_producer.produce(**kwargs)
    assert "PRODUCER_MULTI_SLICE_RAW_OUTPUT_MISSING" in multi_producer.last_diagnostic_codes

    multi_verification_gateway = _support_gateway(
        {"claims": [], "insufficient_need_ids": [knowledge.need_id.root]},
        multi_proposal,
        {"decisions": [{"claim_index": 0, "supports": True}]},
    )
    multi_verification_gateway.raw_responses = _DiscardingResponses(
        discard_keys=("support-whole-verification.",)
    )
    multi_verification_producer = TrustedClaimSupportProducer(
        semantic_gateway=multi_verification_gateway
    )
    multi_verification_producer.produce(**kwargs)
    assert (
        "SEMANTIC_SUPPORT_WHOLE_VERIFIER_RAW_OUTPUT_MISSING"
        in multi_verification_producer.last_diagnostic_codes
    )


def test_producer_records_proposal_and_verifier_transport_failures() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.111.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="transport.a",
    )
    kwargs: Any = {
        "task": task,
        "units": (unit_a,),
        "needs": (knowledge,),
        "basis_commit_id": world.source_commit,
        "basis_snapshot_id": CROSS_NEED_SNAPSHOT,
        "unit_need_ids": {unit_a.unit_id: (knowledge.need_id,)},
        "token_counter": WriterContextAssembler().count_tokens,
    }
    progress: list[Mapping[str, object]] = []
    proposal_failure = TrustedClaimSupportProducer(
        semantic_gateway=_support_gateway(RuntimeError("proposal failed")),
        progress_writer=progress.append,
    )
    proposal_failure.produce(**kwargs)
    assert "PRODUCER_SINGLE_SLICE_RUNTIMEERROR" in proposal_failure.last_diagnostic_codes
    failed_proposal = next(item for item in progress if item["stage"] == "proposal")
    assert failed_proposal["status"] == "failed"
    assert failed_proposal["error_type"] == "RuntimeError"

    proposal: dict[str, object] = {
        "claims": [
            {
                "need_id": knowledge.need_id.root,
                "need_facet_ids": _atom_required_facets(knowledge),
                "slice_unit_id": _slice_id_for(unit_a),
                "claim_text": "徐有容身在南方。",
                "single_slice_sufficient": True,
            }
        ],
        "insufficient_need_ids": [],
    }
    verifier_failure = TrustedClaimSupportProducer(
        semantic_gateway=_support_gateway(proposal, RuntimeError("verification failed"))
    )
    verifier_failure.produce(**kwargs)
    assert "SEMANTIC_SUPPORT_WHOLE_VERIFIER_RUNTIMEERROR" in verifier_failure.last_diagnostic_codes
    assert verifier_failure.last_funnel.verifier_transport_failures == 1


def test_no_workset_need_counts_facet_not_closed() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    stale_basis = _unit(
        world,
        unit_id="grounded.block.test.112.0",
        text="基于旧 commit 的段落。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="facet.stale",
        source_commit=CommitId("sha256:" + "7" * 64),
    )
    producer = TrustedClaimSupportProducer(
        semantic_gateway=_support_gateway(
            {"claims": [], "insufficient_need_ids": [knowledge.need_id.root]}
        )
    )
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(stale_basis,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={stale_basis.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert groups == ()
    assert producer.last_funnel.facet_not_closed >= 1


def test_whole_verifier_cache_hit_reuses_retained_artifacts() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.113.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="cache.a",
    )
    proposal: dict[str, object] = {
        "claims": [
            {
                "need_id": knowledge.need_id.root,
                "need_facet_ids": _atom_required_facets(knowledge),
                "slice_unit_id": _slice_id_for(unit_a),
                "claim_text": "徐有容身在南方。",
                "single_slice_sufficient": True,
            }
        ],
        "insufficient_need_ids": [],
    }
    endpoint = _SemanticSupportEndpoint(
        (
            proposal,
            {"decisions": [{"claim_index": 0, "supports": True}]},
            proposal,
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    kwargs: Any = {
        "task": task,
        "units": (unit_a,),
        "needs": (knowledge,),
        "basis_commit_id": world.source_commit,
        "basis_snapshot_id": CROSS_NEED_SNAPSHOT,
        "unit_need_ids": {unit_a.unit_id: (knowledge.need_id,)},
        "token_counter": WriterContextAssembler().count_tokens,
    }
    groups, _variants, _receipts, _attestations = producer.produce(**kwargs)
    assert any(group.producer.endswith(".single") for group in groups)
    assert len(endpoint.requests) == 2
    groups2, _variants2, _receipts2, _attestations2 = producer.produce(**kwargs)
    assert any(group.producer.endswith(".single") for group in groups2)
    # The second run re-proposes but the identical whole-claim verification is
    # served from the retained-artifact cache, so no new verifier request.
    assert len(endpoint.requests) == 3


def test_multi_slice_insufficient_need_id_records_needs_insufficient() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.114.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="insufficient.a",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert groups == ()
    assert producer.last_funnel.needs_insufficient >= 1


def test_grounded_selection_workset_report_and_selector_round_trip() -> None:
    task, capability, unit, assembler, _selection_result = _grounded_selection()
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [
                    {
                        "need_id": capability.need_id.root,
                        "need_facet_ids": _atom_required_facets(capability),
                        "slice_unit_id": _slice_id_for(unit),
                        "claim_text": "林澈当前能力受伤势限制。",
                        "single_slice_sufficient": True,
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    selector = ControllerSupportSelector(producer)
    selection = selector.select(
        task=task,
        units=(unit,),
        needs=(capability,),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={unit.unit_id: (capability.need_id,)},
        writer_token_budget=4000,
        evidence_ledger_token_budget=12_000,
        token_counter=assembler.count_tokens,
    )
    assert selection.workset_reports
    report = selection.workset_reports[0]
    assert report.need_id == capability.need_id
    assert len(report.slice_ids) == 1
    assert selection.raw_evidence_ledger_entries
    assert selection.support_groups
    assert any(
        group.semantic_support_status is SemanticSupportStatus.VERIFIED
        for group in selection.support_groups
    )


def test_pack_workset_diversity_and_budget_drop_paths() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.120.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="pack.a",
    )
    unit_b = _unit(
        world,
        unit_id="grounded.block.test.121.0",
        text="与陈长生并无直接交谈。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="pack.b",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (unit_a, unit_b),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert len(slices) == 2
    workset, dropped, _workset_rows = producer._pack_workset(
        slices,
        need=knowledge,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert len(workset) == 2
    assert dropped == 0
    # A duplicate chapter slice is only kept once in the diversity pass but
    # still eligible in the fill pass.
    duplicate = _unit(
        world,
        unit_id="grounded.block.test.122.0",
        text="徐有容身在南方第二句。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="pack.dup",
    )
    slices3, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (unit_a, duplicate),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    workset3, _dropped3, _workset_rows3 = producer._pack_workset(
        slices3,
        need=knowledge,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert len(workset3) == 2


def test_segment_paragraph_sentence_window_paths() -> None:
    producer = TrustedClaimSupportProducer()
    # A paragraph with only terminators is preserved as one exact window.
    assert producer._segment_paragraph("。!?", 0) == ((0, 3, "。!?"),)
    # A paragraph that fits one window unchanged.
    assert producer._segment_paragraph("徐有容身在南方。", 10) == ((10, 18, "徐有容身在南方。"),)
    # An oversized paragraph with multiple sentences splits into windows that
    # stay contiguous and in order.
    paragraph = "第一句。第二句。第三句。" * 30
    windows = producer._segment_paragraph(paragraph, 0)
    assert len(windows) > 1
    joined = "".join(text for _start, _end, text in windows)
    assert joined == paragraph


def test_segment_block_text_handles_whitespace_only_tail() -> None:
    producer = TrustedClaimSupportProducer()
    assert producer._segment_block_text("段落一。\n\n\n") == ((0, 4, "段落一。"),)
    assert producer._segment_block_text("   \n  ") == ()


def test_canonical_base_ref_block_id_matching_paths() -> None:
    _task, world = _cross_need_task()
    unit = _unit(
        world,
        unit_id="grounded.block.test.130.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="base.block",
    )
    producer = TrustedClaimSupportProducer()
    base = unit.evidence_refs[0]
    # A ref whose block id equals grounded->block mapping wins.
    mapped = base.model_copy(
        update={
            "evidence_id": StableId("evidence.full.block.test.130.0"),
            "span": (
                cast(Any, base.span).model_copy(update={"block_id": StableId("block.test.130.0")})
                if base.span is not None
                else None
            ),
        }
    )
    assert (
        producer._canonical_base_ref(unit.model_copy(update={"evidence_refs": (mapped,)})) == mapped
    )
    # A ref whose block id is contained in the unit id wins.
    contained = base.model_copy(
        update={
            "evidence_id": StableId("evidence.curator.130"),
            "span": (
                cast(Any, base.span).model_copy(update={"block_id": StableId("block.test.130")})
                if base.span is not None
                else None
            ),
        }
    )
    assert (
        producer._canonical_base_ref(unit.model_copy(update={"evidence_refs": (contained,)}))
        == contained
    )
    # Spanless refs are skipped.
    spanless = base.model_copy(update={"span": None})
    assert (
        producer._canonical_base_ref(unit.model_copy(update={"evidence_refs": (spanless,)}))
        is not None
    )
    # A compact unit's refs are never chosen as canonical base refs; the
    # fallback is still available only as derivation lineage.
    compact = unit.model_copy(update={"unit_id": StableId("compact.grounded.block.test.130.0")})
    compact_mapped = compact.model_copy(update={"evidence_refs": (mapped,)})
    assert producer._canonical_base_ref(compact_mapped) == mapped


def test_legal_for_need_unknown_scope_and_plan_information() -> None:
    task, capability, unit, _assembler, _selection_result = _selection()
    producer = TrustedClaimSupportProducer()
    unknown_scope_need = capability.model_copy(update={"access_scope": "evaluator_only"})
    assert not producer._legal_for_need(task, unknown_scope_need, unit)
    plan_unit = unit.model_copy(
        update={
            "unit_id": StableId("anchor.plan.node-1"),
            "unit_kind": RetrievalUnitKind.PLAN_ANCHOR,
            "access_scope": "author_planning",
            "information_label": "plan",
            "evidence_refs": (),
        }
    )
    # The visible-at-cutoff profile never admits plan information even when the
    # Need allows it.
    assert not producer._legal_for_need(task, capability, plan_unit)
    from novel_agent.domain.writer_context import BenchmarkInformationProfile

    plan_task = build_safe_task_contract(
        case_id=StableId(task.task_id.root),
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    planning_need = capability.model_copy(
        update={"access_scope": "author_planning", "allow_plan": True}
    )
    assert producer._legal_for_need(plan_task, planning_need, plan_unit)


def test_resolution_status_unresolved_basis_and_cutoff_paths() -> None:
    _task, _capability, unit, _assembler, _selection_result = _selection()
    producer = TrustedClaimSupportProducer()
    assert (
        producer._resolution_status(
            (),
            unit,
            basis_commit_id=unit.source_commit,
            checkpoint_chapter=20,
            plan_node_ids=(),
        )
        is EvidenceResolutionStatus.UNRESOLVED
    )
    mismatched = unit.model_copy(update={"source_commit": CommitId("sha256:" + "9" * 64)})
    assert (
        producer._resolution_status(
            unit.evidence_refs,
            mismatched,
            basis_commit_id=unit.source_commit,
            checkpoint_chapter=20,
            plan_node_ids=(),
        )
        is EvidenceResolutionStatus.BASIS_MISMATCH
    )
    future = unit.model_copy(
        update={
            "evidence_refs": (
                unit.evidence_refs[0].model_copy(
                    update={"chapter_id": StableId("chapter.test.99")}
                ),
            )
        }
    )
    assert (
        producer._resolution_status(
            future.evidence_refs,
            future,
            basis_commit_id=unit.source_commit,
            checkpoint_chapter=20,
            plan_node_ids=(),
        )
        is EvidenceResolutionStatus.CUTOFF_VIOLATION
    )


def test_slice_by_id_miss_returns_none() -> None:
    _task, world = _cross_need_task()
    knowledge = _generated_need(_task, world, "knowledge_boundary")
    unit = _unit(
        world,
        unit_id="grounded.block.test.140.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="slice.byid",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (unit,),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert producer._slice_by_id(slices, StableId("slice.missing")) is None


def test_compatible_pool_anchor_tiers_and_lineage() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    relationship = _generated_need(task, world, "relationship_emotion")
    direct = _unit(
        world,
        unit_id="unit.test.tiers.direct",
        text="陈长生直接支持知识边界的证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=10,
        seed="tiers.direct",
    )
    shared_anchor = _unit(
        world,
        unit_id="unit.test.tiers.shared",
        text="共享实体的兼容证据。",
        entity_ids=(StableId("entity.synthetic.lin-che"),),
        chapter=11,
        seed="tiers.shared",
    )
    origin_anchor = _unit(
        world,
        unit_id="unit.test.tiers.origin",
        text="origin 实体重叠的兼容证据。",
        entity_ids=(ENTITY_OTHER,),
        chapter=12,
        seed="tiers.origin",
    )
    child = _unit(
        world,
        unit_id="unit.test.tiers.child",
        text="直接单元的精确子证据。",
        entity_ids=(ENTITY_OTHER,),
        chapter=13,
        seed="tiers.child",
        parent_unit_id=direct.unit_id,
        parent_unit_ids=(direct.unit_id,),
    )
    unrelated = _unit(
        world,
        unit_id="unit.test.tiers.unrelated",
        text="无任何锚点的证据。",
        entity_ids=(ENTITY_OTHER,),
        chapter=14,
        seed="tiers.unrelated",
    )
    foreign_need = _remap_need(
        _generated_need(task, world, "relationship_emotion"),
        StableId("need.test.tiers-foreign"),
    ).model_copy(update={"entity_ids": (ENTITY_OTHER,), "focus_ids": ()})
    producer = TrustedClaimSupportProducer()
    compatible = producer._compatible_support_units(
        task=task,
        target_need=knowledge,
        units=(direct, shared_anchor, origin_anchor, child, unrelated),
        need_by_id={
            knowledge.need_id: knowledge,
            relationship.need_id: relationship,
            foreign_need.need_id: foreign_need,
        },
        units_by_need={
            knowledge.need_id: (direct,),
            relationship.need_id: (shared_anchor, origin_anchor, child),
            foreign_need.need_id: (unrelated,),
        },
        origin_need_ids={
            direct.unit_id: (knowledge.need_id,),
            shared_anchor.unit_id: (relationship.need_id,),
            origin_anchor.unit_id: (relationship.need_id,),
            child.unit_id: (relationship.need_id,),
            unrelated.unit_id: (foreign_need.need_id,),
        },
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
    )
    ids = {unit.unit_id for unit in compatible}
    assert direct.unit_id not in ids
    assert shared_anchor.unit_id in ids
    assert origin_anchor.unit_id in ids
    assert child.unit_id in ids
    assert unrelated.unit_id not in ids


def test_evidence_diverse_pool_keeps_plan_only_and_unresolved_units() -> None:
    _task, world = _cross_need_task()
    direct = _unit(
        world,
        unit_id="unit.test.diverse.direct",
        text="直接证据段落。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=10,
        seed="diverse.direct",
    )
    no_refs = _unit(
        world,
        unit_id="unit.test.diverse.no-refs",
        text="无证据引用的计划锚点。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=11,
        seed="diverse.no-refs",
    ).model_copy(update={"evidence_refs": ()})
    producer = TrustedClaimSupportProducer()
    kept = producer._evidence_diverse_pool((direct, no_refs), ())
    assert {unit.unit_id for unit in kept} == {direct.unit_id, no_refs.unit_id}


def test_has_full_passage_ref_detection() -> None:
    _task, world = _cross_need_task()
    full = _unit(
        world,
        unit_id="unit.test.fp.full",
        text="整段证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=10,
        seed="fp.full",
    )
    ref = full.evidence_refs[0].model_copy(
        update={"evidence_id": StableId("evidence.full.block.test.10.0")}
    )
    assert TrustedClaimSupportProducer._has_full_passage_ref(
        full.model_copy(update={"evidence_refs": (ref,)})
    )
    assert not TrustedClaimSupportProducer._has_full_passage_ref(full)


def test_claim_candidates_lexical_matching_and_clipping() -> None:
    _task, capability, unit, _assembler, _selection_result = _selection()
    producer = TrustedClaimSupportProducer()
    grounded = unit.model_copy(
        update={
            "unit_id": StableId("grounded.block.test.150.0"),
            "unit_kind": RetrievalUnitKind.GROUNDED_BLOCK,
            "text": "林澈当前能力可用, 但受伤时无法持续。这是完全无关的第二句。",
        }
    )
    base = unit.evidence_refs[0]
    refs = (
        base.model_copy(
            update={
                "evidence_id": StableId("evidence.full.test.150"),
                "span": base.span,
                "quote_hash": quote_hash(grounded.text),
            }
        ),
    )
    grounded = grounded.model_copy(update={"evidence_refs": refs})
    claims = producer._claim_candidates(grounded, capability)
    assert claims
    for claim, evidence in claims:
        assert claim
        assert evidence
        assert evidence[0].span is not None
        assert evidence[0].quote_hash == quote_hash(claim)


def test_coalesce_merges_identical_claims_across_needs() -> None:
    task, capability, unit, _assembler, _selection_result = _selection()
    second_need = _remap_need(capability, StableId("need.test.coalesce.second"))
    producer = TrustedClaimSupportProducer()
    groups, variants, receipts, attestations = producer.produce(
        task=task,
        units=(unit,),
        needs=(capability, second_need),
        basis_commit_id=unit.source_commit,
        basis_snapshot_id=unit.snapshot_id,
        unit_need_ids={
            unit.unit_id: (capability.need_id, second_need.need_id),
        },
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert groups
    assert any(len(group.need_ids) > 1 for group in groups)
    assert variants
    assert receipts
    assert attestations


def test_historical_rescue_lineage_and_local_window_ordering() -> None:
    task, world = _cross_need_task()
    callback = _generated_need(task, world, "long_range_callback")
    event_anchor = _unit(
        world,
        unit_id="unit.test.rescue.anchor",
        text="陈长生在近期章节遭遇黑龙。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="rescue.anchor",
        kind=RetrievalUnitKind.EVENT_ANCHOR,
    )
    late_span = _unit(
        world,
        unit_id="unit.test.rescue.span",
        text="黑龙于早期章节点出现。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=3,
        seed="rescue.span",
        kind=RetrievalUnitKind.GROUNDED_SPAN,
        parent_unit_id=StableId("grounded.block.test.3.0"),
        parent_unit_ids=(StableId("grounded.block.test.3.0"),),
    )
    parent_block = _unit(
        world,
        unit_id="grounded.block.test.3.0",
        text="黑龙于早期章节点出现, 陈长生与它对峙。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=3,
        seed="rescue.block",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [callback.need_id.root],
            },
        )
    )
    TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint)).produce(
        task=task,
        units=(event_anchor, late_span, parent_block),
        needs=(callback,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={
            event_anchor.unit_id: (callback.need_id,),
            late_span.unit_id: (callback.need_id,),
            parent_block.unit_id: (callback.need_id,),
        },
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert endpoint.requests
    target_entry = _json_objects(_prompt_input(endpoint, 0), "needs")[0]
    unit_ids = [_unit_id(item) for item in _json_objects(target_entry, "exact_slices")]
    assert any(item.startswith("slice.grounded.block.test.3.0.") for item in unit_ids)


def test_historical_rescue_with_local_grounded_window() -> None:
    task, world = _cross_need_task()
    callback = _generated_need(task, world, "long_range_callback")
    anchor = _unit(
        world,
        unit_id="unit.test.local.anchor",
        text="陈长生在近期章节遭遇黑龙。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="local.anchor",
        kind=RetrievalUnitKind.EVENT_ANCHOR,
    )
    local_grounded = _unit(
        world,
        unit_id="grounded.block.test.19.0",
        text="黑龙与陈长生对峙于第十九章。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="local.grounded",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [callback.need_id.root],
            },
        )
    )
    TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint)).produce(
        task=task,
        units=(anchor, local_grounded),
        needs=(callback,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={
            anchor.unit_id: (callback.need_id,),
            local_grounded.unit_id: (callback.need_id,),
        },
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert endpoint.requests
    target_entry = _json_objects(_prompt_input(endpoint, 0), "needs")[0]
    unit_ids = [_unit_id(item) for item in _json_objects(target_entry, "exact_slices")]
    assert any(item.startswith("slice.grounded.block.test.19.0.") for item in unit_ids)


def test_ledger_retention_budget_drop_and_clean_claim_path() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    big_text = "\n".join(f"第{index}段相关证据内容非常充分。" for index in range(200))
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.160.0",
        text=big_text,
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="retention.a",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert groups == ()
    assert producer.last_funnel.ledger_dropped >= 0


def test_selector_skips_need_without_completion_spec() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    specless = knowledge.model_copy(update={"completion_spec": None, "need_facets": ()})
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.161.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="specless.a",
    )
    selection = ControllerSupportSelector().select(
        task=task,
        units=(unit_a,),
        needs=(specless,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (specless.need_id,)},
        writer_token_budget=4000,
        evidence_ledger_token_budget=12_000,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert selection.support_groups == ()


def test_compatible_pool_rejects_illegal_tainted_and_foreign_basis_units() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    relationship = _generated_need(task, world, "relationship_emotion")
    direct = _unit(
        world,
        unit_id="unit.test.reject.direct",
        text="陈长生直接支持知识边界的证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=10,
        seed="reject.direct",
    )
    illegal_scope = _unit(
        world,
        unit_id="unit.test.reject.scope",
        text="不可见 scope 的兼容证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=11,
        seed="reject.scope",
        access_scope="author_planning",
    )
    tainted = _unit(
        world,
        unit_id="unit.test.reject.tainted",
        text="带 taint 的兼容证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=12,
        seed="reject.tainted",
        derivation_taint=("contradicted",),
    )
    foreign_basis = _unit(
        world,
        unit_id="unit.test.reject.foreign",
        text="不同 basis 的兼容证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=13,
        seed="reject.foreign",
        source_commit=CommitId("sha256:" + "8" * 64),
    )
    no_origin = _unit(
        world,
        unit_id="unit.test.reject.no-origin",
        text="无 origin 的兼容证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=14,
        seed="reject.no-origin",
    )
    producer = TrustedClaimSupportProducer()
    origin_need_ids = {
        direct.unit_id: (knowledge.need_id,),
        illegal_scope.unit_id: (relationship.need_id,),
        tainted.unit_id: (relationship.need_id,),
        foreign_basis.unit_id: (relationship.need_id,),
    }
    compatible = producer._compatible_support_units(
        task=task,
        target_need=knowledge,
        units=(direct, illegal_scope, tainted, foreign_basis, no_origin),
        need_by_id={knowledge.need_id: knowledge, relationship.need_id: relationship},
        units_by_need={
            knowledge.need_id: (direct,),
            relationship.need_id: (illegal_scope, tainted, foreign_basis),
        },
        origin_need_ids=origin_need_ids,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
    )
    assert compatible == ()


def test_resolve_exact_slices_rejects_spanless_and_refless_blocks() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    spanless = _unit(
        world,
        unit_id="grounded.block.test.170.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="spanless.block",
        span=False,
    )
    no_refs = _unit(
        world,
        unit_id="grounded.block.test.171.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="norefs.block",
    ).model_copy(update={"evidence_refs": ()})
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (spanless, no_refs),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert slices == ()


def test_resolve_exact_slices_deduplicates_identical_passages() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    first = _unit(
        world,
        unit_id="grounded.block.test.172.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="dedup.first",
    )
    second = _unit(
        world,
        unit_id="grounded.block.test.173.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="dedup.second",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (first, second),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert len(slices) == 1
    assert slices[0].parent_unit_id == first.unit_id


def test_emit_verified_claim_skips_whitespace_only_text() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.174.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="clean.a",
    )
    endpoint = _SemanticSupportEndpoint(({"decisions": [{"claim_index": 0, "supports": True}]},))
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (unit_a,),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=task.checkpoint_chapter,
        origin_need_ids={},
    )
    audit = producer._verify_claim_whole(
        task=task,
        need=knowledge,
        claim_text="徐有容身在南方。",
        facet_ids=(knowledge.need_facets[0].need_facet_id,),
        cited_slices=(slices[0],),
        context_slices=slices[1:],
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        funnel=producer.last_funnel,
    )
    assert audit is not None
    # The host fail-closed garbage guard rejects whitespace-only claims before
    # verification, so the emit-time defensive clean guard is exercised
    # directly: a whitespace-only claim_text is cleaned to empty and emitted
    # nothing even when a verifier already approved the proposal audit.
    groups: list[ClaimSupportGroup] = []
    variants: list[ClaimVariant] = []
    receipts: list[ClaimSupportReceipt] = []
    attestations: list[CutoffAttestation] = []
    producer._emit_verified_claim(
        need=knowledge,
        claim_text="   \n  ",
        unit_ids=(slices[0].slice_id,),
        evidence_refs=(slices[0].evidence_ref,),
        facet_ids=(knowledge.need_facets[0].need_facet_id,),
        audit=audit,
        proposal_audit=None,
        task=task,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        token_counter=WriterContextAssembler().count_tokens,
        groups=groups,
        variants=variants,
        receipts=receipts,
        attestations=attestations,
        producer_marker="synthesized",
    )
    assert groups == []
    assert variants == []
    assert receipts == []
    assert attestations == []


def test_segment_paragraph_skips_whitespace_only_sentence() -> None:
    producer = TrustedClaimSupportProducer()
    paragraph = "徐有容身在南方。   \n  与陈长生并无直接交谈。"
    windows = producer._segment_paragraph(paragraph, 0)
    assert windows
    joined = "".join(text for _s, _e, text in windows)
    assert "与陈长生并无直接交谈" in joined


def test_pack_workset_diversity_budget_drop() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    huge = _unit(
        world,
        unit_id="grounded.block.test.175.0",
        text="。".join(f"第{index}段内容" for index in range(50)) + "。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="diversity.huge",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (huge,),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert slices
    workset, dropped, _workset_rows = producer._pack_workset(
        slices,
        need=knowledge,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert workset
    assert dropped == len(slices) - len(workset)


def test_single_slice_audit_insufficient_falls_through_to_multi() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.180.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="insufficient.single",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_id": _slice_id_for(unit_a),
                        "claim_text": "徐有容身在南方。",
                        "single_slice_sufficient": False,
                    }
                ],
                "insufficient_need_ids": [],
            },
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_ids": [_slice_id_for(unit_a)],
                        "claim_text": "徐有容身在南方。",
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert any(group.producer.endswith(".synthesized") for group in groups)


def test_units_by_need_skips_unknown_and_illegal_mappings() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.181.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="mapping.unknown",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={
            unit_a.unit_id: (knowledge.need_id, StableId("need.stage2m.missing")),
        },
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert groups == ()


def test_rescue_lineage_parent_none_and_missing_parent() -> None:
    task, world = _cross_need_task()
    callback = _generated_need(task, world, "long_range_callback")
    anchor = _unit(
        world,
        unit_id="unit.test.rescue2.anchor",
        text="陈长生在近期章节遭遇黑龙。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="rescue2.anchor",
        kind=RetrievalUnitKind.EVENT_ANCHOR,
    )
    span_no_parent = _unit(
        world,
        unit_id="unit.test.rescue2.span",
        text="黑龙于早期章节点出现。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=3,
        seed="rescue2.span",
        kind=RetrievalUnitKind.GROUNDED_SPAN,
    )
    span_missing_parent = _unit(
        world,
        unit_id="unit.test.rescue2.span2",
        text="轩辕破成为第三名学生。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=4,
        seed="rescue2.span2",
        kind=RetrievalUnitKind.GROUNDED_SPAN,
        parent_unit_id=StableId("grounded.block.test.missing"),
        parent_unit_ids=(StableId("grounded.block.test.missing"),),
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [callback.need_id.root],
            },
        )
    )
    TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint)).produce(
        task=task,
        units=(anchor, span_no_parent, span_missing_parent),
        needs=(callback,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={
            anchor.unit_id: (callback.need_id,),
            span_no_parent.unit_id: (callback.need_id,),
            span_missing_parent.unit_id: (callback.need_id,),
        },
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert endpoint.requests
    target_entry = _json_objects(_prompt_input(endpoint, 0), "needs")[0]
    unit_ids = [_unit_id(item) for item in _json_objects(target_entry, "exact_slices")]
    assert any(item.startswith("slice.unit.test.rescue2.span.") for item in unit_ids)


def test_segment_paragraph_whitespace_only_segment_skipped() -> None:
    producer = TrustedClaimSupportProducer()
    paragraph = "徐有容身在南方。      与陈长生并无直接交谈。"
    windows = producer._segment_paragraph(paragraph, 0)
    assert windows
    assert all(text.strip() for _s, _e, text in windows)
    # An oversized paragraph whose sentences are separated by whitespace-only
    # gaps produces a whitespace-only sentence match that is skipped while the
    # windows remain contiguous and exact.
    long = ("徐有容身在南方。" + "  ") * 120
    windows = producer._segment_paragraph(long, 0)
    assert len(windows) > 1
    assert "".join(text for _s, _e, text in windows).startswith("徐有容身在南方。")
    # An oversized whitespace-only paragraph yields no sentence windows at all.
    assert producer._segment_paragraph(" " * 400, 0) == ()


def test_canonical_base_ref_contained_block_id() -> None:
    _task, world = _cross_need_task()
    _knowledge = _generated_need(_task, world, "knowledge_boundary")
    unit = _unit(
        world,
        unit_id="grounded.block.test.182.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="base.contained",
    )
    producer = TrustedClaimSupportProducer()
    base = unit.evidence_refs[0]
    contained = base.model_copy(
        update={
            "evidence_id": StableId("evidence.curator.182"),
            "span": (
                cast(Any, base.span).model_copy(update={"block_id": StableId("block.test.182.0")})
                if base.span is not None
                else None
            ),
        }
    )
    # The block id matches the grounded->block mapping exactly, so the equality
    # branch decides before the containment fallback.
    assert (
        producer._canonical_base_ref(unit.model_copy(update={"evidence_refs": (contained,)}))
        == contained
    )
    # A narrower contained id (no trailing index) falls back to containment.
    nested = base.model_copy(
        update={
            "evidence_id": StableId("evidence.curator.182b"),
            "span": (
                cast(Any, base.span).model_copy(update={"block_id": StableId("block.test")})
                if base.span is not None
                else None
            ),
        }
    )
    assert (
        producer._canonical_base_ref(unit.model_copy(update={"evidence_refs": (nested,)})) == nested
    )


def test_chapter_index_non_decimal_suffix() -> None:
    _task, _capability, unit, _assembler, _selection_result = _selection()
    producer = TrustedClaimSupportProducer()
    chapterless = unit.model_copy(
        update={"evidence_refs": (unit.evidence_refs[0].model_copy(update={"chapter_id": None}),)}
    )
    assert producer._chapter_index(chapterless) is None
    non_decimal = unit.model_copy(
        update={
            "evidence_refs": (
                unit.evidence_refs[0].model_copy(
                    update={"chapter_id": StableId("chapter.test.unknown")}
                ),
            )
        }
    )
    assert producer._chapter_index(non_decimal) is None


def test_compatible_pool_rejects_cutoff_violating_unit() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    relationship = _generated_need(task, world, "relationship_emotion")
    direct = _unit(
        world,
        unit_id="unit.test.cutoff.direct",
        text="陈长生直接支持知识边界的证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=10,
        seed="cutoff.direct",
    )
    future = _unit(
        world,
        unit_id="unit.test.cutoff.future",
        text="未来章节的兼容证据。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=99,
        seed="cutoff.future",
    )
    producer = TrustedClaimSupportProducer()
    compatible = producer._compatible_support_units(
        task=task,
        target_need=knowledge,
        units=(direct, future),
        need_by_id={knowledge.need_id: knowledge, relationship.need_id: relationship},
        units_by_need={
            knowledge.need_id: (direct,),
            relationship.need_id: (future,),
        },
        origin_need_ids={
            direct.unit_id: (knowledge.need_id,),
            future.unit_id: (relationship.need_id,),
        },
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
    )
    assert compatible == ()


def test_extract_json_payload_strips_markdown_fence() -> None:
    assert TrustedClaimSupportProducer._extract_json_payload('{"a": 1}') == '{"a": 1}'
    assert (
        TrustedClaimSupportProducer._extract_json_payload('```json\n{"claims": []}\n```')
        == '{"claims": []}'
    )
    assert TrustedClaimSupportProducer._extract_json_payload('```\n{"x": 2}\n```') == '{"x": 2}'
    assert TrustedClaimSupportProducer._extract_json_payload('["a", 1]') == '["a", 1]'
    assert TrustedClaimSupportProducer._extract_json_payload("plain prose") == "plain prose"
    assert TrustedClaimSupportProducer._extract_json_payload("\n\n{'a': 1}") == "{'a': 1}"
    assert TrustedClaimSupportProducer._extract_json_payload("") == ""
    assert TrustedClaimSupportProducer._extract_json_payload("  {  }  ") == "{  }"


def test_multi_slice_fenced_json_payload_is_recovered() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.45.0",
        text="徐有容身在南方。与陈长生并无直接交谈。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="fenced.multi",
    )
    fenced = (
        "```json\n"
        + json.dumps(
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_ids": [_slice_id_for(unit_a)],
                        "claim_text": "徐有容身在南方。且与陈长生并无直接交谈。",
                    }
                ],
                "insufficient_need_ids": [],
            },
            ensure_ascii=False,
        )
        + "\n```"
    )
    endpoint = _RawTextEndpoint(
        (
            json.dumps(
                {"claims": [], "insufficient_need_ids": [knowledge.need_id.root]},
                ensure_ascii=False,
            ),
            fenced,
            json.dumps({"decisions": [{"claim_index": 0, "supports": True}]}),
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert any(group.producer.endswith(".synthesized") for group in groups)


def test_whitespace_only_multi_slice_claim_rejected_at_proposal_stage() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.183.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="empty.claim",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_ids": [_slice_id_for(unit_a)],
                        "claim_text": "   \n  ",
                    }
                ],
                "insufficient_need_ids": [],
            },
        )
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        semantic_gateway=_gateway_for_endpoint(endpoint),
        progress_writer=progress.append,
    )
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert not any(group.producer.endswith(".synthesized") for group in groups)
    assert groups == ()
    assert producer.last_funnel.proposals_rejected >= 1
    rejected = [
        event
        for event in progress
        if event.get("stage") == "proposal_rejected"
        and event.get("reason") == "rejected:garbage_claim"
    ]
    assert len(rejected) == 1
    # The whitespace-only claim was rejected before verification: only the
    # single-slice probe and the multi-slice synthesis request were made.
    assert len(endpoint.requests) == 2


def test_ledger_retention_budget_drops_overflow_slices() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    relationship = _generated_need(task, world, "relationship_emotion")
    paragraphs = "\n".join(f"第{index}段相关证据内容非常充分。" for index in range(500))
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.184.0",
        text=paragraphs,
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="retention.overflow",
    )
    unit_b = _unit(
        world,
        unit_id="grounded.block.test.185.0",
        text=paragraphs,
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="retention.overflow.b",
    )
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root, relationship.need_id.root],
            },
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a, unit_b),
        needs=(knowledge, relationship),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={
            unit_a.unit_id: (knowledge.need_id,),
            unit_b.unit_id: (relationship.need_id,),
        },
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert groups == ()
    assert producer.last_funnel.ledger_dropped >= 1
    assert producer.last_workset_reports


def test_pack_workset_diversity_pass_drops_oversized_first_slice() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit = _unit(
        world,
        unit_id="grounded.block.test.186.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="diversity.oversized",
    )
    producer = TrustedClaimSupportProducer()
    base = unit.evidence_refs[0]
    oversized_text = "长" * 60000
    oversized = EvidenceSlice(
        slice_id=StableId("slice.grounded.block.test.186.0.0.oversized"),
        parent_unit_id=unit.unit_id,
        parent_block_id=StableId("block.test.186.0"),
        chapter_id=StableId("chapter.test.18"),
        scene_id=None,
        object_hash=sha256_id(unit.text.encode("utf-8")),
        text=oversized_text,
        start=0,
        end=len(oversized_text),
        text_hash=quote_hash(oversized_text),
        evidence_ref=base.model_copy(
            update={
                "evidence_id": StableId("evidence.slice.oversized"),
                "quote_hash": quote_hash(oversized_text),
            }
        ),
        source_commit=unit.source_commit,
        snapshot_id=unit.snapshot_id,
        access_scope=unit.access_scope,
        taint=(),
        retrieval_order=0,
    )
    workset, dropped, _workset_rows = producer._pack_workset(
        (oversized,),
        need=knowledge,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert workset == ()
    assert dropped == 1


def test_semantic_receipt_audit_binding_validators() -> None:
    task, capability, unit, assembler, _selection_result = _grounded_selection()
    slice_id = _slice_id_for(unit)
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [
                    {
                        "need_id": capability.need_id.root,
                        "need_facet_ids": _atom_required_facets(capability),
                        "slice_unit_id": slice_id,
                        "claim_text": "林澈当前能力受伤势限制。",
                        "single_slice_sufficient": True,
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    producer = TrustedClaimSupportProducer(semantic_gateway=_gateway_for_endpoint(endpoint))
    _groups, _variants, receipts, _attestations = producer.produce(
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
    assert receipt.need_facet_ids == tuple(
        item.need_facet_id
        for item in capability.need_facets
        if capability.completion_spec is not None
        and item.need_facet_id in capability.completion_spec.required_need_facet_ids
    )
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


def test_pack_workset_fill_pass_budget_drop() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit = _unit(
        world,
        unit_id="grounded.block.test.190.0",
        text="。".join(f"第{index}段相关证据内容非常充分" for index in range(6000)) + "。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="fill.drop",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (unit,),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    assert slices
    workset, dropped, _workset_rows = producer._pack_workset(
        slices,
        need=knowledge,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert workset
    assert dropped == len(slices) - len(workset)


def test_pack_workset_fair_share_round_robin_keeps_deep_chapters() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    shallow = _unit(
        world,
        unit_id="grounded.block.test.191.0",
        text="。".join(f"第{index}段浅层证据" for index in range(300)) + "。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="fair.shallow",
    )
    deep = _unit(
        world,
        unit_id="grounded.block.test.192.0",
        text="。".join(f"第{index}段深层证据" for index in range(60)) + "。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="fair.deep",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (shallow, deep),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    workset, _dropped, _workset_rows = producer._pack_workset(
        slices,
        need=knowledge,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert any(s.chapter_id == deep.evidence_refs[0].chapter_id for s in workset)


def test_single_slice_window_bounds_probe_input() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit = _unit(
        world,
        unit_id="grounded.block.test.193.0",
        text="。".join(f"第{index}段相关证据" for index in range(6000)) + "。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="probe.window",
    )
    producer = TrustedClaimSupportProducer()
    slices, _resolution_rows, _slice_rows = producer._resolve_exact_slices(
        (unit,),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=20,
        origin_need_ids={},
    )
    workset, _dropped, _workset_rows = producer._pack_workset(
        slices,
        need=knowledge,
        token_counter=WriterContextAssembler().count_tokens,
    )
    window = producer._single_slice_window(
        workset,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert window
    assert len(window) <= len(workset)
    assert window[0] == workset[0]


# ---------------------------------------------------------------------------
# Pre-SupportWorkset membership audit, L0-family canonicalization, and
# serialized-request transport budget (Codex REPAIR direction, 2026-08-04).
# ---------------------------------------------------------------------------


def _audit_rows(progress: list[Mapping[str, object]]) -> dict[str, list[dict[str, object]]]:
    rows_by_boundary: dict[str, list[dict[str, object]]] = {}
    for event in progress:
        if event.get("stage") != "handle_audit":
            continue
        rows = event["rows"]
        assert isinstance(rows, list)
        rows_by_boundary.setdefault(str(event["boundary"]), []).extend(
            [item for item in rows if isinstance(item, dict)]
        )
    return rows_by_boundary


def test_handle_audit_records_all_boundaries_with_typed_rows() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.80.0",
        text="徐有容身在南方, 与陈长生并无直接交谈。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="audit.a",
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        progress_writer=progress.append,
        pre_proposal_trace=True,
    )
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert groups == ()
    audit = _audit_rows(progress)
    assert set(audit) == {
        "legal_input_handles",
        "direct_ranked_handles",
        "compatible_handles",
        "deduplicated_diversified_handle_pool",
        "bounded_selected_handles",
        "l0_blocks_spans_resolved",
        "exact_slices_segmented",
        "support_workset_packed",
        "semantic_chunks_exposed",
        "raw_ledger_entries_retained",
    }
    required_keys = {
        "stage",
        "unit_id",
        "l0_family",
        "chapter",
        "kind",
        "origin_need_ids",
        "order",
        "cost",
        "chunk_index",
        "drop_reason",
    }
    for boundary, rows in audit.items():
        for row in rows:
            assert required_keys.issubset(row), boundary
    # Boundaries whose membership is guaranteed by the single legal unit.
    for boundary in (
        "legal_input_handles",
        "direct_ranked_handles",
        "bounded_selected_handles",
        "l0_blocks_spans_resolved",
        "exact_slices_segmented",
        "support_workset_packed",
        "semantic_chunks_exposed",
        "raw_ledger_entries_retained",
    ):
        assert audit[boundary], boundary
    kept = [row for row in audit["exact_slices_segmented"] if row["drop_reason"] is None]
    assert kept
    assert all(row["chapter"] == 18 for row in kept)
    assert all(row["kind"] == "exact_slice" for row in kept)
    assert all(row["l0_family"] == "block.test.18" for row in kept)
    ledger_kept = [
        row for row in audit["raw_ledger_entries_retained"] if row["drop_reason"] is None
    ]
    assert ledger_kept
    assert producer.last_funnel.blocks_resolved == 1
    assert producer.last_funnel.slices_resolved == len(kept)
    assert not any(event.get("stage") == "proposal" for event in progress)
    assert producer.last_workset_reports != ()


def test_pre_proposal_trace_never_issues_model_calls() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.81.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="trace.no.calls",
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        progress_writer=progress.append,
        pre_proposal_trace=True,
    )
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert groups == ()
    assert not any(event.get("stage") in {"proposal", "verification"} for event in progress)
    terminal = next(event for event in progress if event.get("stage") == "terminal")
    assert terminal["proposal_requests"] == 0
    assert terminal["state"] in {"completed", "completed_with_failures"}
    assert producer.last_raw_ledger_entries


def test_producer_without_gateway_and_trace_skips_corridor() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.82.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="trace.off",
    )
    producer = TrustedClaimSupportProducer()
    _groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert producer.last_workset_reports == ()
    assert producer.last_raw_ledger_entries == ()
    assert producer.last_funnel.blocks_resolved == 0


def test_family_canonicalize_keeps_full_passage_block_over_compact_and_anchor() -> None:
    task, world = _cross_need_task()
    _knowledge = _generated_need(task, world, "knowledge_boundary")
    block = _unit(
        world,
        unit_id="grounded.block.test.83.0",
        text="陈长生与黑龙对峙于山谷, 双方僵持不下。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=2,
        seed="family.block",
    )
    full_ref = block.evidence_refs[0].model_copy(
        update={"evidence_id": StableId("evidence.full.block.test.2.0")}
    )
    block = block.model_copy(update={"evidence_refs": (full_ref,)})
    compact = block.model_copy(
        update={
            "unit_id": StableId("compact.grounded.block.test.2.0"),
            "text": "陈长生与黑龙对峙。",
        }
    )
    anchor = _unit(
        world,
        unit_id="anchor.relation.test.2.0",
        text="陈长生遭遇黑龙。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=2,
        seed="family.anchor",
        kind=RetrievalUnitKind.RELATION_ANCHOR,
    )
    canonical, collapsed = TrustedClaimSupportProducer._family_canonicalize(
        (compact, anchor, block)
    )
    assert len(canonical) == 1
    assert canonical[0].unit_id == block.unit_id
    assert collapsed == {compact.unit_id, anchor.unit_id}


def test_family_canonicalize_replaces_earlier_anchor_with_later_block() -> None:
    task, world = _cross_need_task()
    _knowledge = _generated_need(task, world, "knowledge_boundary")
    anchor = _unit(
        world,
        unit_id="anchor.relation.test.84.0",
        text="陈长生遭遇黑龙。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=2,
        seed="family.anchor.first",
        kind=RetrievalUnitKind.RELATION_ANCHOR,
    )
    block = _unit(
        world,
        unit_id="grounded.block.test.84.0",
        text="陈长生与黑龙对峙于山谷, 双方僵持不下。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=2,
        seed="family.block.later",
    )
    canonical, collapsed = TrustedClaimSupportProducer._family_canonicalize((anchor, block))
    assert len(canonical) == 1
    assert canonical[0].unit_id == block.unit_id
    assert collapsed == {anchor.unit_id}


def test_family_canonicalize_distinct_families_all_kept() -> None:
    task, world = _cross_need_task()
    _knowledge = _generated_need(task, world, "knowledge_boundary")
    first = _unit(
        world,
        unit_id="grounded.block.test.85.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="family.one",
    )
    second = _unit(
        world,
        unit_id="grounded.block.test.86.0",
        text="陈长生在学院求学。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="family.two",
    )
    canonical, collapsed = TrustedClaimSupportProducer._family_canonicalize((first, second))
    assert len(canonical) == 2
    assert collapsed == set()


def test_representation_canonical_score_paths() -> None:
    task, world = _cross_need_task()
    _knowledge = _generated_need(task, world, "knowledge_boundary")
    plain_block = _unit(
        world,
        unit_id="grounded.block.test.87.0",
        text="陈长生在学院求学。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="score.block",
    )
    full_block = plain_block.model_copy(
        update={
            "evidence_refs": (
                plain_block.evidence_refs[0].model_copy(
                    update={"evidence_id": StableId("evidence.full.block.test.2.0")}
                ),
            )
        }
    )
    span = _unit(
        world,
        unit_id="grounded.span.test.87.0",
        text="陈长生在学院求学。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="score.span",
        kind=RetrievalUnitKind.GROUNDED_SPAN,
    )
    compact = plain_block.model_copy(
        update={"unit_id": StableId("compact.grounded.block.test.87.0")}
    )
    anchor = _unit(
        world,
        unit_id="anchor.relation.test.87.0",
        text="陈长生在学院求学。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="score.anchor",
        kind=RetrievalUnitKind.RELATION_ANCHOR,
    )
    assert TrustedClaimSupportProducer._representation_canonical_score(full_block) == 5
    assert TrustedClaimSupportProducer._representation_canonical_score(plain_block) == 4
    assert TrustedClaimSupportProducer._representation_canonical_score(span) == 3
    assert TrustedClaimSupportProducer._representation_canonical_score(compact) == 2
    assert TrustedClaimSupportProducer._representation_canonical_score(anchor) == 0


def test_chapter_diverse_order_leads_each_distinct_chapter() -> None:
    task, world = _cross_need_task()
    _knowledge = _generated_need(task, world, "knowledge_boundary")
    ch9_first = _unit(
        world,
        unit_id="grounded.block.test.90.0",
        text="第九章段落。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=9,
        seed="diverse.9",
    )
    ch5 = _unit(
        world,
        unit_id="grounded.block.test.51.0",
        text="第五章段落。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=5,
        seed="diverse.5",
    )
    ch9_second = _unit(
        world,
        unit_id="grounded.block.test.91.0",
        text="第九章另一段。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=9,
        seed="diverse.9b",
    )
    ch2 = _unit(
        world,
        unit_id="grounded.block.test.21.0",
        text="第二章段落。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=2,
        seed="diverse.2",
    )
    ordered = TrustedClaimSupportProducer._chapter_diverse_order((ch9_first, ch5, ch9_second, ch2))
    assert [unit.unit_id for unit in ordered[:3]] == [
        ch9_first.unit_id,
        ch5.unit_id,
        ch2.unit_id,
    ]
    assert ordered[3].unit_id == ch9_second.unit_id


def test_blocks_resolved_counts_unique_resolved_blocks() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    first = _unit(
        world,
        unit_id="grounded.block.test.92.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="blocks.a",
    )
    second = _unit(
        world,
        unit_id="grounded.block.test.93.0",
        text="陈长生在学院求学。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="blocks.b",
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        progress_writer=progress.append,
        pre_proposal_trace=True,
    )
    producer.produce(
        task=task,
        units=(first, second),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={
            first.unit_id: (knowledge.need_id,),
            second.unit_id: (knowledge.need_id,),
        },
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert producer.last_funnel.blocks_resolved == 2
    resolution = _audit_rows(progress)["l0_blocks_spans_resolved"]
    assert len([row for row in resolution if row["drop_reason"] is None]) == 2


def test_resolution_rows_record_every_typed_drop_reason() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    compact = _unit(
        world,
        unit_id="compact.grounded.block.test.94.0",
        text="陈长生在学院求学。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=10,
        seed="resolve.compact",
    )
    anchor = _unit(
        world,
        unit_id="anchor.relation.test.94.0",
        text="陈长生在学院求学。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=11,
        seed="resolve.anchor",
        kind=RetrievalUnitKind.RELATION_ANCHOR,
    )
    spanless = _unit(
        world,
        unit_id="grounded.block.test.94.1",
        text="陈长生在学院求学。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=12,
        seed="resolve.spanless",
        span=False,
    )
    tainted = _unit(
        world,
        unit_id="grounded.block.test.94.2",
        text="陈长生在学院求学。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=13,
        seed="resolve.tainted",
        derivation_taint=("contradicted",),
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        progress_writer=progress.append,
        pre_proposal_trace=True,
    )
    producer.produce(
        task=task,
        units=(compact, anchor, spanless, tainted),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={
            compact.unit_id: (knowledge.need_id,),
            anchor.unit_id: (knowledge.need_id,),
            spanless.unit_id: (knowledge.need_id,),
            tainted.unit_id: (knowledge.need_id,),
        },
        token_counter=WriterContextAssembler().count_tokens,
    )
    resolution = _audit_rows(progress)["l0_blocks_spans_resolved"]
    reasons = {str(row["unit_id"]): str(row["drop_reason"]) for row in resolution}
    assert reasons[compact.unit_id.root].startswith("compact_preview")
    assert reasons[anchor.unit_id.root].startswith("not_grounded")
    assert reasons[spanless.unit_id.root].startswith("resolution_failed")
    assert reasons[tainted.unit_id.root].startswith("filtered:taint")


def test_unit_family_id_lineage_paths() -> None:
    task, world = _cross_need_task()
    _knowledge = _generated_need(task, world, "knowledge_boundary")
    with_span = _unit(
        world,
        unit_id="grounded.block.test.95.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="family.span",
    )
    assert TrustedClaimSupportProducer._unit_family_id(with_span) == "block.test.18"
    lineage = _unit(
        world,
        unit_id="anchor.relation.test.95.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="family.lineage",
        kind=RetrievalUnitKind.RELATION_ANCHOR,
        span=False,
        parent_unit_id=StableId("grounded.block.test.95.0"),
    )
    assert TrustedClaimSupportProducer._unit_family_id(lineage) == "grounded.block.test.95.0"
    self_family = lineage.model_copy(update={"parent_unit_id": None, "parent_unit_ids": ()})
    assert TrustedClaimSupportProducer._unit_family_id(self_family) == self_family.unit_id.root


def test_estimate_prompt_tokens_over_counts_calibrated_tokens() -> None:
    cjk = "她望向远方, 山峦起伏如墨色长卷, 云海翻涌在峰顶之间。" * 100
    latin = "The quick brown fox jumps over the lazy dog. " * 100
    cjk_estimate = TrustedClaimSupportProducer._estimate_prompt_tokens(cjk)
    latin_estimate = TrustedClaimSupportProducer._estimate_prompt_tokens(latin)
    # Calibrated reality: CJK ~0.75 tokens/char (incl. punctuation), ASCII
    # ~0.22 tokens/char.  The estimator over-counts both.
    assert cjk_estimate > len(cjk) * 0.7
    assert latin_estimate > len(latin) * 0.25
    assert cjk_estimate > 0
    assert latin_estimate > 0


def test_sanitize_error_message_strips_urls_and_credentials() -> None:
    message = (
        "connect timeout to http://user:pass@127.0.0.1:8002/v1/chat/completions?key=abc123 "
        + "x" * 600
    )
    cleaned = TrustedClaimSupportProducer._sanitize_error_message(message)
    assert "127.0.0.1" not in cleaned
    assert "user" not in cleaned
    assert "abc123" not in cleaned
    assert len(cleaned) <= 500


def _slice_for_text(
    *,
    text: str,
    index: int,
    chapter: int,
) -> EvidenceSlice:
    block_id = StableId(f"block.test.chunk.{chapter}.{index}")
    return EvidenceSlice(
        slice_id=StableId(f"slice.test.chunk.{index}"),
        parent_unit_id=StableId(f"unit.test.chunk.{index}"),
        parent_block_id=block_id,
        chapter_id=StableId(f"chapter.test.{chapter}"),
        scene_id=None,
        object_hash=sha256_id(text.encode("utf-8")),
        text=text,
        start=0,
        end=len(text),
        text_hash=quote_hash(text),
        evidence_ref=EvidenceRef(
            evidence_id=StableId(f"evidence.slice.test.{index}"),
            root_hash=ArtifactId("sha256:" + "1" * 64),
            object_hash=sha256_id(text.encode("utf-8")),
            chapter_id=StableId(f"chapter.test.{chapter}"),
            span=TextSpanRef(block_id=block_id, start=0, end=len(text)),
            quote_hash=quote_hash(text),
            support_status=EvidenceSupportStatus.CURRENT,
            resolved_at_commit=CommitId("sha256:" + "2" * 64),
        ),
        source_commit=CommitId("sha256:" + "3" * 64),
        snapshot_id=StableId("snapshot.test.chunk"),
        access_scope="writer_safe",
        taint=(),
        retrieval_order=index,
    )


def test_workset_chunks_budget_over_complete_serialized_request() -> None:
    from novel_agent.services.claim_support import (
        SEMANTIC_SUPPORT_SERIALIZED_REQUEST_TOKEN_BUDGET,
    )

    long_text = (
        "她望向远方, 山峦起伏如墨色长卷, 云海翻涌在峰顶之间, 少年背负长剑踏上山道。"
        "山谷中传来龙吟, 惊起一群飞鸟, 他握紧剑柄, 心中默念老师的教诲。"
    ) * 4
    slices = tuple(
        _slice_for_text(
            text=long_text,
            index=index,
            chapter=index % 5,
        )
        for index in range(120)
    )
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    entry: dict[str, object] = {
        "need_id": knowledge.need_id.root,
        "need_type": knowledge.need_type,
        "query_intent": knowledge.query_intent.value,
        "query_text": knowledge.query_text,
        "why_needed": knowledge.why_needed,
        "required_need_facets": [
            {
                "need_facet_id": facet.need_facet_id.root,
                "facet_kind": facet.facet_kind.value,
                "expected_claim_scope": facet.expected_claim_scope.value,
            }
            for facet in knowledge.need_facets
        ],
    }
    chunks = TrustedClaimSupportProducer()._workset_chunks(
        slices,
        task=task,
        need=knowledge,
        entry=entry,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
    )
    assert len(chunks) > 1
    for chunk in chunks:
        prompt = TrustedClaimSupportProducer()._serialized_request_input(
            task=task,
            basis_commit_id=world.source_commit,
            basis_snapshot_id=CROSS_NEED_SNAPSHOT,
            entry=entry,
            need=knowledge,
            slices=chunk,
            template=_MULTI_SLICE_PROMPT_TEMPLATE,
        )
        estimate = TrustedClaimSupportProducer._estimate_prompt_tokens(prompt)
        assert (
            estimate
            <= SEMANTIC_SUPPORT_SERIALIZED_REQUEST_TOKEN_BUDGET
            - SEMANTIC_SUPPORT_MULTI_SLICE_PROPOSAL_MAX_OUTPUT_TOKENS
        )
    union = {item.slice_id for chunk in chunks for item in chunk}
    assert union == {item.slice_id for item in slices}


def test_closure_based_early_exit_stops_remaining_chunks() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    paragraph = (
        "徐有容身在南方, 与陈长生并无直接交谈, 不能声称她此刻的心意, 只能记录她与"
        "陈长生的婚约依然存在, 两人隔着千山万水, 书信往来需要数月之久。"
    ) * 2
    paragraphs = "\n".join(f"第{index}段{paragraph}" for index in range(140))
    block = _unit(
        world,
        unit_id="grounded.block.test.96.0",
        text=paragraphs,
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="early.exit",
    )
    producer_prep = TrustedClaimSupportProducer()
    slices, _res, _slice_rows = producer_prep._resolve_exact_slices(
        (block,),
        need=knowledge,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        checkpoint_chapter=task.checkpoint_chapter,
        origin_need_ids={},
    )
    workset, _dropped, _rows = producer_prep._pack_workset(
        slices,
        need=knowledge,
        token_counter=WriterContextAssembler().count_tokens,
    )
    assert len(workset) > 30
    first_slice = workset[0]
    endpoint = _SemanticSupportEndpoint(
        (
            {
                "claims": [],
                "insufficient_need_ids": [knowledge.need_id.root],
            },
            {
                "claims": [
                    {
                        "need_id": knowledge.need_id.root,
                        "need_facet_ids": _atom_required_facets(knowledge),
                        "slice_unit_ids": [first_slice.slice_id.root],
                        "claim_text": "徐有容身在南方。",
                    }
                ],
                "insufficient_need_ids": [],
            },
            {"decisions": [{"claim_index": 0, "supports": True}]},
        )
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        semantic_gateway=_gateway_for_endpoint(endpoint),
        progress_writer=progress.append,
    )
    groups, _variants, _receipts, _attestations = producer.produce(
        task=task,
        units=(block,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={block.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    # The workset spans two serialized-request chunks.  The single-slice
    # probe returns insufficient, the first multi-slice chunk's verified
    # claim closes the Need, and the second chunk is never scheduled:
    # exactly one multi-slice proposal request and one verification.
    proposal_events = [event for event in progress if event.get("stage") == "proposal"]
    assert [event.get("status") for event in proposal_events] == ["completed"]
    assert [event.get("batch_index") for event in proposal_events] == [2]
    assert len(endpoint.requests) == 3
    assert any(group.producer.endswith(".synthesized") for group in groups)
    assert not any(
        event.get("stage") == "proposal" and event.get("status") == "failed" for event in progress
    )


def test_failed_proposal_event_retains_sanitized_diagnostics_and_failed_input_ref() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.97.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="diag.fail",
    )
    endpoint = _SemanticSupportEndpoint(
        (RuntimeError("boom at http://user:secret-token@127.0.0.1:8002/v1"),)
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        semantic_gateway=_gateway_for_endpoint(endpoint),
        progress_writer=progress.append,
    )
    producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    failed = next(
        event
        for event in progress
        if event.get("stage") == "proposal" and event.get("status") == "failed"
    )
    failed_call = failed["failed_call"]
    assert isinstance(failed_call, dict)
    assert failed_call["category"] == "transport"
    assert "127.0.0.1" not in str(failed_call["detail"])
    assert "secret-token" not in str(failed_call["detail"])
    failed_ref = failed_call["failed_input_ref"]
    assert isinstance(failed_ref, dict)
    assert failed_ref["media_type"] == "application/vnd.novel-agent.support-proposal-prompt+text"
    assert failed_ref["byte_length"] == failed["prompt_bytes"]
    for budget_key in (
        "estimated_input_tokens",
        "max_output_tokens",
        "timeout_seconds",
        "applied_input_token_budget",
    ):
        value = failed[budget_key]
        assert isinstance(value, (int, float))
        assert value >= 1


def test_classify_failed_call_distinguishes_categories() -> None:
    import httpx

    producer = TrustedClaimSupportProducer()
    http_error = RuntimeError("chat completion HTTP 400")
    classified = producer._classify_failed_call(http_error)
    assert classified.category == "http_status"
    assert classified.status_code == 400
    timeout_error = RuntimeError("chat completion request failed after all retries")
    timeout_error.__cause__ = httpx.ConnectTimeout("connect timeout")
    classified = producer._classify_failed_call(timeout_error)
    assert classified.category == "connect_read_timeout"
    truncation = RuntimeError("chat completion was truncated by output length limit")
    assert producer._classify_failed_call(truncation).category == "output_length_truncation"
    invalid_json = RuntimeError("chat completion response is not valid JSON")
    assert producer._classify_failed_call(invalid_json).category == "invalid_json"
    missing = RuntimeError("chat completion response is missing choices")
    assert producer._classify_failed_call(missing).category == "missing_structured_content"


def test_workset_chunks_single_chunk_when_within_budget() -> None:
    slices = tuple(
        _slice_for_text(
            text="短句。",
            index=index,
            chapter=1,
        )
        for index in range(3)
    )
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    entry: dict[str, object] = {
        "need_id": knowledge.need_id.root,
        "need_type": knowledge.need_type,
        "query_intent": knowledge.query_intent.value,
        "query_text": knowledge.query_text,
        "why_needed": knowledge.why_needed,
        "required_need_facets": [],
    }
    chunks = TrustedClaimSupportProducer()._workset_chunks(
        slices,
        task=task,
        need=knowledge,
        entry=entry,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
    )
    assert len(chunks) == 1
    assert chunks[0] == tuple(slices)


def test_chunk_audit_rows_carry_chunk_index_and_origins() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    unit_a = _unit(
        world,
        unit_id="grounded.block.test.98.0",
        text="徐有容身在南方, 与陈长生并无直接交谈。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="chunk.audit",
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        progress_writer=progress.append,
        pre_proposal_trace=True,
    )
    producer.produce(
        task=task,
        units=(unit_a,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit_a.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    audit = _audit_rows(progress)
    chunk_rows = audit["semantic_chunks_exposed"]
    assert chunk_rows
    assert all(row["chunk_index"] == 0 for row in chunk_rows)
    assert all(row["origin_need_ids"] == [knowledge.need_id.root] for row in chunk_rows)
    assert all(row["drop_reason"] is None for row in chunk_rows)


def test_unit_family_id_parent_units_lineage_path() -> None:
    _task, world = _cross_need_task()
    lineage = _unit(
        world,
        unit_id="anchor.relation.test.99.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="family.parents",
        kind=RetrievalUnitKind.RELATION_ANCHOR,
        span=False,
        parent_unit_ids=(StableId("grounded.block.test.99.0"),),
    )
    assert TrustedClaimSupportProducer._unit_family_id(lineage) == "grounded.block.test.99.0"


def test_resolution_rows_record_snapshot_mismatch() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    stale_snapshot = _unit(
        world,
        unit_id="grounded.block.test.100.0",
        text="徐有容身在南方。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=10,
        seed="resolve.snapshot",
        snapshot_id=StableId("snapshot.test.stale"),
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        progress_writer=progress.append,
        pre_proposal_trace=True,
    )
    producer.produce(
        task=task,
        units=(stale_snapshot,),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={stale_snapshot.unit_id: (knowledge.need_id,)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    resolution = _audit_rows(progress)["l0_blocks_spans_resolved"]
    reasons = {str(row["unit_id"]): str(row["drop_reason"]) for row in resolution}
    assert reasons[stale_snapshot.unit_id.root] == "filtered:snapshot_mismatch"


def test_corridor_diversity_collapse_drops_duplicate_evidence_after_family_keep() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    blocks: list[RetrievalUnit] = []
    for index in range(22):
        text = f"第{index}章段落徐有容身在南方与陈长生并无直接交谈。"
        unit = _unit(
            world,
            unit_id=f"grounded.block.test.dv.{index}.0",
            text=text,
            entity_ids=(ENTITY_LIN_CHE,),
            chapter=index + 1,
            seed=f"dv.{index}",
        )
        blocks.append(unit)
    # A second unit of the same chapter carries the same evidence refs; it is
    # a family duplicate and is collapsed before the handle budget applies.
    duplicate = _unit(
        world,
        unit_id="grounded.block.test.dv.dup.0",
        text=blocks[0].text,
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=1,
        seed="dv.dup",
    )
    progress: list[Mapping[str, object]] = []
    producer = TrustedClaimSupportProducer(
        progress_writer=progress.append,
        pre_proposal_trace=True,
    )
    producer.produce(
        task=task,
        units=(*blocks, duplicate),
        needs=(knowledge,),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
        unit_need_ids={unit.unit_id: (knowledge.need_id,) for unit in (*blocks, duplicate)},
        token_counter=WriterContextAssembler().count_tokens,
    )
    audit = _audit_rows(progress)
    diversity = audit["deduplicated_diversified_handle_pool"]
    reasons = {str(row["unit_id"]): str(row["drop_reason"]) for row in diversity}
    assert reasons[duplicate.unit_id.root] == "family_collapsed:duplicate_representation"
    bounded = audit["bounded_selected_handles"]
    bounded_ids = {str(row["unit_id"]) for row in bounded if row["drop_reason"] is None}
    assert blocks[0].unit_id.root in bounded_ids
    assert duplicate.unit_id.root not in bounded_ids


def test_evidence_ref_covered_different_object_hash_and_other_spanless() -> None:
    from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus

    base = EvidenceRef(
        evidence_id=StableId("evidence.test.base"),
        root_hash=ArtifactId("sha256:" + "1" * 64),
        object_hash=ArtifactId("sha256:" + "2" * 64),
        chapter_id=StableId("chapter.test.1"),
        span=TextSpanRef(block_id=StableId("block.test.1"), start=0, end=40),
        quote_hash=quote_hash("x" * 40),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=CommitId("sha256:" + "3" * 64),
    )
    other_hash = base.model_copy(
        update={
            "evidence_id": StableId("evidence.test.other.hash"),
            "object_hash": ArtifactId("sha256:" + "9" * 64),
        }
    )
    assert not TrustedClaimSupportProducer._evidence_ref_covered(other_hash, (base,))
    other_spanless = base.model_copy(
        update={
            "evidence_id": StableId("evidence.test.other.spanless"),
            "object_hash": base.object_hash,
            "span": None,
        }
    )
    assert not TrustedClaimSupportProducer._evidence_ref_covered(base, (other_spanless,))


def test_workset_chunks_oversized_single_slice_stays_alone_in_chunk() -> None:
    huge = _slice_for_text(
        text="陈长生与黑龙对峙于山谷, 双方僵持不下, 剑光如虹。" * 1500,
        index=0,
        chapter=1,
    )
    small = _slice_for_text(
        text="短句。",
        index=1,
        chapter=2,
    )
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    entry: dict[str, object] = {
        "need_id": knowledge.need_id.root,
        "need_type": knowledge.need_type,
        "query_intent": knowledge.query_intent.value,
        "query_text": knowledge.query_text,
        "why_needed": knowledge.why_needed,
        "required_need_facets": [],
    }
    chunks = TrustedClaimSupportProducer()._workset_chunks(
        (huge, small),
        task=task,
        need=knowledge,
        entry=entry,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
    )
    assert len(chunks) == 2
    assert chunks[0] == (huge,)
    assert chunks[1] == (small,)


def test_classify_failed_call_http_status_cause_chain_and_retry_exhaustion() -> None:
    import httpx

    producer = TrustedClaimSupportProducer()
    error = RuntimeError("chat completion request failed after all retries")
    error.__cause__ = httpx.HTTPStatusError(
        "400",
        request=httpx.Request("POST", "http://127.0.0.1:8002/v1/chat/completions"),
        response=httpx.Response(429),
    )
    classified = producer._classify_failed_call(error)
    assert classified.category == "http_status"
    assert classified.status_code == 429

    class _RetriedEndpoint(_SemanticSupportEndpoint):
        attempts: list[object] = [object()]  # noqa: RUF012

    endpoint = _RetriedEndpoint(())
    gateway = _gateway_for_endpoint(endpoint)
    retried = TrustedClaimSupportProducer(semantic_gateway=gateway)._classify_failed_call(
        RuntimeError("boom")
    )
    assert retried.category == "retry_exhausted"
    assert retried.retry_count == 1


def test_endpoint_adapter_missing_role_raises_routing_error() -> None:
    from novel_agent.services.model_gateway import ModelGateway, ModelRoutingError

    endpoint = _SemanticSupportEndpoint(())
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="semantic-support-test",
                model_name=endpoint.model,
                adapter=endpoint,
            ),
        )
    )
    with pytest.raises(ModelRoutingError):
        gateway.endpoint_adapter(ModelRole.IMPLEMENTATION)


def test_evidence_diverse_pool_drops_covered_duplicate_from_other_family() -> None:
    _task, world = _cross_need_task()
    first = _unit(
        world,
        unit_id="grounded.block.test.101.0",
        text="徐有容身在南方与陈长生并无直接交谈。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=18,
        seed="diverse.drop.a",
    )
    shared_ref = first.evidence_refs[0]
    second = _unit(
        world,
        unit_id="grounded.block.test.102.0",
        text="陈长生在学院求学。",
        entity_ids=(ENTITY_LIN_CHE,),
        chapter=19,
        seed="diverse.drop.b",
    )
    second_ref = shared_ref.model_copy(
        update={
            "span": TextSpanRef(
                block_id=StableId("block.test.19"),
                start=0,
                end=len(second.text),
            )
        }
    )
    second = second.model_copy(update={"evidence_refs": (second_ref,)})
    producer = TrustedClaimSupportProducer()
    canonical, _family_collapsed = producer._family_canonicalize((first, second))
    assert len(canonical) == 2
    diversified = producer._evidence_diverse_pool(canonical, ())
    assert [unit.unit_id for unit in diversified] == [first.unit_id]


def test_evidence_ref_covered_same_evidence_id_short_circuits() -> None:
    from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus

    base = EvidenceRef(
        evidence_id=StableId("evidence.test.same"),
        root_hash=ArtifactId("sha256:" + "1" * 64),
        object_hash=ArtifactId("sha256:" + "2" * 64),
        chapter_id=StableId("chapter.test.1"),
        span=TextSpanRef(block_id=StableId("block.test.1"), start=0, end=40),
        quote_hash=quote_hash("x" * 40),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=CommitId("sha256:" + "3" * 64),
    )
    assert TrustedClaimSupportProducer._evidence_ref_covered(base, (base,))


def test_classify_failed_call_walks_multi_level_cause_chain() -> None:
    import httpx

    error = RuntimeError("chat completion request failed after all retries")
    mid = RuntimeError("wrapped endpoint failure")
    error.__cause__ = mid
    mid.__cause__ = httpx.ReadTimeout("read timeout")
    classified = TrustedClaimSupportProducer()._classify_failed_call(error)
    assert classified.category == "connect_read_timeout"


def test_workset_chunks_empty_workset_returns_no_chunks() -> None:
    task, world = _cross_need_task()
    knowledge = _generated_need(task, world, "knowledge_boundary")
    entry: dict[str, object] = {
        "need_id": knowledge.need_id.root,
        "need_type": knowledge.need_type,
        "query_intent": knowledge.query_intent.value,
        "query_text": knowledge.query_text,
        "why_needed": knowledge.why_needed,
        "required_need_facets": [],
    }
    chunks = TrustedClaimSupportProducer()._workset_chunks(
        (),
        task=task,
        need=knowledge,
        entry=entry,
        basis_commit_id=world.source_commit,
        basis_snapshot_id=CROSS_NEED_SNAPSHOT,
    )
    assert chunks == ()
