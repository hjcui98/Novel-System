"""Text ownership and exact evidence addressing contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, StableId


class RangeUnit(StrEnum):
    UNICODE_CODEPOINT = "unicode_codepoint"


class EvidenceSupportStatus(StrEnum):
    CURRENT = "current_support"
    HISTORICAL = "historical_support"
    SUPERSEDED = "superseded_support"
    ORPHANED = "orphaned"
    CONTRADICTED = "contradicted"


class QuoteHash(ArtifactId):
    pass


class TextBlock(DomainModel):
    block_id: StableId
    chapter_id: StableId
    scene_id: StableId | None = None
    narrative_index: int = Field(ge=0)
    text: str


class TextSpanRef(DomainModel):
    block_id: StableId
    range_unit: RangeUnit = RangeUnit.UNICODE_CODEPOINT
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> TextSpanRef:
        if self.end < self.start:
            raise ValueError("span end must be greater than or equal to start")
        return self


class EvidenceRef(DomainModel):
    evidence_id: StableId
    root_hash: ArtifactId
    object_hash: ArtifactId
    chapter_id: StableId | None = None
    scene_id: StableId | None = None
    span: TextSpanRef | None = None
    quote_hash: QuoteHash | None = None
    support_status: EvidenceSupportStatus
    resolved_at_commit: CommitId
