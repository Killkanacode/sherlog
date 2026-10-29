# Sherlog

> *Elementary, my dear SRE.*

Sherlog is an autonomous on-call engineer. It detects production incidents,
finds the real root cause with evidence, takes safe actions and verifies that
the system actually recovered.

Built for the **Meta Global AI Developer Hackathon** (Cerebral Valley) —
Indie Developer track, *Push the Frontier*.

> **Status:** work in progress. This README grows phase by phase.

## What's in the repo

| Path | What it is |
|---|---|
| `demo-app/` | The system under test: a small online shop (gateway, orders, payments, inventory) on Postgres + Redis, fully instrumented with OpenTelemetry, plus Locust traffic |
| `observability/` | OTel Collector, Prometheus, Loki and Tempo configs |
| `chaos/` | Fault injection (Toxiproxy proxies today; the chaos controller is next) |
| `platform-api/` | Simulated deploy platform (deploy / rollback / restart / scale / flags) |
| `agent/` | Sherlog itself (Python) |
| `web/` | Sherlog's web UI (React + Vite) |
| `benchmark/` | Fault scenarios and the scoring runner |

## Quick start

Requirements: Docker with Compose v2 and ~6 GB of RAM for Docker.

```bash
cp .env.example .env      # set GATEWAY_HOST_PORT if 8000 is taken
docker compose up -d --build
```

First boot seeds one million products, so give inventory ~30 s to become healthy.

| URL | What |
|---|---|
| http://localhost:8000/products | Shop gateway (`GATEWAY_HOST_PORT`) |
| http://localhost:9090 | Prometheus |
| http://localhost:3100 | Loki API |
| http://localhost:3200 | Tempo API |
| http://localhost:3000 | Grafana (optional: `docker compose --profile debug up -d grafana`) |

Try a checkout:

```bash
curl -s -X POST localhost:8000/checkout -H 'content-type: application/json' \
  -d '{"user_id":"me","items":[{"sku":"SKU-0000042","qty":2}]}'
```

## Demo app telemetry

Every service emits, over OTLP to the collector:

- **Traces** with W3C propagation: `gateway → orders → inventory / payments`, plus Postgres, Redis and card-provider client spans.
- **RED metrics**: `http_server_duration_seconds` (labels `job`, `route`, `method`, `status_code`, `service_version`) and `http_client_duration_seconds` per attempt (`peer`, `outcome`), process CPU/RSS, and DB pool usage.
- **JSON logs** with `trace_id`, `span_id`, `service`, `version` and `level`; exceptions include the full stack trace.

Tempo's metrics generator turns traces into a live service graph (`traces_service_graph_request_total{client, server}`).

## Built during the hackathon

Everything in this repository was written during the hackathon (Oct 27 – Nov 4, 2026). The git history is the record.

- Demo shop with four instrumented services, Postgres schema + 1M-product seed, and always-on Locust traffic.
- Observability pipeline: OTel Collector → Prometheus / Loki / Tempo, with a trace-derived service graph.

## License

MIT
