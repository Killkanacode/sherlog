import { useState } from "react";
import { LiveIncidentCard } from "../components/IncidentList";
import { ServiceMap } from "../components/ServiceMap";
import { Badge, Button, Chip, cx, Dot, Empty, Panel } from "../components/ui";
import { API, CHAOS, get, post } from "../lib/api";
import { between, duration, ms } from "../lib/format";
import { useDebounced, useNow, usePoll } from "../lib/hooks";
import { categoryOf } from "../lib/labels";
import { useSession } from "../lib/session";
import { useStream } from "../lib/stream";
import type { ChaosRun, IncidentSummary, Scenario, Topology } from "../lib/types";
import { useVersions } from "./Live";

const GROUPS: { title: string; test: (s: Scenario) => boolean }[] = [
  { title: "Red herrings", test: (s) => s.inject.length > 1 },
  { title: "Bad deploys", test: (s) => s.inject.every((i) => i.type === "deploy") },
  { title: "Config and flags", test: (s) => s.inject.some((i) => ["config", "flag"].includes(i.type)) },
  { title: "Network", test: (s) => s.inject.some((i) => ["latency", "partition"].includes(i.type)) },
  { title: "Processes and datastores", test: () => true },
];

const DEMO = new Set(["09-cascade-inventory-slow-with-noise", "03-orders-keyerror-deploy"]);

