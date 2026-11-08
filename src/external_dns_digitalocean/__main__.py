import asyncio
import os
import sys

import structlog
import uvicorn
from fastapi import FastAPI

from . import telemetry
from .digitalocean import DigitalOceanClient
from .provider import Provider
from .server import make_health_app, make_provider_app


def main() -> None:
    telemetry.setup()
    telemetry.configure_logging(os.environ.get("LOG_LEVEL", "INFO").upper())
    token = os.environ.get("DO_TOKEN")
    if not token:
        sys.exit("DO_TOKEN environment variable is required")
    api_url = os.environ.get("DO_API_URL") or "https://api.digitalocean.com/v2"
    domain_filter = [d.strip() for d in os.environ.get("DOMAIN_FILTER", "").split(",") if d.strip()]

    provider = Provider(DigitalOceanClient(token, api_url), domain_filter)
    # Provider API is localhost-only per the webhook spec; health must be reachable by the kubelet.
    # uvicorn handles SIGTERM/SIGINT and drains both servers.
    structlog.get_logger().info("serving", webhook="127.0.0.1:8888", health="0.0.0.0:8080")
    asyncio.run(_serve(make_provider_app(provider), make_health_app()))


async def _serve(provider_app: FastAPI, health_app: FastAPI) -> None:
    def server(app: FastAPI, host: str, port: int, access_log: bool = True) -> uvicorn.Server:
        # log_config=None keeps the root logging setup (JSON with trace ids).
        return uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_config=None, access_log=access_log)
        )

    await asyncio.gather(
        server(provider_app, "127.0.0.1", 8888).serve(),
        server(health_app, "0.0.0.0", 8080, access_log=False).serve(),
    )


if __name__ == "__main__":
    main()
