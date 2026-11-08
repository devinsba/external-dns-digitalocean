import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import Counter, MeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from external_dns_digitalocean import telemetry

# Global OTel providers can only be set once per process.
SPAN_EXPORTER = InMemorySpanExporter()
METRIC_READER = InMemoryMetricReader(preferred_temporality={Counter: AggregationTemporality.DELTA})
_tp = TracerProvider()
_tp.add_span_processor(SimpleSpanProcessor(SPAN_EXPORTER))
trace.set_tracer_provider(_tp)
metrics.set_meter_provider(MeterProvider(metric_readers=[METRIC_READER]))
telemetry.setup()  # no endpoint set: leaves our providers alone, instruments requests
telemetry.configure_logging("INFO")


@pytest.fixture(autouse=True)
def spans():
    SPAN_EXPORTER.clear()
    METRIC_READER.get_metrics_data()  # drain deltas from earlier tests
    return SPAN_EXPORTER


@pytest.fixture
def counter_points():
    def get() -> dict[tuple, int]:
        data = METRIC_READER.get_metrics_data()
        out: dict[tuple, int] = {}
        for rm in data.resource_metrics if data else []:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    if m.name == "dns.record.changes":
                        for dp in m.data.data_points:
                            a = dp.attributes
                            out[(a["action"], a["record_type"], a["zone"])] = dp.value
        return out

    return get


class FakeDO:
    """In-memory DigitalOcean DNS API served over real HTTP."""

    def __init__(self, zones: dict[str, list[dict]], page_size: int = 200):
        self.zones = zones
        self.page_size = page_size
        self.requests: list[tuple[str, str, dict[str, str], object]] = []
        self.fail_with: int | None = None
        self.next_id = 1000
        self.url = ""


def _make_handler(fake: FakeDO):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, body: object | None = None):
            self.send_response(status)
            if body is not None:
                data = json.dumps(body).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_header("Content-Length", "0")
                self.end_headers()

        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            url = urlparse(self.path)
            fake.requests.append((self.command, self.path, dict(self.headers), body))
            if fake.fail_with:
                return self._send(fake.fail_with, {"id": "boom", "message": "kaboom"})
            parts = (
                url.path.removeprefix("/v2").strip("/").split("/")
            )  # domains[/zone/records[/id]]
            q = parse_qs(url.query)
            if parts[0] != "domains":
                return self._send(404, {})
            if len(parts) == 1 and self.command == "GET":
                return self._page("domains", [{"name": z} for z in fake.zones], url, q)
            zone = parts[1]
            if zone not in fake.zones or parts[2:3] != ["records"]:
                return self._send(404, {})
            records = fake.zones[zone]
            if len(parts) == 3 and self.command == "GET":
                return self._page("domain_records", records, url, q)
            if len(parts) == 3 and self.command == "POST":
                fake.next_id += 1
                rec = {"id": fake.next_id, **body}
                records.append(rec)
                return self._send(201, {"domain_record": rec})
            rid = int(parts[3])
            idx = next(i for i, r in enumerate(records) if r["id"] == rid)
            if self.command == "PUT":
                records[idx] = {"id": rid, **body}
                return self._send(200, {"domain_record": records[idx]})
            if self.command == "DELETE":
                del records[idx]
                return self._send(204)
            self._send(405, {})

        def _page(self, key, items, url, q):
            page = int(q.get("page", ["1"])[0])
            size = min(int(q.get("per_page", ["200"])[0]), fake.page_size)
            chunk = items[(page - 1) * size : page * size]
            out: dict = {key: chunk, "links": {}}
            if page * size < len(items):
                nxt = f"{fake.url}{url.path.removeprefix('/v2')}?page={page + 1}&per_page={size}"
                out["links"] = {"pages": {"next": nxt}}
            self._send(200, out)

        do_GET = do_POST = do_PUT = do_DELETE = _handle

    return Handler


@pytest.fixture
def make_fake_do():
    servers = []

    def make(zones: dict[str, list[dict]] | None = None, page_size: int = 200) -> FakeDO:
        fake = FakeDO(zones if zones is not None else {}, page_size)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(fake))
        fake.url = f"http://127.0.0.1:{srv.server_port}/v2"
        servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return fake

    yield make
    for s in servers:
        s.shutdown()
        s.server_close()


@pytest.fixture(autouse=True)
def _no_system_proxy(monkeypatch):
    # macOS system proxy settings would otherwise slow/break loopback requests.
    monkeypatch.setenv("http_proxy", "http://unused.invalid:1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
