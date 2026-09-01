from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from typing import Any

import pytest

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import (
    ExpectedClaimScope,
    FacetEvidenceRequirement,
    NeedCompletionSpec,
    NeedFacet,
    NeedFacetKind,
    NeedGapPolicy,
    NeedUncertaintyPolicy,
)
from novel_agent.domain.model_calls import ModelRole, ModelUsage, ProviderModelResult
from novel_agent.services.evidence_first_writer_context_assembler import (
    NeedEvidenceSelection,
    SliceSelectionTrace,
)
from novel_agent.services.evidence_slice_resolver import EvidenceSliceResolver
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.need_evidence_semantic_judgment import NeedEvidenceSemanticJudge
from tests.unit.test_evidence_first_writer_context import _block, _need, _text_root


class _JudgeEndpoint:
    is_external = False
    model = "semantic-judge-test"
    max_retries = 0

    def __init__(self, mode: str = "supported", fail_on: int | None = None) -> None:
        self.mode = mode
        self.fail_on = fail_on
        self.requests: list[Any] = []

    async def generate(self, request: Any) -> ProviderModelResult:
        self.requests.append(request)
        if self.fail_on is not None and len(self.requests) == self.fail_on:
            raise TimeoutError("test semantic judge timeout")
        facet_match = re.search(r"Facets: ([^ ]+)", request.prompt)
        facet_id = facet_match.group(1) if facet_match else "facet.semantic"
        slice_ids = re.findall(r"SLICE ([^:]+):", request.prompt)
        supporting: list[str] = []
        partial: list[str] = []
        unsupported: list[str] = []
        if self.mode == "supported":
            supporting = slice_ids
            status = "SUPPORTED"
        elif self.mode == "partial":
            partial = slice_ids[:1]
            unsupported = slice_ids[1:]
            status = "PARTIAL"
        else:
            unsupported = slice_ids
            status = "UNSUPPORTED"
        need_match = re.search(r"(?m)^Need ([^:]+):", request.prompt)
        assert need_match is not None
        payload = {
            "decisions": [
                {
                    "need_id": need_match.group(1),
                    "need_facet_id": facet_id,
                    "status": status,
                    "supporting_slice_ids": supporting,
                    "partial_slice_ids": partial,
                    "unsupported_slice_ids": unsupported,
                    "reason": "test judgment",
                }
            ]
        }
        return ProviderModelResult(
            text=json.dumps(payload),
            model_version=self.model,
            usage=ModelUsage(
                input_tokens=max(1, len(request.prompt) // 4),
                output_tokens=32,
                cost_usd=Decimal("0"),
            ),
        )


def _selection(slice_count: int = 7) -> NeedEvidenceSelection:
    need = _need("need.semantic", query="teacher 当前伤势状态是什么?")
    facet_id = StableId("facet.semantic.current")
    facet = NeedFacet(
        need_facet_id=facet_id,
        need_id=need.need_id,
        facet_kind=NeedFacetKind.CURRENT_STATE,
        expected_claim_scope=ExpectedClaimScope.CURRENT,
        derivation_refs=(StableId("derivation.semantic"),),
        producer="test",
        producer_version="v1",
        information_scope="writer_safe",
    )
    spec = NeedCompletionSpec(
        need_id=need.need_id,
        required_need_facet_ids=(facet_id,),
        irreducible_need_facet_ids=(facet_id,),
        evidence_requirement_by_facet={
            facet_id.root: FacetEvidenceRequirement.TRACEABLE_CUTOFF_SOURCE
        },
        uncertainty_policy=NeedUncertaintyPolicy.ALLOW_GAP_ONLY,
        gap_policy=NeedGapPolicy.EMIT_TYPED_GAP,
        producer="test",
        producer_version="v1",
    )
    need = need.model_copy(update={"need_facets": (facet,), "completion_spec": spec})
    text_root = _text_root(
        (_block("\n".join(f"第{i}段: teacher 的伤势记录 {i}。" for i in range(slice_count))),)
    )
    slices = EvidenceSliceResolver().resolve_block(
        text_root.chapters[0].scenes[0].blocks[0],
        source_commit=need.base_commit,
        snapshot_id=StableId("snapshot.evidence-first.test"),
        access_scope="writer_safe",
    )[:slice_count]
    traces = tuple(
        SliceSelectionTrace(
            slice_id=slice_.slice_id,
            unit_id=StableId(f"unit.semantic.{index}"),
            route_channel="anchor_bm25",
            fused_rank=index + 1,
            selection_reason="test",
        )
        for index, slice_ in enumerate(slices)
    )
    return NeedEvidenceSelection(need=need, selections=traces, slices=slices)


def _judge(
    endpoint: _JudgeEndpoint,
    *,
    max_input_tokens: int = 12_000,
) -> NeedEvidenceSemanticJudge:
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="semantic-judge-test",
                model_name=endpoint.model,
                adapter=endpoint,
            ),
        )
    )
    return NeedEvidenceSemanticJudge(
        gateway,
        max_input_tokens=max_input_tokens,
        max_output_tokens=2_048,
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    (("supported", "SUPPORTED"), ("partial", "PARTIAL"), ("unsupported", "UNSUPPORTED")),
)
def test_judge_classifies_complete_finite_slice_set(mode: str, expected: str) -> None:
    endpoint = _JudgeEndpoint(mode)
    result = _judge(endpoint).judge((_selection(7),))
    receipt = result.receipts[0]
    assert receipt.status.value == expected
    assert len(receipt.evaluated_slice_ids) == 7
    assert result.call_count == 1


