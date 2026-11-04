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
echo "SHERLOG_ADMIN_TOKEN=slg_$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
docker compose up -d --build
```

First boot seeds one million products, so give inventory ~30 s to become healthy.
Open the console and sign in with the `SHERLOG_ADMIN_TOKEN` from `.env`. If you
leave it unset, Sherlog creates an admin key on first start and writes it to
its log once (`docker compose logs sherlog-api | grep "admin key"`).

Sherlog's investigator calls Meta's models (`META_*` and `OPEN_MODEL_*` in `.env`).
Without credentials, set `SHERLOG_FAKE_LLM=true` to run the whole pipeline on
stand-in models. They exercise the tools and the streaming, but return no real
diagnosis.

| URL | What |
|---|---|
| http://localhost:8300 | **Sherlog's web console** (`WEB_HOST_PORT`) |
| http://localhost:8000/products | Shop gateway (`GATEWAY_HOST_PORT`) |
| http://localhost:9090 | Prometheus |
| http://localhost:3100 | Loki API |
| http://localhost:3200 | Tempo API |
| http://localhost:8200/incidents | Sherlog API (`/incidents`, `/postmortems`, `/settings`, `/signals`, `/topology`, `/stream`), with `Authorization: Bearer <key>` |
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
- **Logs:** Loki counts every service's lines (for silence) and hands
  Sherlog only the WARN/ERROR/FATAL lines. Drain3 turns those into templates
  per service to detect new templates and spikes. Exceptions are
  fingerprinted by their own-code frames, so new crash types stand out even
  when line numbers move.
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
export KEY=<your API key>; AUTH="Authorization: Bearer $KEY"
curl -s -H "$AUTH" localhost:8200/incidents/<id>             # timeline, triage, diagnosis, llm_calls
curl -s -H "$AUTH" -X POST localhost:8200/incidents/<id>/investigate
curl -s -H "$AUTH" -X POST localhost:8200/tools/get_dependencies -H 'content-type: application/json' -d '{"service":"orders"}'
```

## Mitigation, verification and memory

The diagnosis ends with a recommended action. Sherlog can run five of them,
all through platform-api and only on the shop's services: `rollback`,
`config_revert`, `flag_off`, `restart` (which also covers Redis and Postgres)
and `scale`. How far it goes on its own depends on the autonomy level:

| Level | Behaviour |
|---|---|
| L0 | Suggests the action and never acts |
| L1 (default) | Every action waits for `POST /incidents/<id>/approve` (or `/reject`) |
| L2 | Low-risk rollbacks, flag flips and restarts run on their own when the diagnosis confidence is at least 0.7. Anything else waits for approval |

Restarting a datastore always counts as medium risk. Dry run records what
would have happened and changes nothing. Both settings come from `.env` and
can be changed at runtime with `PUT /settings`.

Before it acts, Sherlog records how to undo the action: the version to
redeploy, the config to reapply, the flag to turn back on or the replica count
to return to. Then it verifies the result. It watches the incident's services
until their impact metrics are back at their learned baselines, no container
is down and the incident's exceptions have stopped, all for 30 s in a row. If
that doesn't happen within 180 s, it reverts the action and runs the
investigator again, telling it what failed. After two failed mitigations it
escalates to a human. A human can also pick an action with
`POST /incidents/<id>/actions`, and it goes through the same verification.

Closing an incident does two things:

