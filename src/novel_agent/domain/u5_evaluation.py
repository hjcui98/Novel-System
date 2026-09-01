"""U5-A evidence for the disposable C20 benchmark side channel."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import CommitId, ProjectId, RunId, StableId
from novel_agent.domain.v05_readout import MemoryIdentitySnapshot


class U5EvaluationTaskEvidence(DomainModel):
    task_id: StableId
    track: Literal["novelmem_qa", "novelmem_context"]
    information_profile: Literal["visible_at_cutoff", "author_plan_conditioned"]
    evaluation_task_identity: StableId
    basis_commit_id: CommitId
    freeze_receipt_id: StableId
    writer_status: Literal["SCHEMA_VALID", "TYPED_FAILURE"]
    response_ref: ArtifactRef | None = None
    readout_record_ref: ArtifactRef | None = None
    raw_response_ref: ArtifactRef | None = None


class U5C20EvaluationIsolationReport(DomainModel):
    """Frozen U5-A proof that three Writer readouts did not touch C20 state."""

    report_schema: Literal["u5-c20-evaluation-isolation.v1"] = Field(
        default="u5-c20-evaluation-isolation.v1",
        alias="schema",
    )
    run_id: RunId
    project_id: ProjectId
    basis_commit: CommitId
    memory_identity_before: MemoryIdentitySnapshot
    memory_identity_after: MemoryIdentitySnapshot
    tasks: tuple[U5EvaluationTaskEvidence, ...]
    discard_receipt_ref: ArtifactRef
    canonical_commit_count_before: int = Field(ge=0)
    canonical_commit_count_after: int = Field(ge=0)
    runtime_task_count_before: int = Field(ge=0)
    runtime_task_count_after: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    evaluation_artifact_count: int = Field(ge=1)
    c21_request_path: str = Field(min_length=1)
    c21_private_fields_absent: Literal[True] = True
    status: Literal["COMPLETED", "REVIEW_REQUIRED"]

    @model_validator(mode="after")
    def validate_isolation(self) -> U5C20EvaluationIsolationReport:
        if len(self.tasks) != 3:
            raise ValueError("U5-A requires exactly three evaluation Writer tasks")
        task_ids = tuple(task.task_id for task in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("U5-A evaluation task identities must be unique")
        if sum(task.track == "novelmem_qa" for task in self.tasks) != 1:
            raise ValueError("U5-A requires one Track A QA task")
        contexts = tuple(task for task in self.tasks if task.track == "novelmem_context")
        if {task.information_profile for task in contexts} != {
            "visible_at_cutoff",
            "author_plan_conditioned",
        }:
            raise ValueError("U5-A requires history-only and APC Context tasks")
        if self.memory_identity_before != self.memory_identity_after:
            raise ValueError("U5-A evaluation must preserve Memory identity")
        if self.basis_commit != self.memory_identity_before.commit_id:
            raise ValueError("U5-A basis does not match the frozen Memory identity")
        if self.canonical_commit_count_before != self.canonical_commit_count_after:
            raise ValueError("U5-A evaluation changed canonical Commit count")
        if self.runtime_task_count_before != self.runtime_task_count_after:
            raise ValueError("U5-A evaluation created runtime tasks")
        if self.status == "COMPLETED" and any(
            task.writer_status != "SCHEMA_VALID" for task in self.tasks
        ):
            raise ValueError("completed U5-A report cannot contain typed Writer failures")
        return self


__all__ = ["U5C20EvaluationIsolationReport", "U5EvaluationTaskEvidence"]
