import logging
import os
import signal
import sys
import threading

from . import telemetry
from .digitalocean import DigitalOceanClient
from .provider import Provider
from .server import make_health_server, make_provider_server


def main() -> None:
    telemetry.setup()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format=telemetry.LOG_FORMAT,
    )
    token = os.environ.get("DO_TOKEN")
    if not token:
        sys.exit("DO_TOKEN environment variable is required")
    api_url = os.environ.get("DO_API_URL") or "https://api.digitalocean.com/v2"
    domain_filter = [d.strip() for d in os.environ.get("DOMAIN_FILTER", "").split(",") if d.strip()]

    provider = Provider(DigitalOceanClient(token, api_url), domain_filter)
    # Provider API is localhost-only per the webhook spec; health must be reachable by the kubelet.
    servers = [
        make_provider_server(provider, "127.0.0.1", 8888),
        make_health_server("0.0.0.0", 8080),
    ]

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    logging.info("Serving webhook on 127.0.0.1:8888, health on 0.0.0.0:8080")
    stop.wait()
    for s in servers:
        s.shutdown()
    sys.exit(0)


if __name__ == "__main__":
    main()
