"""Fixed Writer → Editor → Observer → Reconciliation candidate loop."""

from __future__ import annotations

from datetime import UTC, datetime
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
    SettledArtifactPayload,
    WriterWorkPlanSettledPayload,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.editorial import (
    EditorialReport,
    EditorialReviewInput,
    EditorialVerdict,
    ReconciliationClass,
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
from novel_agent.domain.runtime import RunEvent, RunEventType
from novel_agent.domain.stage2 import AgentMode
from novel_agent.domain.writing_loop import (
    WritingLoopResult,
    WritingLoopTerminalStatus,
)
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
        if model_request.run_id != request.run_id or model_request.task_id != request.task_id:
            return self._result(
                request,
                WritingLoopTerminalStatus.BASIS_CHANGED,
                "ModelRequest belongs to another Writing loop",
            )
        view = self._seed(request)
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
                item_id=StableId(f"writer-work-plan.{work_plan.work_plan.work_plan_id.root}"[:128]),
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
                str(error),
                view=view,
                work_plan=work_plan,
                compactions=tuple(compactions),
                artifacts=tuple(artifacts),
            )
        except (WriterCognitionError, ValueError, RuntimeError) as error:
            return self._result(
                request,
                WritingLoopTerminalStatus.WRITER_FAILED,
                str(error),
                view=view,
                work_plan=work_plan,
                artifacts=tuple(artifacts),
            )

        memory_rounds = 0
        seen_fingerprints: set[ArtifactId] = set()
        while True:
            try:
                view, receipt = self._ensure_dispatch(request, view, policy)
                if receipt is not None:
                    compactions.append(receipt)
                active_turn = await self._cognition.take_turn(
                    request,
                    view,
                    work_plan,
                    self._request(model_request, f"writer-turn-{memory_rounds}"),
                )
                artifacts.extend((active_turn.artifact, active_turn.raw_output_artifact))
                view = self._append_and_apply(
                    request,
                    view,
                    RunEventType.WRITER_TURN_SETTLED,
                    SettledArtifactPayload(artifact_ref=active_turn.artifact).model_dump(
                        mode="json"
                    ),
                    (active_turn.artifact, active_turn.raw_output_artifact),
                    f"writer-turn-{memory_rounds}",
                )
                self._checkpoint(view, f"writer-turn-{memory_rounds}")
            except ContextLimitError as error:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.CONTEXT_LIMIT,
                    str(error),
                    view=view,
                    work_plan=work_plan,
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )
            except (WriterCognitionError, WriterCandidateError, ValueError, RuntimeError) as error:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.WRITER_FAILED,
                    str(error),
                    view=view,
                    work_plan=work_plan,
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )
            if active_turn.output.action is WriterTurnAction.DRAFT_READY:
                break
            if memory_rounds >= request.budgets.max_reactive_memory_rounds:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.MEMORY_INSUFFICIENT,
                    "Writer requested Memory after the bounded reactive round",
                    view=view,
                    work_plan=work_plan,
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )
            try:
                reactive = self._reactive.resolve(
                    request,
                    view,
                    active_turn.output.memory_requests,
                    reactive_inputs,
                    seen_fingerprints=frozenset(seen_fingerprints),
                )
                seen_fingerprints.add(reactive.request_fingerprint)
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
                    str(error),
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
                ContextDeltaStatus.BUDGET_EXHAUSTED,
            }:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.MEMORY_INSUFFICIENT,
                    f"reactive Memory ended with {delta.status.value}",
                    view=view,
                    work_plan=work_plan,
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )
            memory_rounds += 1

        assert active_turn is not None
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
                str(error),
                view=view,
                work_plan=work_plan,
                deltas=tuple(deltas),
                compactions=tuple(compactions),
                artifacts=tuple(artifacts),
            )

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
                str(error),
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
            try:
                repaired_draft = await self._editorial.repair(
                    review_input,
                    report,
                    self._request(model_request, "editor-local-repair"),
                )
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
                    "editor-local-repair",
                )
                verification = await self._editorial.review_repaired(
                    review_input,
                    report,
                    repaired_draft,
                    self._request(model_request, "editor-review-local-repair"),
                )
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
                    "editor-review-local-repair",
                )
            except (EditorialRepairError, EditorialReviewError) as error:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.EDITOR_FAILED,
                    str(error),
                    view=view,
                    work_plan=work_plan,
                    initial_draft=initial_draft,
                    repaired_draft=repaired_draft,
                    reports=tuple(reports),
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )
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
                view = self._projector.put_working_item(view, instruction_item)
                view, receipt = self._ensure_dispatch(request, view, policy)
                if receipt is not None:
                    compactions.append(receipt)
                rewrite_request = request.model_copy(update={"mode": AgentMode.MAJOR_REWRITE})
                rewrite_turn = await self._cognition.take_turn(
                    rewrite_request,
                    view,
                    work_plan,
                    self._request(model_request, "writer-major-rewrite"),
                )
                if rewrite_turn.output.action is not WriterTurnAction.DRAFT_READY:
                    raise WriterCognitionError("major rewrite cannot start another Memory round")
                artifacts.extend((rewrite_turn.artifact, rewrite_turn.raw_output_artifact))
                view = self._append_and_apply(
                    request,
                    view,
                    RunEventType.WRITER_TURN_SETTLED,
                    SettledArtifactPayload(artifact_ref=rewrite_turn.artifact).model_dump(
                        mode="json"
                    ),
                    (rewrite_turn.artifact, rewrite_turn.raw_output_artifact),
                    "writer-major-rewrite",
                )
                rewritten_draft = self._materializer.materialize(
                    rewrite_request,
                    view,
                    work_plan,
                    rewrite_turn,
                    mode=AgentMode.MAJOR_REWRITE,
                    parent_draft=initial_draft,
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
                verification = await self._editorial.review(
                    rewritten_input,
                    self._request(model_request, "editor-review-major-rewrite"),
                )
                reports.append(verification)
                verification_ref = self._persist_report(verification)
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
                    "editor-review-major-rewrite",
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
                    str(error),
                    view=view,
                    work_plan=work_plan,
                    initial_draft=initial_draft,
                    rewritten_draft=rewritten_draft,
                    reports=tuple(reports),
                    deltas=tuple(deltas),
                    compactions=tuple(compactions),
                    artifacts=tuple(artifacts),
                )
            if verification.verdict is not EditorialVerdict.PASS:
                return self._result(
                    request,
                    WritingLoopTerminalStatus.REVIEW_REQUIRED_MAJOR_REWRITE_EXHAUSTED,
                    "full re-review did not pass the one allowed major rewrite",
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
            final_id = rewritten_draft.draft_id
            final_text = rewritten_draft.text_artifact
            final_hints = rewrite_turn.output.declared_memory_hints

        try:
            observation, observation_ref, _call = await self._observer.observe(
                final_id,
                final_text,
                view.context_hash,
                self._request(model_request, "candidate-observation"),
            )
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
                str(error),
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
                str(error),
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
        if any(
            item.classification is not ReconciliationClass.MATCHED
            for item in reconciliation.comparisons
        ):
            return self._result(
                request,
                WritingLoopTerminalStatus.REVIEW_REQUIRED,
                "Writer declarations and independent observation do not fully reconcile",
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
        protected = (
            self._protected_item(
                "writing-task",
                ContextItemKind.WRITING_TASK,
                request.writing_task.model_dump_json(),
                request.writing_task_artifact,
            ),
            self._protected_item(
                "accepted-plan",
                ContextItemKind.ACCEPTED_PLAN,
                request.accepted_plan.model_dump_json(),
                request.accepted_plan.artifact,
            ),
            self._protected_item(
                "project-profile",
                ContextItemKind.PROJECT_PROFILE,
                request.project_profile_revision,
                request.project_profile_artifact,
            ),
        )
        return self._projector.seed_writer(
            run_id=request.run_id,
            task_id=request.task_id,
            package=request.writer_context_package,
            seed_package_ref=request.writer_context_package_artifact,
            profile_ref=request.project_profile_artifact,
            plan_ref=request.accepted_plan.artifact,
            protected_items=protected,
        )

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
        identity = StableId(f"event.stage3.{request.run_id.root}.{label}"[:128])
        existing = next(
            (
                item
                for item in self._events.replay(request.run_id)
                if item.idempotency_identity == identity
            ),
            None,
        )
        if existing is None:
            prior = self._events.replay(request.run_id)
            event = RunEvent(
                event_id=StableId(
                    f"event-id.stage3.{content_id((request.run_id.root, label)).root[-48:]}"
                ),
                run_id=request.run_id,
                task_id=request.task_id,
                sequence_no=(prior[-1].sequence_no + 1 if prior else 1),
                event_type=event_type,
                occurred_at=datetime.now(UTC),
                idempotency_identity=identity,
                payload_schema_version=CONTEXT_EVENT_SCHEMA_VERSION,
                trace_id=f"stage3:{request.run_id.root}",
                payload=payload,
                artifact_refs=artifact_refs,
            )
            existing = self._events.append(event)
        return self._projector.apply_event(view, existing)

    def _checkpoint(self, view: AgentContextView, label: str) -> None:
        self._context_runtime.checkpoint(
            view,
            StableId(f"checkpoint.stage3.{view.run_id.root}.{label}"[:128]),
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
        detail: str | None,
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
    ) -> WritingLoopResult:
        from novel_agent.domain.agent_context import ContextCompactionReceipt, ContextDelta
        from novel_agent.domain.editorial import CuratorObservation, ReconciliationResult

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
            ),
            artifacts=tuple(dict.fromkeys(artifacts)),
            failure_detail=detail,
        )
        self._artifacts.put(
            canonical_json_bytes(result.model_dump(mode="json")),
            WRITING_LOOP_RESULT_MEDIA_TYPE,
            CONTEXT_EVENT_SCHEMA_VERSION,
        )
        return result

    @staticmethod
    def _model_calls(
        work_plan: WriterWorkPlanResult | None,
        initial_draft: DraftArtifact | None,
        rewritten_draft: DraftArtifact | None,
        repaired_draft: RepairedDraft | None,
        reports: tuple[EditorialReport, ...],
        observation: object | None,
    ) -> tuple[ModelCallRecord, ...]:
        from novel_agent.domain.editorial import CuratorObservation

        calls = [
            work_plan.model_call_record if work_plan is not None else None,
            initial_draft.model_call_record if initial_draft is not None else None,
            rewritten_draft.model_call_record if rewritten_draft is not None else None,
            repaired_draft.model_call_record if repaired_draft is not None else None,
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
