from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import JsonValue, ValidationError
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.agents.plan_reviewer import PlanReviewerAgent, PlanReviewerInvocationError
from novel_agent.agents.planner import (
    PlannerAgent,
    PlannerInvocationError,
)
from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.agent_context import ContextConsumer, ContextItemKind
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.memory import (
    ChannelHit,
    ContextBudgetReport,
    FacetClosureStatus,
    FacetEvidenceReceipt,
    FusedCandidate,
    NeedExecutionStatus,
    NeedFacetKind,
    RetrievalChannel,
    RetrievalStopReason,
    RetrievalTrace,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1ContextPackage,
    Stage1MemoryNeed,
)
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.planning import (
    PlannerContextBudgetReport,
    PlannerContextItem,
    PlannerContextPackage,
    PlannerContextProjection,
    PlannerContextSection,
    PlanningEvaluationCase,
    PlanningEvaluationCriterion,
    PlanningEvaluationManifest,
    PlanningEvaluationMetric,
    PlanningEvaluationObservation,
    PlanningEvaluationProfile,
    PlanningEvaluationRubric,
    PlanningEvaluationThresholds,
    PlanningInquiry,
    PlanningInquiryDraft,
    PlanningLoopCheckpoint,
    PlanningLoopRequest,
    PlanningLoopResult,
    PlanningLoopTerminal,
    PlanningProblemIdentitySeed,
    PlanningProvenance,
    PlanningTurnAction,
    PlanningTurnOutput,
    PlanReview,
    PlanReviewDraft,
    PlanReviewIssue,
    ReviewDecision,
    ReviewIssueKind,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    ControllerStopReason,
    PlannerExecutionResult,
    PlannerProposalDraft,
    PlanningTask,
    PlanProposal,
    ProjectIntentModel,
    ProposalProvenance,
    ProposedItem,
)
from novel_agent.ports.model_endpoint import ModelEndpointError
from novel_agent.ports.planning_context import (
    PlannerContextRuntimeFailure,
    PlannerContextRuntimePort,
)
from novel_agent.services.agent_context import (
    AgentContextProjector,
    AgentContextRuntime,
    ContextCompactor,
    ContextWindowPolicy,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.memory_gateway import MemoryGateway, MemoryGatewayBlockedError
from novel_agent.services.model_gateway import ModelRoutingError, StructuredGenerationExhausted
from novel_agent.services.planner_context_assembler import (
    PlannerContextAssembler,
    PlannerContextAssemblyError,
)
from novel_agent.services.planner_context_runtime import (
    PlannerContextRuntimeError,
    SharedPlannerContextRuntime,
)
from novel_agent.services.planning_context_loop import (
    PlanningContextLoopService,
    _planner_memory_question_chunk_size,
    _requested_planner_memory_question_ids,
)
from novel_agent.services.planning_evaluation import (
    REQUIRED_BLIND_REVIEW_METRICS,
    ConfiguredPlanningEvaluationAdapter,
    FakePlanningEvaluationAdapter,
    FrozenPlanningEvaluationGate,
    PlanningCaseLoadError,
    PlanningEvaluationArm,
    PlanningEvaluationError,
    PlanningEvaluationRunner,
    PlanningGateLoadError,
    evaluation_identity,
    load_frozen_planning_evaluation_gate,
    load_planning_evaluation_case,
)
from novel_agent.services.planning_inquiry_need_generation import (
    PlanningInquiryConditionedNeedGenerator,
    PlanningInquiryNeedError,
)
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.unit.test_stage2_planner_agent import (
    artifact as _agent_artifact,
)
from tests.unit.test_stage2_planner_agent import (
    harness as _planner_harness,
)
from tests.unit.test_stage2_planner_agent import (
    request as _agent_request,
)
from tests.unit.test_stage2_planner_agent import (
    task as _agent_task,
)
from tests.unit.test_stage4_planning_contracts import (
    BASE,
    HASH,
    PROJECT,
    VERSION,
    _inquiry,
    _put,
    _receipt,
    _request,
)


def _evaluation_observation(
    result: PlanningLoopResult,
    *,
    configuration_fingerprint: ArtifactId = HASH,
    model_fingerprint: ArtifactId = HASH,
) -> PlanningEvaluationObservation:
    return PlanningEvaluationObservation(
        result=result,
        configuration_fingerprint=configuration_fingerprint,
        model_fingerprint=model_fingerprint,
        prompt_tokens=10,
        completion_tokens=2,
        latency_ms=5,
        model_call_count=1,
        exposed_evidence_count=2,
        used_evidence_count=1,
        channel_failure_count=0,
    )


class _BootstrapPlanner:
    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts

    async def propose_inquiry(self, **kwargs: object) -> tuple[object, ...]:
        source_artifacts = kwargs["source_artifacts"]
        assert isinstance(source_artifacts, tuple)
        inquiry = _inquiry(AgentMode.PROJECT_BOOTSTRAP, source_artifacts[0])
        ref = self._artifacts.put(inquiry.model_dump_json().encode(), "application/json", VERSION)
        return (
            inquiry,
            ref,
            _receipt(AgentMode.PROJECT_BOOTSTRAP, AgentType.PLANNER),
            cast(ModelCallRecord, object()),
        )

    async def run(self, **kwargs: object) -> tuple[PlannerExecutionResult, ModelCallRecord]:
        task = cast(PlanningTask, kwargs["task"])
        assert task.strategy is not None
        receipt = _receipt(AgentMode.PROJECT_BOOTSTRAP, AgentType.PLANNER)
        item = ProposedItem(
            item_id=StableId("plan-item.bootstrap"),
            kind="story_seed",
            payload={"summary": "candidate"},
            provenance=ProposalProvenance.PLANNER_PROPOSED,
        )
        proposal = PlanProposal(
            proposal_id=StableId("plan-proposal.bootstrap"),
            project_id=PROJECT,
            mode=AgentMode.PROJECT_BOOTSTRAP,
            strategy=task.strategy,
            items=(item,),
            coverage=1.0,
            receipt=receipt,
            reviewed_inquiry_ref=cast(ArtifactRef | None, kwargs.get("reviewed_inquiry_ref")),
        )
        output = self._artifacts.put(b"{}", "application/json", VERSION)
        result = PlannerExecutionResult(
            mode=AgentMode.PROJECT_BOOTSTRAP,
            project_intent=ProjectIntentModel(
                intent_id=StableId("intent.bootstrap"),
                project_id=PROJECT,
                strategy=task.strategy,
                items=(item,),
                source_ids=(StableId("source.brief"),),
                coverage=1.0,
            ),
            plan_proposal=proposal,
            output_artifact=output,
            receipt=receipt,
        )
        return result, cast(ModelCallRecord, object())


class _AcceptingReviewer:
    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts

    async def review(self, **kwargs: object) -> tuple[PlanReview, ArtifactRef, ModelCallRecord]:
        mode = cast(AgentMode, kwargs["mode"])
        target_kind = cast(ReviewTargetKind, kwargs["target_kind"])
        target = cast(ArtifactRef, kwargs["target_artifact"])
        review = PlanReview(
            review_id=StableId(
                f"review.accept.{target_kind.value}.{target.artifact_id.root[-12:]}"
            ),
            target_kind=target_kind,
            target_artifact_ref=target,
            decision=ReviewDecision.ACCEPT,
            receipt=_receipt(mode, AgentType.PLAN_REVIEWER),
        )
        ref = self._artifacts.put(review.model_dump_json().encode(), "application/json", VERSION)
        return review, ref, cast(ModelCallRecord, object())


class _ScriptedReviewer(_AcceptingReviewer):
    def __init__(
        self,
        artifacts: ArtifactRepository,
        decisions: list[ReviewDecision],
        *,
        memory_gap: bool = False,
    ) -> None:
        super().__init__(artifacts)
        self._decisions = decisions
        self._memory_gap = memory_gap

    async def review(self, **kwargs: object) -> tuple[PlanReview, ArtifactRef, ModelCallRecord]:
        decision = self._decisions.pop(0)
        mode = cast(AgentMode, kwargs["mode"])
        target_kind = cast(ReviewTargetKind, kwargs["target_kind"])
        target = cast(ArtifactRef, kwargs["target_artifact"])
        review = PlanReview(
            review_id=StableId(f"review.scripted.{target_kind.value}.{len(self._decisions)}"),
            target_kind=target_kind,
            target_artifact_ref=target,
            decision=decision,
            revision_instruction="bounded repair" if decision is ReviewDecision.REVISE else None,
            memory_gap_questions=("relation between 林澈 and 北塔",)
            if self._memory_gap and target_kind is ReviewTargetKind.PLAN_PROPOSAL
            else (),
            receipt=_receipt(mode, AgentType.PLAN_REVIEWER),
        )
        ref = self._artifacts.put(review.model_dump_json().encode(), "application/json", VERSION)
        return review, ref, cast(ModelCallRecord, object())


class _ModePlanner(_BootstrapPlanner):
    def __init__(
        self,
        artifacts: ArtifactRepository,
        mode: AgentMode,
        *,
        unresolved: tuple[str, ...] = (),
    ) -> None:
        super().__init__(artifacts)
        self._mode = mode
        self._unresolved = unresolved
        self.inquiry_calls = 0
        self.plan_calls = 0
        self.turn_calls = 0
        self.plan_requests: list[dict[str, object]] = []
        self.inquiry_source_payloads: list[str] = []

    async def propose_inquiry(self, **kwargs: object) -> tuple[object, ...]:
        self.inquiry_calls += 1
        self.inquiry_source_payloads.append(cast(str, kwargs["source_payload"]))
        source_artifacts = cast(tuple[ArtifactRef, ...], kwargs["source_artifacts"])
        inquiry = _inquiry(self._mode, source_artifacts[0])
        parent = cast(StableId | None, kwargs.get("parent_inquiry_id"))
        if parent is not None:
            inquiry = inquiry.model_copy(
                update={
                    "parent_inquiry_id": parent,
                    "generation": cast(int, kwargs["generation"]),
                    "planning_scope": (
                        *inquiry.planning_scope,
                        f"reviewed-revision-{self.inquiry_calls}",
                    ),
                }
            )
        ref = self._artifacts.put(inquiry.model_dump_json().encode(), "application/json", VERSION)
        return (
            inquiry,
            ref,
            _receipt(self._mode, AgentType.PLANNER),
            cast(ModelCallRecord, object()),
        )

    async def run(self, **kwargs: object) -> tuple[PlannerExecutionResult, ModelCallRecord]:
        self.plan_calls += 1
        self.plan_requests.append(dict(kwargs))
        task = cast(PlanningTask, kwargs["task"])
        receipt = _receipt(self._mode, AgentType.PLANNER)
        proposal = PlanProposal(
            proposal_id=StableId(f"plan-proposal.{self._mode.value}.{self.plan_calls}"),
            project_id=PROJECT,
            mode=self._mode,
            strategy=task.strategy,
            base_commit=task.base_commit,
            items=(
                ProposedItem(
                    item_id=StableId(f"plan-item.{self._mode.value}.{self.plan_calls}"),
                    kind="chapter_goal",
                    payload={"revision": self.plan_calls},
                    provenance=ProposalProvenance.PLANNER_PROPOSED,
                ),
            ),
            unresolved=self._unresolved,
            coverage=1.0,
            receipt=receipt,
            reviewed_inquiry_ref=cast(ArtifactRef | None, kwargs.get("reviewed_inquiry_ref")),
            parent_proposal_id=cast(StableId | None, kwargs.get("parent_proposal_id")),
        )
        output = self._artifacts.put(b"{}", "application/json", VERSION)
        return (
            PlannerExecutionResult(
                mode=self._mode,
                plan_proposal=proposal,
                output_artifact=output,
                receipt=receipt,
            ),
            cast(ModelCallRecord, object()),
        )


class _TurnPlanner(_ModePlanner):
    """Planner that exposes run_turn so the loop can request reactive Memory."""

    def __init__(
        self,
        artifacts: ArtifactRepository,
        mode: AgentMode,
        *,
        questions: tuple[str, ...] = ("what is the causal relation with 北塔?",),
    ) -> None:
        super().__init__(artifacts, mode)
        self._questions = questions
        self.turn_calls = 0

    async def run_turn(self, **kwargs: object) -> tuple[object, object | None, object]:
        del kwargs
        self.turn_calls += 1
        turn = PlanningTurnOutput(
            action=PlanningTurnAction.REQUEST_MEMORY,
            memory_questions=self._questions,
        )
        return turn, None, cast(ModelCallRecord, object())


class _MemoryThenReadyPlanner(_ModePlanner):
    """First turn requests Memory; the follow-up turn authors the Plan."""

    def __init__(
        self,
        artifacts: ArtifactRepository,
        mode: AgentMode,
        *,
        questions: tuple[str, ...] = ("what is the causal relation with 北塔?",),
        first_turn_usage: object | None = None,
    ) -> None:
        super().__init__(artifacts, mode)
        self._questions = questions
        self._first_turn_usage = first_turn_usage
        self.turn_calls = 0

    async def run_turn(self, **kwargs: object) -> tuple[object, object | None, object]:
        self.turn_calls += 1
        if self.turn_calls == 1:
            turn = PlanningTurnOutput(
                action=PlanningTurnAction.REQUEST_MEMORY,
                memory_questions=self._questions,
            )
            return turn, None, self._first_turn_usage or cast(ModelCallRecord, object())
        result, call = await self.run(**kwargs)
        turn = PlanningTurnOutput(
            action=PlanningTurnAction.PLAN_READY,
            plan_proposal=result.plan_proposal,
        )
        return turn, result, call


class _HandledMemoryThenReadyPlanner(_ModePlanner):
    """Repeats a closed Memory request once, then accepts the existing context."""

    def __init__(
        self,
        artifacts: ArtifactRepository,
        mode: AgentMode,
        *,
        questions: tuple[str, ...] = ("what is the causal relation with 北塔?",),
    ) -> None:
        super().__init__(artifacts, mode)
        self._questions = questions
        self.turn_source_payloads: list[str] = []

    async def run_turn(self, **kwargs: object) -> tuple[object, object | None, object]:
        self.turn_calls += 1
        self.turn_source_payloads.append(cast(str, kwargs["source_payload"]))
        if self.turn_calls <= 2:
            turn = PlanningTurnOutput(
                action=PlanningTurnAction.REQUEST_MEMORY,
                memory_questions=self._questions,
            )
            return turn, None, cast(ModelCallRecord, object())
        result, call = await self.run(**kwargs)
        turn = PlanningTurnOutput(
            action=PlanningTurnAction.PLAN_READY,
            plan_proposal=result.plan_proposal,
        )
        return turn, result, call


class _RejectedMemoryThenReadyPlanner(_ModePlanner):
    """Requests an ungroundable fact once, then uses the rejection status."""

    def __init__(
        self,
        artifacts: ArtifactRepository,
        mode: AgentMode,
        *,
        questions: tuple[str, ...] = ("文风和句式参考是什么?",),
    ) -> None:
        super().__init__(artifacts, mode)
        self._questions = questions
        self.turn_source_payloads: list[str] = []

    async def propose_inquiry(self, **kwargs: object) -> tuple[object, ...]:
        inquiry, _ref, receipt, call = await super().propose_inquiry(**kwargs)
        inquiry = cast(PlanningInquiry, inquiry).model_copy(
            update={
                "goal_proposals": tuple(
                    goal.model_copy(update={"summary": "unanchored planner goal"})
                    for goal in cast(PlanningInquiry, inquiry).goal_proposals
                )
            }
        )
        ref = self._artifacts.put(inquiry.model_dump_json().encode(), "application/json", VERSION)
        return inquiry, ref, receipt, call

    async def run_turn(self, **kwargs: object) -> tuple[object, object | None, object]:
        self.turn_calls += 1
        self.turn_source_payloads.append(cast(str, kwargs["source_payload"]))
        if self.turn_calls == 1:
            turn = PlanningTurnOutput(
                action=PlanningTurnAction.REQUEST_MEMORY,
                memory_questions=self._questions,
            )
            return turn, None, cast(ModelCallRecord, object())
        result, call = await self.run(**kwargs)
        turn = PlanningTurnOutput(
            action=PlanningTurnAction.PLAN_READY,
            plan_proposal=result.plan_proposal,
        )
        return turn, result, call


class _UnsupportedMemoryThenReadyPlanner(_ModePlanner):
    """Receives a typed unsupported-content status before authoring the Plan."""

    def __init__(
        self,
        artifacts: ArtifactRepository,
        mode: AgentMode,
        *,
        questions: tuple[str, ...] = ("what is the causal relation with 北塔?",),
    ) -> None:
        super().__init__(artifacts, mode)
        self._questions = questions
        self.turn_source_payloads: list[str] = []

    async def run_turn(self, **kwargs: object) -> tuple[object, object | None, object]:
        self.turn_calls += 1
        self.turn_source_payloads.append(cast(str, kwargs["source_payload"]))
        if self.turn_calls == 1:
            turn = PlanningTurnOutput(
                action=PlanningTurnAction.REQUEST_MEMORY,
                memory_questions=self._questions,
            )
            return turn, None, cast(ModelCallRecord, object())
        result, call = await self.run(**kwargs)
        turn = PlanningTurnOutput(
            action=PlanningTurnAction.PLAN_READY,
            plan_proposal=result.plan_proposal,
        )
        return turn, result, call


class _UnsupportedRepeatsThenReadyPlanner(_ModePlanner):
    """Repeats an unsupported request once; the bounded fallback then authors the Plan."""

    def __init__(
        self,
        artifacts: ArtifactRepository,
        mode: AgentMode,
        *,
        questions: tuple[str, ...] = ("what is the causal relation with 北塔?",),
    ) -> None:
        super().__init__(artifacts, mode)
        self._questions = questions
        self.turn_source_payloads: list[str] = []

    async def run_turn(self, **kwargs: object) -> tuple[object, object | None, object]:
        self.turn_calls += 1
        self.turn_source_payloads.append(cast(str, kwargs["source_payload"]))
        return (
            PlanningTurnOutput(
                action=PlanningTurnAction.REQUEST_MEMORY,
                memory_questions=self._questions,
            ),
            None,
            cast(ModelCallRecord, object()),
        )


class _UnsupportedThenNarrowerThenReadyPlanner(_ModePlanner):
    """Narrows an unsupported request once before authoring the Plan."""

    def __init__(
        self,
        artifacts: ArtifactRepository,
        mode: AgentMode,
        *,
        questions: tuple[str, ...] = ("what is the causal relation with 北塔?",),
        narrower_questions: tuple[str, ...] = ("what is the current state of 北塔?",),
    ) -> None:
        super().__init__(artifacts, mode)
        self._questions = questions
        self._narrower_questions = narrower_questions
        self.turn_source_payloads: list[str] = []

    async def run_turn(self, **kwargs: object) -> tuple[object, object | None, object]:
        self.turn_calls += 1
        self.turn_source_payloads.append(cast(str, kwargs["source_payload"]))
        questions = (
            self._questions
            if self.turn_calls == 1
            else self._narrower_questions
            if self.turn_calls == 2
            else ()
        )
        if questions:
            turn = PlanningTurnOutput(
                action=PlanningTurnAction.REQUEST_MEMORY,
                memory_questions=questions,
            )
            return turn, None, cast(ModelCallRecord, object())
        result, call = await self.run(**kwargs)
        turn = PlanningTurnOutput(
            action=PlanningTurnAction.PLAN_READY,
            plan_proposal=result.plan_proposal,
        )
        return turn, result, call


def _fixture_supported_trace(
    need: Stage1MemoryNeed,
    *,
    supported: bool,
    with_candidate: bool = False,
    snapshot_id: StableId | None = None,
) -> RetrievalTrace:
    required = (
        tuple(need.completion_spec.required_need_facet_ids)
        if need.completion_spec is not None
        else tuple(facet.need_facet_id for facet in need.need_facets)
    )
    facet_by_id = {facet.need_facet_id: facet for facet in need.need_facets}
    receipts = tuple(
        FacetEvidenceReceipt(
            need_id=need.need_id,
            need_facet_id=facet_id,
            facet_kind=facet_by_id[facet_id].facet_kind,
            mandatory=need.requirement.value == "mandatory",
            status=(FacetClosureStatus.SUPPORTED if supported else FacetClosureStatus.UNSUPPORTED),
            stop_reason="fixture",
        )
        for facet_id in required
        if facet_id in facet_by_id
    )
    candidates = ()
    if with_candidate:
        unit = RetrievalUnit(
            unit_id=StableId(f"fixture-unit.{need.need_id.root}"),
            unit_kind=RetrievalUnitKind.FACT_ANCHOR,
            source_commit=need.base_commit,
            snapshot_id=snapshot_id or StableId("fixture-snapshot"),
            text="fixture evidence",
        )
        candidates = (
            FusedCandidate(
                unit=unit,
                fused_rank=1,
                rrf_score=1.0,
                channel_hits=(
                    ChannelHit(
                        unit=unit,
                        channel=RetrievalChannel.R0,
                        channel_rank=1,
                        raw_score=1.0,
                        candidate_count=1,
                        hit_reason="fixture",
                    ),
                ),
            ),
        )
    return RetrievalTrace(
        need_id=need.need_id,
        intent=need.query_intent,
        allowed_channels=(),
        channel_candidate_counts={},
        candidates=candidates,
        fusion_applied=False,
        stop_reason=(
            RetrievalStopReason.BUDGET_SATISFIED
            if candidates
            else RetrievalStopReason.NO_EXECUTABLE_QUERY
        ),
        need_execution_status=(
            NeedExecutionStatus.EXECUTED_WITH_CANDIDATES
            if candidates
            else NeedExecutionStatus.EXECUTED_EMPTY
        ),
        required_need_facet_ids=required,
        closed_need_facet_ids=tuple(
            receipt.need_facet_id
            for receipt in receipts
            if receipt.status is FacetClosureStatus.SUPPORTED
        ),
        facet_receipts=receipts,
    )


class _FixtureMemory:
    def __init__(
        self,
        artifacts: ArtifactRepository,
        *,
        stop_reason: ControllerStopReason = ControllerStopReason.SUFFICIENT,
        stop_reasons: tuple[ControllerStopReason, ...] = (),
        blocked: bool = False,
        mandatory_facets_total: int = 1,
        mandatory_facets_closed: int = 1,
        with_candidate: bool = False,
    ) -> None:
        self._artifacts = artifacts
        self._stop_reason = stop_reason
        self._stop_reasons = stop_reasons
        self._blocked = blocked
        self._mandatory_facets_total = mandatory_facets_total
        self._mandatory_facets_closed = mandatory_facets_closed
        self._with_candidate = with_candidate
        self.calls = 0
        self.requests: list[object] = []

    def resolve(self, request: object, text_root: object, **kwargs: object) -> object:
        del text_root, kwargs
        self.calls += 1
        self.requests.append(request)
        if self._blocked:
            raise MemoryGatewayBlockedError("blocked")
        typed_request = cast(SimpleNamespace, request)
        closed = self._mandatory_facets_closed >= self._mandatory_facets_total
        traces = tuple(
            _fixture_supported_trace(
                need,
                supported=closed,
                with_candidate=self._with_candidate,
                snapshot_id=typed_request.snapshot_id,
            )
            for need in tuple(getattr(typed_request, "initial_memory_needs", ()) or ())
            if isinstance(need, Stage1MemoryNeed)
        )
        package = Stage1ContextPackage(
            context_id=StableId(f"context.memory.{self.calls}"),
            base_commit=typed_request.base_commit,
            snapshot_id=typed_request.snapshot_id,
            task_contract="stage4",
            retrieval_traces=traces,
            budget_report=ContextBudgetReport(
                token_budget=100,
                mandatory_tokens=0,
                optional_tokens=0,
                full_chapter_read_count=0,
            ),
        )
        ref = self._artifacts.put(package.model_dump_json().encode(), "application/json", VERSION)
        return SimpleNamespace(
            context=package,
            frozen_context_artifact=ref,
            selected_result=SimpleNamespace(
                stop_reason=(
                    self._stop_reasons[min(self.calls - 1, len(self._stop_reasons) - 1)]
                    if self._stop_reasons
                    else self._stop_reason
                ),
                mandatory_need_facets_total=self._mandatory_facets_total,
                mandatory_need_facets_closed=self._mandatory_facets_closed,
            ),
        )


class _SupportThenUnresolvedMemory(_FixtureMemory):
    """Keeps the inquiry supported, then returns an honest planner facet gap."""

    def resolve(self, request: object, text_root: object, **kwargs: object) -> object:
        if self.calls >= 1:
            self._mandatory_facets_closed = 0
        return super().resolve(request, text_root, **kwargs)


class _SupportedThenReviewerUnresolvedMemory(_FixtureMemory):
    """Keeps inquiry/planner Memory supported, then leaves reviewer Memory unresolved."""

    def resolve(self, request: object, text_root: object, **kwargs: object) -> object:
        if self.calls >= 1:
            self._mandatory_facets_closed = 0
        return super().resolve(request, text_root, **kwargs)


class _UnresolvedThenSupportMemory(_FixtureMemory):
    """Returns one content gap, then supports a narrower bounded question."""

    def resolve(self, request: object, text_root: object, **kwargs: object) -> object:
        if self.calls == 1:
            self._mandatory_facets_closed = 0
        else:
            self._mandatory_facets_closed = self._mandatory_facets_total
        return super().resolve(request, text_root, **kwargs)


class _NoProgressPlanner(_ModePlanner):
    async def propose_inquiry(self, **kwargs: object) -> tuple[object, ...]:
        result = await super().propose_inquiry(**kwargs)
        parent = cast(StableId | None, kwargs.get("parent_inquiry_id"))
        if parent is None:
            return result
        source_artifacts = cast(tuple[ArtifactRef, ...], kwargs["source_artifacts"])
        inquiry = _inquiry(self._mode, source_artifacts[0]).model_copy(
            update={
                "inquiry_id": StableId(f"planning-inquiry.no-progress.{self.inquiry_calls}"),
                "parent_inquiry_id": parent,
                "generation": cast(int, kwargs["generation"]),
            }
        )
        ref = self._artifacts.put(inquiry.model_dump_json().encode(), "application/json", VERSION)
        return inquiry, ref, result[2], result[3]

    async def run(self, **kwargs: object) -> tuple[PlannerExecutionResult, ModelCallRecord]:
        result, call = await super().run(**kwargs)
        if self.plan_calls == 1:
            return result, call
        proposal = result.plan_proposal.model_copy(
            update={
                "items": tuple(
                    item.model_copy(update={"payload": {"revision": 1}})
                    for item in result.plan_proposal.items
                )
            }
        )
        return result.model_copy(update={"plan_proposal": proposal}), call


class _NeedFailure:
    def generate(self, **kwargs: object) -> object:
        del kwargs
        raise PlanningInquiryNeedError("invalid")


class _NoNeeds:
    def generate(self, **kwargs: object) -> object:
        del kwargs
        return SimpleNamespace(needs=())


class _ReviewerNoNeeds:
    def __init__(self) -> None:
        self._delegate = PlanningInquiryConditionedNeedGenerator()
        self.calls = 0

    def generate(self, **kwargs: object) -> object:
        self.calls += 1
        if self.calls == 2:
            return SimpleNamespace(needs=())
        return self._delegate.generate(**kwargs)  # type: ignore[arg-type]


class _BrokenAssembler:
    def assemble(self, **kwargs: object) -> object:
        del kwargs
        raise PlannerContextAssemblyError("too large")


class _ForbiddenMemory:
    def resolve(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("bootstrap must not call project Memory")


class _FixtureContextRuntime:
    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts
        self._projection: PlannerContextProjection | None = None

    def start(
        self,
        *,
        run_id: RunId,
        task_id: TaskId,
        seed: PlannerContextPackage,
        seed_ref: ArtifactRef,
    ) -> PlannerContextProjection:
        view_ref = self._artifacts.put(
            seed.rendered_context.encode(), "application/vnd.test.context-view", VERSION
        )
        self._projection = PlannerContextProjection(
            run_id=run_id,
            task_id=task_id,
            seed_ref=seed_ref,
            view_ref=view_ref,
            generation=1,
            basis_event_position=0,
            rendered_context=seed.rendered_context,
            token_count=seed.budget_report.selected_tokens,
            exposed_context_item_ids=tuple(item.context_item_id for item in seed.items),
        )
        return self._projection

    def append_delta(self, **kwargs: object) -> PlannerContextProjection:
        del kwargs
        assert self._projection is not None
        return self._projection

    def project(self, **kwargs: object) -> PlannerContextProjection:
        del kwargs
        assert self._projection is not None
        return self._projection


class _SuspendedContextRuntime(_FixtureContextRuntime):
    def start(
        self,
        *,
        run_id: RunId,
        task_id: TaskId,
        seed: PlannerContextPackage,
        seed_ref: ArtifactRef,
    ) -> PlannerContextProjection:
        projection = super().start(
            run_id=run_id,
            task_id=task_id,
            seed=seed,
            seed_ref=seed_ref,
        )
        self._projection = projection.model_copy(
            update={"suspended": True, "suspension_reason": "pressure"}
        )
        return self._projection


class _FailingPlanner(_ModePlanner):
    def __init__(self, artifacts: ArtifactRepository, error: Exception) -> None:
        super().__init__(artifacts, AgentMode.PROJECT_BOOTSTRAP)
        self._error = error

    async def propose_inquiry(self, **kwargs: object) -> tuple[object, ...]:
        del kwargs
        raise self._error


class _FailingReviewer(_AcceptingReviewer):
    async def review(self, **kwargs: object) -> tuple[PlanReview, ArtifactRef, ModelCallRecord]:
        del kwargs
        raise PlanReviewerInvocationError("invalid reviewer output")


class _FailingContextRuntime(_FixtureContextRuntime):
    def start(self, **kwargs: object) -> PlannerContextProjection:
        del kwargs
        raise PlannerContextRuntimeFailure("runtime unavailable")


def _shared_context_runtime(
    tmp_path: Path,
    artifacts: ArtifactRepository,
    *,
    policy: ContextWindowPolicy | None = None,
) -> tuple[SharedPlannerContextRuntime, AgentContextRuntime]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    events = RunEventLogRepository(factory)
    checkpoints = RunCheckpointRepository(factory)
    projector = AgentContextProjector(lambda text: max(1, len(text)))
    runtime = AgentContextRuntime(projector, artifacts, events, checkpoints, VERSION)
    compactor = ContextCompactor(
        projector,
        artifacts,
        VERSION,
        lambda text: max(1, len(text)),
    )
    return (
        SharedPlannerContextRuntime(
            projector=projector,
            runtime=runtime,
            compactor=compactor,
            artifacts=artifacts,
            schema_version=VERSION,
            policy=policy
            or ContextWindowPolicy(
                sequence_limit=100_000,
                reserved_output_tokens=1_000,
                safety_allowance_tokens=1_000,
                soft_limit_tokens=90_000,
                tokenizer="test",
                tokenizer_version="v1",
            ),
        ),
        runtime,
    )


def test_shared_runtime_long_task_checkpoints_do_not_alias_labels(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "long-task-runtime"))
    inquiry_ref = _put(artifacts, "reviewed inquiry", "application/json")
    stage1_ref = _put(artifacts, "memory context", "application/json")
    profile_ref = _put(artifacts, "profile")
    package = PlannerContextPackage(
        package_id=StableId("planner-context.long.initial"),
        contract_version="planner_context.v1",
        project_id=PROJECT,
        mode=AgentMode.STORY,
        planning_scope=("story",),
        base_commit=BASE,
        snapshot_id=StableId("snapshot.stage4.long"),
        profile_ref=profile_ref,
        reviewed_inquiry_ref=inquiry_ref,
        stage1_context_ref=stage1_ref,
        items=(),
        budget_report=PlannerContextBudgetReport(
            token_budget=300,
            mandatory_tokens=0,
            selected_tokens=0,
        ),
        rendered_context="seed",
    )
    package_ref = artifacts.put(
        package.model_dump_json().encode(),
        "application/vnd.novel-agent.planner-context-package+json",
        VERSION,
    )
    adapter, runtime = _shared_context_runtime(tmp_path, artifacts)
    run_id = RunId("run.stage4.long-checkpoint")
    task_id = TaskId("task.stage4.long." + "x" * 105)

    initial = adapter.start(
        run_id=run_id,
        task_id=task_id,
        seed=package,
        seed_ref=package_ref,
    )
    first = PlannerContextItem(
        context_item_id=StableId("planner-context.long.first"),
        section=PlannerContextSection.CURRENT_STATE,
        text="first fact",
        token_count=10,
    )
    second = PlannerContextItem(
        context_item_id=StableId("planner-context.long.second"),
        section=PlannerContextSection.RELATION_CAUSAL,
        text="second fact",
        token_count=10,
    )
    delta_one = package.model_copy(
        update={
            "package_id": StableId("planner-context.long.delta-one"),
            "items": (first,),
            "budget_report": PlannerContextBudgetReport(
                token_budget=300,
                mandatory_tokens=0,
                selected_tokens=10,
            ),
        }
    )
    delta_one_ref = artifacts.put(
        delta_one.model_dump_json().encode(),
        "application/vnd.novel-agent.planner-context-package+json",
        VERSION,
    )
    first_projection = adapter.append_delta(
        run_id=run_id,
        task_id=task_id,
        delta_ref=delta_one_ref,
    )
    delta_two = delta_one.model_copy(
        update={
            "package_id": StableId("planner-context.long.delta-two"),
            "items": (first, second),
            "budget_report": PlannerContextBudgetReport(
                token_budget=300,
                mandatory_tokens=0,
                selected_tokens=20,
            ),
        }
    )
    delta_two_ref = artifacts.put(
        delta_two.model_dump_json().encode(),
        "application/vnd.novel-agent.planner-context-package+json",
        VERSION,
    )
    second_projection = adapter.append_delta(
        run_id=run_id,
        task_id=task_id,
        delta_ref=delta_two_ref,
    )

    assert initial.basis_event_position == 1
    assert first_projection.basis_event_position > initial.basis_event_position
    assert second_projection.basis_event_position > first_projection.basis_event_position
    assert runtime.restore_latest(run_id, task_id=task_id, consumer=ContextConsumer.PLANNER)


def test_shared_runtime_seeds_and_recovers_bootstrap_planner_context(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "bootstrap-runtime"))
    source = _put(artifacts, "author source")
    request = _request(AgentMode.PROJECT_BOOTSTRAP, source)
    inquiry = _inquiry(AgentMode.PROJECT_BOOTSTRAP, source)
    inquiry_ref = artifacts.put(inquiry.model_dump_json().encode(), "application/json", VERSION)
    package, package_ref = PlannerContextAssembler(
        artifacts,
        schema_version=VERSION,
    ).assemble(request=request, inquiry=inquiry, inquiry_ref=inquiry_ref)
    adapter, runtime = _shared_context_runtime(tmp_path, artifacts)

    projection = adapter.start(
        run_id=request.run_id,
        task_id=request.task_id,
        seed=package,
        seed_ref=package_ref,
    )

    assert projection.generation == 0
    assert projection.basis_event_position == 1
    assert projection.seed_ref == package_ref
    assert not projection.suspended
    restored = runtime.restore_latest(
        request.run_id,
        task_id=request.task_id,
        consumer=ContextConsumer.PLANNER,
    )
    assert restored is not None
    assert restored.consumer is ContextConsumer.PLANNER
    assert restored.base_commit is None
    assert restored.snapshot_id is None
    assert {item.kind for item in restored.protected_items} >= {
        ContextItemKind.AUTHOR_INTENT,
        ContextItemKind.GOAL_PROPOSAL,
    }
    assert adapter.project(run_id=request.run_id, task_id=request.task_id).rendered_context == (
        projection.rendered_context
    )
    with pytest.raises(PlannerContextRuntimeError, match="already exists"):
        adapter.start(
            run_id=request.run_id,
            task_id=request.task_id,
            seed=package,
            seed_ref=package_ref,
        )
    second_task_id = TaskId("task.stage4.bootstrap.second")
    second_projection = adapter.start(
        run_id=request.run_id,
        task_id=second_task_id,
        seed=package,
        seed_ref=package_ref,
    )
    assert second_projection.basis_event_position == 2
    first_projection = adapter.project(run_id=request.run_id, task_id=request.task_id)
    assert first_projection.task_id == request.task_id
    assert first_projection.basis_event_position == 2
    assert adapter.project(run_id=request.run_id, task_id=second_task_id).task_id == second_task_id
    bootstrap_memory = PlannerContextItem(
        context_item_id=StableId("planner-context.bootstrap-memory"),
        section=PlannerContextSection.CURRENT_STATE,
        text="forbidden project memory",
        token_count=24,
    )
    bootstrap_delta = package.model_copy(
        update={
            "package_id": StableId("planner-context.bootstrap-delta"),
            "items": (*package.items, bootstrap_memory),
            "budget_report": package.budget_report.model_copy(
                update={
                    "selected_tokens": package.budget_report.selected_tokens + 24,
                }
            ),
        }
    )
    bootstrap_delta_ref = artifacts.put(
        bootstrap_delta.model_dump_json().encode(),
        "application/vnd.novel-agent.planner-context-package+json",
        VERSION,
    )
    with pytest.raises(PlannerContextRuntimeError, match="bootstrap"):
        adapter.append_delta(
            run_id=request.run_id,
            task_id=request.task_id,
            delta_ref=bootstrap_delta_ref,
        )
    with pytest.raises(PlannerContextRuntimeError, match="unavailable"):
        adapter.project(run_id=RunId("run.missing"), task_id=request.task_id)

    import novel_agent.services as services

    assert services.AgentContextProjector is AgentContextProjector
    assert services.AgentContextRuntime is AgentContextRuntime
    assert services.ContextCompactor is ContextCompactor
    assert services.SharedPlannerContextRuntime is SharedPlannerContextRuntime
    with pytest.raises(AttributeError, match="missing_service"):
        services.__getattr__("missing_service")


