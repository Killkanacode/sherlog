import { useEffect, useMemo, useRef, useState } from "react";
import { clock } from "../lib/format";
import { actionStateOf, TONE_HEX, type Tone } from "../lib/labels";
import type { TimelineEntry } from "../lib/types";
import { cx } from "./ui";

type Row =
  | { kind: "entry"; entry: TimelineEntry }
  | { kind: "signals"; entries: TimelineEntry[] };

const STYLE: Record<string, { label: string; tone: Tone }> = {
  status: { label: "Status", tone: "muted" },
  evidence: { label: "Evidence", tone: "brass" },
  seen_before: { label: "Memory", tone: "brass" },
  triage: { label: "Triage", tone: "info" },
  hypothesis: { label: "Hypothesis", tone: "info" },
  tool: { label: "Tool call", tone: "muted" },
  diagnosis: { label: "Conclusion", tone: "ok" },
  action: { label: "Action", tone: "brass" },
  investigation_failed: { label: "Failed", tone: "bad" },
  signal: { label: "Signal", tone: "bad" },
  signal_cleared: { label: "Cleared", tone: "ok" },
};

// The investigation as it happens: signals, evidence, each hypothesis and
// tool call, the conclusion and every action with its verification.
export function Timeline({ entries, live }: { entries: TimelineEntry[]; live: boolean }) {
  const [showCleared, setShowCleared] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  const rows = useMemo(() => {
    const out: Row[] = [];
    for (const entry of entries) {
      if (entry.kind === "signal_cleared" && !showCleared) continue;
      const prev = out[out.length - 1];
      if (entry.kind === "signal") {
        if (prev?.kind === "signals") prev.entries.push(entry);
        else out.push({ kind: "signals", entries: [entry] });
      } else out.push({ kind: "entry", entry });
    }
    return out;
  }, [entries, showCleared]);

  useEffect(() => {
    const el = box.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [rows.length]);

  const cleared = entries.filter((e) => e.kind === "signal_cleared").length;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        ref={box}
        onScroll={(e) => {
          const el = e.currentTarget;
          pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
        className="min-h-0 flex-1 overflow-y-auto px-4 py-3"
      >
        <ol className="relative">
          <span className="absolute top-2 bottom-2 left-[71px] w-px bg-line" />
          {rows.map((row, i) =>
            row.kind === "signals" ? (
              <SignalGroup key={`s-${i}`} entries={row.entries} />
            ) : (
              <Entry key={`${row.entry.ts}-${i}`} entry={row.entry} />
            ),
          )}
          {live && (
            <li className="relative grid grid-cols-[64px_16px_1fr] items-center gap-0 py-1.5">
              <span />
              <span className="relative z-10 ml-[3px] size-2 bg-info blink" />
              <span className="text-xs text-muted">Watching…</span>
            </li>
          )}
        </ol>
      </div>
      {cleared > 0 && (
        <div className="border-t border-line px-4 py-2 text-xs">
          <label className="flex cursor-pointer items-center gap-2 text-muted">
            <input type="checkbox" checked={showCleared} onChange={(e) => setShowCleared(e.target.checked)} className="accent-[#d4a84b]" />
            Show {cleared} cleared signal{cleared > 1 ? "s" : ""}
          </label>
        </div>
      )}
    </div>
  );
}

function Marker({ tone }: { tone: Tone }) {
  return <span className="relative z-10 mt-[5px] ml-[3px] size-2" style={{ background: TONE_HEX[tone] }} />;
}

function Entry({ entry }: { entry: TimelineEntry }) {
  const [open, setOpen] = useState(false);
  const style = STYLE[entry.kind] ?? { label: entry.kind, tone: "muted" as Tone };
  let tone = style.tone;
  if (entry.kind === "action" && entry.data?.action) tone = actionStateOf(entry.data.action.state).tone;
  const isTool = entry.kind === "tool";
  const result = isTool ? entry.data?.result : null;

  return (
    <li className="relative grid grid-cols-[64px_16px_1fr] gap-0 py-1.5">
      <span className="num pt-px font-mono text-[11px] text-faint">{clock(entry.ts)}</span>
      <Marker tone={tone} />
      <div className="min-w-0 pl-2">
        <div className="text-[10.5px] font-medium tracking-[0.09em] uppercase" style={{ color: TONE_HEX[tone] }}>
          {style.label}
          {isTool && entry.data?.step != null && <span className="text-faint"> · step {entry.data.step}</span>}
        </div>
        {isTool ? (
          <button onClick={() => setOpen(!open)} className="mt-0.5 block w-full text-left font-mono text-[12.5px] break-all text-ink hover:text-brass">
            {entry.text}
            <span className="ml-2 text-faint">{open ? "−" : "+"}</span>
          </button>
        ) : (
          <div
            className={cx(
              "mt-0.5 text-[13px] break-words",
              entry.kind === "hypothesis" ? "text-ink italic" : entry.kind === "status" ? "font-medium text-ink" : "text-ink-2",
              entry.kind === "diagnosis" && "font-medium text-ink",
            )}
          >
            {entry.text}
          </div>
        )}
        {open && result != null && (
          <pre className="mt-1.5 max-h-64 overflow-auto border border-line bg-bg p-2 font-mono text-[11.5px] leading-relaxed whitespace-pre-wrap text-ink-2">
            {pretty(result)}
          </pre>
        )}
      </div>
    </li>
  );
}

function SignalGroup({ entries }: { entries: TimelineEntry[] }) {
  const [open, setOpen] = useState(entries.length <= 3);
  const services = [...new Set(entries.map((e) => e.data?.service).filter(Boolean))];
  return (
    <li className="relative grid grid-cols-[64px_16px_1fr] gap-0 py-1.5">
      <span className="num pt-px font-mono text-[11px] text-faint">{clock(entries[0].ts)}</span>
      <Marker tone="bad" />
      <div className="min-w-0 pl-2">
        <button onClick={() => setOpen(!open)} className="text-left">
          <div className="text-[10.5px] font-medium tracking-[0.09em] text-bad uppercase">
            {entries.length} signal{entries.length > 1 ? "s" : ""} <span className="text-faint">{open ? "−" : "+"}</span>
          </div>
          {!open && <div className="mt-0.5 text-[13px] text-ink-2">Anomalies on {services.join(", ")}</div>}
        </button>
        {open && (
          <ul className="mt-1 flex flex-col gap-0.5">
            {entries.map((e, i) => (
              <li key={i} className="text-[13px] break-words text-ink-2">
                {e.text}
              </li>
            ))}
          </ul>
        )}
      </div>
    </li>
  );
}

function pretty(value: unknown): string {
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}
