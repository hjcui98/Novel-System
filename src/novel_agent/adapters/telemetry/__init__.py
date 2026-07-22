"""Telemetry adapters."""

from novel_agent.adapters.telemetry.opentelemetry import (
    OpenTelemetryAdapter,
    build_otlp_telemetry,
)

__all__ = ["OpenTelemetryAdapter", "build_otlp_telemetry"]