def test_shared_runtime_compacts_and_does_not_reexpand_retired_items(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "compact-runtime"))
    author_ref = _put(artifacts, "author")
    inquiry_ref = _put(artifacts, "reviewed inquiry", "application/json")
    stage1_ref = _put(artifacts, "memory context", "application/json")
    profile_ref = _put(artifacts, "profile", "application/json")
    author = PlannerContextItem(
        context_item_id=StableId("planner-context.author"),
        section=PlannerContextSection.AUTHOR_INTENT,
        text="author intent",
        protected=True,
        mandatory=True,
        token_count=13,
        source_artifact_refs=(author_ref,),
    )
    retired = PlannerContextItem(
        context_item_id=StableId("planner-context.retired"),
        section=PlannerContextSection.CURRENT_STATE,
        text="x" * 220,
        token_count=220,
        compact_handle=StableId("compact.handle.retired"),
    )
    package = PlannerContextPackage(
        package_id=StableId("planner-context.initial"),
        contract_version="planner_context.v1",
        project_id=PROJECT,
        mode=AgentMode.STORY,
        planning_scope=("story",),
        base_commit=CommitId("sha256:" + "a" * 64),
        snapshot_id=StableId("snapshot.stage4"),
        profile_ref=profile_ref,
        reviewed_inquiry_ref=inquiry_ref,
        stage1_context_ref=stage1_ref,
        items=(author, retired),
        budget_report=PlannerContextBudgetReport(
            token_budget=300,
            mandatory_tokens=13,
            selected_tokens=233,
        ),
        rendered_context="seed",
    )
    package_ref = artifacts.put(
        package.model_dump_json().encode(),
        "application/vnd.novel-agent.planner-context-package+json",
        VERSION,
    )
    adapter, runtime = _shared_context_runtime(
        tmp_path,
        artifacts,
        policy=ContextWindowPolicy(
            sequence_limit=300,
            reserved_output_tokens=20,
            safety_allowance_tokens=20,
            # The rendered context carries stable item ids. Keep the
            # post-compaction author+new fixture below the soft limit while
            # the initial author+retired fixture still requires compaction.
            soft_limit_tokens=220,
            tokenizer="test",
            tokenizer_version="v1",
        ),
    )

    initial = adapter.start(
        run_id=RunId("run.stage4.compact"),
        task_id=TaskId("task.stage4.compact"),
        seed=package,
        seed_ref=package_ref,
    )
    assert initial.generation == 1
    assert initial.compaction_receipt_ref is not None
    view = runtime.restore_latest(
        initial.run_id,
        task_id=initial.task_id,
        consumer=ContextConsumer.PLANNER,
    )
    assert view is not None
    assert retired.context_item_id in view.compacted_item_ids

    new_item = PlannerContextItem(
        context_item_id=StableId("planner-context.new"),
        section=PlannerContextSection.RELATION_CAUSAL,
        text="new fact",
        token_count=8,
    )
    delta_package = package.model_copy(
        update={
            "package_id": StableId("planner-context.delta"),
            "items": (author, retired, new_item),
            "budget_report": PlannerContextBudgetReport(
                token_budget=300,
                mandatory_tokens=13,
                selected_tokens=241,
            ),
        }
    )
    delta_ref = artifacts.put(
        delta_package.model_dump_json().encode(),
        "application/vnd.novel-agent.planner-context-package+json",
        VERSION,
    )
    updated = adapter.append_delta(
        run_id=initial.run_id,
        task_id=initial.task_id,
        delta_ref=delta_ref,
    )
    assert new_item.context_item_id in updated.exposed_context_item_ids
    assert retired.context_item_id not in updated.exposed_context_item_ids
    final_view = runtime.restore_latest(
        initial.run_id,
        task_id=initial.task_id,
        consumer=ContextConsumer.PLANNER,
    )
    assert final_view is not None
    assert tuple(item.item_id for item in final_view.active_memory_items) == (
        new_item.context_item_id,
    )
    assert (
        adapter.append_delta(
            run_id=initial.run_id,
            task_id=initial.task_id,
            delta_ref=delta_ref,
        ).rendered_context
        == updated.rendered_context
    )

    changed_item = new_item.model_copy(update={"text": "changed identity"})
    changed_package = delta_package.model_copy(update={"items": (author, changed_item)})
    changed_ref = artifacts.put(
        changed_package.model_dump_json().encode(),
        "application/vnd.novel-agent.planner-context-package+json",
        VERSION,
    )
    with pytest.raises(PlannerContextRuntimeError, match="identity changed"):
        adapter.append_delta(
            run_id=initial.run_id,
            task_id=initial.task_id,
            delta_ref=changed_ref,
        )

    changed_basis = delta_package.model_copy(update={"base_commit": CommitId("sha256:" + "b" * 64)})
    changed_basis_ref = artifacts.put(
        changed_basis.model_dump_json().encode(),
        "application/vnd.novel-agent.planner-context-package+json",
        VERSION,
    )
    with pytest.raises(PlannerContextRuntimeError, match="basis changed"):
        adapter.append_delta(
            run_id=initial.run_id,
            task_id=initial.task_id,
            delta_ref=changed_basis_ref,
        )
    with pytest.raises(PlannerContextRuntimeError, match="unavailable"):
        adapter.project(run_id=initial.run_id, task_id=TaskId("task.wrong"))


