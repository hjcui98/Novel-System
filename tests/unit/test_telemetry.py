from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from novel_agent.adapters.telemetry import OpenTelemetryAdapter, build_otlp_telemetry


def test_adapter_propagates_parent_trace_and_records_attributes() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = OpenTelemetryAdapter(provider.get_tracer("telemetry-test"))

    with (
        telemetry.span("parent", {}, {"answer": 42}) as parent,
        telemetry.span("child", parent.carrier, {"enabled": True}) as child,
    ):
        assert child.trace_id == parent.trace_id
        assert child.span_id != parent.span_id

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert spans[1].attributes == {"answer": 42}
    assert spans[0].attributes == {"enabled": True}
    provider.shutdown()


def test_otlp_builder_configures_provider_without_exporting() -> None:
    telemetry, provider = build_otlp_telemetry("http://127.0.0.1:4317", service_name="stage0-test")

    assert isinstance(telemetry, OpenTelemetryAdapter)
    assert isinstance(provider, TracerProvider)
    provider.shutdown()
