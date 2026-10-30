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
| `chaos/` | Chaos controller, Toxiproxy proxies and the buggy branches (stored as patches) used by the scenarios |
| `platform-api/` | Simulated deploy platform (deploy / rollback / restart / scale / config / flags) and its change-event feed |
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

Sherlog's investigator calls Meta's models (`META_*` and `OPEN_MODEL_*` in `.env`).
Without credentials, set `SHERLOG_FAKE_LLM=true` to run the whole pipeline on
stand-in models. They exercise the tools and the streaming, but return no real
diagnosis.

| URL | What |
|---|---|
| http://localhost:8000/products | Shop gateway (`GATEWAY_HOST_PORT`) |
| http://localhost:9090 | Prometheus |
| http://localhost:3100 | Loki API |
| http://localhost:3200 | Tempo API |
| http://localhost:8200/incidents | Sherlog API (`/incidents`, `/signals`, `/topology`, `/stream`) |
| http://localhost:8050/services | platform-api |
| http://localhost:8100/scenarios | chaos-controller (benchmark only; Sherlog can't reach it) |
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

## Platform and chaos

**platform-api** plays the role of a deploy platform. It builds a service
image from any commit of this repo and rolls it out with health-gated
replacement, and it also handles rollback, restart, scale, config changes
(with revert) and feature flags. Every change becomes an event with an actor,
a version and a commit message. That is what Sherlog sees, the same way an
on-call engineer sees a deploy log. Branch names are never exposed, because
`bug/orders-keyerror` would give the answer away.

**chaos-controller** injects the faults: crash, hang, stop, network latency
and partition (Toxiproxy), bad deploys, config changes and harmful flags. It
records ground truth in its own SQLite database, on a network Sherlog isn't
attached to. The bad code lives on branches that look like ordinary work and
are rebuilt from `chaos/bugs/*.patch` with `chaos/bugs/rebuild-branches.sh`.

```bash
python3 chaos/verify_scenarios.py 03   # inject scenario 3 and print the telemetry it moved
curl -s -X POST localhost:8100/reset   # undo every active fault
```

## Detection (no LLM)

Sherlog only reads telemetry, change events and source code. It watches all
of it continuously:

- **Metrics:** error rate, p95 latency, traffic, client-side failures and
  latency per dependency, CPU, memory growth and limit, DB pool saturation,
  and datastore failures. Each series is compared with an EWMA baseline
  learned from history, and a signal must persist before it fires.
- **Logs:** Drain3 templates per service, which detect new templates, spikes
  and silence. Exceptions are fingerprinted by their own-code frames, so new
  crash types stand out even when line numbers move.
- **Containers:** restarts, crashes and not-ready replicas, read from
  platform-api.

Signals that are connected in the service graph and close in time are
grouped into one incident. The evidence funnel then condenses thousands of
log lines into a few thousand tokens: recent changes, new templates,
exceptions, slow or failed traces, and a root-cause hint. The hint names the
deepest anomalous service, and it accounts for retry storms and starved
services.

`python3 benchmark/runner/run.py` injects each scenario, waits for the
incident and scores it. Results from 2026-10-29 (`benchmark/results/`):

| | |
|---|---|
| Scenarios detected | **15 / 15**, each as exactly one incident |
| False positives in 10 fault-free minutes | **0** |
| Median time to detect | **20.1 s** |
| Evidence funnel | 10k–21k log lines → 2.4k–9k tokens per incident |
| Root-cause hint | 14 / 15; the retry storm missed, and after a fix a re-run pointed at the right service |

## Investigation (Meta models)

Once the evidence pack is ready (45 s after an incident opens), two models
take over:

1. **Triage, on Muse Spark Open.** This is the cheap first pass. For each
   suspect service it reports what changed, which errors are new and what
   looks like noise.
2. **The investigator, on Muse Spark with tool calling.** It states a
   hypothesis and tests it with ten read-only tools: `query_metrics`,
   `get_service_health`, `get_traces`, `get_logs`, `get_exception`,
   `get_code_context` (source at the deployed version), `get_recent_changes`,
   `diff_deploy`, `get_dependencies` and `search_past_incidents`. It ends with
   a JSON diagnosis that is validated against a strict schema: root cause
   (service, category, confidence), evidence, ruled-out hypotheses and a
   recommended action.

The investigator is bounded by a maximum number of steps, a token budget per
incident and a timeout per tool. Every hypothesis and tool call is streamed
over `/stream` and kept in the incident timeline. Each model call is
accounted for per incident, with tokens, latency and cost. Cost appears once
prices are configured.

All model-specific code lives in `agent/sherlog/llm/`. Wire-format details
that haven't been checked against Meta's documentation are marked
`TODO(VERIFY)`.

```bash
curl -s localhost:8200/incidents/<id>             # timeline, triage, diagnosis, llm_calls
curl -s -X POST localhost:8200/incidents/<id>/investigate
curl -s -X POST localhost:8200/tools/get_dependencies -H 'content-type: application/json' -d '{"service":"orders"}'
```

## Built during the hackathon

Everything in this repository was written during the hackathon (Oct 27 – Nov 4, 2026). The git history is the record.

- Demo shop with four instrumented services, Postgres schema + 1M-product seed, and always-on Locust traffic.
- Observability pipeline: OTel Collector → Prometheus / Loki / Tempo, with a trace-derived service graph.
- platform-api (deploys from git commits, rollback, config, flags, change events), chaos-controller with 15 scenarios, and the buggy branches.
- Sherlog's detection pipeline: metric, log, exception and container detectors, incident grouping, topology, evidence funnel and root-cause hint, plus the detection benchmark.
- The model layer, triage, the tool-calling investigator with its ten tools, and diagnosis scoring in the benchmark.

## License

MIT