def test_shared_runtime_accepts_same_evidence_with_new_graph_provenance(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "graph-provenance-runtime"))
    author_ref = _put(artifacts, "author")
    inquiry_ref = _put(artifacts, "reviewed inquiry", "application/json")
    stage1_ref = _put(artifacts, "memory context", "application/json")
    profile_ref = _put(artifacts, "profile")
    graph_one = _put(artifacts, "graph path one", "application/vnd.novel-agent.graph-path+json")
    graph_two = _put(artifacts, "graph path two", "application/vnd.novel-agent.graph-path+json")
    author = PlannerContextItem(
        context_item_id=StableId("planner-context.graph.author"),
        section=PlannerContextSection.AUTHOR_INTENT,
        text="author intent",
        protected=True,
        mandatory=True,
        token_count=13,
        source_artifact_refs=(author_ref,),
    )
    memory = PlannerContextItem(
        context_item_id=StableId("planner-context.graph.memory"),
        section=PlannerContextSection.CURRENT_STATE,
        text="same evidence",
        token_count=12,
        graph_path_receipt_refs=(graph_one,),
    )
    package = PlannerContextPackage(
        package_id=StableId("planner-context.graph.initial"),
        contract_version="planner_context.v1",
        project_id=PROJECT,
        mode=AgentMode.STORY,
        planning_scope=("story",),
        base_commit=CommitId("sha256:" + "a" * 64),
        snapshot_id=StableId("snapshot.graph"),
        profile_ref=profile_ref,
        reviewed_inquiry_ref=inquiry_ref,
        stage1_context_ref=stage1_ref,
        items=(author, memory),
        budget_report=PlannerContextBudgetReport(
            token_budget=300,
            mandatory_tokens=13,
            selected_tokens=25,
        ),
        rendered_context="seed",
    )
    package_ref = artifacts.put(
        package.model_dump_json().encode(),
        "application/vnd.novel-agent.planner-context-package+json",
        VERSION,
    )
    adapter, runtime = _shared_context_runtime(tmp_path, artifacts)
    initial = adapter.start(
        run_id=RunId("run.stage4.graph-provenance"),
        task_id=TaskId("task.stage4.graph-provenance"),
        seed=package,
        seed_ref=package_ref,
    )

    rerouted = memory.model_copy(
        update={"graph_path_receipt_refs": (graph_two,), "mandatory": True}
    )
    delta = package.model_copy(
        update={
            "package_id": StableId("planner-context.graph.delta"),
            "items": (author, rerouted),
        }
    )
    delta_ref = artifacts.put(
        delta.model_dump_json().encode(),
        "application/vnd.novel-agent.planner-context-package+json",
        VERSION,
    )
    updated = adapter.append_delta(
        run_id=initial.run_id,
        task_id=initial.task_id,
        delta_ref=delta_ref,
    )

    assert not updated.suspended
    view = runtime.restore_latest(
        initial.run_id,
        task_id=initial.task_id,
        consumer=ContextConsumer.PLANNER,
    )
    assert view is not None
    assert tuple(item.item_id for item in view.active_memory_items) == (memory.context_item_id,)
    assert view.active_memory_items[0].mandatory is False


