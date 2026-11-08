import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from .provider import Provider

log = logging.getLogger(__name__)

tracer = trace.get_tracer(__name__)
MEDIA_TYPE = "application/external.dns.webhook+json;version=1"


class BadRequest(Exception):
    pass


class _Handler(BaseHTTPRequestHandler):
    provider: Provider

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), format % args)

    def _reply(self, status: int, body: Any = None) -> None:
        self._status = status
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", MEDIA_TYPE)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> Any:
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        except ValueError as e:  # includes JSONDecodeError and UnicodeDecodeError
            raise BadRequest(str(e)) from e

    def _route(self) -> None:
        routes = {
            ("GET", "/"): lambda: self._reply(200, self._negotiate()),
            ("GET", "/records"): lambda: self._reply(200, self.provider.records()),
            ("POST", "/records"): self._apply,
            ("POST", "/adjustendpoints"): lambda: self._reply(200, self._body()),
        }
        path = self.path.split("?")[0]
        known = path in {p for _, p in routes}
        # Unknown paths get no route in the name/attributes to keep cardinality bounded.
        name = f"{self.command} {path}" if known else self.command
        attrs = {"http.request.method": self.command, "url.path": path}
        if known:
            attrs["http.route"] = path
        with tracer.start_as_current_span(
            name,
            context=propagate.extract(self.headers),
            kind=SpanKind.SERVER,
            attributes=attrs,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            self._status = 500
            try:
                self._dispatch(known, routes.get((self.command, path)))
            except BadRequest:
                log.exception("Bad request body")
                self._reply(400)
            except Exception as e:
                log.exception("Request failed")
                span.record_exception(e)
                self._reply(500)
            span.set_attribute("http.response.status_code", self._status)
            if self._status >= 500:
                span.set_status(Status(StatusCode.ERROR))

    def _dispatch(self, known: bool, handler: Any) -> None:
        if not known:
            self._reply(404)
        elif handler is None:
            self._reply(400)
        else:
            handler()

    def _negotiate(self) -> dict:
        include = self.provider.domain_filter()
        return {"include": include} if include else {}

    def _apply(self) -> None:
        changes = self._body()
        if not isinstance(changes, dict):
            raise BadRequest("changes must be an object")
        self.provider.apply(changes)
        self._reply(204)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _route


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/healthz" else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def make_provider_server(provider: Provider, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"provider": provider})
    return ThreadingHTTPServer((host, port), handler)


def make_health_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _HealthHandler)
