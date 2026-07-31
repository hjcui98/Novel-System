"""Versioned canonical-value receipts shared by retrieval and writer-context contracts."""

from __future__ import annotations

from pydantic import Field, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import StableId


class CanonicalAliasReceipt(DomainModel):
    """Predicate-scoped proof that a versioned registry equates two raw values."""

    receipt_id: StableId
    registry_version: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    raw_values: tuple[str, str]
    canonical_value_id: StableId
    canonicalizer_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_values(self) -> CanonicalAliasReceipt:
        if not all(item.strip() for item in self.raw_values):
            raise ValueError("canonical alias receipt raw values cannot be empty")
        if self.raw_values[0] == self.raw_values[1]:
            raise ValueError("canonical alias receipt requires distinct raw values")
        return self


__all__ = ["CanonicalAliasReceipt"]