def test_shared_runtime_scopes_compact_groups_per_memory_package(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "scoped-compact-groups"))
    author_ref = _put(artifacts, "author")
    inquiry_ref = _put(artifacts, "reviewed inquiry", "application/json")
    stage1_ref = _put(artifacts, "memory context", "application/json")
    profile_ref = _put(artifacts, "profile")
    author = PlannerContextItem(
        context_item_id=StableId("planner-context.scoped.author"),
        section=PlannerContextSection.AUTHOR_INTENT,
        text="author intent",
        protected=True,
        mandatory=True,
        token_count=13,
        source_artifact_refs=(author_ref,),
    )
    first_excerpt = PlannerContextItem(
        context_item_id=StableId("planner-context.scoped.first"),
        section=PlannerContextSection.HISTORY_DEVIATION,
        text="first excerpt",
        token_count=12,
        compact_handle=StableId("compact.source.chapter-2"),
    )
    package = PlannerContextPackage(
        package_id=StableId("planner-context.scoped.initial"),
        contract_version="planner_context.v1",
        project_id=PROJECT,
        mode=AgentMode.STORY,
        planning_scope=("story",),
        base_commit=BASE,
        snapshot_id=StableId("snapshot.scoped-compact-groups"),
        profile_ref=profile_ref,
        reviewed_inquiry_ref=inquiry_ref,
        stage1_context_ref=stage1_ref,
        items=(author, first_excerpt),
        budget_report=PlannerContextBudgetReport(
            token_budget=300,
            mandatory_tokens=13,
            selected_tokens=25,
        ),
        rendered_context="seed",
    )
    package_ref = artifacts.put(
        package.model_dump_json().encode(),
        "application/vnd.novel-agent.planner-context-package+json",
        VERSION,
    )
    adapter, runtime = _shared_context_runtime(tmp_path, artifacts)
    initial = adapter.start(
        run_id=RunId("run.stage4.scoped-compact-groups"),
        task_id=TaskId("task.stage4.scoped-compact-groups"),
        seed=package,
        seed_ref=package_ref,
    )

    second_excerpt = first_excerpt.model_copy(
        update={
            "context_item_id": StableId("planner-context.scoped.second"),
            "text": "second excerpt from the same source",
        }
    )
    delta = package.model_copy(
        update={
            "package_id": StableId("planner-context.scoped.delta"),
            "items": (author, first_excerpt, second_excerpt),
            "budget_report": PlannerContextBudgetReport(
                token_budget=300,
                mandatory_tokens=13,
                selected_tokens=37,
            ),
        }
    )
    delta_ref = artifacts.put(
        delta.model_dump_json().encode(),
        "application/vnd.novel-agent.planner-context-package+json",
        VERSION,
    )
    updated = adapter.append_delta(
        run_id=initial.run_id,
        task_id=initial.task_id,
        delta_ref=delta_ref,
    )

    assert not updated.suspended
    view = runtime.restore_latest(
        initial.run_id,
        task_id=initial.task_id,
        consumer=ContextConsumer.PLANNER,
    )
    assert view is not None
    assert view.provider_validity_receipt is not None
    assert view.provider_validity_receipt.atomic_groups_valid
    assert tuple(item.item_id for item in view.active_memory_items) == (
        first_excerpt.context_item_id,
        second_excerpt.context_item_id,
    )
    assert (
        view.active_memory_items[0].atomic_group_id != view.active_memory_items[1].atomic_group_id
    )


def test_planner_compact_excerpt_identity_includes_content(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "compact-item-identity"))
    assembler = PlannerContextAssembler(artifacts, schema_version=VERSION)
    first = RetrievalUnit(
        unit_id=StableId("compact.grounded.block.chapter-1"),
        unit_kind=RetrievalUnitKind.GROUNDED_SPAN,
        source_commit=BASE,
        snapshot_id=StableId("snapshot.compact-item-identity"),
        text="first query-shaped excerpt",
    )
    second = first.model_copy(update={"text": "second query-shaped excerpt"})

    first_item, _ = assembler._unit_item(first, (), compact_handle=first.unit_id)
    second_item, _ = assembler._unit_item(second, (), compact_handle=second.unit_id)

    assert first_item.context_item_id != second_item.context_item_id
    assert first_item.context_item_id.root.startswith("planner-context.unit.compact.")


def test_shared_runtime_soft_pressure_hard_limit_and_provider_gate(tmp_path: Path) -> None:
    def bootstrap_package(
        name: str,
        text: str,
    ) -> tuple[ArtifactRepository, PlanningLoopRequest, PlannerContextPackage, ArtifactRef]:
        artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / name))
        source = _put(artifacts, text)
        request = _request(AgentMode.PROJECT_BOOTSTRAP, source)
        inquiry = _inquiry(AgentMode.PROJECT_BOOTSTRAP, source)
        inquiry_ref = artifacts.put(
            inquiry.model_dump_json().encode(),
            "application/json",
            VERSION,
        )
        package, package_ref = PlannerContextAssembler(
            artifacts,
            schema_version=VERSION,
        ).assemble(request=request, inquiry=inquiry, inquiry_ref=inquiry_ref)
        return artifacts, request, package, package_ref

    artifacts, request, package, package_ref = bootstrap_package("soft", "s" * 80)
    soft_adapter, _runtime = _shared_context_runtime(
        tmp_path,
        artifacts,
        policy=ContextWindowPolicy(
            sequence_limit=1_000,
            reserved_output_tokens=10,
            safety_allowance_tokens=10,
            soft_limit_tokens=50,
            tokenizer="test",
            tokenizer_version="v1",
        ),
    )
    soft = soft_adapter.start(
        run_id=RunId("run.stage4.soft"),
        task_id=request.task_id,
        seed=package,
        seed_ref=package_ref,
    )
    assert not soft.suspended
    assert soft.compaction_receipt_ref is None
    assert soft.basis_event_position == 2

    hard_artifacts, hard_request, hard_package, hard_ref = bootstrap_package(
        "hard",
        "h" * 200,
    )
    hard_adapter, _runtime = _shared_context_runtime(
        tmp_path,
        hard_artifacts,
        policy=ContextWindowPolicy(
            sequence_limit=100,
            reserved_output_tokens=10,
            safety_allowance_tokens=10,
            soft_limit_tokens=50,
            tokenizer="test",
            tokenizer_version="v1",
        ),
    )
    hard = hard_adapter.start(
        run_id=RunId("run.stage4.hard"),
        task_id=hard_request.task_id,
        seed=hard_package,
        seed_ref=hard_ref,
    )
    assert hard.suspended
    assert hard.suspension_reason == "CONTEXT_HARD_LIMIT"

    invalid_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "invalid"))
    inquiry_ref = _put(invalid_artifacts, "inquiry", "application/json")
    stage1_ref = _put(invalid_artifacts, "memory", "application/json")
    grouped = tuple(
        PlannerContextItem(
            context_item_id=StableId(f"planner-context.group.{index}"),
            section=PlannerContextSection.CURRENT_STATE,
            text=f"fact {index}",
            token_count=6,
            compact_handle=(
                StableId("compact.split") if index in {0, 2} else StableId("compact.other")
            ),
        )
        for index in range(3)
    )
    invalid_package = PlannerContextPackage(
        package_id=StableId("planner-context.invalid-provider"),
        contract_version="planner_context.v1",
        project_id=PROJECT,
        mode=AgentMode.STORY,
        planning_scope=("story",),
        base_commit=CommitId("sha256:" + "c" * 64),
        snapshot_id=StableId("snapshot.invalid-provider"),
        reviewed_inquiry_ref=inquiry_ref,
        stage1_context_ref=stage1_ref,
        items=grouped,
        budget_report=PlannerContextBudgetReport(
            token_budget=100,
            mandatory_tokens=0,
            selected_tokens=18,
        ),
        rendered_context="invalid atomic order",
    )
    invalid_ref = invalid_artifacts.put(
        invalid_package.model_dump_json().encode(),
        "application/vnd.novel-agent.planner-context-package+json",
        VERSION,
    )
    invalid_adapter, _runtime = _shared_context_runtime(tmp_path, invalid_artifacts)
    invalid = invalid_adapter.start(
        run_id=RunId("run.stage4.invalid-provider"),
        task_id=TaskId("task.stage4.invalid-provider"),
        seed=invalid_package,
        seed_ref=invalid_ref,
    )
    assert invalid.suspension_reason == "PROVIDER_CONTEXT_INVALID"
    assert (
        invalid_adapter.project(
            run_id=invalid.run_id,
            task_id=invalid.task_id,
        ).suspension_reason
        == "PROVIDER_CONTEXT_INVALID"
    )


def _model_request(stage: str, mode: AgentMode, attempt: int) -> ModelRequest:
    del stage, mode, attempt
    return cast(ModelRequest, object())


def _inquiry_draft(mode: AgentMode, source: ArtifactRef) -> PlanningInquiryDraft:
    inquiry = _inquiry(mode, source)
    return PlanningInquiryDraft(
        mode=mode,
        planning_scope=inquiry.planning_scope,
        horizon_start=inquiry.horizon_start,
        horizon_end=inquiry.horizon_end,
        goal_proposals=inquiry.goal_proposals,
        assumptions=inquiry.assumptions,
        questions=inquiry.questions,
        expected_output_shape=inquiry.expected_output_shape,
        human_choices=("author choice",),
    )


def test_planner_inquiry_agent_enforces_trusted_mode_horizon_and_sources(tmp_path: Path) -> None:
    mode = AgentMode.CHAPTER
    source = _agent_artifact()
    good = _inquiry_draft(mode, source)
    agent, endpoint, repository = _planner_harness(tmp_path, mode, cast(Any, good))
    inquiry, inquiry_ref, receipt, _ = asyncio.run(
        agent.propose_inquiry(
            version=VERSION,
            task=_agent_task(mode),
            source_payload="author data",
            source_artifacts=(source,),
            request=_agent_request(mode),
            horizon_start=21,
            horizon_end=23,
            explicit_overrides=("keep the vow",),
        )
    )
    assert inquiry.human_choices == ("author choice",)
    assert inquiry.generation == 1
    assert receipt.unresolved == inquiry.human_choices
    assert repository.read_verified(inquiry_ref)
    assert "PLANNING_PHASE=inquiry" in endpoint.requests[0].prompt

    with pytest.raises(PlannerInvocationError, match="artifact bindings"):
        asyncio.run(
            agent.propose_inquiry(
                version=VERSION,
                task=_agent_task(mode),
                source_payload="x",
                source_artifacts=(),
                request=_agent_request(mode),
            )
        )

    variants = (
        (good.model_copy(update={"mode": AgentMode.SCENE}), "mode differs"),
        (good.model_copy(update={"horizon_end": 24}), "horizon differs"),
        (
            good.model_copy(
                update={
                    "goal_proposals": (
                        good.goal_proposals[0].model_copy(
                            update={
                                "provenance": good.goal_proposals[0].provenance.model_copy(
                                    update={
                                        "provenance": PlanningProvenance.AUTHOR_SUPPLIED,
                                        "reference_ids": (StableId("source.foreign"),),
                                    }
                                )
                            }
                        ),
                    )
                }
            ),
            "foreign author source",
        ),
    )
    for index, (draft, message) in enumerate(variants):
        bad, _, _ = _planner_harness(tmp_path / f"bad-{index}", mode, cast(Any, draft))
        with pytest.raises(PlannerInvocationError, match=message):
            asyncio.run(
                bad.propose_inquiry(
                    version=VERSION,
                    task=_agent_task(mode),
                    source_payload="x",
                    source_artifacts=(source,),
                    request=_agent_request(mode),
                    horizon_start=21,
                    horizon_end=23,
                )
            )

    revision_agent, _, _ = _planner_harness(tmp_path / "revision", mode, cast(Any, good))
    revised, _, _, _ = asyncio.run(
        revision_agent.propose_inquiry(
            version=VERSION,
            task=_agent_task(mode),
            source_payload="author data",
            source_artifacts=(source,),
            request=_agent_request(mode),
            horizon_start=21,
            horizon_end=23,
            parent_inquiry_id=inquiry.inquiry_id,
        )
    )
    assert revised.generation == 2
    assert revised.parent_inquiry_id == inquiry.inquiry_id


class _ReviewRunner:
    def __init__(self, draft: PlanReviewDraft, receipt: object) -> None:
        self.draft = draft
        self._receipt = receipt
        self.prepared: object | None = None

    def prepare(self, *args: object, **kwargs: object) -> object:
        self.prepared = (args, kwargs)
        return object()

    async def execute(self, prepared: object, output_type: object) -> object:
        del prepared, output_type
        return SimpleNamespace(output=self.draft, model_call=cast(ModelCallRecord, object()))

    def receipt(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return self._receipt


def test_independent_reviewer_persists_receipt_and_rejects_target_substitution(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "reviewer"))
    target = _put(artifacts, "candidate")
    source = _put(artifacts, "author")
    issue = PlanReviewIssue(
        issue_id=StableId("issue.memory"),
        kind=ReviewIssueKind.MEMORY_GAP,
        summary="verify relation",
        blocking=True,
    )
    draft = PlanReviewDraft(
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        decision=ReviewDecision.REVISE,
        issues=(issue,),
        revision_instruction="verify then revise",
        memory_gap_questions=("relation between A and B",),
    )
    runner = _ReviewRunner(draft, _receipt(AgentMode.STORY, AgentType.PLAN_REVIEWER))
    agent = PlanReviewerAgent(cast(StructuredAgentRunner, runner), artifacts)
    review, review_ref, _ = asyncio.run(
        agent.review(
            version=VERSION,
            mode=AgentMode.STORY,
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            target_payload="candidate payload",
            target_artifact=target,
            trusted_source_artifacts=(source,),
            request=cast(ModelRequest, object()),
            base_commit=None,
        )
    )
    assert review.issues == (issue,)
    assert artifacts.read_verified(review_ref)
    assert runner.prepared is not None

    wrong = _ReviewRunner(
        draft.model_copy(update={"target_kind": ReviewTargetKind.INQUIRY}),
        _receipt(AgentMode.STORY, AgentType.PLAN_REVIEWER),
    )
    with pytest.raises(PlanReviewerInvocationError, match="target kind"):
        asyncio.run(
            PlanReviewerAgent(cast(StructuredAgentRunner, wrong), artifacts).review(
                version=VERSION,
                mode=AgentMode.STORY,
                target_kind=ReviewTargetKind.PLAN_PROPOSAL,
                target_payload="candidate payload",
                target_artifact=target,
                trusted_source_artifacts=(source,),
                request=cast(ModelRequest, object()),
                base_commit=None,
            )
        )


def test_bootstrap_full_loop_never_calls_memory_and_returns_reviewed_candidate(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    source = _put(artifacts, "author-approved source")
    shared_runtime, _runtime = _shared_context_runtime(tmp_path, artifacts)
    service = PlanningContextLoopService(
        planner=cast(PlannerAgent, _BootstrapPlanner(artifacts)),
        reviewer=cast(PlanReviewerAgent, _AcceptingReviewer(artifacts)),
        need_generator=PlanningInquiryConditionedNeedGenerator(),
        memory_gateway=cast(MemoryGateway, _ForbiddenMemory()),
        context_assembler=PlannerContextAssembler(artifacts, schema_version=VERSION),
        context_runtime=shared_runtime,
        artifacts=artifacts,
        schema_version=VERSION,
    )

    result = asyncio.run(
        service.run(
            request=_request(AgentMode.PROJECT_BOOTSTRAP, source),
            model_request=_model_request,
        )
    )

    assert result.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert result.proposal is not None
    assert result.proposal.base_commit is None
    assert result.plan_review_ref is not None
    assert result.memory_context_ref is None
    assert result.event_artifacts


def test_post_genesis_inquiry_receives_exact_world_entity_labels(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    source = _put(artifacts, "author-approved source")
    accepted = (
        _put(artifacts, "accepted-0"),
        _put(artifacts, "accepted-1"),
        _put(artifacts, "accepted-2"),
    )
    service, planner, _memory = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT, ReviewDecision.ACCEPT]),
    )

    asyncio.run(
        service.run(
            request=_request(AgentMode.CHAPTER_SET, source, accepted=accepted),
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )

    assert len(planner.inquiry_source_payloads) == 1
    payload = planner.inquiry_source_payloads[0]
    assert "WORLD_ENTITY_LABELS=" in payload
    assert world.entities[0].internal_label in payload


def test_loop_rehydrates_json_arrays_into_strict_domain_tuples(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    service, _planner, _memory = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT]),
    )
    package = Stage1ContextPackage(
        context_id=StableId("context.resume"),
        base_commit=BASE,
        snapshot_id=StableId("snapshot.resume"),
        task_contract="stage4",
        budget_report=ContextBudgetReport(
            token_budget=1,
            mandatory_tokens=0,
            optional_tokens=0,
            full_chapter_read_count=0,
        ),
    )
    ref = artifacts.put(package.model_dump_json().encode(), "application/json", VERSION)

    assert service._read(ref, Stage1ContextPackage) == package


