"""Evidence-first Writer package pipeline over one frozen checkpoint.

Reuses the frozen Commit/World/TextRoot/Plan and the frozen real-hybrid
snapshot indexes, re-runs public Need -> Retrieval/Rank -> Exact L0 Slice
Selection, and stops at WriterContextPackage v2 + EvidenceLedger v2.  The
product path may use the existing Planner and bounded Controller model owners;
Claim Support, whole verifier and semantic evaluation remain outside it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from novel_agent.agents import seal_tool_policy
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import AuthorPlanningContext, PlanRootDocument, TextRootDocument
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    GraphPathDereferenceStatus,
    GraphPathReceipt,
    RetrievalChannel,
    RetrievalTrace,
    Stage1MemoryNeed,
    WorldRootDocument,
)
from novel_agent.domain.planning_memory import (
    GroundingStatus,
    PlannerFallbackStatus,
    PlannerInvocationArtifact,
)
from novel_agent.domain.retrieval_routing import (
    RoutePlan,
    SnapshotCapabilityStatus,
)
from novel_agent.domain.stage2 import (
    AccessScope,
    ContextBudget,
    MemoryResolutionRequest,
    RequiredSnapshotPolicy,
    RetrievalBudget,
    ToolPolicy,
)
from novel_agent.domain.writer_context import (
    BenchmarkTaskContract,
    UnresolvedLexicalAnchor,
)
from novel_agent.runtime.memory_controller import RouteBoundControllerPolicy
from novel_agent.services.content_addressing import content_id
from novel_agent.services.evidence_first_writer_context_assembler import (
    EvidenceFirstAssemblyResult,
    EvidenceFirstWriterContextAssembler,
    NeedEvidenceSelection,
    SliceSelectionTrace,
)
from novel_agent.services.evidence_slice_resolver import EvidenceSliceResolver
from novel_agent.services.facet_support import FacetSupportEvaluator
from novel_agent.services.memory_pipeline import ContextCompiler, EvidenceExpander
from novel_agent.services.model_gateway import ModelGateway
from novel_agent.services.need_draft_grounder import NeedDraftGrounder
from novel_agent.services.need_query_compiler import NeedQueryCompiler
from novel_agent.services.need_validator import NeedValidator
from novel_agent.services.paired_controller import PairedMemoryControllerRunner
from novel_agent.services.retrieval_routing import ROUTE_POLICY_VERSION, DeterministicChannelPlanner
from novel_agent.services.stage2_retrieval_backend import Stage2RetrievalBackendBundle
from novel_agent.services.task_conditioned_need_generation import (
    NeedGenerationResult,
    TaskPlanConditionedNeedGenerator,
)


class EvidenceFirstCheckpointResult:
    """Full record of one evidence-first checkpoint pipeline execution."""

    def __init__(
        self,
        *,
        needs: tuple[Stage1MemoryNeed, ...],
        route_plans: tuple[RoutePlan, ...],
        retrieval_call_count: int,
        future_leakage_count: int,
        stop_reason: str,
        selections: tuple[NeedEvidenceSelection, ...],
        assembly: EvidenceFirstAssemblyResult,
        planner_fallback_used: bool,
        unresolved_lexical_anchors: tuple[UnresolvedLexicalAnchor, ...],
        need_generation: NeedGenerationResult | None,
        trace_records: tuple[dict[str, Any], ...],
        need_planner_model_call_count: int,
        controller_model_call_count: int,
        controller_repair_count: int,
        controller_decisions: tuple[dict[str, Any], ...] = (),
        controller_repairs: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.needs = needs
        self.route_plans = route_plans
        self.retrieval_call_count = retrieval_call_count
        self.future_leakage_count = future_leakage_count
        self.stop_reason = stop_reason
        self.selections = selections
        self.assembly = assembly
        self.planner_fallback_used = planner_fallback_used
        self.unresolved_lexical_anchors = unresolved_lexical_anchors
        self.need_generation = need_generation
        self.trace_records = trace_records
        self.need_planner_model_call_count = need_planner_model_call_count
        self.controller_model_call_count = controller_model_call_count
        self.controller_repair_count = controller_repair_count
        self.controller_decisions = controller_decisions
        self.controller_repairs = controller_repairs


class EvidenceFirstCheckpointRunner:
    """Run one frozen checkpoint through the evidence-first package pipeline."""

    version = "evidence_first_checkpoint_runner.v2"

    def __init__(
        self,
        *,
        writer_token_budget: int = 4000,
        evidence_ledger_token_budget: int = 12_000,
        max_candidates: int = 20,
        max_tool_calls: int = 48,
        artifact_writer: Callable[[bytes, str], ArtifactRef] | None = None,
        graph_receipt_validator: (
            Callable[
                [tuple[GraphPathReceipt, ...], TextRootDocument],
                tuple[GraphPathReceipt, ...],
            ]
            | None
        ) = None,
        planner_gateway: ModelGateway | None = None,
        controller_policy_factory: (
            Callable[[ToolPolicy, tuple[RoutePlan, ...]], Any] | None
        ) = None,
        require_model_decisions: bool = False,
        planner_max_output_tokens: int = 8192,
    ) -> None:
        if writer_token_budget < 1 or evidence_ledger_token_budget < 1:
            raise ValueError("writer and ledger budgets must be positive")
        if max_candidates < 1 or max_candidates > 100:
            raise ValueError("candidate limit must be between 1 and 100")
        if max_tool_calls < 1:
            raise ValueError("tool-call limit must be positive")
        self._writer_token_budget = writer_token_budget
        self._evidence_ledger_token_budget = evidence_ledger_token_budget
        self._max_candidates = max_candidates
        self._max_tool_calls = max_tool_calls
        self._generator = TaskPlanConditionedNeedGenerator(
            planner_gateway=planner_gateway,
            planner_artifact_writer=artifact_writer,
            planner_max_output_tokens=planner_max_output_tokens,
        )
        if require_model_decisions and planner_gateway is None:
            raise ValueError("model-driven evidence-first requires a Planner gateway")
        if require_model_decisions and controller_policy_factory is None:
            raise ValueError("model-driven evidence-first requires a Controller policy factory")
        self._controller_policy_factory = controller_policy_factory
        self._require_model_decisions = require_model_decisions
        self._assembler = EvidenceFirstWriterContextAssembler()
        self._resolver = EvidenceSliceResolver()
        self._graph_receipt_validator = graph_receipt_validator

    @property
    def assembler(self) -> EvidenceFirstWriterContextAssembler:
        return self._assembler

    def run(
        self,
        *,
        case_id: ProjectId,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        text: TextRootDocument,
        plan: PlanRootDocument,
        base_commit: CommitId,
        snapshot_id: StableId,
        planning_context: AuthorPlanningContext,
        frozen_planner_artifact: PlannerInvocationArtifact,
        frozen_needs: tuple[Stage1MemoryNeed, ...],
        backend_bundle: Stage2RetrievalBackendBundle,
        fingerprint: ArtifactId,
        run_id: StableId,
    ) -> EvidenceFirstCheckpointResult:
        """Execute Need -> Retrieval/Rank -> Selection -> Package/Ledger."""
        if backend_bundle.attestation.source_commit != base_commit:
            raise ValueError("backend attestation basis differs from the checkpoint commit")
        capability = backend_bundle.attestation.capability
        if (
            capability.status is not SnapshotCapabilityStatus.EXACT
            or capability.snapshot_id != snapshot_id
        ):
            raise ValueError("checkpoint snapshot capability must be exact for the basis")
        need_generation: NeedGenerationResult | None = None
        if self._require_model_decisions:
            need_generation = self._generator.generate_with_lineage(
                task,
                world,
                plan,
                planning_context=planning_context,
            )
            planner_fallback_used = need_generation.fallback_used
            if planner_fallback_used:
                reason = need_generation.planner_fallback_reason or "unknown"
                artifact_ref = need_generation.planner_artifact_document_ref
                artifact = need_generation.planner_artifact
                missing = tuple(artifact.missing_goal_chapters) if artifact is not None else ()
                detail = f"missing_goal_chapters={list(missing)}" if missing else "no coverage gap"
                ref_text = (
                    artifact_ref.artifact_id.root if artifact_ref is not None else "not persisted"
                )
                raise RuntimeError(
                    "model-driven evidence-first Planner fell back: "
                    f"{reason}; {detail}; planner_artifact_ref={ref_text}"
                )
            needs = need_generation.needs
        else:
            planner_fallback_used = frozen_planner_artifact.fallback_status is (
                PlannerFallbackStatus.PLANNER_FALLBACK
            )
            if planner_fallback_used:
                needs = frozen_needs
            else:
                need_generation = self._generator.generate_evidence_first(
                    task,
                    world,
                    plan,
                    planning_context,
                    frozen_planner_artifact,
                )
                if need_generation is None or not need_generation.needs:
                    needs = frozen_needs
                    planner_fallback_used = True
                else:
                    needs = need_generation.needs
        needs = self._scope_needs(needs)
        if not needs:
            raise ValueError(f"evidence-first checkpoint produced no memory needs: {case_id.root}")
        planner_model_call_count = (
            len(need_generation.planner_artifact.attempts)
            if need_generation is not None and need_generation.planner_artifact is not None
            else 0
        )
        if self._require_model_decisions and planner_model_call_count < 1:
            raise RuntimeError("model-driven evidence-first made no Planner model call")
        route_plans = tuple(DeterministicChannelPlanner().plan(need, capability) for need in needs)
        tool_policy = seal_tool_policy(
            ToolPolicy(
                policy_id=StableId(f"policy.evidence-first.v2.{case_id.root}"[:128]),
                version=SchemaVersion("1.0.0"),
                content_hash=fingerprint,
                allowed_tools=self._allowed_tools(route_plans),
                max_rounds=2,
                max_tool_calls=self._max_tool_calls,
                max_query_rewrites_per_need=0,
                wall_clock_budget_ms=120_000,
                token_budget=self._writer_token_budget,
            )
        )
        controller_policy = (
            self._controller_policy_factory(tool_policy, route_plans)
            if self._controller_policy_factory is not None
            else RouteBoundControllerPolicy(route_plans)
        )
        controller = PairedMemoryControllerRunner.from_shared_backend(
            backend=backend_bundle.backend,
            needs=needs,
            tool_policy=tool_policy,
            compiler=ContextCompiler(EvidenceExpander()),
            controller_policy=controller_policy,
            freshness_check=lambda _: True,
            checkpointer=InMemorySaver(),
            comparison_basis_fingerprint=fingerprint,
            route_plans=route_plans,
            reranker=backend_bundle.reranker,
        )
        first = needs[0]
        request = MemoryResolutionRequest(
            request_id=run_id,
            run_id=first.run_id,
            task_id=first.task_id,
            project_id=case_id,
            base_commit=base_commit,
            snapshot_id=snapshot_id,
            required_snapshot_policy=RequiredSnapshotPolicy.EXACT,
            task_contract=task.task_text,
            initial_memory_needs=needs,
            worldline="main",
            narrative_chapter=task.target_chapter_start,
            access_scope=AccessScope.WRITER_SAFE,
            allow_future_plan=False,
            retrieval_budget=RetrievalBudget(
                max_rounds=2,
                max_tool_calls=self._max_tool_calls,
                max_query_rewrites_per_need=0,
                max_candidates=self._max_candidates,
                wall_clock_budget_ms=120_000,
                token_budget=self._writer_token_budget,
            ),
            context_budget=ContextBudget(token_budget=self._writer_token_budget),
        )
        if self._require_model_decisions:
            resolved = controller.run_agentic(
                request,
                text,
                thread_id=f"evidence-first.{case_id.root}.{run_id.root}",
                evaluator_only_artifacts=(),
            )
        else:
            resolved = controller.run_deterministic(
                request,
                text,
                evaluator_only_artifacts=(),
            )
        controller_receipts = tuple(getattr(controller_policy, "decision_receipts", ()))
        controller_repairs = tuple(getattr(controller_policy, "decision_repairs", ()))
        controller_decisions = tuple(getattr(controller_policy, "decision_history", ()))
        if self._require_model_decisions and not controller_receipts:
            raise RuntimeError("model-driven evidence-first made no Controller model decision")
        selections, trace_records = self._selections(
            needs,
            resolved.context.retrieval_traces,
            text,
            snapshot_id,
            route_plans=route_plans,
            graph_edge_count=backend_bundle.attestation.graph_edge_count,
        )
        unresolved = self._unresolved_anchors(need_generation, planner_fallback_used)
        planner_ref: ArtifactRef | None
        planner_hash: ArtifactId | None
        if need_generation is not None:
            planner_ref = need_generation.planner_artifact_document_ref
            if planner_ref is not None:
                planner_hash = planner_ref.artifact_id
            else:
                refreshed = need_generation.planner_artifact
                planner_hash = (
                    None if refreshed is None else content_id(refreshed.model_dump(mode="json"))
                )
        else:
            planner_ref = None
            planner_hash = content_id(frozen_planner_artifact.model_dump(mode="json"))
        assembly = self._assembler.assemble(
            task=task,
            selections=selections,
            text_root=text,
            basis_commit_id=base_commit,
            basis_snapshot_id=snapshot_id,
            arm=("B" if self._require_model_decisions else "A"),
            writer_token_budget=self._writer_token_budget,
            evidence_ledger_token_budget=self._evidence_ledger_token_budget,
            grounder_version=NeedDraftGrounder.version,
            validator_version=NeedValidator.version,
            generator_version=TaskPlanConditionedNeedGenerator.version,
            query_compiler_version=NeedQueryCompiler.version,
            route_plan_version=ROUTE_POLICY_VERSION.root,
            planner_artifact_ref=planner_ref,
            planner_artifact_hash=planner_hash,
            planner_fallback_used=planner_fallback_used,
            unresolved_lexical_anchors=unresolved,
        )
        return EvidenceFirstCheckpointResult(
            needs=needs,
            route_plans=route_plans,
            retrieval_call_count=resolved.retrieval_call_count,
            future_leakage_count=resolved.future_leakage_count,
            stop_reason=resolved.stop_reason.value,
            selections=selections,
            assembly=assembly,
            planner_fallback_used=planner_fallback_used,
            unresolved_lexical_anchors=unresolved,
            need_generation=need_generation,
            trace_records=trace_records,
            need_planner_model_call_count=planner_model_call_count,
            controller_model_call_count=len(controller_receipts),
            controller_repair_count=len(controller_repairs),
            controller_decisions=tuple(
                decision.model_dump(mode="json") for decision in controller_decisions
            ),
            controller_repairs=tuple(
                {
                    "request_id": repair.request_id.root,
                    "reason": repair.reason,
                    "selected_need_id": (
                        repair.selected_need_id.root
                        if repair.selected_need_id is not None
                        else None
                    ),
                    "selected_tool_name": repair.selected_tool_name,
                }
                for repair in controller_repairs
            ),
        )

    def _selections(
        self,
        needs: tuple[Stage1MemoryNeed, ...],
        traces: tuple[RetrievalTrace, ...],
        text: TextRootDocument,
        snapshot_id: StableId,
        *,
        route_plans: tuple[RoutePlan, ...] = (),
        graph_edge_count: int | None = None,
    ) -> tuple[tuple[NeedEvidenceSelection, ...], tuple[dict[str, Any], ...]]:
        need_by_id = {need.need_id: need for need in needs}
        plan_by_need_id = {plan.need_id: plan for plan in route_plans}
        trace_records: list[dict[str, Any]] = []
        selections: list[NeedEvidenceSelection] = []
        for trace in traces:
            need = need_by_id.get(trace.need_id)
            if need is None:
                raise ValueError(f"retrieval trace has unknown need: {trace.need_id.root}")
            plan = plan_by_need_id.get(trace.need_id)
            selected = tuple(
                sorted(
                    (candidate for candidate in trace.candidates if candidate.selected),
                    key=lambda candidate: (candidate.fused_rank, candidate.unit.unit_id.root),
                )
            )
            receipts = FacetSupportEvaluator.evaluate(need, selected)
            slice_by_id: dict[StableId, Any] = {}
            slice_traces: list[SliceSelectionTrace] = []
            graph_receipts = tuple(
                {
                    receipt.path_id: receipt
                    for candidate in selected
                    for hit in candidate.channel_hits
                    for receipt in hit.graph_path_receipts
                }.values()
            )
            verified_graph_receipts: tuple[GraphPathReceipt, ...] = ()
            if graph_receipts:
                if self._graph_receipt_validator is None:
                    raise ValueError("graph path receipts require exact L0 validation")
                verified_graph_receipts = self._graph_receipt_validator(graph_receipts, text)
                if any(
                    receipt.dereference_status is not GraphPathDereferenceStatus.L0_VERIFIED
                    for receipt in verified_graph_receipts
                ):
                    raise ValueError("graph path receipt did not verify exact L0 evidence")
            for candidate in selected:
                for evidence in candidate.unit.evidence_refs:
                    block = self._block(text, evidence)
                    if block is None:
                        continue
                    try:
                        resolved = self._resolver.resolve_evidence(
                            evidence,
                            block,
                            source_commit=candidate.unit.source_commit,
                            snapshot_id=snapshot_id,
                            access_scope=need.access_scope,
                        )
                    except ValueError:
                        continue
                    for slice_ in resolved:
                        if slice_.slice_id in slice_by_id:
                            continue
                        slice_by_id[slice_.slice_id] = slice_
                        first_hit = candidate.channel_hits[0]
                        slice_traces.append(
                            SliceSelectionTrace(
                                slice_id=slice_.slice_id,
                                unit_id=candidate.unit.unit_id,
                                route_channel=first_hit.channel.value,
                                fused_rank=candidate.fused_rank,
                                rerank_score=None,
                                selection_reason=(
                                    f"channel_rank={first_hit.channel_rank};"
                                    f"rrf={candidate.rrf_score:.6f};hit_reason={first_hit.hit_reason}"
                                ),
                                evidence_ref=evidence,
                                supported_facet_ids=FacetSupportEvaluator.supporting_facet_ids(
                                    need, candidate.unit
                                ),
                            )
                        )
            selections.append(
                NeedEvidenceSelection(
                    need=need,
                    selections=tuple(slice_traces),
                    slices=tuple(slice_by_id.values()),
                    facet_receipts=receipts,
                )
            )
            trace_records.append(
                {
                    "need_id": need.need_id.root,
                    "query": need.query_text,
                    "semantic_question": need.semantic_question,
                    "entity_ids": [item.root for item in need.entity_ids],
                    "intent": need.query_intent.value,
                    "execution_status": trace.need_execution_status.value,
                    "eligible_channels": (
                        [channel.value for channel in plan.effective_channels]
                        if plan is not None
                        else [channel.value for channel in trace.effective_channels]
                    ),
                    "ineligible_channels": (
                        [
                            {"channel": item.channel.value, "reason": item.reason}
                            for item in plan.excluded_channels
                        ]
                        if plan is not None
                        else [
                            {"channel": channel.value, "reason": reason}
                            for channel, reason in trace.query_unavailable_reasons.items()
                        ]
                    ),
                    "graph_unavailable_reason": self._graph_unavailable_reason(plan, trace),
                    "graph_readiness_status": self._graph_readiness_status(
                        plan,
                        trace,
                        graph_edge_count=graph_edge_count,
                        verified_receipt_count=len(verified_graph_receipts),
                    ),
                    "graph_edge_count": graph_edge_count,
                    "verified_graph_path_receipt_ids": [
                        receipt.path_id.root for receipt in verified_graph_receipts
                    ],
                    "channel_candidate_counts": {
                        channel.value: count
                        for channel, count in trace.channel_candidate_counts.items()
                    },
                    "channel_failures": {
                        channel.value: reason for channel, reason in trace.channel_failures.items()
                    },
                    "fallback_used": trace.fallback_used,
                    "fallback_reason": trace.fallback_reason,
                    "stop_reason": trace.stop_reason.value,
                    "retrieval_pages": trace.retrieval_pages,
                    "facet_receipts": [
                        {
                            "need_facet_id": receipt.need_facet_id.root,
                            "facet_kind": receipt.facet_kind.value,
                            "mandatory": receipt.mandatory,
                            "status": receipt.status.value,
                            "supporting_unit_ids": [
                                unit_id.root for unit_id in receipt.supporting_unit_ids
                            ],
                            "stop_reason": receipt.stop_reason,
                        }
                        for receipt in receipts
                    ],
                    "closed_need_facet_ids": [
                        facet_id.root for facet_id in trace.closed_need_facet_ids
                    ],
                    "selected_candidates": [
                        {
                            "unit_id": candidate.unit.unit_id.root,
                            "fused_rank": candidate.fused_rank,
                            "channels": [hit.channel.value for hit in candidate.channel_hits],
                            "kind": candidate.unit.unit_kind.value,
                        }
                        for candidate in selected
                    ],
                }
            )
        return tuple(selections), tuple(trace_records)

    @staticmethod
    def _graph_unavailable_reason(plan: RoutePlan | None, trace: RetrievalTrace) -> str | None:
        """Typed graph unavailability: compiler reason wins, then zero-edge evidence."""
        if plan is not None:
            reason = plan.query_unavailable_reasons.get(RetrievalChannel.TYPED_GRAPH)
            if reason is not None:
                return reason
            if RetrievalChannel.TYPED_GRAPH in plan.effective_channels:
                count = trace.channel_candidate_counts.get(RetrievalChannel.TYPED_GRAPH, 0)
                if count == 0:
                    return "graph_zero_candidates"
            return None
        reason = trace.query_unavailable_reasons.get(RetrievalChannel.TYPED_GRAPH)
        if reason is not None:
            return reason
        if (
            RetrievalChannel.TYPED_GRAPH in trace.effective_channels
            and trace.channel_candidate_counts.get(RetrievalChannel.TYPED_GRAPH, 0) == 0
        ):
            return "graph_zero_candidates"
        return None

    @staticmethod
    def _graph_readiness_status(
        plan: RoutePlan | None,
        trace: RetrievalTrace,
        *,
        graph_edge_count: int | None,
        verified_receipt_count: int,
    ) -> str:
        reason = EvidenceFirstCheckpointRunner._graph_unavailable_reason(plan, trace)
        if reason == "missing_graph_seed":
            return "missing_seed"
        effective = (
            plan is not None and RetrievalChannel.TYPED_GRAPH in plan.effective_channels
        ) or RetrievalChannel.TYPED_GRAPH in trace.effective_channels
        if not effective:
            return "not_required"
        if graph_edge_count == 0:
            return "zero_edge"
        candidate_count = trace.channel_candidate_counts.get(RetrievalChannel.TYPED_GRAPH, 0)
        if candidate_count == 0:
            return "filtered_or_no_path"
        if verified_receipt_count == 0:
            return "unverified_receipt"
        return "ready"

    @staticmethod
    def _block(text: TextRootDocument, evidence: Any) -> Any:
        if evidence.span is None:
            return None
        for scene in (
            *(text.prelude.scenes if text.prelude is not None else ()),
            *(scene for chapter in text.chapters for scene in chapter.scenes),
        ):
            for block in scene.blocks:
                if block.block_id == evidence.span.block_id:
                    return block
        return None

    @staticmethod
    def _unresolved_anchors(
        need_generation: NeedGenerationResult | None,
        fallback_used: bool,
    ) -> tuple[UnresolvedLexicalAnchor, ...]:
        if fallback_used or need_generation is None:
            return ()
        artifact = need_generation.planner_artifact
        if artifact is None:
            return ()
        anchors: list[UnresolvedLexicalAnchor] = []
        for draft in artifact.grounded_drafts:
            for mention in draft.entity_mentions:
                if mention.grounding_status is GroundingStatus.UNRESOLVED:
                    anchors.append(
                        UnresolvedLexicalAnchor(
                            mention=mention.mention,
                            source_draft_id=draft.draft_id,
                            source_fields=mention.mention_source_fields,
                            grounding_method=mention.grounding_method,
                        )
                    )
        return tuple(anchors)

    @staticmethod
    def _scope_needs(needs: tuple[Stage1MemoryNeed, ...]) -> tuple[Stage1MemoryNeed, ...]:
        return tuple(
            need.model_copy(
                update={
                    "access_scope": "writer_safe",
                    "planner_may_read_plan": True,
                    "retrieval_may_return_plan": False,
                    "claim_may_cite_plan": False,
                    "legacy_allow_plan": False,
                    "allow_plan": False,
                }
            )
            for need in needs
        )

    @staticmethod
    def _allowed_tools(route_plans: tuple[RoutePlan, ...]) -> tuple[str, ...]:
        from novel_agent.tools.retrieval import CHANNEL_BY_TOOL

        active_channels = {
            step.channel
            for plan in route_plans
            for step in (
                *plan.mandatory_steps,
                *(step for group in plan.primary_groups for step in group.steps),
                *(step for fallback in plan.conditional_fallbacks for step in fallback.steps),
            )
        }
        return tuple(
            tool_name
            for tool_name, channel in sorted(CHANNEL_BY_TOOL.items())
            if channel in active_channels
        )


__all__ = ["EvidenceFirstCheckpointResult", "EvidenceFirstCheckpointRunner"]
