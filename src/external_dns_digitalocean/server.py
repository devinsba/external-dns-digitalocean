import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from .provider import Provider

# FastAPI ships its own OTel; we use the contrib instrumentor instead, so turn it off.
NO_NATIVE_OTEL = {"tracing": False, "metrics": False, "logs": False, "operation_spans": False}
MEDIA_TYPE = "application/external.dns.webhook+json;version=1"


def _reply(body: Any) -> Response:
    return Response(json.dumps(body), media_type=MEDIA_TYPE)


async def _body(request: Request) -> Any:
    try:
        return json.loads(await request.body())
    except ValueError as e:  # includes JSONDecodeError and UnicodeDecodeError
        raise HTTPException(400, str(e)) from e


def make_provider_app(provider: Provider) -> FastAPI:
    app = FastAPI(
        openapi_url=None, telemetry=NO_NATIVE_OTEL
    )  # no docs routes; this API is only for external-dns

    @app.get("/")
    def negotiate() -> Response:
        include = provider.domain_filter()
        return _reply({"include": include} if include else {})

    # Sync handlers run in a threadpool (the provider does blocking urllib I/O).
    @app.get("/records")
    def records() -> Response:
        return _reply(provider.records())

    @app.post("/records")
    async def apply(request: Request) -> Response:
        changes = await _body(request)
        if not isinstance(changes, dict):
            raise HTTPException(400, "changes must be an object")
        await run_in_threadpool(provider.apply, changes)
        return Response(status_code=204)

    @app.post("/adjustendpoints")
    async def adjust(request: Request) -> Response:
        return _reply(await _body(request))

    FastAPIInstrumentor.instrument_app(app)
    return app


def make_health_app() -> FastAPI:
    # Not instrumented: probes would only add noise.
    app = FastAPI(openapi_url=None, telemetry=NO_NATIVE_OTEL)

    @app.get("/healthz")
    def healthz() -> Response:
        return Response()

    return app