class _DuplicateUnsupportedEndpoint(_JudgeEndpoint):
    async def generate(self, request: Any) -> ProviderModelResult:
        result = await super().generate(request)
        payload = json.loads(result.text)
        unsupported = list(payload["decisions"][0]["unsupported_slice_ids"])
        payload["decisions"][0]["unsupported_slice_ids"] = [*unsupported, unsupported[-1]]
        payload["decisions"][0]["status"] = "UNSUPPORTED"
        payload["decisions"][0]["supporting_slice_ids"] = []
        payload["decisions"][0]["partial_slice_ids"] = []
        return result.model_copy(update={"text": json.dumps(payload)})


def test_judge_dedupes_duplicate_slice_ids_in_one_bucket() -> None:
    endpoint = _DuplicateUnsupportedEndpoint("unsupported")
    result = _judge(endpoint).judge((_selection(3),))
    assert result.failed_batch_count == 0
    receipt = result.receipts[0]
    assert receipt.status.value == "UNSUPPORTED"
    assert len(receipt.evaluated_slice_ids) == 3
    assert len(receipt.unsupported_slice_ids) == 3


class _TypoSupportingEndpoint(_JudgeEndpoint):
    """C95 落落 batch: one supporting SHA mistyped, one unsupported id duplicated."""

    async def generate(self, request: Any) -> ProviderModelResult:
        result = await super().generate(request)
        payload = json.loads(result.text)
        decision = payload["decisions"][0]
        supporting = list(decision["supporting_slice_ids"])
        unsupported = list(decision["unsupported_slice_ids"])
        typed = supporting[0]
        mistyped = typed[:40] + "6" + typed[40:]
        decision["supporting_slice_ids"] = [mistyped, *supporting[1:]]
        if unsupported:
            decision["unsupported_slice_ids"] = [*unsupported, unsupported[-1]]
        return result.model_copy(update={"text": json.dumps(payload)})


def test_judge_keeps_valid_supporting_ids_when_one_slice_id_is_mistyped() -> None:
    endpoint = _TypoSupportingEndpoint("supported")
    result = _judge(endpoint).judge((_selection(4),))
    assert result.failed_batch_count == 0
    receipt = result.receipts[0]
    assert receipt.status.value == "SUPPORTED"
    assert len(receipt.evaluated_slice_ids) == 4
    assert len(receipt.supporting_slice_ids) == 3
    assert len(receipt.unsupported_slice_ids) == 1
    assert set(receipt.supporting_slice_ids).issubset(receipt.evaluated_slice_ids)
    assert set(receipt.unsupported_slice_ids).issubset(receipt.evaluated_slice_ids)


class _TruncatedNeedIdEndpoint(_JudgeEndpoint):
    """C95 location batch: model dropped a middle run of hex from need_id."""

    async def generate(self, request: Any) -> ProviderModelResult:
        result = await super().generate(request)
        payload = json.loads(result.text)
        decision = payload["decisions"][0]
        need_id = decision["need_id"]
        decision["need_id"] = need_id[:24] + need_id[-12:]
        return result.model_copy(update={"text": json.dumps(payload)})


def test_judge_applies_decision_when_need_id_is_truncated_but_facet_id_matches() -> None:
    endpoint = _TruncatedNeedIdEndpoint("partial")
    result = _judge(endpoint).judge((_selection(4),))
    assert result.failed_batch_count == 0
    receipt = result.receipts[0]
    assert receipt.status.value == "PARTIAL"
    assert len(receipt.evaluated_slice_ids) == 4
    assert len(receipt.partial_slice_ids) == 1
    assert len(receipt.unsupported_slice_ids) == 3


def test_judge_async_runs_on_the_already_running_event_loop() -> None:
    endpoint = _JudgeEndpoint("supported")
    judge = _judge(endpoint)
    selection = _selection(2)

    async def _from_loop() -> object:
        return await judge.judge_async((selection,))

    result = asyncio.run(_from_loop())
    assert endpoint.requests
    assert result.receipts[0].status.value == "SUPPORTED"
    assert result.failed_batch_count == 0


