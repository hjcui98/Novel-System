"""Bounded semantic relevance checks for selected exact L0 evidence.

This service is deliberately narrower than Claim Support: it only asks whether
the supplied raw slices answer a public Need facet.  It never writes Canon,
creates a claim, reads Gold, or changes the exact evidence set.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import StableId
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.writer_context import (
    NeedEvidenceJudgmentBatchReceipt,
    NeedEvidenceJudgmentBatchStatus,
    NeedEvidenceSemanticStatus,
    NeedFacetSemanticReceipt,
)
from novel_agent.services.content_addressing import content_id
from novel_agent.services.evidence_first_writer_context_assembler import NeedEvidenceSelection
from novel_agent.services.model_gateway import ModelGateway

TokenCounter = Callable[[str], int]


class SemanticFacetDecision(DomainModel):
    """Structured model answer for one Need facet within one batch."""

    need_id: StableId
    need_facet_id: StableId
    status: Literal["SUPPORTED", "PARTIAL", "UNSUPPORTED"]
    supporting_slice_ids: tuple[StableId, ...] = ()
    partial_slice_ids: tuple[StableId, ...] = ()
    unsupported_slice_ids: tuple[StableId, ...] = ()
    reason: str = Field(default="", max_length=4096)

    @model_validator(mode="after")
    def validate_buckets(self) -> SemanticFacetDecision:
        buckets = (
            self.supporting_slice_ids,
            self.partial_slice_ids,
            self.unsupported_slice_ids,
        )
        values = tuple(item for bucket in buckets for item in bucket)
        if len(values) != len(set(values)):
            raise ValueError("semantic decision slice buckets must be disjoint")
        if self.status == "SUPPORTED" and not self.supporting_slice_ids:
            raise ValueError("SUPPORTED decision requires supporting slices")
        if self.status == "PARTIAL" and (self.supporting_slice_ids or not self.partial_slice_ids):
            raise ValueError("PARTIAL decision requires partial slices only")
        if self.status == "UNSUPPORTED" and (
            self.supporting_slice_ids or self.partial_slice_ids or not self.unsupported_slice_ids
        ):
            raise ValueError("UNSUPPORTED decision requires unsupported slices only")
        return self


class SemanticJudgmentBatchOutput(DomainModel):
    decisions: tuple[SemanticFacetDecision, ...] = ()


@dataclass(frozen=True, slots=True)
class _WorkItem:
    need_id: StableId
    semantic_question: str
    facets: tuple[tuple[StableId, str], ...]
    slices: tuple[tuple[StableId, str], ...]


@dataclass(frozen=True, slots=True)
class _WorkChunk:
    work: _WorkItem
    slices: tuple[tuple[StableId, str], ...]


@dataclass(slots=True)
class _AggregateState:
    evaluated: list[StableId]
    supporting: list[StableId]
    partial: list[StableId]
    unsupported: list[StableId]
    batch_ids: list[StableId]
    failed: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class NeedEvidenceSemanticResult:
    receipts: tuple[NeedFacetSemanticReceipt, ...]
    batch_receipts: tuple[NeedEvidenceJudgmentBatchReceipt, ...]

    @property
    def call_count(self) -> int:
        return len(self.batch_receipts)

    @property
    def planned_batch_count(self) -> int:
        """Number of capacity-planned batches, including typed failures."""
        return len(self.batch_receipts)

    @property
    def completed_batch_count(self) -> int:
        return sum(
            batch.status is NeedEvidenceJudgmentBatchStatus.COMPLETED
            for batch in self.batch_receipts
        )

    @property
    def failed_batch_count(self) -> int:
        return sum(
            batch.status is NeedEvidenceJudgmentBatchStatus.FAILED for batch in self.batch_receipts
        )


class NeedEvidenceSemanticJudge:
    """Judge all selected evidence using token-capacity-driven batches."""

    version = "need_evidence_semantic_judge.v1"

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        max_input_tokens: int = 12_000,
        max_output_tokens: int = 2_048,
        token_counter: TokenCounter | None = None,
    ) -> None:
        if max_input_tokens < 256:
            raise ValueError("semantic judge input budget must be at least 256 tokens")
        if max_output_tokens < 256:
            raise ValueError("semantic judge output budget must be at least 256 tokens")
        self._gateway = gateway
        self._max_input_tokens = max_input_tokens
        self._max_output_tokens = max_output_tokens
        self._count = token_counter or self._default_token_count

    def judge(
        self,
        selections: tuple[NeedEvidenceSelection, ...],
    ) -> NeedEvidenceSemanticResult:
        works = self._work_items(selections)
        chunks = self._chunks(works)
        batches = self._pack_batches(chunks)
        accumulated: dict[tuple[StableId, StableId], _AggregateState] = {
            (work.need_id, facet_id): _AggregateState(
                evaluated=[],
                supporting=[],
                partial=[],
                unsupported=[],
                batch_ids=[],
            )
            for work in works
            for facet_id, _kind in work.facets
        }
        batch_receipts: list[NeedEvidenceJudgmentBatchReceipt] = []
        for index, batch in enumerate(batches, start=1):
            batch_id = StableId(f"semantic-judge.batch.{index}"[:128])
            prompt = self._prompt(batch)
            request_id = StableId(
                f"semantic-judge.{content_id({'batch': index, 'prompt': prompt}).root[7:]}"[:128]
            )
            input_tokens = self._count(prompt)
            batch_keys = tuple(
                (chunk.work.need_id, facet_id)
                for chunk in batch
                for facet_id, _kind in chunk.work.facets
            )
            for key in batch_keys:
                accumulated[key].batch_ids.append(batch_id)
            request = ModelRequest(
                request_id=request_id,
                run_id=next(iter(selections)).need.run_id,
                task_id=next(iter(selections)).need.task_id,
                model_role=ModelRole.BATCH_TEST,
                purpose=ModelCallPurpose.BATCH_TEST,
                trace_id=f"need-evidence-semantic:{request_id.root}",
                prompt=prompt,
                max_output_tokens=self._max_output_tokens,
                timeout_seconds=900.0,
                enable_thinking=False,
                scheduling_stage="need_evidence_semantic_judge",
            )
            try:
                output, call = asyncio.run(
                    self._gateway.generate_structured(request, SemanticJudgmentBatchOutput)
                )
                self._apply_output(output, batch, accumulated)
                batch_receipts.append(
                    NeedEvidenceJudgmentBatchReceipt(
                        batch_id=batch_id,
                        request_id=request_id,
                        need_facet_ids=tuple(dict.fromkeys(key[1] for key in batch_keys)),
                        slice_ids=tuple(
                            dict.fromkeys(
                                slice_id for chunk in batch for slice_id, _ in chunk.slices
                            )
                        ),
                        status=NeedEvidenceJudgmentBatchStatus.COMPLETED,
                        input_tokens=call.usage.input_tokens,
                        output_tokens=call.usage.output_tokens,
                        model=call.model,
                        model_version=call.model_version,
                        endpoint=call.endpoint,
                    )
                )
            except Exception as error:
                reason = type(error).__name__
                for key in batch_keys:
                    accumulated[key].failed = True
                    accumulated[key].reason = reason
                batch_receipts.append(
                    NeedEvidenceJudgmentBatchReceipt(
                        batch_id=batch_id,
                        request_id=request_id,
                        need_facet_ids=tuple(dict.fromkeys(key[1] for key in batch_keys)),
                        slice_ids=tuple(
                            dict.fromkeys(
                                slice_id for chunk in batch for slice_id, _ in chunk.slices
                            )
                        ),
                        status=NeedEvidenceJudgmentBatchStatus.FAILED,
                        input_tokens=input_tokens,
                        output_tokens=0,
                        error_category=reason,
                    )
                )
        receipts = self._finalize(works, accumulated, batch_receipts)
        return NeedEvidenceSemanticResult(
            receipts=receipts,
            batch_receipts=tuple(batch_receipts),
        )

    def _work_items(
        self,
        selections: tuple[NeedEvidenceSelection, ...],
    ) -> tuple[_WorkItem, ...]:
        works: list[_WorkItem] = []
        for selection in selections:
            need = selection.need
            if need.requirement.value != "mandatory":
                continue
            required_ids = (
                need.completion_spec.required_need_facet_ids
                if need.completion_spec is not None
                else tuple(facet.need_facet_id for facet in need.need_facets)
            )
            facets = tuple(
                (facet.need_facet_id, facet.facet_kind.value)
                for facet in need.need_facets
                if facet.need_facet_id in required_ids
            )
            if not facets:
                continue
            by_id = {slice_.slice_id: slice_ for slice_ in selection.slices}
            ordered_ids = tuple(
                dict.fromkeys(
                    (
                        trace.slice_id
                        for trace in sorted(
                            selection.selections,
                            key=lambda item: (item.fused_rank, item.slice_id.root),
                        )
                        if trace.slice_id in by_id
                    ),
                )
            )
            ordered_ids = (
                *ordered_ids,
                *tuple(
                    slice_id
                    for slice_id in sorted(by_id, key=lambda item: item.root)
                    if slice_id not in ordered_ids
                ),
            )
            ordered_slices = tuple((slice_id, by_id[slice_id].text) for slice_id in ordered_ids)
            works.append(
                _WorkItem(
                    need_id=need.need_id,
                    semantic_question=need.semantic_question,
                    facets=facets,
                    slices=ordered_slices,
                )
            )
        return tuple(works)

    def _chunks(self, works: tuple[_WorkItem, ...]) -> tuple[_WorkChunk, ...]:
        chunks: list[_WorkChunk] = []
        for work in works:
            # Keep an empty-slice work in the aggregate even though it does
            # not produce a model batch.  The final receipt is UNRESOLVED
            # (``no_selected_evidence``), so model-driven packages still
            # account for every mandatory Need/facet without inventing a
            # semantic call or upgrading the structural GAP.
            if not work.slices:
                continue
            current: list[tuple[StableId, str]] = []
            for slice_item in work.slices:
                candidate = tuple((*current, slice_item))
                if current and (
                    self._count(self._prompt((_WorkChunk(work, candidate),)))
                    > self._max_input_tokens
                ):
                    chunks.append(_WorkChunk(work=work, slices=tuple(current)))
                    current = [slice_item]
                else:
                    current.append(slice_item)
            if current:
                chunks.append(_WorkChunk(work=work, slices=tuple(current)))
        return tuple(chunks)

    def _pack_batches(self, chunks: tuple[_WorkChunk, ...]) -> tuple[tuple[_WorkChunk, ...], ...]:
        batches: list[list[_WorkChunk]] = []
        for chunk in chunks:
            placed = False
            for batch in batches:
                if any(existing.work.need_id == chunk.work.need_id for existing in batch):
                    continue
                candidate = tuple((*batch, chunk))
                if self._count(self._prompt(candidate)) <= self._max_input_tokens:
                    batch.append(chunk)
                    placed = True
                    break
            if not placed:
                batches.append([chunk])
        return tuple(tuple(batch) for batch in batches)

    def _apply_output(
        self,
        output: SemanticJudgmentBatchOutput,
        batch: tuple[_WorkChunk, ...],
        accumulated: dict[tuple[StableId, StableId], _AggregateState],
    ) -> None:
        expected: dict[tuple[StableId, StableId], set[StableId]] = {
            (chunk.work.need_id, facet_id): {slice_id for slice_id, _ in chunk.slices}
            for chunk in batch
            for facet_id, _kind in chunk.work.facets
        }
        decisions = {(item.need_id, item.need_facet_id): item for item in output.decisions}
        if len(decisions) != len(output.decisions) or set(decisions) != set(expected):
            raise ValueError(
                "semantic judge output must contain exactly one decision per Need facet"
            )
        for key, decision in decisions.items():
            expected_slices = expected[key]
            buckets = {
                "supporting": set(decision.supporting_slice_ids),
                "partial": set(decision.partial_slice_ids),
                "unsupported": set(decision.unsupported_slice_ids),
            }
            classified = set().union(*buckets.values())
            if classified != expected_slices:
                raise ValueError("semantic judge output must classify every supplied slice exactly")
            state = accumulated[key]
            state.evaluated.extend(sorted(classified, key=lambda item: item.root))
            state.supporting.extend(sorted(buckets["supporting"], key=lambda item: item.root))
            state.partial.extend(sorted(buckets["partial"], key=lambda item: item.root))
            state.unsupported.extend(sorted(buckets["unsupported"], key=lambda item: item.root))
            state.reason = decision.reason

    def _finalize(
        self,
        works: tuple[_WorkItem, ...],
        accumulated: dict[tuple[StableId, StableId], _AggregateState],
        batches: list[NeedEvidenceJudgmentBatchReceipt],
    ) -> tuple[NeedFacetSemanticReceipt, ...]:
        receipts: list[NeedFacetSemanticReceipt] = []
        batch_by_id = {batch.batch_id: batch for batch in batches}
        for work in works:
            expected = {slice_id for slice_id, _ in work.slices}
            for facet_id, facet_kind in work.facets:
                state = accumulated[(work.need_id, facet_id)]
                evaluated = tuple(dict.fromkeys(state.evaluated))
                supporting = tuple(dict.fromkeys(state.supporting))
                partial = tuple(dict.fromkeys(state.partial))
                unsupported = tuple(dict.fromkeys(state.unsupported))
                complete = (
                    not state.failed
                    and set(evaluated) == expected
                    and set(supporting) | set(partial) | set(unsupported) == expected
                )
                if not work.slices:
                    status = NeedEvidenceSemanticStatus.UNRESOLVED
                    reason = "no_selected_evidence"
                elif not complete:
                    status = NeedEvidenceSemanticStatus.UNRESOLVED
                    reason = state.reason or "semantic_judgment_incomplete"
                elif supporting:
                    status = NeedEvidenceSemanticStatus.SUPPORTED
                    reason = state.reason or "at_least_one_slice_directly_answers_need"
                elif partial:
                    status = NeedEvidenceSemanticStatus.PARTIAL
                    reason = state.reason or "evidence_answers_only_part_of_need"
                else:
                    status = NeedEvidenceSemanticStatus.UNSUPPORTED
                    reason = state.reason or "evidence_is_related_but_not_answering"
                batch_ids = tuple(dict.fromkeys(state.batch_ids))
                receipts.append(
                    NeedFacetSemanticReceipt(
                        need_id=work.need_id,
                        need_facet_id=facet_id,
                        facet_kind=facet_kind,
                        mandatory=True,
                        status=status,
                        evaluated_slice_ids=evaluated,
                        supporting_slice_ids=supporting,
                        partial_slice_ids=partial,
                        unsupported_slice_ids=unsupported,
                        reason=reason,
                        batch_receipt_ids=batch_ids,
                        judge_version=self.version,
                    )
                )
                for batch_id in batch_ids:
                    if batch_id not in batch_by_id:
                        raise ValueError(
                            f"semantic receipt references unknown batch: {batch_id.root}"
                        )
        return tuple(receipts)

    def _prompt(self, batch: tuple[_WorkChunk, ...]) -> str:
        lines = [
            "你是证据相关性裁决器。只判断给定截止点前的精确原文是否直接回答公开 Need 的 facet。",
            "不得补写事实、不得使用 Gold、不得读取未来正文、不得修改原文。",
            "对每个 Need/facet 的每个 slice 必须放入且只放入一个数组: "
            "supporting、partial、unsupported.",
            "SUPPORTED 表示至少一条原文直接回答该 facet; PARTIAL 表示只回答一部分;",
            "UNSUPPORTED 表示全部原文只是相关或没有回答。保留否定、不确定性和知情边界。",
            "输出单个 JSON 对象, 不要输出其它文字。",
            '{"decisions":[{"need_id":"...","need_facet_id":"...",'
            '"status":"SUPPORTED|PARTIAL|UNSUPPORTED",'
            '"supporting_slice_ids":["..."],"partial_slice_ids":["..."],'
            '"unsupported_slice_ids":["..."],"reason":"..."}]}',
        ]
        for chunk in batch:
            lines.append(f"Need {chunk.work.need_id.root}: {chunk.work.semantic_question}")
            lines.append(
                "Facets: "
                + "; ".join(f"{facet_id.root} ({kind})" for facet_id, kind in chunk.work.facets)
            )
            for slice_id, text in chunk.slices:
                lines.append(f"SLICE {slice_id.root}: {text}")
        return "\n".join(lines)

    @staticmethod
    def _default_token_count(text: str) -> int:
        return max(1, (len(text) + 3) // 4)


__all__ = [
    "NeedEvidenceSemanticJudge",
    "NeedEvidenceSemanticResult",
    "SemanticFacetDecision",
    "SemanticJudgmentBatchOutput",
]
