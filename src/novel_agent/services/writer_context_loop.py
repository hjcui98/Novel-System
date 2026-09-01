"""Fixed Writer → Editor → Observer → Reconciliation candidate loop."""

from __future__ import annotations

from typing import cast

from pydantic import JsonValue

from novel_agent.agents.candidate_observer import (
    CandidateObservationAgent,
    CandidateObservationError,
)
from novel_agent.domain.agent_context import (
    CONTEXT_EVENT_SCHEMA_VERSION,
    AgentContextView,
    ContextCompactedPayload,
    ContextCompactionReceipt,
    ContextDelta,
    ContextDeltaAppliedPayload,
    ContextDeltaStatus,
    ContextItemKind,
    ContextLayer,
    ContextMemoryRequestedPayload,
    ContextMemoryResolvedPayload,
    ContextPressureDetectedPayload,
    ContextViewItem,
    LoopRoundProgress,
    SettledArtifactPayload,
    WriterWorkPlanSettledPayload,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import PlanRootDocument
from novel_agent.domain.editorial import (
    EditorialReport,
    EditorialReviewInput,
    EditorialVerdict,
    RepairedDraft,
)
from novel_agent.domain.generation import (
    DraftArtifact,
    RewriteDirective,
    WriterTurnAction,
    WriterWorkPlanResult,
    WritingLoopRequest,
)
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.runtime import RunEventType
from novel_agent.domain.stage2 import AgentMode, ProjectProfileRootDocument
from novel_agent.domain.writer_context import EvidenceLedgerV2, WriterContextPackageV2
from novel_agent.domain.writing_loop import (
    WRITING_LOOP_CHECKPOINT_MEDIA_TYPE,
    WritingLoopCheckpoint,
    WritingLoopPhase,
    WritingLoopResult,
    WritingLoopTerminalStatus,
)
from novel_agent.ports.model_endpoint import ModelEndpointError
from novel_agent.services.agent_context import (
    AgentContextProjector,
    AgentContextRuntime,
    ContextCompactor,
    ContextLimitError,
    ContextWindowPolicy,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.editorial import (
    EditorialRepairError,
    EditorialReviewError,
    EditorialService,
)
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.loop_round_progress import (
    editor_round_progress,
    writer_checkpoint_progress,
    writer_package_precondition,
    writer_round_progress,
)
from novel_agent.services.writer_candidate import (
    WriterCandidateError,
    WriterCandidateMaterializer,
)
from novel_agent.services.writer_change_reconciliation import (
    ReconciliationError,
    WriterChangeReconciliationService,
)
from novel_agent.services.writer_cognition import (
    WriterCognitionError,
    WriterCognitionService,
    WriterTurnResult,
)
from novel_agent.services.writer_reactive_memory import (
    ReactiveMemoryInputs,
    WriterReactiveMemoryError,
    WriterReactiveNeedAdapter,
)

WRITING_LOOP_RESULT_MEDIA_TYPE = "application/vnd.novel-agent.writing-loop-result+json"
EDITORIAL_REPORT_MEDIA_TYPE = "application/vnd.novel-agent.editorial-report+json"
RECONCILIATION_MEDIA_TYPE = "application/vnd.novel-agent.reconciliation+json"


class WriterContextLoopService:
    """Own only fixed routing, budgets, events, checkpoints, and terminal mapping."""

    def __init__(
        self,
        projector: AgentContextProjector,
        compactor: ContextCompactor,
        context_runtime: AgentContextRuntime,
        cognition: WriterCognitionService,
        reactive_memory: WriterReactiveNeedAdapter,
        candidate_materializer: WriterCandidateMaterializer,
        editorial: EditorialService,
        observer: CandidateObservationAgent,
        reconciliation: WriterChangeReconciliationService,
        artifacts: ArtifactRepository,
        events: RunEventLogRepository,
    ) -> None:
        self._projector = projector
        self._compactor = compactor
        self._context_runtime = context_runtime
        self._cognition = cognition
        self._reactive = reactive_memory
        self._materializer = candidate_materializer
        self._editorial = editorial
        self._observer = observer
        self._reconciliation = reconciliation
        self._artifacts = artifacts
        self._events = events

    async def execute(
        self,
        request: WritingLoopRequest,
        model_request: ModelRequest,
        reactive_inputs: ReactiveMemoryInputs,
    ) -> WritingLoopResult:
        try:
            return await self._execute(request, model_request, reactive_inputs)
        except (ModelEndpointError, TimeoutError) as error:
            return self._result(
                request,
                WritingLoopTerminalStatus.MODEL_UNAVAILABLE,
                error,
            )

    async def _execute(
        self,
        request: WritingLoopRequest,
        model_request: ModelRequest,
        reactive_inputs: ReactiveMemoryInputs,
    ) -> WritingLoopResult:
        if model_request.run_id != request.run_id or model_request.task_id != request.task_id:
            return self._result(
                request,
                WritingLoopTerminalStatus.BASIS_CHANGED,
                "ModelRequest belongs to another Writing loop",
            )
        not_ready = writer_package_precondition(request.writer_context_package)
        if not_ready is not None:
            return self._result(
                request,
                not_ready,
                "Writer Context package is not READY",
            )
        try:
            resume_checkpoint = self._load_resume_checkpoint(request)
            view = (
                self._seed(request)
                if resume_checkpoint is None
                else self._context_runtime.restore(
                    request.run_id,
                    resume_checkpoint.context_view,
                )
            )
        except (UnicodeError, ValueError, RuntimeError) as error:
            return self._result(
                request,
                WritingLoopTerminalStatus.BASIS_CHANGED,
                error,
            )
        policy = ContextWindowPolicy(
            sequence_limit=request.budgets.context_sequence_limit,
            reserved_output_tokens=request.budgets.reserved_output_tokens,
            safety_allowance_tokens=request.budgets.context_safety_allowance_tokens,
            soft_limit_tokens=request.budgets.context_soft_limit_tokens,
            tokenizer=request.writer_context_package.budget_report.tokenizer,
            tokenizer_version=request.writer_context_package.budget_report.tokenizer_version,
        )
        work_plan: WriterWorkPlanResult | None = None
        initial_draft: DraftArtifact | None = None
        rewritten_draft: DraftArtifact | None = None
        repaired_draft: RepairedDraft | None = None
        reports: list[EditorialReport] = []
        deltas: list[ContextDelta] = []
        compactions: list[ContextCompactionReceipt] = []
        artifacts: list[ArtifactRef] = []
        active_turn: WriterTurnResult | None = None
        initial_editor_context = None
        post_draft_calls_this_slice = 0

        if resume_checkpoint is None:
            try:
                view, receipt = self._ensure_dispatch(request, view, policy)
                if receipt is not None:
                    compactions.append(receipt)
                work_plan = await self._cognition.create_work_plan(
                    request,
                    view,
                    self._request(model_request, "writer-work-plan"),
                )
                artifacts.append(work_plan.work_plan_artifact)
                plan_item = ContextViewItem(
                    item_id=StableId(
                        f"writer-work-plan.{work_plan.work_plan.work_plan_id.root}"[:128]
                    ),
                    layer=ContextLayer.WORKING,
                    kind=ContextItemKind.WORK_PLAN,
                    content=work_plan.work_plan.model_dump_json(),
                    token_count=max(
                        1,
                        sum(
                            item.token_count
                            for item in view.protected_items
                            if item.kind is ContextItemKind.WRITING_TASK
                        ),
                    ),
                    source_artifact_refs=(work_plan.work_plan_artifact,),
                    mandatory=True,
                    information_scope="writer_safe",
                )
                view = self._append_and_apply(
                    request,
                    view,
                    RunEventType.WRITER_WORK_PLAN_SETTLED,
                    WriterWorkPlanSettledPayload(
                        work_plan_ref=work_plan.work_plan_artifact,
                        working_item=plan_item,
                    ).model_dump(mode="json"),
                    (work_plan.work_plan_artifact,),
                    "writer-work-plan",
                )
                self._checkpoint(view, "writer-work-plan")
            except ContextLimitError as error:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.CONTEXT_LIMIT,
                    error,
                    view=view,
                    work_plan=work_plan,
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )
            except (WriterCognitionError, ValueError, RuntimeError) as error:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.WRITER_FAILED,
                    error,
                    view=view,
                    work_plan=work_plan,
                    artifacts=tuple(artifacts),
                )
            memory_rounds = 0
            writer_turns = 0
            seen_fingerprints: set[ArtifactId] = set()
        else:
            work_plan = resume_checkpoint.work_plan
            active_turn = WriterTurnResult(
                output=resume_checkpoint.active_writer_turn_output,
                artifact=resume_checkpoint.active_writer_turn_artifact,
                raw_output_artifact=resume_checkpoint.active_writer_raw_output_artifact,
                model_call=resume_checkpoint.active_writer_model_call,
            )
            memory_rounds = resume_checkpoint.memory_rounds
            writer_turns = resume_checkpoint.writer_turns
            seen_fingerprints = set(resume_checkpoint.seen_memory_fingerprints)
            artifacts.extend(self._available_lineage(request))
            artifacts.extend(resume_checkpoint.settled_artifacts)
            initial_draft = resume_checkpoint.initial_draft
            rewritten_draft = resume_checkpoint.rewritten_draft
            repaired_draft = resume_checkpoint.repaired_draft
            reports.extend(resume_checkpoint.editorial_reports)
            if resume_checkpoint.observation_artifact is not None:
                artifacts.append(resume_checkpoint.observation_artifact)

        assert work_plan is not None
        slice_memory_rounds = 0
        slice_writer_turns = 0
        while True:
            if active_turn is None:
                try:
                    view, receipt = self._ensure_dispatch(request, view, policy)
                    if receipt is not None:
                        compactions.append(receipt)
                    turn_label = f"writer-turn-{writer_turns}"
                    active_turn = await self._cognition.take_turn(
                        request,
                        view,
                        work_plan,
                        self._request(model_request, turn_label),
                    )
                    writer_turns += 1
                    slice_writer_turns += 1
                    artifacts.extend((active_turn.artifact, active_turn.raw_output_artifact))
                    view = self._append_and_apply(
                        request,
                        view,
                        RunEventType.WRITER_TURN_SETTLED,
                        SettledArtifactPayload(artifact_ref=active_turn.artifact).model_dump(
                            mode="json"
                        ),
                        (active_turn.artifact, active_turn.raw_output_artifact),
                        turn_label,
                    )
                    self._checkpoint(view, turn_label)
                except ContextLimitError as error:
                    return self._result(
                        request,
                        WritingLoopTerminalStatus.CONTEXT_LIMIT,
                        error,
                        view=view,
                        work_plan=work_plan,
                        deltas=tuple(deltas),
                        compactions=tuple(compactions),
                        artifacts=tuple(artifacts),
                    )
                except (
                    WriterCognitionError,
                    WriterCandidateError,
                    ValueError,
                    RuntimeError,
                ) as error:
                    return self._result(
                        request,
                        WritingLoopTerminalStatus.WRITER_FAILED,
                        error,
                        view=view,
                        work_plan=work_plan,
                        deltas=tuple(deltas),
                        compactions=tuple(compactions),
                        artifacts=tuple(artifacts),
                    )
            if active_turn.output.action is WriterTurnAction.DRAFT_READY:
                break
            if request.budgets.max_reactive_memory_rounds == 0:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.MEMORY_INSUFFICIENT,
                    "Writer requested Memory but reactive Memory is disabled",
                    view=view,
                    work_plan=work_plan,
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )
            if (
                slice_memory_rounds >= request.budgets.max_reactive_memory_rounds
                or slice_writer_turns >= request.budgets.max_writer_turns
            ):
                checkpoint_ref = self._persist_workflow_checkpoint(
                    request,
                    view,
                    work_plan,
                    active_turn,
                    memory_rounds=memory_rounds,
                    writer_turns=writer_turns,
                    seen_fingerprints=seen_fingerprints,
                )
                artifacts.append(checkpoint_ref)
                return self._result(
                    request,
                    WritingLoopTerminalStatus.YIELDED,
                    "Writer reactive work slice ended with resumable state",
                    view=view,
                    work_plan=work_plan,
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                    checkpoint_ref=checkpoint_ref,
                    active_turn=active_turn,
                )
            try:
                reactive = self._reactive.resolve(
                    request,
                    view,
                    active_turn.output.memory_requests,
                    reactive_inputs,
                    seen_fingerprints=frozenset(seen_fingerprints),
                )
                delta = reactive.delta
                view = self._append_and_apply(
                    request,
                    view,
                    RunEventType.CONTEXT_MEMORY_REQUESTED,
                    ContextMemoryRequestedPayload(
                        request_ref=delta.request_ref,
                        request_fingerprint=reactive.request_fingerprint,
                    ).model_dump(mode="json"),
                    (delta.request_ref,),
                    f"memory-request-{memory_rounds}",
                )
                view = self._append_and_apply(
                    request,
                    view,
                    RunEventType.CONTEXT_MEMORY_RESOLVED,
                    ContextMemoryResolvedPayload(
                        request_ref=delta.request_ref,
                        resolution_ref=delta.resolution_ref,
                        status=delta.status,
                    ).model_dump(mode="json"),
                    (delta.resolution_ref,),
                    f"memory-resolved-{memory_rounds}",
                )
                view = self._append_and_apply(
                    request,
                    view,
                    RunEventType.CONTEXT_DELTA_APPLIED,
                    ContextDeltaAppliedPayload(delta=delta).model_dump(mode="json"),
                    (delta.request_ref, delta.resolution_ref, *delta.evidence_refs),
                    f"context-delta-{memory_rounds}",
                )
                deltas.append(delta)
                artifacts.extend((delta.request_ref, delta.resolution_ref, *delta.evidence_refs))
                self._checkpoint(view, f"context-delta-{memory_rounds}")
            except (WriterReactiveMemoryError, ValueError, RuntimeError) as error:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.MEMORY_DENIED,
                    error,
                    view=view,
                    work_plan=work_plan,
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )
            if delta.status is ContextDeltaStatus.DENIED:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.MEMORY_DENIED,
                    "reactive Memory request was denied",
                    view=view,
                    work_plan=work_plan,
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )
            if delta.status in {
                ContextDeltaStatus.INSUFFICIENT,
            }:
                # An unresolved marker is advisory input for the next Writer turn. A repeated
                # fingerprint means Memory has already had its bounded chance; it must not turn
                # that useful marker into a hard stop. Existing per-slice Writer/Memory limits
                # still yield a resumable checkpoint if the model keeps asking.
                seen_fingerprints.add(reactive.request_fingerprint)
                memory_rounds += 1
                slice_memory_rounds += 1
                active_turn = None
                continue
            if delta.status is ContextDeltaStatus.BUDGET_EXHAUSTED:
                memory_rounds += 1
                checkpoint_ref = self._persist_workflow_checkpoint(
                    request,
                    view,
                    work_plan,
                    active_turn,
                    memory_rounds=memory_rounds,
                    writer_turns=writer_turns,
                    seen_fingerprints=seen_fingerprints,
                )
                artifacts.append(checkpoint_ref)
                return self._result(
                    request,
                    WritingLoopTerminalStatus.MEMORY_BUDGET_EXHAUSTED,
                    "reactive Memory requires an explicit budget extension",
                    view=view,
                    work_plan=work_plan,
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                    checkpoint_ref=checkpoint_ref,
                    active_turn=active_turn,
                )
            seen_fingerprints.add(reactive.request_fingerprint)
            memory_rounds += 1
            slice_memory_rounds += 1
            active_turn = None

        assert active_turn is not None
        if initial_draft is None:
            try:
                initial_draft = self._materializer.materialize(
                    request,
                    view,
                    work_plan,
                    active_turn,
                    mode=request.mode,
                )
                # The Editor must read the exact Context revision bound into the Draft basis.
                # Settling the candidate is a later event and therefore changes the live View hash.
                initial_editor_context = self._materializer.editor_context(request, view)
                artifacts.extend(
                    (
                        initial_draft.text_artifact,
                        initial_draft.sidecar_artifact,
                        initial_draft.raw_output_artifact,
                    )
                )
                view = self._append_and_apply(
                    request,
                    view,
                    RunEventType.DRAFT_CANDIDATE_SETTLED,
                    SettledArtifactPayload(artifact_ref=initial_draft.text_artifact).model_dump(
                        mode="json"
                    ),
                    (initial_draft.text_artifact,),
                    "initial-draft",
                )
                self._checkpoint(view, "initial-draft")
            except (WriterCandidateError, ValueError, RuntimeError) as error:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.WRITER_FAILED,
                    error,
                    view=view,
                    work_plan=work_plan,
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )
        elif (
            resume_checkpoint is not None
            and resume_checkpoint.phase is WritingLoopPhase.EDITOR_PENDING
        ):
            initial_editor_context = resume_checkpoint.editor_context

        if (
            resume_checkpoint is None
            and post_draft_calls_this_slice >= request.budgets.max_post_draft_model_calls
        ):
            checkpoint_ref = self._persist_workflow_checkpoint(
                request,
                view,
                work_plan,
                active_turn,
                memory_rounds=memory_rounds,
                writer_turns=writer_turns,
                seen_fingerprints=seen_fingerprints,
                phase=WritingLoopPhase.EDITOR_PENDING,
                initial_draft=initial_draft,
                editor_context=initial_editor_context,
                final_candidate_id=initial_draft.draft_id,
                final_text_artifact=initial_draft.text_artifact,
                final_declared_memory_hints=active_turn.output.declared_memory_hints,
                settled_artifacts=tuple(artifacts),
            )
            artifacts.append(checkpoint_ref)
            return self._result(
                request,
                WritingLoopTerminalStatus.YIELDED,
                "post-Draft work slice ended before Editor",
                view=view,
                work_plan=work_plan,
                initial_draft=initial_draft,
                final_candidate_id=initial_draft.draft_id,
                final_text_artifact=initial_draft.text_artifact,
                artifacts=tuple(artifacts),
                checkpoint_ref=checkpoint_ref,
                active_turn=active_turn,
            )

        if resume_checkpoint is not None and resume_checkpoint.phase in {
            WritingLoopPhase.OBSERVER_PENDING,
            WritingLoopPhase.RECONCILIATION_PENDING,
        }:
            report = reports[-1]
            assert resume_checkpoint.final_candidate_id is not None
            assert resume_checkpoint.final_text_artifact is not None
            final_id = resume_checkpoint.final_candidate_id
            final_text = resume_checkpoint.final_text_artifact
            final_hints = resume_checkpoint.final_declared_memory_hints
        else:
            assert initial_editor_context is not None
            review_input = EditorialReviewInput(
                draft=initial_draft,
                writing_task=request.writing_task,
                context=initial_editor_context,
            )
            try:
                report = await self._editorial.review(
                    review_input,
                    self._request(model_request, "editor-review-initial"),
                )
                post_draft_calls_this_slice += 1
                reports.append(report)
                report_ref = self._persist_report(report)
                artifacts.append(report_ref)
                view = self._append_and_apply(
                    request,
                    view,
                    RunEventType.EDITOR_REVIEW_SETTLED,
                    SettledArtifactPayload(artifact_ref=report_ref).model_dump(mode="json"),
                    (report_ref,),
                    "editor-review-initial",
                )
            except EditorialReviewError as error:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.EDITOR_FAILED,
                    error,
                    view=view,
                    work_plan=work_plan,
                    initial_draft=initial_draft,
                    reports=tuple(reports),
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )

            final_id = initial_draft.draft_id
            final_text = initial_draft.text_artifact
            final_hints = active_turn.output.declared_memory_hints
        if report.verdict is EditorialVerdict.LOCAL_REPAIR:
            if request.budgets.max_local_repairs < 1:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.REVIEW_REQUIRED_LOCAL_REPAIR_EXHAUSTED,
                    "local repair requires a new reviewed attempt under the pinned policy",
                    view=view,
                    work_plan=work_plan,
                    initial_draft=initial_draft,
                    reports=tuple(reports),
                    final_candidate_id=initial_draft.draft_id,
                    final_text_artifact=initial_draft.text_artifact,
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )
            local_repair_attempt = 0
            while True:
                local_repair_attempt += 1
                repair_label = (
                    "editor-local-repair"
                    if local_repair_attempt == 1
                    else f"editor-local-repair-{local_repair_attempt}"
                )
                review_label = (
                    "editor-review-local-repair"
                    if local_repair_attempt == 1
                    else f"editor-review-local-repair-{local_repair_attempt}"
                )
                try:
                    repaired_draft = await self._editorial.repair(
                        review_input,
                        report,
                        self._request(model_request, repair_label),
                    )
                    post_draft_calls_this_slice += 1
                    artifacts.append(repaired_draft.text_artifact)
                    view = self._append_and_apply(
                        request,
                        view,
                        RunEventType.EDITOR_REPAIR_SETTLED,
                        SettledArtifactPayload(
                            artifact_ref=repaired_draft.text_artifact,
                            parent_artifact_ref=initial_draft.text_artifact,
                        ).model_dump(mode="json"),
                        (repaired_draft.text_artifact,),
                        repair_label,
                    )
                    verification = await self._editorial.review_repaired(
                        review_input,
                        report,
                        repaired_draft,
                        self._request(model_request, review_label),
                    )
                    post_draft_calls_this_slice += 1
                    reports.append(verification)
                    verification_ref = self._persist_report(verification)
                    artifacts.append(verification_ref)
                    view = self._append_and_apply(
                        request,
                        view,
                        RunEventType.EDITOR_REVIEW_SETTLED,
                        SettledArtifactPayload(
                            artifact_ref=verification_ref,
                            parent_artifact_ref=repaired_draft.text_artifact,
                        ).model_dump(mode="json"),
                        (verification_ref,),
                        review_label,
                    )
                except EditorialRepairError as error:
                    if (
                        str(error) == "LOCAL_REPAIR produced no text change"
                        and local_repair_attempt < request.budgets.max_local_repairs
                    ):
                        continue
                    return self._result(
                        request,
                        WritingLoopTerminalStatus.EDITOR_FAILED,
                        error,
                        view=view,
                        work_plan=work_plan,
                        initial_draft=initial_draft,
                        repaired_draft=repaired_draft,
                        reports=tuple(reports),
                        deltas=tuple(deltas),
                        compactions=tuple(compactions),
                        artifacts=tuple(artifacts),
                    )
                except EditorialReviewError as error:
                    return self._result(
                        request,
                        WritingLoopTerminalStatus.EDITOR_FAILED,
                        error,
                        view=view,
                        work_plan=work_plan,
                        initial_draft=initial_draft,
                        repaired_draft=repaired_draft,
                        reports=tuple(reports),
                        deltas=tuple(deltas),
                        compactions=tuple(compactions),
                        artifacts=tuple(artifacts),
                    )
                break
            if verification.verdict is not EditorialVerdict.PASS:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.REVIEW_REQUIRED_LOCAL_REPAIR_EXHAUSTED,
                    "independent re-review did not pass the one allowed local repair",
                    view=view,
                    work_plan=work_plan,
                    initial_draft=initial_draft,
                    repaired_draft=repaired_draft,
                    reports=tuple(reports),
                    final_candidate_id=repaired_draft.draft_id,
                    final_text_artifact=repaired_draft.text_artifact,
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )
            final_id = repaired_draft.draft_id
            final_text = repaired_draft.text_artifact
        elif report.verdict is EditorialVerdict.MAJOR_REWRITE:
            assert initial_draft is not None
            rewrite_attempt = 0
            rewrite_parent = initial_draft
            rewrite_turn: WriterTurnResult | None = None
            major_verification: EditorialReport | None = None
            while True:
                if rewrite_attempt >= request.budgets.max_major_rewrites:
                    allowance = request.budgets.max_major_rewrites
                    return self._result(
                        request,
                        WritingLoopTerminalStatus.REVIEW_REQUIRED_MAJOR_REWRITE_EXHAUSTED,
                        f"full re-review did not pass the {allowance} allowed major rewrite(s)",
                        view=view,
                        work_plan=work_plan,
                        initial_draft=initial_draft,
                        rewritten_draft=rewritten_draft,
                        reports=tuple(reports),
                        final_candidate_id=(
                            rewritten_draft.draft_id
                            if rewritten_draft is not None
                            else initial_draft.draft_id
                        ),
                        final_text_artifact=(
                            rewritten_draft.text_artifact
                            if rewritten_draft is not None
                            else initial_draft.text_artifact
                        ),
                        deltas=tuple(deltas),
                        compactions=tuple(compactions),
                        artifacts=tuple(artifacts),
                    )
                rewrite_attempt += 1
                try:
                    directive = cast(RewriteDirective, report.rewrite_directive)
                    instruction_item = ContextViewItem(
                        item_id=StableId(f"editor-directive.{directive.directive_id.root}"[:128]),
                        layer=ContextLayer.WORKING,
                        kind=ContextItemKind.EDITOR_INSTRUCTION,
                        content=directive.model_dump_json(),
                        token_count=max(1, len(directive.model_dump_json().encode("utf-8")) // 3),
                        source_artifact_refs=(directive.directive_artifact,),
                        mandatory=True,
                        information_scope="writer_safe",
                    )
                    view = self._projector.put_working_item(
                        view,
                        instruction_item,
                        replace_kind=ContextItemKind.EDITOR_INSTRUCTION,
                    )
                    view, receipt = self._ensure_dispatch(request, view, policy)
                    if receipt is not None:
                        compactions.append(receipt)
                    rewrite_request = request.model_copy(update={"mode": AgentMode.MAJOR_REWRITE})
                    rewrite_label = (
                        "writer-major-rewrite"
                        if rewrite_attempt == 1
                        else f"writer-major-rewrite-{rewrite_attempt}"
                    )
                    rewrite_turn = await self._cognition.take_turn(
                        rewrite_request,
                        view,
                        work_plan,
                        self._request(model_request, rewrite_label),
                        major_rewrite_attempt=rewrite_attempt,
                    )
                    post_draft_calls_this_slice += 1
                    if rewrite_turn.output.action is not WriterTurnAction.DRAFT_READY:
                        raise WriterCognitionError(
                            "major rewrite cannot start another Memory round"
                        )
                    artifacts.extend((rewrite_turn.artifact, rewrite_turn.raw_output_artifact))
                    view = self._append_and_apply(
                        request,
                        view,
                        RunEventType.WRITER_TURN_SETTLED,
                        SettledArtifactPayload(artifact_ref=rewrite_turn.artifact).model_dump(
                            mode="json"
                        ),
                        (rewrite_turn.artifact, rewrite_turn.raw_output_artifact),
                        rewrite_label,
                    )
                    rewritten_draft = self._materializer.materialize(
                        rewrite_request,
                        view,
                        work_plan,
                        rewrite_turn,
                        mode=AgentMode.MAJOR_REWRITE,
                        parent_draft=rewrite_parent,
                    )
                    artifacts.extend(
                        (
                            rewritten_draft.text_artifact,
                            rewritten_draft.sidecar_artifact,
                            rewritten_draft.raw_output_artifact,
                        )
                    )
                    rewritten_input = EditorialReviewInput(
                        draft=rewritten_draft,
                        writing_task=request.writing_task,
                        context=self._materializer.editor_context(request, view),
                    )
                    editor_label = (
                        "editor-review-major-rewrite"
                        if rewrite_attempt == 1
                        else f"editor-review-major-rewrite-{rewrite_attempt}"
                    )
                    major_verification = await self._editorial.review(
                        rewritten_input,
                        self._request(model_request, editor_label),
                    )
                    post_draft_calls_this_slice += 1
                    reports.append(major_verification)
                    verification_ref = self._persist_report(major_verification)
                    artifacts.append(verification_ref)
                    view = self._append_and_apply(
                        request,
                        view,
                        RunEventType.EDITOR_REVIEW_SETTLED,
                        SettledArtifactPayload(
                            artifact_ref=verification_ref,
                            parent_artifact_ref=rewritten_draft.text_artifact,
                        ).model_dump(mode="json"),
                        (verification_ref,),
                        editor_label,
                    )
                except (
                    WriterCognitionError,
                    WriterCandidateError,
                    EditorialReviewError,
                    ContextLimitError,
                    ValueError,
                    RuntimeError,
                ) as error:
                    return self._result(
                        request,
                        WritingLoopTerminalStatus.WRITER_FAILED,
                        error,
                        view=view,
                        work_plan=work_plan,
                        initial_draft=initial_draft,
                        rewritten_draft=rewritten_draft,
                        reports=tuple(reports),
                        deltas=tuple(deltas),
                        compactions=tuple(compactions),
                        artifacts=tuple(artifacts),
                    )
                assert major_verification is not None
                assert rewrite_turn is not None
                assert rewritten_draft is not None
                if major_verification.verdict is EditorialVerdict.PASS:
                    final_id = rewritten_draft.draft_id
                    final_text = rewritten_draft.text_artifact
                    final_hints = rewrite_turn.output.declared_memory_hints
                    break
                if (
                    major_verification.verdict is not EditorialVerdict.MAJOR_REWRITE
                    or rewrite_attempt >= request.budgets.max_major_rewrites
                ):
                    allowance = request.budgets.max_major_rewrites
                    return self._result(
                        request,
                        WritingLoopTerminalStatus.REVIEW_REQUIRED_MAJOR_REWRITE_EXHAUSTED,
                        f"full re-review did not pass the {allowance} allowed major rewrite(s)",
                        view=view,
                        work_plan=work_plan,
                        initial_draft=initial_draft,
                        rewritten_draft=rewritten_draft,
                        reports=tuple(reports),
                        final_candidate_id=rewritten_draft.draft_id,
                        final_text_artifact=rewritten_draft.text_artifact,
                        deltas=tuple(deltas),
                        compactions=tuple(compactions),
                        artifacts=tuple(artifacts),
                    )
                report = major_verification
                rewrite_parent = rewritten_draft

        if (
            resume_checkpoint is None
            or resume_checkpoint.phase is not WritingLoopPhase.RECONCILIATION_PENDING
        ) and post_draft_calls_this_slice >= request.budgets.max_post_draft_model_calls:
            checkpoint_ref = self._persist_workflow_checkpoint(
                request,
                view,
                work_plan,
                active_turn,
                memory_rounds=memory_rounds,
                writer_turns=writer_turns,
                seen_fingerprints=seen_fingerprints,
                phase=WritingLoopPhase.OBSERVER_PENDING,
                initial_draft=initial_draft,
                rewritten_draft=rewritten_draft,
                repaired_draft=repaired_draft,
                reports=tuple(reports),
                final_candidate_id=final_id,
                final_text_artifact=final_text,
                final_declared_memory_hints=final_hints,
                settled_artifacts=tuple(artifacts),
            )
            artifacts.append(checkpoint_ref)
            return self._result(
                request,
                WritingLoopTerminalStatus.YIELDED,
                "post-Draft work slice ended before Observer",
                view=view,
                work_plan=work_plan,
                initial_draft=initial_draft,
                rewritten_draft=rewritten_draft,
                repaired_draft=repaired_draft,
                reports=tuple(reports),
                final_candidate_id=final_id,
                final_text_artifact=final_text,
                artifacts=tuple(artifacts),
                checkpoint_ref=checkpoint_ref,
                active_turn=active_turn,
            )

        if (
            resume_checkpoint is not None
            and resume_checkpoint.phase is WritingLoopPhase.RECONCILIATION_PENDING
        ):
            assert resume_checkpoint.observation is not None
            assert resume_checkpoint.observation_artifact is not None
            observation = resume_checkpoint.observation
            observation_ref = resume_checkpoint.observation_artifact
        else:
            try:
                observation, observation_ref, _call = await self._observer.observe(
                    final_id,
                    final_text,
                    view.context_hash,
                    self._request(model_request, "candidate-observation"),
                )
                post_draft_calls_this_slice += 1
                artifacts.append(observation_ref)
                view = self._append_and_apply(
                    request,
                    view,
                    RunEventType.CANDIDATE_OBSERVATION_SETTLED,
                    SettledArtifactPayload(
                        artifact_ref=observation_ref,
                        parent_artifact_ref=final_text,
                    ).model_dump(mode="json"),
                    (observation_ref,),
                    "candidate-observation",
                )
            except (CandidateObservationError, ValueError, RuntimeError) as error:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.OBSERVER_FAILED,
                    error,
                    view=view,
                    work_plan=work_plan,
                    initial_draft=initial_draft,
                    rewritten_draft=rewritten_draft,
                    repaired_draft=repaired_draft,
                    reports=tuple(reports),
                    final_candidate_id=final_id,
                    final_text_artifact=final_text,
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )

        if (
            resume_checkpoint is None
            or resume_checkpoint.phase is not WritingLoopPhase.RECONCILIATION_PENDING
        ) and post_draft_calls_this_slice >= request.budgets.max_post_draft_model_calls:
            checkpoint_ref = self._persist_workflow_checkpoint(
                request,
                view,
                work_plan,
                active_turn,
                memory_rounds=memory_rounds,
                writer_turns=writer_turns,
                seen_fingerprints=seen_fingerprints,
                phase=WritingLoopPhase.RECONCILIATION_PENDING,
                initial_draft=initial_draft,
                rewritten_draft=rewritten_draft,
                repaired_draft=repaired_draft,
                reports=tuple(reports),
                final_candidate_id=final_id,
                final_text_artifact=final_text,
                final_declared_memory_hints=final_hints,
                observation=observation,
                observation_artifact=observation_ref,
                settled_artifacts=tuple(artifacts),
            )
            artifacts.append(checkpoint_ref)
            return self._result(
                request,
                WritingLoopTerminalStatus.YIELDED,
                "post-Draft work slice ended before reconciliation",
                view=view,
                work_plan=work_plan,
                initial_draft=initial_draft,
                rewritten_draft=rewritten_draft,
                repaired_draft=repaired_draft,
                reports=tuple(reports),
                final_candidate_id=final_id,
                final_text_artifact=final_text,
                observation=observation,
                observation_artifact=observation_ref,
                artifacts=tuple(artifacts),
                checkpoint_ref=checkpoint_ref,
                active_turn=active_turn,
            )

        try:
            reconciliation = self._reconciliation.reconcile(
                final_id,
                final_hints,
                observation,
            )
            reconciliation_ref = self._artifacts.put(
                canonical_json_bytes(reconciliation.model_dump(mode="json")),
                RECONCILIATION_MEDIA_TYPE,
                CONTEXT_EVENT_SCHEMA_VERSION,
            )
            artifacts.append(reconciliation_ref)
            view = self._append_and_apply(
                request,
                view,
                RunEventType.CANDIDATE_RECONCILIATION_SETTLED,
                SettledArtifactPayload(
                    artifact_ref=reconciliation_ref,
                    parent_artifact_ref=observation_ref,
                ).model_dump(mode="json"),
                (reconciliation_ref,),
                "candidate-reconciliation",
            )
            self._checkpoint(view, "candidate-reconciliation")
        except (ReconciliationError, ValueError, RuntimeError) as error:
            return self._result(
                request,
                WritingLoopTerminalStatus.RECONCILIATION_FAILED,
                error,
                view=view,
                work_plan=work_plan,
                initial_draft=initial_draft,
                rewritten_draft=rewritten_draft,
                repaired_draft=repaired_draft,
                reports=tuple(reports),
                final_candidate_id=final_id,
                final_text_artifact=final_text,
                observation=observation,
                observation_artifact=observation_ref,
                deltas=tuple(deltas),
                compactions=tuple(compactions),
                artifacts=tuple(artifacts),
            )
        # Reconciliation is a diagnostic audit of weak Writer memory hints.  The
        # Editor's final PASS remains the hard content gate; a declared-only or
        # mismatched hint is retained in the reconciliation artifact as
        # unverified advisory context and must not block the chapter candidate.
        # Identity/basis failures still fail closed in the reconciliation call
        # above, and the Chapter Settlement Curator remains the only owner that
        # can turn visible evidence into a durable Memory/Canon delta.
        return self._result(
            request,
            WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY,
            None,
            view=view,
            work_plan=work_plan,
            initial_draft=initial_draft,
            rewritten_draft=rewritten_draft,
            repaired_draft=repaired_draft,
            reports=tuple(reports),
            final_candidate_id=final_id,
            final_text_artifact=final_text,
            observation=observation,
            observation_artifact=observation_ref,
            reconciliation=reconciliation,
            deltas=tuple(deltas),
            compactions=tuple(compactions),
            artifacts=tuple(artifacts),
        )

    def _seed(self, request: WritingLoopRequest) -> AgentContextView:
        accepted_plan_content = self._accepted_plan_content(request)
        project_profile_content = self._project_profile_content(request)
        completeness = self._writer_context_completeness_item(request)
        protected = (
            *completeness,
            self._protected_item(
                "writing-task",
                ContextItemKind.WRITING_TASK,
                request.writing_task.model_dump_json(),
                request.writing_task_artifact,
            ),
            self._protected_item(
                "accepted-plan",
                ContextItemKind.ACCEPTED_PLAN,
                accepted_plan_content,
                request.accepted_plan.artifact,
            ),
            self._protected_item(
                "project-profile",
                ContextItemKind.PROJECT_PROFILE,
                project_profile_content,
                request.project_profile_artifact,
            ),
        )
        recent_prose = self._recent_prose_items(request)
        evidence_first = self._evidence_first_items(request)
        return self._projector.seed_writer(
            run_id=request.run_id,
            task_id=request.task_id,
            package=request.writer_context_package,
            seed_package_ref=request.writer_context_package_artifact,
            profile_ref=request.project_profile_artifact,
            plan_ref=request.accepted_plan.artifact,
            protected_items=protected,
            recent_prose_items=recent_prose,
            evidence_first_items=evidence_first,
        )

    def _writer_context_completeness_item(
        self,
        request: WritingLoopRequest,
    ) -> tuple[ContextViewItem, ...]:
        package = request.writer_context_package
        if not isinstance(package, WriterContextPackageV2):
            return ()
        content = canonical_json_bytes(
            {
                "assembly_status": package.assembly_status,
                "semantic_status": package.semantic_status,
                "usable_with_gaps": package.usable_with_gaps,
                "structural_mandatory_facet_closure": (package.structural_mandatory_facet_closure),
                "unclosed_mandatory_need_facets": [
                    item.root for item in package.unclosed_mandatory_need_facets
                ],
            }
        ).decode("utf-8")
        return (
            self._protected_item(
                "writer-context-completeness",
                ContextItemKind.SYSTEM_POLICY,
                content,
                request.writer_context_package_artifact,
            ),
        )

    def _evidence_first_items(
        self,
        request: WritingLoopRequest,
    ) -> tuple[ContextViewItem, ...]:
        package = request.writer_context_package
        if not isinstance(package, WriterContextPackageV2):
            return ()
        ledger = EvidenceLedgerV2.model_validate_json(
            self._artifacts.read_verified(package.evidence_ledger_ref)
        )
        entries = {entry.ledger_id: entry for entry in ledger.entries}
        requested = {
            ledger_id
            for item in package.items
            if item.gap is None
            for ledger_id in item.evidence_ledger_ids
        }
        if requested - entries.keys():
            raise ValueError("evidence-first Writer context references a missing ledger entry")
        items: list[ContextViewItem] = []
        for package_item in package.items:
            if package_item.gap is not None:
                gap = package_item.gap
                content = (
                    f"[未解决记忆需求: {package_item.section.value}]\n"
                    f"目的: {package_item.purpose}\n"
                    f"状态: {gap.kind.value}\n"
                    f"原因: {gap.reason}\n"
                    f"unverified={str(package_item.unverified).lower()}; "
                    "仅作 advisory, 不得把它当作已证实事实。\n"
                    "需要时通过受控 reactive Memory 请求补充。不得把缺失内容当成事实。"
                )
                items.append(
                    ContextViewItem(
                        item_id=StableId(f"unresolved-need.{gap.gap_id.root}"[:128]),
                        layer=ContextLayer.MEMORY,
                        kind=ContextItemKind.UNRESOLVED_NEED,
                        content=content,
                        token_count=max(1, len(content.encode("utf-8")) // 3),
                        source_artifact_refs=(
                            request.writer_context_package_artifact,
                            *package_item.advisory_artifact_refs,
                        ),
                        mandatory=package_item.mandatory,
                        information_scope="writer_safe",
                    )
                )
                continue
            expansion = (
                "\n[渐进展开]\n该预览已截断。完整 exact evidence 保留在绑定 Ledger。"
                "只有当前动作确实受阻时才请求展开。"
                if package_item.preview_truncated
                else ""
            )
            content = (
                f"[记忆需求: {package_item.section.value}]\n{package_item.purpose}\n"
                f"[有界原始证据预览]\n{package_item.raw_preview}{expansion}"
            )
            items.append(
                ContextViewItem(
                    item_id=StableId(f"evidence-handle.{package_item.item_id.root}"[:128]),
                    layer=ContextLayer.MEMORY,
                    kind=ContextItemKind.EVIDENCE_HANDLE,
                    content=content,
                    token_count=max(1, len(content.encode("utf-8")) // 3),
                    source_artifact_refs=(
                        request.writer_context_package_artifact,
                        package.evidence_ledger_ref,
                    ),
                    mandatory=package_item.mandatory,
                    information_scope="writer_safe",
                )
            )
        return tuple(items)

    def _recent_prose_items(
        self,
        request: WritingLoopRequest,
    ) -> tuple[ContextViewItem, ...]:
        context = request.recent_prose_context
        items: list[ContextViewItem] = []
        previous = context.previous_chapter
        if previous is not None:
            text = self._artifacts.read_verified(previous.full_text_artifact).decode("utf-8")
            if len(text) != previous.full_text_characters:
                raise ValueError("previous chapter artifact length differs from recent prose")
            title = f" {previous.title}" if previous.title else ""
            content = f"[上一章完整正文: 第{previous.chapter_index}章{title}]\n{text}"
            items.append(
                ContextViewItem(
                    item_id=StableId(f"recent-prose.full.chapter.{previous.chapter_index}"),
                    layer=ContextLayer.MEMORY,
                    kind=ContextItemKind.RECENT_PROSE,
                    content=content,
                    token_count=max(1, len(content.encode("utf-8")) // 3),
                    source_artifact_refs=(
                        request.recent_prose_context_artifact,
                        previous.full_text_artifact,
                    ),
                    mandatory=True,
                    information_scope="writer_safe",
                )
            )
        for chapter in context.earlier_chapters:
            title = f" {chapter.title}" if chapter.title else ""
            content = f"[近期章尾: 第{chapter.chapter_index}章{title}]\n{chapter.compact_trail}"
            items.append(
                ContextViewItem(
                    item_id=StableId(f"recent-prose.trail.chapter.{chapter.chapter_index}"),
                    layer=ContextLayer.MEMORY,
                    kind=ContextItemKind.RECENT_PROSE,
                    content=content,
                    token_count=max(1, len(content.encode("utf-8")) // 3),
                    source_artifact_refs=(
                        request.recent_prose_context_artifact,
                        chapter.full_text_artifact,
                    ),
                    information_scope="writer_safe",
                )
            )
        return tuple(items)

    def _accepted_plan_content(self, request: WritingLoopRequest) -> str:
        raw = self._artifacts.read_verified(request.accepted_plan.artifact)
        try:
            plan = PlanRootDocument.model_validate_json(raw)
        except ValueError:
            return raw.decode("utf-8")
        target = request.writing_task.target_chapter
        goals = tuple(
            goal for goal in plan.chapter_goals if target - 1 <= goal.chapter_index <= target + 2
        )
        selected_goal_ids = {goal.goal_id for goal in goals}
        active_obligations = set(request.writing_task.active_plan_obligations)
        nodes = tuple(
            node
            for node in plan.nodes
            if node.plan_node_id in selected_goal_ids
            or bool(set(node.obligation_ids) & active_obligations)
        )
        return canonical_json_bytes(
            {
                "revision": request.accepted_plan.revision,
                "target_chapter": target,
                "chapter_goals": [goal.model_dump(mode="json") for goal in goals],
                "relevant_plan_nodes": [node.model_dump(mode="json") for node in nodes],
            }
        ).decode("utf-8")

    def _project_profile_content(self, request: WritingLoopRequest) -> str:
        raw = self._artifacts.read_verified(request.project_profile_artifact)
        try:
            profile = ProjectProfileRootDocument.model_validate_json(raw)
        except ValueError:
            return raw.decode("utf-8")
        return canonical_json_bytes(
            {
                "revision": request.project_profile_revision,
                "style_profile": profile.style_profile,
                "capability_profile": profile.capability_profile,
                "model_profiles": profile.model_profiles,
            }
        ).decode("utf-8")

    @staticmethod
    def _protected_item(
        suffix: str,
        kind: ContextItemKind,
        content: str,
        artifact: ArtifactRef,
    ) -> ContextViewItem:
        return ContextViewItem(
            item_id=StableId(f"context-protected.{suffix}"),
            layer=ContextLayer.PROTECTED,
            kind=kind,
            content=content,
            token_count=max(1, len(content.encode("utf-8")) // 3),
            source_artifact_refs=(artifact,),
            mandatory=True,
            information_scope="writer_safe",
            instruction_boundary=True,
        )

    def _ensure_dispatch(
        self,
        request: WritingLoopRequest,
        view: AgentContextView,
        policy: ContextWindowPolicy,
    ) -> tuple[AgentContextView, ContextCompactionReceipt | None]:
        pressure = self._compactor.pressure(view, policy)
        compaction_receipt = None
        if pressure.soft_exceeded:
            view = self._append_and_apply(
                request,
                view,
                RunEventType.CONTEXT_PRESSURE_DETECTED,
                ContextPressureDetectedPayload(pressure=pressure).model_dump(mode="json"),
                (),
                f"context-pressure-{view.revision}-{view.generation}",
            )
            compacted, receipt = self._compactor.compact(
                view,
                policy,
                hard=pressure.hard_exceeded,
            )
            if receipt is not None:
                compaction_receipt = receipt
                view = self._append_and_apply(
                    request,
                    view,
                    RunEventType.CONTEXT_COMPACTED,
                    ContextCompactedPayload(receipt=receipt).model_dump(mode="json"),
                    tuple(
                        item
                        for item in (receipt.summary_artifact, receipt.detail_artifact)
                        if item is not None
                    ),
                    f"context-compacted-{receipt.published_generation}",
                )
                compacted = view
            view = compacted
        provider_receipt = self._compactor.provider_receipt(view, policy)
        if not provider_receipt.provider_valid:
            raise ContextLimitError("provider-valid dispatch Gate rejected the Context View")
        return (
            view.model_copy(update={"provider_validity_receipt": provider_receipt}),
            compaction_receipt,
        )

    def _append_and_apply(
        self,
        request: WritingLoopRequest,
        view: AgentContextView,
        event_type: RunEventType,
        payload: JsonValue,
        artifact_refs: tuple[ArtifactRef, ...],
        label: str,
    ) -> AgentContextView:
        if view.task_id != request.task_id or view.run_id != request.run_id:
            raise ValueError("Writer Context View belongs to another Writing loop")
        return self._context_runtime.append_and_apply(
            view,
            event_type=event_type,
            payload=payload,
            artifact_refs=artifact_refs,
            label=label,
            trace_namespace=self._trace_namespace(request),
        )

    @staticmethod
    def _trace_namespace(request: WritingLoopRequest) -> str:
        """Keep Context event identities distinct across durable Writer retries."""

        if request.attempt_id is None:
            return "stage3"
        attempt_suffix = content_id(request.attempt_id.root).root.removeprefix("sha256:")[:24]
        return f"stage3-attempt-{attempt_suffix}"

    def _checkpoint(self, view: AgentContextView, label: str) -> None:
        readable = (
            f"checkpoint.stage3.{view.consumer.value}.{view.task_id.root}."
            f"e{view.basis_event_position}.{label}"
        )
        checkpoint_id = (
            StableId(readable)
            if len(readable) <= 128
            else StableId(
                "checkpoint.stage3."
                + content_id(
                    (
                        view.task_id.root,
                        view.consumer.value,
                        view.basis_event_position,
                        label,
                    )
                ).root[-48:]
            )
        )
        self._context_runtime.checkpoint(
            view,
            checkpoint_id,
        )

    def _load_resume_checkpoint(
        self,
        request: WritingLoopRequest,
    ) -> WritingLoopCheckpoint | None:
        ref = request.resume_checkpoint_ref
        if ref is None:
            return None
        if ref.media_type != WRITING_LOOP_CHECKPOINT_MEDIA_TYPE:
            raise ValueError("Writer resume artifact is not a WritingLoopCheckpoint")
        checkpoint = WritingLoopCheckpoint.model_validate_json(self._artifacts.read_verified(ref))
        expected = (
            checkpoint.run_id == request.run_id,
            checkpoint.task_id == request.task_id,
            checkpoint.base_commit == request.base_commit,
            checkpoint.snapshot_id == request.snapshot_id,
            checkpoint.writing_task_ref == request.writing_task_artifact,
            checkpoint.accepted_plan_ref == request.accepted_plan.artifact,
            checkpoint.project_profile_ref == request.project_profile_artifact,
            checkpoint.writer_context_ref == request.writer_context_package_artifact,
            checkpoint.recent_prose_ref == request.recent_prose_context_artifact,
        )
        if not all(expected):
            raise ValueError("Writer resume checkpoint differs from the current request basis")
        return checkpoint

    def _persist_workflow_checkpoint(
        self,
        request: WritingLoopRequest,
        view: AgentContextView,
        work_plan: WriterWorkPlanResult,
        active_turn: WriterTurnResult,
        *,
        memory_rounds: int,
        writer_turns: int,
        seen_fingerprints: set[ArtifactId],
        phase: WritingLoopPhase = WritingLoopPhase.REACTIVE_MEMORY_PENDING,
        initial_draft: DraftArtifact | None = None,
        editor_context: object | None = None,
        rewritten_draft: DraftArtifact | None = None,
        repaired_draft: RepairedDraft | None = None,
        reports: tuple[EditorialReport, ...] = (),
        final_candidate_id: ArtifactId | None = None,
        final_text_artifact: ArtifactRef | None = None,
        final_declared_memory_hints: tuple[object, ...] = (),
        observation: object | None = None,
        observation_artifact: ArtifactRef | None = None,
        settled_artifacts: tuple[ArtifactRef, ...] = (),
    ) -> ArtifactRef:
        from novel_agent.domain.editorial import CuratorObservation
        from novel_agent.domain.generation import DeclaredMemoryHint, WriterContextSnapshot

        suffix = content_id(
            (
                request.task_id.root,
                view.basis_event_position,
                memory_rounds,
                writer_turns,
            )
        ).root[-48:]
        checkpoint = WritingLoopCheckpoint(
            checkpoint_id=StableId(f"writing-loop-checkpoint.{suffix}"),
            run_id=request.run_id,
            task_id=request.task_id,
            phase=phase,
            base_commit=request.base_commit,
            snapshot_id=request.snapshot_id,
            writing_task_ref=request.writing_task_artifact,
            accepted_plan_ref=request.accepted_plan.artifact,
            project_profile_ref=request.project_profile_artifact,
            writer_context_ref=request.writer_context_package_artifact,
            recent_prose_ref=request.recent_prose_context_artifact,
            work_plan=work_plan,
            active_writer_turn_output=active_turn.output,
            active_writer_turn_artifact=active_turn.artifact,
            active_writer_raw_output_artifact=active_turn.raw_output_artifact,
            active_writer_model_call=active_turn.model_call,
            memory_rounds=memory_rounds,
            writer_turns=writer_turns,
            seen_memory_fingerprints=tuple(sorted(seen_fingerprints, key=lambda item: item.root)),
            context_view=view,
            initial_draft=initial_draft,
            editor_context=(
                editor_context if isinstance(editor_context, WriterContextSnapshot) else None
            ),
            rewritten_draft=rewritten_draft,
            repaired_draft=repaired_draft,
            editorial_reports=reports,
            final_candidate_id=final_candidate_id,
            final_text_artifact=final_text_artifact,
            final_declared_memory_hints=tuple(
                item for item in final_declared_memory_hints if isinstance(item, DeclaredMemoryHint)
            ),
            observation=(observation if isinstance(observation, CuratorObservation) else None),
            observation_artifact=observation_artifact,
            settled_artifacts=tuple(dict.fromkeys(settled_artifacts)),
            round_progress=self._checkpoint_round_progress(
                request,
                phase,
                work_plan=work_plan,
                active_turn=active_turn,
                reports=reports,
            ),
        )
        return self._artifacts.put(
            canonical_json_bytes(checkpoint.model_dump(mode="json")),
            WRITING_LOOP_CHECKPOINT_MEDIA_TYPE,
            CONTEXT_EVENT_SCHEMA_VERSION,
        )

    @staticmethod
    def _checkpoint_round_progress(
        request: WritingLoopRequest,
        phase: WritingLoopPhase,
        *,
        work_plan: WriterWorkPlanResult,
        active_turn: WriterTurnResult,
        reports: tuple[EditorialReport, ...],
    ) -> LoopRoundProgress:
        if reports:
            current = reports[-1]
            previous = reports[-2].issues if len(reports) > 1 else ()
            return editor_round_progress(
                current.verdict,
                basis_commit=request.base_commit,
                previous_issue_ids=tuple(issue.issue_id for issue in previous),
                current_issue_ids=tuple(issue.issue_id for issue in current.issues),
                remaining_work=tuple(issue.issue_id.root for issue in current.issues),
                artifact_ref=active_turn.artifact,
                input_candidate_ref=work_plan.work_plan_artifact,
            )
        return writer_checkpoint_progress(
            basis_commit=request.base_commit,
            remaining_work=(phase.value,),
            artifact_ref=active_turn.artifact,
            input_candidate_ref=work_plan.work_plan_artifact,
        )

    def _persist_report(self, report: EditorialReport) -> ArtifactRef:
        return self._artifacts.put(
            canonical_json_bytes(report.model_dump(mode="json")),
            EDITORIAL_REPORT_MEDIA_TYPE,
            CONTEXT_EVENT_SCHEMA_VERSION,
        )

    @staticmethod
    def _request(request: ModelRequest, label: str) -> ModelRequest:
        digest = content_id({"request_id": request.request_id.root, "stage": label}).root[-48:]
        return request.model_copy(
            update={
                "request_id": StableId(f"request.stage3.{label}.{digest}"[:128]),
                "trace_id": f"{request.trace_id}:{label}",
                "prompt": "",
            }
        )

    def _result(
        self,
        request: WritingLoopRequest,
        status: WritingLoopTerminalStatus,
        detail: str | Exception | None,
        *,
        view: AgentContextView | None = None,
        work_plan: WriterWorkPlanResult | None = None,
        initial_draft: DraftArtifact | None = None,
        rewritten_draft: DraftArtifact | None = None,
        repaired_draft: RepairedDraft | None = None,
        reports: tuple[EditorialReport, ...] = (),
        final_candidate_id: ArtifactId | None = None,
        final_text_artifact: ArtifactRef | None = None,
        observation: object | None = None,
        observation_artifact: ArtifactRef | None = None,
        reconciliation: object | None = None,
        deltas: tuple[object, ...] = (),
        compactions: tuple[object, ...] = (),
        artifacts: tuple[ArtifactRef, ...] = (),
        checkpoint_ref: ArtifactRef | None = None,
        active_turn: WriterTurnResult | None = None,
    ) -> WritingLoopResult:
        from novel_agent.domain.agent_context import ContextCompactionReceipt, ContextDelta
        from novel_agent.domain.editorial import CuratorObservation, ReconciliationResult

        if isinstance(detail, Exception) and self._is_model_runtime_unavailable(detail):
            status = WritingLoopTerminalStatus.MODEL_UNAVAILABLE
        detail_text = None if detail is None else (str(detail).strip() or type(detail).__name__)
        if status is WritingLoopTerminalStatus.MODEL_UNAVAILABLE:
            artifacts = tuple(dict.fromkeys((*self._available_lineage(request), *artifacts)))
        result = WritingLoopResult(
            result_id=StableId(f"writing-loop-result.{request.run_id.root}.{status.value}"[:128]),
            run_id=request.run_id,
            task_id=request.task_id,
            status=status,
            work_plan=work_plan,
            initial_draft=initial_draft,
            rewritten_draft=rewritten_draft,
            repaired_draft=repaired_draft,
            final_candidate_id=final_candidate_id,
            final_text_artifact=final_text_artifact,
            editorial_reports=reports,
            observation=(observation if isinstance(observation, CuratorObservation) else None),
            observation_artifact=observation_artifact,
            reconciliation=(
                reconciliation if isinstance(reconciliation, ReconciliationResult) else None
            ),
            context_view=view,
            checkpoint_ref=checkpoint_ref,
            context_deltas=tuple(item for item in deltas if isinstance(item, ContextDelta)),
            compaction_receipts=tuple(
                item for item in compactions if isinstance(item, ContextCompactionReceipt)
            ),
            model_call_records=self._model_calls(
                work_plan,
                initial_draft,
                rewritten_draft,
                repaired_draft,
                reports,
                observation,
                active_turn,
            ),
            artifacts=tuple(dict.fromkeys(artifacts)),
            failure_detail=detail_text,
            round_progress=writer_round_progress(
                status,
                basis_commit=request.base_commit,
                remaining_work=() if detail_text is None else (detail_text,),
                artifact_ref=checkpoint_ref or final_text_artifact,
                input_candidate_ref=(None if work_plan is None else work_plan.work_plan_artifact),
            ),
        )
        self._artifacts.put(
            canonical_json_bytes(result.model_dump(mode="json")),
            WRITING_LOOP_RESULT_MEDIA_TYPE,
            CONTEXT_EVENT_SCHEMA_VERSION,
        )
        return result

    def _available_lineage(self, request: WritingLoopRequest) -> tuple[ArtifactRef, ...]:
        package_refs = (
            (request.writer_context_package.evidence_ledger_ref,)
            if isinstance(request.writer_context_package, WriterContextPackageV2)
            else ()
        )
        resume_refs = (
            (request.resume_checkpoint_ref,) if request.resume_checkpoint_ref is not None else ()
        )
        event_refs = tuple(
            ref
            for event in self._events.replay(request.run_id)
            if event.task_id == request.task_id
            for ref in event.artifact_refs
        )
        return tuple(
            dict.fromkeys(
                (
                    request.writing_task_artifact,
                    request.accepted_plan.artifact,
                    request.project_profile_artifact,
                    request.writer_context_package_artifact,
                    *package_refs,
                    request.recent_prose_context_artifact,
                    *resume_refs,
                    *event_refs,
                )
            )
        )

    @staticmethod
    def _is_model_runtime_unavailable(error: Exception) -> bool:
        current: BaseException | None = error
        while current is not None:
            if isinstance(current, (ModelEndpointError, TimeoutError)):
                return True
            current = current.__cause__
        return False

    @staticmethod
    def _model_calls(
        work_plan: WriterWorkPlanResult | None,
        initial_draft: DraftArtifact | None,
        rewritten_draft: DraftArtifact | None,
        repaired_draft: RepairedDraft | None,
        reports: tuple[EditorialReport, ...],
        observation: object | None,
        active_turn: WriterTurnResult | None,
    ) -> tuple[ModelCallRecord, ...]:
        from novel_agent.domain.editorial import CuratorObservation

        calls = [
            work_plan.model_call_record if work_plan is not None else None,
            initial_draft.model_call_record if initial_draft is not None else None,
            rewritten_draft.model_call_record if rewritten_draft is not None else None,
            repaired_draft.model_call_record if repaired_draft is not None else None,
            active_turn.model_call if active_turn is not None else None,
            *(report.model_call_record for report in reports),
            (
                observation.model_call_record
                if isinstance(observation, CuratorObservation)
                else None
            ),
        ]
        unique = {call.request_id: call for call in calls if call is not None}
        return tuple(unique.values())


__all__ = [
    "EDITORIAL_REPORT_MEDIA_TYPE",
    "RECONCILIATION_MEDIA_TYPE",
    "WRITING_LOOP_RESULT_MEDIA_TYPE",
    "WriterContextLoopService",
]
