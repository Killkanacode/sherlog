import { useMemo, useState } from "react";
import { ActionPanel } from "../components/ActionPanel";
import { EvidencePanel } from "../components/Evidence";
import { ImpactChart } from "../components/ImpactChart";
import { ServiceMap } from "../components/ServiceMap";
import { Timeline } from "../components/Timeline";
import { Badge, Button, Chip, Empty, Meter, Note, Panel, cx } from "../components/ui";
import { API, get, post } from "../lib/api";
import { between, clock, dateTime, duration, money, num } from "../lib/format";
import { useDebounced, useNow, usePoll } from "../lib/hooks";
import { categoryOf, SERVICES, statusOf } from "../lib/labels";
import { href } from "../lib/router";
import { useSession } from "../lib/session";
import { useStream } from "../lib/stream";
import type { IncidentDetail, Topology } from "../lib/types";

export function IncidentPage({ id }: { id: number }) {
  const now = useNow();
  const { can } = useSession();
  const incident = usePoll(() => get<IncidentDetail>(`${API}/incidents/${id}`), 5000, [id]);
  const refresh = useDebounced(incident.refresh, 300);
  useStream((m) => {
    if (m.incident_id === id || m.incident?.id === id) refresh();
  });

  const d = incident.data;
  if (!d) {
    return incident.error ? (
      <Panel>
        <Empty>
          Incident #{id} isn't in Sherlog's live memory ({incident.error}).{" "}
          <a className="text-brass hover:underline" href={href(`postmortems/${id}`)}>
            Read its postmortem
          </a>
          .
        </Empty>
      </Panel>
    ) : (
      <Empty>Loading incident #{id}…</Empty>
    );
  }

  const status = statusOf(d.status);
  const live = !d.resolved_at;
  const busy = d.investigation?.state === "running" || d.actions.some((a) => ["approved", "executing", "verifying", "reverting"].includes(a.state));

  return (
    <div className="flex flex-col gap-5">
      <div>
        <a href={href("live")} className="text-xs text-muted hover:text-ink">
          ← Live
        </a>
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2">
          <span className="font-mono text-[15px] text-muted">#{d.id}</span>
          <h1 className="text-[22px] font-semibold tracking-tight text-ink">{d.title}</h1>
          <Badge tone={status.tone} live={live && d.status !== "escalated"}>
            {status.label}
          </Badge>
          <div className="ml-auto flex gap-2">
            {live && !busy && can("operator") && (
              <Button size="sm" onClick={() => post(`${API}/incidents/${id}/investigate`).then(incident.refresh, incident.refresh)}>
                Investigate again
              </Button>
            )}
            {d.has_postmortem && (
              <a href={href(`postmortems/${id}`)}>
                <Button size="sm" variant="primary">
                  Postmortem →
                </Button>
              </a>
            )}
          </div>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-2 text-[13px] text-muted">
          <span>Opened {dateTime(d.created_at)}</span>
          <span className="num">
            {live ? "Open for " : "Lasted "}
            <span className="font-mono text-ink-2">{duration(between(d.created_at, d.resolved_at ?? now))}</span>
          </span>
          <span className="flex flex-wrap gap-1.5">
            {d.services.map((s) => (
              <Chip key={s}>{s}</Chip>
            ))}
          </span>
          <span>{d.signal_count} signals</span>
        </div>
      </div>

      {d.seen_before && (
        <div className="flex flex-wrap items-center gap-3 border border-brass/40 bg-brass/5 px-4 py-2.5 text-[13px]">
          <span className="font-mono text-[11px] tracking-wider text-brass uppercase">Seen before</span>
          <span className="text-ink-2">
            Incident #{d.seen_before.incident_id}
            {d.seen_before.service && ` on ${d.seen_before.service}`}
            {d.seen_before.category && ` (${categoryOf(d.seen_before.category)})`},{" "}
            {d.seen_before.action ? `fixed by ${d.seen_before.action.type}` : d.seen_before.outcome.replace(/_/g, " ")}
            {d.seen_before.shared_fingerprints ? ` · ${d.seen_before.shared_fingerprints} shared exception fingerprints` : ""}
          </span>
          <a href={href(`postmortems/${d.seen_before.incident_id}`)} className="ml-auto text-brass hover:underline">
            Its postmortem →
          </a>
        </div>
      )}

      <div className="grid grid-cols-1 items-start gap-5 xl:grid-cols-[minmax(0,1.55fr)_minmax(380px,1fr)]">
        <div className="flex min-w-0 flex-col gap-5">
          <Panel title="Diagnosis" aside={d.investigation && <ModelTag mode={d.investigation.mode} />}>
            <Verdict incident={d} />
          </Panel>
          <Panel title="Mitigation">
            <ActionPanel incident={d} onChange={incident.refresh} />
          </Panel>
          <ImpactPanel incident={d} />
          <Panel title="Evidence pack" aside="what the investigator read" bodyClass="">
            <EvidencePanel incident={d} />
          </Panel>
        </div>

        <div className="flex min-w-0 flex-col gap-5 xl:sticky xl:top-[72px]">
          <Panel
            title="Investigation"
            aside={d.investigation?.steps != null && <span>{d.investigation.steps} steps</span>}
            bodyClass="h-[calc(100vh-430px)] min-h-[360px]"
          >
            <Timeline entries={d.timeline} live={live} />
          </Panel>
          <Panel title="Blast radius" bodyClass="px-1 py-3">
            <IncidentMap incident={d} />
          </Panel>
          <Usage incident={d} />
        </div>
      </div>
    </div>
  );
}

