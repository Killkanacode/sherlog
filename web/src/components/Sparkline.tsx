import type { Point } from "../lib/api";
import { TONE_HEX, type Tone } from "../lib/labels";

/** A bare line of the last minutes; the end point is marked. */
export function Sparkline({
  points,
  tone = "muted",
  width = 132,
  height = 30,
  floor,
}: {
  points: Point[];
  tone?: Tone;
  width?: number;
  height?: number;
  floor?: number; // keep tiny values from filling the box (e.g. an error rate of 0.1%)
}) {
  const values = points.filter((p) => p.v != null) as { t: number; v: number }[];
  if (values.length < 2) return <svg width={width} height={height} />;
  const t0 = values[0].t;
  const t1 = values[values.length - 1].t;
  const max = Math.max(floor ?? 0, ...values.map((p) => p.v));
  const min = floor != null ? 0 : Math.min(...values.map((p) => p.v));
  const span = max - min || 1;
  const x = (t: number) => ((t - t0) / (t1 - t0 || 1)) * (width - 4) + 2;
  const y = (v: number) => height - 3 - ((v - min) / span) * (height - 6);
  const d = values.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const last = values[values.length - 1];
  const color = TONE_HEX[tone];
  return (
    <svg width={width} height={height} className="block">
      <path d={d} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" />
      <rect x={x(last.t) - 2} y={y(last.v) - 2} width="4" height="4" fill={color} />
    </svg>
  );
}