- **Postmortem.** Sherlog writes one in Markdown with the outcome, the impact
  measured at the gateway (duration, and failed requests out of the total),
  detect/mitigate/recover times, the root cause and its evidence, what was
  ruled out (including mitigations that didn't help), the timeline and
  follow-up actions.
- **Incident memory.** Sherlog stores the incident's exception fingerprints,
  error templates, root cause and the action that fixed it. When a new
  incident shares a fingerprint or a template, its timeline says, for example,
  "Seen before: incident #12 (orders, bad_deploy_code_bug), fixed by
  rollback", and the investigator gets the match.
  `search_past_incidents` reads the same memory. A diagnosis with zero
  confidence isn't stored as knowledge.

```bash
curl -s -H "$AUTH" -X POST localhost:8200/incidents/<id>/approve           # operator
curl -s -H "$AUTH" -X POST localhost:8200/incidents/<id>/actions -H 'content-type: application/json' \
  -d '{"type":"rollback","params":{"service":"orders"}}'                      # operator
curl -s -H "$AUTH" localhost:8200/incidents/<id>/postmortem                 # Markdown
curl -s -H "$AUTH" -X PUT localhost:8200/settings -H 'content-type: application/json' -d '{"autonomy":"L2","dry_run":false}'  # admin
```

## Web console

Everything above can be done from the browser at http://localhost:8300:

- **Live:** the service map learned from traces (nodes turn red when
  anomalous, edges show req/s and errors), live incidents with an inline
  Approve button, RED sparklines per service, and the change feed.
- **Incident:** the investigation streaming in (signals, evidence, each
  hypothesis and tool call, the conclusion), the root cause with its
  confidence, the mitigation with Approve / Reject or an action of your
  choice, verification progress check by check, an impact chart marked with
  detection, action and recovery, and the evidence pack: the funnel from log
  lines to tokens, exceptions with the code they point at, the deploy diff,
  log patterns and trace waterfalls.
- **Chaos Lab:** one button per scenario, the run in progress with time to
  detect, an answer key to compare with Sherlog's diagnosis, and Reset.
- **Postmortems**, rendered, with copy and download. **Settings** for the
  autonomy level and dry run. **How it works** for the pipeline and the
  architecture.

The console is a React + Vite app in `web/`, served by nginx, which also
proxies Sherlog's API, the chaos controller and two read-only views
(Prometheus range queries and platform-api's services and flags) under one
origin. The console joins the chaos network to drive the Chaos Lab; the agent
still can't reach it.

To work on it with hot reload against the running stack:

```bash
npm --prefix web install
npm --prefix web run dev   # http://localhost:5173
```

## Production readiness

### Scale: Sherlog's cost follows errors, not log volume

Sherlog doesn't download every log line. Each poll asks Loki to count lines
per service, which is all that silence detection needs, and to return only
WARN and above. Both filters run inside Loki. A poll that still brings more
than 20,000 lines (an error storm) stops there and counts the rest as dropped,
so the detector never falls behind. Language wasn't the bottleneck: the
heavy per-line work now happens in Loki, which is written in Go.

`benchmark/load/flood.py` sends synthetic OTLP logs (99.9% INFO) through the
collector, and `benchmark/load/measure.py` reads Sherlog's CPU from its
cgroup and its own counters. Both versions ran on the same laptop,
2026-11-04 (`benchmark/results/load-*.json`):

| 20,000 log lines/s for 2 min | Before | After |
|---|---|---|
| Lines Sherlog parsed per second | 16,320 (everything) | 19 (WARN+) |
| Sherlog CPU time | 12.8 s | 1.0 s (13x less) |
| Loki CPU (average) | 17.4% | 9.7% |
| Detection lag (max) | not measured | 8.1 s |
| Error storm, 30% WARN | not run | 3,670 lines/s parsed, 139k counted as dropped, lag 7.9 s |
| False "log silence" after the flood | yes, an incident opened | none in the 3 minutes after |

The tests also turned up three bugs, all fixed:

- **Learning zeroed every minute.** While learning, every minute's count was
  zeroed before it reached the baseline, so log baselines started at zero
  after each restart.
- **Floods inflated the volume baseline.** A flood could push the volume
  baseline up and trigger a false silence. Now a burst counts as at most 3x
  normal, and a silence alert stops freezing the baseline after 10 minutes.
- **A recent incident could hide the next one.** If Sherlog restarted right
  after an incident, it learned that incident as "normal": the next scenario
  3 run (77% errors on orders) only surfaced 82 s later, through a side
  effect. Now baselines start from robust statistics of the learning window:
  metrics from the median and MAD, log counts from a low quantile. Up to half
  of the window can be an incident.

### Restarts resume where they stopped

The log detector writes its state to Postgres every minute: Drain3 trees,
templates, fingerprints, baselines and its read position. On restart it
loads the state and reads only the gap. Before this, Sherlog re-read the last
10 minutes and forgot everything older.

| Restart after the floods | Before | After |
|---|---|---|
| Ready (learning done) | 60 s, re-reading 4.8M lines | 2 s, reading 8 s of gap |
| Known templates kept | 10 of 42 (the rest would look new) | all |

Templates and fingerprints stop counting as new after 6 hours, so a
long-lived, flaky dependency becomes part of normal, and only its spikes alert.
Incident memory now ignores keys that appear in more than 20% of past
incidents. A match needs a shared exception fingerprint, or at least two
shared log templates.

### Models: budget, breaker, queue, redaction

- **Monthly budget.** Model calls stop when the month reaches
  `SHERLOG_MONTHLY_SPEND_LIMIT_USD` or `SHERLOG_MONTHLY_TOKEN_LIMIT`. The
  incident then escalates.
- **Circuit breaker.** After 3 consecutive model failures, calls pause for
  5 minutes, and incidents escalate right away instead of piling up timeouts.
- **Concurrency.** At most `SHERLOG_MAX_CONCURRENT_INVESTIGATIONS` (default
  2) investigations run at once. The rest queue.
- **Redaction.** Everything sent to a model is first scrubbed of emails,
  card numbers (Luhn-checked), bearer tokens, JWTs, cloud and GitHub keys,
  private keys, and `password=`/`token:`-style values.

### Security

- **Access.** Every API route except `/health` needs an API key with a role.
  A **viewer** can read; an **operator** can also approve, reject and run
  actions and drive the Chaos Lab; an **admin** can also change settings,
  manage keys and read the audit log. Keys are stored as SHA-256 hashes, and
  the console exchanges a key for a signed `HttpOnly`, `SameSite=Strict`
  session. Writes made with that session must carry `X-Requested-With`, which
  blocks CSRF. Logins are rate-limited.
- **The actor is the caller.** Approvals and actions are attributed to the
  key's owner. An `actor` field in the request body is ignored.
- **Audit.** Every write and every refused request is recorded, with who,
  what, when, the result and the client IP. Admins see it in Settings.
- **Console and nginx.** nginx asks Sherlog before proxying the Chaos Lab,
  Prometheus or platform-api: reads need a viewer and writes an operator. It
  also sets CSP, `X-Frame-Options`, `nosniff` and `Referrer-Policy`, and hides
  its version.
- **Change events.** platform-api pushes them with `SHERLOG_EVENTS_TOKEN`.
  Sherlog also pulls them every 5 s, so none is lost.
- **Untrusted telemetry.** Logs, exceptions and commit messages are data,
  never instructions. The prompts say so. Every WARN+ line is checked at
  ingest, along with the evidence and tool results, for text that reads like
  instructions to an AI ("ignore previous instructions…"). A match flags the
  incident, and nothing then runs without a human's approval. Checking at
  ingest also catches a repeated injection whose template is already known.
- **Guardrails that don't trust the model.** An action Sherlog proposes must
  target a service involved in the incident. A rollback, config revert or
  flag flip also needs the matching change in the evidence. Automatic runs
  are limited to 6 per hour, with a 10-minute cooldown per service.
- **Reverts can't clobber later changes.** A revert runs only if the service
  is still in the state Sherlog left it in. A load test once failed
  verification, and Sherlog would otherwise have redeployed the buggy version
  over a reset that had already fixed it.
- **Containers.** Sherlog runs as non-root on a read-only filesystem, with
  all Linux capabilities dropped and `no-new-privileges`.
- **Secrets.** A scan of the whole git history found no keys or tokens.

`python3 benchmark/security/check.py` runs 36 checks against the live stack
through the console's nginx. They cover anonymous access, each role, sessions
and CSRF, forged cookies, headers, brute force and revoked keys. All 36 pass.
`python3 benchmark/e2e/scenario3.py` runs scenario 3 end to end as an
operator, with an injection line planted in orders' logs. All 8 checks
pass: Sherlog detected the fault in 12 s, flagged the planted line, saw the
rollback recover 70 s later, wrote the postmortem and audited the request.

### Still open

- **One Sherlog instance.** Restarts are warm, but there is no hot standby.
- **No seasonality.** Baselines don't model daily or weekly patterns.
- **No SLOs.** There are no SLOs or error-budget alerts yet.
- **TLS.** The console expects a TLS-terminating proxy in front; set
  `SHERLOG_SECURE_COOKIES=true` there.
- **Simulated platform.** The deploy platform is a simulator, so real
  deployments need Kubernetes or Argo CD and feature-flag integrations.

## Built during the hackathon

Everything in this repository was written during the hackathon (Oct 27 – Nov 4, 2026). The git history is the record.

- Demo shop with four instrumented services, Postgres schema + 1M-product seed, and always-on Locust traffic.
- Observability pipeline: OTel Collector → Prometheus / Loki / Tempo, with a trace-derived service graph.
- platform-api (deploys from git commits, rollback, config, flags, change events), chaos-controller with 15 scenarios, and the buggy branches.
- Sherlog's detection pipeline: metric, log, exception and container detectors, incident grouping, topology, evidence funnel and root-cause hint, plus the detection benchmark.
- The model layer, triage, the tool-calling investigator with its ten tools, and diagnosis scoring in the benchmark.
- Actions with autonomy levels and dry run, the verification loop with automatic revert and re-investigation, incident memory and postmortems.
- The web console: Live, Incident, Chaos Lab, Postmortems, Settings and How it works, with live streaming.
- Production hardening: Loki-side log filtering and counting, warm restarts from snapshots, model budget, breaker, queue and redaction, access control with roles, sessions and audit, and action guardrails, with load, security and end-to-end checks.

## License

MIT
