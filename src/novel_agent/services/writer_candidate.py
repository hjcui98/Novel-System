"""Materialize a DRAFT_READY Writer turn as an immutable candidate-only Draft."""

from __future__ import annotations

import hashlib

from novel_agent.domain.agent_context import AgentContextView
from novel_agent.domain.generation import (
    DraftArtifact,
    WriterArtifactBasis,
    WriterContextItem,
    WriterContextSnapshot,
    WriterSidecar,
    WriterTurnAction,
    WriterWorkPlanResult,
    WritingLoopRequest,
)
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    ContractRef,
    ExecutionStatus,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.writer_cognition import WriterTurnResult

WRITER_VIEW_MEDIA_TYPE = "application/vnd.novel-agent.agent-context-view+json"
WRITER_DRAFT_TEXT_MEDIA_TYPE = "application/vnd.novel-agent.draft-text+plain"
WRITER_SIDECAR_MEDIA_TYPE = "application/vnd.novel-agent.writer-sidecar+json"


class WriterCandidateError(ValueError):
    """A Writer turn cannot form a candidate-only DraftArtifact."""


class WriterCandidateMaterializer:
    def __init__(
        self,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion,
    ) -> None:
        self._artifacts = artifacts
        self._schema_version = schema_version

    def materialize(
        self,
        request: WritingLoopRequest,
        view: AgentContextView,
        plan: WriterWorkPlanResult,
        turn: WriterTurnResult,
        *,
        mode: AgentMode,
        parent_draft: DraftArtifact | None = None,
    ) -> DraftArtifact:
        output = turn.output
        if output.action is not WriterTurnAction.DRAFT_READY or output.draft_text is None:
            raise WriterCandidateError("only DRAFT_READY can form a DraftArtifact")
        if mode is AgentMode.DRAFT and parent_draft is not None:
            raise WriterCandidateError("initial Draft cannot have a parent")
        if mode is AgentMode.MAJOR_REWRITE and parent_draft is None:
            raise WriterCandidateError("major rewrite requires a parent Draft")
        context_ref = self._artifacts.put(
            canonical_json_bytes(view.model_dump(mode="json")),
            WRITER_VIEW_MEDIA_TYPE,
            self._schema_version,
        )
        text_ref = self._artifacts.put(
            output.draft_text.encode("utf-8"),
            WRITER_DRAFT_TEXT_MEDIA_TYPE,
            self._schema_version,
        )
        sidecar = WriterSidecar(
            declared_memory_hints=output.declared_memory_hints,
            unresolved_questions=output.unresolved_questions,
            self_observations=output.self_observations,
        )
        sidecar_ref = self._artifacts.put(
            canonical_json_bytes(sidecar.model_dump(mode="json")),
            WRITER_SIDECAR_MEDIA_TYPE,
            self._schema_version,
        )
        context_id = StableId(f"agent-context.{view.context_hash.root[-64:]}")
        basis = WriterArtifactBasis(
            project_id=request.project_id,
            base_commit=request.base_commit,
            snapshot_id=request.snapshot_id,
            context_id=context_id,
            context_artifact=context_ref,
            context_fingerprint=context_ref.artifact_id,
            writing_contract_artifact=request.writing_task_artifact,
            plan_artifact=request.accepted_plan.artifact,
            project_profile_artifact=request.project_profile_artifact,
            configuration_fingerprint=request.writer_configuration_fingerprint,
            model_configuration_fingerprint=request.model_configuration_fingerprint,
            future_isolation_attestation=request.future_isolation_attestation,
        )
        call = turn.model_call
        output_artifacts = (text_ref, sidecar_ref, turn.raw_output_artifact)
        receipt = AgentExecutionReceipt(
            receipt_id=StableId(
                "writer-receipt."
                + hashlib.sha256(
                    f"{request.run_id.root}\0{call.request_id.root}\0{mode.value}".encode()
                ).hexdigest()
            ),
            run_id=request.run_id,
            task_id=request.task_id,
            agent_spec=ContractRef(
                contract_id=StableId("agent.writer.turn"),
                version=self._schema_version,
                content_hash=request.writer_configuration_fingerprint,
            ),
            agent_type=AgentType.WRITER,
            agent_mode=mode,
            prompt_fingerprint=content_id(
                {
                    "view": view.context_hash.root,
                    "work_plan": plan.work_plan_artifact.artifact_id.root,
                    "mode": mode.value,
                }
            ),
            configuration_fingerprint=request.writer_configuration_fingerprint,
            base_commit=request.base_commit,
            input_artifacts=(
                request.writing_task_artifact,
                request.accepted_plan.artifact,
                request.writer_context_package_artifact,
                context_ref,
                plan.work_plan_artifact,
            ),
            output_artifacts=output_artifacts,
            skill_receipts=plan.skill_receipts,
            model_call_ids=(call.request_id,),
            unresolved=output.unresolved_questions,
            status=ExecutionStatus.SUCCEEDED,
            started_at=call.started_at,
            completed_at=call.completed_at,
            latency_ms=call.latency_ms,
        )
        draft_id = content_id(
            {
                "kind": "stage3-draft-candidate.v2",
                "mode": mode.value,
                "basis": basis.model_dump(mode="json"),
                "text": text_ref.model_dump(mode="json"),
                "sidecar": sidecar_ref.model_dump(mode="json"),
                "raw": turn.raw_output_artifact.model_dump(mode="json"),
                "parent": parent_draft.draft_id.root if parent_draft is not None else None,
            }
        )
        return DraftArtifact(
            draft_id=draft_id,
            mode=mode,
            basis=basis,
            text_artifact=text_ref,
            sidecar_artifact=sidecar_ref,
            raw_output_artifact=turn.raw_output_artifact,
            parent_draft_id=parent_draft.draft_id if parent_draft is not None else None,
            writer_receipt=receipt,
            model_call_ids=(call.request_id,),
            model_call_record=call,
            created_at=call.completed_at,
        )

    @staticmethod
    def editor_context(
        request: WritingLoopRequest,
        view: AgentContextView,
    ) -> WriterContextSnapshot:
        items = (
            *view.protected_items,
            *view.active_memory_items,
            *view.working_items,
            *view.compacted_prefix_items,
            *view.recent_settled_tail,
        )
        return WriterContextSnapshot(
            context_id=StableId(f"agent-context.{view.context_hash.root[-64:]}"),
            base_commit=request.base_commit,
            snapshot_id=request.snapshot_id,
            task_contract=request.writing_task.contract_id.root,
            items=tuple(
                WriterContextItem(
                    item_id=item.item_id,
                    category=item.kind.value,
                    text=item.content,
                    source_commit=request.base_commit,
                    snapshot_id=request.snapshot_id,
                    information_label=item.information_scope,
                    truth_class="runtime" if item.information_scope == "runtime" else "visible",
                    support_status="verified",
                    mandatory=item.mandatory,
                )
                for item in items
            ),
            unresolved_gaps=tuple(item.root for item in view.unresolved_need_ids),
            budget_report=view.token_report,
        )


__all__ = [
    "WRITER_DRAFT_TEXT_MEDIA_TYPE",
    "WRITER_SIDECAR_MEDIA_TYPE",
    "WRITER_VIEW_MEDIA_TYPE",
    "WriterCandidateError",
    "WriterCandidateMaterializer",
]