@pytest.mark.parametrize(
    ("planner_error", "reviewer_fails", "runtime_fails", "expected"),
    (
        (
            PlannerInvocationError("invalid planner output"),
            False,
            False,
            PlanningLoopTerminal.BLOCKED,
        ),
        (
            ModelRoutingError("endpoint missing"),
            False,
            False,
            PlanningLoopTerminal.MODEL_UNAVAILABLE,
        ),
        (
            ModelEndpointError("transport exhausted"),
            False,
            False,
            PlanningLoopTerminal.MODEL_UNAVAILABLE,
        ),
        (None, True, False, PlanningLoopTerminal.REVIEW_REQUIRED),
        (None, False, True, PlanningLoopTerminal.SUSPENDED),
    ),
)
def test_loop_maps_owner_failures_to_typed_terminals(
    tmp_path: Path,
    planner_error: Exception | None,
    reviewer_fails: bool,
    runtime_fails: bool,
    expected: PlanningLoopTerminal,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / expected.value))
    source = _put(artifacts, "author-approved source")
    planner = (
        _FailingPlanner(artifacts, planner_error)
        if planner_error is not None
        else _ModePlanner(artifacts, AgentMode.PROJECT_BOOTSTRAP)
    )
    reviewer = _FailingReviewer(artifacts) if reviewer_fails else _AcceptingReviewer(artifacts)
    runtime = (
        _FailingContextRuntime(artifacts) if runtime_fails else _FixtureContextRuntime(artifacts)
    )
    service = PlanningContextLoopService(
        planner=cast(PlannerAgent, planner),
        reviewer=cast(PlanReviewerAgent, reviewer),
        need_generator=PlanningInquiryConditionedNeedGenerator(),
        memory_gateway=cast(MemoryGateway, _ForbiddenMemory()),
        context_assembler=PlannerContextAssembler(artifacts, schema_version=VERSION),
        context_runtime=cast(PlannerContextRuntimePort, runtime),
        artifacts=artifacts,
        schema_version=VERSION,
    )

    result = asyncio.run(
        service.run(
            request=_request(AgentMode.PROJECT_BOOTSTRAP, source),
            model_request=_model_request,
        )
    )

    assert result.terminal is expected
    assert result.diagnostic_codes
    if reviewer_fails or runtime_fails:
        assert result.event_artifacts


def test_loop_maps_structured_planner_rejection_to_typed_contract_failure(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "structured-rejection"))
    source = _put(artifacts, "author-approved source")
    with pytest.raises(ValidationError) as captured:
        PlannerProposalDraft.model_validate({"mode": AgentMode.CHAPTER_SET, "coverage": 0.6})
    planner_error = StructuredGenerationExhausted(captured.value, ())
    service = PlanningContextLoopService(
        planner=cast(PlannerAgent, _FailingPlanner(artifacts, planner_error)),
        reviewer=cast(PlanReviewerAgent, _AcceptingReviewer(artifacts)),
        need_generator=PlanningInquiryConditionedNeedGenerator(),
        memory_gateway=cast(MemoryGateway, _ForbiddenMemory()),
        context_assembler=PlannerContextAssembler(artifacts, schema_version=VERSION),
        context_runtime=cast(PlannerContextRuntimePort, _FixtureContextRuntime(artifacts)),
        artifacts=artifacts,
        schema_version=VERSION,
    )

    result = asyncio.run(
        service.run(
            request=_request(AgentMode.PROJECT_BOOTSTRAP, source),
            model_request=_model_request,
        )
    )

    assert result.terminal is PlanningLoopTerminal.BLOCKED
    assert result.diagnostic_codes == ("PLANNER_STRUCTURED_OUTPUT_REJECTED",)


def _post_genesis_service(
    artifacts: ArtifactRepository,
    *,
    reviewer: _ScriptedReviewer,
    planner: _ModePlanner | None = None,
    needs: object | None = None,
    memory: _FixtureMemory | None = None,
    assembler: object | None = None,
    runtime: _FixtureContextRuntime | None = None,
) -> tuple[PlanningContextLoopService, _ModePlanner, _FixtureMemory]:
    selected_planner = planner or _ModePlanner(artifacts, AgentMode.CHAPTER_SET)
    selected_memory = memory or _FixtureMemory(artifacts)
    return (
        PlanningContextLoopService(
            planner=cast(PlannerAgent, selected_planner),
            reviewer=cast(PlanReviewerAgent, reviewer),
            need_generator=cast(
                PlanningInquiryConditionedNeedGenerator,
                needs or PlanningInquiryConditionedNeedGenerator(),
            ),
            memory_gateway=cast(MemoryGateway, selected_memory),
            context_assembler=cast(
                PlannerContextAssembler,
                assembler or PlannerContextAssembler(artifacts, schema_version=VERSION),
            ),
            context_runtime=cast(
                PlannerContextRuntimePort,
                runtime or _FixtureContextRuntime(artifacts),
            ),
            artifacts=artifacts,
            schema_version=VERSION,
        ),
        selected_planner,
        selected_memory,
    )


