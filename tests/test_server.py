import http.client
import json
import logging
import threading

import pytest
from opentelemetry.trace import SpanKind, StatusCode

from external_dns_digitalocean.digitalocean import DigitalOceanClient
from external_dns_digitalocean.provider import Provider
from external_dns_digitalocean.server import make_health_server, make_provider_server

MEDIA = "application/external.dns.webhook+json;version=1"


class StubProvider:
    def __init__(self, filters=(), fail=False):
        self.filters = list(filters)
        self.fail = fail
        self.applied = []

    def domain_filter(self):
        return self.filters

    def records(self):
        if self.fail:
            raise RuntimeError("boom")
        return [{"dnsName": "a.example.com", "targets": ["1.1.1.1"], "recordType": "A"}]

    def apply(self, changes):
        if self.fail:
            raise RuntimeError("boom")
        self.applied.append(changes)


def serve(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def start():
    servers = []

    def _start(make, *args):
        srv = serve(make(*args, "127.0.0.1", 0))
        servers.append(srv)
        return srv.server_port

    yield _start
    for s in servers:
        s.shutdown()
        s.server_close()


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = body if isinstance(body, bytes) else None if body is None else json.dumps(body)
    conn.request(method, path, body=data)
    r = conn.getresponse()
    raw = r.read()
    return r.status, r.getheader("Content-Type"), raw


def test_negotiate_without_filter(start):
    port = start(make_provider_server, StubProvider())
    assert call(port, "GET", "/") == (200, MEDIA, b"{}")


def test_negotiate_with_filter(start):
    port = start(make_provider_server, StubProvider(["example.com", "foo.org"]))
    status, ctype, raw = call(port, "GET", "/")
    assert (status, ctype) == (200, MEDIA)
    assert json.loads(raw) == {"include": ["example.com", "foo.org"]}


def test_get_records(start):
    port = start(make_provider_server, StubProvider())
    status, ctype, raw = call(port, "GET", "/records")
    assert (status, ctype) == (200, MEDIA)
    assert json.loads(raw) == [
        {"dnsName": "a.example.com", "targets": ["1.1.1.1"], "recordType": "A"}
    ]


def test_apply_changes_204(start):
    p = StubProvider()
    port = start(make_provider_server, p)
    changes = {"create": [{"dnsName": "a.example.com"}], "updateOld": None}
    assert call(port, "POST", "/records", changes) == (204, None, b"")
    assert p.applied == [changes]


def test_adjust_endpoints_echoes(start):
    port = start(make_provider_server, StubProvider())
    eps = [{"dnsName": "a.example.com", "targets": ["x"], "recordType": "A", "labels": {"k": "v"}}]
    status, ctype, raw = call(port, "POST", "/adjustendpoints", eps)
    assert (status, ctype) == (200, MEDIA)
    assert json.loads(raw) == eps


@pytest.mark.parametrize("path", ["/records", "/adjustendpoints"])
def test_bad_json_400(start, path):
    port = start(make_provider_server, StubProvider())
    assert call(port, "POST", path, b"{nope")[0] == 400


def test_unknown_path_404_and_wrong_method_400(start):
    port = start(make_provider_server, StubProvider())
    assert call(port, "GET", "/nope")[0] == 404
    assert call(port, "POST", "/")[0] == 400
    assert call(port, "DELETE", "/records")[0] == 400
    assert call(port, "GET", "/adjustendpoints")[0] == 400


def test_provider_error_500(start):
    port = start(make_provider_server, StubProvider(fail=True))
    assert call(port, "GET", "/records")[0] == 500
    assert call(port, "POST", "/records", {"create": []})[0] == 500


def test_healthz(start):
    port = start(make_health_server)
    assert call(port, "GET", "/healthz")[0] == 200
    assert call(port, "GET", "/other")[0] == 404


def test_end_to_end_with_fake_do(start, make_fake_do):
    fake = make_fake_do({"example.com": []})
    provider = Provider(DigitalOceanClient("tok", fake.url), [])
    port = start(make_provider_server, provider)
    changes = {
        "create": [
            {
                "dnsName": "app.example.com",
                "targets": ["1.2.3.4"],
                "recordType": "A",
                "recordTTL": 60,
            },
            {
                "dnsName": "a-app.example.com",
                "targets": ['"heritage=external-dns"'],
                "recordType": "TXT",
            },
        ]
    }
    assert call(port, "POST", "/records", changes)[0] == 204
    status, ctype, raw = call(port, "GET", "/records")
    assert (status, ctype) == (200, MEDIA)
    assert json.loads(raw) == [
        {"dnsName": "app.example.com", "targets": ["1.2.3.4"], "recordType": "A", "recordTTL": 60},
        {
            "dnsName": "a-app.example.com",
            "targets": ['"heritage=external-dns"'],
            "recordType": "TXT",
            "recordTTL": 300,
        },
    ]


@pytest.mark.parametrize("exc", [ValueError("x"), json.JSONDecodeError("bad", "", 0)])
def test_value_error_from_provider_is_500(start, exc):
    class P(StubProvider):
        def apply(self, changes):
            raise exc

        def records(self):
            raise exc

    port = start(make_provider_server, P())
    assert call(port, "POST", "/records", {"create": []})[0] == 500
    assert call(port, "GET", "/records")[0] == 500


# --- telemetry -------------------------------------------------------------


def test_server_span_for_get_records(start, spans):
    port = start(make_provider_server, StubProvider())
    call(port, "GET", "/records")
    (s,) = spans.get_finished_spans()
    assert s.name == "GET /records"
    assert s.kind == SpanKind.SERVER
    assert dict(s.attributes) == {
        "http.request.method": "GET",
        "url.path": "/records",
        "http.route": "/records",
        "http.response.status_code": 200,
    }


def test_unknown_path_span_name_has_no_path(start, spans):
    port = start(make_provider_server, StubProvider())
    call(port, "GET", "/nope/123")
    (s,) = spans.get_finished_spans()
    assert s.name == "GET"
    assert "http.route" not in s.attributes
    assert s.attributes["http.response.status_code"] == 404


def test_provider_error_marks_span_error(start, spans):
    port = start(make_provider_server, StubProvider(fail=True))
    call(port, "GET", "/records")
    (s,) = spans.get_finished_spans()
    assert s.status.status_code == StatusCode.ERROR
    assert s.attributes["http.response.status_code"] == 500
    assert [e.name for e in s.events] == ["exception"]


def test_bad_request_is_not_span_error(start, spans):
    port = start(make_provider_server, StubProvider())
    call(port, "POST", "/records", b"{nope")
    (s,) = spans.get_finished_spans()
    assert s.attributes["http.response.status_code"] == 400
    assert s.status.status_code != StatusCode.ERROR


@pytest.mark.parametrize("header", ["traceparent", "Traceparent"])
def test_incoming_traceparent_continues_trace(start, spans, header):
    port = start(make_provider_server, StubProvider())
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    tid = "0af7651916cd43dd8448eb211c80319c"
    conn.request("GET", "/records", headers={header: f"00-{tid}-b7ad6b7169203331-01"})
    conn.getresponse().read()
    (s,) = spans.get_finished_spans()
    assert f"{s.context.trace_id:032x}" == tid
    assert f"{s.parent.span_id:016x}" == "b7ad6b7169203331"


def test_healthz_has_no_span(start, spans):
    port = start(make_health_server)
    call(port, "GET", "/healthz")
    assert spans.get_finished_spans() == ()


def test_log_line_carries_trace_id(start, spans, caplog):
    class Logging(StubProvider):
        def records(self):
            logging.getLogger("t").warning("inside")
            return []

    caplog.set_level(logging.INFO)
    port = start(make_provider_server, Logging())
    call(port, "GET", "/records")
    (s,) = spans.get_finished_spans()
    rec = next(r for r in caplog.records if r.getMessage() == "inside")
    assert rec.otelTraceID == f"{s.context.trace_id:032x}"
    assert rec.otelSpanID == f"{s.context.span_id:016x}"


def test_e2e_do_calls_are_client_child_spans(start, make_fake_do, spans):
    fake = make_fake_do({"example.com": []})
    port = start(make_provider_server, Provider(DigitalOceanClient("tok", fake.url), []))
    call(port, "GET", "/records")
    finished = spans.get_finished_spans()
    server = next(s for s in finished if s.kind == SpanKind.SERVER)
    clients = [s for s in finished if s.kind == SpanKind.CLIENT]
    assert clients
    assert {c.context.trace_id for c in clients} == {server.context.trace_id}
