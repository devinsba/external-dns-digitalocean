import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.urllib import URLLibInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s: %(message)s "
    "trace_id=%(otelTraceID)s span_id=%(otelSpanID)s"
)


def setup() -> None:
    """Call before any logging. Export is opt-in via OTEL_EXPORTER_OTLP_ENDPOINT."""
    os.environ.setdefault("OTEL_SERVICE_NAME", "external-dns-digitalocean")
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
    URLLibInstrumentor().instrument()
    # Adds otelTraceID/otelSpanID to every record (via the record factory).
    LoggingInstrumentor().instrument(set_logging_format=False, inject_trace_context=True)