export function ChaosLab() {
  const now = useNow();
  const scenarios = usePoll(() => get<Scenario[]>(`${CHAOS}/scenarios`), 0);
  const runs = usePoll(() => get<ChaosRun[]>(`${CHAOS}/runs`), 3000);
  const incidents = usePoll(() => get<IncidentSummary[]>(`${API}/incidents`), 5000);
  const topology = usePoll(() => get<Topology>(`${API}/topology`), 4000);
  const { versions } = useVersions();
  const { can } = useSession();
  const operator = can("operator");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reveal, setReveal] = useState(false);

  const refresh = useDebounced(() => {
    incidents.refresh();
    topology.refresh();
  });
  useStream((m) => ["incident", "action", "signal", "postmortem"].includes(m.type) && refresh());

  const active = (runs.data ?? []).find((r) => r.status === "active") ?? null;
  const scenario = active ? scenarios.data?.find((s) => s.id === active.scenario_id) : null;
  const incident = active
    ? (incidents.data ?? [])
        .filter((i) => ms(i.created_at) >= ms(active.started_at) - 5000)
        .sort((a, b) => a.id - b.id)[0]
    : null;

  const inject = async (id: string) => {
    setBusy(id);
    setError(null);
    setReveal(false);
    try {
      await post(`${CHAOS}/scenarios/${id}/start`);
      runs.refresh();
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const reset = async () => {
    setBusy("reset");
    setError(null);
    try {
      await post(`${CHAOS}/reset`);
      runs.refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const grouped = new Map<string, Scenario[]>();
  for (const s of scenarios.data ?? []) {
    const group = GROUPS.find((g) => g.test(s))!.title;
    grouped.set(group, [...(grouped.get(group) ?? []), s]);
  }

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight text-ink">Chaos Lab</h1>
          <p className="mt-1 text-[13px] text-muted">
            Break the shop for real. Sherlog never sees this panel: it has no route to the chaos controller and works
            only from telemetry, deploy events and source code.
          </p>
        </div>
        <Button variant="danger" onClick={reset} disabled={busy === "reset" || !operator} title={operator ? undefined : "Needs the operator role"}>
          {busy === "reset" ? "Resetting…" : "Reset to baseline"}
        </Button>
      </div>
      {error && <div className="border border-bad/40 bg-bad/10 px-4 py-2 text-[13px] text-bad">{error}</div>}

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <Panel
          title="Live system"
          aside={active ? <Badge tone="bad" live>fault active</Badge> : <Badge tone="ok">baseline</Badge>}
          bodyClass="px-2 py-3"
        >
          <ServiceMap topology={topology.data} versions={versions} height={300} />
        </Panel>

        <Panel title="Current run">
          {!active || !scenario ? (
            <div className="flex h-full flex-col justify-center py-6 text-center">
              <div className="text-[15px] text-ink">No fault running.</div>
              <div className="mt-1 text-[13px] text-muted">Pick a scenario below. Detection usually takes 10–30 s.</div>
            </div>
          ) : (
            <div className="flex flex-col gap-4">
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs text-muted">{scenario.id.slice(0, 2)}</span>
                  <span className="text-[16px] font-semibold text-ink">{scenario.title}</span>
                </div>
                <div className="mt-1 text-[13px] text-muted">{scenario.description}</div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <div className="text-[11px] tracking-[0.09em] text-muted uppercase">Fault injected</div>
                  <div className="num mt-1 font-mono text-[20px] text-ink">{duration(between(active.started_at, now))} ago</div>
                </div>
                <div>
                  <div className="text-[11px] tracking-[0.09em] text-muted uppercase">Time to detect</div>
                  <div className={cx("num mt-1 font-mono text-[20px]", incident ? "text-ok" : "text-warn")}>
                    {incident ? duration(between(active.started_at, incident.created_at)) : "waiting…"}
                  </div>
                </div>
              </div>
              {incident ? (
                <LiveIncidentCard incident={incident} onChange={incidents.refresh} />
              ) : (
                <div className="flex items-center gap-2 border border-line px-4 py-3 text-[13px] text-muted">
                  <Dot tone="warn" live /> Sherlog is watching the telemetry…
                </div>
              )}
              <AnswerKey run={active} incident={incident} open={reveal} onToggle={() => setReveal(!reveal)} />
            </div>
          )}
        </Panel>
      </div>

      {GROUPS.filter((g) => grouped.has(g.title)).map(({ title: group }) => [group, grouped.get(group)!] as const).map(([group, items]) => (
        <section key={group}>
          <h2 className="mb-2 text-[11px] font-semibold tracking-[0.09em] text-muted uppercase">{group}</h2>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 2xl:grid-cols-3">
            {items.map((s) => (
              <article
                key={s.id}
                className={cx(
                  "flex flex-col border bg-panel px-4 py-3",
                  active?.scenario_id === s.id ? "border-bad" : "border-line",
                )}
              >
                <div className="flex items-start gap-3">
                  <span className="font-mono text-[13px] text-faint">{s.id.slice(0, 2)}</span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="text-[14px] font-semibold text-ink">{s.title}</h3>
                      {DEMO.has(s.id) && <span className="font-mono text-[10px] tracking-wider text-brass uppercase">demo</span>}
                    </div>
                    <p className="mt-1 text-[13px] text-muted">{s.description}</p>
                  </div>
                </div>
                <div className="mt-3 flex items-center gap-1.5 pl-7">
                  {s.inject.map((i, n) => (
                    <Chip key={n}>
                      {i.type}
                      {i.service ? ` · ${i.service}` : ""}
                    </Chip>
                  ))}
                  <Button
                    size="sm"
                    variant={active ? "default" : "primary"}
                    className="ml-auto"
                    disabled={!!active || busy !== null || !operator}
                    title={!operator ? "Needs the operator role" : active ? "Reset the current run first" : undefined}
                    onClick={() => inject(s.id)}
                  >
                    {busy === s.id ? "Injecting…" : active?.scenario_id === s.id ? "Running" : "Inject"}
                  </Button>
                </div>
              </article>
            ))}
          </div>
        </section>
      ))}
      {!scenarios.data && <Empty>{scenarios.error ? `Chaos controller unreachable: ${scenarios.error}` : "Loading scenarios…"}</Empty>}
    </div>
  );
}

function AnswerKey({
  run,
  incident,
  open,
  onToggle,
}: {
  run: ChaosRun;
  incident: IncidentSummary | null | undefined;
  open: boolean;
  onToggle: () => void;
}) {
  const truth = run.ground_truth;
  const cause = incident?.root_cause && incident.root_cause.confidence > 0 ? incident.root_cause : null;
  const services = truth.services_accepted ?? [truth.service];
  const categories = truth.categories_accepted ?? [truth.category];
  return (
    <div className="border-t border-line pt-3">
      <button onClick={onToggle} className="text-[13px] text-muted hover:text-ink">
        {open ? "Hide answer key" : "Reveal answer key"}
      </button>
      {open && (
        <table className="mt-2 w-full text-[13px]">
          <thead>
            <tr className="text-left text-[11px] tracking-[0.09em] text-faint uppercase">
              <th className="py-1 font-medium" />
              <th className="py-1 font-medium">Expected</th>
              <th className="py-1 font-medium">Sherlog</th>
            </tr>
          </thead>
          <tbody>
            <Row label="Service" expected={truth.service} got={cause?.service} ok={!!cause && services.includes(cause.service)} />
            <Row
              label="Category"
              expected={categoryOf(truth.category)}
              got={cause ? categoryOf(cause.category) : undefined}
              ok={!!cause && categories.includes(cause.category)}
            />
            <Row
              label="Action"
              expected={truth.acceptable_actions.join(" or ")}
              got={incident?.pending_action?.type}
              ok={!!incident?.pending_action && truth.acceptable_actions.includes(incident.pending_action.type)}
            />
          </tbody>
        </table>
      )}
    </div>
  );
}

function Row({ label, expected, got, ok }: { label: string; expected: string; got?: string; ok: boolean }) {
  return (
    <tr className="border-t border-line">
      <td className="py-1.5 pr-3 text-muted">{label}</td>
      <td className="py-1.5 pr-3 text-ink-2">{expected}</td>
      <td className={cx("py-1.5", got ? (ok ? "text-ok" : "text-bad") : "text-faint")}>
        {got ? `${got} ${ok ? "✓" : "✗"}` : "–"}
      </td>
    </tr>
  );
}
