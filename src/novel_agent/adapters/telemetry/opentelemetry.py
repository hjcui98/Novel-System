"""OpenTelemetry tracing adapter and local OTLP configuration."""

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import propagate
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Tracer

from novel_agent.ports.telemetry import AttributeValue, TelemetrySpan


class OpenTelemetryAdapter:
    def __init__(self, tracer: Tracer) -> None:
        self._tracer = tracer

    @contextmanager
    def span(
        self,
        name: str,
        carrier: dict[str, str],
        attributes: dict[str, AttributeValue],
    ) -> Iterator[TelemetrySpan]:
        parent_context = propagate.extract(carrier)
        with self._tracer.start_as_current_span(name, context=parent_context) as span:
            for key, value in attributes.items():
                span.set_attribute(key, value)
            next_carrier: dict[str, str] = {}
            propagate.inject(next_carrier)
            context = span.get_span_context()
            yield TelemetrySpan(
                trace_id=format(context.trace_id, "032x"),
                span_id=format(context.span_id, "016x"),
                carrier=next_carrier,
            )


def build_otlp_telemetry(
    endpoint: str, *, service_name: str = "novel-agent"
) -> tuple[OpenTelemetryAdapter, TracerProvider]:
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    tracer = provider.get_tracer("novel_agent")
    return OpenTelemetryAdapter(tracer), provider
