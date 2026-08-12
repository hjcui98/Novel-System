"""Stage 3 WriterContext handoff and candidate-only integration flow.

This module is intentionally small.  Stage 2M owns ``WriterContextPackage`` production, the
Writer and Editor services own their model calls, and the reconciliation service owns its
matching rules.  The code here only validates the boundary, converts the package into the
Writer's local snapshot contract, and executes the documented business order.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.editorial import (
    CuratorObservation,
    EditorialReport,
    EditorialReviewInput,
    EditorialVerdict,
    ReconciliationResult,
    RepairedDraft,
)
from novel_agent.domain.generation import (
    DraftArtifact,
    WriterArtifactBasis,
    WriterContextHandoffRequest,
    WriterContextSnapshot,
    WriterExecutionResult,
    WriterInvocation,
    WriterSidecar,
    WriterTerminalStatus,
)
from novel_agent.domain.generation import (
    WriterContextItem as GenerationContextItem,
)
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.stage2 import AgentMode
from novel_agent.domain.writer_context import (
    ContextAssemblyStatus,
    WriterContextPackage,
    WriterContextSection,
)
from novel_agent.domain.writer_context import (
    WriterContextItem as PackageContextItem,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.editorial import EditorialService
from novel_agent.services.writer_change_reconciliation import (
    ReconciliationError,
    WriterChangeReconciliationService,
)
from novel_agent.services.writer_generation import WriterGenerationService

CONTEXT_SNAPSHOT_MEDIA_TYPE = "application/vnd.novel-agent.writer-context-snapshot+json"
WRITING_TASK_MEDIA_TYPE = "application/vnd.novel-agent.writing-task-contract+json"

CuratorObserver = Callable[
    [ArtifactId, ArtifactRef], CuratorObservation | Awaitable[CuratorObservation]
]


class WriterContextHandoffError(ValueError):
    """A formal Context package cannot safely enter the Writing Core."""

    def __init__(self, detail: str, *, code: str = "HANDOFF_REJECTED") -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class WriterIntegrationStatus(StrEnum):
    """Terminal states for the candidate-only integration flow."""

    COMPLETED = "completed"
    HANDOFF_REJECTED = "handoff_rejected"
    WRITER_FAILED = "writer_failed"
    EDITOR_FAILED = "editor_failed"
    REWRITE_REQUIRED = "rewrite_required"
    RECONCILIATION_FAILED = "reconciliation_failed"


@dataclass(frozen=True, slots=True)
class WriterContextHandoff:
    """The trusted conversion result consumed by ``WriterGenerationService``."""

    request: WriterContextHandoffRequest
    package: WriterContextPackage
    context: WriterContextSnapshot
    basis: WriterArtifactBasis
    invocation: WriterInvocation
    context_artifact: ArtifactRef
    writing_contract_artifact: ArtifactRef

    @property
    def evidence_ledger_ref(self) -> ArtifactRef:
        """Expose the ledger lineage without expanding it into the Writer prompt."""

        return self.package.evidence_ledger_ref


@dataclass(frozen=True, slots=True)
class WriterIntegrationResult:
    """Combined result for one candidate generation and read-only quality pass."""

    status: WriterIntegrationStatus
    handoff: WriterContextHandoff | None = None
    writer_result: WriterExecutionResult | None = None
    editorial_report: EditorialReport | None = None
    repaired_draft: RepairedDraft | None = None
    repair_verification_report: EditorialReport | None = None
    curator_observation: CuratorObservation | None = None
    reconciliation: ReconciliationResult | None = None
    failure_detail: str | None = None

    @property
    def draft(self) -> DraftArtifact | None:
        """Return the original Writer candidate, including when a repair is later produced."""

        return self.writer_result.draft if self.writer_result is not None else None

    @property
    def final_candidate_id(self) -> ArtifactId | None:
        if self.repaired_draft is not None:
            return self.repaired_draft.draft_id
        if self.writer_result is not None and self.writer_result.draft is not None:
            return self.writer_result.draft.draft_id
        return None

    @property
    def complete(self) -> bool:
        return self.status is WriterIntegrationStatus.COMPLETED


class WriterContextHandoffAdapter:
    """Convert the Stage 2M package into exactly one Writer invocation."""

    def __init__(
        self,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion,
    ) -> None:
        self._artifacts = artifacts
        self._schema_version = schema_version

    def adapt(self, request: WriterContextHandoffRequest) -> WriterContextHandoff:
        self._validate_readiness(request)
        context = self._snapshot(request)
        try:
            context_artifact = self._artifacts.put(
                canonical_json_bytes(context.model_dump(mode="json")),
                CONTEXT_SNAPSHOT_MEDIA_TYPE,
                self._schema_version,
            )
            writing_contract_artifact = self._artifacts.put(
                canonical_json_bytes(request.writing_task.model_dump(mode="json")),
                WRITING_TASK_MEDIA_TYPE,
                self._schema_version,
            )
            basis = WriterArtifactBasis(
                project_id=request.project_id,
                base_commit=request.context_package.basis_commit_id,
                snapshot_id=request.context_package.basis_snapshot_id,
                context_id=context.context_id,
                context_artifact=context_artifact,
                context_fingerprint=context_artifact.artifact_id,
                writing_contract_artifact=writing_contract_artifact,
                plan_artifact=request.plan_artifact,
                project_profile_artifact=request.project_profile_artifact,
                configuration_fingerprint=request.writer_configuration_fingerprint,
                model_configuration_fingerprint=request.model_configuration_fingerprint,
                future_isolation_attestation=request.future_isolation_attestation,
                memory_gate_report=request.memory_gate_report,
                memory_gate_artifact=request.memory_gate_artifact,
                source_artifacts=request.source_artifacts,
            )
            input_artifacts = _unique_artifacts(
                (
                    context_artifact,
                    writing_contract_artifact,
                    request.plan_artifact,
                    request.project_profile_artifact,
                    *(
                        (request.memory_gate_artifact,)
                        if request.memory_gate_artifact is not None
                        else ()
                    ),
                    *(binding.source_artifact for binding in request.source_artifacts),
                    *(
                        (
                            request.prior_draft.text_artifact,
                            request.prior_draft.sidecar_artifact,
                            request.prior_draft.raw_output_artifact,
                        )
                        if request.prior_draft is not None
                        else ()
                    ),
                    *(
                        (request.continuation_boundary.frozen_prefix_artifact,)
                        if request.continuation_boundary is not None
                        else ()
                    ),
                    *(
                        (request.rewrite_directive.directive_artifact,)
                        if request.rewrite_directive is not None
                        else ()
                    ),
                )
            )
            invocation = WriterInvocation(
                invocation_id=StableId(f"writer-invocation.{request.integration_id.root}"[:128]),
                run_id=request.run_id,
                task_id=request.task_id,
                mode=request.mode,
                basis=basis,
                writing_task=request.writing_task,
                context_package=context,
                input_artifacts=input_artifacts,
                prior_draft=request.prior_draft,
                continuation_boundary=request.continuation_boundary,
                rewrite_directive=request.rewrite_directive,
                budget=request.budget,
            )
        except WriterContextHandoffError:
            raise
        except (ValidationError, ValueError, RuntimeError) as error:
            raise WriterContextHandoffError(
                f"Writer Context handoff could not be materialized: {error}"
            ) from error
        return WriterContextHandoff(
            request=request,
            package=request.context_package,
            context=context,
            basis=basis,
            invocation=invocation,
            context_artifact=context_artifact,
            writing_contract_artifact=writing_contract_artifact,
        )

    @staticmethod
    def _validate_readiness(request: WriterContextHandoffRequest) -> None:
        package = request.context_package
        task = package.task_contract
        writing_task = request.writing_task
        if package.budget_report.final_status is not ContextAssemblyStatus.READY:
            raise WriterContextHandoffError(
                "Writer Context is not ready for generation: "
                f"{package.budget_report.final_status.value}",
                code="CONTEXT_NOT_READY",
            )
        if package.budget_report.actual_rendered_writer_tokens > request.budget.input_token_limit:
            raise WriterContextHandoffError(
                "Writer Context exceeds the invocation input budget",
                code="CONTEXT_BUDGET_INSUFFICIENT",
            )
        if not package.rendered_context.strip():
            raise WriterContextHandoffError(
                "Writer Context has no rendered writer-safe content",
                code="CONTEXT_EMPTY",
            )
        if not task.target_chapter_start <= writing_task.target_chapter <= task.target_chapter_end:
            raise WriterContextHandoffError(
                "WritingTask target chapter is outside the Context target range",
                code="TASK_TARGET_MISMATCH",
            )
        attestation = request.future_isolation_attestation
        if attestation.checkpoint_chapter != task.checkpoint_chapter:
            raise WriterContextHandoffError(
                "Future-isolation checkpoint differs from the Context checkpoint",
                code="BASIS_MISMATCH",
            )
        if not attestation.passed or attestation.overlap_source_ids:
            raise WriterContextHandoffError(
                "Writer Context handoff requires passing future isolation",
                code="FUTURE_ISOLATION_FAILED",
            )
        if writing_task.blocking_gaps:
            raise WriterContextHandoffError(
                "WritingTask contains blocking Context gaps",
                code="BLOCKING_GAP",
            )
        if any(gap.conflict for gap in package.gaps):
            raise WriterContextHandoffError(
                "Writer Context contains a conflicting unresolved gap",
                code="BLOCKING_GAP",
            )
        if request.mode is AgentMode.DRAFT and any(
            value is not None
            for value in (
                request.prior_draft,
                request.continuation_boundary,
                request.rewrite_directive,
            )
        ):
            raise WriterContextHandoffError(
                "DRAFT handoff cannot include prior or rewrite inputs",
                code="MODE_CONTRACT_REJECTED",
            )
        if request.mode is AgentMode.CONTINUE and (
            request.prior_draft is None
            or request.continuation_boundary is None
            or request.rewrite_directive is not None
        ):
            raise WriterContextHandoffError(
                "CONTINUE handoff requires a prior Draft and continuation boundary",
                code="MODE_CONTRACT_REJECTED",
            )
        if request.mode is AgentMode.MAJOR_REWRITE and (
            request.prior_draft is None
            or request.rewrite_directive is None
            or request.continuation_boundary is not None
        ):
            raise WriterContextHandoffError(
                "MAJOR_REWRITE handoff requires a prior Draft and rewrite directive",
                code="MODE_CONTRACT_REJECTED",
            )

    @staticmethod
    def _snapshot(request: WriterContextHandoffRequest) -> WriterContextSnapshot:
        package = request.context_package
        items: list[GenerationContextItem] = []
        sections = (
            (WriterContextSection.CONTINUITY_CONSTRAINTS, package.continuity_constraints),
            (WriterContextSection.CURRENT_WORLD_STATE, package.current_world_state),
            (WriterContextSection.RELATIONSHIP_AND_EMOTION, package.relationship_and_emotion),
            (WriterContextSection.CAUSAL_HISTORY, package.causal_history),
            (WriterContextSection.KNOWLEDGE_AND_DISCLOSURE, package.knowledge_and_disclosure),
            (WriterContextSection.PLAN_AND_OBLIGATIONS, package.plan_and_obligations),
            (WriterContextSection.LONG_RANGE_CALLBACKS, package.long_range_callbacks),
        )
        for section, section_items in sections:
            items.extend(
                _convert_item(
                    item,
                    section,
                    package.basis_commit_id,
                    package.basis_snapshot_id,
                )
                for item in section_items
            )
        rendered_id = _stable_id(
            "context-rendered",
            {
                "context": package.model_dump(mode="json"),
                "task": request.writing_task.contract_id.root,
            },
        )
        items.append(
            GenerationContextItem(
                item_id=rendered_id,
                category="rendered_context",
                text=package.rendered_context,
                source_commit=package.basis_commit_id,
                snapshot_id=package.basis_snapshot_id,
                information_label="writer_safe",
                truth_class="rendered_context",
                support_status="writer_safe",
                mandatory=True,
            )
        )
        context_id = _stable_id(
            "context-handoff",
            {
                "package": package.model_dump(mode="json"),
                "writing_task": request.writing_task.model_dump(mode="json"),
            },
        )
        return WriterContextSnapshot(
            context_id=context_id,
            base_commit=package.basis_commit_id,
            snapshot_id=package.basis_snapshot_id,
            task_contract=request.writing_task.contract_id.root,
            items=tuple(items),
            unresolved_gaps=tuple(gap.description for gap in package.gaps),
            budget_report={
                "configured_writer_token_budget": (
                    package.budget_report.configured_writer_token_budget
                ),
                "actual_rendered_writer_tokens": (
                    package.budget_report.actual_rendered_writer_tokens
                ),
                "evidence_ledger_tokens": package.budget_report.evidence_ledger_tokens,
                "mandatory_conclusion_tokens": package.budget_report.mandatory_conclusion_tokens,
                "optional_conclusion_tokens": package.budget_report.optional_conclusion_tokens,
            },
        )


class WriterDraftIntegrationService:
    """Run the minimal Writer → Editor → observation → reconciliation chain."""

    def __init__(
        self,
        writer: WriterGenerationService,
        editorial: EditorialService,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion,
        *,
        curator_observer: CuratorObserver | None = None,
        reconciliation: WriterChangeReconciliationService | None = None,
    ) -> None:
        self._writer = writer
        self._editorial = editorial
        self._artifacts = artifacts
        self._adapter = WriterContextHandoffAdapter(artifacts, schema_version)
        self._curator_observer = curator_observer
        self._reconciliation = reconciliation or WriterChangeReconciliationService()

    def handoff(self, request: WriterContextHandoffRequest) -> WriterContextHandoff:
        """Expose the single formal Context adapter for callers that need preflight only."""

        return self._adapter.adapt(request)

    async def execute(
        self,
        request: WriterContextHandoffRequest,
        writer_request: ModelRequest,
        *,
        curator_observation: CuratorObservation | None = None,
    ) -> WriterIntegrationResult:
        try:
            handoff = self.handoff(request)
        except WriterContextHandoffError as error:
            return WriterIntegrationResult(
                status=WriterIntegrationStatus.HANDOFF_REJECTED,
                failure_detail=f"{error.code}: {error.detail}",
            )

        try:
            writer_result = await self._writer.execute(handoff.invocation, writer_request)
        except Exception as error:
            return WriterIntegrationResult(
                status=WriterIntegrationStatus.WRITER_FAILED,
                handoff=handoff,
                failure_detail=f"Writer execution raised {type(error).__name__}: {error}",
            )
        if (
            writer_result.status is not WriterTerminalStatus.COMPLETED
            or writer_result.draft is None
        ):
            return WriterIntegrationResult(
                status=WriterIntegrationStatus.WRITER_FAILED,
                handoff=handoff,
                writer_result=writer_result,
                failure_detail=writer_result.failure_detail or writer_result.status.value,
            )

        draft = writer_result.draft
        try:
            review_input = EditorialReviewInput(
                draft=draft,
                writing_task=request.writing_task,
                context=handoff.context,
            )
            report = await self._editorial.review(
                review_input,
                _derived_request(writer_request, "editor-review"),
            )
        except Exception as error:
            return WriterIntegrationResult(
                status=WriterIntegrationStatus.EDITOR_FAILED,
                handoff=handoff,
                writer_result=writer_result,
                failure_detail=f"Editor REVIEW failed: {type(error).__name__}: {error}",
            )

        if report.verdict is EditorialVerdict.MAJOR_REWRITE:
            return WriterIntegrationResult(
                status=WriterIntegrationStatus.REWRITE_REQUIRED,
                handoff=handoff,
                writer_result=writer_result,
                editorial_report=report,
                failure_detail="Editor routed the candidate to Writer MAJOR_REWRITE",
            )

        repaired: RepairedDraft | None = None
        repair_verification: EditorialReport | None = None
        candidate_id = draft.draft_id
        candidate_text_artifact = draft.text_artifact
        if report.verdict is EditorialVerdict.LOCAL_REPAIR:
            try:
                repaired = await self._editorial.repair(
                    review_input,
                    report,
                    _derived_request(writer_request, "editor-local-repair"),
                )
            except Exception as error:
                return WriterIntegrationResult(
                    status=WriterIntegrationStatus.EDITOR_FAILED,
                    handoff=handoff,
                    writer_result=writer_result,
                    editorial_report=report,
                    failure_detail=f"Editor LOCAL_REPAIR failed: {type(error).__name__}: {error}",
                )
            candidate_id = repaired.draft_id
            candidate_text_artifact = repaired.text_artifact
            try:
                repair_verification = await self._editorial.review_repaired(
                    review_input,
                    report,
                    repaired,
                    _derived_request(writer_request, "editor-repair-review"),
                )
            except Exception as error:
                return WriterIntegrationResult(
                    status=WriterIntegrationStatus.EDITOR_FAILED,
                    handoff=handoff,
                    writer_result=writer_result,
                    editorial_report=report,
                    repaired_draft=repaired,
                    failure_detail=(
                        f"Editor repaired-candidate REVIEW failed: {type(error).__name__}: {error}"
                    ),
                )
            if repair_verification.verdict is not EditorialVerdict.PASS:
                return WriterIntegrationResult(
                    status=(
                        WriterIntegrationStatus.REWRITE_REQUIRED
                        if repair_verification.verdict is EditorialVerdict.MAJOR_REWRITE
                        else WriterIntegrationStatus.EDITOR_FAILED
                    ),
                    handoff=handoff,
                    writer_result=writer_result,
                    editorial_report=report,
                    repaired_draft=repaired,
                    repair_verification_report=repair_verification,
                    failure_detail=(
                        "repaired candidate did not pass the single verification review: "
                        f"{repair_verification.verdict.value}"
                    ),
                )

        try:
            hints = self._read_sidecar(draft.sidecar_artifact)
            observation = await self._observation(
                candidate_id,
                candidate_text_artifact,
                curator_observation,
            )
            reconciliation = self._reconciliation.reconcile(
                candidate_id,
                hints.declared_memory_hints,
                observation,
            )
        except Exception as error:
            return WriterIntegrationResult(
                status=WriterIntegrationStatus.RECONCILIATION_FAILED,
                handoff=handoff,
                writer_result=writer_result,
                editorial_report=report,
                repaired_draft=repaired,
                repair_verification_report=repair_verification,
                failure_detail=f"Reconciliation failed: {type(error).__name__}: {error}",
            )
        return WriterIntegrationResult(
            status=WriterIntegrationStatus.COMPLETED,
            handoff=handoff,
            writer_result=writer_result,
            editorial_report=report,
            repaired_draft=repaired,
            repair_verification_report=repair_verification,
            curator_observation=observation,
            reconciliation=reconciliation,
        )

    async def run(
        self,
        request: WriterContextHandoffRequest,
        writer_request: ModelRequest,
        *,
        curator_observation: CuratorObservation | None = None,
    ) -> WriterIntegrationResult:
        """Alias used by offline runners and callers that prefer a workflow verb."""

        return await self.execute(
            request,
            writer_request,
            curator_observation=curator_observation,
        )

    def _read_sidecar(self, artifact: ArtifactRef) -> WriterSidecar:
        return WriterSidecar.model_validate_json(self._artifacts.read_verified(artifact))

    async def _observation(
        self,
        draft_id: ArtifactId,
        text_artifact: ArtifactRef,
        supplied: CuratorObservation | None,
    ) -> CuratorObservation:
        if supplied is not None:
            if supplied.draft_id != draft_id:
                raise ReconciliationError("Curator observation belongs to another Draft")
            return supplied
        if self._curator_observer is None:
            raise ReconciliationError("no independent Curator observation was supplied")
        value = self._curator_observer(draft_id, text_artifact)
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, CuratorObservation):
            raise ReconciliationError("Curator observer returned an invalid observation")
        if value.draft_id != draft_id:
            raise ReconciliationError("Curator observer returned an observation for another Draft")
        return value


def _convert_item(
    item: PackageContextItem,
    section: WriterContextSection,
    source_commit: CommitId,
    snapshot_id: StableId,
) -> GenerationContextItem:
    return GenerationContextItem(
        item_id=item.context_item_id,
        category=section.value,
        text=item.claim,
        source_commit=source_commit,
        snapshot_id=snapshot_id,
        information_label="writer_safe",
        truth_class=item.validity.value,
        support_status="verified" if item.validity.value != "uncertain" else "uncertain",
        mandatory=item.mandatory,
    )


def _unique_artifacts(artifacts: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
    indexed: dict[ArtifactId, ArtifactRef] = {}
    for artifact in artifacts:
        prior = indexed.get(artifact.artifact_id)
        if prior is not None and prior != artifact:
            raise WriterContextHandoffError(
                "one Writer input artifact identity has conflicting metadata",
                code="ARTIFACT_BASIS_MISMATCH",
            )
        indexed[artifact.artifact_id] = artifact
    return tuple(indexed.values())


def _stable_id(prefix: str, value: object) -> StableId:
    digest = content_id(value).root.removeprefix("sha256:")
    return StableId(f"{prefix}.{digest[:96]}"[:128])


def _derived_request(request: ModelRequest, label: str) -> ModelRequest:
    digest = content_id({"request": request.request_id.root, "label": label}).root.removeprefix(
        "sha256:"
    )
    return request.model_copy(
        update={
            "request_id": StableId(f"request.stage3.{label}.{digest[:48]}"),
            "trace_id": f"{request.trace_id}:{label}",
            "prompt": f"Stage 3 integration {label}; replaced by the sealed agent.",
        }
    )


__all__ = [
    "CONTEXT_SNAPSHOT_MEDIA_TYPE",
    "CuratorObserver",
    "WriterContextHandoff",
    "WriterContextHandoffAdapter",
    "WriterContextHandoffError",
    "WriterDraftIntegrationService",
    "WriterIntegrationResult",
    "WriterIntegrationStatus",
]
