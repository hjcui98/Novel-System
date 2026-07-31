"""Scrubbed r35 Stage 2M regression facts; contains no prose, Gold, or future data."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LegacyOversizedCase:
    case_id: str
    checkpoint: int
    selected_units: int
    mandatory_entries: int
    mandatory_tokens: int
    configured_tokens: int


LEGACY_OVERSIZED_CASES = (
    LegacyOversizedCase("ZTJ-P004", 80, 206, 258, 36_069, 4_000),
    LegacyOversizedCase("ZTJ-P005", 95, 251, 305, 42_309, 4_000),
)
