import json
import subprocess
import sys

CODE = """
import logging
from opentelemetry import trace
from external_dns_digitalocean import telemetry
telemetry.setup()
tp = trace.get_tracer_provider()
from opentelemetry.sdk.trace import TracerProvider
print(isinstance(tp, TracerProvider))
if isinstance(tp, TracerProvider):
    print(tp.resource.attributes["service.name"])
telemetry.configure_logging("INFO")
logging.getLogger("x").info("hello")
"""


def run(**env):
    r = subprocess.run(
        [sys.executable, "-c", CODE],
        env={"PATH": "", "OTEL_EXPORTER_OTLP_TIMEOUT": "1", **env},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    return r


def test_noop_without_endpoint():
    r = run()
    assert r.stdout.splitlines()[0] == "False"
    log = json.loads(r.stderr.splitlines()[-1])  # stdlib records come out as JSON too
    assert (log["event"], log["level"], log["logger"]) == ("hello", "info", "x")
    assert "trace_id" not in log  # no active span


def test_sdk_with_endpoint_and_default_service_name():
    r = run(OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:1")
    assert r.stdout.splitlines()[:2] == ["True", "external-dns-digitalocean"]


def test_service_name_env_wins():
    r = run(OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:1", OTEL_SERVICE_NAME="custom")
    assert r.stdout.splitlines()[:2] == ["True", "custom"]
