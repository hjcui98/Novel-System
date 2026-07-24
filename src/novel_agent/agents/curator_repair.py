"""Audited Curator repair facade for the Stage 2W workflow."""

from __future__ import annotations

from pydantic import ValidationError

from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import ObservedChangeSet
from novel_agent.domain.ids import CommitId, SchemaVersion
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.memory_write import (
    CandidateRevision,
    RepairDirective,
    ValidationDecision,
)
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    CuratorEvidenceContract,
)
from novel_agent.ports.memory_write import (
    CuratorRepairRejectedError,
    CuratorRepairRequest,
    CuratorRepairResult,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.model_curation import (
    ModelCurationContractError,
    ModelCurator,
)


class CuratorRepairContractError(CuratorRepairRejectedError):
    """The model returned a repair outside the trusted repair scope."""


class CuratorRepairAgent:
    """Run a bounded repair while keeping candidate materialization trusted.

    Under the v2 evidence contract the repair is evidence-only: the model
    picks replacement candidate IDs and cannot rewrite the record payload.
    Under the legacy v1 contract the repair regenerates the full draft but
    is still scope-checked against the parent operations.
    """

    def __init__(
        self,
        curator: ModelCurator,
        runner: StructuredAgentRunner,
        *,
        evidence_contract: CuratorEvidenceContract = CuratorEvidenceContract.CANDIDATE_ID_V2,
    ) -> None:
        self._curator = curator
        self._runner = runner
        self._evidence_contract = evidence_contract

    @property
    def evidence_contract(self) -> CuratorEvidenceContract:
        return self._evidence_contract

    async def run(
        self,
        *,
        version: SchemaVersion,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        current_world: WorldRootDocument,
        parent_candidate: CandidateRevision,
        parent_changes: ObservedChangeSet,
        validation: ValidationDecision | None,
        directive: RepairDirective,
        request: CuratorRepairRequest,
        model_request: ModelRequest,
    ) -> CuratorRepairResult:
        self._validate_parent(request, parent_candidate, directive, base_commit)
        if self._evidence_contract is CuratorEvidenceContract.CANDIDATE_ID_V2:
            return await self._run_v2(
                version=version,
                text_root=text_root,
                chapter_index=chapter_index,
                base_commit=base_commit,
                parent_candidate=parent_candidate,
                parent_changes=parent_changes,
                directive=directive,
                request=request,
                model_request=model_request,
            )
        return await self._run_v1(
            version=version,
            text_root=text_root,
            chapter_index=chapter_index,
            base_commit=base_commit,
            current_world=current_world,
            parent_candidate=parent_candidate,
            parent_changes=parent_changes,
            validation=validation,
            directive=directive,
            request=request,
            model_request=model_request,
        )

    async def _run_v2(
        self,
        *,
        version: SchemaVersion,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        parent_candidate: CandidateRevision,
        parent_changes: ObservedChangeSet,
        directive: RepairDirective,
        request: CuratorRepairRequest,
        model_request: ModelRequest,
    ) -> CuratorRepairResult:
        scope = directive.allowed_scope
        if scope.operation_ids:
            wanted = set(scope.operation_ids)
            repair_indexes = tuple(
                index
                for index, operation in enumerate(parent_changes.operations)
                if operation.operation_id in wanted
            )
        else:
            repair_indexes = ()
        prepared = self._runner.prepare(
            AgentType.MEMORY_CURATOR,
            AgentMode.CURATOR_REPAIR,
            version.root,
            model_request,
            (
                "Evidence-only repair: output EvidenceRepairDraft items. "
                "Choose replacement evidence_candidate_ids only; "
                "do NOT rewrite target_id, record_kind, record payload, or operation type.\n"
                f"PARENT_CANDIDATE={parent_candidate.model_dump_json()}\n"
                f"REPAIR_DIRECTIVE={directive.model_dump_json()}\n"
                f"REPAIR_OPERATION_INDEXES={repair_indexes}\n"
            ),
            source_hashes=(text_root.root_hash,),
            base_commit=base_commit,
        )
        try:
            changes, call, drafts = await self._curator.evidence_repair_v2(
                text_root,
                chapter_index,
                base_commit,
                parent_changes,
                prepared.request,
                contract_prompt=prepared.rendered_prompt,
                repair_operation_indexes=repair_indexes,
            )
        except ModelCurationContractError as error:
            raise CuratorRepairContractError(
                str(error),
                reason_code="CURATOR_REPAIR_DOMAIN_REJECTED",
            ) from error
        self._validate_evidence_only(changes, parent_changes, parent_candidate, directive)
        output_bytes = canonical_json_bytes(changes.model_dump(mode="json"))
        output_artifact = ArtifactRef(
            artifact_id=sha256_id(output_bytes),
            media_type="application/vnd.novel-agent.curator-repair-observed-changes+json",
            byte_length=len(output_bytes),
            schema_version=version,
        )
        unresolved = tuple(
            f"evidence_repair:{draft.operation_index}:{draft.action.value}"
            for draft in drafts
            if draft.action.value != "replace_evidence"
        )
        receipt = self._runner.receipt(
            prepared,
            call,
            output_artifacts=(output_artifact,),
            unresolved=unresolved,
        )
        receipt_bytes = canonical_json_bytes(receipt.model_dump(mode="json"))
        producer_receipt = ArtifactRef(
            artifact_id=sha256_id(receipt_bytes),
            media_type="application/vnd.novel-agent.curator-repair-receipt+json",
            byte_length=len(receipt_bytes),
            schema_version=version,
        )
        return CuratorRepairResult(
            observed_changes=changes,
            agent_receipt=receipt,
            producer_receipt=producer_receipt,
            candidate_artifact=output_artifact,
            applied_directive_ids=(directive.directive_id,),
        )

    async def _run_v1(
        self,
        *,
        version: SchemaVersion,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        current_world: WorldRootDocument,
        parent_candidate: CandidateRevision,
        parent_changes: ObservedChangeSet,
        validation: ValidationDecision | None,
        directive: RepairDirective,
        request: CuratorRepairRequest,
        model_request: ModelRequest,
    ) -> CuratorRepairResult:
        constraints = self._prompt_constraints(parent_changes, directive)
        prepared = self._runner.prepare(
            AgentType.MEMORY_CURATOR,
            AgentMode.CURATOR_REPAIR,
            version.root,
            model_request,
            (
                "Output only ChapterChangeDraft. The trusted service will bind all IDs "
                "and EvidenceRef values.\n"
                f"PARENT_CANDIDATE={parent_candidate.model_dump_json()}\n"
                f"PARENT_CHANGES={parent_changes.model_dump_json()}\n"
                f"VALIDATION={None if validation is None else validation.model_dump_json()}\n"
                f"REPAIR_DIRECTIVE={directive.model_dump_json()}\n"
                f"REPAIR_CONSTRAINTS={canonical_json_bytes(constraints).decode('utf-8')}\n"
                "Return exactly one operation for every immutable parent target. Copy every "
                "target_id exactly. Do not add, remove, merge, or rename targets. Only fields "
                "listed in allowed_field_paths may change. For evidence-only repair, preserve "
                "the operation type and typed record verbatim and change only the cited span. "
                "Do not use future evidence or change the canonical base commit."
            ),
            source_hashes=(text_root.root_hash,),
            base_commit=base_commit,
        )
        try:
            changes, call, draft = await self._curator.extract_reported(
                text_root,
                chapter_index,
                base_commit,
                current_world,
                prepared.request,
                contract_prompt=prepared.rendered_prompt,
            )
        except ValidationError as error:
            raise CuratorRepairContractError(
                "repair output failed the structured domain schema",
                reason_code="CURATOR_REPAIR_SCHEMA_REJECTED",
            ) from error
        except ModelCurationContractError as error:
            raise CuratorRepairContractError(
                str(error),
                reason_code="CURATOR_REPAIR_DOMAIN_REJECTED",
            ) from error
        self._validate_scope(changes, parent_candidate, parent_changes, directive)
        output_bytes = canonical_json_bytes(changes.model_dump(mode="json"))
        output_artifact = ArtifactRef(
            artifact_id=sha256_id(output_bytes),
            media_type="application/vnd.novel-agent.curator-repair-observed-changes+json",
            byte_length=len(output_bytes),
            schema_version=version,
        )
        receipt = self._runner.receipt(
            prepared,
            call,
            output_artifacts=(output_artifact,),
            unresolved=draft.unresolved,
        )
        receipt_bytes = canonical_json_bytes(receipt.model_dump(mode="json"))
        producer_receipt = ArtifactRef(
            artifact_id=sha256_id(receipt_bytes),
            media_type="application/vnd.novel-agent.curator-repair-receipt+json",
            byte_length=len(receipt_bytes),
            schema_version=version,
        )
        return CuratorRepairResult(
            observed_changes=changes,
            agent_receipt=receipt,
            producer_receipt=producer_receipt,
            candidate_artifact=output_artifact,
            applied_directive_ids=(directive.directive_id,),
        )

    @staticmethod
    def _prompt_constraints(
        parent_changes: ObservedChangeSet,
        directive: RepairDirective,
    ) -> dict[str, object]:
        scope = directive.allowed_scope
        allowed_operation_ids = set(scope.operation_ids)
        return {
            "allowed_field_paths": scope.field_paths,
            "allow_identity_rebind": scope.allow_identity_rebind,
            "allow_operation_type_change": scope.allow_operation_type_change,
            "allow_successor_creation": scope.allow_successor_creation,
            "immutable_target_ids": (
                ()
                if scope.allow_identity_rebind
                else tuple(operation.target_id.root for operation in parent_changes.operations)
            ),
            "operations": tuple(
                {
                    "operation_id": operation.operation_id.root,
                    "target_id": operation.target_id.root,
                    "operation_type": operation.operation.value,
                    "selected_for_repair": (
                        not allowed_operation_ids or operation.operation_id in allowed_operation_ids
                    ),
                }
                for operation in parent_changes.operations
            ),
        }

    @staticmethod
    def _validate_parent(
        request: CuratorRepairRequest,
        parent: CandidateRevision,
        directive: RepairDirective,
        base_commit: CommitId,
    ) -> None:
        if request.parent_candidate != parent:
            raise CuratorRepairContractError("repair request parent candidate was changed")
        if parent.base_commit != base_commit or request.basis.commit_id != base_commit:
            raise CuratorRepairContractError("repair request base commit is not canonical")
        if directive.action.value != "curator_repair":
            raise CuratorRepairContractError("repair Agent requires a curator-repair directive")

    @staticmethod
    def _validate_evidence_only(
        changes: ObservedChangeSet,
        parent_changes: ObservedChangeSet,
        parent: CandidateRevision,
        directive: RepairDirective,
    ) -> None:
        """V2 repair must preserve record payloads; only evidence_refs may change."""
        if changes.base_commit != parent.base_commit:
            raise CuratorRepairContractError("repair changed the candidate base commit")
        parent_payloads = {
            operation.target_id: CuratorRepairAgent._payload_without_evidence(operation.payload)
            for operation in parent_changes.operations
        }
        for operation in changes.operations:
            original = parent_payloads.get(operation.target_id)
            if original is None:
                raise CuratorRepairContractError(
                    "evidence-only repair introduced a new target"
                )
            if original != CuratorRepairAgent._payload_without_evidence(operation.payload):
                raise CuratorRepairContractError(
                    "evidence-only repair changed immutable record content"
                )

    @staticmethod
    def _validate_scope(
        changes: ObservedChangeSet,
        parent: CandidateRevision,
        parent_changes: ObservedChangeSet,
        directive: RepairDirective,
    ) -> None:
        if changes.base_commit != parent.base_commit:
            raise CuratorRepairContractError("repair changed the candidate base commit")
        allowed_operation_ids = set(directive.allowed_scope.operation_ids)
        parent_targets = {operation.target_id for operation in parent_changes.operations}
        repaired_targets = {operation.target_id for operation in changes.operations}
        if not directive.allowed_scope.allow_identity_rebind and repaired_targets != parent_targets:
            raise CuratorRepairContractError(
                "repair output must preserve the complete immutable parent target set"
            )
        parent_by_target = {
            operation.target_id: operation for operation in parent_changes.operations
        }
        repaired_by_target = {operation.target_id: operation for operation in changes.operations}
        if not directive.allowed_scope.allow_operation_type_change:
            changed_types = {
                target
                for target in parent_targets & repaired_targets
                if parent_by_target[target].operation != repaired_by_target[target].operation
            }
            if changed_types:
                raise CuratorRepairContractError(
                    "repair output changed an operation type without authority"
                )
        evidence_paths = {"evidence_refs", "record.evidence_refs"}
        allowed_fields = set(directive.allowed_scope.field_paths)
        if allowed_fields and allowed_fields.issubset(evidence_paths):
            changed_records = {
                target
                for target in parent_targets & repaired_targets
                if CuratorRepairAgent._payload_without_evidence(parent_by_target[target].payload)
                != CuratorRepairAgent._payload_without_evidence(repaired_by_target[target].payload)
            }
            if changed_records:
                raise CuratorRepairContractError(
                    "evidence-only repair changed immutable record content"
                )
        if allowed_operation_ids:
            allowed_targets = {
                operation.target_id
                for operation in parent_changes.operations
                if operation.operation_id in allowed_operation_ids
            }
            unexpected = {
                operation.target_id
                for operation in changes.operations
                if operation.target_id not in allowed_targets
            }
            if unexpected:
                raise CuratorRepairContractError(
                    "repair output contains targets outside the allowed operation scope"
                )

    @staticmethod
    def _payload_without_evidence(payload: object) -> object:
        if not isinstance(payload, dict):
            return payload
        result = dict(payload)
        record = result.get("record")
        if isinstance(record, dict):
            record_without_evidence = dict(record)
            record_without_evidence.pop("evidence_refs", None)
            result["record"] = record_without_evidence
        return result


__all__ = ["CuratorRepairAgent", "CuratorRepairContractError"]
