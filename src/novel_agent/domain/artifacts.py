"""Content-addressed artifacts and the five canonical root references."""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, SchemaVersion


class RootKind(StrEnum):
    TEXT = "text"
    PLAN = "plan"
    WORLD = "world"
    REFERENCE = "reference"
    PROJECT_PROFILE = "project_profile"


MODEL_RAW_RESPONSE_MEDIA_TYPE = "application/vnd.novel-agent.model.raw-response+json"
EVALUATION_ARTIFACT_MEDIA_TYPE_PREFIX = "application/vnd.novel-agent.evaluation."
CONTEXT_WRITER_RESPONSE_MEDIA_TYPE = (
    "application/vnd.novel-agent.evaluation.context-writer-response+json"
)
QA_WRITER_RESPONSE_MEDIA_TYPE = "application/vnd.novel-agent.evaluation.qa-writer-response+json"
CONTEXT_WRITER_READOUT_RECORD_MEDIA_TYPE = (
    "application/vnd.novel-agent.evaluation.context-writer-readout+json"
)
QA_WRITER_READOUT_RECORD_MEDIA_TYPE = (
    "application/vnd.novel-agent.evaluation.qa-writer-readout+json"
)
V05_FAKE_CAMPAIGN_RECEIPT_MEDIA_TYPE = (
    "application/vnd.novel-agent.evaluation.v05-fake-campaign-receipt+json"
)
V05_READOUT_CAMPAIGN_MANIFEST_MEDIA_TYPE = (
    "application/vnd.novel-agent.evaluation.v05-readout-campaign-manifest+json"
)
V05_READOUT_FREEZE_RECEIPT_MEDIA_TYPE = (
    "application/vnd.novel-agent.evaluation.v05-readout-freeze-receipt+json"
)
PRODUCTION_ASSEMBLY_ATTESTATION_MEDIA_TYPE = (
    "application/vnd.novel-agent.production-assembly-attestation+json"
)
EFFECTIVE_BUDGET_MEDIA_TYPE = "application/vnd.novel-agent.effective-budget+json"
WRITER_JUDGE_RECEIPT_MEDIA_TYPE = "application/vnd.novel-agent.evaluation.writer-judge-receipt+json"
WRITER_JUDGE_INPUT_MEDIA_TYPE = "application/vnd.novel-agent.evaluation.writer-judge-input+json"
WRITER_JUDGE_OUTPUT_MEDIA_TYPE = "application/vnd.novel-agent.evaluation.writer-judge-output+json"
EVALUATION_NAMESPACE_DISCARD_MEDIA_TYPE = (
    "application/vnd.novel-agent.evaluation.namespace-discard+json"
)
DURABLE_EVIDENCE_REPORT_MEDIA_TYPE = (
    "application/vnd.novel-agent.evaluation.durable-evidence-report+json"
)
U4S_SEED_READOUT_REPORT_MEDIA_TYPE = (
    "application/vnd.novel-agent.evaluation.u4s-seed-readout-report+json"
)


def is_evaluation_artifact_media_type(media_type: str) -> bool:
    return media_type.startswith(EVALUATION_ARTIFACT_MEDIA_TYPE_PREFIX)


class ArtifactRef(DomainModel):
    artifact_id: ArtifactId
    media_type: str = Field(min_length=1, max_length=255)
    byte_length: int = Field(ge=0)
    schema_version: SchemaVersion


class TextRootRef(ArtifactRef):
    root_kind: Literal[RootKind.TEXT] = RootKind.TEXT


class PlanRootRef(ArtifactRef):
    root_kind: Literal[RootKind.PLAN] = RootKind.PLAN


class WorldRootRef(ArtifactRef):
    root_kind: Literal[RootKind.WORLD] = RootKind.WORLD


class ReferenceRootRef(ArtifactRef):
    root_kind: Literal[RootKind.REFERENCE] = RootKind.REFERENCE


class ProjectProfileRootRef(ArtifactRef):
    root_kind: Literal[RootKind.PROJECT_PROFILE] = RootKind.PROJECT_PROFILE


class RootManifest(DomainModel):
    project_id: ProjectId
    schema_version: SchemaVersion
    text_root: TextRootRef
    plan_root: PlanRootRef
    world_root: WorldRootRef
    reference_root: ReferenceRootRef
    project_profile_root: ProjectProfileRootRef
    parent_commit_ids: tuple[CommitId, ...] = ()
