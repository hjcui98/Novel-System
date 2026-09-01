"""High-level Stage 2 Memory Gateway with frozen output and deterministic fallback."""

from __future__ import annotations

from dataclasses import dataclass

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    FacetClosureStatus,
    FacetEvidenceReceipt,
    NeedFacetKind,
    RetrievalTrace,
    RetrievalUnitKind,
    Stage1MemoryNeed,
)
from novel_agent.domain.stage2 import (
    ControllerArm,
    ControllerStopReason,
    MemoryGatewayMode,
    MemoryGatewayPolicy,
    MemoryGatewayResult,
    MemoryResolutionRequest,
    PairedContextArmResult,
    PairedContextComparison,
)
from novel_agent.domain.text import EvidenceRef, TextBlock
from novel_agent.domain.writer_context import (
    EvidenceSlice,
    NeedEvidenceSemanticStatus,
    NeedFacetSemanticReceipt,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.evidence_first_writer_context_assembler import (
    NeedEvidenceSelection,
    SliceSelectionTrace,
)
from novel_agent.services.evidence_slice_resolver import (
    EvidenceSliceResolver,
    LiveEvidenceBasis,
    text_root_indexes,
)
from novel_agent.services.need_evidence_semantic_judgment import (
    NeedEvidenceSemanticJudge,
    NeedEvidenceSemanticResult,
)
from novel_agent.services.paired_controller import PairedMemoryControllerRunner


@dataclass(frozen=True, slots=True)
class _PreparedLiveOutput:
    selected: PairedContextArmResult
    needs: dict[StableId, Stage1MemoryNeed]
    selections: list[NeedEvidenceSelection]
    assembled: list[tuple[RetrievalTrace, tuple[EvidenceRef, ...], tuple[StableId, ...], bool]]


class MemoryGatewayBlockedError(RuntimeError):
    pass


class MemoryGateway:
    def __init__(
        self,
        paired_runner: PairedMemoryControllerRunner,
        policy: MemoryGatewayPolicy,
        artifacts: ArtifactRepository,
        *,
        schema_version: SchemaVersion,
        semantic_judge: NeedEvidenceSemanticJudge | None = None,
        slice_resolver: EvidenceSliceResolver | None = None,
    ) -> None:
        if policy.configuration_fingerprint != paired_runner.comparison_basis_fingerprint:
            raise ValueError("Memory Gateway policy and runner configuration differ")
        self._paired = paired_runner
        self._policy = policy
        self._artifacts = artifacts
        self._schema_version = schema_version
        self._semantic_judge = semantic_judge
        self._slice_resolver = slice_resolver or EvidenceSliceResolver()

    def resolve(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        *,
        thread_id: str,
        evaluator_only_artifacts: tuple[ArtifactId, ...] = (),
    ) -> MemoryGatewayResult:
        selected, comparison, fallback, fallback_reason = self._select_arm(
            request,
            text_root,
            thread_id=thread_id,
            evaluator_only_artifacts=evaluator_only_artifacts,
        )
        selected = self._assemble_live_output(request, text_root, selected)
        return self._freeze_result(
            request,
            selected,
            comparison=comparison,
            fallback=fallback,
            fallback_reason=fallback_reason,
        )

    async def resolve_async(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        *,
        thread_id: str,
        evaluator_only_artifacts: tuple[ArtifactId, ...] = (),
    ) -> MemoryGatewayResult:
        selected, comparison, fallback, fallback_reason = self._select_arm(
            request,
            text_root,
            thread_id=thread_id,
            evaluator_only_artifacts=evaluator_only_artifacts,
        )
        selected = await self._assemble_live_output_async(request, text_root, selected)
        return self._freeze_result(
            request,
            selected,
            comparison=comparison,
            fallback=fallback,
            fallback_reason=fallback_reason,
        )

    def _select_arm(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        *,
        thread_id: str,
        evaluator_only_artifacts: tuple[ArtifactId, ...],
    ) -> tuple[
        PairedContextArmResult,
        PairedContextComparison | None,
        bool,
        str | None,
    ]:
        comparison = None
        fallback = False
        fallback_reason: str | None = None
        if self._policy.mode is MemoryGatewayMode.DETERMINISTIC:
            selected = self._paired.run_deterministic(
                request,
                text_root,
                evaluator_only_artifacts=evaluator_only_artifacts,
            )
        else:
            agentic = self._paired.run_agentic(
                request,
                text_root,
                thread_id=thread_id,
                evaluator_only_artifacts=evaluator_only_artifacts,
            )
            bounded_eligible = (
                agentic.stop_reason is ControllerStopReason.SUFFICIENT
                and agentic.future_leakage_count == 0
            )
            if bounded_eligible:
                selected = agentic
            elif self._policy.allow_deterministic_fallback:
                deterministic = self._paired.run_deterministic(
                    request,
                    text_root,
                    evaluator_only_artifacts=evaluator_only_artifacts,
                )
                comparison = self._paired.compare(request, deterministic, agentic)
                if deterministic.future_leakage_count:
                    raise MemoryGatewayBlockedError(
                        "deterministic fallback contains evaluator-only artifacts"
                    )
                selected = deterministic
                fallback = True
                fallback_reason = (
                    "bounded controller contains evaluator-only artifacts"
                    if agentic.future_leakage_count
                    else f"bounded controller stopped: {agentic.stop_reason.value}"
                )
            else:
                raise MemoryGatewayBlockedError(
                    "bounded controller is ineligible and deterministic fallback is disabled"
                )
        if selected.future_leakage_count:
            raise MemoryGatewayBlockedError("selected context contains evaluator-only artifacts")
        return selected, comparison, fallback, fallback_reason

    def _freeze_result(
        self,
        request: MemoryResolutionRequest,
        selected: PairedContextArmResult,
        *,
        comparison: PairedContextComparison | None,
        fallback: bool,
        fallback_reason: str | None,
    ) -> MemoryGatewayResult:
        if comparison is not None:
            if selected.arm is ControllerArm.DETERMINISTIC:
                comparison = comparison.model_copy(update={"deterministic": selected})
            else:
                comparison = comparison.model_copy(update={"agentic": selected})
        frozen = self._artifacts.put(
            canonical_json_bytes(selected.context.model_dump(mode="json")),
            "application/vnd.novel-agent.context-package+json",
            self._schema_version,
        )
        return MemoryGatewayResult(
            gateway_result_id=StableId(f"gateway-result.{request.request_id.root}"),
            request_id=request.request_id,
            selected_arm=selected.arm,
            fallback_used=fallback,
            fallback_reason=fallback_reason,
            context=selected.context,
            frozen_context_artifact=frozen,
            selected_result=selected,
            comparison=comparison,
            promotion_evidence=self._policy.promotion_evidence,
            policy_id=self._policy.policy_id,
            configuration_fingerprint=self._policy.configuration_fingerprint,
        )

    def _assemble_live_output(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        selected: PairedContextArmResult,
    ) -> PairedContextArmResult:
        """Selected unit → exact L0 → facet Judge on the Gateway output path."""

        prepared = self._prepare_live_output(request, text_root, selected)
        if prepared is None:
            return selected
        semantic_result, judge_error = self._judge_selections(tuple(prepared.selections))
        return self._commit_live_output(request, prepared, semantic_result, judge_error)

    async def _assemble_live_output_async(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        selected: PairedContextArmResult,
    ) -> PairedContextArmResult:
        prepared = self._prepare_live_output(request, text_root, selected)
        if prepared is None:
            return selected
        semantic_result, judge_error = await self._judge_selections_async(
            tuple(prepared.selections)
        )
        return self._commit_live_output(request, prepared, semantic_result, judge_error)

    def _prepare_live_output(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        selected: PairedContextArmResult,
    ) -> _PreparedLiveOutput | None:
        needs = {need.need_id: need for need in request.initial_memory_needs}
        if not selected.context.retrieval_traces or not needs:
            return None
        blocks, chapter_indexes = text_root_indexes(text_root)
        basis = LiveEvidenceBasis(
            request_commit=request.base_commit,
            request_snapshot_id=request.snapshot_id,
            checkpoint_chapter=request.narrative_chapter,
        )
        selections: list[NeedEvidenceSelection] = []
        assembled: list[
            tuple[RetrievalTrace, tuple[EvidenceRef, ...], tuple[StableId, ...], bool]
        ] = []
        for trace in selected.context.retrieval_traces:
            need = needs.get(trace.need_id)
            if need is None:
                assembled.append((trace, (), (), False))
                continue
            selection, evidence_refs, slice_ids, truncated = self._selection_for_trace(
                need=need,
                trace=trace,
                basis=basis,
                blocks=blocks,
                chapter_indexes=chapter_indexes,
                access_scope=request.access_scope.value,
            )
            selections.append(selection)
            assembled.append((trace, evidence_refs, slice_ids, truncated))
        if not any(refs or ids for _trace, refs, ids, _truncated in assembled):
            return None
        return _PreparedLiveOutput(
            selected=selected,
            needs=needs,
            selections=selections,
            assembled=assembled,
        )

    def _judge_selections(
        self,
        selections: tuple[NeedEvidenceSelection, ...],
    ) -> tuple[NeedEvidenceSemanticResult | None, str | None]:
        if self._semantic_judge is None or not selections:
            return None, None
        try:
            return self._semantic_judge.judge(selections), None
        except Exception as error:  # fail closed; keep structured receipts
            return None, type(error).__name__

    async def _judge_selections_async(
        self,
        selections: tuple[NeedEvidenceSelection, ...],
    ) -> tuple[NeedEvidenceSemanticResult | None, str | None]:
        if self._semantic_judge is None or not selections:
            return None, None
        try:
            judge_async = getattr(self._semantic_judge, "judge_async", None)
            if callable(judge_async):
                return await judge_async(selections), None
            return self._semantic_judge.judge(selections), None
        except Exception as error:  # fail closed; keep structured receipts
            return None, type(error).__name__

    def _commit_live_output(
        self,
        request: MemoryResolutionRequest,
        prepared: _PreparedLiveOutput,
        semantic_result: NeedEvidenceSemanticResult | None,
        judge_error: str | None,
    ) -> PairedContextArmResult:
        selected = prepared.selected
        needs = prepared.needs
        selections = prepared.selections
        assembled = prepared.assembled
        semantic_receipts_by_need: dict[StableId, tuple[NeedFacetSemanticReceipt, ...]] = {}
        receipt_ref: ArtifactRef | None = None
        if self._semantic_judge is not None and selections:
            payload = {
                "contract_version": "memory_gateway.live_l0_judge.v1",
                "request_id": request.request_id.root,
                "base_commit": request.base_commit.root,
                "snapshot_id": request.snapshot_id.root,
                "semantic_receipts": (
                    ()
                    if semantic_result is None
                    else tuple(item.model_dump(mode="json") for item in semantic_result.receipts)
                ),
                "semantic_batch_receipts": (
                    ()
                    if semantic_result is None
                    else tuple(
                        item.model_dump(mode="json") for item in semantic_result.batch_receipts
                    )
                ),
                "judge_error": judge_error,
            }
            receipt_ref = self._artifacts.put(
                canonical_json_bytes(payload),
                "application/vnd.novel-agent.semantic-fallback-receipt+json",
                self._schema_version,
            )
            if semantic_result is not None:
                grouped: dict[StableId, list[NeedFacetSemanticReceipt]] = {}
                for receipt in semantic_result.receipts:
                    grouped.setdefault(receipt.need_id, []).append(receipt)
                semantic_receipts_by_need = {
                    need_id: tuple(items) for need_id, items in grouped.items()
                }
        updated_traces: list[RetrievalTrace] = []
        for trace, evidence_refs, slice_ids, truncated in assembled:
            need = needs.get(trace.need_id)
            if need is None:
                updated_traces.append(trace)
                continue
            selection = next(
                (item for item in selections if item.need.need_id == trace.need_id),
                None,
            )
            updated_traces.append(
                self._trace_with_live_closure(
                    trace=trace,
                    need=need,
                    selection=selection,
                    evidence_refs=evidence_refs,
                    slice_ids=slice_ids,
                    truncated=truncated,
                    semantic_receipts=semantic_receipts_by_need.get(trace.need_id, ()),
                    receipt_ref=receipt_ref,
                    judge_error=judge_error,
                )
            )
        updated_by_id = {trace.need_id: trace for trace in updated_traces}
        all_traces = tuple(
            updated_by_id.get(trace.need_id, trace) for trace in selected.context.retrieval_traces
        )
        closed_need_ids: set[StableId] = set()
        for need in request.initial_memory_needs:
            if need.requirement.value != "mandatory":
                continue
            closed_trace = updated_by_id.get(need.need_id)
            if closed_trace is not None and _required_facet_ids(need).issubset(
                set(closed_trace.closed_need_facet_ids)
            ):
                closed_need_ids.add(need.need_id)
        unresolved_gaps = tuple(
            gap
            for gap in selected.context.unresolved_gaps
            if not any(
                need.need_id in closed_need_ids and need.query_text == gap
                for need in needs.values()
            )
        )
        updated_context = selected.context.model_copy(
            update={"retrieval_traces": all_traces, "unresolved_gaps": unresolved_gaps}
        )
        mandatory_total, mandatory_closed = _mandatory_facet_counts(
            request.initial_memory_needs, all_traces
        )
        stop_reason = selected.stop_reason
        if (
            mandatory_total > 0
            and mandatory_closed == mandatory_total
            and stop_reason is ControllerStopReason.MANDATORY_GAP_UNRESOLVED
        ):
            stop_reason = ControllerStopReason.NO_ADDITIONAL_EVIDENCE
        elif (
            mandatory_total > 0
            and mandatory_closed < mandatory_total
            and stop_reason
            in {
                ControllerStopReason.SUFFICIENT,
                ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
            }
        ):
            stop_reason = ControllerStopReason.MANDATORY_GAP_UNRESOLVED
        return selected.model_copy(
            update={
                "context": updated_context,
                "stop_reason": stop_reason,
                "mandatory_need_facets_total": mandatory_total,
                "mandatory_need_facets_closed": mandatory_closed,
            }
        )

    def _selection_for_trace(
        self,
        *,
        need: Stage1MemoryNeed,
        trace: RetrievalTrace,
        basis: LiveEvidenceBasis,
        blocks: dict[StableId, TextBlock],
        chapter_indexes: dict[StableId, int],
        access_scope: str,
    ) -> tuple[NeedEvidenceSelection, tuple[EvidenceRef, ...], tuple[StableId, ...], bool]:
        """Exact L0 from every selected live unit. Judge packing owns capacity."""

        selected = tuple(
            sorted(
                (candidate for candidate in trace.candidates if candidate.selected),
                key=lambda candidate: (candidate.fused_rank, candidate.unit.unit_id.root),
            )
        )
        resolved_refs: list[EvidenceRef] = []
        slices: list[EvidenceSlice] = []
        slice_traces: list[SliceSelectionTrace] = []
        seen_slice_ids: set[StableId] = set()
        for candidate in selected:
            if candidate.unit.unit_kind in {
                RetrievalUnitKind.GROUNDED_BLOCK,
                RetrievalUnitKind.GROUNDED_SPAN,
            }:
                continue
            first_hit = sorted(
                candidate.channel_hits,
                key=lambda hit: (hit.channel_rank, hit.channel.value),
            )[0]
            for evidence in candidate.unit.evidence_refs:
                block = blocks.get(evidence.span.block_id) if evidence.span is not None else None
                chapter_index = None
                if block is not None:
                    chapter_index = chapter_indexes.get(block.chapter_id)
                resolved = self._slice_resolver.resolve_live_evidence(
                    basis=basis,
                    unit_source_commit=candidate.unit.source_commit,
                    unit_snapshot_id=candidate.unit.snapshot_id,
                    evidence=evidence,
                    block=block,
                    chapter_index=chapter_index,
                    access_scope=need.access_scope or access_scope,
                )
                if not resolved:
                    continue
                for slice_ in resolved:
                    if slice_.slice_id in seen_slice_ids:
                        continue
                    seen_slice_ids.add(slice_.slice_id)
                    slices.append(slice_)
                    resolved_refs.append(evidence)
                    slice_traces.append(
                        SliceSelectionTrace(
                            slice_id=slice_.slice_id,
                            unit_id=candidate.unit.unit_id,
                            route_channel=first_hit.channel.value,
                            fused_rank=candidate.fused_rank,
                            selection_reason=(
                                "gateway_live_l0;"
                                f"channel={first_hit.channel.value};rank={candidate.fused_rank}"
                            ),
                            evidence_ref=evidence,
                        )
                    )
        return (
            NeedEvidenceSelection(
                need=need,
                selections=tuple(slice_traces),
                slices=tuple(slices),
                facet_receipts=trace.facet_receipts,
            ),
            tuple(dict.fromkeys(resolved_refs)),
            tuple(slice_.slice_id for slice_ in slices),
            False,
        )

    def _trace_with_live_closure(
        self,
        *,
        trace: RetrievalTrace,
        need: Stage1MemoryNeed,
        selection: NeedEvidenceSelection | None,
        evidence_refs: tuple[EvidenceRef, ...],
        slice_ids: tuple[StableId, ...],
        truncated: bool,
        semantic_receipts: tuple[NeedFacetSemanticReceipt, ...],
        receipt_ref: ArtifactRef | None,
        judge_error: str | None,
    ) -> RetrievalTrace:
        required = _required_facet_ids(need)
        old_receipts = {receipt.need_facet_id: receipt for receipt in trace.facet_receipts}
        semantic_by_facet = {receipt.need_facet_id: receipt for receipt in semantic_receipts}
        support_units_by_facet: dict[StableId, tuple[StableId, ...]] = {}
        if selection is not None:
            for receipt in semantic_receipts:
                support_ids = set(receipt.supporting_slice_ids) | set(receipt.partial_slice_ids)
                support_units_by_facet[receipt.need_facet_id] = tuple(
                    dict.fromkeys(
                        item.unit_id
                        for item in selection.selections
                        if item.slice_id in support_ids
                    )
                )
        facet_receipts: list[FacetEvidenceReceipt] = []
        for facet in need.need_facets:
            if facet.need_facet_id not in required:
                continue
            semantic = semantic_by_facet.get(facet.need_facet_id)
            old = old_receipts.get(facet.need_facet_id)
            location_units = _l0_current_location_unit_ids(trace, selection)
            relation_cover_units = _l0_relation_seed_cover_unit_ids(need, trace, selection)
            if semantic is not None and semantic.status in {
                NeedEvidenceSemanticStatus.SUPPORTED,
                NeedEvidenceSemanticStatus.PARTIAL,
            }:
                # ADR-0009: at least one answering slice closes the facet. Models
                # often put those slices in `partial` when they want every name
                # covered; Stage 4 still treats that as supported exact L0.
                stop_reason = (
                    "semantic_judge_supported_exact_l0"
                    if semantic.status is NeedEvidenceSemanticStatus.SUPPORTED
                    else "semantic_judge_partial_exact_l0"
                )
                facet_receipts.append(
                    FacetEvidenceReceipt(
                        need_id=need.need_id,
                        need_facet_id=facet.need_facet_id,
                        facet_kind=facet.facet_kind,
                        mandatory=need.requirement.value == "mandatory",
                        status=FacetClosureStatus.SUPPORTED,
                        supporting_unit_ids=support_units_by_facet.get(facet.need_facet_id, ()),
                        stop_reason=stop_reason,
                    )
                )
            elif (
                semantic is not None
                and semantic.status is NeedEvidenceSemanticStatus.UNSUPPORTED
                and facet.facet_kind in {NeedFacetKind.RELATION_STATE, NeedFacetKind.CAUSAL_HISTORY}
                and relation_cover_units
            ):
                # ADR-0009: L0 quote that names both Need seeds (or a 家-stem
                # member such as 天海胜雪 for 天海家) answers the seed-pair facet.
                facet_receipts.append(
                    FacetEvidenceReceipt(
                        need_id=need.need_id,
                        need_facet_id=facet.need_facet_id,
                        facet_kind=facet.facet_kind,
                        mandatory=need.requirement.value == "mandatory",
                        status=FacetClosureStatus.SUPPORTED,
                        supporting_unit_ids=relation_cover_units,
                        stop_reason="semantic_judge_unsupported_exact_l0_answering_unit",
                    )
                )
            elif (
                semantic is not None
                and semantic.status is NeedEvidenceSemanticStatus.UNSUPPORTED
                and facet.facet_kind is NeedFacetKind.CURRENT_STATE
                and location_units
            ):
                # ADR-0009: live location/located_at/resides_at units in L0 answer
                # CURRENT_STATE. Models still dump them into unsupported when the
                # question names a chapter boundary and the quote has no clock.
                facet_receipts.append(
                    FacetEvidenceReceipt(
                        need_id=need.need_id,
                        need_facet_id=facet.need_facet_id,
                        facet_kind=facet.facet_kind,
                        mandatory=need.requirement.value == "mandatory",
                        status=FacetClosureStatus.SUPPORTED,
                        supporting_unit_ids=location_units,
                        stop_reason="semantic_judge_unsupported_exact_l0_answering_unit",
                    )
                )
            elif semantic is not None:
                stop_reason = (
                    "semantic_judge_unsupported_exact_l0"
                    if semantic.status is NeedEvidenceSemanticStatus.UNSUPPORTED
                    else "semantic_judge_unresolved"
                )
                facet_receipts.append(
                    FacetEvidenceReceipt(
                        need_id=need.need_id,
                        need_facet_id=facet.need_facet_id,
                        facet_kind=facet.facet_kind,
                        mandatory=need.requirement.value == "mandatory",
                        status=FacetClosureStatus.UNSUPPORTED,
                        supporting_unit_ids=support_units_by_facet.get(facet.need_facet_id, ()),
                        stop_reason=stop_reason,
                    )
                )
            elif old is not None:
                facet_receipts.append(old)
            else:
                facet_receipts.append(
                    FacetEvidenceReceipt(
                        need_id=need.need_id,
                        need_facet_id=facet.need_facet_id,
                        facet_kind=facet.facet_kind,
                        mandatory=need.requirement.value == "mandatory",
                        status=FacetClosureStatus.UNSUPPORTED,
                        stop_reason="no_exact_evidence_for_facet",
                    )
                )
        closed = tuple(
            receipt.need_facet_id
            for receipt in facet_receipts
            if receipt.status is FacetClosureStatus.SUPPORTED
        )
        statuses = tuple(receipt.status for receipt in semantic_receipts)
        if semantic_receipts and all(
            status is NeedEvidenceSemanticStatus.SUPPORTED for status in statuses
        ):
            status: str | None = "SUPPORTED"
        elif any(
            item in {NeedEvidenceSemanticStatus.SUPPORTED, NeedEvidenceSemanticStatus.PARTIAL}
            for item in statuses
        ):
            status = "PARTIAL"
        elif judge_error is not None:
            status = "FAILED"
        elif any(item is NeedEvidenceSemanticStatus.UNSUPPORTED for item in statuses):
            status = "UNSUPPORTED"
        elif semantic_receipts:
            status = "UNRESOLVED"
        else:
            status = trace.semantic_fallback_status
        update: dict[str, object] = {
            "l0_fallback_evidence_refs": evidence_refs or trace.l0_fallback_evidence_refs,
            "l0_fallback_slice_ids": slice_ids or trace.l0_fallback_slice_ids,
            "l0_fallback_truncated": truncated or trace.l0_fallback_truncated,
            "closed_need_facet_ids": closed,
            "facet_receipts": tuple(facet_receipts) if facet_receipts else trace.facet_receipts,
        }
        if receipt_ref is not None:
            update["semantic_receipt_refs"] = (receipt_ref,)
        if status is not None:
            update["semantic_fallback_status"] = status
            update["semantic_fallback_reason"] = judge_error or "gateway_live_exact_l0"
        return trace.model_copy(update=update)


_CURRENT_LOCATION_PREDICATES = frozenset({"location", "located_at", "resides_at"})


_FAMILY_SUFFIXES = ("家", "族", "氏")


def _seed_mention_labels(need: Stage1MemoryNeed, trace: RetrievalTrace) -> tuple[str, ...]:
    if len(need.entity_ids) < 2:
        return ()
    labels: list[str] = []
    seeds = set(need.entity_ids)
    for candidate in trace.candidates:
        ents = candidate.unit.entity_ids
        if len(ents) != 1 or ents[0] not in seeds or not candidate.unit.text:
            continue
        head = candidate.unit.text.split()[0]
        if head:
            labels.append(head)
    prefix = (need.semantic_question or "").split("的", 1)[0]
    named = tuple(
        part.strip()
        for part in prefix.replace("、", ",").split(",")
        if part.strip() and " " not in part.strip()
    )
    labels.extend(named)
    return tuple(dict.fromkeys(labels))


def _label_stems(label: str) -> tuple[str, ...]:
    stems = [label]
    for suffix in _FAMILY_SUFFIXES:
        if label.endswith(suffix) and len(label) > len(suffix):
            stems.append(label[: -len(suffix)])
    return tuple(dict.fromkeys(stems))


def _text_covers_seed_labels(text: str, labels: tuple[str, ...]) -> bool:
    return all(any(stem in text for stem in _label_stems(label)) for label in labels)


def _l0_relation_seed_cover_unit_ids(
    need: Stage1MemoryNeed,
    trace: RetrievalTrace,
    selection: NeedEvidenceSelection | None,
) -> tuple[StableId, ...]:
    if selection is None or len(need.entity_ids) < 2:
        return ()
    labels = _seed_mention_labels(need, trace)
    if len(labels) < 2:
        return ()
    unit_by_slice = {item.slice_id: item.unit_id for item in selection.selections}
    covered: list[StableId] = []
    for slice_ in selection.slices:
        if not _text_covers_seed_labels(slice_.text, labels):
            continue
        unit_id = unit_by_slice.get(slice_.slice_id)
        if unit_id is not None:
            covered.append(unit_id)
    return tuple(dict.fromkeys(covered))


def _l0_current_location_unit_ids(
    trace: RetrievalTrace,
    selection: NeedEvidenceSelection | None,
) -> tuple[StableId, ...]:
    if selection is None:
        return ()
    sliced = {item.unit_id for item in selection.selections}
    return tuple(
        dict.fromkeys(
            candidate.unit.unit_id
            for candidate in trace.candidates
            if candidate.selected
            and candidate.unit.unit_id in sliced
            and (candidate.unit.predicate or "") in _CURRENT_LOCATION_PREDICATES
        )
    )


def _required_facet_ids(need: Stage1MemoryNeed) -> set[StableId]:
    if need.completion_spec is not None:
        return set(need.completion_spec.required_need_facet_ids)
    return {facet.need_facet_id for facet in need.need_facets}


def _mandatory_facet_counts(
    needs: tuple[Stage1MemoryNeed, ...],
    traces: tuple[RetrievalTrace, ...],
) -> tuple[int, int]:
    by_id = {trace.need_id: trace for trace in traces}
    total = 0
    closed = 0
    for need in needs:
        required = _required_facet_ids(need)
        if need.requirement.value != "mandatory":
            continue
        total += len(required)
        trace = by_id.get(need.need_id)
        if trace is not None:
            closed += len(set(trace.closed_need_facet_ids) & required)
    return total, closed
