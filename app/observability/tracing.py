"""OpenTelemetry tracing for the API and the worker.

Off by default: with ``tracing_enabled`` unset nothing is configured and
spans are no-ops. When enabled, spans export over OTLP/HTTP to a Jaeger
endpoint, and the instrumentors cover FastAPI requests, SQLAlchemy
queries, httpx clients and boto3 without per-call-site code. The worker
adds a handful of manual spans around the pipeline stages; see
``app.processing.worker``.

The extraction sandbox subprocess is deliberately never traced: it has
no network. Its cost is visible in the worker's ``extract`` span.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from app.core.config import Settings

_provider: TracerProvider | None = None


def configure_tracing(
    settings: Settings,
    service_name: str,
    *,
    app: Any = None,
    engine: Any = None,
    span_processor: SpanProcessor | None = None,
) -> None:
    """Install the provider and instrumentors, unless tracing is disabled.

    ``span_processor`` replaces the default OTLP batch processor so tests
    can hand in an in-memory exporter. The call is idempotent per process
    and must happen before the first request or run.
    """
    global _provider
    if not settings.tracing_enabled:
        return
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "deployment.environment": settings.environment,
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(settings.tracing_sample_ratio)),
    )
    provider.add_span_processor(
        span_processor or BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.tracing_endpoint))
    )
    trace.set_tracer_provider(provider)
    _provider = provider

    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    if app is not None:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
    HTTPXClientInstrumentor().instrument()
    BotocoreInstrumentor().instrument()
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


def get_tracer(name: str) -> trace.Tracer:
    """A tracer under the configured provider; a no-op when disabled."""
    return trace.get_tracer(name)


def shutdown_tracing() -> None:
    """Flush and release the provider so trailing spans are not lost."""
    global _provider
    if _provider is None:
        return
    _provider.shutdown()
    _provider = None
