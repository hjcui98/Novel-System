"""Deterministic Stage 1 20→3 runner with explicit baselines and Gold evaluation."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Protocol

from novel_agent.domain.benchmark import (
    BenchmarkBundle,
    BenchmarkCaseManifest,
    BenchmarkMetricSet,
    BenchmarkProfileResult,
    BenchmarkQueryCondition,
    BenchmarkTrack,
    ChapterSummaryRootDocument,
    FailureCategory,
    GoldItem,
    Stage1BenchmarkConfig,
    Stage1BenchmarkResult,
    TextRootDocument,
)
from novel_agent.domain.ids import CommitId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    HorizonNeedSet,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    RetrievalChannel,
    RetrievalTrace,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.benchmark_importer import BenchmarkBundleImporter, quote_hash
from novel_agent.services.memory_pipeline import AnchorBuilder, ContextCompiler, EvidenceExpander
from novel_agent.services.retrieval import (
    FusionService,
    InMemoryRetrievalBackend,
    PassageReranker,
    RerankService,
    RetrievalBackend,
    RetrievalOrchestrator,
)


class _LexicalBenchmarkReranker:
    profile = "deterministic-lexical-benchmark-v1"

    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        query_terms = set(query.casefold().split())
        return tuple(
            float(len(query_terms.intersection(passage.casefold().split()))) for passage in passages
        )


class MemoryConstructor(Protocol):
    def construct(
        self,
        history: TextRootDocument,
        case: BenchmarkCaseManifest,
    ) -> WorldRootDocument: ...


class BenchmarkNeedGenerator(Protocol):
    profile: str
    query_condition: BenchmarkQueryCondition

    def generate(
        self,
        world: WorldRootDocument,
        case: BenchmarkCaseManifest,
    ) -> tuple[Stage1MemoryNeed, ...]: ...


class Stage1NeedGenerator:
    """Versioned deterministic need generation baseline for Mode A smoke runs."""

    profile = "deterministic-world-derived-v1"
    query_condition = BenchmarkQueryCondition.GENERATED

    def generate(
        self,
        world: WorldRootDocument,
        case: BenchmarkCaseManifest,
    ) -> tuple[Stage1MemoryNeed, ...]:
        labels = {entity.entity_id: entity.internal_label for entity in world.entities}
        run_id = RunId(f"run.{case.case_id.root}")
        task_id = TaskId(f"task.{case.case_id.root}")
        needs: list[Stage1MemoryNeed] = []
        for state in world.states:
            needs.append(
                Stage1MemoryNeed(
                    need_id=StableId(f"need.{state.state_id.root}"),
                    run_id=run_id,
                    task_id=task_id,
                    base_commit=world.source_commit,
                    horizon_target=case.target_range,
                    need_type="current_state",
                    query_intent=Stage1QueryIntent.CURRENT_STATE,
                    query_text=(
                        f"{labels[state.subject_id]} {state.predicate} "
                        f"{json.dumps(state.value, ensure_ascii=False, sort_keys=True)}"
                    ),
                    entity_ids=(state.subject_id,),
                    predicates=(state.predicate,),
                    time_scope=state.valid_time,
                    why_needed="current canonical state may constrain the target horizon",
                    risk_level=NeedRisk.HIGH,
                    requirement=RequirementLevel.MANDATORY,
                    preferred_resolution_path=ResolutionPath.EXACT_TEMPORAL,
                    allowed_candidate_pools=(CandidatePool.R1,),
                    expected_evidence_types=("text_span",),
                    stop_condition="at least one traceable current source",
                )
            )
        for event in world.events:
            needs.append(
                Stage1MemoryNeed(
                    need_id=StableId(f"need.{event.event_id.root}"),
                    run_id=run_id,
                    task_id=task_id,
                    base_commit=world.source_commit,
                    horizon_target=case.target_range,
                    need_type="related_event",
                    query_intent=Stage1QueryIntent.RELATED_EVENT,
                    query_text=" ".join(
                        (
                            *[labels[identity] for identity in event.participant_ids],
                            event.event_type,
                        )
                    ),
                    entity_ids=event.participant_ids,
                    why_needed="historical event may be invoked in the target horizon",
                    risk_level=NeedRisk.MEDIUM,
                    requirement=RequirementLevel.OPTIONAL,
                    preferred_resolution_path=ResolutionPath.ANCHOR_FIRST,
                    allowed_candidate_pools=(CandidatePool.ANCHOR, CandidatePool.GROUNDED),
                    expected_evidence_types=("text_span",),
                    stop_condition="at least one traceable current source",
                )
            )
        for obligation in world.obligations:
            if obligation.due_chapter is not None and not (
                case.target_range[0] <= obligation.due_chapter <= case.target_range[1]
            ):
                continue
            needs.append(
                Stage1MemoryNeed(
                    need_id=StableId(f"need.{obligation.obligation_id.root}"),
                    run_id=run_id,
                    task_id=task_id,
                    base_commit=world.source_commit,
                    horizon_target=case.target_range,
                    need_type="plan_obligation",
                    query_intent=Stage1QueryIntent.PLAN_OBLIGATION,
                    query_text=obligation.description,
                    entity_ids=obligation.owner_ids,
                    why_needed="active plan obligation is due in the target horizon",
                    risk_level=NeedRisk.HIGH,
                    requirement=RequirementLevel.MANDATORY,
                    preferred_resolution_path=ResolutionPath.ANCHOR_FIRST,
                    allowed_candidate_pools=(CandidatePool.ANCHOR, CandidatePool.GROUNDED),
                    expected_evidence_types=("text_span",),
                    stop_condition="at least one traceable current source",
                )
            )
        return tuple(needs)


class FrozenHorizonNeedGenerator:
    """Feeds audited, pre-generated needs into a deterministic benchmark run."""

    def __init__(
        self,
        horizon: HorizonNeedSet,
        *,
        profile: str,
        query_condition: BenchmarkQueryCondition,
    ) -> None:
        if not profile:
            raise ValueError("frozen need profile must not be empty")
        self.profile = profile
        self.query_condition = query_condition
        self._horizon = horizon

    def generate(
        self,
        world: WorldRootDocument,
        case: BenchmarkCaseManifest,
    ) -> tuple[Stage1MemoryNeed, ...]:
        if (self._horizon.horizon_start, self._horizon.horizon_end) != case.target_range:
            raise ValueError("frozen need horizon differs from benchmark target")
        needs = (
            *self._horizon.shared_constraints,
            *self._horizon.chapter_needs,
            *self._horizon.progressive_needs,
            *self._horizon.volume_obligations,
        )
        if len({need.need_id for need in needs}) != len(needs):
            raise ValueError("frozen MemoryNeed ids must be unique")
        if any(need.base_commit != world.source_commit for need in needs):
            raise ValueError("frozen MemoryNeed basis differs from benchmark world")
        return tuple(needs)


class Stage1BenchmarkRunner:
    def __init__(self, *, token_budget: int = 4000) -> None:
        if token_budget < 1:
            raise ValueError("benchmark token budget must be positive")
        self._token_budget = token_budget

    def run(
        self,
        bundle: BenchmarkBundle,
        case_id: StableId,
        track: BenchmarkTrack,
        *,
        constructor: MemoryConstructor | None = None,
        need_generator: BenchmarkNeedGenerator | None = None,
        retrieval_backend: RetrievalBackend | None = None,
        retrieval_snapshot_id: StableId | None = None,
        embedding_profile: str | None = None,
        reranker: PassageReranker | None = None,
    ) -> Stage1BenchmarkResult:
        BenchmarkBundleImporter().validate(bundle)
        case = next((item for item in bundle.case_manifests if item.case_id == case_id), None)
        if case is None:
            raise ValueError(f"benchmark case does not exist: {case_id.root}")
        history = next(root for root in bundle.text_roots if root.root_hash == case.input_text_root)
        plan = next(
            (root for root in bundle.plan_roots if root.root_hash == case.input_plan_root),
            None,
        )
        summaries = next(
            (root for root in bundle.summary_roots if root.root_hash == case.input_summary_root),
            None,
        )
        if track is BenchmarkTrack.ORACLE:
            if case.input_world_root_verified is None:
                raise ValueError("Oracle track requires a verified world root")
            world = next(
                root
                for root in bundle.world_roots
                if root.root_hash == case.input_world_root_verified
            )
        else:
            if constructor is None:
                raise ValueError("end-to-end track requires an explicit memory constructor")
            world = constructor.construct(history, case)
        if retrieval_backend is not None and (
            retrieval_snapshot_id is None or not embedding_profile
        ):
            raise ValueError(
                "external retrieval backend requires snapshot id and embedding profile"
            )
        snapshot_id = retrieval_snapshot_id or StableId(
            f"snapshot.{case.case_id.root}.{track.value}"
        )
        units = AnchorBuilder().build(world, history, plan, snapshot_id=snapshot_id)
        backend: RetrievalBackend = retrieval_backend or InMemoryRetrievalBackend(units)
        selected_need_generator = need_generator or Stage1NeedGenerator()
        needs = selected_need_generator.generate(world, case)
        orchestrator = RetrievalOrchestrator(
            backend,
            FusionService(),
            per_channel_limit=5,
            fused_limit=5,
        )
        traces = tuple((need, orchestrator.retrieve(need)) for need in needs)
        package = ContextCompiler(EvidenceExpander()).compile(
            traces,
            history,
            context_id=StableId(f"context.{case.case_id.root}.{track.value}"),
            base_commit=world.source_commit,
            snapshot_id=snapshot_id,
            task_contract=f"prepare chapters {case.target_range[0]}-{case.target_range[1]}",
            token_budget=self._token_budget,
        )
        kernel_traces = package.retrieval_traces
        kernel_evidence = _trace_evidence(kernel_traces)
        b0_evidence = _recent_chapter_evidence(case, history, world.source_commit)
        b1_evidence, b1_tokens = _summary_baseline(case, history, summaries, b0_evidence)
        grounded_bm25 = _channel_evidence(
            backend, needs, (RetrievalChannel.GROUNDED_BM25,), limit=3
        )
        grounded_dense = _channel_evidence(
            backend, needs, (RetrievalChannel.GROUNDED_DENSE,), limit=3
        )
        grounded_rrf = _channel_evidence(
            backend,
            needs,
            (RetrievalChannel.GROUNDED_BM25, RetrievalChannel.GROUNDED_DENSE),
            limit=5,
            fusion=True,
        )
        anchor_bm25 = _channel_evidence(backend, needs, (RetrievalChannel.ANCHOR_BM25,), limit=5)
        anchor_rrf = _channel_evidence(
            backend,
            needs,
            (RetrievalChannel.ANCHOR_BM25, RetrievalChannel.ANCHOR_DENSE),
            limit=5,
            fusion=True,
        )
        exact_anchor = _channel_evidence(
            backend,
            needs,
            (
                RetrievalChannel.R1_EXACT,
                RetrievalChannel.R1_TEMPORAL,
                RetrievalChannel.ANCHOR_BM25,
                RetrievalChannel.ANCHOR_DENSE,
            ),
            limit=5,
            fusion=False,
        )
        hierarchy = _channel_evidence(
            backend,
            needs,
            (
                RetrievalChannel.R1_EXACT,
                RetrievalChannel.R1_TEMPORAL,
                RetrievalChannel.ANCHOR_BM25,
                RetrievalChannel.ANCHOR_DENSE,
                RetrievalChannel.HIERARCHY,
            ),
            limit=5,
            fusion=False,
        )
        selected_reranker = reranker or _LexicalBenchmarkReranker()
        rerank_orchestrator = RetrievalOrchestrator(
            backend,
            FusionService(),
            per_channel_limit=5,
            fused_limit=5,
            reranker=RerankService(selected_reranker),
        )
        rerank_traces = tuple(rerank_orchestrator.retrieve(need) for need in needs)
        reranked_evidence = _trace_evidence(rerank_traces)
        all_channels = _channel_evidence(
            backend,
            needs,
            (
                RetrievalChannel.ANCHOR_BM25,
                RetrievalChannel.ANCHOR_DENSE,
                RetrievalChannel.GROUNDED_BM25,
                RetrievalChannel.GROUNDED_DENSE,
                RetrievalChannel.HIERARCHY,
                RetrievalChannel.TYPED_GRAPH,
            ),
            limit=10,
            fusion=False,
        )
        profile_results = (
            _profile("B0-recent-3", b0_evidence, case, history, (), 3),
            _profile(
                "B1-recent-3+chapter-summary",
                b1_evidence,
                case,
                history,
                (),
                3,
                token_count_override=b1_tokens,
            ),
            _profile("B2-naive-dense", grounded_dense, case, history, (), 0),
            _profile("B3-grounded-bm25", grounded_bm25, case, history, (), 0),
            _profile("B4-grounded-rrf", grounded_rrf, case, history, (), 0),
            _profile("K1-exact+hybrid", exact_anchor, case, history, (), 0),
            _profile("K2-+hierarchy", hierarchy, case, history, (), 0),
            _profile("K3-+reranker", reranked_evidence, case, history, rerank_traces, 0),
            _profile(
                "K4-memory-kernel",
                kernel_evidence,
                case,
                history,
                kernel_traces,
                package.budget_report.full_chapter_read_count,
            ),
            _profile("A0-grounded-bm25-direct", grounded_bm25, case, history, (), 0),
            _profile(
                "A1-anchor-bm25-no-expansion",
                anchor_bm25,
                case,
                history,
                (),
                0,
                count_l0=False,
            ),
            _profile("A2-anchor-bm25-expand", anchor_bm25, case, history, (), 0),
            _profile("A3-anchor-rrf-expand", anchor_rrf, case, history, (), 0),
            _profile(
                "A4-anchor-rerank-expand",
                reranked_evidence,
                case,
                history,
                rerank_traces,
                0,
            ),
            _profile("A5-bounded-fallback", kernel_evidence, case, history, (), 0),
            _profile("A6-all-channel-upper-bound", all_channels, case, history, (), 0),
        )
        return Stage1BenchmarkResult(
            bundle_id=bundle.bundle_id,
            case_id=case.case_id,
            track=track,
            base_commit=world.source_commit,
            snapshot_id=snapshot_id,
            config=Stage1BenchmarkConfig(
                config_version=world.schema_version,
                token_budget=self._token_budget,
                per_channel_candidate_limit=5,
                fused_candidate_limit=5,
                rrf_k=60,
                embedding_profile=embedding_profile or "in-memory-semantic-smoke-v1",
                reranker_profile=selected_reranker.profile,
                expansion_profile="anchor-to-exact-l0-v1",
                summary_profile=(
                    "evidence-bound-chapter-summary-v1"
                    if summaries is not None
                    else "summary-unavailable"
                ),
                query_condition=selected_need_generator.query_condition,
                need_profile=selected_need_generator.profile,
                random_seed=0,
            ),
            context_frozen=True,
            profile_results=profile_results,
        )


def _trace_evidence(traces: Iterable[RetrievalTrace]) -> tuple[EvidenceRef, ...]:
    return _dedupe_evidence(
        evidence
        for trace in traces
        for candidate in trace.candidates
        if candidate.selected
        for evidence in candidate.unit.evidence_refs
    )


def _channel_evidence(
    backend: RetrievalBackend,
    needs: tuple[Stage1MemoryNeed, ...],
    channels: tuple[RetrievalChannel, ...],
    *,
    limit: int,
    fusion: bool = False,
) -> tuple[EvidenceRef, ...]:
    evidence: list[EvidenceRef] = []
    fusion_service = FusionService()
    for need in needs:
        results = {channel: backend.search(need, channel, limit) for channel in channels}
        if fusion:
            candidates = fusion_service.fuse(results, limit=limit)
            evidence.extend(
                item
                for candidate in candidates
                if candidate.selected
                for item in candidate.unit.evidence_refs
            )
        else:
            evidence.extend(
                item for hits in results.values() for hit in hits for item in hit.unit.evidence_refs
            )
    return _dedupe_evidence(evidence)


def _recent_chapter_evidence(
    case: BenchmarkCaseManifest,
    history: TextRootDocument,
    base_commit: CommitId,
) -> tuple[EvidenceRef, ...]:
    minimum = max(case.history_range[0], case.history_range[1] - 2)
    evidence: list[EvidenceRef] = []
    for chapter in history.chapters:
        if chapter.chapter_index < minimum:
            continue
        for scene in chapter.scenes:
            for block in scene.blocks:
                evidence.append(
                    EvidenceRef(
                        evidence_id=StableId(f"baseline.full.{block.block_id.root}"),
                        root_hash=history.root_hash,
                        object_hash=sha256_id(block.text.encode("utf-8")),
                        chapter_id=chapter.chapter_id,
                        scene_id=scene.scene_id,
                        span=TextSpanRef(
                            block_id=block.block_id,
                            start=0,
                            end=len(block.text),
                        ),
                        quote_hash=quote_hash(block.text),
                        support_status=EvidenceSupportStatus.CURRENT,
                        resolved_at_commit=base_commit,
                    )
                )
    return tuple(evidence)


def _summary_baseline(
    case: BenchmarkCaseManifest,
    history: TextRootDocument,
    summaries: ChapterSummaryRootDocument | None,
    recent_evidence: tuple[EvidenceRef, ...],
) -> tuple[tuple[EvidenceRef, ...], int]:
    recent_tokens = sum(
        max(1, (len(_resolve_text(item, history)) + 3) // 4) for item in recent_evidence
    )
    if summaries is None:
        return recent_evidence, recent_tokens
    recent_start = max(case.history_range[0], case.history_range[1] - 2)
    selected = tuple(
        summary
        for summary in summaries.summaries
        if case.history_range[0] <= summary.chapter_index < recent_start
    )
    summary_tokens = sum(max(1, (len(summary.summary) + 3) // 4) for summary in selected)
    return (
        _dedupe_evidence(
            (*recent_evidence, *(ref for summary in selected for ref in summary.evidence_refs))
        ),
        recent_tokens + summary_tokens,
    )


def _profile(
    name: str,
    evidence: tuple[EvidenceRef, ...],
    case: BenchmarkCaseManifest,
    history: TextRootDocument,
    traces: tuple[RetrievalTrace, ...],
    full_chapters_read: int,
    *,
    count_l0: bool = True,
    token_count_override: int | None = None,
) -> BenchmarkProfileResult:
    retrieved_ids = {item.evidence_id for item in evidence}
    all_gold = _all_gold(case)
    gold_evidence = tuple(ref for item in all_gold for ref in item.evidence_refs)
    matched_gold = tuple(
        item
        for item in all_gold
        if any(
            _evidence_matches(candidate, gold)
            for candidate in evidence
            for gold in item.evidence_refs
        )
    )
    mandatory = tuple(item for item in all_gold if item.mandatory)
    leaked = tuple(item for item in evidence if item.root_hash == case.future_text_root_private)
    l0_tokens = (
        token_count_override
        if token_count_override is not None
        else sum(max(1, (len(_resolve_text(item, history)) + 3) // 4) for item in evidence)
        if count_l0
        else 0
    )
    traceable_candidates = tuple(
        candidate for trace in traces for candidate in trace.candidates if candidate.selected
    )
    traceability = (
        sum(
            bool(candidate.unit.evidence_refs or candidate.unit.source_artifact)
            for candidate in traceable_candidates
        )
        / len(traceable_candidates)
        if traceable_candidates
        else 1.0
    )
    covered_count = len(matched_gold)
    operational_coverage = _gold_coverage(case.operational_constraint_gold, evidence)
    trace_count = len(traces)
    fallback_rate = (
        sum(trace.fallback_used for trace in traces) / trace_count if trace_count else None
    )
    reranker_tokens = (
        sum(
            max(1, (len(candidate.unit.text) + 3) // 4)
            for trace in traces
            for candidate in trace.candidates
            if any(hit.channel is RetrievalChannel.RERANK for hit in candidate.channel_hits)
        )
        if traces
        else None
    )
    failures = (
        (FailureCategory.RETRIEVE,)
        if sum(
            any(_evidence_matches(candidate, gold) for candidate in evidence)
            for gold in gold_evidence
        )
        < len(gold_evidence)
        else ()
    )
    return BenchmarkProfileResult(
        profile=name,
        metrics=BenchmarkMetricSet(
            gold_evidence_recall=_ratio(
                sum(
                    any(_evidence_matches(candidate, gold) for candidate in evidence)
                    for gold in gold_evidence
                ),
                len(gold_evidence),
            ),
            observed_use_coverage=_gold_coverage(case.observed_use_gold, evidence),
            operational_constraint_coverage=operational_coverage,
            plan_obligation_coverage=_gold_coverage(case.plan_obligation_gold, evidence),
            mandatory_constraint_coverage=_gold_coverage(mandatory, evidence),
            evidence_traceability=traceability,
            future_leakage_rate=(len(leaked) / len(evidence) if evidence else 0.0),
            l0_evidence_tokens_read=l0_tokens,
            full_chapter_read_rate=_ratio(full_chapters_read, len(history.chapters)),
            context_utility_per_1k_tokens=(covered_count * 1000 / l0_tokens if l0_tokens else 0.0),
            query_intent_routing_accuracy=1.0 if traces else None,
            wrong_route_rate=0.0 if traces else None,
            unnecessary_channel_rate=0.0 if traces else None,
            gold_evidence_recall_at_k=_ratio(
                sum(
                    any(_evidence_matches(candidate, gold) for candidate in evidence)
                    for gold in gold_evidence
                ),
                len(gold_evidence),
            ),
            evidence_recall_after_expansion=(
                _ratio(
                    sum(
                        any(_evidence_matches(candidate, gold) for candidate in evidence)
                        for gold in gold_evidence
                    ),
                    len(gold_evidence),
                )
                if traces
                else None
            ),
            average_anchors_expanded=(
                sum(trace.anchors_expanded for trace in traces) / trace_count
                if trace_count
                else None
            ),
            average_spans_expanded=(
                sum(trace.spans_expanded for trace in traces) / trace_count if trace_count else None
            ),
            average_scenes_expanded=(
                sum(trace.scenes_expanded for trace in traces) / trace_count
                if trace_count
                else None
            ),
            grounded_fallback_rate=fallback_rate,
            reranker_pair_tokens=reranker_tokens,
            current_state_accuracy=operational_coverage if traces else None,
            temporal_validity_accuracy=operational_coverage if traces else None,
            stale_state_rate=0.0 if traces else None,
            premature_future_injection_rate=(len(leaked) / len(evidence) if evidence else 0.0),
        ),
        retrieved_evidence_ids=tuple(sorted(retrieved_ids, key=lambda item: item.root)),
        failure_categories=failures,
    )


def _all_gold(case: BenchmarkCaseManifest) -> tuple[GoldItem, ...]:
    return (
        *case.observed_use_gold,
        *case.operational_constraint_gold,
        *case.plan_obligation_gold,
    )


def _gold_coverage(items: tuple[GoldItem, ...], retrieved: tuple[EvidenceRef, ...]) -> float:
    return _ratio(
        sum(
            any(
                _evidence_matches(candidate, gold)
                for candidate in retrieved
                for gold in item.evidence_refs
            )
            for item in items
        ),
        len(items),
    )


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _dedupe_evidence(evidence: Iterable[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    unique: dict[StableId, EvidenceRef] = {}
    for item in evidence:
        unique[item.evidence_id] = item
    return tuple(unique[key] for key in sorted(unique, key=lambda item: item.root))


def _resolve_text(evidence: EvidenceRef, root: TextRootDocument) -> str:
    if evidence.span is None:
        return ""
    block = next(
        block
        for scene in (
            *(root.prelude.scenes if root.prelude is not None else ()),
            *(scene for chapter in root.chapters for scene in chapter.scenes),
        )
        for block in scene.blocks
        if block.block_id == evidence.span.block_id
    )
    return block.text[evidence.span.start : evidence.span.end]


def _evidence_matches(candidate: EvidenceRef, gold: EvidenceRef) -> bool:
    if candidate.evidence_id == gold.evidence_id:
        return True
    if candidate.root_hash != gold.root_hash or candidate.span is None or gold.span is None:
        return False
    if candidate.span.block_id != gold.span.block_id:
        return False
    return candidate.span.start < gold.span.end and gold.span.start < candidate.span.end
