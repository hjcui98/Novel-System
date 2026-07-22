"""OpenTelemetry-compatible tracing boundary."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

AttributeValue = str | bool | int | float


@dataclass(frozen=True, slots=True)
class TelemetrySpan:
    trace_id: str
    span_id: str
    carrier: dict[str, str]


class TelemetryPort(Protocol):
    def span(
        self,
        name: str,
        carrier: dict[str, str],
        attributes: dict[str, AttributeValue],
    ) -> AbstractContextManager[TelemetrySpan]: ...
