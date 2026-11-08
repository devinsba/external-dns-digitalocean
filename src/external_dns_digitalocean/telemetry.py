import logging
import os

import structlog
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def _add_trace(_, __, event: dict) -> dict:
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event["trace_id"] = f"{ctx.trace_id:032x}"
        event["span_id"] = f"{ctx.span_id:016x}"
    return event


def configure_logging(level: str = "INFO") -> None:
    """JSON logs to stderr for structlog and stdlib (uvicorn, OTel SDK) records alike."""
    shared = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        _add_trace,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    handler = logging.StreamHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)


def setup() -> None:
    """Export is opt-in via OTEL_EXPORTER_OTLP_ENDPOINT."""
    os.environ.setdefault("OTEL_SERVICE_NAME", "external-dns-digitalocean")
    # Stable semconv: requests emits http.client.request.duration (s), not legacy ms.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        # Exporters and Resource read the standard OTEL_* env vars themselves. The SDK
        # registers atexit shutdown hooks that flush both providers on sys.exit.
        tp = TracerProvider(resource=Resource.create())
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tp)
        reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        metrics.set_meter_provider(
            MeterProvider(resource=Resource.create(), metric_readers=[reader])
        )
    RequestsInstrumentor().instrument()
    SystemMetricsInstrumentor().instrument()