def test_sync_judge_refuses_a_running_event_loop() -> None:
    judge = _judge(_JudgeEndpoint("supported"))

    async def _from_loop() -> object:
        return judge.judge((_selection(2),))

    with pytest.raises(RuntimeError, match="await judge_async"):
        asyncio.run(_from_loop())


def test_judge_emits_unresolved_receipt_without_selected_slices() -> None:
    endpoint = _JudgeEndpoint()
    selection = _selection(1).model_copy(update={"selections": (), "slices": ()})

    result = _judge(endpoint).judge((selection,))

    assert result.call_count == 0
    assert len(result.receipts) == 1
    assert result.receipts[0].status.value == "UNRESOLVED"
    assert result.receipts[0].reason == "no_selected_evidence"


def _selection_named(name: str, slice_count: int = 3) -> NeedEvidenceSelection:
    selection = _selection(slice_count)
    need_id = StableId(f"need.semantic.{name}")
    facet_id = StableId(f"facet.semantic.{name}")
    facet = selection.need.need_facets[0].model_copy(
        update={"need_id": need_id, "need_facet_id": facet_id}
    )
    spec = selection.need.completion_spec
    assert spec is not None
    requirement = next(iter(spec.evidence_requirement_by_facet.values()))
    spec = spec.model_copy(
        update={
            "need_id": need_id,
            "required_need_facet_ids": (facet_id,),
            "irreducible_need_facet_ids": (facet_id,),
            "evidence_requirement_by_facet": {facet_id.root: requirement},
        }
    )
    need = selection.need.model_copy(
        update={"need_id": need_id, "need_facets": (facet,), "completion_spec": spec}
    )
    return selection.model_copy(update={"need": need})


def test_judge_issues_one_model_call_per_required_facet() -> None:
    selection = _selection(3)
    first = selection.need.need_facets[0]
    second = first.model_copy(
        update={
            "need_facet_id": StableId("facet.semantic.causal"),
            "facet_kind": NeedFacetKind.CAUSAL_HISTORY,
        }
    )
    spec = selection.need.completion_spec
    assert spec is not None
    requirement = next(iter(spec.evidence_requirement_by_facet.values()))
    spec = spec.model_copy(
        update={
            "required_need_facet_ids": (first.need_facet_id, second.need_facet_id),
            "irreducible_need_facet_ids": (first.need_facet_id, second.need_facet_id),
            "evidence_requirement_by_facet": {
                first.need_facet_id.root: requirement,
                second.need_facet_id.root: requirement,
            },
        }
    )
    need = selection.need.model_copy(
        update={"need_facets": (first, second), "completion_spec": spec}
    )
    endpoint = _JudgeEndpoint("supported")
    result = _judge(endpoint).judge((selection.model_copy(update={"need": need}),))
    assert result.call_count == 2
    assert result.failed_batch_count == 0
    assert {receipt.need_facet_id.root for receipt in result.receipts} == {
        first.need_facet_id.root,
        second.need_facet_id.root,
    }
    assert all(receipt.status.value == "SUPPORTED" for receipt in result.receipts)


def test_judge_issues_one_model_call_per_need() -> None:
    endpoint = _JudgeEndpoint("supported")
    result = _judge(endpoint).judge((_selection_named("left"), _selection_named("right")))
    assert result.call_count == 2
    assert result.failed_batch_count == 0
    assert {receipt.need_id.root for receipt in result.receipts} == {
        "need.semantic.left",
        "need.semantic.right",
    }
    assert all(receipt.status.value == "SUPPORTED" for receipt in result.receipts)


def test_judge_is_not_limited_to_four_slices_and_can_use_multiple_batches() -> None:
    endpoint = _JudgeEndpoint()
    result = _judge(endpoint, max_input_tokens=256).judge((_selection(8),))
    receipt = result.receipts[0]
    assert len(receipt.evaluated_slice_ids) == 8
    assert len(endpoint.requests) > 1
    assert receipt.status.value == "SUPPORTED"
    assert result.planned_batch_count == result.call_count
    assert result.completed_batch_count == result.call_count
    assert result.failed_batch_count == 0


def test_failed_batch_marks_aggregate_unresolved() -> None:
    endpoint = _JudgeEndpoint(fail_on=2)
    result = _judge(endpoint, max_input_tokens=256).judge((_selection(8),))
    receipt = result.receipts[0]
    assert any(batch.status.value == "failed" for batch in result.batch_receipts)
    assert receipt.status.value == "UNRESOLVED"
    assert result.planned_batch_count == result.completed_batch_count + result.failed_batch_count
    assert result.failed_batch_count == 1
