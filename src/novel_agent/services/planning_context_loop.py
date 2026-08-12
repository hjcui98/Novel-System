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
            if inquiry_review.decision is ReviewDecision.REVISE:
                if request.budgets.inquiry_revisions < 1:
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.INQUIRY_REVIEW_REQUIRED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                    )
                inquiry_revisions = 1
                instruction = inquiry_review.revision_instruction or "bounded inquiry revision"
                inquiry, inquiry_ref, _receipt, _call = await self._planner.propose_inquiry(
                    version=self._schema_version,
                    task=request.task,
                    source_payload=f"{source_payload}\nREVIEW_REVISION={instruction}",
                    source_artifacts=request.author_intent_artifacts,
                    request=model_request("inquiry_revision", request.task.mode, 2),
                    horizon_start=request.horizon_start,
                    horizon_end=request.horizon_end,
                    explicit_overrides=request.explicit_author_overrides,
                    parent_inquiry_id=inquiry.inquiry_id,
                )
                inquiry_review, inquiry_review_ref, _call = await self._reviewer.review(
                    version=self._schema_version,
                    mode=request.task.mode,
                    target_kind=ReviewTargetKind.INQUIRY,
                    target_payload=inquiry.model_dump_json(),
                    target_artifact=inquiry_ref,
                    trusted_source_artifacts=request.author_intent_artifacts,
                    request=model_request("inquiry_rereview", request.task.mode, 2),
                    base_commit=request.task.base_commit,
                )
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
                    generation = self._needs.generate(
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
                if not generation.needs:
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.MEMORY_INSUFFICIENT,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        diagnostics=("NO_VALID_PLANNER_MEMORY_NEEDS",),
                    )
                try:
                    memory_result = self._resolve_memory(
                        request=request,
                        needs=generation.needs,
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
                if memory_result.selected_result.stop_reason not in {
                    ControllerStopReason.SUFFICIENT,
                    ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
                }:
                    terminal = (
                        PlanningLoopTerminal.PLAN_CONFLICT
                        if memory_result.selected_result.stop_reason
                        is ControllerStopReason.CONFLICT_REQUIRES_REVIEW
                        else PlanningLoopTerminal.MEMORY_INSUFFICIENT
                    )
                    return self._terminal(
                        request,
                        terminal,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        diagnostics=(memory_result.selected_result.stop_reason.value,),
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
            )
        )

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
        proposal = result.plan_proposal
        proposal_ref = self._persist_proposal(proposal)
        execution_ref = self._artifacts.put(
            canonical_json_bytes(result.model_dump(mode="json")),
            "application/vnd.novel-agent.planner-execution-result+json",
            self._schema_version,
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
        plan_revisions = 0
        reviewer_memory_rounds = 0
        if (
            plan_review.decision is ReviewDecision.REVISE
            and plan_review.memory_gap_questions
            and request.budgets.reviewer_memory_rounds > 0
            and request.task.mode is not AgentMode.PROJECT_BOOTSTRAP
        ):
            assert world is not None and text_root is not None
            reviewer_memory_rounds = 1
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
            if gap_generation.needs:
                gap_memory = self._resolve_memory(
                    request=request,
                    needs=gap_generation.needs,
                    text_root=text_root,
                    suffix="reviewer",
                )
                gap_context, gap_context_ref = self._assembler.assemble(
                    request=request,
                    inquiry=reviewer_inquiry,
                    inquiry_ref=inquiry_ref,
                    stage1_context=gap_memory.context,
                    stage1_context_ref=gap_memory.frozen_context_artifact,
                )
                del gap_context
                projection = self._context_runtime.append_delta(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    delta_ref=gap_context_ref,
                )

        if plan_review.decision is ReviewDecision.REVISE:
            if request.budgets.plan_revisions < 1:
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
                )
            plan_revisions = 1
            instruction = plan_review.revision_instruction or "bounded Plan revision"
            revised, _call = await self._planner.run(
                version=self._schema_version,
                task=request.task,
                source_payload=(
                    f"{projection.rendered_context}\nREVIEW_REVISION={instruction}\n"
                    f"PARENT_PROPOSAL={proposal.model_dump_json()}"
                ),
                source_artifacts=request.author_intent_artifacts,
                trusted_context_artifacts=(
                    planner_context_ref,
                    projection.view_ref,
                    plan_review_ref,
                ),
                reviewed_inquiry_ref=inquiry_ref,
                memory_need_ids=planner_context.need_ids,
                evidence_refs=planner_context.evidence_refs,
                graph_path_receipt_refs=planner_context.graph_path_receipt_refs,
                parent_proposal_id=proposal.proposal_id,
                request=model_request("plan_revision", request.task.mode, 2),
            )
            proposal = revised.plan_proposal
            proposal_ref = self._persist_proposal(proposal)
            execution_ref = self._artifacts.put(
                canonical_json_bytes(revised.model_dump(mode="json")),
                "application/vnd.novel-agent.planner-execution-result+json",
                self._schema_version,
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
                request=model_request("plan_rereview", request.task.mode, 2),
                base_commit=request.task.base_commit,
            )
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
                inquiry_revisions_used=inquiry_revisions,
                plan_revisions_used=plan_revisions,
                reviewer_memory_rounds_used=reviewer_memory_rounds,
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
        checkpoint_id = content_id(
            {
                "request": request.request_id.root,
                "phase": phase.value,
                "updates": {
                    key: value.model_dump(mode="json") if isinstance(value, ArtifactRef) else value
                    for key, value in updates.items()
                },
            }
        ).root.removeprefix("sha256:")[:24]
        checkpoint = PlanningLoopCheckpoint.model_validate(
            {
                "checkpoint_id": StableId(f"planning-checkpoint.{checkpoint_id}"),
                "request_id": request.request_id,
                "phase": phase,
                "base_commit": request.task.base_commit,
                "snapshot_id": request.snapshot_id,
                "configuration_fingerprint": request.configuration_fingerprint,
                **updates,
            }
        )
        return self._artifacts.put(
            canonical_json_bytes(checkpoint.model_dump(mode="json")),
            "application/vnd.novel-agent.planning-loop-checkpoint+json",
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