function ModelTag({ mode }: { mode: "fake" | "live" }) {
  return mode === "fake" ? (
    <span className="font-mono text-[11px] text-warn">STAND-IN MODELS</span>
  ) : (
    <span className="font-mono text-[11px] text-ink-2">MUSE SPARK OPEN → MUSE SPARK</span>
  );
}

function Verdict({ incident }: { incident: IncidentDetail }) {
  const dx = incident.diagnosis;
  const cause = dx?.root_cause;
  const running = incident.investigation?.state === "running";
  const hint = incident.evidence?.root_cause_hint;
  const lastHypothesis = [...incident.timeline].reverse().find((e) => e.kind === "hypothesis");

  if (running || (!dx && incident.evidence)) {
    return (
      <div>
        <div className="flex items-center gap-2 text-[15px] font-medium text-info">
          <span className="size-2 bg-info blink" /> Investigating
        </div>
        {lastHypothesis && <p className="mt-2 text-[14px] text-ink italic">“{lastHypothesis.text}”</p>}
        {hint && (
          <p className="mt-2 text-[13px] text-muted">
            Graph heuristic: <span className="text-ink-2">{hint.service}</span>. {hint.reason}
          </p>
        )}
      </div>
    );
  }
  if (!dx || !cause) {
    return (
      <div className="text-[13px] text-muted">
        {incident.evidence ? "No diagnosis." : "Building the evidence pack: Sherlog waits 45 s after detection for the picture to settle."}
      </div>
    );
  }

  const known = cause.confidence > 0;
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-5 md:grid-cols-[1fr_220px]">
        <div>
          <div className="text-[11px] tracking-[0.09em] text-muted uppercase">Root cause</div>
          <div className="mt-1 flex flex-wrap items-baseline gap-x-3">
            <span className="text-[28px] leading-tight font-semibold text-ink">{cause.service}</span>
            <span className="text-[16px] text-brass">{categoryOf(cause.category)}</span>
          </div>
          <p className="mt-2 text-[14px] text-ink-2">{cause.summary}</p>
        </div>
        <div>
          <div className="flex items-baseline justify-between">
            <span className="text-[11px] tracking-[0.09em] text-muted uppercase">Confidence</span>
            <span className="num font-mono text-[15px] text-ink">{Math.round(cause.confidence * 100)}%</span>
          </div>
          <div className="mt-2">
            <Meter value={cause.confidence} tone={cause.confidence >= 0.7 ? "ok" : cause.confidence >= 0.4 ? "warn" : "bad"} />
          </div>
        </div>
      </div>
      {!known && (
        <Note tone="warn">
          Stand-in model: it repeats the graph heuristic and always escalates. Connect Muse Spark for a real diagnosis.
          {hint && ` The heuristic's reasoning: ${hint.reason}`}
        </Note>
      )}
      {known && dx.evidence.length > 0 && (
        <div>
          <div className="mb-1.5 text-[11px] tracking-[0.09em] text-muted uppercase">Evidence</div>
          <ul className="flex flex-col gap-1.5">
            {dx.evidence.map((e, i) => (
              <li key={i} className="grid grid-cols-[84px_1fr] gap-3 text-[13px]">
                <span className="font-mono text-[11px] text-brass uppercase">{e.type}</span>
                <span className="text-ink-2">
                  {e.detail} <span className="font-mono text-[11.5px] text-faint">{e.ref}</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {dx.ruled_out.length > 0 && (
        <div>
          <div className="mb-1.5 text-[11px] tracking-[0.09em] text-muted uppercase">Ruled out</div>
          <ul className="flex flex-col gap-1.5">
            {dx.ruled_out.map((r, i) => (
              <li key={i} className="text-[13px] text-ink-2">
                <span className="text-muted line-through decoration-faint">{r.hypothesis}</span> — {r.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ImpactPanel({ incident }: { incident: IncidentDetail }) {
  const options = ["gateway", ...incident.services.filter((s) => s !== "gateway" && SERVICES.includes(s))];
  const [service, setService] = useState("gateway");
  return (
    <Panel
      title="Impact"
      aside={
        <span className="flex items-center gap-3">
          <span className="flex items-center gap-1.5">
            <span className="h-0.5 w-3 bg-bad" /> error rate
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-0.5 w-3 bg-info" /> p95 latency
          </span>
          <select
            value={service}
            onChange={(e) => setService(e.target.value)}
            className="h-6 border border-line bg-bg px-1 text-xs text-ink-2 outline-none"
          >
            {options.map((s) => (
              <option key={s} value={s}>
                {s === "gateway" ? "gateway (customers)" : s}
              </option>
            ))}
          </select>
        </span>
      }
      bodyClass="px-2 pt-2 pb-1"
    >
      <ImpactChart incident={incident} service={service} />
    </Panel>
  );
}

function IncidentMap({ incident }: { incident: IncidentDetail }) {
  const topology = useMemo<Topology | null>(() => {
    const edges = incident.evidence?.topology;
    if (!edges?.length) return null;
    const ids = [...new Set(edges.flatMap((e) => [e.source, e.target]))];
    return {
      nodes: ids.map((n) => ({ id: n, kind: SERVICES.includes(n) ? "service" : "dependency", status: "ok" })),
      edges,
    };
  }, [incident.evidence]);
  const anomalous = useMemo(() => new Set(incident.signals.map((s) => s.service)), [incident.signals]);
  const cause = incident.diagnosis?.root_cause;
  const root = cause && cause.confidence > 0 ? cause.service : null;
  if (!topology) return <Empty>The map appears with the evidence pack.</Empty>;
  return (
    <ServiceMap
      topology={topology}
      anomalous={anomalous}
      rootCause={root}
      suspects={root ? [] : incident.root_cause_hint ? [incident.root_cause_hint] : []}
      height={250}
    />
  );
}

function Usage({ incident }: { incident: IncidentDetail }) {
  const u = incident.llm_usage;
  if (!u || !incident.llm_calls.length) return null;
  const roles = incident.llm_calls.reduce<Record<string, { calls: number; tokens: number; model: string }>>((acc, c) => {
    const r = (acc[c.role] ??= { calls: 0, tokens: 0, model: c.model });
    r.calls += 1;
    r.tokens += c.input_tokens + c.output_tokens;
    return acc;
  }, {});
  return (
    <Panel title="Model usage" aside={<span className="font-mono">{money(u.cost_usd)}</span>} bodyClass="">
      <table className="w-full text-[13px]">
        <tbody>
          {Object.entries(roles).map(([role, r]) => (
            <tr key={role} className="border-b border-line last:border-0">
              <td className="px-4 py-2 text-ink-2 capitalize">{role}</td>
              <td className="py-2 font-mono text-xs text-muted">{r.model}</td>
              <td className="num py-2 text-right font-mono text-xs text-ink-2">{r.calls} calls</td>
              <td className="num px-4 py-2 text-right font-mono text-xs text-ink-2">{num(r.tokens)} tok</td>
            </tr>
          ))}
          <tr className={cx("text-ink")}>
            <td className="px-4 py-2 font-medium">Total</td>
            <td />
            <td className="num py-2 text-right font-mono text-xs">{u.calls} calls</td>
            <td className="num px-4 py-2 text-right font-mono text-xs">{num(u.total_tokens)} tok</td>
          </tr>
        </tbody>
      </table>
      <div className="border-t border-line px-4 py-2 text-xs text-faint">Last call {clock(incident.llm_calls[incident.llm_calls.length - 1].ts)}</div>
    </Panel>
  );
}
