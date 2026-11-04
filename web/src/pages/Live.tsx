import { useMemo } from "react";
import { IncidentRows, LiveIncidentCard } from "../components/IncidentList";
import { ServiceMap } from "../components/ServiceMap";
import { Sparkline } from "../components/Sparkline";
import { Badge, Dot, Empty, Panel, Stat } from "../components/ui";
import { API, get, PLATFORM, promRange, RED, type Series } from "../lib/api";
import { ago, compact, latency, num, pct } from "../lib/format";
import { useDebounced, useNow, usePoll } from "../lib/hooks";
import { SERVICES, type Tone } from "../lib/labels";
import { useStream } from "../lib/stream";
import type { ChangeEvent, IncidentSummary, PlatformService, Stats, Topology } from "../lib/types";

type MetricState = { rule: string; service: string; qualifier?: string | null; value: number; baseline: number; firing: boolean };

export function useVersions() {
  const services = usePoll(() => get<PlatformService[]>(`${PLATFORM}/services`), 15000);
  return useMemo(() => {
    const out: Record<string, string> = {};
    for (const s of services.data ?? []) if (s.version) out[s.service] = s.version === "main" ? "main" : s.version.slice(0, 7);
    return { versions: out, services: services.data ?? [] };
  }, [services.data]);
}

