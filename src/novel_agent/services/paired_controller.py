"""Fair deterministic-vs-bounded-controller execution on one immutable retrieval basis."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory import (
    ChannelHit,
    FusedCandidate,
    RequirementLevel,
    RetrievalChannel,
    RetrievalStopReason,
    RetrievalTrace,
    RetrievalUnit,
    Stage1ContextPackage,
    Stage1MemoryNeed,
)
from novel_agent.domain.retrieval_routing import RoutePlan
from novel_agent.domain.stage2 import (
    ControllerArm,
    ControllerStopReason,
    MemoryResolutionRequest,
    PairedContextArmResult,
    PairedContextComparison,
    ToolPolicy,
)
from novel_agent.runtime.memory_controller import (
    BoundedMemoryController,
    ControllerPolicy,
)
from novel_agent.services.memory_pipeline import ContextCompiler
from novel_agent.services.retrieval import (
    FusionService,
    RerankService,
    RetrievalBackend,
    RetrievalOrchestrator,
)
from novel_agent.tools import RetrievalToolAdapter, ToolBinding
from novel_agent.tools.retrieval import PLAN_INTENTS


class _BudgetedBackend:
    def __init__(self, backend: RetrievalBackend, max_calls: int) -> None:
        self._backend = backend
        self._max_calls = max_calls
        self.call_count = 0
        self.exhausted = False

    def search(
        self,
        need: Stage1MemoryNeed,
        channel: RetrievalChannel,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        if self.call_count >= self._max_calls:
            self.exhausted = True
            return ()
        self.call_count += 1
        return self._backend.search(need, channel, limit)


class PairedMemoryControllerRunner:
    def __init__(
        self,
        backend: RetrievalBackend,
        controller: BoundedMemoryController,
        compiler: ContextCompiler,
        comparison_basis_fingerprint: ArtifactId,
        freshness_check: Callable[[MemoryResolutionRequest], bool],
        route_plans: tuple[RoutePlan, ...] = (),
        reranker: RerankService | None = None,
    ) -> None:
        self._backend = backend
        self._controller = controller
        self._compiler = compiler
        self._basis_fingerprint = comparison_basis_fingerprint
        self._freshness_check = freshness_check
        self._route_plans = {plan.need_id: plan for plan in route_plans}
        self._reranker = reranker
        if len(self._route_plans) != len(route_plans):
            raise ValueError("paired runner route plans must have unique memory need ids")

    @property
    def comparison_basis_fingerprint(self) -> ArtifactId:
        return self._basis_fingerprint

    @classmethod
    def from_shared_backend(
        cls,
        *,
        backend: RetrievalBackend,
        needs: tuple[Stage1MemoryNeed, ...],
        tool_policy: ToolPolicy,
        compiler: ContextCompiler,
        controller_policy: ControllerPolicy,
        freshness_check: Callable[[MemoryResolutionRequest], bool],
        checkpointer: Any,
        comparison_basis_fingerprint: ArtifactId,
        route_plans: tuple[RoutePlan, ...] = (),
        reranker: RerankService | None = None,
    ) -> PairedMemoryControllerRunner:
        active_channels_by_need = {
            plan.need_id: tuple(
                dict.fromkeys(
                    step.channel
                    for step in (
                        *plan.mandatory_steps,
                        *(step for group in plan.primary_groups for step in group.steps),
                        *(
                            step
                            for fallback in plan.conditional_fallbacks
                            for step in fallback.steps
                        ),
                    )
                )
            )
            for plan in route_plans
        }
        adapter = RetrievalToolAdapter(
            backend,
            needs,
            allowed_channels_by_need=active_channels_by_need or None,
        )
        binding = ToolBinding(tool_policy, adapter.handlers())
        controller = BoundedMemoryController(
            binding,
            tool_policy,
            compiler,
            controller_policy,
            freshness_check,
            checkpointer,
            route_plans=route_plans,
            reranker=reranker,
        )
        return cls(
            backend,
            controller,
            compiler,
            comparison_basis_fingerprint,
            freshness_check,
            route_plans,
            reranker,
        )

    def run(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        *,
        thread_id: str,
        evaluator_only_artifacts: tuple[ArtifactId, ...] = (),
    ) -> PairedContextComparison:
        deterministic = self.run_deterministic(
            request,
            text_root,
            evaluator_only_artifacts=evaluator_only_artifacts,
        )
        agentic = self.run_agentic(
            request,
            text_root,
            thread_id=thread_id,
            evaluator_only_artifacts=evaluator_only_artifacts,
        )
        return self.compare(request, deterministic, agentic)

    def run_agentic(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        *,
        thread_id: str,
        evaluator_only_artifacts: tuple[ArtifactId, ...] = (),
    ) -> PairedContextArmResult:
        agentic_run = self._controller.resolve(
            request,
            text_root,
            thread_id=f"{thread_id}.agentic",
        )
        agentic_context = agentic_run["context"]
        agentic_leaks = self._leakage_count(agentic_context, evaluator_only_artifacts)
        return PairedContextArmResult(
            arm=ControllerArm.BOUNDED_R2,
            context=agentic_context,
            selected_unit_ids=self._selected_ids(agentic_context),
            retrieval_call_count=len(agentic_run["tool_results"]),
            stop_reason=agentic_run["resolution"].stop_reason,
            comparison_basis_fingerprint=self._basis_fingerprint,
            future_leakage_count=agentic_leaks,
        )

    def compare(
        self,
        request: MemoryResolutionRequest,
        deterministic: PairedContextArmResult,
        agentic: PairedContextArmResult,
    ) -> PairedContextComparison:
        if deterministic.arm is not ControllerArm.DETERMINISTIC:
            raise ValueError("paired deterministic input has the wrong arm")
        if agentic.arm is not ControllerArm.BOUNDED_R2:
            raise ValueError("paired agentic input has the wrong arm")
        if (
            deterministic.comparison_basis_fingerprint != self._basis_fingerprint
            or agentic.comparison_basis_fingerprint != self._basis_fingerprint
        ):
            raise ValueError("paired inputs do not share the runner comparison basis")
        blockers: list[str] = []
        if deterministic.future_leakage_count:
            blockers.append("deterministic arm contains evaluator-only artifacts")
        if agentic.future_leakage_count:
            blockers.append("agentic arm contains evaluator-only artifacts")
        return PairedContextComparison(
            pair_id=StableId(f"pair.{request.request_id.root}"),
            request_id=request.request_id,
            deterministic=deterministic,
            agentic=agentic,
            comparable=not blockers,
            blockers=tuple(blockers),
        )

    def run_deterministic(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        *,
        evaluator_only_artifacts: tuple[ArtifactId, ...] = (),
    ) -> PairedContextArmResult:
        budgeted = _BudgetedBackend(self._backend, request.retrieval_budget.max_tool_calls)
        traces: list[tuple[Stage1MemoryNeed, RetrievalTrace]] = []
        fresh = self._freshness_check(request)
        for need in request.initial_memory_needs:
            if not fresh or (need.query_intent in PLAN_INTENTS and not request.allow_future_plan):
                trace = self._empty_trace(need)
            else:
                plan = self._route_plans.get(need.need_id)
                trace = (
                    self._retrieve_route_plan(
                        budgeted,
                        need,
                        plan,
                        per_channel_limit=request.retrieval_budget.max_candidates,
                        reranker=self._reranker,
                    )
                    if plan is not None
                    else RetrievalOrchestrator(
                        budgeted,
                        FusionService(),
                        per_channel_limit=request.retrieval_budget.max_candidates,
                        fused_limit=request.retrieval_budget.max_candidates,
                    ).retrieve(need)
                )
            traces.append((need, trace))
        context = self._compiler.compile(
            tuple(traces),
            text_root,
            context_id=StableId(f"context.deterministic.{request.request_id.root}"),
            base_commit=request.base_commit,
            snapshot_id=request.snapshot_id,
            task_contract=request.task_contract,
            token_budget=request.context_budget.token_budget,
        )
        mandatory_gap = any(
            need.requirement is RequirementLevel.MANDATORY and not trace.candidates
            for need, trace in traces
        )
        stop_reason = (
            ControllerStopReason.FRESHNESS_BLOCKED
            if not fresh
            else ControllerStopReason.BUDGET_EXHAUSTED
            if budgeted.exhausted
            else ControllerStopReason.MANDATORY_GAP_UNRESOLVED
            if mandatory_gap
            else ControllerStopReason.SUFFICIENT
        )
        result = PairedContextArmResult(
            arm=ControllerArm.DETERMINISTIC,
            context=context,
            selected_unit_ids=self._selected_ids(context),
            retrieval_call_count=budgeted.call_count,
            stop_reason=stop_reason,
            comparison_basis_fingerprint=self._basis_fingerprint,
            future_leakage_count=0,
        )
        return result.model_copy(
            update={
                "future_leakage_count": self._leakage_count(
                    result.context,
                    evaluator_only_artifacts,
                )
            }
        )

    @staticmethod
    def _retrieve_route_plan(
        backend: RetrievalBackend,
        need: Stage1MemoryNeed,
        plan: RoutePlan,
        *,
        per_channel_limit: int,
        reranker: RerankService | None = None,
    ) -> RetrievalTrace:
        """Execute only the registered channels for one Need.

        The legacy ``RetrievalOrchestrator`` is intentionally retained for
        scripted Stage 1 callers.  A Stage 2R pair receives a RoutePlan,
        though, so its deterministic arm must not reconstruct a route from
        the old global intent table.  This method is the single owner of the
        plan's deterministic fallback semantics and applies RRF only to a
        declared parallel group.
        """

        if plan.need_id != need.need_id or plan.base_commit != need.base_commit:
            raise ValueError("route plan does not belong to deterministic memory need")
        fusion = FusionService()
        results: dict[RetrievalChannel, tuple[ChannelHit, ...]] = {}
        candidates: tuple[FusedCandidate, ...] = ()
        called: list[RetrievalChannel] = []

        def execute(channels: tuple[RetrievalChannel, ...], *, use_rrf: bool) -> None:
            nonlocal candidates
            group_results: dict[RetrievalChannel, tuple[ChannelHit, ...]] = {}
            for channel in channels:
                if channel in results:
                    continue
                hits = backend.search(need, channel, per_channel_limit)
                results[channel] = hits
                group_results[channel] = hits
                called.append(channel)
            if not group_results:
                return
            group_candidates = (
                fusion.fuse(group_results, limit=per_channel_limit)
                if use_rrf and len(group_results) > 1
                else PairedMemoryControllerRunner._direct_candidates(
                    group_results, limit=per_channel_limit
                )
            )
            candidates = PairedMemoryControllerRunner._merge_candidates(
                candidates, group_candidates, limit=per_channel_limit
            )

        execute(tuple(step.channel for step in plan.mandatory_steps), use_rrf=False)
        for group in plan.primary_groups:
            execute(
                tuple(step.channel for step in group.steps),
                use_rrf=group.fusion_profile is not None,
            )

        fallback_used = False
        fallback_reason: str | None = None
        for fallback in plan.conditional_fallbacks:
            if not PairedMemoryControllerRunner._fallback_applies(fallback.condition, candidates):
                continue
            fallback_used = True
            fallback_reason = fallback.condition
            steps = tuple(step.channel for step in fallback.steps)
            execute(steps, use_rrf=fallback.fusion_profile is not None)

        fusion_applied = any(group.fusion_profile is not None for group in plan.primary_groups) or (
            fallback_used
            and any(item.fusion_profile is not None for item in plan.conditional_fallbacks)
        )
        rerank_applied = False
        rerank_failure: str | None = None
        if fusion_applied and reranker is not None:
            try:
                candidates = reranker.rerank(need, candidates, limit=per_channel_limit)
                rerank_applied = True
            except Exception as error:
                rerank_failure = f"reranker_degraded:{type(error).__name__}"
        selected = tuple(candidate for candidate in candidates if candidate.selected)
        return RetrievalTrace(
            need_id=need.need_id,
            intent=need.query_intent,
            allowed_channels=tuple(called),
            channel_candidate_counts={
                channel: hits[0].candidate_count if hits else 0 for channel, hits in results.items()
            },
            candidates=candidates,
            fusion_applied=fusion_applied,
            rerank_applied=rerank_applied,
            channel_failures=(
                {RetrievalChannel.RERANK: rerank_failure} if rerank_failure is not None else {}
            ),
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            stop_reason=(
                RetrievalStopReason.EXACT_SATISFIED
                if selected and not fallback_used
                else RetrievalStopReason.BUDGET_SATISFIED
                if selected
                else RetrievalStopReason.FALLBACK_EXHAUSTED
                if fallback_used
                else RetrievalStopReason.CANDIDATES_EXHAUSTED
            ),
        )

    @staticmethod
    def _direct_candidates(
        results: dict[RetrievalChannel, tuple[ChannelHit, ...]],
        *,
        limit: int,
    ) -> tuple[FusedCandidate, ...]:
        hits: list[ChannelHit] = []
        seen: set[StableId] = set()
        for channel_hits in results.values():
            for hit in channel_hits:
                if hit.unit.unit_id in seen:
                    continue
                seen.add(hit.unit.unit_id)
                hits.append(hit)
        return tuple(
            FusedCandidate(
                unit=hit.unit,
                fused_rank=rank,
                rrf_score=1.0 / rank,
                channel_hits=(hit,),
                selected=rank <= limit or hit.unit.mandatory,
                rejection_reason=(
                    None if rank <= limit or hit.unit.mandatory else "direct_result_limit"
                ),
            )
            for rank, hit in enumerate(hits, start=1)
        )

    @staticmethod
    def _merge_candidates(
        existing: tuple[FusedCandidate, ...],
        incoming: tuple[FusedCandidate, ...],
        *,
        limit: int,
    ) -> tuple[FusedCandidate, ...]:
        """Keep route-stage order without making a second cross-channel fusion pass."""

        unique: dict[StableId, FusedCandidate] = {}
        for candidate in (*existing, *incoming):
            unique.setdefault(candidate.unit.unit_id, candidate)
        return tuple(
            candidate.model_copy(
                update={
                    "fused_rank": rank,
                    "selected": rank <= limit or candidate.unit.mandatory,
                    "rejection_reason": (
                        None if rank <= limit or candidate.unit.mandatory else "route_result_limit"
                    ),
                }
            )
            for rank, candidate in enumerate(unique.values(), start=1)
        )

    @staticmethod
    def _fallback_applies(
        condition: str,
        candidates: tuple[FusedCandidate, ...],
    ) -> bool:
        has_candidates = any(candidate.selected for candidate in candidates)
        if condition in {"anchor_evidence_insufficient", "plan_anchor_insufficient"}:
            return not has_candidates
        if condition == "hierarchy_scope_resolved":
            return has_candidates
        raise ValueError(f"unregistered deterministic fallback condition: {condition}")

    @staticmethod
    def _empty_trace(need: Stage1MemoryNeed) -> RetrievalTrace:
        return RetrievalTrace(
            need_id=need.need_id,
            intent=need.query_intent,
            allowed_channels=(),
            channel_candidate_counts={},
            candidates=(),
            fusion_applied=False,
            stop_reason=RetrievalStopReason.CANDIDATES_EXHAUSTED,
        )

    @staticmethod
    def _selected_ids(context: Stage1ContextPackage) -> tuple[StableId, ...]:
        ids: list[StableId] = []
        for trace in context.retrieval_traces:
            ids.extend(
                candidate.unit.unit_id for candidate in trace.candidates if candidate.selected
            )
        return tuple(dict.fromkeys(ids))

    @staticmethod
    def _leakage_count(
        context: Stage1ContextPackage,
        evaluator_only_artifacts: tuple[ArtifactId, ...],
    ) -> int:
        private = set(evaluator_only_artifacts)
        units: dict[StableId, RetrievalUnit] = {}
        for trace in context.retrieval_traces:
            for candidate in trace.candidates:
                if candidate.selected:
                    units[candidate.unit.unit_id] = candidate.unit
        return sum(unit.source_artifact in private for unit in units.values())
