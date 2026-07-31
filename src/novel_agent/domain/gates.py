"""Strict evidence contracts for deterministic Stage 2 retrieval gates."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import CommitId, ProjectId, StableId
from novel_agent.domain.retrieval_routing import L2IndexKind, RetrievalBackendProfile


class Stage2RetrievalGateR1Counts(DomainModel):
    records: int = Field(ge=0)
    entity_associations: int = Field(ge=0)
    relation_edges: int = Field(ge=0)


class Stage2RetrievalCheckpointEvidence(DomainModel):
    checkpoint: int = Field(ge=1)
    source_commit: CommitId
    snapshot_id: StableId | None
    r1_counts: Stage2RetrievalGateR1Counts
    index_targets: dict[L2IndexKind, str]
    index_totals: dict[L2IndexKind, int]
    failures: tuple[str, ...]
    passed: bool

    @model_validator(mode="after")
    def validate_result(self) -> Stage2RetrievalCheckpointEvidence:
        required = {L2IndexKind.ANCHOR, L2IndexKind.GROUNDED}
        if self.passed != (not self.failures):
            raise ValueError("checkpoint pass status must agree with failures")
        if self.passed and self.snapshot_id is None:
            raise ValueError("passed checkpoint requires an exact snapshot")
        if self.passed and (
            set(self.index_targets) != required or set(self.index_totals) != required
        ):
            raise ValueError("passed checkpoint requires Anchor and Grounded index evidence")
        if any(not target for target in self.index_targets.values()):
            raise ValueError("checkpoint index targets must be non-empty")
        return self


class Stage2RetrievalGateReport(DomainModel):
    status: Literal["passed", "failed"]
    project_id: ProjectId
    retrieval_backend_profile: RetrievalBackendProfile
    checkpoints: tuple[Stage2RetrievalCheckpointEvidence, ...]

    @model_validator(mode="after")
    def validate_report(self) -> Stage2RetrievalGateReport:
        chapters = tuple(item.checkpoint for item in self.checkpoints)
        if chapters != tuple(sorted(set(chapters))):
            raise ValueError("retrieval gate checkpoints must be unique and ascending")
        passed = bool(self.checkpoints) and all(item.passed for item in self.checkpoints)
        if self.status != ("passed" if passed else "failed"):
            raise ValueError("retrieval gate status must agree with checkpoint evidence")
        return self
