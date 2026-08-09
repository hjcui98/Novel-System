"""Fair deterministic-vs-bounded-controller execution on one immutable retrieval basis."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, TypedDict

from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory import (
    ChannelHit,
    FusedCandidate,
    NeedExecutionStatus,
    NeedRisk,
    RequirementLevel,
    RetrievalChannel,
    RetrievalStopReason,
    RetrievalTrace,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1ContextPackage,
    Stage1MemoryNeed,
    Stage1QueryIntent,
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

DETERMINISTIC_ROUTE_SCHEDULER_VERSION = "deterministic_task_weighted.v6"
_HISTORICAL_NEED_TYPES = frozenset(
    {"causal_history", "entity_history", "long_range_callback", "plan_conditioned_history"}
)
_HISTORICAL_QUERY_EXPANSION = (
    "历史 经过 前因 起因 后果 结果 影响 变化 早期 首次 来源 "
    "物件 持有 携带 保护 作用 决定 选择 未解决"
)


class _TraceDiagnostics(TypedDict):
    need_execution_status: NeedExecutionStatus
    calls_allocated: int
    required_need_facet_ids: tuple[StableId, ...]
    irreducible_need_facet_ids: tuple[StableId, ...]


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
        try:
            agentic = self.run_agentic(
                request,
                text_root,
                thread_id=thread_id,
                evaluator_only_artifacts=evaluator_only_artifacts,
            )
        except TimeoutError:
            # The paired B arm is experimental and must not abort an accepted
            # deterministic checkpoint.  Reuse A as the conservative B result
            # so Arm C cannot acquire unverified evidence, and make the failed
            # comparison explicit for evaluation.
            agentic = deterministic.model_copy(
                update={
                    "arm": ControllerArm.BOUNDED_R2,
                    "retrieval_call_count": request.retrieval_budget.max_tool_calls,
                    "stop_reason": ControllerStopReason.TOOL_FAILURE,
                    "quality_eligible": False,
                    "failure_category": "AGENTIC_TIMEOUT",
                }
            )
            comparison = self.compare(request, deterministic, agentic)
            return comparison.model_copy(
                update={
                    "comparable": False,
                    "blockers": ("agentic_controller_timeout",),
                }
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
            calls_allocated_by_need={
                trace.need_id.root: trace.calls_allocated
                for trace in agentic_context.retrieval_traces
            },
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
        if not deterministic.quality_eligible:
            blockers.append("deterministic_arm_quality_ineligible")
        if not agentic.quality_eligible:
            blockers.append("agentic_controller_timeout")
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
        fresh = self._freshness_check(request)
        traces = (
            self._retrieve_fair_registered_routes(
                budgeted,
                request.initial_memory_needs,
                allow_future_plan=request.allow_future_plan,
                per_channel_limit=request.retrieval_budget.max_candidates,
            )
            if fresh and self._route_plans
            else self._retrieve_legacy_routes(
                budgeted,
                request.initial_memory_needs,
                fresh=fresh,
                allow_future_plan=request.allow_future_plan,
                per_channel_limit=request.retrieval_budget.max_candidates,
            )
        )
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
            calls_allocated_by_need={
                trace.need_id.root: trace.calls_allocated for _, trace in traces
            },
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

    def _retrieve_legacy_routes(
        self,
        backend: _BudgetedBackend,
        needs: tuple[Stage1MemoryNeed, ...],
        *,
        fresh: bool,
        allow_future_plan: bool,
        per_channel_limit: int,
    ) -> list[tuple[Stage1MemoryNeed, RetrievalTrace]]:
        traces: list[tuple[Stage1MemoryNeed, RetrievalTrace]] = []
        for need in needs:
            if not fresh:
                trace = self._empty_trace(
                    need,
                    status=NeedExecutionStatus.NOT_EXECUTED_FRESHNESS_BLOCKED,
                )
            elif need.query_intent in PLAN_INTENTS and not allow_future_plan:
                trace = self._empty_trace(
                    need,
                    status=NeedExecutionStatus.NOT_EXECUTED_SCOPE_BLOCKED,
                )
            elif backend.call_count >= backend._max_calls:
                backend.exhausted = True
                trace = self._empty_trace(
                    need,
                    status=NeedExecutionStatus.NOT_EXECUTED_BUDGET_EXHAUSTED,
                )
            else:
                before = backend.call_count
                trace = RetrievalOrchestrator(
                    backend,
                    FusionService(),
                    per_channel_limit=per_channel_limit,
                    fused_limit=per_channel_limit,
                ).retrieve(need)
                allocated = backend.call_count - before
                trace = trace.model_copy(
                    update=self._trace_diagnostics(
                        need,
                        allocated=allocated,
                        has_candidates=bool(trace.candidates),
                    )
                )
            traces.append((need, trace))
        return traces

    def _retrieve_fair_registered_routes(
        self,
        backend: _BudgetedBackend,
        needs: tuple[Stage1MemoryNeed, ...],
        *,
        allow_future_plan: bool,
        per_channel_limit: int,
    ) -> list[tuple[Stage1MemoryNeed, RetrievalTrace]]:
        """Execute registered route calls with deterministic max-min fairness."""

        plans: dict[StableId, RoutePlan] = {}
        results: dict[StableId, dict[RetrievalChannel, tuple[ChannelHit, ...]]] = {}
        call_counts = {need.need_id: 0 for need in needs}
        blocked: dict[StableId, NeedExecutionStatus] = {}
        index_by_need = {need.need_id: index for index, need in enumerate(needs)}
        need_by_id = {need.need_id: need for need in needs}
        for need in needs:
            if need.query_intent in PLAN_INTENTS and not allow_future_plan:
                blocked[need.need_id] = NeedExecutionStatus.NOT_EXECUTED_SCOPE_BLOCKED
                continue
            plan = self._route_plans.get(need.need_id)
            if plan is None:
                raise ValueError("registered-route run has no RoutePlan for an actual Need")
            if plan.base_commit != need.base_commit:
                raise ValueError("registered RoutePlan basis differs from its Need")
            if not plan.effective_channels:
                blocked[need.need_id] = NeedExecutionStatus.NOT_EXECUTED_NO_EXECUTABLE_QUERY
                continue
            plans[need.need_id] = plan
            results[need.need_id] = {}

        def next_channel(
            need: Stage1MemoryNeed,
        ) -> tuple[RetrievalChannel | None, bool, bool]:
            plan = plans[need.need_id]
            need_results = results[need.need_id]
            primary = tuple(
                dict.fromkeys(
                    step.channel
                    for step in (
                        *plan.mandatory_steps,
                        *(step for group in plan.primary_groups for step in group.steps),
                    )
                )
            )
            for channel_index, channel in enumerate(primary):
                if channel not in need_results:
                    if channel_index > 0:
                        early_fallback = next_registered_fallback(need, plan, need_results)
                        if early_fallback is not None:
                            return early_fallback[0], True, early_fallback[1]
                    return channel, False, False
            partial = self._assemble_route_trace(
                need,
                plan,
                need_results,
                per_channel_limit=per_channel_limit,
                reranker=None,
            )
            for fallback in plan.conditional_fallbacks:
                remaining = tuple(
                    step.channel for step in fallback.steps if step.channel not in need_results
                )
                if not remaining or not self._fallback_applies(
                    fallback.condition, partial.candidates, need=need
                ):
                    continue
                return remaining[0], True, bool(len(remaining) < len(fallback.steps))
            return None, False, False

        def next_registered_fallback(
            need: Stage1MemoryNeed,
            plan: RoutePlan,
            need_results: dict[RetrievalChannel, tuple[ChannelHit, ...]],
        ) -> tuple[RetrievalChannel, bool] | None:
            """Spend the next slot on a declared rescue route before a second primary call."""

            partial = self._assemble_route_trace(
                need,
                plan,
                need_results,
                per_channel_limit=per_channel_limit,
                reranker=None,
            )
            for fallback in plan.conditional_fallbacks:
                remaining = tuple(
                    step.channel for step in fallback.steps if step.channel not in need_results
                )
                if not remaining:
                    continue
                if not self._fallback_applies(
                    fallback.condition,
                    partial.candidates,
                    need=need,
                ):
                    continue
                return remaining[0], len(remaining) < len(fallback.steps)
            return None

        while backend.call_count < backend._max_calls:
            initial_round_complete = all(call_counts[need_id] >= 1 for need_id in plans)
            pending = tuple(
                (need, channel, is_fallback, is_fallback_continuation)
                for need_id, need in need_by_id.items()
                if need_id in plans
                for channel, is_fallback, is_fallback_continuation in (next_channel(need),)
                if channel is not None
            )
            if not pending:
                break
            risk_order = {NeedRisk.HIGH: 0, NeedRisk.MEDIUM: 1, NeedRisk.LOW: 2}
            need, channel, is_fallback, _ = min(
                pending,
                key=lambda item: (
                    0 if initial_round_complete and item[3] else 1,
                    # Before task weighting starts, every executable Need gets one
                    # retrieval call.  Afterwards, spend the finite call budget on
                    # the most task-relevant evidence group instead of mechanically
                    # giving every low-priority Need a second call first.
                    call_counts[item[0].need_id] if not initial_round_complete else 0,
                    0 if item[0].requirement is RequirementLevel.MANDATORY else 1,
                    risk_order[item[0].risk_level],
                    -len(
                        item[0].completion_spec.required_need_facet_ids
                        if item[0].completion_spec is not None
                        else ()
                    ),
                    -item[0].priority,
                    call_counts[item[0].need_id],
                    0 if item[2] else 1,
                    index_by_need[item[0].need_id],
                ),
            )
            query_need = (
                PairedMemoryControllerRunner._historical_fallback_need(need)
                if is_fallback
                else need
            )
            results[need.need_id][channel] = backend.search(
                query_need,
                channel,
                per_channel_limit,
            )
            call_counts[need.need_id] += 1

        still_pending = any(
            next_channel(need)[0] is not None
            for need_id, need in need_by_id.items()
            if need_id in plans
        )
        backend.exhausted = backend.call_count >= backend._max_calls and still_pending
        traces: list[tuple[Stage1MemoryNeed, RetrievalTrace]] = []
        for need in needs:
            if need.need_id in blocked:
                trace = self._empty_trace(
                    need,
                    status=blocked[need.need_id],
                    plan=self._route_plans.get(need.need_id),
                )
            elif not call_counts[need.need_id] and backend.exhausted:
                trace = self._empty_trace(
                    need,
                    status=NeedExecutionStatus.NOT_EXECUTED_BUDGET_EXHAUSTED,
                )
            else:
                trace = self._assemble_route_trace(
                    need,
                    plans[need.need_id],
                    results[need.need_id],
                    per_channel_limit=per_channel_limit,
                    reranker=self._reranker,
                )
            traces.append((need, trace))
        return traces

    @staticmethod
    def _historical_fallback_need(need: Stage1MemoryNeed) -> Stage1MemoryNeed:
        """Expand only the registered grounded rescue query for historical Needs.

        The primary Anchor query remains unchanged. The fallback receives public
        Need text plus its public hints/reason and a small history vocabulary so
        a composite causal alternative can be retrieved without adding an
        evaluator-only query or a new semantic subsystem.
        """

        if (
            need.need_type not in _HISTORICAL_NEED_TYPES
            and need.query_intent is not Stage1QueryIntent.SEMANTIC_HISTORY
        ):
            return need
        # Real task-conditioned queries can already exceed the backend's useful
        # query budget because they include a large entity-anchor dump.  Reserve
        # space for the public hints and history vocabulary instead of appending
        # them after an unbounded primary query and then truncating them away.
        query_prefix = need.query_text.strip()[:700]
        hint_budget = 500
        bounded_hints: list[str] = []
        for hint in need.query_hints:
            if hint_budget <= 0:
                break
            bounded = hint.strip()[: min(250, hint_budget)]
            if bounded:
                bounded_hints.append(bounded)
                hint_budget -= len(bounded)
        need_type = str(need.need_type).replace("_", " ")[:120]
        need_id_suffix = need.need_id.root.rsplit(".", 1)[-1].replace("-", " ")[:160]
        parts = tuple(
            dict.fromkeys(
                item.strip()
                for item in (
                    query_prefix,
                    *bounded_hints,
                    need.why_needed[:280],
                    (need.purpose or "")[:280],
                    need_type,
                    need_id_suffix,
                    _HISTORICAL_QUERY_EXPANSION,
                )
                if item.strip()
            )
        )
        expanded = " ".join(parts)
        return need.model_copy(update={"query_text": expanded[:2400]})

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

        def execute(
            channels: tuple[RetrievalChannel, ...],
            *,
            use_rrf: bool,
            reserve_incoming: bool = False,
            expand_historical_query: bool = False,
        ) -> None:
            nonlocal candidates
            group_results: dict[RetrievalChannel, tuple[ChannelHit, ...]] = {}
            for channel in channels:
                if channel in results:
                    continue
                query_need = (
                    PairedMemoryControllerRunner._historical_fallback_need(need)
                    if expand_historical_query
                    else need
                )
                hits = backend.search(query_need, channel, per_channel_limit)
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
                candidates,
                group_candidates,
                limit=per_channel_limit,
                reserve_incoming=reserve_incoming,
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
            if not PairedMemoryControllerRunner._fallback_applies(
                fallback.condition,
                candidates,
                need=need,
            ):
                continue
            fallback_used = True
            fallback_reason = fallback.condition
            steps = tuple(step.channel for step in fallback.steps)
            execute(
                steps,
                use_rrf=fallback.fusion_profile is not None,
                reserve_incoming=True,
                expand_historical_query=True,
            )

        fusion_applied = any(group.fusion_profile is not None for group in plan.primary_groups) or (
            fallback_used
            and any(item.fusion_profile is not None for item in plan.conditional_fallbacks)
        )
        rerank_applied = False
        rerank_failure: str | None = None
        if fusion_applied and reranker is not None:
            try:
                ranking_need = (
                    PairedMemoryControllerRunner._historical_fallback_need(need)
                    if fallback_used
                    else need
                )
                candidates = reranker.rerank(ranking_need, candidates, limit=per_channel_limit)
                rerank_applied = True
            except Exception as error:
                rerank_failure = f"reranker_degraded:{type(error).__name__}"
        candidates = PairedMemoryControllerRunner._protect_historical_evidence(
            need,
            candidates,
            limit=per_channel_limit,
        )
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
            compiled_query_bundle=plan.compiled_query_bundle.model_dump(mode="json"),
            effective_channels=plan.effective_channels,
            query_unavailable_reasons=plan.query_unavailable_reasons,
            stop_reason=(
                RetrievalStopReason.EXACT_SATISFIED
                if selected and not fallback_used
                else RetrievalStopReason.BUDGET_SATISFIED
                if selected
                else RetrievalStopReason.FALLBACK_EXHAUSTED
                if fallback_used
                else RetrievalStopReason.CANDIDATES_EXHAUSTED
            ),
            **PairedMemoryControllerRunner._trace_diagnostics(
                need,
                allocated=len(called),
                has_candidates=bool(candidates),
            ),
        )

    @staticmethod
    def _assemble_route_trace(
        need: Stage1MemoryNeed,
        plan: RoutePlan,
        results: dict[RetrievalChannel, tuple[ChannelHit, ...]],
        *,
        per_channel_limit: int,
        reranker: RerankService | None,
    ) -> RetrievalTrace:
        """Build one trace from calls already allocated by the fair scheduler."""

        candidates: tuple[FusedCandidate, ...] = ()
        fusion_applied = False
        consumed: set[RetrievalChannel] = set()

        def merge_stage(
            stage_results: dict[RetrievalChannel, tuple[ChannelHit, ...]],
            *,
            use_rrf: bool,
            reserve_incoming: bool = False,
        ) -> None:
            nonlocal candidates, fusion_applied
            if not stage_results:
                return
            incoming = (
                FusionService().fuse(stage_results, limit=per_channel_limit)
                if use_rrf and len(stage_results) > 1
                else PairedMemoryControllerRunner._direct_candidates(
                    stage_results,
                    limit=per_channel_limit,
                )
            )
            fusion_applied = fusion_applied or (use_rrf and len(stage_results) > 1)
            candidates = PairedMemoryControllerRunner._merge_candidates(
                candidates,
                incoming,
                limit=per_channel_limit,
                reserve_incoming=reserve_incoming,
            )
            consumed.update(stage_results)

        merge_stage(
            {
                step.channel: results[step.channel]
                for step in plan.mandatory_steps
                if step.channel in results
            },
            use_rrf=False,
        )
        for group in plan.primary_groups:
            merge_stage(
                {
                    step.channel: results[step.channel]
                    for step in group.steps
                    if step.channel in results
                },
                use_rrf=group.fusion_profile is not None,
            )
        fallback_used = False
        fallback_reason: str | None = None
        for fallback in plan.conditional_fallbacks:
            fallback_results = {
                step.channel: results[step.channel]
                for step in fallback.steps
                if step.channel in results
            }
            if fallback_results:
                fallback_used = True
                fallback_reason = fallback.condition
                merge_stage(
                    fallback_results,
                    use_rrf=fallback.fusion_profile is not None,
                    reserve_incoming=True,
                )
        remaining = {channel: hits for channel, hits in results.items() if channel not in consumed}
        merge_stage(remaining, use_rrf=False)

        rerank_applied = False
        rerank_failure: str | None = None
        if fusion_applied and reranker is not None:
            try:
                ranking_need = (
                    PairedMemoryControllerRunner._historical_fallback_need(need)
                    if fallback_used
                    else need
                )
                candidates = reranker.rerank(ranking_need, candidates, limit=per_channel_limit)
                rerank_applied = True
            except Exception as error:
                rerank_failure = f"reranker_degraded:{type(error).__name__}"
        candidates = PairedMemoryControllerRunner._protect_historical_evidence(
            need,
            candidates,
            limit=per_channel_limit,
        )
        selected = tuple(candidate for candidate in candidates if candidate.selected)
        return RetrievalTrace(
            need_id=need.need_id,
            intent=need.query_intent,
            allowed_channels=tuple(results),
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
            compiled_query_bundle=plan.compiled_query_bundle.model_dump(mode="json"),
            effective_channels=plan.effective_channels,
            query_unavailable_reasons=plan.query_unavailable_reasons,
            stop_reason=(
                RetrievalStopReason.EXACT_SATISFIED
                if selected and not fallback_used
                else RetrievalStopReason.BUDGET_SATISFIED
                if selected
                else RetrievalStopReason.FALLBACK_EXHAUSTED
                if fallback_used
                else RetrievalStopReason.CANDIDATES_EXHAUSTED
            ),
            **PairedMemoryControllerRunner._trace_diagnostics(
                need,
                allocated=len(results),
                has_candidates=bool(candidates),
            ),
        )

    @staticmethod
    def _trace_diagnostics(
        need: Stage1MemoryNeed,
        *,
        allocated: int,
        has_candidates: bool,
    ) -> _TraceDiagnostics:
        spec = need.completion_spec
        return {
            "need_execution_status": (
                NeedExecutionStatus.EXECUTED_WITH_CANDIDATES
                if has_candidates
                else NeedExecutionStatus.EXECUTED_EMPTY
            ),
            "calls_allocated": allocated,
            "required_need_facet_ids": (spec.required_need_facet_ids if spec is not None else ()),
            "irreducible_need_facet_ids": (
                spec.irreducible_need_facet_ids if spec is not None else ()
            ),
        }

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
        reserve_incoming: bool = False,
    ) -> tuple[FusedCandidate, ...]:
        """Keep route-stage order without making a second cross-channel fusion pass."""

        ordered = (*existing, *incoming)
        if reserve_incoming and existing and incoming:
            # A full Anchor page must not make a legal fallback a no-op.  Keep
            # a short alternating prefix for stable route ordering, then give
            # evidence-bearing fallback candidates a larger bounded share. A
            # grounded hit at channel rank 11 must remain selectable in a
            # twenty-candidate page; plain interleaving would move it to rank
            # 22 and silently discard the very evidence the fallback fetched.
            incoming_has_evidence = any(
                candidate.unit.unit_kind
                in {RetrievalUnitKind.GROUNDED_BLOCK, RetrievalUnitKind.GROUNDED_SPAN}
                and candidate.unit.evidence_refs
                for candidate in incoming
            )
            if incoming_has_evidence:
                incoming_quota = min(len(incoming), max(1, (limit * 2 + 2) // 3))
                existing_quota = min(len(existing), max(0, limit - incoming_quota))
                prefix = min(2, incoming_quota, existing_quota)
                prioritized: list[FusedCandidate] = []
                for index in range(prefix):
                    prioritized.extend((existing[index], incoming[index]))
                prioritized.extend(incoming[prefix:incoming_quota])
                prioritized.extend(existing[prefix:existing_quota])
                prioritized.extend(incoming[incoming_quota:])
                prioritized.extend(existing[existing_quota:])
                ordered = tuple(prioritized)
            else:
                interleaved: list[FusedCandidate] = []
                for index in range(max(len(existing), len(incoming))):
                    if index < len(existing):
                        interleaved.append(existing[index])
                    if index < len(incoming):
                        interleaved.append(incoming[index])
                ordered = tuple(interleaved)
        unique: dict[StableId, FusedCandidate] = {}
        for candidate in ordered:
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
    def _protect_historical_evidence(
        need: Stage1MemoryNeed,
        candidates: tuple[FusedCandidate, ...],
        *,
        limit: int,
    ) -> tuple[FusedCandidate, ...]:
        """Reserve a small bounded share for retrieved historical evidence.

        RRF and reranking are allowed to prefer structured Anchor records, but
        a historical Need must not lose every grounded passage merely because
        those passages fall just below the shared top-k window.  The reserve
        is limited to one fifth of the candidate window (at most four at the
        formal limit of twenty) and only replaces non-mandatory non-grounded
        candidates.  It never invents a candidate or broadens a retrieval
        route.
        """

        if need.query_intent.value not in {"semantic_history", "related_event"}:
            return candidates
        if limit < 1:
            raise ValueError("historical evidence protection limit must be positive")
        evidence_candidates = tuple(
            candidate
            for candidate in candidates
            if candidate.unit.unit_kind
            in {RetrievalUnitKind.GROUNDED_BLOCK, RetrievalUnitKind.GROUNDED_SPAN}
            and candidate.unit.evidence_refs
        )
        if not evidence_candidates:
            return candidates

        reserve = min(4, max(1, limit // 5))
        selected_evidence = sum(candidate.selected for candidate in evidence_candidates)
        if selected_evidence >= reserve:
            return candidates

        by_unit_id = {candidate.unit.unit_id: candidate for candidate in candidates}
        selected_count = sum(candidate.selected for candidate in candidates)
        available_slots = max(0, limit - selected_count)
        replacements = iter(
            sorted(
                (
                    candidate
                    for candidate in candidates
                    if candidate.selected
                    and not candidate.unit.mandatory
                    and candidate.unit.unit_kind
                    not in {RetrievalUnitKind.GROUNDED_BLOCK, RetrievalUnitKind.GROUNDED_SPAN}
                ),
                key=lambda candidate: (-candidate.fused_rank, candidate.unit.unit_id.root),
            )
        )
        for candidate in evidence_candidates:
            if selected_evidence >= reserve:
                break
            if candidate.selected:
                continue
            if available_slots > 0:
                by_unit_id[candidate.unit.unit_id] = candidate.model_copy(
                    update={"selected": True, "rejection_reason": None}
                )
                selected_evidence += 1
                available_slots -= 1
                continue
            try:
                replacement = next(replacements)
            except StopIteration:
                break
            by_unit_id[candidate.unit.unit_id] = candidate.model_copy(
                update={"selected": True, "rejection_reason": None}
            )
            by_unit_id[replacement.unit.unit_id] = replacement.model_copy(
                update={
                    "selected": False,
                    "rejection_reason": "historical_evidence_reserve",
                }
            )
            selected_evidence += 1
        return tuple(by_unit_id[candidate.unit.unit_id] for candidate in candidates)

    @staticmethod
    def _fallback_applies(
        condition: str,
        candidates: tuple[FusedCandidate, ...],
        *,
        need: Stage1MemoryNeed | None = None,
    ) -> bool:
        has_candidates = any(candidate.selected for candidate in candidates)
        if condition == "anchor_evidence_insufficient":
            if not has_candidates:
                return True
            if need is None:
                return False
            if need.need_type in {
                "causal_history",
                "entity_history",
                "knowledge_boundary",
                "long_range_callback",
                "plan_conditioned_history",
            }:
                return not any(
                    candidate.selected
                    and candidate.unit.unit_kind
                    in {
                        RetrievalUnitKind.GROUNDED_BLOCK,
                        RetrievalUnitKind.GROUNDED_SPAN,
                    }
                    and candidate.unit.evidence_refs
                    for candidate in candidates
                )
            evidenced_text = " ".join(
                candidate.unit.text.casefold()
                for candidate in candidates
                if candidate.selected and candidate.unit.evidence_refs
            )
            terms = PairedMemoryControllerRunner._semantic_query_terms(need.query_text)
            if not terms:
                return False
            matched = sum(term in evidenced_text for term in terms)
            # Broad history/callback Needs require evidence expansion unless
            # Anchor conclusions cover at least half of their semantic hints.
            return matched / len(terms) < 0.5
        if condition == "exact_current_record_absent":
            return not has_candidates
        if condition == "plan_anchor_insufficient":
            return not has_candidates
        if condition == "hierarchy_scope_resolved":
            return has_candidates
        raise ValueError(f"unregistered deterministic fallback condition: {condition}")

    @staticmethod
    def _semantic_query_terms(value: str) -> tuple[str, ...]:
        spaced = tuple(
            token
            for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", value.casefold())
            if len(token) >= 2
        )
        terms: list[str] = []
        for token in spaced:
            if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 2:
                terms.extend(token[index : index + 2] for index in range(len(token) - 1))
            else:
                terms.append(token)
        return tuple(dict.fromkeys(terms))

    @staticmethod
    def _empty_trace(
        need: Stage1MemoryNeed,
        *,
        status: NeedExecutionStatus = NeedExecutionStatus.EXECUTED_EMPTY,
        plan: RoutePlan | None = None,
    ) -> RetrievalTrace:
        spec = need.completion_spec
        return RetrievalTrace(
            need_id=need.need_id,
            intent=need.query_intent,
            allowed_channels=(),
            channel_candidate_counts={},
            candidates=(),
            fusion_applied=False,
            stop_reason=(
                RetrievalStopReason.NO_EXECUTABLE_QUERY
                if status is NeedExecutionStatus.NOT_EXECUTED_NO_EXECUTABLE_QUERY
                else RetrievalStopReason.CANDIDATES_EXHAUSTED
            ),
            need_execution_status=status,
            calls_allocated=0,
            required_need_facet_ids=(spec.required_need_facet_ids if spec is not None else ()),
            irreducible_need_facet_ids=(
                spec.irreducible_need_facet_ids if spec is not None else ()
            ),
            compiled_query_bundle=(
                plan.compiled_query_bundle.model_dump(mode="json") if plan is not None else {}
            ),
            effective_channels=(plan.effective_channels if plan is not None else ()),
            query_unavailable_reasons=(plan.query_unavailable_reasons if plan is not None else {}),
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
