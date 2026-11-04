import type { ReactNode } from "react";
import { Panel } from "../components/ui";
import { API, get } from "../lib/api";
import { compact, num } from "../lib/format";
import { usePoll } from "../lib/hooks";
import type { Stats } from "../lib/types";

const STAGES: { n: string; name: string; engine: string; llm?: boolean; lines: string[] }[] = [
  {
    n: "01",
    name: "Detect",
    engine: "No LLM",
    lines: ["EWMA baselines on RED metrics", "Drain3 log patterns", "Exception fingerprints", "Container state"],
  },
  {
    n: "02",
    name: "Correlate",
    engine: "No LLM",
    lines: ["Signals joined by the service graph", "Deepest anomalous service", "Retry storms, starved callers"],
  },
  {
    n: "03",
    name: "Condense",
    engine: "No LLM",
    lines: ["Changes, new patterns, exceptions", "Slow and failed traces", "Thousands of lines → a few K tokens"],
  },
  {
    n: "04",
    name: "Triage",
    engine: "Muse Spark Open",
    llm: true,
    lines: ["Cheap first pass per suspect", "What changed, what's new", "What is noise"],
  },
  {
    n: "05",
    name: "Investigate",
    engine: "Muse Spark",
    llm: true,
    lines: ["Hypothesis → tool → evidence", "10 read-only tools, code at the deployed version", "Strict JSON diagnosis"],
  },
  {
    n: "06",
    name: "Act and verify",
    engine: "platform-api",
    lines: ["L0 suggest · L1 approve · L2 auto", "Undo recorded first", "Healthy 30 s or revert at 3 min"],
  },
  {
    n: "07",
    name: "Learn",
    engine: "Postgres",
    lines: ["Postmortem in Markdown", "Incident memory by fingerprint", "“Seen before” on repeats"],
  },
];

export function HowItWorks() {
  const stats = usePoll(() => get<Stats>(`${API}/stats`), 0);
  const s = stats.data;
  return (
    <div className="flex flex-col gap-5">
      <div>
        <h1 className="text-[22px] font-semibold tracking-tight text-ink">How Sherlog works</h1>
        <p className="mt-1 text-[13px] text-muted">
          Cheap, deterministic detection runs all the time. Meta's models are called only once there is an incident,
          and only on evidence that has already been condensed.
        </p>
      </div>

      <Panel title="Pipeline" bodyClass="p-0">
        <div className="grid grid-cols-1 md:grid-cols-7">
          {STAGES.map((stage, i) => (
            <div key={stage.n} className={`relative border-line px-4 py-4 ${i ? "border-t md:border-t-0 md:border-l" : ""}`}>
              <div className="flex items-center gap-2">
                <span className="font-mono text-[11px] text-faint">{stage.n}</span>
                <span className="text-[15px] font-semibold text-ink">{stage.name}</span>
              </div>
              <div className={`mt-1.5 inline-block border px-1.5 py-px font-mono text-[10.5px] ${stage.llm ? "border-brass/50 text-brass" : "border-line text-muted"}`}>
                {stage.engine}
              </div>
              <ul className="mt-3 flex flex-col gap-1.5">
                {stage.lines.map((l) => (
                  <li key={l} className="text-[12.5px] leading-snug text-ink-2">
                    {l}
                  </li>
                ))}
              </ul>
              {i < STAGES.length - 1 && (
                <span className="absolute top-5 -right-[7px] z-10 hidden size-3 rotate-45 border-t border-r border-line-strong bg-panel md:block" />
              )}
            </div>
          ))}
        </div>
        <div className="flex items-center gap-3 border-t border-line px-4 py-2.5 text-xs text-muted">
          <svg width="44" height="14" viewBox="0 0 44 14" aria-hidden>
            <path d="M42 2 V7 H2 V2" fill="none" stroke="#d4a84b" strokeWidth="1.5" />
            <path d="M-1 5 L2 1 L5 5" fill="none" stroke="#d4a84b" strokeWidth="1.5" />
          </svg>
          A mitigation that doesn't restore service is reverted and goes back to step 05 with what failed. After two
          failures Sherlog escalates to a human.
        </div>
      </Panel>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <Panel title="Architecture" bodyClass="p-5">
          <Architecture />
        </Panel>
        <Panel title="Right now">
          <dl className="grid grid-cols-[1fr_auto] gap-x-6 gap-y-3 text-[13px]">
            <dt className="text-muted">Log lines read</dt>
            <dd className="num font-mono text-ink">{compact(s?.log_lines_seen)}</dd>
            <dt className="text-muted">Log patterns learned</dt>
            <dd className="num font-mono text-ink">{num(s?.log_templates)}</dd>
            <dt className="text-muted">Exception fingerprints</dt>
            <dd className="num font-mono text-ink">{num(s?.exception_fingerprints)}</dd>
            <dt className="text-muted">Lines in incident windows</dt>
            <dd className="num font-mono text-ink">{compact(s?.funnel.log_lines_in_windows)}</dd>
            <dt className="text-muted">Tokens sent to models for them</dt>
            <dd className="num font-mono text-brass">{compact(s?.funnel.estimated_tokens)}</dd>
            <dt className="text-muted">Incidents handled</dt>
            <dd className="num font-mono text-ink">{num(s?.incidents_total)}</dd>
          </dl>
        </Panel>
      </div>
    </div>
  );
}

