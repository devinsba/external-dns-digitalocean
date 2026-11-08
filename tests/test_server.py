import json
import logging

import pytest
import structlog
from fastapi.testclient import TestClient
from opentelemetry.trace import SpanKind, StatusCode

from external_dns_digitalocean.digitalocean import DigitalOceanClient
from external_dns_digitalocean.provider import Provider
from external_dns_digitalocean.server import make_health_app, make_provider_app

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


@pytest.fixture
def start():
    clients = []

    def _start(make, *args):
        c = TestClient(make(*args), raise_server_exceptions=False)
        clients.append(c)
        return c

    yield _start
    for c in clients:
        c.close()


def call(client, method, path, body=None):
    kw = {} if body is None else {"content": body if isinstance(body, bytes) else json.dumps(body)}
    r = client.request(method, path, **kw)
    return r.status_code, r.headers.get("content-type"), r.content


def test_negotiate_without_filter(start):
    client = start(make_provider_app, StubProvider())
    assert call(client, "GET", "/") == (200, MEDIA, b"{}")


def test_negotiate_with_filter(start):
    client = start(make_provider_app, StubProvider(["example.com", "foo.org"]))
    status, ctype, raw = call(client, "GET", "/")
    assert (status, ctype) == (200, MEDIA)
    assert json.loads(raw) == {"include": ["example.com", "foo.org"]}


def test_get_records(start):
    client = start(make_provider_app, StubProvider())
    status, ctype, raw = call(client, "GET", "/records")
    assert (status, ctype) == (200, MEDIA)
    assert json.loads(raw) == [
        {"dnsName": "a.example.com", "targets": ["1.1.1.1"], "recordType": "A"}
    ]


def test_apply_changes_204(start):
    p = StubProvider()
    client = start(make_provider_app, p)
    changes = {"create": [{"dnsName": "a.example.com"}], "updateOld": None}
    assert call(client, "POST", "/records", changes) == (204, None, b"")
    assert p.applied == [changes]


def test_adjust_endpoints_echoes(start):
    client = start(make_provider_app, StubProvider())
    eps = [{"dnsName": "a.example.com", "targets": ["x"], "recordType": "A", "labels": {"k": "v"}}]
    status, ctype, raw = call(client, "POST", "/adjustendpoints", eps)
    assert (status, ctype) == (200, MEDIA)
    assert json.loads(raw) == eps


@pytest.mark.parametrize("path", ["/records", "/adjustendpoints"])
def test_bad_json_400(start, path):
    client = start(make_provider_app, StubProvider())
    assert call(client, "POST", path, b"{nope")[0] == 400


def test_unknown_path_404_and_wrong_method_405(start):
    client = start(make_provider_app, StubProvider())
    assert call(client, "GET", "/nope")[0] == 404
    assert call(client, "POST", "/")[0] == 405
    assert call(client, "DELETE", "/records")[0] == 405
    assert call(client, "GET", "/adjustendpoints")[0] == 405


def test_provider_error_500(start):
    client = start(make_provider_app, StubProvider(fail=True))
    assert call(client, "GET", "/records")[0] == 500
    assert call(client, "POST", "/records", {"create": []})[0] == 500


def test_healthz(start):
    client = start(make_health_app)
    assert call(client, "GET", "/healthz")[0] == 200
    assert call(client, "GET", "/other")[0] == 404


def test_end_to_end_with_fake_do(start, make_fake_do):
    fake = make_fake_do({"example.com": []})
    provider = Provider(DigitalOceanClient("tok", fake.url), [])
    client = start(make_provider_app, provider)
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
    assert call(client, "POST", "/records", changes)[0] == 204
    status, ctype, raw = call(client, "GET", "/records")
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

    client = start(make_provider_app, P())
    assert call(client, "POST", "/records", {"create": []})[0] == 500
    assert call(client, "GET", "/records")[0] == 500


# --- telemetry -------------------------------------------------------------


def _server_span(spans):
    # FastAPI instrumentation also emits INTERNAL asgi send/receive spans.
    (s,) = [s for s in spans.get_finished_spans() if s.kind == SpanKind.SERVER]
    return s


def test_server_span_for_get_records(start, spans):
    client = start(make_provider_app, StubProvider())
    call(client, "GET", "/records")
    s = _server_span(spans)
    assert s.name == "GET /records"
    assert s.kind == SpanKind.SERVER
    assert s.attributes["http.route"] == "/records"
    assert s.attributes["http.response.status_code"] == 200


def test_unknown_path_span_name_has_no_path(start, spans):
    client = start(make_provider_app, StubProvider())
    call(client, "GET", "/nope/123")
    s = _server_span(spans)
    assert s.name == "GET"
    assert "http.route" not in s.attributes
    assert s.attributes["http.response.status_code"] == 404


def test_provider_error_marks_span_error(start, spans):
    client = start(make_provider_app, StubProvider(fail=True))
    call(client, "GET", "/records")
    s = _server_span(spans)
    assert s.status.status_code == StatusCode.ERROR
    assert s.attributes["http.response.status_code"] == 500
    assert [e.name for e in s.events] == ["exception"]


def test_bad_request_is_not_span_error(start, spans):
    client = start(make_provider_app, StubProvider())
    call(client, "POST", "/records", b"{nope")
    s = _server_span(spans)
    assert s.attributes["http.response.status_code"] == 400
    assert s.status.status_code != StatusCode.ERROR


@pytest.mark.parametrize("header", ["traceparent", "Traceparent"])
def test_incoming_traceparent_continues_trace(start, spans, header):
    client = start(make_provider_app, StubProvider())
    tid = "0af7651916cd43dd8448eb211c80319c"
    client.get("/records", headers={header: f"00-{tid}-b7ad6b7169203331-01"})
    s = _server_span(spans)
    assert f"{s.context.trace_id:032x}" == tid
    assert f"{s.parent.span_id:016x}" == "b7ad6b7169203331"


def test_healthz_has_no_span(start, spans):
    client = start(make_health_app)
    call(client, "GET", "/healthz")
    assert spans.get_finished_spans() == ()


def test_log_line_carries_trace_id(start, spans, caplog):
    class Logging(StubProvider):
        def records(self):
            structlog.get_logger("t").warning("inside")
            return []

    caplog.set_level(logging.INFO)
    client = start(make_provider_app, Logging())
    call(client, "GET", "/records")
    s = _server_span(spans)
    # structlog hands the event dict to stdlib logging as the record message.
    rec = next(
        r.msg for r in caplog.records if isinstance(r.msg, dict) and r.msg["event"] == "inside"
    )
    assert rec["trace_id"] == f"{s.context.trace_id:032x}"
    assert rec["span_id"] == f"{s.context.span_id:016x}"


def test_e2e_do_calls_are_client_child_spans(start, make_fake_do, spans):
    fake = make_fake_do({"example.com": []})
    client = start(make_provider_app, Provider(DigitalOceanClient("tok", fake.url), []))
    call(client, "GET", "/records")
    finished = spans.get_finished_spans()
    server = next(s for s in finished if s.kind == SpanKind.SERVER)
    clients = [s for s in finished if s.kind == SpanKind.CLIENT]
    assert clients
    assert {c.context.trace_id for c in clients} == {server.context.trace_id}
