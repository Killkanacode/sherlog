import { useState } from "react";
import { API, post } from "../lib/api";
import { clock, num } from "../lib/format";
import { describeSignal, KIND_OF_SIGNAL } from "../lib/labels";
import type { ChangeEvent, Evidence, ExceptionInfo, IncidentDetail, TraceInfo } from "../lib/types";
import { Badge, Button, Chip, cx, Empty, Tabs } from "./ui";

/** How much telemetry went in, and how little reached the model. */
export function Funnel({ stats }: { stats: Evidence["stats"] }) {
  const steps = [
    { value: num(stats.log_lines_in_window), label: "log lines in window" },
    { value: `${stats.templates_selected}/${stats.templates_known}`, label: "log patterns kept" },
    { value: num(stats.exceptions_selected), label: "exceptions" },
    { value: num(stats.traces_selected), label: "slow or failed traces" },
    { value: `~${num(stats.estimated_tokens)}`, label: "tokens to the model" },
  ];
  const W = 1000;
  const H = 120;
  const heights = [120, 108, 98, 90, 84];
  const seg = W / steps.length;
  return (
    <svg viewBox={`0 0 ${W} ${H + 4}`} className="block h-auto w-full" role="img" aria-label="Evidence funnel">
      {steps.map((s, i) => {
        const h0 = heights[i];
        const h1 = heights[i + 1] ?? heights[i];
        const x0 = i * seg;
        const x1 = x0 + seg - 6;
        const top0 = (H - h0) / 2 + 2;
        const top1 = (H - h1) / 2 + 2;
        const last = i === steps.length - 1;
        return (
          <g key={s.label}>
            <polygon
              points={`${x0},${top0} ${x1},${top1} ${x1},${top1 + h1} ${x0},${top0 + h0}`}
              fill={last ? "#2a2416" : "#181a1e"}
              stroke={last ? "#d4a84b" : "#34373e"}
              strokeWidth="1.5"
            />
            <text x={x0 + 14} y={H / 2 - 2} fill={last ? "#d4a84b" : "#ebe9e4"} fontSize="26" fontWeight="600" fontFamily="IBM Plex Sans">
              {s.value}
            </text>
            <text x={x0 + 14} y={H / 2 + 20} fill="#8a8c94" fontSize="14" fontFamily="IBM Plex Sans">
              {s.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

type Tab = "exceptions" | "logs" | "changes" | "traces" | "signals";

export function EvidencePanel({ incident }: { incident: IncidentDetail }) {
  const ev = incident.evidence;
  const [tab, setTab] = useState<Tab>("exceptions");
  if (!ev) return <Empty>The evidence pack is built 45 s after detection, once the picture is clearer.</Empty>;
  const deploys = ev.changes ?? [];
  return (
    <div>
      <div className="border-b border-line px-4 py-4">
        <Funnel stats={ev.stats} />
      </div>
      <Tabs<Tab>
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "exceptions", label: "Exceptions", count: ev.exceptions.length },
          { id: "logs", label: "Log patterns", count: ev.log_templates.length },
          { id: "changes", label: "Changes", count: deploys.length },
          { id: "traces", label: "Traces", count: ev.traces.length },
          { id: "signals", label: "Signals", count: incident.signals.length },
        ]}
      />
      <div className="max-h-[520px] overflow-y-auto">
        {tab === "exceptions" && <Exceptions incident={incident} items={ev.exceptions} />}
        {tab === "logs" && (
          <ul>
            {ev.log_templates.length === 0 && <Empty>No unusual log patterns.</Empty>}
            {ev.log_templates.map((t) => (
              <li key={t.id} className="border-b border-line px-4 py-3 last:border-0">
                <div className="flex flex-wrap items-center gap-2">
                  <Chip>{t.service}</Chip>
                  <span className={cx("font-mono text-[11px]", t.level === "ERROR" ? "text-bad" : "text-warn")}>{t.level}</span>
                  {t.new && <Badge tone="bad">new</Badge>}
                  <span className="num ml-auto font-mono text-xs text-ink-2">
                    {t.per_minute}/min <span className="text-faint">vs {t.baseline_per_minute}/min usual</span>
                  </span>
                </div>
                <div className="mt-1.5 font-mono text-[12.5px] break-words text-ink">{t.template}</div>
                {t.examples?.[0] && (
                  <div className="mt-1 truncate font-mono text-[11.5px] text-muted">e.g. {t.examples[0].line}</div>
                )}
              </li>
            ))}
          </ul>
        )}
        {tab === "changes" && <Changes incidentId={incident.id} items={deploys} />}
        {tab === "traces" && (
          <div className="flex flex-col gap-4 p-4">
            {ev.traces.length === 0 && <Empty>No slow or failed traces in the window.</Empty>}
            {ev.traces.map((t) => (
              <Waterfall key={t.trace_id} trace={t} />
            ))}
          </div>
        )}
        {tab === "signals" && (
          <table className="w-full text-[13px]">
            <tbody>
              {incident.signals.map((s) => (
                <tr key={s.id} className="border-b border-line last:border-0">
                  <td className="w-20 px-4 py-2 font-mono text-[11px] text-faint">{clock(s.started_at)}</td>
                  <td className="w-24 py-2"><Chip>{s.service}</Chip></td>
                  <td className="w-20 py-2 font-mono text-[11px] text-muted uppercase">{KIND_OF_SIGNAL[s.type] ?? "signal"}</td>
                  <td className="py-2 pr-4 text-ink-2">{describeSignal(s)}</td>
                  <td className="py-2 pr-4 text-right">{s.ended_at ? <span className="text-xs text-ok">cleared</span> : <span className="text-xs text-bad">active</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function Exceptions({ incident, items }: { incident: IncidentDetail; items: ExceptionInfo[] }) {
  if (!items.length) return <Empty>No exceptions in the window.</Empty>;
  return (
    <ul>
      {items.map((e) => (
        <ExceptionRow key={e.fingerprint} incidentId={incident.id} item={e} />
      ))}
    </ul>
  );
}

function ExceptionRow({ incidentId, item }: { incidentId: number; item: ExceptionInfo }) {
  const [stack, setStack] = useState(false);
  const [code, setCode] = useState<{ file: string; version: string; lines: string } | { error: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const frame = item.frames[item.frames.length - 1];

  const loadCode = async () => {
    if (!frame) return;
    const [file, fn] = frame.split(":");
    setLoading(true);
    try {
      setCode(
        await post(`${API}/tools/get_code_context?incident_id=${incidentId}`, {
          service: item.service,
          file,
          function: fn,
          radius: 12,
          version: item.version,
        }),
      );
    } catch (err) {
      setCode({ error: (err as Error).message });
    } finally {
      setLoading(false);
    }
  };

  return (
    <li className="border-b border-line px-4 py-3 last:border-0">
      <div className="flex flex-wrap items-center gap-2">
        <Chip>{item.service}</Chip>
        <Chip>{item.version === "main" ? "main" : item.version.slice(0, 7)}</Chip>
        {item.new && <Badge tone="bad">new fingerprint</Badge>}
        <span className="num ml-auto font-mono text-xs text-ink-2">
          {item.total} total · {item.per_minute}/min
        </span>
      </div>
      <div className="mt-1.5 font-mono text-[13px] break-words">
        <span className="text-bad">{item.type}</span>
        <span className="text-ink">: {item.message}</span>
      </div>
      <div className="mt-1 font-mono text-[11.5px] text-muted">
        {item.frames.join(" → ")} <span className="text-faint">· {item.fingerprint}</span>
      </div>
      <div className="mt-2 flex gap-2">
        {frame && (
          <Button size="sm" onClick={loadCode} disabled={loading}>
            {loading ? "Loading…" : "Show code"}
          </Button>
        )}
        {item.stack && (
          <Button size="sm" variant="ghost" onClick={() => setStack(!stack)}>
            {stack ? "Hide stack" : "Stack trace"}
          </Button>
        )}
      </div>
      {code && (
        <div className="mt-2 border border-line bg-bg">
          {"error" in code ? (
            <div className="px-3 py-2 text-xs text-bad">{code.error}</div>
          ) : (
            <>
              <div className="border-b border-line px-3 py-1.5 font-mono text-[11px] text-muted">
                {code.file} @ {code.version.slice(0, 7)}
              </div>
              <CodeLines text={code.lines} />
            </>
          )}
        </div>
      )}
      {stack && item.stack && (
        <pre className="mt-2 max-h-60 overflow-auto border border-line bg-bg p-3 font-mono text-[11.5px] leading-relaxed whitespace-pre text-ink-2">
          {item.stack}
        </pre>
      )}
    </li>
  );
}

function CodeLines({ text }: { text: string }) {
  return (
    <pre className="max-h-72 overflow-auto py-2 font-mono text-[12px] leading-[1.55]">
      {text.split("\n").map((line, i) => {
        const hit = line.slice(4, 5) === ">";
        return (
          <div key={i} className={cx("px-3 whitespace-pre", hit ? "bg-bad/15 text-ink" : "text-ink-2")}>
            <span className="text-faint select-none">{line.slice(0, 5)}</span>
            {line.slice(5)}
          </div>
        );
      })}
    </pre>
  );
}

function Changes({ incidentId, items }: { incidentId: number; items: ChangeEvent[] }) {
  if (!items.length) return <Empty>No deploys, config or flag changes before this incident.</Empty>;
  return (
    <ul>
      {items.map((c) => (
        <ChangeRow key={c.id} incidentId={incidentId} change={c} />
      ))}
    </ul>
  );
}

function ChangeRow({ incidentId, change }: { incidentId: number; change: ChangeEvent }) {
  const [diff, setDiff] = useState<{ files: { file: string; added: number; removed: number }[]; diff: string } | { error: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const canDiff = change.type === "deploy" && change.service && change.from_version && change.to_version;

  const loadDiff = async () => {
    setLoading(true);
    try {
      setDiff(
        await post(`${API}/tools/diff_deploy?incident_id=${incidentId}`, {
          service: change.service,
          from_version: change.from_version,
          to_version: change.to_version,
        }),
      );
    } catch (err) {
      setDiff({ error: (err as Error).message });
    } finally {
      setLoading(false);
    }
  };

  return (
    <li className="border-b border-line px-4 py-3 last:border-0">
      <div className="flex flex-wrap items-center gap-2 text-[13px]">
        <span className="font-mono text-[11px] text-brass uppercase">{change.details?.rollback ? "rollback" : change.type}</span>
        {change.service && <Chip>{change.service}</Chip>}
        {change.to_version && (
          <span className="font-mono text-xs text-muted">
            {change.from_version?.slice(0, 7)} → {change.to_version.slice(0, 7)}
          </span>
        )}
        <span className="ml-auto font-mono text-[11px] text-faint">{clock(change.timestamp)}</span>
      </div>
      <div className="mt-1 text-[13px] text-ink">{change.commit_message ?? JSON.stringify(change.details ?? {})}</div>
      <div className="mt-0.5 text-xs text-muted">
        by {change.actor}
        {change.changed_files?.length ? ` · ${change.changed_files.join(", ")}` : ""}
      </div>
      {canDiff && !diff && (
        <Button size="sm" className="mt-2" onClick={loadDiff} disabled={loading}>
          {loading ? "Loading…" : "Show diff"}
        </Button>
      )}
      {diff && "error" in diff && <div className="mt-2 text-xs text-bad">{diff.error}</div>}
      {diff && !("error" in diff) && (
        <pre className="mt-2 max-h-80 overflow-auto border border-line bg-bg py-2 font-mono text-[12px] leading-[1.55]">
          {diff.diff.split("\n").map((line, i) => (
            <div
              key={i}
              className={cx(
                "px-3 whitespace-pre",
                line.startsWith("+") && !line.startsWith("+++") && "bg-ok/10 text-ok",
                line.startsWith("-") && !line.startsWith("---") && "bg-bad/10 text-bad",
                line.startsWith("@@") && "text-info",
                !/^[-+@]/.test(line) && "text-ink-2",
              )}
            >
              {line || " "}
            </div>
          ))}
        </pre>
      )}
    </li>
  );
}

function Waterfall({ trace }: { trace: TraceInfo }) {
  const total = Math.max(trace.duration_ms, 1);
  return (
    <div className="border border-line">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2 text-[13px]">
        <span className="font-medium text-ink">{trace.root}</span>
        {trace.error && <Badge tone="bad">error</Badge>}
        <span className="num ml-auto font-mono text-xs text-ink-2">{fmtMs(trace.duration_ms)}</span>
        <span className="font-mono text-[11px] text-faint">{trace.span_count} spans · {trace.trace_id.slice(0, 12)}</span>
      </div>
      <div className="px-3 py-2">
        {trace.critical_spans.map((s, i) => (
          <div key={i} className="grid grid-cols-[220px_1fr_70px] items-center gap-3 py-[3px]">
            <div className="truncate font-mono text-[11.5px] text-ink-2" style={{ paddingLeft: Math.min(i, 6) * 8 }}>
              <span className="text-muted">{s.service}</span> {s.name}
            </div>
            <div className="relative h-3 bg-bg">
              <div
                className={cx("absolute top-0 h-3", s.error ? "bg-bad" : s.kind === "client" ? "bg-info/50" : "bg-info")}
                style={{ left: `${(s.offset_ms / total) * 100}%`, width: `${Math.max(0.5, (s.duration_ms / total) * 100)}%` }}
              />
            </div>
            <div className="num text-right font-mono text-[11px] text-muted">{fmtMs(s.duration_ms)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

const fmtMs = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)} s` : `${Math.round(v)} ms`);
