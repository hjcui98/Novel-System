"""Framework-independent base configuration for immutable domain values."""

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Base for all Stage 0 domain contracts."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