export function Live() {
  const now = useNow(5000);
  const topology = usePoll(() => get<Topology>(`${API}/topology`), 5000);
  const stats = usePoll(() => get<Stats>(`${API}/stats`), 10000);
  const incidents = usePoll(() => get<IncidentSummary[]>(`${API}/incidents`), 10000);
  const events = usePoll(
    () => get<ChangeEvent[]>(`${API}/events?limit=12`).then((list) => [...list].sort((a, b) => b.timestamp.localeCompare(a.timestamp))),
    30000,
  );
  const metricState = usePoll(() => get<MetricState[]>(`${API}/detectors/metrics`), 10000);
  const red = usePoll(async () => {
    const end = Date.now();
    const start = end - 20 * 60 * 1000;
    const [rate, errors, p95] = await Promise.all([
      promRange(RED.rate, start, end, 20),
      promRange(RED.errors, start, end, 20),
      promRange(RED.p95, start, end, 20),
    ]);
    return { rate, errors, p95 };
  }, 15000);
  const { versions, services } = useVersions();

  const refresh = useDebounced(() => {
    incidents.refresh();
    topology.refresh();
  });
  useStream((m) => {
    if (["incident", "action", "postmortem", "signal"].includes(m.type)) refresh();
    if (m.type === "change") events.refresh();
  });

  const list = incidents.data ?? [];
  const live = list.filter((i) => !i.resolved_at);
  const closed = list.filter((i) => i.resolved_at);
  const lastClosed = closed[0];
  const anomalous = (topology.data?.nodes ?? []).filter((n) => n.status === "anomalous");
  const serviceCount = topology.data?.nodes.filter((n) => n.kind === "service").length ?? 0;
  const depCount = (topology.data?.nodes.length ?? 0) - serviceCount;
  const firing = new Set((metricState.data ?? []).filter((m) => m.firing).map((m) => `${m.rule}:${m.service}`));
  const s = stats.data;
  const healthy = live.length === 0 && anomalous.length === 0;

  return (
    <div className="flex flex-col gap-5">
      <section className="grid grid-cols-1 gap-6 border border-line bg-panel px-5 py-5 lg:grid-cols-[1.3fr_2fr]">
        <div className="flex flex-col justify-center">
          <div className="flex items-center gap-3">
            <Dot tone={healthy ? "ok" : "bad"} live={!healthy} className="size-3" />
            <h1 className="text-[22px] font-semibold tracking-tight text-ink">
              {healthy
                ? "All systems operational"
                : live.length
                  ? `${live.length} live incident${live.length > 1 ? "s" : ""}`
                  : `${anomalous.length} service${anomalous.length > 1 ? "s" : ""} degraded`}
            </h1>
          </div>
          <p className="mt-2 text-[13px] text-muted">
            Sherlog is watching {serviceCount || "–"} services and {depCount || "–"} dependencies: every metric, log
            line, trace and deploy.
            {lastClosed && healthy && ` Last incident closed ${ago(lastClosed.resolved_at!, now)}.`}
          </p>
        </div>
        <div className="grid grid-cols-2 gap-5 sm:grid-cols-4">
          <Stat label="Log lines read" value={compact(s?.log_lines_seen)} sub={`${num(s?.log_templates)} patterns learned`} />
          <Stat label="Exception types" value={num(s?.exception_fingerprints)} sub="fingerprinted by code" />
          <Stat
            label="Sent to models"
            value={`${compact(s?.funnel.estimated_tokens)} tok`}
            sub={`from ${compact(s?.funnel.log_lines_in_windows)} log lines`}
          />
          <Stat label="Incidents handled" value={num(s?.incidents_total)} sub={`${num(s?.incidents_open)} open now`} />
        </div>
      </section>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,2fr)_minmax(340px,1fr)]">
        <Panel
          title="Service map"
          aside={
            <span className="flex items-center gap-3">
              <span className="flex items-center gap-1.5">
                <Dot tone="ok" /> healthy
              </span>
              <span className="flex items-center gap-1.5">
                <Dot tone="bad" /> anomalous
              </span>
              <span className="text-faint">learned from traces · req/s</span>
            </span>
          }
          bodyClass="px-2 py-3"
        >
          <ServiceMap topology={topology.data} versions={versions} height={340} />
        </Panel>

        <Panel
          title="Incidents"
          aside={live.length ? <Badge tone="bad" live>{live.length} live</Badge> : <span>none live</span>}
          bodyClass=""
        >
          {live.length > 0 && (
            <div className="flex flex-col gap-2 border-b border-line p-3">
              {live.map((i) => (
                <LiveIncidentCard key={i.id} incident={i} onChange={incidents.refresh} />
              ))}
            </div>
          )}
          <IncidentRows incidents={closed} limit={live.length ? 5 : 8} />
        </Panel>
      </div>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,2fr)_minmax(340px,1fr)]">
        <Panel title="Services · last 20 minutes" aside="rate · errors · p95 latency" bodyClass="">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-[13px]">
              <thead>
                <tr className="border-b border-line text-left text-[11px] tracking-[0.09em] text-muted uppercase">
                  <th className="px-4 py-2 font-medium">Service</th>
                  <th className="px-2 py-2 font-medium">Requests</th>
                  <th className="px-2 py-2 font-medium">Errors</th>
                  <th className="px-2 py-2 font-medium">p95 latency</th>
                </tr>
              </thead>
              <tbody>
                {SERVICES.map((svc) => {
                  const platform = services.find((p) => p.service === svc);
                  const cell = (series: Series[] | undefined) => series?.find((x) => x.labels.job === svc)?.points ?? [];
                  const rate = cell(red.data?.rate);
                  const errors = cell(red.data?.errors);
                  const p95 = cell(red.data?.p95);
                  const last = (pts: typeof rate) => [...pts].reverse().find((p) => p.v != null)?.v ?? null;
                  const tone = (rule: string): Tone => (firing.has(`${rule}:${svc}`) ? "bad" : "muted");
                  return (
                    <tr key={svc} className="border-b border-line last:border-0">
                      <td className="px-4 py-2.5">
                        <div className="font-medium text-ink">{svc}</div>
                        <div className="font-mono text-[11px] text-muted">
                          {versions[svc] ?? "–"}
                          {platform?.replicas ? ` · ${platform.replicas} replica${platform.replicas > 1 ? "s" : ""}` : ""}
                        </div>
                      </td>
                      <MetricCell points={rate} tone={tone("traffic")} value={`${num(last(rate), 1)}/s`} />
                      <MetricCell points={errors} tone={tone("error_rate")} value={pct(last(errors))} floor={0.02} />
                      <MetricCell points={p95} tone={tone("latency_p95")} value={latency(last(p95))} />
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel title="Change feed" aside="deploys, config, flags" bodyClass="">
          {(events.data ?? []).length === 0 ? (
            <Empty>No changes recorded.</Empty>
          ) : (
            <ul className="max-h-[260px] overflow-y-auto">
              {(events.data ?? []).map((e) => (
                <li key={e.id} className="border-b border-line px-4 py-2 last:border-0">
                  <div className="flex items-center gap-2 text-[13px]">
                    <span className="font-mono text-[11px] text-brass uppercase">
                      {e.details?.rollback ? "rollback" : e.type}
                    </span>
                    <span className="text-ink">{e.service ?? e.details?.flag ?? ""}</span>
                    {e.to_version && e.to_version !== e.from_version && (
                      <span className="font-mono text-xs text-muted">
                        {e.from_version?.slice(0, 7)} → {e.to_version.slice(0, 7)}
                      </span>
                    )}
                    <span className="ml-auto shrink-0 text-xs text-faint">{ago(e.timestamp, now)}</span>
                  </div>
                  <div className="truncate text-xs text-muted">
                    {e.commit_message ?? describeChange(e)} · {e.actor}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  );
}

function MetricCell({
  points,
  value,
  tone,
  floor,
}: {
  points: { t: number; v: number | null }[];
  value: string;
  tone: Tone;
  floor?: number;
}) {
  return (
    <td className="px-2 py-2.5">
      <div className="flex items-center gap-3">
        <Sparkline points={points} tone={tone === "muted" ? "info" : tone} floor={floor} />
        <span className={`num w-16 font-mono text-xs ${tone === "bad" ? "text-bad" : "text-ink-2"}`}>{value}</span>
      </div>
    </td>
  );
}

function describeChange(e: ChangeEvent): string {
  const d = e.details ?? {};
  if (e.type === "config") return Object.keys(d.changes ?? d).slice(0, 3).join(", ") || "config change";
  if (e.type === "flag") return `${d.flag ?? ""} ${d.enabled === false ? "off" : "on"}`;
  return e.type;
}
