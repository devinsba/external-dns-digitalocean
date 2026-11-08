# external-dns-digitalocean

A small Python 3.14 [external-dns](https://github.com/kubernetes-sigs/external-dns) **webhook provider** for DigitalOcean DNS. It runs as a sidecar next to external-dns, whose v0.21 removed the in-tree DigitalOcean provider.

Supported record types: A, AAAA, CNAME, TXT, NS, MX.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `DO_TOKEN` | yes | | DigitalOcean API token with domain read/create/update/delete scopes |
| `DOMAIN_FILTER` | no | all domains | Comma-separated list of domains to manage |
| `DO_API_URL` | no | `https://api.digitalocean.com/v2` | DigitalOcean API base URL |
| `LOG_LEVEL` | no | `INFO` | Log level |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | no | unset (telemetry export off) | OTLP/HTTP collector, e.g. `http://alloy.observability.svc:4318` |
| `OTEL_SERVICE_NAME` | no | `external-dns-digitalocean` | Service name on exported telemetry |

Other standard `OTEL_*` variables (`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_TIMEOUT`, ...) are honored by the OpenTelemetry SDK.

| Port | Bind | Purpose |
|---|---|---|
| 8888 | `127.0.0.1` | external-dns webhook API (pod-local only) |
| 8080 | `0.0.0.0` | `GET /healthz` for probes |

## Deploy

With the official external-dns Helm chart (its default webhook probes already hit `/healthz` on port 8080):

```yaml
provider:
  name: webhook
  webhook:
    image:
      repository: ghcr.io/devinsba/external-dns-digitalocean
      tag: <version>
    env:
      - name: DO_TOKEN
        valueFrom: { secretKeyRef: { name: external-dns-do, key: token } }
      - name: DOMAIN_FILTER
        value: example.com
      - name: OTEL_EXPORTER_OTLP_ENDPOINT
        value: http://alloy.observability.svc:4318
```

## Telemetry

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to export traces and metrics over OTLP/HTTP; with it unset nothing is exported. Logs are not exported (stdout only), but every log line carries `trace_id=` and `span_id=` for correlation.

- Traces: a `SERVER` span per webhook request (`GET /records`, ... ; `/healthz` is not traced) continuing any incoming `traceparent`, with `CLIENT` child spans for each DigitalOcean API call.
- Metrics: counter `dns.record.changes` (`{change}`) with attributes `action` (create/update/delete), `record_type`, `zone`.

## Development

```sh
uv sync
uv run ruff check
uv run ruff format --check
uv run pytest
```

## Releasing

Push a `vX.Y.Z` tag. CI runs, then a multi-arch image is pushed to `ghcr.io/devinsba/external-dns-digitalocean` and a draft GitHub Release is created. An AI agent (the Release Notes workflow) then writes the notes into the draft and the release is published; if the agent fails, the release is published with GitHub's generated notes. To regenerate the notes of a release, run the Release Notes workflow manually with the tag.
