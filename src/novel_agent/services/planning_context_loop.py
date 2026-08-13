"""Thin Stage 4 inquiry -> Memory -> Plan -> independent review orchestration."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from novel_agent.agents.plan_reviewer import PlanReviewerAgent, PlanReviewerInvocationError
from novel_agent.agents.planner import PlannerAgent, PlannerInvocationError
from novel_agent.agents.runner import AgentExecutionError
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.memory import Stage1ContextPackage, Stage1MemoryNeed, WorldRootDocument
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.planning import (
    PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
    PlannerContextPackage,
    PlanningInquiry,
    PlanningLoopCheckpoint,
    PlanningLoopEventReceipt,
    PlanningLoopPhase,
    PlanningLoopRequest,
    PlanningLoopResult,
    PlanningLoopTerminal,
    PlanningProvenance,
    PlanningQuestion,
    PlanningQuestionKind,
    PlanningReference,
    PlanningTurnAction,
    PlanReview,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentMode,
    ControllerStopReason,
    MemoryGatewayResult,
    MemoryResolutionRequest,
    PlanProposal,
    RequiredSnapshotPolicy,
)
from novel_agent.ports.model_endpoint import ModelEndpointError
from novel_agent.ports.planning_context import (
    PlannerContextRuntimeFailure,
    PlannerContextRuntimePort,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.memory_gateway import MemoryGateway, MemoryGatewayBlockedError
from novel_agent.services.model_gateway import (
    ModelCallForbiddenError,
    ModelRoutingError,
    StructuredGenerationExhausted,
)
from novel_agent.services.planner_context_assembler import (
    PlannerContextAssembler,
    PlannerContextAssemblyError,
)
from novel_agent.services.planning_inquiry_need_generation import (
    PlanningInquiryConditionedNeedGenerator,
    PlanningInquiryNeedError,
)

ModelRequestFactory = Callable[[str, AgentMode, int], ModelRequest]
ModelT = TypeVar("ModelT", bound=BaseModel)


class PlanningContextLoopService:
    """One bounded product loop; retrieval, projection, and review keep their owners."""

    version = "planning_context_loop.v1"

    def __init__(
        self,
        *,
        planner: PlannerAgent,
        reviewer: PlanReviewerAgent,
        need_generator: PlanningInquiryConditionedNeedGenerator,
        memory_gateway: MemoryGateway,
        context_assembler: PlannerContextAssembler,
        context_runtime: PlannerContextRuntimePort,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion,
    ) -> None:
        self._planner = planner
        self._reviewer = reviewer
        self._needs = need_generator
        self._memory = memory_gateway
        self._assembler = context_assembler
        self._context_runtime = context_runtime
        self._artifacts = artifacts
        self._schema_version = schema_version

    async def run(
        self,
        *,
        request: PlanningLoopRequest,
        model_request: ModelRequestFactory,
        world: WorldRootDocument | None = None,
        text_root: TextRootDocument | None = None,
        resume_checkpoint_ref: ArtifactRef | None = None,
    ) -> PlanningLoopResult:
        event_refs: list[ArtifactRef] = []
        try:
            return await self._run(
                request=request,
                model_request=model_request,
                world=world,
                text_root=text_root,
                resume_checkpoint_ref=resume_checkpoint_ref,
                event_refs=event_refs,
            )
        except PlanReviewerInvocationError:
            return self._terminal(
                request,
                PlanningLoopTerminal.REVIEW_REQUIRED,
                event_refs,
                diagnostics=("REVIEWER_CONTRACT_FAILURE",),
            )
        except (PlannerInvocationError, AgentExecutionError):
            return self._terminal(
                request,
                PlanningLoopTerminal.BLOCKED,
                event_refs,
                diagnostics=("PLANNER_CONTRACT_FAILURE",),
            )
        except (
            ModelRoutingError,
            ModelCallForbiddenError,
            StructuredGenerationExhausted,
            ModelEndpointError,
            TimeoutError,
        ):
            return self._terminal(
                request,
                PlanningLoopTerminal.MODEL_UNAVAILABLE,
                event_refs,
                diagnostics=("MODEL_RUNTIME_UNAVAILABLE",),
            )
        except PlannerContextRuntimeFailure:
            return self._terminal(
                request,
                PlanningLoopTerminal.SUSPENDED,
                event_refs,
                diagnostics=("CONTEXT_RUNTIME_FAILURE",),
            )

    async def _run(
        self,
        *,
        request: PlanningLoopRequest,
        model_request: ModelRequestFactory,
        world: WorldRootDocument | None = None,
        text_root: TextRootDocument | None = None,
        resume_checkpoint_ref: ArtifactRef | None = None,
        event_refs: list[ArtifactRef],
    ) -> PlanningLoopResult:
        source_payload = self._source_payload(request.author_intent_artifacts)
        if request.task.mode is AgentMode.PROJECT_BOOTSTRAP:
            if world is not None or text_root is not None:
                return self._terminal(
                    request,
                    PlanningLoopTerminal.BASIS_CHANGED,
                    event_refs,
                    diagnostics=("BOOTSTRAP_RECEIVED_PROJECT_MEMORY",),
                )
        elif world is None or text_root is None or world.source_commit != request.task.base_commit:
            return self._terminal(
                request,
                PlanningLoopTerminal.BASIS_CHANGED,
                event_refs,
                diagnostics=("POST_GENESIS_BASIS_MISMATCH",),
            )
        event_refs.append(self._event(request, PlanningLoopPhase.PREFLIGHT, "preflight.passed"))

        checkpoint = (
            None
            if resume_checkpoint_ref is None
            else self._read(resume_checkpoint_ref, PlanningLoopCheckpoint)
        )
        reviewer_context_refs = list(() if checkpoint is None else checkpoint.reviewer_context_refs)
        planner_memory_context_refs = list(
            () if checkpoint is None else checkpoint.planner_memory_context_refs
        )
        plan_revisions = 0 if checkpoint is None else checkpoint.plan_revisions_used
        reviewer_memory_rounds = 0 if checkpoint is None else checkpoint.reviewer_memory_rounds_used
        planner_memory_rounds = 0 if checkpoint is None else checkpoint.planner_memory_rounds_used
        handled_memory_reviews = set(
            () if checkpoint is None else checkpoint.reviewer_memory_review_ids
        )
        handled_memory_questions = set(
            () if checkpoint is None else checkpoint.handled_memory_question_ids
        )
        deferred_memory_questions = set(
            () if checkpoint is None else checkpoint.deferred_memory_question_ids
        )
        model_calls_used = 0 if checkpoint is None else checkpoint.model_calls_used
        model_input_tokens_used = (
            0 if checkpoint is None else checkpoint.model_input_tokens_used
        )
        model_output_tokens_used = (
            0 if checkpoint is None else checkpoint.model_output_tokens_used
        )
        model_reasoning_tokens_used = (
            0 if checkpoint is None else checkpoint.model_reasoning_tokens_used
        )
        pending_planner_memory_questions = (
            () if checkpoint is None else checkpoint.pending_planner_memory_questions
        )
        slice_model_tokens_used = 0

        def record_model_call(call: object) -> None:
            nonlocal model_calls_used
            nonlocal model_input_tokens_used
            nonlocal model_output_tokens_used
            nonlocal model_reasoning_tokens_used
            nonlocal slice_model_tokens_used
            usage = getattr(call, "usage", None)
            if usage is None:
                return
            model_calls_used += 1
            model_input_tokens_used += int(usage.input_tokens)
            model_output_tokens_used += int(usage.output_tokens)
            model_reasoning_tokens_used += int(usage.reasoning_tokens)
            slice_model_tokens_used += int(
                usage.input_tokens + usage.output_tokens + usage.reasoning_tokens
            )

        def token_slice_exhausted() -> bool:
            return slice_model_tokens_used >= request.budgets.model_token_budget

        def progress_updates() -> dict[str, object]:
            return {
                "planner_memory_rounds_used": planner_memory_rounds,
                "planner_memory_context_refs": tuple(planner_memory_context_refs),
                "handled_memory_question_ids": self._ordered_ids(handled_memory_questions),
                "deferred_memory_question_ids": self._ordered_ids(deferred_memory_questions),
                "pending_planner_memory_questions": pending_planner_memory_questions,
                "model_calls_used": model_calls_used,
                "model_input_tokens_used": model_input_tokens_used,
                "model_output_tokens_used": model_output_tokens_used,
                "model_reasoning_tokens_used": model_reasoning_tokens_used,
            }
        if checkpoint is not None and (
            checkpoint.request_id != request.request_id
            or checkpoint.base_commit != request.task.base_commit
            or checkpoint.snapshot_id != request.snapshot_id
            or checkpoint.configuration_fingerprint != request.configuration_fingerprint
        ):
            return self._terminal(
                request,
                PlanningLoopTerminal.BASIS_CHANGED,
                event_refs,
                diagnostics=("RESUME_CHECKPOINT_BASIS_MISMATCH",),
            )

        inquiry: PlanningInquiry
        inquiry_ref: ArtifactRef
        inquiry_review: PlanReview
        inquiry_review_ref: ArtifactRef
        inquiry_revisions = 0 if checkpoint is None else checkpoint.inquiry_revisions_used
        inquiry_revisions_this_slice = 0
        if checkpoint is not None and checkpoint.inquiry_ref and checkpoint.inquiry_review_ref:
            inquiry_ref = checkpoint.inquiry_ref
            inquiry_review_ref = checkpoint.inquiry_review_ref
            inquiry = self._read(inquiry_ref, PlanningInquiry)
            inquiry_review = self._read(inquiry_review_ref, PlanReview)
        else:
            inquiry, inquiry_ref, _receipt, _call = await self._planner.propose_inquiry(
                version=self._schema_version,
                task=request.task,
                source_payload=source_payload,
                source_artifacts=request.author_intent_artifacts,
                request=model_request("inquiry", request.task.mode, 1),
                horizon_start=request.horizon_start,
                horizon_end=request.horizon_end,
                explicit_overrides=request.explicit_author_overrides,
            )
            record_model_call(_call)
            inquiry_review, inquiry_review_ref, _call = await self._reviewer.review(
                version=self._schema_version,
                mode=request.task.mode,
                target_kind=ReviewTargetKind.INQUIRY,
                target_payload=inquiry.model_dump_json(),
                target_artifact=inquiry_ref,
                trusted_source_artifacts=request.author_intent_artifacts,
                request=model_request("inquiry_review", request.task.mode, 1),
                base_commit=request.task.base_commit,
            )
            record_model_call(_call)

        while inquiry_review.decision is ReviewDecision.REVISE:
            if request.budgets.inquiry_revisions == 0:
                return self._terminal(
                    request,
                    PlanningLoopTerminal.INQUIRY_REVIEW_REQUIRED,
                    event_refs,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    diagnostics=("INQUIRY_REVISION_DISABLED",),
                )
            if inquiry_revisions_this_slice >= request.budgets.inquiry_revisions:
                event_refs.append(
                    self._checkpoint(
                        request,
                        PlanningLoopPhase.INQUIRY_REVIEWED,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        inquiry_revisions_used=inquiry_revisions,
                        **progress_updates(),
                    )
                )
                return self._terminal(
                    request,
                    PlanningLoopTerminal.YIELDED,
                    event_refs,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    diagnostics=("INQUIRY_REVISION_SLICE_EXHAUSTED",),
                )
            parent_inquiry = inquiry
            instruction = inquiry_review.revision_instruction or "bounded inquiry revision"
            inquiry_revisions += 1
            inquiry_revisions_this_slice += 1
            inquiry_generation = inquiry_revisions + 1
            inquiry, inquiry_ref, _receipt, _call = await self._planner.propose_inquiry(
                version=self._schema_version,
                task=request.task,
                source_payload=(
                    f"{source_payload}\nREVIEW_REVISION={instruction}\n"
                    f"REVIEW={inquiry_review.model_dump_json()}\n"
                    f"PARENT_INQUIRY={parent_inquiry.model_dump_json()}"
                ),
                source_artifacts=request.author_intent_artifacts,
                request=model_request("inquiry_revision", request.task.mode, inquiry_generation),
                horizon_start=request.horizon_start,
                horizon_end=request.horizon_end,
                explicit_overrides=request.explicit_author_overrides,
                parent_inquiry_id=parent_inquiry.inquiry_id,
                generation=inquiry_generation,
            )
            record_model_call(_call)
            if self._same_inquiry_content(parent_inquiry, inquiry):
                return self._terminal(
                    request,
                    PlanningLoopTerminal.INQUIRY_REVIEW_REQUIRED,
                    event_refs,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    diagnostics=("INQUIRY_REVISION_NO_PROGRESS",),
                )
            inquiry_review, inquiry_review_ref, _call = await self._reviewer.review(
                version=self._schema_version,
                mode=request.task.mode,
                target_kind=ReviewTargetKind.INQUIRY,
                target_payload=inquiry.model_dump_json(),
                target_artifact=inquiry_ref,
                trusted_source_artifacts=request.author_intent_artifacts,
                request=model_request("inquiry_rereview", request.task.mode, inquiry_generation),
                base_commit=request.task.base_commit,
            )
            record_model_call(_call)
        if inquiry_review.decision is not ReviewDecision.ACCEPT:
            terminal = (
                PlanningLoopTerminal.HUMAN_REQUIRED
                if inquiry_review.decision is ReviewDecision.HUMAN_REQUIRED
                else PlanningLoopTerminal.INQUIRY_REVIEW_REQUIRED
            )
            return self._terminal(
                request,
                terminal,
                event_refs,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
            )
        event_refs.append(
            self._event(
                request,
                PlanningLoopPhase.INQUIRY_ACCEPTED,
                "inquiry.review_settled",
                (inquiry_ref, inquiry_review_ref),
            )
        )
        event_refs.append(
            self._checkpoint(
                request,
                PlanningLoopPhase.INQUIRY_ACCEPTED,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
                inquiry_revisions_used=inquiry_revisions,
                **progress_updates(),
            )
        )

        stage1_context: Stage1ContextPackage | None = None
        memory_context_ref: ArtifactRef | None = None
        if request.task.mode is not AgentMode.PROJECT_BOOTSTRAP:
            assert world is not None and text_root is not None
            if checkpoint is not None and checkpoint.memory_context_ref is not None:
                memory_context_ref = checkpoint.memory_context_ref
                stage1_context = self._read(memory_context_ref, Stage1ContextPackage)
            else:
                try:
                    need_generation = self._needs.generate(
                        inquiry=inquiry,
                        inquiry_ref=inquiry_ref,
                        review=inquiry_review,
                        review_ref=inquiry_review_ref,
                        world=world,
                        run_id=request.run_id,
                        task_id=request.task_id,
                    )
                except PlanningInquiryNeedError:
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.INQUIRY_INVALID,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                    )
                if not need_generation.needs:
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.MEMORY_INSUFFICIENT,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        diagnostics=("NO_VALID_PLANNER_MEMORY_NEEDS",),
                    )
                handled_memory_questions.update(
                    getattr(need_generation, "selected_question_ids", ())
                )
                deferred_memory_questions.update(
                    getattr(need_generation, "deferred_question_ids", ())
                )
                try:
                    memory_result = self._resolve_memory(
                        request=request,
                        needs=need_generation.needs,
                        text_root=text_root,
                        suffix="inquiry",
                    )
                except MemoryGatewayBlockedError:
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.MEMORY_INSUFFICIENT,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        diagnostics=("MEMORY_GATEWAY_BLOCKED",),
                    )
                stop_reason = memory_result.selected_result.stop_reason
                if stop_reason is ControllerStopReason.BUDGET_EXHAUSTED:
                    event_refs.append(
                        self._checkpoint(
                            request,
                            PlanningLoopPhase.INQUIRY_ACCEPTED,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            inquiry_revisions_used=inquiry_revisions,
                            **progress_updates(),
                        )
                    )
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.YIELDED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        diagnostics=("INQUIRY_MEMORY_BUDGET_EXHAUSTED",),
                    )
                if (
                    memory_result.selected_result.mandatory_need_facets_closed
                    < memory_result.selected_result.mandatory_need_facets_total
                ):
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.MEMORY_INSUFFICIENT,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        diagnostics=("MANDATORY_MEMORY_FACETS_UNRESOLVED",),
                    )
                if stop_reason not in {
                    ControllerStopReason.SUFFICIENT,
                    ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
                }:
                    terminal = (
                        PlanningLoopTerminal.PLAN_CONFLICT
                        if stop_reason is ControllerStopReason.CONFLICT_REQUIRES_REVIEW
                        else PlanningLoopTerminal.MEMORY_INSUFFICIENT
                    )
                    return self._terminal(
                        request,
                        terminal,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        diagnostics=(stop_reason.value,),
                    )
                stage1_context = memory_result.context
                memory_context_ref = memory_result.frozen_context_artifact
                event_refs.append(
                    self._event(
                        request,
                        PlanningLoopPhase.MEMORY_RESOLVED,
                        "memory.resolved",
                        (memory_context_ref,),
                    )
                )

        try:
            if checkpoint is not None and checkpoint.planner_context_ref is not None:
                planner_context_ref = checkpoint.planner_context_ref
                planner_context = self._read_planner_context(planner_context_ref)
                for reviewer_context_ref in reviewer_context_refs:
                    planner_context = self._merge_context_metadata(
                        planner_context,
                        self._read_planner_context(reviewer_context_ref),
                    )
                for planner_memory_context_ref in planner_memory_context_refs:
                    planner_context = self._merge_context_metadata(
                        planner_context,
                        self._read_planner_context(planner_memory_context_ref),
                    )
                projection = self._context_runtime.project(
                    run_id=request.run_id,
                    task_id=request.task_id,
                )
            else:
                planner_context, planner_context_ref = self._assembler.assemble(
                    request=request,
                    inquiry=inquiry,
                    inquiry_ref=inquiry_ref,
                    stage1_context=stage1_context,
                    stage1_context_ref=memory_context_ref,
                )
                projection = self._context_runtime.start(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    seed=planner_context,
                    seed_ref=planner_context_ref,
                )
        except PlannerContextAssemblyError:
            return self._terminal(
                request,
                PlanningLoopTerminal.CONTEXT_LIMIT,
                event_refs,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
                memory_context_ref=memory_context_ref,
            )
        if projection.suspended:
            return self._terminal(
                request,
                PlanningLoopTerminal.SUSPENDED,
                event_refs,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
                memory_context_ref=memory_context_ref,
                planner_context_ref=planner_context_ref,
                diagnostics=(projection.suspension_reason or "CONTEXT_RUNTIME_SUSPENDED",),
            )
        event_refs.append(
            self._event(
                request,
                PlanningLoopPhase.CONTEXT_READY,
                "context.view_ready",
                (planner_context_ref, projection.view_ref),
            )
        )
        event_refs.append(
            self._checkpoint(
                request,
                PlanningLoopPhase.CONTEXT_READY,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
                memory_context_ref=memory_context_ref,
                planner_context_ref=planner_context_ref,
                inquiry_revisions_used=inquiry_revisions,
                plan_revisions_used=plan_revisions,
                reviewer_memory_rounds_used=reviewer_memory_rounds,
                reviewer_memory_review_ids=self._ordered_ids(handled_memory_reviews),
                reviewer_context_refs=tuple(reviewer_context_refs),
                **progress_updates(),
            )
        )

        if token_slice_exhausted():
            return self._terminal(
                request,
                PlanningLoopTerminal.YIELDED,
                event_refs,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
                memory_context_ref=memory_context_ref,
                planner_context_ref=planner_context_ref,
                diagnostics=("MODEL_TOKEN_SLICE_EXHAUSTED",),
            )

        plan_revisions_this_slice = 0
        reviewer_memory_this_slice = 0
        planner_memory_this_slice = 0
        if (
            checkpoint is not None
            and checkpoint.proposal_ref is not None
            and checkpoint.plan_review_ref is not None
            and checkpoint.execution_ref is not None
        ):
            proposal_ref = checkpoint.proposal_ref
            plan_review_ref = checkpoint.plan_review_ref
            execution_ref = checkpoint.execution_ref
            proposal = self._read(proposal_ref, PlanProposal)
            plan_review = self._read(plan_review_ref, PlanReview)
        elif (
            checkpoint is not None
            and checkpoint.proposal_ref is not None
            and checkpoint.execution_ref is not None
        ):
            proposal_ref = checkpoint.proposal_ref
            execution_ref = checkpoint.execution_ref
            proposal = self._read(proposal_ref, PlanProposal)
            plan_review, plan_review_ref, _call = await self._reviewer.review(
                version=self._schema_version,
                mode=request.task.mode,
                target_kind=ReviewTargetKind.PLAN_PROPOSAL,
                target_payload=proposal.model_dump_json(),
                target_artifact=proposal_ref,
                trusted_source_artifacts=(
                    *request.author_intent_artifacts,
                    planner_context_ref,
                    projection.view_ref,
                ),
                request=model_request("plan_review", request.task.mode, 1),
                base_commit=request.task.base_commit,
            )
            record_model_call(_call)
        else:
            if deferred_memory_questions and not pending_planner_memory_questions:
                by_id = {
                    item.question_id: item.question
                    for item in (*inquiry.assumptions, *inquiry.questions)
                }
                pending_planner_memory_questions = tuple(
                    by_id[item]
                    for item in self._ordered_ids(deferred_memory_questions)
                    if item in by_id
                )
            result = None
            while result is None:
                if pending_planner_memory_questions:
                    if (
                        request.budgets.planner_memory_rounds == 0
                        or planner_memory_this_slice >= request.budgets.planner_memory_rounds
                    ):
                        event_refs.append(
                            self._checkpoint(
                                request,
                                PlanningLoopPhase.PLANNER_MEMORY_PENDING,
                                inquiry_ref=inquiry_ref,
                                inquiry_review_ref=inquiry_review_ref,
                                memory_context_ref=memory_context_ref,
                                planner_context_ref=planner_context_ref,
                                inquiry_revisions_used=inquiry_revisions,
                                plan_revisions_used=plan_revisions,
                                reviewer_memory_rounds_used=reviewer_memory_rounds,
                                reviewer_memory_review_ids=self._ordered_ids(
                                    handled_memory_reviews
                                ),
                                reviewer_context_refs=tuple(reviewer_context_refs),
                                **progress_updates(),
                            )
                        )
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.YIELDED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=("PLANNER_MEMORY_SLICE_EXHAUSTED",),
                        )
                    assert world is not None and text_root is not None
                    planner_questions = tuple(
                        PlanningQuestion(
                            question_id=StableId(
                                "planner-memory."
                                + content_id(
                                    {
                                        "inquiry": inquiry.inquiry_id.root,
                                        "question": question,
                                    }
                                ).root[-48:]
                            ),
                            kind=(
                                PlanningQuestionKind.RELATION_CAUSAL
                                if any(
                                    marker in question.casefold()
                                    for marker in ("relation", "causal", "关系", "因果")
                                )
                                else PlanningQuestionKind.FACT
                            ),
                            question=question,
                            provenance=PlanningReference(
                                provenance=PlanningProvenance.PLANNER_PROPOSED
                            ),
                            goal_id=inquiry.goal_proposals[0].goal_id,
                            blocking=True,
                        )
                        for question in pending_planner_memory_questions
                    )
                    if all(
                        item.question_id in handled_memory_questions
                        for item in planner_questions
                    ):
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.REVIEW_REQUIRED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=("PLANNER_MEMORY_NO_PROGRESS",),
                        )
                    planner_inquiry = inquiry.model_copy(
                        update={"assumptions": (), "questions": planner_questions}
                    )
                    planner_inquiry_ref = self._artifacts.put(
                        canonical_json_bytes(planner_inquiry.model_dump(mode="json")),
                        "application/vnd.novel-agent.planning-inquiry+json",
                        self._schema_version,
                    )
                    planner_memory_review, planner_memory_review_ref, _call = (
                        await self._reviewer.review(
                            version=self._schema_version,
                            mode=request.task.mode,
                            target_kind=ReviewTargetKind.INQUIRY,
                            target_payload=planner_inquiry.model_dump_json(),
                            target_artifact=planner_inquiry_ref,
                            trusted_source_artifacts=(
                                *request.author_intent_artifacts,
                                planner_context_ref,
                                projection.view_ref,
                            ),
                            request=model_request(
                                "planner_memory_review",
                                request.task.mode,
                                planner_memory_rounds + 1,
                            ),
                            base_commit=request.task.base_commit,
                        )
                    )
                    record_model_call(_call)
                    if planner_memory_review.decision is not ReviewDecision.ACCEPT:
                        return self._terminal(
                            request,
                            (
                                PlanningLoopTerminal.HUMAN_REQUIRED
                                if planner_memory_review.decision
                                is ReviewDecision.HUMAN_REQUIRED
                                else PlanningLoopTerminal.REVIEW_REQUIRED
                            ),
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=("PLANNER_MEMORY_REVIEW_NOT_ACCEPTED",),
                        )
                    planner_generation = self._needs.generate(
                        inquiry=planner_inquiry,
                        inquiry_ref=planner_inquiry_ref,
                        review=planner_memory_review,
                        review_ref=planner_memory_review_ref,
                        world=world,
                        run_id=request.run_id,
                        task_id=request.task_id,
                        exclude_question_ids=self._ordered_ids(handled_memory_questions),
                    )
                    if not planner_generation.needs:
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.REVIEW_REQUIRED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=("NO_VALID_PLANNER_TURN_MEMORY_NEEDS",),
                        )
                    planner_memory = self._resolve_memory(
                        request=request,
                        needs=planner_generation.needs,
                        text_root=text_root,
                        suffix=f"planner-{planner_memory_rounds + 1}",
                    )
                    if (
                        planner_memory.selected_result.stop_reason
                        is ControllerStopReason.BUDGET_EXHAUSTED
                    ):
                        event_refs.append(
                            self._checkpoint(
                                request,
                                PlanningLoopPhase.PLANNER_MEMORY_PENDING,
                                inquiry_ref=inquiry_ref,
                                inquiry_review_ref=inquiry_review_ref,
                                memory_context_ref=memory_context_ref,
                                planner_context_ref=planner_context_ref,
                                inquiry_revisions_used=inquiry_revisions,
                                **progress_updates(),
                            )
                        )
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.YIELDED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=("PLANNER_MEMORY_BUDGET_EXHAUSTED",),
                        )
                    if (
                        planner_memory.selected_result.mandatory_need_facets_closed
                        < planner_memory.selected_result.mandatory_need_facets_total
                    ):
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.REVIEW_REQUIRED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=planner_memory.frozen_context_artifact,
                            planner_context_ref=planner_context_ref,
                            diagnostics=("PLANNER_MEMORY_FACETS_UNRESOLVED",),
                        )
                    if planner_memory.selected_result.stop_reason not in {
                        ControllerStopReason.SUFFICIENT,
                        ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
                    }:
                        return self._terminal(
                            request,
                            (
                                PlanningLoopTerminal.PLAN_CONFLICT
                                if planner_memory.selected_result.stop_reason
                                is ControllerStopReason.CONFLICT_REQUIRES_REVIEW
                                else PlanningLoopTerminal.REVIEW_REQUIRED
                            ),
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=planner_memory.frozen_context_artifact,
                            planner_context_ref=planner_context_ref,
                            diagnostics=(planner_memory.selected_result.stop_reason.value,),
                        )
                    planner_delta, planner_delta_ref = self._assembler.assemble(
                        request=request,
                        inquiry=planner_inquiry,
                        inquiry_ref=planner_inquiry_ref,
                        stage1_context=planner_memory.context,
                        stage1_context_ref=planner_memory.frozen_context_artifact,
                    )
                    projection = self._context_runtime.append_delta(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        delta_ref=planner_delta_ref,
                    )
                    if projection.suspended:
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.SUSPENDED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=planner_memory.frozen_context_artifact,
                            planner_context_ref=planner_context_ref,
                            diagnostics=(
                                projection.suspension_reason
                                or "PLANNER_MEMORY_CONTEXT_SUSPENDED",
                            ),
                        )
                    planner_context = self._merge_context_metadata(
                        planner_context, planner_delta
                    )
                    planner_memory_context_refs.append(planner_delta_ref)
                    planner_memory_rounds += 1
                    planner_memory_this_slice += 1
                    handled_memory_questions.update(
                        planner_generation.selected_question_ids
                    )
                    deferred_memory_questions = set(
                        planner_generation.deferred_question_ids
                    )
                    deferred_by_id = {
                        item.question_id: item.question for item in planner_questions
                    }
                    pending_planner_memory_questions = tuple(
                        deferred_by_id[item]
                        for item in self._ordered_ids(deferred_memory_questions)
                        if item in deferred_by_id
                    )
                    if pending_planner_memory_questions:
                        continue
                    if token_slice_exhausted():
                        event_refs.append(
                            self._checkpoint(
                                request,
                                PlanningLoopPhase.CONTEXT_READY,
                                inquiry_ref=inquiry_ref,
                                inquiry_review_ref=inquiry_review_ref,
                                memory_context_ref=memory_context_ref,
                                planner_context_ref=planner_context_ref,
                                inquiry_revisions_used=inquiry_revisions,
                                **progress_updates(),
                            )
                        )
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.YIELDED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=("MODEL_TOKEN_SLICE_EXHAUSTED",),
                        )
                run_turn = getattr(self._planner, "run_turn", None)
                if run_turn is None:
                    result, _call = await self._planner.run(
                        version=self._schema_version,
                        task=request.task,
                        source_payload=projection.rendered_context,
                        source_artifacts=request.author_intent_artifacts,
                        trusted_context_artifacts=(planner_context_ref, projection.view_ref),
                        reviewed_inquiry_ref=inquiry_ref,
                        memory_need_ids=planner_context.need_ids,
                        evidence_refs=planner_context.evidence_refs,
                        graph_path_receipt_refs=planner_context.graph_path_receipt_refs,
                        request=model_request("plan", request.task.mode, 1),
                    )
                    break
                turn, result, _call = await run_turn(
                    version=self._schema_version,
                    task=request.task,
                    source_payload=projection.rendered_context,
                    source_artifacts=request.author_intent_artifacts,
                    trusted_context_artifacts=(
                        planner_context_ref,
                        projection.view_ref,
                        *planner_memory_context_refs,
                    ),
                    reviewed_inquiry_ref=inquiry_ref,
                    memory_need_ids=planner_context.need_ids,
                    evidence_refs=planner_context.evidence_refs,
                    graph_path_receipt_refs=planner_context.graph_path_receipt_refs,
                    request=model_request(
                        "plan_turn", request.task.mode, planner_memory_rounds + 1
                    ),
                )
                record_model_call(_call)
                if turn.action is PlanningTurnAction.REQUEST_MEMORY:
                    if request.task.mode is AgentMode.PROJECT_BOOTSTRAP:
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.REVIEW_REQUIRED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=("BOOTSTRAP_PLANNER_MEMORY_FORBIDDEN",),
                        )
                    pending_planner_memory_questions = turn.memory_questions
                    continue
                assert result is not None
            proposal = result.plan_proposal
            proposal_ref = self._persist_proposal(proposal)
            execution_ref = self._artifacts.put(
                canonical_json_bytes(result.model_dump(mode="json")),
                "application/vnd.novel-agent.planner-execution-result+json",
                self._schema_version,
            )
            if token_slice_exhausted():
                event_refs.append(
                    self._checkpoint(
                        request,
                        PlanningLoopPhase.PLAN_PROPOSED,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal_ref=proposal_ref,
                        execution_ref=execution_ref,
                        inquiry_revisions_used=inquiry_revisions,
                        plan_revisions_used=plan_revisions,
                        reviewer_memory_rounds_used=reviewer_memory_rounds,
                        reviewer_memory_review_ids=self._ordered_ids(handled_memory_reviews),
                        reviewer_context_refs=tuple(reviewer_context_refs),
                        **progress_updates(),
                    )
                )
                return self._terminal(
                    request,
                    PlanningLoopTerminal.YIELDED,
                    event_refs,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    memory_context_ref=memory_context_ref,
                    planner_context_ref=planner_context_ref,
                    proposal=proposal,
                    diagnostics=("MODEL_TOKEN_SLICE_EXHAUSTED",),
                )
            plan_review, plan_review_ref, _call = await self._reviewer.review(
                version=self._schema_version,
                mode=request.task.mode,
                target_kind=ReviewTargetKind.PLAN_PROPOSAL,
                target_payload=proposal.model_dump_json(),
                target_artifact=proposal_ref,
                trusted_source_artifacts=(
                    *request.author_intent_artifacts,
                    planner_context_ref,
                    projection.view_ref,
                ),
                request=model_request("plan_review", request.task.mode, 1),
                base_commit=request.task.base_commit,
            )
            record_model_call(_call)

        while plan_review.decision is ReviewDecision.REVISE:
            if request.budgets.plan_revisions == 0:
                return self._terminal(
                    request,
                    PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                    event_refs,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    memory_context_ref=memory_context_ref,
                    planner_context_ref=planner_context_ref,
                    proposal=proposal,
                    plan_review_ref=plan_review_ref,
                    diagnostics=("PLAN_REVISION_DISABLED",),
                )
            if plan_revisions_this_slice >= request.budgets.plan_revisions:
                event_refs.append(
                    self._checkpoint(
                        request,
                        PlanningLoopPhase.PLAN_REVIEWED,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal_ref=proposal_ref,
                        plan_review_ref=plan_review_ref,
                        execution_ref=execution_ref,
                        inquiry_revisions_used=inquiry_revisions,
                        plan_revisions_used=plan_revisions,
                        reviewer_memory_rounds_used=reviewer_memory_rounds,
                        reviewer_memory_review_ids=self._ordered_ids(handled_memory_reviews),
                        reviewer_context_refs=tuple(reviewer_context_refs),
                        **progress_updates(),
                    )
                )
                return self._terminal(
                    request,
                    PlanningLoopTerminal.YIELDED,
                    event_refs,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    memory_context_ref=memory_context_ref,
                    planner_context_ref=planner_context_ref,
                    proposal=proposal,
                    plan_review_ref=plan_review_ref,
                    diagnostics=("PLAN_REVISION_SLICE_EXHAUSTED",),
                )

            if (
                plan_review.memory_gap_questions
                and plan_review.review_id not in handled_memory_reviews
                and request.task.mode is not AgentMode.PROJECT_BOOTSTRAP
            ):
                if request.budgets.reviewer_memory_rounds == 0:
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=("REVIEWER_MEMORY_DISABLED",),
                    )
                if reviewer_memory_this_slice >= request.budgets.reviewer_memory_rounds:
                    event_refs.append(
                        self._checkpoint(
                            request,
                            PlanningLoopPhase.PLAN_REVIEWED,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            proposal_ref=proposal_ref,
                            plan_review_ref=plan_review_ref,
                            execution_ref=execution_ref,
                            inquiry_revisions_used=inquiry_revisions,
                            plan_revisions_used=plan_revisions,
                            reviewer_memory_rounds_used=reviewer_memory_rounds,
                            reviewer_memory_review_ids=self._ordered_ids(handled_memory_reviews),
                            reviewer_context_refs=tuple(reviewer_context_refs),
                            **progress_updates(),
                        )
                    )
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.YIELDED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=("REVIEWER_MEMORY_SLICE_EXHAUSTED",),
                    )
                assert world is not None and text_root is not None
                gap_questions = tuple(
                    PlanningQuestion(
                        question_id=StableId(
                            f"reviewer-gap.{index}.{plan_review.review_id.root}"[:128]
                        ),
                        kind=(
                            PlanningQuestionKind.RELATION_CAUSAL
                            if any(
                                marker in question.casefold()
                                for marker in ("relation", "causal", "关系", "因果")
                            )
                            else PlanningQuestionKind.FACT
                        ),
                        question=question,
                        provenance=PlanningReference(
                            provenance=PlanningProvenance.REVIEWER_DERIVED,
                            artifact_refs=(plan_review_ref,),
                        ),
                        goal_id=inquiry.goal_proposals[0].goal_id,
                        blocking=True,
                    )
                    for index, question in enumerate(plan_review.memory_gap_questions)
                )
                reviewer_inquiry = inquiry.model_copy(
                    update={"questions": (*inquiry.questions, *gap_questions)}
                )
                try:
                    gap_generation = self._needs.generate(
                        inquiry=reviewer_inquiry,
                        inquiry_ref=inquiry_ref,
                        review=plan_review,
                        review_ref=plan_review_ref,
                        world=world,
                        run_id=request.run_id,
                        task_id=request.task_id,
                        reviewer_bound=True,
                    )
                    if not gap_generation.needs:
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            proposal=proposal,
                            plan_review_ref=plan_review_ref,
                            diagnostics=("NO_VALID_REVIEWER_MEMORY_NEEDS",),
                        )
                    gap_memory = self._resolve_memory(
                        request=request,
                        needs=gap_generation.needs,
                        text_root=text_root,
                        suffix=f"reviewer-{reviewer_memory_rounds + 1}",
                    )
                except (PlanningInquiryNeedError, MemoryGatewayBlockedError):
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=("REVIEWER_MEMORY_BLOCKED",),
                    )
                stop_reason = gap_memory.selected_result.stop_reason
                if stop_reason is ControllerStopReason.BUDGET_EXHAUSTED:
                    event_refs.append(
                        self._checkpoint(
                            request,
                            PlanningLoopPhase.PLAN_REVIEWED,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            proposal_ref=proposal_ref,
                            plan_review_ref=plan_review_ref,
                            execution_ref=execution_ref,
                            inquiry_revisions_used=inquiry_revisions,
                            plan_revisions_used=plan_revisions,
                            reviewer_memory_rounds_used=reviewer_memory_rounds,
                            reviewer_memory_review_ids=self._ordered_ids(handled_memory_reviews),
                            reviewer_context_refs=tuple(reviewer_context_refs),
                            **progress_updates(),
                        )
                    )
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.YIELDED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=("REVIEWER_MEMORY_BUDGET_EXHAUSTED",),
                    )
                if (
                    gap_memory.selected_result.mandatory_need_facets_closed
                    < gap_memory.selected_result.mandatory_need_facets_total
                ):
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=("REVIEWER_MANDATORY_MEMORY_FACETS_UNRESOLVED",),
                    )
                if stop_reason not in {
                    ControllerStopReason.SUFFICIENT,
                    ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
                }:
                    terminal = (
                        PlanningLoopTerminal.PLAN_CONFLICT
                        if stop_reason is ControllerStopReason.CONFLICT_REQUIRES_REVIEW
                        else PlanningLoopTerminal.REVIEW_REVISION_REQUIRED
                    )
                    return self._terminal(
                        request,
                        terminal,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=gap_memory.frozen_context_artifact,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=(stop_reason.value,),
                    )
                try:
                    gap_context, gap_context_ref = self._assembler.assemble(
                        request=request,
                        inquiry=reviewer_inquiry,
                        inquiry_ref=inquiry_ref,
                        stage1_context=gap_memory.context,
                        stage1_context_ref=gap_memory.frozen_context_artifact,
                    )
                    projection = self._context_runtime.append_delta(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        delta_ref=gap_context_ref,
                    )
                except (PlannerContextAssemblyError, PlannerContextRuntimeFailure):
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.SUSPENDED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=gap_memory.frozen_context_artifact,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=("REVIEWER_CONTEXT_SUSPENDED",),
                    )
                if projection.suspended:
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.SUSPENDED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=gap_memory.frozen_context_artifact,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=(projection.suspension_reason or "REVIEWER_CONTEXT_SUSPENDED",),
                    )
                planner_context = self._merge_context_metadata(planner_context, gap_context)
                reviewer_memory_rounds += 1
                reviewer_memory_this_slice += 1
                handled_memory_reviews.add(plan_review.review_id)
                reviewer_context_refs.append(gap_context_ref)

            parent_proposal = proposal
            instruction = plan_review.revision_instruction or "bounded Plan revision"
            plan_revisions += 1
            plan_revisions_this_slice += 1
            attempt = plan_revisions + 1
            revised, _call = await self._planner.run(
                version=self._schema_version,
                task=request.task,
                source_payload=(
                    f"{projection.rendered_context}\nREVIEW_REVISION={instruction}\n"
                    f"REVIEW={plan_review.model_dump_json()}\n"
                    f"PARENT_PROPOSAL={parent_proposal.model_dump_json()}"
                ),
                source_artifacts=request.author_intent_artifacts,
                trusted_context_artifacts=(
                    planner_context_ref,
                    projection.view_ref,
                    plan_review_ref,
                    *reviewer_context_refs,
                ),
                reviewed_inquiry_ref=inquiry_ref,
                memory_need_ids=planner_context.need_ids,
                evidence_refs=planner_context.evidence_refs,
                graph_path_receipt_refs=planner_context.graph_path_receipt_refs,
                parent_proposal_id=parent_proposal.proposal_id,
                request=model_request("plan_revision", request.task.mode, attempt),
            )
            record_model_call(_call)
            proposal = revised.plan_proposal
            proposal_ref = self._persist_proposal(proposal)
            execution_ref = self._artifacts.put(
                canonical_json_bytes(revised.model_dump(mode="json")),
                "application/vnd.novel-agent.planner-execution-result+json",
                self._schema_version,
            )
            if self._same_proposal_content(parent_proposal, proposal):
                return self._terminal(
                    request,
                    PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                    event_refs,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    memory_context_ref=memory_context_ref,
                    planner_context_ref=planner_context_ref,
                    proposal=proposal,
                    plan_review_ref=plan_review_ref,
                    diagnostics=("PLAN_REVISION_NO_PROGRESS",),
                )
            plan_review, plan_review_ref, _call = await self._reviewer.review(
                version=self._schema_version,
                mode=request.task.mode,
                target_kind=ReviewTargetKind.PLAN_PROPOSAL,
                target_payload=proposal.model_dump_json(),
                target_artifact=proposal_ref,
                trusted_source_artifacts=(
                    *request.author_intent_artifacts,
                    planner_context_ref,
                    projection.view_ref,
                ),
                request=model_request("plan_rereview", request.task.mode, attempt),
                base_commit=request.task.base_commit,
            )
            record_model_call(_call)
        event_refs.append(
            self._event(
                request,
                PlanningLoopPhase.PLAN_REVIEWED,
                "plan.review_settled",
                (proposal_ref, plan_review_ref, execution_ref),
            )
        )
        event_refs.append(
            self._checkpoint(
                request,
                PlanningLoopPhase.PLAN_REVIEWED,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
                memory_context_ref=memory_context_ref,
                planner_context_ref=planner_context_ref,
                proposal_ref=proposal_ref,
                plan_review_ref=plan_review_ref,
                execution_ref=execution_ref,
                inquiry_revisions_used=inquiry_revisions,
                plan_revisions_used=plan_revisions,
                reviewer_memory_rounds_used=reviewer_memory_rounds,
                reviewer_memory_review_ids=self._ordered_ids(handled_memory_reviews),
                reviewer_context_refs=tuple(reviewer_context_refs),
                **progress_updates(),
            )
        )
        if plan_review.decision is ReviewDecision.HUMAN_REQUIRED:
            terminal = PlanningLoopTerminal.HUMAN_REQUIRED
        elif plan_review.decision is not ReviewDecision.ACCEPT:
            terminal = PlanningLoopTerminal.REVIEW_REVISION_REQUIRED
        elif proposal.unresolved:
            terminal = PlanningLoopTerminal.DEGRADED_NOT_PROMOTABLE
        else:
            terminal = PlanningLoopTerminal.PLAN_CANDIDATE_READY
        return self._terminal(
            request,
            terminal,
            event_refs,
            inquiry_ref=inquiry_ref,
            inquiry_review_ref=inquiry_review_ref,
            memory_context_ref=memory_context_ref,
            planner_context_ref=planner_context_ref,
            proposal=proposal,
            plan_review_ref=plan_review_ref,
        )

    def _resolve_memory(
        self,
        *,
        request: PlanningLoopRequest,
        needs: tuple[Stage1MemoryNeed, ...],
        text_root: TextRootDocument,
        suffix: str,
    ) -> MemoryGatewayResult:
        typed_needs = needs
        assert request.task.base_commit is not None and request.snapshot_id is not None
        identity = content_id(
            {
                "request": request.request_id.root,
                "suffix": suffix,
                "needs": tuple(item.need_id.root for item in typed_needs),
            }
        ).root.removeprefix("sha256:")[:24]
        memory_request = MemoryResolutionRequest(
            request_id=StableId(f"memory-resolution.stage4.{identity}"),
            run_id=request.run_id,
            task_id=request.task_id,
            project_id=request.project_id,
            base_commit=request.task.base_commit,
            snapshot_id=request.snapshot_id,
            required_snapshot_policy=RequiredSnapshotPolicy.EXACT,
            task_contract=f"stage4:{request.task.mode.value}:{suffix}",
            initial_memory_needs=typed_needs,
            worldline="main",
            narrative_chapter=request.horizon_end or request.horizon_start or 0,
            access_scope=AccessScope.AUTHOR_PLANNING,
            allow_future_plan=any(item.retrieval_may_return_plan for item in typed_needs),
            retrieval_budget=request.budgets.retrieval,
            context_budget=request.budgets.context,
        )
        return self._memory.resolve(
            memory_request,
            text_root,
            thread_id=request.run_id.root,
        )

    def _persist_proposal(self, proposal: PlanProposal) -> ArtifactRef:
        return self._artifacts.put(
            canonical_json_bytes(proposal.model_dump(mode="json")),
            "application/vnd.novel-agent.plan-proposal+json",
            self._schema_version,
        )

    def _source_payload(self, artifacts: tuple[ArtifactRef, ...]) -> str:
        parts: list[str] = []
        for artifact in artifacts:
            try:
                parts.append(self._artifacts.read_verified(artifact).decode("utf-8"))
            except UnicodeDecodeError as error:
                raise ValueError("Planner author source is not UTF-8") from error
        return "\n\n".join(parts)

    @staticmethod
    def _same_inquiry_content(left: PlanningInquiry, right: PlanningInquiry) -> bool:
        """Detect a reviewer loop that changed lineage but made no semantic progress."""

        def semantic(inquiry: PlanningInquiry) -> dict[str, object]:
            goal_positions = {
                goal.goal_id: position for position, goal in enumerate(inquiry.goal_proposals)
            }

            def question_semantic(question: PlanningQuestion) -> dict[str, object]:
                return {
                    "question": question.model_dump(
                        mode="json", exclude={"question_id", "goal_id"}
                    ),
                    "goal_position": goal_positions.get(question.goal_id),
                }

            return {
                "inquiry": inquiry.model_dump(
                    mode="json",
                    exclude={
                        "inquiry_id",
                        "parent_inquiry_id",
                        "generation",
                        "goal_proposals",
                        "assumptions",
                        "questions",
                    },
                ),
                "goals": tuple(
                    goal.model_dump(mode="json", exclude={"goal_id"})
                    for goal in inquiry.goal_proposals
                ),
                "assumptions": tuple(
                    question_semantic(question) for question in inquiry.assumptions
                ),
                "questions": tuple(question_semantic(question) for question in inquiry.questions),
            }

        return semantic(left) == semantic(right)

    @staticmethod
    def _same_proposal_content(left: PlanProposal, right: PlanProposal) -> bool:
        """Stop repeated review cycles when only execution lineage changed."""

        def semantic(proposal: PlanProposal) -> dict[str, object]:
            return {
                "proposal": proposal.model_dump(
                    mode="json",
                    exclude={
                        "proposal_id",
                        "receipt",
                        "parent_proposal_id",
                        "reviewer_receipt_ref",
                        "items",
                    },
                ),
                "items": tuple(
                    item.model_dump(mode="json", exclude={"item_id"}) for item in proposal.items
                ),
            }

        return semantic(left) == semantic(right)

    @staticmethod
    def _ordered_ids(values: set[StableId]) -> tuple[StableId, ...]:
        return tuple(sorted(values, key=lambda item: item.root))

    @staticmethod
    def _merge_context_metadata(
        current: PlannerContextPackage,
        added: PlannerContextPackage,
    ) -> PlannerContextPackage:
        return current.model_copy(
            update={
                "need_ids": tuple(dict.fromkeys((*current.need_ids, *added.need_ids))),
                "retrieval_unit_ids": tuple(
                    dict.fromkeys((*current.retrieval_unit_ids, *added.retrieval_unit_ids))
                ),
                "evidence_refs": tuple(
                    dict.fromkeys((*current.evidence_refs, *added.evidence_refs))
                ),
                "graph_path_receipt_refs": tuple(
                    dict.fromkeys(
                        (*current.graph_path_receipt_refs, *added.graph_path_receipt_refs)
                    )
                ),
                "expansion_receipt_refs": tuple(
                    dict.fromkeys((*current.expansion_receipt_refs, *added.expansion_receipt_refs))
                ),
            }
        )

    def _event(
        self,
        request: PlanningLoopRequest,
        phase: PlanningLoopPhase,
        event_kind: str,
        refs: tuple[ArtifactRef, ...] = (),
    ) -> ArtifactRef:
        identity = content_id(
            {
                "request": request.request_id.root,
                "phase": phase.value,
                "kind": event_kind,
                "refs": tuple(item.artifact_id.root for item in refs),
            }
        ).root.removeprefix("sha256:")[:24]
        event = PlanningLoopEventReceipt(
            event_id=StableId(f"planning-event.{identity}"),
            request_id=request.request_id,
            phase=phase,
            event_kind=event_kind,
            artifact_refs=refs,
        )
        return self._artifacts.put(
            canonical_json_bytes(event.model_dump(mode="json")),
            "application/vnd.novel-agent.planning-loop-event+json",
            self._schema_version,
        )

    def _checkpoint(
        self,
        request: PlanningLoopRequest,
        phase: PlanningLoopPhase,
        **updates: object,
    ) -> ArtifactRef:
        draft = PlanningLoopCheckpoint.model_validate(
            {
                "checkpoint_id": StableId("planning-checkpoint.pending"),
                "request_id": request.request_id,
                "phase": phase,
                "base_commit": request.task.base_commit,
                "snapshot_id": request.snapshot_id,
                "configuration_fingerprint": request.configuration_fingerprint,
                **updates,
            }
        )
        checkpoint_id = content_id(
            draft.model_dump(mode="json", exclude={"checkpoint_id"})
        ).root.removeprefix("sha256:")[:24]
        checkpoint = draft.model_copy(
            update={"checkpoint_id": StableId(f"planning-checkpoint.{checkpoint_id}")}
        )
        return self._artifacts.put(
            canonical_json_bytes(checkpoint.model_dump(mode="json")),
            PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
            self._schema_version,
        )

    def _terminal(
        self,
        request: PlanningLoopRequest,
        terminal: PlanningLoopTerminal,
        event_refs: list[ArtifactRef],
        *,
        inquiry_ref: ArtifactRef | None = None,
        inquiry_review_ref: ArtifactRef | None = None,
        memory_context_ref: ArtifactRef | None = None,
        planner_context_ref: ArtifactRef | None = None,
        proposal: PlanProposal | None = None,
        plan_review_ref: ArtifactRef | None = None,
        diagnostics: tuple[str, ...] = (),
    ) -> PlanningLoopResult:
        event_refs.append(
            self._event(
                request,
                PlanningLoopPhase.TERMINAL,
                f"terminal.{terminal.value}",
                tuple(
                    item
                    for item in (
                        inquiry_ref,
                        inquiry_review_ref,
                        memory_context_ref,
                        planner_context_ref,
                        plan_review_ref,
                    )
                    if item is not None
                ),
            )
        )
        return PlanningLoopResult(
            request_id=request.request_id,
            terminal=terminal,
            inquiry_ref=inquiry_ref,
            inquiry_review_ref=inquiry_review_ref,
            memory_context_ref=memory_context_ref,
            planner_context_ref=planner_context_ref,
            proposal=proposal,
            plan_review_ref=plan_review_ref,
            event_artifacts=tuple(event_refs),
            diagnostic_codes=diagnostics,
            degraded=terminal is PlanningLoopTerminal.DEGRADED_NOT_PROMOTABLE,
        )

    def _read(self, artifact: ArtifactRef, model: type[ModelT]) -> ModelT:
        raw = self._artifacts.read_verified(artifact)
        return model.model_validate_json(raw)

    def _read_planner_context(self, artifact: ArtifactRef) -> PlannerContextPackage:
        return self._read(artifact, PlannerContextPackage)
