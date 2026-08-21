# OpenTelemetry Collector Deployment Matrix

CAO emits OTel spans + metrics from
[`cli_agent_orchestrator.telemetry`](../src/cli_agent_orchestrator/telemetry/).
Telemetry is **off by default** — set `OTEL_SDK_DISABLED=false` to enable.
Once on, the SDK honors the standard OTel environment variables, so any
collector that speaks OTLP works without code changes.

This page documents the supported transports + auth modes and gives
copy-paste recipes for the common backends.

---

## Transport × auth matrix

|                   | gRPC (`:4317`)                    | HTTP/protobuf (`:4318`)               | HTTP/JSON (`:4318`)                    |
|-------------------|-----------------------------------|---------------------------------------|----------------------------------------|
| **mTLS**          | `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` + `OTEL_EXPORTER_OTLP_CERTIFICATE` + `OTEL_EXPORTER_OTLP_CLIENT_KEY` + `OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE` | `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` + cert env vars above | not yet supported by the OTel Python SDK |
| **Bearer token**  | `OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer ...` | `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` + same headers | `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` + same headers |
| **No auth (dev)** | default — endpoint defaults to `http://localhost:4317` | `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` + endpoint `http://localhost:4318` | `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` + endpoint `http://localhost:4318` |

CAO's default exporter is gRPC (no auth) — what the FastAPI lifespan
installs when `OTEL_SDK_DISABLED=false` and no other vars are set.
Switch transports / auth via env vars only; no code changes needed.

---

## Backend recipes

### Datadog

```bash
export OTEL_SDK_DISABLED=false
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.us5.datadoghq.com:4317
export OTEL_EXPORTER_OTLP_HEADERS="dd-api-key=$DD_API_KEY"
```

Datadog accepts OTLP/gRPC with `dd-api-key` as the auth header. The
service name comes from CAO's resource attribute (`service.name=cao`)
which the SDK sets automatically.

### Honeycomb

```bash
export OTEL_SDK_DISABLED=false
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_ENDPOINT=https://api.honeycomb.io
export OTEL_EXPORTER_OTLP_HEADERS="x-honeycomb-team=$HONEYCOMB_API_KEY"
```

Honeycomb accepts both gRPC and HTTP/protobuf. Use `x-honeycomb-team`
for the API key. For multi-environment setups, set
`x-honeycomb-dataset` to a non-default dataset.

### Grafana Cloud (Tempo / Mimir)

```bash
export OTEL_SDK_DISABLED=false
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp-gateway-prod-us-central-0.grafana.net/otlp
export OTEL_EXPORTER_OTLP_HEADERS="authorization=Basic $(echo -n "$GRAFANA_INSTANCE_ID:$GRAFANA_API_KEY" | base64)"
```

Grafana Cloud requires HTTP/protobuf in the standard signal pipeline.
Auth is HTTP Basic with the instance id as username and API key as
password.

### Local OpenTelemetry Collector (sidecar)

For local dev or operator-managed collectors:

```bash
# Run the collector locally (e.g. via docker):
docker run --rm -p 4317:4317 -p 4318:4318 \
  -v $(pwd)/otel-config.yaml:/etc/otel-config.yaml \
  otel/opentelemetry-collector-contrib --config /etc/otel-config.yaml

# CAO points at the local collector:
export OTEL_SDK_DISABLED=false
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
```

A minimal `otel-config.yaml` that fans CAO spans + metrics to stdout:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
      http:
exporters:
  debug:
    verbosity: detailed
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [debug]
    metrics:
      receivers: [otlp]
      exporters: [debug]
```

### Self-hosted Jaeger (traces only)

```bash
export OTEL_SDK_DISABLED=false
export OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger-collector:4317
```

Jaeger accepts OTLP natively since v1.35; no translation layer
required. Metrics need a separate Prometheus / OpenTelemetry Collector
pipeline since Jaeger ingests traces only.

---

## Verifying the connection

After setting the env vars, restart CAO and trigger one inter-agent dispatch.
The send-message seam (`terminal_service.send_input`, which `handoff` /
`assign` / `send_message` all route through) is instrumented. From outside a
running session you can drive a dispatch with `cao session send`:

```bash
# Send a task to a running supervisor session that performs a handoff:
cao session send cao-my-session "hand off to the reviewer agent: verify"
```

Then check the backend for:

- an `execute_tool` span named `send_message:<orchestration_type>` carrying
  `gen_ai.operation.name` and `gen_ai.conversation.id`, and
- the `cao.orchestration.dispatches` counter (tagged with
  `cao.orchestration.type`) incrementing per dispatch.

The active trace context is also injected into the outgoing plugin event as a
W3C `traceparent`, so a downstream consumer can continue the same trace.

The `invoke_agent` and `chat` span helpers (with `cao.tier` / `gen_ai.request.model`
attributes) ship as a library for instrumenting agent- and model-level calls;
the dispatch seam above is the wiring included in this change.

If nothing arrives, check `cao-server` stderr — the OTel SDK logs
exporter retry attempts inline. The most common failures are:

- **Wrong endpoint protocol** — `localhost:4317` is gRPC, `localhost:4318`
  is HTTP. Mixing them yields `StatusCode.UNAVAILABLE` errors.
- **Missing auth header** — Datadog / Honeycomb / Grafana refuse silently
  on bad keys; check the backend's ingest log for 401/403.
- **TLS cert validation** — set `OTEL_EXPORTER_OTLP_CERTIFICATE` to a CA
  bundle when running against an internal collector with a self-signed
  cert.

---

## See also

- [OTel SDK env vars](https://opentelemetry.io/docs/specs/otel/configuration/sdk-environment-variables/) — upstream spec.