def test_loop_typed_terminals_revision_memory_pressure_and_resume(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]

    def setup(name: str) -> tuple[ArtifactRepository, ArtifactRef, PlanningLoopRequest]:
        artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / name))
        source = _put(artifacts, "author source")
        roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
        request = _request(
            AgentMode.CHAPTER_SET,
            source,
            accepted=(roots[0], roots[1], roots[2]),
        )
        return artifacts, source, request

    artifacts, source, request = setup("basis")
    service, _, _ = _post_genesis_service(
        artifacts, reviewer=_ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT])
    )
    assert (
        asyncio.run(service.run(request=request, model_request=_model_request)).terminal
        is PlanningLoopTerminal.BASIS_CHANGED
    )
    bootstrap_request = _request(AgentMode.PROJECT_BOOTSTRAP, source)
    bootstrap_service = PlanningContextLoopService(
        planner=cast(PlannerAgent, _ModePlanner(artifacts, AgentMode.PROJECT_BOOTSTRAP)),
        reviewer=cast(
            PlanReviewerAgent,
            _ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT]),
        ),
        need_generator=PlanningInquiryConditionedNeedGenerator(),
        memory_gateway=cast(MemoryGateway, _ForbiddenMemory()),
        context_assembler=PlannerContextAssembler(artifacts, schema_version=VERSION),
        context_runtime=cast(PlannerContextRuntimePort, _FixtureContextRuntime(artifacts)),
        artifacts=artifacts,
        schema_version=VERSION,
    )
    assert asyncio.run(
        bootstrap_service.run(
            request=bootstrap_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    ).diagnostic_codes == ("BOOTSTRAP_RECEIVED_PROJECT_MEMORY",)

    terminal_cases: tuple[
        tuple[
            str,
            list[ReviewDecision],
            object | None,
            object | None,
            object | None,
            PlanningLoopTerminal,
        ],
        ...,
    ] = (
        (
            "inquiry-budget",
            [ReviewDecision.REVISE],
            None,
            None,
            None,
            PlanningLoopTerminal.INQUIRY_REVIEW_REQUIRED,
        ),
        (
            "inquiry-human",
            [ReviewDecision.REVISE, ReviewDecision.HUMAN_REQUIRED],
            None,
            None,
            None,
            PlanningLoopTerminal.HUMAN_REQUIRED,
        ),
        (
            "need-invalid",
            [ReviewDecision.ACCEPT],
            _NeedFailure(),
            None,
            None,
            PlanningLoopTerminal.INQUIRY_INVALID,
        ),
        (
            "need-empty",
            [ReviewDecision.ACCEPT],
            _NoNeeds(),
            None,
            None,
            PlanningLoopTerminal.MEMORY_INSUFFICIENT,
        ),
        (
            "context-error",
            [ReviewDecision.ACCEPT],
            None,
            None,
            _BrokenAssembler(),
            PlanningLoopTerminal.CONTEXT_LIMIT,
        ),
    )
    for name, decisions, needs, memory, assembler, terminal in terminal_cases:
        case_artifacts, _, case_request = setup(name)
        if name == "inquiry-budget":
            budgets = case_request.budgets.model_copy(update={"inquiry_revisions": 0})
            case_request = case_request.model_copy(update={"budgets": budgets})
        case_service, _, _ = _post_genesis_service(
            case_artifacts,
            reviewer=_ScriptedReviewer(case_artifacts, decisions),
            needs=needs,
            memory=cast(_FixtureMemory | None, memory),
            assembler=assembler,
        )
        result = asyncio.run(
            case_service.run(
                request=case_request,
                model_request=_model_request,
                world=world,
                text_root=text_root,
            )
        )
        assert result.terminal is terminal

    for name, memory, expected_terminal in (
        (
            "memory-blocked",
            _FixtureMemory(
                ArtifactRepository(FilesystemObjectStore(tmp_path / "placeholder")), blocked=True
            ),
            PlanningLoopTerminal.MEMORY_INSUFFICIENT,
        ),
        (
            "memory-budget",
            _FixtureMemory(
                ArtifactRepository(FilesystemObjectStore(tmp_path / "placeholder-2")),
                stop_reason=ControllerStopReason.BUDGET_EXHAUSTED,
            ),
            PlanningLoopTerminal.YIELDED,
        ),
        (
            "memory-conflict",
            _FixtureMemory(
                ArtifactRepository(FilesystemObjectStore(tmp_path / "placeholder-3")),
                stop_reason=ControllerStopReason.CONFLICT_REQUIRES_REVIEW,
            ),
            PlanningLoopTerminal.PLAN_CONFLICT,
        ),
    ):
        case_artifacts, _, case_request = setup(name)
        memory._artifacts = case_artifacts
        case_service, _, _ = _post_genesis_service(
            case_artifacts,
            reviewer=_ScriptedReviewer(case_artifacts, [ReviewDecision.ACCEPT]),
            memory=memory,
        )
        result = asyncio.run(
            case_service.run(
                request=case_request,
                model_request=_model_request,
                world=world,
                text_root=text_root,
            )
        )
        assert result.terminal is expected_terminal

    pressure_artifacts, _, pressure_request = setup("pressure")
    pressure_runtime = _SuspendedContextRuntime(pressure_artifacts)
    pressure_service, _, _ = _post_genesis_service(
        pressure_artifacts,
        reviewer=_ScriptedReviewer(pressure_artifacts, [ReviewDecision.ACCEPT]),
        runtime=pressure_runtime,
    )
    pressure = asyncio.run(
        pressure_service.run(
            request=pressure_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert pressure.terminal is PlanningLoopTerminal.SUSPENDED
    assert pressure.diagnostic_codes == ("pressure",)

    outcome_cases = (
        (
            "plan-budget",
            [ReviewDecision.ACCEPT, ReviewDecision.REVISE, ReviewDecision.ACCEPT],
            (),
            0,
            PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
        ),
        (
            "plan-human",
            [ReviewDecision.ACCEPT, ReviewDecision.REVISE, ReviewDecision.HUMAN_REQUIRED],
            (),
            1,
            PlanningLoopTerminal.HUMAN_REQUIRED,
        ),
        (
            "plan-rerevise",
            [ReviewDecision.ACCEPT, ReviewDecision.REVISE, ReviewDecision.REVISE],
            (),
            1,
            PlanningLoopTerminal.YIELDED,
        ),
        (
            "plan-degraded",
            [ReviewDecision.ACCEPT, ReviewDecision.ACCEPT],
            ("unresolved author choice",),
            1,
            PlanningLoopTerminal.PLAN_CANDIDATE_READY,
        ),
    )
    for name, decisions, unresolved, revision_budget, expected in outcome_cases:
        case_artifacts, _, case_request = setup(name)
        case_request = case_request.model_copy(
            update={
                "budgets": case_request.budgets.model_copy(
                    update={"plan_revisions": revision_budget}
                )
            }
        )
        case_planner = _ModePlanner(
            case_artifacts,
            AgentMode.CHAPTER_SET,
            unresolved=unresolved,
        )
        case_service, _, _ = _post_genesis_service(
            case_artifacts,
            reviewer=_ScriptedReviewer(case_artifacts, decisions),
            planner=case_planner,
        )
        result = asyncio.run(
            case_service.run(
                request=case_request,
                model_request=_model_request,
                world=world,
                text_root=text_root,
            )
        )
        assert result.terminal is expected
        assert result.degraded is (expected is PlanningLoopTerminal.DEGRADED_NOT_PROMOTABLE)

    no_gap_artifacts, _, no_gap_request = setup("reviewer-no-needs")
    reviewer_no_needs = _ReviewerNoNeeds()
    no_gap_service, no_gap_planner, no_gap_memory = _post_genesis_service(
        no_gap_artifacts,
        reviewer=_ScriptedReviewer(
            no_gap_artifacts,
            [ReviewDecision.ACCEPT, ReviewDecision.REVISE, ReviewDecision.ACCEPT],
            memory_gap=True,
        ),
        needs=reviewer_no_needs,
    )
    no_gap = asyncio.run(
        no_gap_service.run(
            request=no_gap_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert no_gap.terminal is PlanningLoopTerminal.REVIEW_REVISION_REQUIRED
    assert no_gap.diagnostic_codes == ("NO_VALID_REVIEWER_MEMORY_NEEDS",)
    assert no_gap_planner.plan_calls == 1
    assert no_gap_memory.calls == 1

    revision_artifacts, _, revision_request = setup("revision")
    revision_runtime = _FixtureContextRuntime(revision_artifacts)
    revision_reviewer = _ScriptedReviewer(
        revision_artifacts,
        [ReviewDecision.ACCEPT, ReviewDecision.REVISE, ReviewDecision.ACCEPT],
        memory_gap=True,
    )
    revision_service, revision_planner, revision_memory = _post_genesis_service(
        revision_artifacts,
        reviewer=revision_reviewer,
        runtime=revision_runtime,
    )
    revised = asyncio.run(
        revision_service.run(
            request=revision_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert revised.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert revision_planner.plan_calls == 2
    assert revision_memory.calls == 2
    assert revised.proposal is not None and revised.proposal.parent_proposal_id is not None

    checkpoint_refs = tuple(
        ref for ref in revised.event_artifacts if "checkpoint" in ref.media_type
    )
    checkpoint_ref = checkpoint_refs[-1]
    final_checkpoint = PlanningLoopCheckpoint.model_validate_json(
        revision_artifacts.read_verified(checkpoint_ref)
    )
    assert final_checkpoint.execution_ref is not None
    assert final_checkpoint.reviewer_memory_rounds_used == 1
    assert len(final_checkpoint.reviewer_memory_review_ids) == 1
    assert len(final_checkpoint.reviewer_context_refs) == 1
    resume_reviewer = _ScriptedReviewer(
        revision_artifacts,
        [],
    )
    resume_service, resume_planner, resume_memory = _post_genesis_service(
        revision_artifacts,
        reviewer=resume_reviewer,
        runtime=revision_runtime,
    )
    resumed = asyncio.run(
        resume_service.run(
            request=revision_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
            resume_checkpoint_ref=checkpoint_ref,
        )
    )
    assert resumed.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert resume_planner.inquiry_calls == 0
    assert resume_planner.plan_calls == 0
    assert resume_memory.calls == 0
    changed = revision_request.model_copy(
        update={"configuration_fingerprint": ArtifactId("sha256:" + "b" * 64)}
    )
    mismatched = asyncio.run(
        resume_service.run(
            request=changed,
            model_request=_model_request,
            world=world,
            text_root=text_root,
            resume_checkpoint_ref=checkpoint_ref,
        )
    )
    assert mismatched.diagnostic_codes == ("RESUME_CHECKPOINT_BASIS_MISMATCH",)

    binary_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "binary"))
    binary = binary_artifacts.put(b"\xff", "application/octet-stream", VERSION)
    binary_request = _request(AgentMode.PROJECT_BOOTSTRAP, binary)
    binary_service = PlanningContextLoopService(
        planner=cast(PlannerAgent, _ModePlanner(binary_artifacts, AgentMode.PROJECT_BOOTSTRAP)),
        reviewer=cast(
            PlanReviewerAgent,
            _ScriptedReviewer(binary_artifacts, [ReviewDecision.ACCEPT]),
        ),
        need_generator=PlanningInquiryConditionedNeedGenerator(),
        memory_gateway=cast(MemoryGateway, _ForbiddenMemory()),
        context_assembler=PlannerContextAssembler(binary_artifacts, schema_version=VERSION),
        context_runtime=cast(PlannerContextRuntimePort, _FixtureContextRuntime(binary_artifacts)),
        artifacts=binary_artifacts,
        schema_version=VERSION,
    )
    with pytest.raises(ValueError, match="not UTF-8"):
        asyncio.run(binary_service.run(request=binary_request, model_request=_model_request))


def test_planning_work_slices_resume_progress_and_stop_identity_only_revisions(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]

    def setup(name: str) -> tuple[ArtifactRepository, PlanningLoopRequest]:
        artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / name))
        source = _put(artifacts, "author source")
        roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
        return artifacts, _request(
            AgentMode.CHAPTER_SET,
            source,
            accepted=(roots[0], roots[1], roots[2]),
        )

    inquiry_artifacts, inquiry_request = setup("inquiry-slices")
    inquiry_reviewer = _ScriptedReviewer(
        inquiry_artifacts,
        [
            ReviewDecision.REVISE,
            ReviewDecision.REVISE,
            ReviewDecision.ACCEPT,
            ReviewDecision.ACCEPT,
        ],
    )
    inquiry_service, inquiry_planner, inquiry_memory = _post_genesis_service(
        inquiry_artifacts,
        reviewer=inquiry_reviewer,
    )
    inquiry_yield = asyncio.run(
        inquiry_service.run(
            request=inquiry_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert inquiry_yield.terminal is PlanningLoopTerminal.YIELDED
    assert inquiry_yield.diagnostic_codes == ("INQUIRY_REVISION_SLICE_EXHAUSTED",)
    inquiry_checkpoint = next(
        ref for ref in reversed(inquiry_yield.event_artifacts) if "checkpoint" in ref.media_type
    )
    inquiry_ready = asyncio.run(
        inquiry_service.run(
            request=inquiry_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
            resume_checkpoint_ref=inquiry_checkpoint,
        )
    )
    assert inquiry_ready.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert inquiry_planner.inquiry_calls == 3
    assert inquiry_planner.plan_calls == 1
    assert inquiry_memory.calls == 1

    plan_artifacts, plan_request = setup("plan-slices")
    plan_reviewer = _ScriptedReviewer(
        plan_artifacts,
        [
            ReviewDecision.ACCEPT,
            ReviewDecision.REVISE,
            ReviewDecision.REVISE,
            ReviewDecision.ACCEPT,
        ],
        memory_gap=True,
    )
    plan_service, plan_planner, plan_memory = _post_genesis_service(
        plan_artifacts,
        reviewer=plan_reviewer,
    )
    plan_yield = asyncio.run(
        plan_service.run(
            request=plan_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert plan_yield.terminal is PlanningLoopTerminal.YIELDED
    assert plan_yield.diagnostic_codes == ("PLAN_REVISION_SLICE_EXHAUSTED",)
    plan_checkpoint = next(
        ref for ref in reversed(plan_yield.event_artifacts) if "checkpoint" in ref.media_type
    )
    plan_ready = asyncio.run(
        plan_service.run(
            request=plan_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
            resume_checkpoint_ref=plan_checkpoint,
        )
    )
    assert plan_ready.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert plan_planner.inquiry_calls == 1
    assert plan_planner.plan_calls == 3
    assert plan_memory.calls == 3
    final_trusted_context = cast(
        tuple[ArtifactRef, ...], plan_planner.plan_requests[-1]["trusted_context_artifacts"]
    )
    assert len(final_trusted_context) == 5
    final_plan_checkpoint_ref = next(
        ref for ref in reversed(plan_ready.event_artifacts) if "checkpoint" in ref.media_type
    )
    final_plan_checkpoint = PlanningLoopCheckpoint.model_validate_json(
        plan_artifacts.read_verified(final_plan_checkpoint_ref)
    )
    assert final_plan_checkpoint.reviewer_memory_rounds_used == 2
    assert len(final_plan_checkpoint.reviewer_context_refs) == 2

    inquiry_stall_artifacts, inquiry_stall_request = setup("inquiry-no-progress")
    inquiry_stall_service, _, _ = _post_genesis_service(
        inquiry_stall_artifacts,
        reviewer=_ScriptedReviewer(inquiry_stall_artifacts, [ReviewDecision.REVISE]),
        planner=_NoProgressPlanner(inquiry_stall_artifacts, AgentMode.CHAPTER_SET),
    )
    inquiry_stall = asyncio.run(
        inquiry_stall_service.run(
            request=inquiry_stall_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert inquiry_stall.terminal is PlanningLoopTerminal.INQUIRY_REVIEW_REQUIRED
    assert inquiry_stall.diagnostic_codes == ("INQUIRY_REVISION_NO_PROGRESS",)

    plan_stall_artifacts, plan_stall_request = setup("plan-no-progress")
    plan_stall_service, _, _ = _post_genesis_service(
        plan_stall_artifacts,
        reviewer=_ScriptedReviewer(
            plan_stall_artifacts,
            [ReviewDecision.ACCEPT, ReviewDecision.REVISE],
        ),
        planner=_NoProgressPlanner(plan_stall_artifacts, AgentMode.CHAPTER_SET),
    )
    plan_stall = asyncio.run(
        plan_stall_service.run(
            request=plan_stall_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert plan_stall.terminal is PlanningLoopTerminal.REVIEW_REVISION_REQUIRED
    assert plan_stall.diagnostic_codes == ("PLAN_REVISION_NO_PROGRESS",)


def test_planning_memory_budget_yields_checkpoint_and_incomplete_facets_never_ready(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]

    def setup(name: str) -> tuple[ArtifactRepository, PlanningLoopRequest]:
        artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / name))
        source = _put(artifacts, "author source")
        roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
        return artifacts, _request(
            AgentMode.CHAPTER_SET,
            source,
            accepted=(roots[0], roots[1], roots[2]),
        )

    budget_artifacts, budget_request = setup("reviewer-memory-budget")
    budget_memory = _FixtureMemory(
        budget_artifacts,
        stop_reasons=(
            ControllerStopReason.SUFFICIENT,
            ControllerStopReason.BUDGET_EXHAUSTED,
        ),
    )
    budget_service, budget_planner, _ = _post_genesis_service(
        budget_artifacts,
        reviewer=_ScriptedReviewer(
            budget_artifacts,
            [ReviewDecision.ACCEPT, ReviewDecision.REVISE],
            memory_gap=True,
        ),
        memory=budget_memory,
    )
    yielded = asyncio.run(
        budget_service.run(
            request=budget_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert yielded.terminal is PlanningLoopTerminal.YIELDED
    assert yielded.diagnostic_codes == ("REVIEWER_MEMORY_BUDGET_EXHAUSTED",)
    checkpoint_ref = next(
        ref for ref in reversed(yielded.event_artifacts) if "checkpoint" in ref.media_type
    )
    checkpoint = PlanningLoopCheckpoint.model_validate_json(
        budget_artifacts.read_verified(checkpoint_ref)
    )
    assert checkpoint.execution_ref is not None
    assert checkpoint.plan_review_ref is not None
    assert budget_planner.plan_calls == 1

    facet_artifacts, facet_request = setup("mandatory-facets")
    facet_memory = _FixtureMemory(
        facet_artifacts,
        stop_reason=ControllerStopReason.SUFFICIENT,
        mandatory_facets_total=2,
        mandatory_facets_closed=1,
    )
    facet_service, facet_planner, _ = _post_genesis_service(
        facet_artifacts,
        reviewer=_ScriptedReviewer(facet_artifacts, [ReviewDecision.ACCEPT]),
        memory=facet_memory,
    )
    incomplete = asyncio.run(
        facet_service.run(
            request=facet_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert incomplete.terminal is PlanningLoopTerminal.MEMORY_INSUFFICIENT
    assert incomplete.diagnostic_codes == ("MANDATORY_MEMORY_FACETS_UNRESOLVED",)
    assert facet_planner.plan_calls == 0

    advisory_artifacts, advisory_request = setup("reviewer-memory-advisory")
    advisory_memory = _SupportedThenReviewerUnresolvedMemory(advisory_artifacts)
    # Keep the initial inquiry Memory supported, then exercise the plan-review
    # Memory gap directly.  A turn planner would consume the scripted reviewer
    # decision as a planner-memory review before reaching the reviewer gap.
    advisory_planner = _ModePlanner(advisory_artifacts, AgentMode.CHAPTER_SET)
    advisory_service, _, _ = _post_genesis_service(
        advisory_artifacts,
        reviewer=_ScriptedReviewer(
            advisory_artifacts,
            [ReviewDecision.ACCEPT, ReviewDecision.REVISE],
            memory_gap=True,
        ),
        planner=advisory_planner,
        memory=advisory_memory,
    )
    advisory = asyncio.run(
        advisory_service.run(
            request=advisory_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert advisory.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY, advisory.diagnostic_codes
    assert advisory.diagnostic_codes == ("REVIEWER_MEMORY_UNRESOLVED_ADVISORY",)
    assert advisory.proposal is not None
    assert any("reviewer_memory_gap" in item for item in advisory.proposal.unresolved)
    assert advisory.plan_review_ref is not None
    advisory_review = PlanReview.model_validate_json(
        advisory_artifacts.read_verified(advisory.plan_review_ref),
        strict=True,
    )
    assert advisory_review.decision is ReviewDecision.ACCEPT
    assert advisory_review.memory_gap_questions
    assert all(not issue.blocking for issue in advisory_review.issues)
    assert advisory_review.target_artifact_ref in advisory_review.receipt.input_artifacts

    no_evidence_artifacts, no_evidence_request = setup("reviewer-memory-no-evidence")
    no_evidence_memory = _SupportedThenReviewerUnresolvedMemory(
        no_evidence_artifacts,
        stop_reason=ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
        mandatory_facets_total=1,
        mandatory_facets_closed=1,
    )
    no_evidence_service, _, _ = _post_genesis_service(
        no_evidence_artifacts,
        reviewer=_ScriptedReviewer(
            no_evidence_artifacts,
            [ReviewDecision.ACCEPT, ReviewDecision.REVISE],
            memory_gap=True,
        ),
        planner=_ModePlanner(no_evidence_artifacts, AgentMode.CHAPTER_SET),
        memory=no_evidence_memory,
    )
    no_evidence = asyncio.run(
        no_evidence_service.run(
            request=no_evidence_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert no_evidence.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert no_evidence.diagnostic_codes == ("REVIEWER_MEMORY_UNRESOLVED_ADVISORY",)

    partial_artifacts, partial_request = setup("partial-budget")
    partial_memory = _FixtureMemory(
        partial_artifacts,
        stop_reason=ControllerStopReason.BUDGET_EXHAUSTED,
        mandatory_facets_total=1,
        mandatory_facets_closed=0,
        with_candidate=True,
    )
    partial_service, _, _ = _post_genesis_service(
        partial_artifacts,
        reviewer=_AcceptingReviewer(partial_artifacts),
        memory=partial_memory,
    )
    partial = asyncio.run(
        partial_service.run(
            request=partial_request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert partial.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert partial.memory_context_ref is not None
    partial_context = Stage1ContextPackage.model_validate_json(
        partial_artifacts.read_verified(partial.memory_context_ref),
        strict=False,
    )
    assert any("budget exhausted" in gap for gap in partial_context.unresolved_gaps)
    assert any(trace.candidates for trace in partial_context.retrieval_traces)


def test_evaluation_freezes_manifest_before_blind_export_and_reports_arms(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "evaluation"))
    source = _put(artifacts, "author source")
    raw_accepted = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    accepted = (raw_accepted[0], raw_accepted[1], raw_accepted[2])
    cases: list[PlanningEvaluationCase] = []
    results: dict[tuple[str, str], PlanningLoopResult] = {}
    modes = (
        AgentMode.PROJECT_BOOTSTRAP,
        AgentMode.STORY,
        AgentMode.ARC_VOLUME,
        AgentMode.CHAPTER_SET,
        AgentMode.CHAPTER,
        AgentMode.SCENE,
        AgentMode.REPLAN,
    )
    review_ref = _put(artifacts, "accepted review")
    for mode in modes:
        request = _request(
            mode,
            source,
            accepted=None if mode is AgentMode.PROJECT_BOOTSTRAP else accepted,
        )
        case = PlanningEvaluationCase(
            case_id=StableId(f"case.stage4.{mode.value}"),
            mode=mode,
            request=request,
            corpus_fingerprint=HASH,
            expected_issue_tags=("relation",) if mode is AgentMode.REPLAN else (),
        )
        cases.append(case)
        receipt = _receipt(mode, AgentType.PLANNER)
        proposal = PlanProposal(
            proposal_id=StableId(f"proposal.{mode.value}"),
            project_id=PROJECT,
            mode=mode,
            strategy=request.task.strategy,
            base_commit=request.task.base_commit,
            items=(),
            coverage=1.0,
            receipt=receipt,
        )
        result = PlanningLoopResult(
            request_id=request.request_id,
            terminal=PlanningLoopTerminal.PLAN_CANDIDATE_READY,
            proposal=proposal,
            plan_review_ref=review_ref,
        )
        for arm in (
            PlanningEvaluationArm.CONFIGURED,
            PlanningEvaluationArm.ANCHOR_GRAPH_CONDITIONAL,
        ):
            results[(case.case_id.root, arm.value)] = result
    rubric = PlanningEvaluationRubric(
        rubric_id=StableId("rubric.stage4.formal"),
        schema_version="v1",
        criteria=tuple(
            PlanningEvaluationCriterion(
                metric=metric,
                description=f"Blind score for {metric.value}",
                higher_is_better=not metric.value.endswith("_count"),
            )
            for metric in PlanningEvaluationMetric
        ),
    )
    thresholds = PlanningEvaluationThresholds(
        threshold_id=StableId("thresholds.stage4.formal"),
        schema_version="v1",
        author_intent_coverage_rate_min=0.8,
        accepted_plan_canon_contradiction_count_max=0,
        obligation_arc_hook_continuity_score_min=0.8,
        rolling_hierarchy_consistency_score_min=0.8,
        chapter_feasibility_score_min=0.8,
        alternative_quality_score_min=0.8,
        decision_rationale_score_min=0.8,
        reviewer_issue_recall_min=0.8,
        human_required_rate_max=0.0,
    )
    pilot_path = tmp_path / "pilot.json"
    rubric_path = tmp_path / "rubric.json"
    threshold_path = tmp_path / "thresholds.json"
    pilot_path.write_text('{"pilot":"frozen"}', encoding="utf-8")
    rubric_path.write_text(rubric.model_dump_json(), encoding="utf-8")
    threshold_path.write_text(thresholds.model_dump_json(), encoding="utf-8")
    frozen_gate = load_frozen_planning_evaluation_gate(
        pilot_path=pilot_path,
        rubric_path=rubric_path,
        threshold_path=threshold_path,
        artifacts=artifacts,
        schema_version=VERSION,
    )
    pilot_ref = frozen_gate.pilot_ref
    rubric_ref = frozen_gate.rubric_ref
    threshold_ref = frozen_gate.threshold_ref
    manifest = PlanningEvaluationManifest(
        manifest_id=StableId("manifest.stage4.formal"),
        schema_version="stage4-evaluation.v1",
        cases=tuple(cases),
        configuration_fingerprint=HASH,
        model_fingerprint=HASH,
        corpus_fingerprint=HASH,
        pilot_fingerprint=pilot_ref.artifact_id,
        rubric_fingerprint=rubric_ref.artifact_id,
        threshold_fingerprint=threshold_ref.artifact_id,
        frozen_before_evaluator=True,
    )
    observed_blind: list[ArtifactRef] = []

    def evaluate(ref: ArtifactRef) -> dict[str, JsonValue]:
        observed_blind.append(ref)
        payload = json.loads(artifacts.read_verified(ref))
        per_candidate: dict[str, JsonValue] = {
            cast(str, candidate["candidate_id"]): cast(
                JsonValue,
                {
                    metric: 0 if metric.endswith("_count") else 1.0
                    for metric in REQUIRED_BLIND_REVIEW_METRICS
                },
            )
            for candidate in payload["candidates"]
        }
        return {"candidate_scores": per_candidate}

    report, report_ref = PlanningEvaluationRunner(
        adapter=FakePlanningEvaluationAdapter(
            {identity: _evaluation_observation(result) for identity, result in results.items()}
        ),
        artifacts=artifacts,
        schema_version=SchemaVersion("1.0.0"),
        blind_evaluator=evaluate,
    ).run(
        manifest,
        arms=(
            PlanningEvaluationArm.CONFIGURED,
            PlanningEvaluationArm.ANCHOR_GRAPH_CONDITIONAL,
        ),
    )

    assert len(report.results) == 7
    assert report.evaluation_profile is PlanningEvaluationProfile.DETERMINISTIC_FAKE
    assert not report.gate_eligible
    assert report.semantic_gate_passed is None
    assert observed_blind and artifacts.read_verified(observed_blind[0])
    blind_payload = json.loads(artifacts.read_verified(observed_blind[0]))
    assert len(blind_payload["candidates"]) == 14
    assert all("arm" not in candidate for candidate in blind_payload["candidates"])
    assert artifacts.read_verified(report_ref)
    configured_metrics = report.ablation_metrics["configured"]
    assert isinstance(configured_metrics, dict)
    assert configured_metrics["ready_rate"] == 1.0
    assert configured_metrics["prompt_tokens"] == 70
    assert configured_metrics["used_evidence_count"] == 7
    assert evaluation_identity(manifest).root.startswith("stage4-evaluation.")
    configured_adapter = ConfiguredPlanningEvaluationAdapter(
        lambda case, arm: _evaluation_observation(results[(case.case_id.root, arm.value)])
    )
    assert (
        configured_adapter.run_case(cases[0], PlanningEvaluationArm.CONFIGURED).result.request_id
        == cases[0].request.request_id
    )
    with pytest.raises(ValidationError, match="must have been exposed"):
        PlanningEvaluationObservation.model_validate(
            _evaluation_observation(results[(cases[0].case_id.root, "configured")]).model_dump()
            | {"used_evidence_count": 3}
        )
    configured_observations = {
        (case.case_id.root, PlanningEvaluationArm.CONFIGURED.value): _evaluation_observation(
            results[(case.case_id.root, PlanningEvaluationArm.CONFIGURED.value)]
        )
        for case in cases
    }
    wrong_configuration = dict(configured_observations)
    wrong_configuration[(cases[0].case_id.root, PlanningEvaluationArm.CONFIGURED.value)] = (
        _evaluation_observation(
            results[(cases[0].case_id.root, PlanningEvaluationArm.CONFIGURED.value)],
            configuration_fingerprint=ArtifactId("sha256:" + "b" * 64),
        )
    )
    with pytest.raises(PlanningEvaluationError, match="configuration differs"):
        PlanningEvaluationRunner(
            adapter=FakePlanningEvaluationAdapter(wrong_configuration),
            artifacts=artifacts,
            schema_version=VERSION,
        ).run(manifest, arms=(PlanningEvaluationArm.CONFIGURED,))
    wrong_model = dict(configured_observations)
    wrong_model[(cases[0].case_id.root, PlanningEvaluationArm.CONFIGURED.value)] = (
        _evaluation_observation(
            results[(cases[0].case_id.root, PlanningEvaluationArm.CONFIGURED.value)],
            model_fingerprint=ArtifactId("sha256:" + "b" * 64),
        )
    )
    with pytest.raises(PlanningEvaluationError, match="model differs"):
        PlanningEvaluationRunner(
            adapter=FakePlanningEvaluationAdapter(wrong_model),
            artifacts=artifacts,
            schema_version=VERSION,
        ).run(manifest, arms=(PlanningEvaluationArm.CONFIGURED,))
    with pytest.raises(PlanningEvaluationError, match="blind evaluator"):
        PlanningEvaluationRunner(
            adapter=configured_adapter,
            artifacts=artifacts,
            schema_version=VERSION,
        )
    with pytest.raises(PlanningEvaluationError, match="frozen pilot"):
        PlanningEvaluationRunner(
            adapter=configured_adapter,
            artifacts=artifacts,
            schema_version=VERSION,
            blind_evaluator=evaluate,
        )
    formal_report, _ = PlanningEvaluationRunner(
        adapter=configured_adapter,
        artifacts=artifacts,
        schema_version=VERSION,
        blind_evaluator=evaluate,
        frozen_gate=frozen_gate,
    ).run(
        manifest,
        arms=(
            PlanningEvaluationArm.CONFIGURED,
            PlanningEvaluationArm.ANCHOR_GRAPH_CONDITIONAL,
        ),
    )
    assert formal_report.evaluation_profile is PlanningEvaluationProfile.FORMAL_CONFIGURED
    assert formal_report.gate_eligible
    assert formal_report.semantic_gate_passed
    formal_ablation = formal_report.ablation_metrics["anchor_graph_conditional"]
    assert isinstance(formal_ablation, dict)
    assert "semantic_metrics" in formal_ablation

    def evaluate_failing(ref: ArtifactRef) -> dict[str, JsonValue]:
        output = evaluate(ref)
        raw_scores = output["candidate_scores"]
        assert isinstance(raw_scores, dict)
        for score in raw_scores.values():
            assert isinstance(score, dict)
            score.update(
                {
                    metric: 1 if metric.endswith("_count") else 0.0
                    for metric in REQUIRED_BLIND_REVIEW_METRICS
                }
            )
        return output

    failed_report, _ = PlanningEvaluationRunner(
        adapter=configured_adapter,
        artifacts=artifacts,
        schema_version=VERSION,
        blind_evaluator=evaluate_failing,
        frozen_gate=frozen_gate,
    ).run(manifest, arms=(PlanningEvaluationArm.CONFIGURED,))
    assert failed_report.semantic_gate_passed is False
    leaky_results = dict(results)
    leaky_results[(cases[0].case_id.root, PlanningEvaluationArm.CONFIGURED.value)] = results[
        (cases[0].case_id.root, PlanningEvaluationArm.CONFIGURED.value)
    ].model_copy(update={"diagnostic_codes": ("FUTURE_LEAK", "PROVENANCE_ERROR")})
    leaky_report, _ = PlanningEvaluationRunner(
        adapter=ConfiguredPlanningEvaluationAdapter(
            lambda case, arm: _evaluation_observation(leaky_results[(case.case_id.root, arm.value)])
        ),
        artifacts=artifacts,
        schema_version=VERSION,
        blind_evaluator=evaluate,
        frozen_gate=frozen_gate,
    ).run(manifest, arms=(PlanningEvaluationArm.CONFIGURED,))
    assert leaky_report.semantic_gate_passed is False
    assert leaky_report.leakage_count == leaky_report.provenance_error_count == 1
    human_results = dict(results)
    human_results[(cases[0].case_id.root, PlanningEvaluationArm.CONFIGURED.value)] = (
        PlanningLoopResult(
            request_id=cases[0].request.request_id,
            terminal=PlanningLoopTerminal.HUMAN_REQUIRED,
        )
    )
    human_report, _ = PlanningEvaluationRunner(
        adapter=ConfiguredPlanningEvaluationAdapter(
            lambda case, arm: _evaluation_observation(human_results[(case.case_id.root, arm.value)])
        ),
        artifacts=artifacts,
        schema_version=VERSION,
        blind_evaluator=evaluate,
        frozen_gate=frozen_gate,
    ).run(manifest, arms=(PlanningEvaluationArm.CONFIGURED,))
    assert human_report.semantic_gate_passed is False

    def evaluate_non_numeric(ref: ArtifactRef) -> dict[str, JsonValue]:
        output = evaluate(ref)
        raw_scores = output["candidate_scores"]
        assert isinstance(raw_scores, dict)
        first = next(iter(raw_scores.values()))
        assert isinstance(first, dict)
        first["obligation_arc_hook_continuity_score"] = "invalid"
        return output

    with pytest.raises(PlanningEvaluationError, match="must be numeric"):
        PlanningEvaluationRunner(
            adapter=configured_adapter,
            artifacts=artifacts,
            schema_version=VERSION,
            blind_evaluator=evaluate_non_numeric,
            frozen_gate=frozen_gate,
        ).run(manifest, arms=(PlanningEvaluationArm.CONFIGURED,))
    with pytest.raises(PlanningEvaluationError, match="pilot artifact"):
        PlanningEvaluationRunner(
            adapter=configured_adapter,
            artifacts=artifacts,
            schema_version=VERSION,
            blind_evaluator=evaluate,
            frozen_gate=frozen_gate,
        ).run(
            manifest.model_copy(update={"pilot_fingerprint": HASH}),
            arms=(PlanningEvaluationArm.CONFIGURED,),
        )
    changed_rubric_gate = FrozenPlanningEvaluationGate(
        pilot_ref=pilot_ref,
        rubric_ref=rubric_ref,
        threshold_ref=threshold_ref,
        rubric=rubric.model_copy(update={"rubric_id": StableId("rubric.changed")}),
        thresholds=thresholds,
    )
    with pytest.raises(PlanningEvaluationError, match="rubric content"):
        PlanningEvaluationRunner(
            adapter=configured_adapter,
            artifacts=artifacts,
            schema_version=VERSION,
            blind_evaluator=evaluate,
            frozen_gate=changed_rubric_gate,
        ).run(manifest, arms=(PlanningEvaluationArm.CONFIGURED,))
    changed_threshold_gate = FrozenPlanningEvaluationGate(
        pilot_ref=pilot_ref,
        rubric_ref=rubric_ref,
        threshold_ref=threshold_ref,
        rubric=rubric,
        thresholds=thresholds.model_copy(update={"threshold_id": StableId("thresholds.changed")}),
    )
    with pytest.raises(PlanningEvaluationError, match="thresholds content"):
        PlanningEvaluationRunner(
            adapter=configured_adapter,
            artifacts=artifacts,
            schema_version=VERSION,
            blind_evaluator=evaluate,
            frozen_gate=changed_threshold_gate,
        ).run(manifest, arms=(PlanningEvaluationArm.CONFIGURED,))
    with pytest.raises(PlanningEvaluationError, match="omitted candidate_scores"):
        PlanningEvaluationRunner(
            adapter=configured_adapter,
            artifacts=artifacts,
            schema_version=VERSION,
            blind_evaluator=lambda ref: {"partial": ref.media_type},
            frozen_gate=frozen_gate,
        ).run(manifest, arms=(PlanningEvaluationArm.CONFIGURED,))
    with pytest.raises(PlanningEvaluationError, match="candidate identities"):
        PlanningEvaluationRunner(
            adapter=configured_adapter,
            artifacts=artifacts,
            schema_version=VERSION,
            blind_evaluator=lambda ref: {"candidate_scores": {}, "blind_ref": ref.media_type},
            frozen_gate=frozen_gate,
        ).run(manifest, arms=(PlanningEvaluationArm.CONFIGURED,))

    def evaluate_invalid_score_shape(ref: ArtifactRef) -> dict[str, JsonValue]:
        payload = json.loads(artifacts.read_verified(ref))
        return {
            "candidate_scores": {
                candidate["candidate_id"]: "invalid" for candidate in payload["candidates"]
            }
        }

    with pytest.raises(PlanningEvaluationError, match="score must be an object"):
        PlanningEvaluationRunner(
            adapter=configured_adapter,
            artifacts=artifacts,
            schema_version=VERSION,
            blind_evaluator=evaluate_invalid_score_shape,
            frozen_gate=frozen_gate,
        ).run(manifest, arms=(PlanningEvaluationArm.CONFIGURED,))

    def evaluate_incomplete_score(ref: ArtifactRef) -> dict[str, JsonValue]:
        payload = json.loads(artifacts.read_verified(ref))
        return {
            "candidate_scores": {
                candidate["candidate_id"]: {} for candidate in payload["candidates"]
            }
        }

    with pytest.raises(PlanningEvaluationError, match="rubric metrics differ"):
        PlanningEvaluationRunner(
            adapter=configured_adapter,
            artifacts=artifacts,
            schema_version=VERSION,
            blind_evaluator=evaluate_incomplete_score,
            frozen_gate=frozen_gate,
        ).run(manifest, arms=(PlanningEvaluationArm.CONFIGURED,))
    with pytest.raises(ValidationError, match="only formal configured"):
        type(formal_report).model_validate(formal_report.model_dump() | {"gate_eligible": False})
    with pytest.raises(ValidationError, match="post-freeze blind review"):
        type(formal_report).model_validate(formal_report.model_dump() | {"reviewer_metrics": {}})
    with pytest.raises(ValidationError, match="settle the semantic Gate"):
        type(formal_report).model_validate(
            formal_report.model_dump() | {"semantic_gate_passed": None}
        )
    with pytest.raises(ValueError, match="not predeclared"):
        FakePlanningEvaluationAdapter({}).run_case(cases[0], PlanningEvaluationArm.CONFIGURED)
    replan_case = next(case for case in cases if case.mode is AgentMode.REPLAN)
    results[(replan_case.case_id.root, PlanningEvaluationArm.GRAPH_ONLY.value)] = results[
        (replan_case.case_id.root, PlanningEvaluationArm.CONFIGURED.value)
    ]
    edge_runner = PlanningEvaluationRunner(
        adapter=FakePlanningEvaluationAdapter(
            {identity: _evaluation_observation(result) for identity, result in results.items()}
        ),
        artifacts=artifacts,
        schema_version=VERSION,
    )
    with pytest.raises(ValueError, match="freeze"):
        edge_runner.run(manifest.model_copy(update={"frozen_before_evaluator": False}))
    for arms in ((), (PlanningEvaluationArm.CONFIGURED, PlanningEvaluationArm.CONFIGURED)):
        with pytest.raises(ValueError, match="non-empty and unique"):
            edge_runner.run(manifest, arms=arms)
    with pytest.raises(ValueError, match="cover every"):
        edge_runner.run(manifest, arms=(PlanningEvaluationArm.GRAPH_ONLY,))

    no_graph_cases = tuple(
        case.model_copy(update={"expected_issue_tags": ()}) for case in manifest.cases
    )
    no_graph_manifest = manifest.model_copy(update={"cases": no_graph_cases})
    empty_graph_report, _ = edge_runner.run(
        no_graph_manifest,
        arms=(PlanningEvaluationArm.CONFIGURED, PlanningEvaluationArm.GRAPH_ONLY),
    )
    graph_metrics = empty_graph_report.ablation_metrics["graph_only"]
    assert isinstance(graph_metrics, dict) and graph_metrics["ready_rate"] == 0.0
    empty_formal_report, _ = PlanningEvaluationRunner(
        adapter=configured_adapter,
        artifacts=artifacts,
        schema_version=VERSION,
        blind_evaluator=evaluate,
        frozen_gate=frozen_gate,
    ).run(
        no_graph_manifest,
        arms=(PlanningEvaluationArm.CONFIGURED, PlanningEvaluationArm.GRAPH_ONLY),
    )
    empty_formal_graph = empty_formal_report.ablation_metrics["graph_only"]
    assert isinstance(empty_formal_graph, dict)
    assert empty_formal_graph["semantic_metrics"] == {}

    wrong_results = dict(results)
    wrong_results[(cases[0].case_id.root, PlanningEvaluationArm.CONFIGURED.value)] = results[
        (cases[1].case_id.root, PlanningEvaluationArm.CONFIGURED.value)
    ]
    with pytest.raises(ValueError, match="another request"):
        PlanningEvaluationRunner(
            adapter=FakePlanningEvaluationAdapter(
                {
                    identity: _evaluation_observation(result)
                    for identity, result in wrong_results.items()
                }
            ),
            artifacts=artifacts,
            schema_version=VERSION,
        ).run(manifest, arms=(PlanningEvaluationArm.CONFIGURED,))

    case_path = tmp_path / "case.json"
    case_path.write_text(cases[0].model_dump_json(), encoding="utf-8")
    assert load_planning_evaluation_case(case_path) == cases[0]
    case_path.write_text("{}", encoding="utf-8")
    with pytest.raises(PlanningCaseLoadError, match="invalid"):
        load_planning_evaluation_case(case_path)
    with pytest.raises(PlanningCaseLoadError, match="cannot read"):
        load_planning_evaluation_case(tmp_path / "missing.json")
    with pytest.raises(PlanningGateLoadError, match="cannot load"):
        load_frozen_planning_evaluation_gate(
            pilot_path=tmp_path / "missing-pilot.json",
            rubric_path=rubric_path,
            threshold_path=threshold_path,
            artifacts=artifacts,
            schema_version=VERSION,
        )


def test_planner_memory_turn_yields_or_forbids_bootstrap(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "planner-memory-turn"))
    source = _put(artifacts, "author source")
    roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    base_request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(roots[0], roots[1], roots[2]),
    )
    request = base_request.model_copy(
        update={"budgets": base_request.budgets.model_copy(update={"planner_memory_rounds": 0})}
    )
    service, planner, _ = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT]),
        planner=_TurnPlanner(artifacts, AgentMode.CHAPTER_SET),
    )
    yielded = asyncio.run(
        service.run(
            request=request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert yielded.terminal is PlanningLoopTerminal.YIELDED
    assert yielded.diagnostic_codes == ("PLANNER_MEMORY_SLICE_EXHAUSTED",)
    assert planner.turn_calls == 1

    bootstrap_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "bootstrap-memory"))
    bootstrap_source = _put(bootstrap_artifacts, "author source")
    bootstrap_planner = _TurnPlanner(bootstrap_artifacts, AgentMode.PROJECT_BOOTSTRAP)
    bootstrap_service = PlanningContextLoopService(
        planner=cast(PlannerAgent, bootstrap_planner),
        reviewer=cast(
            PlanReviewerAgent,
            _ScriptedReviewer(bootstrap_artifacts, [ReviewDecision.ACCEPT]),
        ),
        need_generator=PlanningInquiryConditionedNeedGenerator(),
        memory_gateway=cast(MemoryGateway, _ForbiddenMemory()),
        context_assembler=PlannerContextAssembler(bootstrap_artifacts, schema_version=VERSION),
        context_runtime=cast(
            PlannerContextRuntimePort, _FixtureContextRuntime(bootstrap_artifacts)
        ),
        artifacts=bootstrap_artifacts,
        schema_version=VERSION,
    )
    forbidden = asyncio.run(
        bootstrap_service.run(
            request=_request(AgentMode.PROJECT_BOOTSTRAP, bootstrap_source),
            model_request=_model_request,
        )
    )
    assert forbidden.terminal is PlanningLoopTerminal.REVIEW_REQUIRED
    assert forbidden.diagnostic_codes == ("BOOTSTRAP_PLANNER_MEMORY_FORBIDDEN",)
    assert bootstrap_planner.turn_calls == 1


def test_planner_memory_turn_resolves_or_fails_closed_when_slice_allows(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "planner-memory-resolve"))
    source = _put(artifacts, "author source")
    roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(roots[0], roots[1], roots[2]),
    )
    shared_runtime, _ = _shared_context_runtime(tmp_path, artifacts)
    service, planner, _ = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(
            artifacts, [ReviewDecision.ACCEPT, ReviewDecision.ACCEPT, ReviewDecision.ACCEPT]
        ),
        planner=_TurnPlanner(artifacts, AgentMode.CHAPTER_SET),
        runtime=cast(_FixtureContextRuntime, shared_runtime),
    )
    result = asyncio.run(
        service.run(
            request=request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert planner.turn_calls >= 1
    assert result.terminal in {
        PlanningLoopTerminal.YIELDED,
        PlanningLoopTerminal.REVIEW_REQUIRED,
        PlanningLoopTerminal.PLAN_CANDIDATE_READY,
        PlanningLoopTerminal.MEMORY_INSUFFICIENT,
        PlanningLoopTerminal.HUMAN_REQUIRED,
    }


def test_repeated_supported_planner_memory_falls_back_to_plan_review(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]
    artifacts = ArtifactRepository(
        FilesystemObjectStore(tmp_path / "planner-memory-supported-fallback")
    )
    source = _put(artifacts, "author source")
    roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(roots[0], roots[1], roots[2]),
    )
    planner = _TurnPlanner(artifacts, AgentMode.CHAPTER_SET)
    service, _, memory = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT] * 3),
        planner=planner,
    )

    result = asyncio.run(
        service.run(
            request=request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )

    assert result.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert result.proposal is not None
    assert planner.turn_calls == 3
    assert planner.plan_calls == 1
    assert memory.calls == 2


def test_planner_memory_fanout_is_carried_across_retrieval_tranches(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "planner-memory-fanout"))
    source = _put(artifacts, "author source")
    roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(roots[0], roots[1], roots[2]),
    )
    request = request.model_copy(
        update={"budgets": request.budgets.model_copy(update={"planner_memory_rounds": 3})}
    )
    questions = tuple(f"林澈当前的伤势状态是什么 (证据角度 {index})?" for index in range(8))
    planner = _TurnPlanner(artifacts, AgentMode.CHAPTER_SET, questions=questions)
    service, _, memory = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT] * 9),
        planner=planner,
    )

    result = asyncio.run(
        service.run(
            request=request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )

    assert result.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert result.proposal is not None
    assert planner.plan_calls == 1
    assert _planner_memory_question_chunk_size(request) == 3
    request_sizes = [len(getattr(item, "initial_memory_needs", ())) for item in memory.requests]
    planner_request_sizes = request_sizes[1:]
    assert planner_request_sizes
    assert max(planner_request_sizes) <= 3
    assert sum(planner_request_sizes) == 8


def test_supported_planner_memory_request_gets_one_bounded_reprompt(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "planner-memory-reprompt"))
    source = _put(artifacts, "author source")
    roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(roots[0], roots[1], roots[2]),
    )
    planner = _HandledMemoryThenReadyPlanner(artifacts, AgentMode.CHAPTER_SET)
    service, _, _ = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT] * 3),
        planner=planner,
    )

    result = asyncio.run(
        service.run(
            request=request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )

    assert result.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert result.proposal is not None
    assert planner.turn_calls == 3
    assert "PLANNER_MEMORY_STATUS=SUPPORTED" in planner.turn_source_payloads[-1]
    assert (
        "SUPPORTED_MEMORY_QUESTIONS=what is the causal relation with 北塔?"
        in (planner.turn_source_payloads[-1])
    )


def test_seeded_mixed_memory_request_reuses_handled_problem_identity() -> None:
    """Extra model follow-ups must not turn an already-closed seed into no-progress."""

    source = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "a" * 64),
        media_type="text/plain",
        byte_length=1,
        schema_version=VERSION,
    )
    inquiry = _inquiry(AgentMode.CHAPTER_SET, source)
    seed = PlanningProblemIdentitySeed(
        need_id=StableId("need.u8c.seed"),
        question_id=StableId("question.u8c.seed"),
        need_query="林澈当前的伤势状态是什么?",
        semantic_question="预注册: 林澈当前的伤势状态是什么?",
        facet=NeedFacetKind.CURRENT_STATE,
        source_commit=BASE,
        source_text_root=source.artifact_id,
        cutoff_chapter=20,
    )

    assert _requested_planner_memory_question_ids(
        (seed.need_query, "unregistered follow-up?"), inquiry, seed
    ) == (seed.question_id,)


