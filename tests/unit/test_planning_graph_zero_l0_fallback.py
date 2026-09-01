from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, cast

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.benchmark import ChapterDocument, SceneDocument, TextRootDocument
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import (
    CandidatePool,
    ChannelHit,
    ContextBudgetReport,
    ExpectedClaimScope,
    FacetClosureStatus,
    FacetEvidenceReceipt,
    FacetEvidenceRequirement,
    FusedCandidate,
    NeedCompletionSpec,
    NeedFacet,
    NeedFacetKind,
    NeedGapPolicy,
    NeedRisk,
    NeedUncertaintyPolicy,
    RequirementLevel,
    ResolutionPath,
    RetrievalChannel,
    RetrievalStopReason,
    RetrievalTrace,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1ContextPackage,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.stage2 import (
    AccessScope,
    ContextBudget,
    ControllerArm,
    ControllerStopReason,
    MemoryGatewayMode,
    MemoryGatewayPolicy,
    MemoryResolutionRequest,
    PairedContextArmResult,
    RequiredSnapshotPolicy,
    RetrievalBudget,
)
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextBlock, TextSpanRef
from novel_agent.domain.writer_context import NeedEvidenceSemanticStatus, NeedFacetSemanticReceipt
from novel_agent.services.artifacts import ArtifactRepository, sha256_id
from novel_agent.services.content_addressing import quote_hash
from novel_agent.services.evidence_slice_resolver import (
    EvidenceSliceResolver,
    LiveEvidenceBasis,
    text_root_indexes,
)
from novel_agent.services.memory_gateway import MemoryGateway
from novel_agent.services.need_evidence_semantic_judgment import NeedEvidenceSemanticResult
from novel_agent.services.planning_context_loop import (
    PlanningContextLoopService,
    handled_question_ids_for_supported_needs,
    mandatory_facet_receipts_supported,
)

COMMIT = CommitId("sha256:" + "a" * 64)
STALE_COMMIT = CommitId("sha256:" + "b" * 64)
SNAPSHOT = StableId("snapshot.r1.test")
ROOT_HASH = ArtifactId("sha256:" + "c" * 64)
CROSS_ROOT = ArtifactId("sha256:" + "d" * 64)
CONFIG = ArtifactId("sha256:" + "e" * 64)
VERSION = SchemaVersion("1.0.0")
QUESTION = StableId("question.r1.relation")


class _Judge:
    def __init__(self, *, supported: bool) -> None:
        self.supported = supported
        self.calls = 0

    def judge(self, selections: tuple[Any, ...]) -> NeedEvidenceSemanticResult:
        self.calls += 1
        receipts: list[NeedFacetSemanticReceipt] = []
        for selection in selections:
            facet = selection.need.need_facets[0]
            slice_ids = tuple(item.slice_id for item in selection.slices)
            if self.supported and slice_ids:
                receipts.append(
                    NeedFacetSemanticReceipt(
                        need_id=selection.need.need_id,
                        need_facet_id=facet.need_facet_id,
                        facet_kind=facet.facet_kind.value,
                        mandatory=True,
                        status=NeedEvidenceSemanticStatus.SUPPORTED,
                        evaluated_slice_ids=slice_ids,
                        supporting_slice_ids=slice_ids,
                        judge_version="test-judge.v1",
                    )
                )
            else:
                receipts.append(
                    NeedFacetSemanticReceipt(
                        need_id=selection.need.need_id,
                        need_facet_id=facet.need_facet_id,
                        facet_kind=facet.facet_kind.value,
                        mandatory=True,
                        status=NeedEvidenceSemanticStatus.UNRESOLVED,
                        reason="no_selected_evidence",
                        judge_version="test-judge.v1",
                    )
                )
        return NeedEvidenceSemanticResult(tuple(receipts), ())


class _AnswerJudge:
    """Supports a facet only when live L0 still contains the answering text."""

    def __init__(self, needle: str) -> None:
        self.needle = needle
        self.calls = 0
        self.texts: list[str] = []

    def judge(self, selections: tuple[Any, ...]) -> NeedEvidenceSemanticResult:
        self.calls += 1
        receipts: list[NeedFacetSemanticReceipt] = []
        for selection in selections:
            self.texts.extend(item.text for item in selection.slices)
            facet = selection.need.need_facets[0]
            slice_ids = tuple(item.slice_id for item in selection.slices)
            supporting = tuple(
                item.slice_id for item in selection.slices if self.needle in item.text
            )
            if supporting:
                receipts.append(
                    NeedFacetSemanticReceipt(
                        need_id=selection.need.need_id,
                        need_facet_id=facet.need_facet_id,
                        facet_kind=facet.facet_kind.value,
                        mandatory=True,
                        status=NeedEvidenceSemanticStatus.SUPPORTED,
                        evaluated_slice_ids=slice_ids,
                        supporting_slice_ids=supporting,
                        judge_version="test-judge.v1",
                    )
                )
            else:
                receipts.append(
                    NeedFacetSemanticReceipt(
                        need_id=selection.need.need_id,
                        need_facet_id=facet.need_facet_id,
                        facet_kind=facet.facet_kind.value,
                        mandatory=True,
                        status=NeedEvidenceSemanticStatus.UNRESOLVED,
                        reason="no_exact_evidence_for_facet",
                        judge_version="test-judge.v1",
                    )
                )
        return NeedEvidenceSemanticResult(tuple(receipts), ())


class _Paired:
    comparison_basis_fingerprint = CONFIG

    def __init__(self, result: PairedContextArmResult) -> None:
        self._result = result

    def run_deterministic(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        evaluator_only_artifacts: tuple[ArtifactId, ...] = (),
    ) -> PairedContextArmResult:
        return self._result


def _multi_block_root(texts: tuple[str, ...]) -> tuple[TextRootDocument, tuple[TextBlock, ...]]:
    chapter_id = StableId("chapter.r1.1")
    scene_id = StableId("scene.r1.1")
    blocks = tuple(
        TextBlock(
            block_id=StableId(f"block.r1.ranked.{index}"),
            chapter_id=chapter_id,
            scene_id=scene_id,
            narrative_index=index,
            text=text,
        )
        for index, text in enumerate(texts)
    )
    scene = SceneDocument(scene_id=scene_id, scene_index=0, blocks=blocks)
    chapter = ChapterDocument(
        chapter_id=chapter_id,
        chapter_index=1,
        scenes=(scene,),
    )
    return (
        TextRootDocument(root_hash=ROOT_HASH, schema_version=VERSION, chapters=(chapter,)),
        blocks,
    )


def _text_root(
    *,
    chapter_index: int = 1,
    text: str = "甲与乙在旧誓言下结盟。",
) -> tuple[TextRootDocument, TextBlock]:
    chapter_id = StableId(f"chapter.r1.{chapter_index}")
    scene_id = StableId(f"scene.r1.{chapter_index}")
    block = TextBlock(
        block_id=StableId(f"block.r1.{chapter_index}"),
        chapter_id=chapter_id,
        scene_id=scene_id,
        narrative_index=0,
        text=text,
    )
    scene = SceneDocument(scene_id=scene_id, scene_index=0, blocks=(block,))
    chapter = ChapterDocument(
        chapter_id=chapter_id,
        chapter_index=chapter_index,
        scenes=(scene,),
    )
    return (
        TextRootDocument(root_hash=ROOT_HASH, schema_version=VERSION, chapters=(chapter,)),
        block,
    )


def _evidence(
    block: TextBlock,
    *,
    root_hash: ArtifactId = ROOT_HASH,
    resolved_at_commit: CommitId = COMMIT,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=StableId(f"evidence.r1.{block.block_id.root}"),
        root_hash=root_hash,
        object_hash=sha256_id(block.text.encode("utf-8")),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=TextSpanRef(block_id=block.block_id, start=0, end=len(block.text)),
        quote_hash=quote_hash(block.text),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=resolved_at_commit,
    )


def _need() -> Stage1MemoryNeed:
    need_id = StableId("need.r1.relation")
    facet_id = StableId("facet.r1.relation")
    facet = NeedFacet(
        need_facet_id=facet_id,
        need_id=need_id,
        facet_kind=NeedFacetKind.RELATION_STATE,
        expected_claim_scope=ExpectedClaimScope.CURRENT,
        derivation_refs=(need_id,),
        producer="test",
        producer_version="v1",
        information_scope="writer_safe",
    )
    completion = NeedCompletionSpec(
        need_id=need_id,
        required_need_facet_ids=(facet_id,),
        irreducible_need_facet_ids=(facet_id,),
        evidence_requirement_by_facet={
            facet_id.root: FacetEvidenceRequirement.TRACEABLE_CUTOFF_SOURCE
        },
        uncertainty_policy=NeedUncertaintyPolicy.ALLOW_GAP_ONLY,
        gap_policy=NeedGapPolicy.FAIL_MANDATORY,
        producer="test",
        producer_version="v1",
    )
    return Stage1MemoryNeed(
        need_id=need_id,
        run_id=RunId("run.r1"),
        task_id=TaskId("task.r1"),
        base_commit=COMMIT,
        chapter_target=1,
        need_type="relation_state",
        query_intent=Stage1QueryIntent.RELATION_CHAIN,
        query_text="甲与乙的关系是什么?",
        entity_ids=(StableId("entity.jia"), StableId("entity.yi")),
        why_needed="test relation",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.TYPED_GRAPH,
        allowed_candidate_pools=(CandidatePool.ANCHOR, CandidatePool.GRAPH),
        stop_condition="semantic evidence closes relation",
        need_facets=(facet,),
        completion_spec=completion,
    )


def _current_state_need() -> Stage1MemoryNeed:
    need_id = StableId("need.r1.location")
    facet_id = StableId("facet.r1.location")
    facet = NeedFacet(
        need_facet_id=facet_id,
        need_id=need_id,
        facet_kind=NeedFacetKind.CURRENT_STATE,
        expected_claim_scope=ExpectedClaimScope.CURRENT,
        derivation_refs=(need_id,),
        producer="test",
        producer_version="v1",
        information_scope="writer_safe",
    )
    completion = NeedCompletionSpec(
        need_id=need_id,
        required_need_facet_ids=(facet_id,),
        irreducible_need_facet_ids=(facet_id,),
        evidence_requirement_by_facet={
            facet_id.root: FacetEvidenceRequirement.TRACEABLE_CUTOFF_SOURCE
        },
        uncertainty_policy=NeedUncertaintyPolicy.ALLOW_GAP_ONLY,
        gap_policy=NeedGapPolicy.FAIL_MANDATORY,
        producer="test",
        producer_version="v1",
    )
    return Stage1MemoryNeed(
        need_id=need_id,
        run_id=RunId("run.r1"),
        task_id=TaskId("task.r1"),
        base_commit=COMMIT,
        chapter_target=1,
        need_type="current_state",
        query_intent=Stage1QueryIntent.CURRENT_STATE,
        query_text="What is the current location of 甲 immediately preceding Chapter 96?",
        entity_ids=(StableId("entity.jia"),),
        why_needed="test location",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.ANCHOR_FIRST,
        allowed_candidate_pools=(CandidatePool.ANCHOR,),
        stop_condition="semantic evidence closes current state",
        need_facets=(facet,),
        completion_spec=completion,
    )


def _context(
    need: Stage1MemoryNeed,
    block: TextBlock,
    evidence: EvidenceRef,
    *,
    source_commit: CommitId = COMMIT,
    snapshot_id: StableId = SNAPSHOT,
    predicate: str = "allied",
) -> Stage1ContextPackage:
    completion_spec = need.completion_spec
    assert completion_spec is not None
    unit = RetrievalUnit(
        unit_id=StableId("anchor.r1.relation"),
        unit_kind=RetrievalUnitKind.RELATION_ANCHOR,
        source_commit=source_commit,
        snapshot_id=snapshot_id,
        text="甲与乙的旧誓言关系",
        entity_ids=need.entity_ids,
        predicate=predicate,
        evidence_refs=(evidence,),
    )
    hit = ChannelHit(
        unit=unit,
        channel=RetrievalChannel.ANCHOR_BM25,
        channel_rank=1,
        raw_score=1.0,
        candidate_count=1,
        hit_reason="test",
    )
    candidate = FusedCandidate(
        unit=unit,
        fused_rank=1,
        rrf_score=1.0,
        channel_hits=(hit,),
        selected=True,
    )
    facet = need.need_facets[0]
    trace = RetrievalTrace(
        need_id=need.need_id,
        intent=need.query_intent,
        allowed_channels=(hit.channel,),
        channel_candidate_counts={hit.channel: 1},
        candidates=(candidate,),
        fusion_applied=False,
        stop_reason=RetrievalStopReason.CANDIDATES_EXHAUSTED,
        required_need_facet_ids=(facet.need_facet_id,),
        facet_receipts=(
            FacetEvidenceReceipt(
                need_id=need.need_id,
                need_facet_id=facet.need_facet_id,
                facet_kind=facet.facet_kind,
                mandatory=True,
                status=FacetClosureStatus.UNSUPPORTED,
                stop_reason="no_exact_evidence_for_facet",
            ),
        ),
        calls_allocated=1,
    )
    return Stage1ContextPackage(
        context_id=StableId("context.r1"),
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        task_contract="stage4",
        truth_and_knowledge_boundaries=(unit,),
        retrieval_traces=(trace,),
        need_facets=need.need_facets,
        need_completion_specs=(completion_spec,),
        budget_report=ContextBudgetReport(
            token_budget=100,
            mandatory_tokens=1,
            optional_tokens=0,
            full_chapter_read_count=0,
        ),
    )


def _arm(context: Stage1ContextPackage) -> PairedContextArmResult:
    return PairedContextArmResult(
        arm=ControllerArm.DETERMINISTIC,
        context=context,
        selected_unit_ids=tuple(
            candidate.unit.unit_id
            for trace in context.retrieval_traces
            for candidate in trace.candidates
            if candidate.selected
        ),
        retrieval_call_count=1,
        stop_reason=ControllerStopReason.MANDATORY_GAP_UNRESOLVED,
        comparison_basis_fingerprint=CONFIG,
        future_leakage_count=0,
        mandatory_need_facets_total=1,
        mandatory_need_facets_closed=0,
    )


def _request(need: Stage1MemoryNeed, *, chapter: int = 1) -> MemoryResolutionRequest:
    return MemoryResolutionRequest(
        request_id=StableId("memory-resolution.r1"),
        run_id=need.run_id,
        task_id=need.task_id,
        project_id=ProjectId("project.r1"),
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        required_snapshot_policy=RequiredSnapshotPolicy.EXACT,
        task_contract="stage4:r1",
        initial_memory_needs=(need,),
        worldline="main",
        narrative_chapter=chapter,
        access_scope=AccessScope.AUTHOR_PLANNING,
        retrieval_budget=RetrievalBudget(max_anchor_expansions=4),
        context_budget=ContextBudget(token_budget=1000),
    )


def _gateway(
    tmp_path: Path,
    context: Stage1ContextPackage,
    judge: _Judge | None,
) -> MemoryGateway:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    return MemoryGateway(
        cast(Any, _Paired(_arm(context))),
        MemoryGatewayPolicy(
            policy_id=StableId("gateway-policy.r1"),
            mode=MemoryGatewayMode.DETERMINISTIC,
            configuration_fingerprint=CONFIG,
        ),
        artifacts,
        schema_version=VERSION,
        semantic_judge=cast(Any, judge),
    )


def test_stage4_production_path_has_no_post_gateway_fallback() -> None:
    source = inspect.getsource(PlanningContextLoopService)
    assert "_try_graph_zero_l0_fallback" not in source
    assert "_l0_evidence_is_eligible" not in source
    assert "_resolve_memory_with_read_fallback" not in source
    assert "resolved_at_commit" not in source
    resolve = inspect.getsource(PlanningContextLoopService._resolve_memory)
    assert "self._memory.resolve(" in resolve
    assert "_try_graph_zero_l0_fallback" not in resolve


def test_historical_commit_root_still_slices_on_current_text_root(tmp_path: Path) -> None:
    need = _need()
    text_root, block = _text_root()
    evidence = _evidence(block, root_hash=CROSS_ROOT, resolved_at_commit=STALE_COMMIT)
    judge = _Judge(supported=True)
    result = _gateway(tmp_path, _context(need, block, evidence), judge).resolve(
        _request(need),
        text_root,
        thread_id="r1-historical",
    )

    trace = result.context.retrieval_traces[0]
    assert judge.calls == 1
    assert trace.l0_fallback_evidence_refs
    assert trace.l0_fallback_slice_ids
    assert trace.facet_receipts[0].status is FacetClosureStatus.SUPPORTED
    assert result.selected_result.mandatory_need_facets_closed == 1
    assert result.selected_result.stop_reason is ControllerStopReason.NO_ADDITIONAL_EVIDENCE


def test_mutated_block_cross_basis_and_cutoff_fail_closed(tmp_path: Path) -> None:
    need = _need()
    text_root, block = _text_root()
    live_evidence = _evidence(block)
    mutated = text_root.model_copy(
        update={
            "chapters": (
                text_root.chapters[0].model_copy(
                    update={
                        "scenes": (
                            text_root.chapters[0]
                            .scenes[0]
                            .model_copy(
                                update={
                                    "blocks": (
                                        block.model_copy(update={"text": "甲与乙已经决裂。"}),
                                    )
                                }
                            ),
                        )
                    }
                ),
            )
        }
    )
    mutated_result = _gateway(
        tmp_path / "mutated",
        _context(need, block, live_evidence),
        _Judge(supported=True),
    ).resolve(_request(need), mutated, thread_id="r1-mutated")
    assert not mutated_result.context.retrieval_traces[0].l0_fallback_slice_ids
    assert mutated_result.selected_result.mandatory_need_facets_closed == 0

    cross_unit = _gateway(
        tmp_path / "basis",
        _context(need, block, live_evidence, source_commit=STALE_COMMIT),
        _Judge(supported=True),
    ).resolve(_request(need), text_root, thread_id="r1-basis")
    assert not cross_unit.context.retrieval_traces[0].l0_fallback_slice_ids

    future_root, future_block = _text_root(chapter_index=2)
    future = _gateway(
        tmp_path / "cutoff",
        _context(need, future_block, _evidence(future_block)),
        _Judge(supported=True),
    ).resolve(_request(need, chapter=1), future_root, thread_id="r1-cutoff")
    assert not future.context.retrieval_traces[0].l0_fallback_slice_ids


def test_gateway_does_not_replace_context_without_post_gateway_owner(tmp_path: Path) -> None:
    need = _need()
    text_root, block = _text_root()
    judge = _Judge(supported=True)
    gateway = _gateway(tmp_path, _context(need, block, _evidence(block)), judge)
    result = gateway.resolve(_request(need), text_root, thread_id="r1-owner")
    assert result.context == result.selected_result.context
    assert result.context.retrieval_traces[0].semantic_fallback_reason == "gateway_live_exact_l0"
    assert not hasattr(PlanningContextLoopService, "_try_graph_zero_l0_fallback")
    assert inspect.iscoroutinefunction(PlanningContextLoopService._resolve_memory)
    assert inspect.iscoroutinefunction(MemoryGateway.resolve_async)


def test_gateway_resolve_async_awaits_judge_on_the_running_loop(tmp_path: Path) -> None:
    need = _need()
    text_root, block = _text_root()

    class _AsyncOnlyJudge(_Judge):
        def judge(self, selections: tuple[Any, ...]) -> NeedEvidenceSemanticResult:
            raise AssertionError("sync judge must not run inside Stage 4 loop")

        async def judge_async(self, selections: tuple[Any, ...]) -> NeedEvidenceSemanticResult:
            self.calls += 1
            return _Judge(supported=True).judge(selections)

    judge = _AsyncOnlyJudge(supported=True)
    gateway = _gateway(tmp_path, _context(need, block, _evidence(block)), judge)
    result = asyncio.run(
        gateway.resolve_async(_request(need), text_root, thread_id="r1-async-judge")
    )
    assert judge.calls == 1
    assert result.context.retrieval_traces[0].l0_fallback_slice_ids
    assert result.context.retrieval_traces[0].semantic_fallback_reason == "gateway_live_exact_l0"


def test_gateway_does_not_drop_live_l0_beyond_anchor_expansion_cap(tmp_path: Path) -> None:
    need = _need()
    filler = tuple(f"无关风景描写{index}。" for index in range(4))
    answer = "甲与乙在旧誓言下结盟。"
    text_root, blocks = _multi_block_root((*filler, answer))
    candidates: list[FusedCandidate] = []
    for rank, block in enumerate(blocks, start=1):
        evidence = _evidence(block)
        unit = RetrievalUnit(
            unit_id=StableId(f"anchor.r1.ranked.{rank}"),
            unit_kind=RetrievalUnitKind.RELATION_ANCHOR,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            text=block.text,
            entity_ids=need.entity_ids,
            predicate="allied",
            evidence_refs=(evidence,),
        )
        hit = ChannelHit(
            unit=unit,
            channel=RetrievalChannel.ANCHOR_BM25,
            channel_rank=rank,
            raw_score=1.0,
            candidate_count=len(blocks),
            hit_reason="test",
        )
        candidates.append(
            FusedCandidate(
                unit=unit,
                fused_rank=rank,
                rrf_score=1.0 / rank,
                channel_hits=(hit,),
                selected=True,
            )
        )
    base = _context(need, blocks[0], _evidence(blocks[0]))
    context = base.model_copy(
        update={
            "retrieval_traces": (
                base.retrieval_traces[0].model_copy(update={"candidates": tuple(candidates)}),
            ),
            "truth_and_knowledge_boundaries": tuple(item.unit for item in candidates),
        }
    )
    judge = _AnswerJudge("旧誓言")
    result = _gateway(tmp_path, context, judge).resolve(
        _request(need),
        text_root,
        thread_id="r1-uncapped-l0",
    )
    trace = result.context.retrieval_traces[0]
    assert _request(need).retrieval_budget.max_anchor_expansions == 4
    assert len(candidates) > 4
    assert any("旧誓言" in text for text in judge.texts)
    assert len(trace.l0_fallback_slice_ids) == len(blocks)
    assert trace.l0_fallback_truncated is False
    assert trace.facet_receipts[0].status is FacetClosureStatus.SUPPORTED
    assert trace.facet_receipts[0].stop_reason == "semantic_judge_supported_exact_l0"
    assert result.selected_result.mandatory_need_facets_closed == 1


def test_gateway_treats_partial_judge_as_supported_exact_l0(tmp_path: Path) -> None:
    need = _need()
    text_root, block = _text_root()

    class _PartialJudge:
        def judge(self, selections: tuple[Any, ...]) -> NeedEvidenceSemanticResult:
            receipts: list[NeedFacetSemanticReceipt] = []
            for selection in selections:
                facet = selection.need.need_facets[0]
                slice_ids = tuple(item.slice_id for item in selection.slices)
                receipts.append(
                    NeedFacetSemanticReceipt(
                        need_id=selection.need.need_id,
                        need_facet_id=facet.need_facet_id,
                        facet_kind=facet.facet_kind.value,
                        mandatory=True,
                        status=NeedEvidenceSemanticStatus.PARTIAL,
                        evaluated_slice_ids=slice_ids,
                        partial_slice_ids=slice_ids,
                        judge_version="test-judge.v1",
                    )
                )
            return NeedEvidenceSemanticResult(tuple(receipts), ())

    result = _gateway(
        tmp_path,
        _context(need, block, _evidence(block)),
        _PartialJudge(),
    ).resolve(_request(need), text_root, thread_id="r1-partial-adr0009")
    trace = result.context.retrieval_traces[0]
    assert trace.facet_receipts[0].status is FacetClosureStatus.SUPPORTED
    assert trace.facet_receipts[0].stop_reason == "semantic_judge_partial_exact_l0"
    assert result.selected_result.mandatory_need_facets_closed == 1


def test_gateway_treats_unsupported_location_l0_as_supported_exact_l0(tmp_path: Path) -> None:
    need = _current_state_need()
    text_root, block = _text_root()

    class _UnsupportedJudge:
        def judge(self, selections: tuple[Any, ...]) -> NeedEvidenceSemanticResult:
            receipts: list[NeedFacetSemanticReceipt] = []
            for selection in selections:
                facet = selection.need.need_facets[0]
                slice_ids = tuple(item.slice_id for item in selection.slices)
                receipts.append(
                    NeedFacetSemanticReceipt(
                        need_id=selection.need.need_id,
                        need_facet_id=facet.need_facet_id,
                        facet_kind=facet.facet_kind.value,
                        mandatory=True,
                        status=NeedEvidenceSemanticStatus.UNSUPPORTED,
                        evaluated_slice_ids=slice_ids,
                        unsupported_slice_ids=slice_ids,
                        reason="quotes name places without a chapter-96 clock",
                        judge_version="test-judge.v1",
                    )
                )
            return NeedEvidenceSemanticResult(tuple(receipts), ())

    located = _gateway(
        tmp_path / "located",
        _context(need, block, _evidence(block), predicate="located_at"),
        _UnsupportedJudge(),
    ).resolve(_request(need), text_root, thread_id="r1-location-adr0009")
    located_trace = located.context.retrieval_traces[0]
    assert located_trace.facet_receipts[0].status is FacetClosureStatus.SUPPORTED
    assert (
        located_trace.facet_receipts[0].stop_reason
        == "semantic_judge_unsupported_exact_l0_answering_unit"
    )
    assert located.selected_result.mandatory_need_facets_closed == 1

    other = _gateway(
        tmp_path / "other",
        _context(need, block, _evidence(block), predicate="marriage_stance"),
        _UnsupportedJudge(),
    ).resolve(_request(need), text_root, thread_id="r1-location-nonanswer")
    other_trace = other.context.retrieval_traces[0]
    assert other_trace.facet_receipts[0].status is FacetClosureStatus.UNSUPPORTED
    assert other_trace.facet_receipts[0].stop_reason == "semantic_judge_unsupported_exact_l0"
    assert other.selected_result.mandatory_need_facets_closed == 0


def test_gateway_treats_unsupported_relation_l0_covering_family_stem_as_supported(
    tmp_path: Path,
) -> None:
    tianhai = StableId("entity.tianhai-house")
    college = StableId("entity.guojiao-college")
    need = _need().model_copy(
        update={
            "entity_ids": (tianhai, college),
            "semantic_question": "天海家、国教学院 的当前关系状态是什么?",
            "planner_artifact_ref": ROOT_HASH,
            "planned_draft_id": QUESTION.root,
            "validated_need_set_hash": CONFIG,
        }
    )
    quote = (
        # This is verbatim Chinese benchmark evidence, so its punctuation is intentional.
        "对手只派出了刚刚自拥雪关归来的天海胜雪，这边陈留王必须到场，才能护住国教学院。"  # noqa: RUF001
    )
    text_root, block = _text_root(text=quote)

    class _UnsupportedJudge:
        def judge(self, selections: tuple[Any, ...]) -> NeedEvidenceSemanticResult:
            receipts: list[NeedFacetSemanticReceipt] = []
            for selection in selections:
                facet = selection.need.need_facets[0]
                slice_ids = tuple(item.slice_id for item in selection.slices)
                receipts.append(
                    NeedFacetSemanticReceipt(
                        need_id=selection.need.need_id,
                        need_facet_id=facet.need_facet_id,
                        facet_kind=facet.facet_kind.value,
                        mandatory=True,
                        status=NeedEvidenceSemanticStatus.UNSUPPORTED,
                        evaluated_slice_ids=slice_ids,
                        unsupported_slice_ids=slice_ids,
                        reason="quotes talk about 离山剑宗 not 天海家",
                        judge_version="test-judge.v1",
                    )
                )
            return NeedEvidenceSemanticResult(tuple(receipts), ())

    covered = _gateway(
        tmp_path / "cover",
        _context(need, block, _evidence(block)),
        _UnsupportedJudge(),
    ).resolve(_request(need), text_root, thread_id="r1-relation-stem")
    assert (
        covered.context.retrieval_traces[0].facet_receipts[0].status is FacetClosureStatus.SUPPORTED
    )
    assert (
        covered.context.retrieval_traces[0].facet_receipts[0].stop_reason
        == "semantic_judge_unsupported_exact_l0_answering_unit"
    )
    assert covered.selected_result.mandatory_need_facets_closed == 1

    miss_root, miss_block = _text_root(chapter_index=2, text="国教学院胜了离山剑宗。")
    missed_result = _gateway(
        tmp_path / "miss2",
        _context(need, miss_block, _evidence(miss_block)),
        _UnsupportedJudge(),
    ).resolve(_request(need, chapter=2), miss_root, thread_id="r1-relation-no-stem")
    assert (
        missed_result.context.retrieval_traces[0].facet_receipts[0].status
        is FacetClosureStatus.UNSUPPORTED
    )


def test_gateway_does_not_label_judged_live_l0_as_missing_evidence(tmp_path: Path) -> None:
    need = _need()
    text_root, block = _text_root()
    result = _gateway(
        tmp_path,
        _context(need, block, _evidence(block)),
        _Judge(supported=False),
    ).resolve(_request(need), text_root, thread_id="r1-judged-l0")
    trace = result.context.retrieval_traces[0]
    assert trace.l0_fallback_slice_ids
    assert trace.facet_receipts[0].status is FacetClosureStatus.UNSUPPORTED
    assert trace.facet_receipts[0].stop_reason != "no_exact_evidence_for_facet"
    assert trace.facet_receipts[0].stop_reason == "semantic_judge_unresolved"
    assert result.selected_result.mandatory_need_facets_closed == 0


def test_handled_groups_split_facet_needs_by_planned_draft_id() -> None:
    need = _need()
    relation = need.model_copy(
        update={"need_id": StableId("need.r1.relation.split"), "planned_draft_id": QUESTION.root}
    )
    causal = need.model_copy(
        update={
            "need_id": StableId("need.r1.causal.split"),
            "planned_draft_id": QUESTION.root,
            "need_facets": (
                need.need_facets[0].model_copy(
                    update={
                        "need_id": StableId("need.r1.causal.split"),
                        "need_facet_id": StableId("facet.r1.causal"),
                        "facet_kind": NeedFacetKind.CAUSAL_HISTORY,
                    }
                ),
            ),
            "completion_spec": need.completion_spec.model_copy(
                update={
                    "need_id": StableId("need.r1.causal.split"),
                    "required_need_facet_ids": (StableId("facet.r1.causal"),),
                    "irreducible_need_facet_ids": (StableId("facet.r1.causal"),),
                    "evidence_requirement_by_facet": {
                        "facet.r1.causal": FacetEvidenceRequirement.TRACEABLE_CUTOFF_SOURCE
                    },
                }
            )
            if need.completion_spec is not None
            else None,
        }
    )
    _text_root_doc, block = _text_root()
    evidence = _evidence(block)
    unsupported = _context(relation, block, evidence).retrieval_traces[0]
    supported_relation = unsupported.model_copy(
        update={
            "need_id": relation.need_id,
            "facet_receipts": (
                FacetEvidenceReceipt(
                    need_id=relation.need_id,
                    need_facet_id=relation.need_facets[0].need_facet_id,
                    facet_kind=relation.need_facets[0].facet_kind,
                    mandatory=True,
                    status=FacetClosureStatus.SUPPORTED,
                    stop_reason="semantic_judge_supported_exact_l0",
                ),
            ),
            "closed_need_facet_ids": (relation.need_facets[0].need_facet_id,),
        }
    )
    supported_causal = unsupported.model_copy(
        update={
            "need_id": causal.need_id,
            "facet_receipts": (
                FacetEvidenceReceipt(
                    need_id=causal.need_id,
                    need_facet_id=causal.need_facets[0].need_facet_id,
                    facet_kind=causal.need_facets[0].facet_kind,
                    mandatory=True,
                    status=FacetClosureStatus.SUPPORTED,
                    stop_reason="semantic_judge_supported_exact_l0",
                ),
            ),
            "closed_need_facet_ids": (causal.need_facets[0].need_facet_id,),
        }
    )
    assert (
        handled_question_ids_for_supported_needs(
            (QUESTION,), (relation, causal), (supported_relation, unsupported)
        )
        == ()
    )
    assert handled_question_ids_for_supported_needs(
        (QUESTION,), (relation, causal), (supported_relation, supported_causal)
    ) == (QUESTION,)


def test_handled_only_when_all_mandatory_facets_supported() -> None:
    need = _need()
    _text_root_doc, block = _text_root()
    evidence = _evidence(block)
    unsupported = _context(need, block, evidence).retrieval_traces[0]
    supported = unsupported.model_copy(
        update={
            "facet_receipts": (
                FacetEvidenceReceipt(
                    need_id=need.need_id,
                    need_facet_id=need.need_facets[0].need_facet_id,
                    facet_kind=need.need_facets[0].facet_kind,
                    mandatory=True,
                    status=FacetClosureStatus.SUPPORTED,
                    stop_reason="semantic_judge_supported_exact_l0",
                ),
            ),
            "closed_need_facet_ids": (need.need_facets[0].need_facet_id,),
        }
    )
    assert mandatory_facet_receipts_supported(need, unsupported) is False
    assert mandatory_facet_receipts_supported(need, supported) is True
    assert handled_question_ids_for_supported_needs((QUESTION,), (need,), (unsupported,)) == ()
    assert handled_question_ids_for_supported_needs((QUESTION,), (need,), (supported,)) == (
        QUESTION,
    )


def test_both_structured_and_l0_unsupported_is_unresolved_not_false_handled() -> None:
    need = _need()
    _text_root_doc, block = _text_root()
    trace = _context(need, block, _evidence(block)).retrieval_traces[0]
    assert mandatory_facet_receipts_supported(need, trace) is False
    handled = handled_question_ids_for_supported_needs((QUESTION,), (need,), (trace,))
    assert handled == ()
    assert "PLANNER_MEMORY_NO_PROGRESS" not in handled
    unresolved = any(
        not mandatory_facet_receipts_supported(need, trace)
        for _need in (need,)
        if _need.requirement.value == "mandatory"
    )
    assert unresolved is True


def test_live_helper_rejects_only_current_basis_keys() -> None:
    text_root, block = _text_root()
    evidence = _evidence(block, root_hash=CROSS_ROOT, resolved_at_commit=STALE_COMMIT)
    blocks, chapters = text_root_indexes(text_root)
    resolver = EvidenceSliceResolver()
    basis = LiveEvidenceBasis(
        request_commit=COMMIT,
        request_snapshot_id=SNAPSHOT,
        checkpoint_chapter=1,
    )
    live = resolver.live_decision(
        basis=basis,
        unit_source_commit=COMMIT,
        unit_snapshot_id=SNAPSHOT,
        evidence=evidence,
        block=blocks[block.block_id],
        chapter_index=chapters[block.chapter_id],
    )
    assert live.live is True
    mutated = resolver.live_decision(
        basis=basis,
        unit_source_commit=COMMIT,
        unit_snapshot_id=SNAPSHOT,
        evidence=evidence,
        block=block.model_copy(update={"text": "甲与乙已经决裂。"}),
        chapter_index=1,
    )
    assert mutated.live is False
    assert mutated.reason in {"hash_mismatch", "quote_mismatch", "span_mismatch"}
    cutoff = resolver.live_decision(
        basis=basis,
        unit_source_commit=COMMIT,
        unit_snapshot_id=SNAPSHOT,
        evidence=evidence,
        block=block,
        chapter_index=2,
    )
    assert cutoff.live is False
    assert cutoff.reason == "cutoff"
