import { useMemo } from "react";
import {
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { promRange } from "../lib/api";
import { clock, latency, ms, pct } from "../lib/format";
import { usePoll } from "../lib/hooks";
import type { IncidentDetail } from "../lib/types";

const AXIS = { fill: "#8a8c94", fontSize: 11, fontFamily: "IBM Plex Mono" };

// What the service's users felt: error rate and p95 latency around the
// incident, with when Sherlog detected it, acted and saw it recover.
export function ImpactChart({ incident, service }: { incident: IncidentDetail; service: string }) {
  const created = ms(incident.created_at);
  const firstSignal = Math.min(created, ...incident.signals.map((s) => ms(s.started_at)));
  const resolved = incident.resolved_at ? ms(incident.resolved_at) : null;
  const start = firstSignal - 6 * 60 * 1000;
  const live = !resolved;

  const series = usePoll(
    async () => {
      const end = resolved ? resolved + 4 * 60 * 1000 : Date.now();
      const step = Math.max(5, Math.round((end - start) / 1000 / 150));
      const job = `job="${service}"`;
      const [errors, p95] = await Promise.all([
        promRange(
          `(sum(rate(http_server_duration_seconds_count{${job},status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_duration_seconds_count{${job}}[1m]))`,
          start,
          end,
          step,
        ),
        promRange(`histogram_quantile(0.95, sum by (le) (rate(http_server_duration_seconds_bucket{${job}}[1m])))`, start, end, step),
      ]);
      const rows = new Map<number, { t: number; errors?: number | null; p95?: number | null }>();
      for (const p of errors[0]?.points ?? []) rows.set(p.t, { t: p.t, errors: p.v });
      for (const p of p95[0]?.points ?? []) rows.set(p.t, { ...(rows.get(p.t) ?? { t: p.t }), p95: p.v });
      return [...rows.values()].sort((a, b) => a.t - b.t);
    },
    live ? 10000 : 0,
    [service, incident.id, resolved],
  );

  const markers = useMemo(() => {
    const out: { t: number; label: string; color: string }[] = [{ t: created, label: "Detected", color: "#ee5b4d" }];
    for (const a of incident.actions) {
      if (a.executed_at) out.push({ t: ms(a.executed_at), label: a.type, color: "#d4a84b" });
      if (a.verification?.recovered_at && a.state === "recovered")
        out.push({ t: ms(a.verification.recovered_at), label: "Recovered", color: "#4fb87e" });
    }
    if (resolved && !out.some((m) => m.label === "Recovered")) out.push({ t: resolved, label: "Closed", color: "#4fb87e" });
    return out;
  }, [incident.actions, created, resolved]);

  const data = series.data ?? [];
  const end = data.length ? data[data.length - 1].t : Date.now();

  return (
    <div className="h-[230px] w-full">
      {data.length > 1 ? (
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data} margin={{ top: 18, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="#25272c" vertical={false} />
            <ReferenceArea x1={firstSignal} x2={resolved ?? end} fill="#ee5b4d" fillOpacity={0.06} ifOverflow="hidden" />
            <XAxis
              dataKey="t"
              type="number"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(t) => clock(t).slice(0, 5)}
              tick={AXIS}
              stroke="#34373e"
              minTickGap={40}
            />
            <YAxis yAxisId="err" tickFormatter={(v) => pct(v, 0)} tick={AXIS} stroke="#34373e" width={42} tickCount={6} domain={[0, (max: number) => Math.max(0.05, Math.ceil(max * 20) / 20)]} />
            <YAxis yAxisId="lat" orientation="right" tickFormatter={(v) => latency(v)} tick={AXIS} stroke="#34373e" width={56} />
            <Tooltip
              contentStyle={{ background: "#181a1e", border: "1px solid #34373e", borderRadius: 0, fontSize: 12 }}
              labelStyle={{ color: "#8a8c94", fontFamily: "IBM Plex Mono" }}
              labelFormatter={(t) => clock(Number(t))}
              formatter={(v, name) => [name === "errors" ? pct(Number(v)) : latency(Number(v)), name === "errors" ? "error rate" : "p95 latency"]}
            />
            {markers.map((m) => (
              <ReferenceLine
                key={`${m.label}-${m.t}`}
                yAxisId="err"
                x={m.t}
                stroke={m.color}
                strokeDasharray="3 3"
                label={{ value: m.label, position: "top", fill: m.color, fontSize: 11, fontFamily: "IBM Plex Mono" }}
              />
            ))}
            <Line yAxisId="err" dataKey="errors" stroke="#ee5b4d" strokeWidth={1.75} dot={false} isAnimationActive={false} connectNulls />
            <Line yAxisId="lat" dataKey="p95" stroke="#74a7f2" strokeWidth={1.5} dot={false} isAnimationActive={false} connectNulls />
          </ComposedChart>
        </ResponsiveContainer>
      ) : (
        <div className="flex h-full items-center justify-center text-[13px] text-muted">
          {series.error
            ? `Metrics unavailable: ${series.error}`
            : series.data == null
              ? "Loading metrics…"
              : "No metrics for this window: Prometheus keeps the last 24 hours."}
        </div>
      )}
    </div>
  );
}
