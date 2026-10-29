"""Tracing configuration: a no-op when disabled, recording when enabled."""

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.config import Settings
from app.observability.tracing import configure_tracing, get_tracer, shutdown_tracing


def test_disabled_tracing_configures_nothing() -> None:
    settings = Settings(environment="test", tracing_enabled=False)

    configure_tracing(settings, "retriv-test")

    span = get_tracer("test").start_span("idle")
    try:
        assert not span.is_recording()
    finally:
        span.end()


def test_enabled_tracing_records_spans_with_attributes() -> None:
    exporter = InMemorySpanExporter()
    settings = Settings(environment="test", tracing_enabled=True)

    try:
        configure_tracing(
            settings,
            "retriv-test",
            span_processor=SimpleSpanProcessor(exporter),
        )
        with get_tracer("test").start_as_current_span("unit", attributes={"retriv.k": "v"}):
            pass

        finished = exporter.get_finished_spans()
        assert len(finished) == 1
        assert finished[0].name == "unit"
        attributes = finished[0].attributes
        assert attributes is not None
        assert attributes["retriv.k"] == "v"
        resource_attributes = finished[0].resource.attributes
        assert resource_attributes is not None
        assert resource_attributes["service.name"] == "retriv-test"
    finally:
        shutdown_tracing()


def test_shutdown_without_configuration_is_harmless() -> None:
    shutdown_tracing()