function Box({ title, sub, children, accent }: { title: string; sub?: string; children?: ReactNode; accent?: boolean }) {
  return (
    <div className={`border px-3 py-2.5 ${accent ? "border-brass/60 bg-brass/5" : "border-line-strong bg-raised"}`}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[13px] font-semibold text-ink">{title}</span>
        {sub && <span className="font-mono text-[10.5px] text-muted">{sub}</span>}
      </div>
      {children && <div className="mt-1.5 text-[12px] leading-snug text-ink-2">{children}</div>}
    </div>
  );
}

const Arrow = ({ label }: { label: string }) => (
  <div className="flex items-center justify-center gap-2 py-1.5 font-mono text-[10.5px] text-muted">
    <span>↓</span>
    {label}
  </div>
);

function Architecture() {
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_1.15fr_1fr]">
      <div>
        <div className="mb-2 text-[11px] tracking-[0.09em] text-muted uppercase">System under test</div>
        <Box title="Shop" sub="4 services">gateway → orders → inventory, payments · Postgres · Redis · Locust traffic</Box>
        <Arrow label="OTLP" />
        <Box title="Observability" sub="OTel">Prometheus · Loki · Tempo service graph</Box>
        <div className="mt-4 border border-dashed border-bad/40 px-3 py-2.5">
          <div className="text-[13px] font-semibold text-ink">Chaos controller</div>
          <div className="mt-1 text-[12px] text-ink-2">Injects faults, keeps the answer key. Isolated network: Sherlog has no route to it.</div>
        </div>
      </div>
      <div>
        <div className="mb-2 text-[11px] tracking-[0.09em] text-muted uppercase">Sherlog</div>
        <Box title="Detectors" sub="always on">metrics · logs · exceptions · containers</Box>
        <Arrow label="incident" />
        <Box title="Evidence funnel" sub="no LLM">changes · patterns · exceptions · traces · hint</Box>
        <Arrow label="~5K tokens" />
        <Box title="Investigator" sub="Meta models" accent>
          Muse Spark Open triage, then Muse Spark with 10 tools
        </Box>
        <Arrow label="diagnosis + action" />
        <Box title="Actions, verification, memory">autonomy policy · undo · revert · postmortem</Box>
      </div>
      <div>
        <div className="mb-2 text-[11px] tracking-[0.09em] text-muted uppercase">What it can touch</div>
        <Box title="platform-api" sub="CD">deploy · rollback · restart · scale · config · flags · change events · source at any version</Box>
        <div className="mt-4">
          <Box title="This console" sub="SSE">live map · incidents · approvals · postmortems · settings</Box>
        </div>
        <div className="mt-4 text-[12px] leading-snug text-muted">
          Read-only access to telemetry, deploy history and code. Write access only through the platform's action
          API, on a whitelist of services.
        </div>
      </div>
    </div>
  );
}