def test_rejected_planner_memory_request_gets_grounding_status_before_plan(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "planner-memory-rejected"))
    source = _put(artifacts, "author source")
    roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(roots[0], roots[1], roots[2]),
    )
    planner = _RejectedMemoryThenReadyPlanner(artifacts, AgentMode.CHAPTER_SET)
    service, _, _ = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT] * 3),
        planner=planner,
    )

    result = asyncio.run(
        service.run(
            request=request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )

    assert result.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert result.proposal is not None
    assert planner.turn_calls == 2
    assert "PLANNER_MEMORY_STATUS=UNEXECUTABLE_REQUESTS" in planner.turn_source_payloads[-1]
    assert "文风和句式参考是什么?" in planner.turn_source_payloads[-1]


def test_unsupported_planner_memory_gets_content_status_without_being_handled(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "planner-memory-unsupported"))
    source = _put(artifacts, "author source")
    roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(roots[0], roots[1], roots[2]),
    )
    planner = _UnsupportedMemoryThenReadyPlanner(artifacts, AgentMode.CHAPTER_SET)
    memory = _SupportThenUnresolvedMemory(
        artifacts,
        stop_reasons=(
            ControllerStopReason.SUFFICIENT,
            ControllerStopReason.MANDATORY_GAP_UNRESOLVED,
        ),
    )
    service, _, _ = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT] * 3),
        planner=planner,
        memory=memory,
    )

    result = asyncio.run(
        service.run(
            request=request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )

    assert result.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert result.proposal is not None
    assert planner.turn_calls == 2
    assert memory.calls == 2
    assert "PLANNER_MEMORY_STATUS=UNSUPPORTED_CONTENT" in planner.turn_source_payloads[-1]
    assert "current_state" in planner.turn_source_payloads[-1]
    assert "final bounded content-recovery turn" in planner.turn_source_payloads[-1]
    assert "do not issue new memory_questions" in planner.turn_source_payloads[-1]


def test_evidence_bound_unsupported_planner_memory_enters_gap_terminal(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "planner-memory-gap"))
    source = _put(artifacts, "author source")
    roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(roots[0], roots[1], roots[2]),
    )
    planner = _UnsupportedMemoryThenReadyPlanner(artifacts, AgentMode.CHAPTER_SET)
    memory = _SupportThenUnresolvedMemory(
        artifacts,
        stop_reasons=(
            ControllerStopReason.SUFFICIENT,
            ControllerStopReason.MANDATORY_GAP_UNRESOLVED,
        ),
        with_candidate=True,
    )
    service, _, _ = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT] * 3),
        planner=planner,
        memory=memory,
    )
    result = asyncio.run(
        service.run(
            request=request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert result.terminal is PlanningLoopTerminal.REVIEW_REQUIRED
    assert result.diagnostic_codes == ("PLANNER_MEMORY_FACETS_UNRESOLVED",)
    assert result.proposal is None
    assert planner.turn_calls == 1
    assert planner.plan_calls == 0
    assert memory.calls == 2


def test_genesis_unsupported_planner_memory_uses_read_only_fallback(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0].model_copy(update={"chapters": ()})
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "planner-memory-genesis"))
    source = _put(artifacts, "author source")
    roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(roots[0], roots[1], roots[2]),
    )
    planner = _UnsupportedMemoryThenReadyPlanner(artifacts, AgentMode.CHAPTER_SET)
    memory = _SupportThenUnresolvedMemory(
        artifacts,
        stop_reasons=(
            ControllerStopReason.SUFFICIENT,
            ControllerStopReason.MANDATORY_GAP_UNRESOLVED,
        ),
        with_candidate=True,
    )
    service, _, _ = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT] * 3),
        planner=planner,
        memory=memory,
    )

    result = asyncio.run(
        service.run(
            request=request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )

    assert result.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert result.proposal is not None
    assert planner.turn_calls == 2
    assert planner.plan_calls == 1
    assert memory.calls == 2
    assert "PLANNER_MEMORY_STATUS=UNSUPPORTED_CONTENT" in planner.turn_source_payloads[-1]


def test_repeated_unsupported_planner_memory_returns_marked_candidate(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]
    artifacts = ArtifactRepository(
        FilesystemObjectStore(tmp_path / "planner-memory-unsupported-repeated")
    )
    source = _put(artifacts, "author source")
    roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(roots[0], roots[1], roots[2]),
    )
    planner = _UnsupportedRepeatsThenReadyPlanner(artifacts, AgentMode.CHAPTER_SET)
    memory = _SupportThenUnresolvedMemory(
        artifacts,
        stop_reasons=(
            ControllerStopReason.SUFFICIENT,
            ControllerStopReason.MANDATORY_GAP_UNRESOLVED,
        ),
    )
    service, _, _ = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT] * 3),
        planner=planner,
        memory=memory,
    )

    result = asyncio.run(
        service.run(
            request=request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )

    assert result.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert result.proposal is not None
    assert result.proposal.unresolved
    assert planner.turn_calls == 2
    assert planner.plan_calls == 1
    assert memory.calls == 2
    assert "PLANNER_MEMORY_FALLBACK" in planner.plan_requests[0]["source_payload"]


def test_unsupported_planner_memory_allows_one_narrower_followup(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "planner-memory-narrower"))
    source = _put(artifacts, "author source")
    roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(roots[0], roots[1], roots[2]),
    )
    request = request.model_copy(
        update={"budgets": request.budgets.model_copy(update={"planner_memory_rounds": 2})}
    )
    planner = _UnsupportedThenNarrowerThenReadyPlanner(artifacts, AgentMode.CHAPTER_SET)
    memory = _UnresolvedThenSupportMemory(
        artifacts,
        stop_reasons=(
            ControllerStopReason.SUFFICIENT,
            ControllerStopReason.MANDATORY_GAP_UNRESOLVED,
            ControllerStopReason.SUFFICIENT,
        ),
    )
    service, _, _ = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(artifacts, [ReviewDecision.ACCEPT] * 4),
        planner=planner,
        memory=memory,
    )
    request_ids: list[str] = []

    def capture_model_request(stage: str, mode: AgentMode, attempt: int) -> ModelRequest:
        del mode
        request_ids.append(f"{stage}.{attempt}")
        return cast(ModelRequest, object())

    result = asyncio.run(
        service.run(
            request=request,
            model_request=capture_model_request,
            world=world,
            text_root=text_root,
        )
    )

    assert result.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert result.proposal is not None
    assert planner.turn_calls == 3
    assert memory.calls == 3
    assert "PLANNER_MEMORY_STATUS=UNSUPPORTED_CONTENT" in planner.turn_source_payloads[1]
    assert "what is the current state of 北塔?" in {
        need.query_text for need in memory.requests[2].initial_memory_needs
    }
    planner_request_ids = [item for item in request_ids if item.startswith("plan_turn")]
    assert len(planner_request_ids) == len(set(planner_request_ids))


def test_token_slice_does_not_abort_plan_turn_after_memory_close(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    text_root = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "token-slice-after-memory"))
    source = _put(artifacts, "author source")
    roots = tuple(_put(artifacts, f"root-{index}") for index in range(3))
    base_request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(roots[0], roots[1], roots[2]),
    )
    request = base_request.model_copy(
        update={"budgets": base_request.budgets.model_copy(update={"model_token_budget": 50})}
    )
    heavy_call = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=80, output_tokens=20, reasoning_tokens=0)
    )
    service, planner, _ = _post_genesis_service(
        artifacts,
        reviewer=_ScriptedReviewer(
            artifacts, [ReviewDecision.ACCEPT, ReviewDecision.ACCEPT, ReviewDecision.ACCEPT]
        ),
        planner=_MemoryThenReadyPlanner(
            artifacts,
            AgentMode.CHAPTER_SET,
            first_turn_usage=heavy_call,
        ),
    )
    result = asyncio.run(
        service.run(
            request=request,
            model_request=_model_request,
            world=world,
            text_root=text_root,
        )
    )
    assert planner.turn_calls == 2
    assert result.proposal is not None
    assert result.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
    assert "MODEL_TOKEN_SLICE_EXHAUSTED" not in result.diagnostic_codes
