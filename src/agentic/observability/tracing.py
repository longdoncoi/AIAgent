"""Distributed Tracing integration with OpenTelemetry and LangSmith."""

import functools
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from src.agentic.core.config import Settings, get_settings, is_strict_env
from src.agentic.observability.langsmith_tracer import setup_langsmith
from src.agentic.observability.metrics import OBSERVABILITY_ERRORS_TOTAL

logger = logging.getLogger(__name__)


def configure_tracing(settings: Settings | None = None) -> TracerProvider | None:
    """Configure the process tracer once and fail closed in strict environments."""
    cfg = settings or get_settings()
    # LangSmith setup is independent of who configured the OTel provider.
    setup_langsmith(cfg)
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return None

    resource = Resource.create({"service.name": "agentic-platform"})
    provider = TracerProvider(resource=resource)
    if cfg.enable_tracing:
        # OTLP always wins over the debug console. In production an endpoint is
        # mandatory; console export must never silently replace a real backend.
        if cfg.otel_exporter_otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                otlp_exporter = OTLPSpanExporter(
                    endpoint=cfg.otel_exporter_otlp_endpoint,
                    insecure=cfg.otel_exporter_insecure,
                )
                provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
                logger.info("Initialized OTLPSpanExporter to %s", cfg.otel_exporter_otlp_endpoint)
            except Exception as exc:
                OBSERVABILITY_ERRORS_TOTAL.labels(component="otel_otlp").inc()
                logger.error("OTLPSpanExporter unavailable: %s", exc)
                if is_strict_env(cfg):
                    raise RuntimeError(
                        "Production tracing requires a working OTLP exporter: "
                        "install opentelemetry-exporter-otlp and verify the collector."
                    ) from exc
        elif cfg.debug and not is_strict_env(cfg):
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
            logger.warning("Using ConsoleSpanExporter; traces are NOT sent to a backend")
        elif is_strict_env(cfg):
            OBSERVABILITY_ERRORS_TOTAL.labels(component="otel_no_exporter").inc()
            logger.error("Production tracing has no exporter configured")
            raise RuntimeError("Production tracing requires an OTLP exporter endpoint.")
    trace.set_tracer_provider(provider)
    return provider


configure_tracing()


def get_tracer(name: str = "agentic-platform") -> trace.Tracer:
    """Returns an OpenTelemetry tracer."""
    return trace.get_tracer(name)


@asynccontextmanager
async def trace_span(
    name: str, attributes: dict[str, Any] | None = None
) -> AsyncIterator[trace.Span]:
    """Async context manager for tracing a specific code block."""
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as span:
        if attributes:
            for key, value in attributes.items():
                if value is not None:
                    span.set_attribute(key, str(value))
        yield span


def traced_operation(span_name: str) -> Callable[..., Any]:
    """Decorator to automatically wrap async functions with an OpenTelemetry span."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()
            with tracer.start_as_current_span(span_name) as span:
                span.set_attribute("function.name", func.__name__)
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(trace.StatusCode.ERROR, str(exc))
                    raise

        return wrapper

    return decorator


def inject_trace_context(carrier: dict[str, str]) -> None:
    """Injects current active span traceparent into carrier dictionary."""
    current_span = trace.get_current_span()
    if current_span.is_recording():
        TraceContextTextMapPropagator().inject(carrier)


def extract_trace_context(carrier: Any) -> Any:
    """Extracts OpenTelemetry context from request headers/carrier."""
    return TraceContextTextMapPropagator().extract(carrier=carrier)
