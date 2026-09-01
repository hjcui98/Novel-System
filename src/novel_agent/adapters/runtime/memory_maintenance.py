"""Thin Stage 5 adapter for durable Stage 2 memory maintenance."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId
from novel_agent.domain.memory_write import (
    CuratorWorldProposalInput,
    MaintenanceTrigger,
    MemoryGapClassification,
    MemoryRepairFinding,
    MemoryWriteBudget,
    MemoryWriteCommitProfile,
    MemoryWriteWorkflowRequest,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
    SourceVisibilityReceipt,
    semantic_id,
)
from novel_agent.domain.runtime import TaskKind, TaskPurpose, TaskRecord
from novel_agent.domain.stage2 import ContractRef
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.memory_write_workflow import LocalMemoryWriteWorkflow

FINDING_MEDIA_TYPE = "application/vnd.novel-agent.memory-repair-finding+json"
WORKFLOW_RESULT_MEDIA_TYPE = "application/vnd.novel-agent.memory-write-workflow-result+json"


@dataclass(frozen=True, slots=True)
class MemoryMaintenancePolicy:
    """Pinned contracts shared by the production memory-write owner."""

    curator_agent_spec: ContractRef
    tool_policy_ref: ContractRef
    repair_policy_ref: ContractRef
    configuration_fingerprint: ArtifactId
    boundary_policy_ref: ContractRef
    prompt_contract_refs: tuple[ContractRef, ...] = ()
    skill_contract_refs: tuple[ContractRef, ...] = ()
    budget: MemoryWriteBudget = field(default_factory=MemoryWriteBudget)
    # Isolated U8-C evidence can validate a candidate without granting this
    # maintenance invocation Canon commit authority.  The default remains the
    # production canonical-commit path.
    validation_only: bool = False


class MemoryMaintenanceAdapter:
    """Map a durable finding to the existing LocalMemoryWriteWorkflow only."""

    is_fixture = False

    def __init__(
        self,
        *,
        workflow: LocalMemoryWriteWorkflow,
        commits: CommitService,
        artifacts: ArtifactRepository,
        policy: MemoryMaintenancePolicy,
    ) -> None:
        self._workflow = workflow
        self._commits = commits
        self._artifacts = artifacts
        self._policy = policy

    async def run(
        self, task: TaskRecord, finding: MemoryRepairFinding
    ) -> MemoryWriteWorkflowResult:
        self._validate_task_and_find_finding(task, finding)
        manifest = self._commits.load_manifest(task.basis_commit)
        if manifest.project_id != task.project_id:
            raise ValueError("maintenance basis manifest belongs to another project")
        visibility = tuple(
            self._read_visibility(reference) for reference in finding.source_visibility_receipt_refs
        )
        for source, receipt in zip(finding.source_artifact_refs, visibility, strict=True):
            if (
                receipt.source_artifact != source
                or receipt.boundary_id != finding.information_boundary.boundary_id
                or receipt.access_scope is not finding.access_scope
            ):
                raise ValueError("maintenance visibility receipt is not bound to the finding")
        try:
            request_id = semantic_id("memory-write", finding.finding_id.root)
        except ValueError:
            # The complete finding identity remains in the request payload and
            # finding artifact.  Use the existing planner task/attempt scope
            # only when the readable finding-scoped surface exceeds StableId.
            try:
                request_id = semantic_id(
                    "memory-write",
                    finding.planner_task_id.root,
                    finding.planner_attempt_id.root,
                )
            except ValueError:
                # A maximum-length planner task can also overflow the
                # composite fallback. The finding itself is an existing
                # globally addressable identity, so preserve it rather than
                # collapsing multiple findings in one run onto one request.
                request_id = finding.finding_id
        resume_checkpoint = self._resume_checkpoint(task)
        request = MemoryWriteWorkflowRequest(
            request_id=request_id,
            run_id=task.run_id,
            task_id=task.task_id,
            project_id=task.project_id,
            trigger=MaintenanceTrigger(
                maintenance_task_id=task.task_id,
                chapter_indices=(finding.source_chapter_indices or (finding.cutoff.chapter_index,)),
            ),
            commit_profile=MemoryWriteCommitProfile.REQUIRE_CANONICAL_COMMIT,
            validation_only=self._policy.validation_only,
            base_commit=task.basis_commit,
            source_artifacts=finding.source_artifact_refs,
            root_update_intents=(),
            world_mutation=CuratorWorldProposalInput(
                curator_agent_spec=self._policy.curator_agent_spec
            ),
            canonical_root_refs=manifest,
            information_boundary=finding.information_boundary,
            source_visibility_receipts=visibility,
            access_scope=finding.access_scope,
            source_provenance=tuple(receipt.provenance for receipt in visibility),
            configuration_fingerprint=self._policy.configuration_fingerprint,
            prompt_contract_refs=self._policy.prompt_contract_refs,
            skill_contract_refs=self._policy.skill_contract_refs,
            tool_policy_ref=self._policy.tool_policy_ref,
            repair_policy_ref=self._policy.repair_policy_ref,
            repair_owner=finding.repair_owner,
            # The production maintenance policy is the owner of the workflow
            # budget.  ``MemoryRepairFinding.budget`` is an evidence payload
            # emitted by Planner and defaults to the domain baseline; using it
            # here would silently discard a campaign-local settlement
            # override before the workflow ever reaches its admission gate.
            budget=self._policy.budget,
            idempotency_key=request_id,
            resume_checkpoint=resume_checkpoint,
            repair_query=finding.semantic_question,
            source_evidence_requirement=finding.source_evidence_requirement,
        )
        return await self._workflow.execute(request)

    def _resume_checkpoint(self, task: TaskRecord) -> ArtifactRef | None:
        result_refs = tuple(
            reference
            for reference in task.terminal_artifact_refs
            if reference.media_type == WORKFLOW_RESULT_MEDIA_TYPE
        )
        if not result_refs:
            return None
        if len(result_refs) != 1:
            raise ValueError("maintenance task carries multiple workflow result artifacts")
        result = MemoryWriteWorkflowResult.model_validate_json(
            self._artifacts.read_verified(result_refs[0]), strict=False
        )
        if (
            result.status is MemoryWriteWorkflowStatus.FATAL
            and result.checkpoint_ref is not None
            and any(
                code.startswith("1 validation error for StableId")
                and "input_value='validation.bundle." in code
                for code in result.terminal_codes
            )
        ):
            prepare = getattr(self._workflow, "prepare_validation_retry", None)
            if not callable(prepare):
                raise ValueError(
                    "validation-id fatal result requires a workflow validation-retry owner"
                )
            return cast(
                ArtifactRef,
                prepare(
                    result.checkpoint_ref,
                    task_id=task.task_id,
                    run_id=task.run_id,
                    base_commit=task.basis_commit,
                ),
            )
        if result.status not in {
            MemoryWriteWorkflowStatus.SUSPENDED,
            MemoryWriteWorkflowStatus.HUMAN_REQUIRED,
        }:
            raise ValueError("maintenance retry cannot resume a non-resumable workflow result")
        if result.checkpoint_ref is None:
            raise ValueError("resumable maintenance result has no checkpoint")
        return result.checkpoint_ref

    def _validate_task_and_find_finding(
        self, task: TaskRecord, finding: MemoryRepairFinding
    ) -> ArtifactRef:
        if task.kind is not TaskKind.MAINTENANCE:
            raise ValueError("memory maintenance requires a MAINTENANCE task")
        if task.purpose is not TaskPurpose.DERIVED_MAINTENANCE:
            raise ValueError("memory maintenance requires a derived maintenance task")
        if finding.classification is not MemoryGapClassification.CANON_EXTRACTION_GAP:
            raise ValueError("memory maintenance only accepts CANON_EXTRACTION_GAP")
        if (
            task.run_id != finding.planner_run_id
            or task.project_id != finding.project_id
            or task.basis_commit != finding.base_commit
            or task.basis_snapshot != finding.basis_snapshot_id
        ):
            raise ValueError("maintenance task and finding basis do not match")
        refs = tuple(
            reference
            for reference in task.input_artifact_refs
            if reference.media_type == FINDING_MEDIA_TYPE
        )
        if len(refs) != 1:
            raise ValueError("maintenance task must carry exactly one finding artifact")
        reference = refs[0]
        stored = MemoryRepairFinding.model_validate_json(
            self._artifacts.read_verified(reference), strict=False
        )
        if stored != finding:
            raise ValueError("maintenance task finding artifact differs from supplied finding")
        return reference

    def _read_visibility(self, reference: ArtifactRef) -> SourceVisibilityReceipt:
        return SourceVisibilityReceipt.model_validate_json(
            self._artifacts.read_verified(reference), strict=True
        )


__all__ = [
    "FINDING_MEDIA_TYPE",
    "WORKFLOW_RESULT_MEDIA_TYPE",
    "MemoryMaintenanceAdapter",
    "MemoryMaintenancePolicy",
]
