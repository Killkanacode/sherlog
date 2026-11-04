import { useState } from "react";
import { cx, Dot, Panel } from "../components/ui";
import { API, get, put } from "../lib/api";
import { usePoll } from "../lib/hooks";
import type { Autonomy, Health, Policy } from "../lib/types";

const LEVELS: { id: Autonomy; name: string; tagline: string; detail: string }[] = [
  {
    id: "L0",
    name: "Suggest",
    tagline: "Sherlog diagnoses and recommends. It never acts.",
    detail: "Good for a first week: compare its calls with yours.",
  },
  {
    id: "L1",
    name: "Approve",
    tagline: "Every action waits for a human to approve it.",
    detail: "One click from the incident page or the live list. Verification and automatic revert still apply.",
  },
  {
    id: "L2",
    name: "Autonomous",
    tagline: "Low-risk rollbacks, flag flips and restarts run on their own.",
    detail: "Only above the confidence threshold. Anything riskier still waits for approval.",
  },
];

export function Settings() {
  const policy = usePoll(() => get<Policy>(`${API}/settings`), 0);
  const health = usePoll(() => get<Health>(`${API}/health`), 0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const p = policy.data;

  const update = async (change: Partial<Policy>) => {
    setSaving(true);
    setError(null);
    try {
      policy.setData(await put<Policy>(`${API}/settings`, change));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <div>
        <h1 className="text-[22px] font-semibold tracking-tight text-ink">Settings</h1>
        <p className="mt-1 text-[13px] text-muted">How far Sherlog may go on its own. Changes apply immediately and survive restarts.</p>
      </div>
      {error && <div className="border border-bad/40 bg-bad/10 px-4 py-2 text-[13px] text-bad">{error}</div>}

      <Panel title="Autonomy">
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
          {LEVELS.map((level) => {
            const active = p?.autonomy === level.id;
            return (
              <button
                key={level.id}
                disabled={saving || !p}
                onClick={() => update({ autonomy: level.id })}
                className={cx(
                  "flex flex-col justify-start border px-4 py-4 text-left transition-colors",
                  active ? "border-brass bg-brass/5" : "border-line hover:border-line-strong",
                )}
              >
                <div className="flex items-center gap-3">
                  <span className={cx("font-mono text-[13px]", active ? "text-brass" : "text-muted")}>{level.id}</span>
                  <span className="text-[16px] font-semibold text-ink">{level.name}</span>
                  {active && <span className="ml-auto font-mono text-[10px] tracking-wider text-brass uppercase">current</span>}
                </div>
                <div className="mt-2 text-[13px] text-ink-2">{level.tagline}</div>
                <div className="mt-1 text-xs text-muted">{level.detail}</div>
              </button>
            );
          })}
        </div>
      </Panel>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Panel title="Dry run">
          <div className="flex items-start gap-4">
            <button
              role="switch"
              aria-checked={p?.dry_run ?? false}
              disabled={saving || !p}
              onClick={() => p && update({ dry_run: !p.dry_run })}
              className={cx("relative mt-0.5 h-5 w-9 shrink-0 border", p?.dry_run ? "border-info bg-info/20" : "border-line-strong bg-bg")}
            >
              <span className={cx("absolute top-[3px] size-3 transition-all", p?.dry_run ? "left-[19px] bg-info" : "left-[3px] bg-muted")} />
            </button>
            <div>
              <div className="text-[14px] font-medium text-ink">{p?.dry_run ? "On: nothing will change" : "Off: actions run for real"}</div>
              <div className="mt-1 text-[13px] text-muted">
                With dry run on, Sherlog records exactly what it would have done and changes nothing. Useful to trial a
                higher autonomy level.
              </div>
            </div>
          </div>
        </Panel>

        <Panel title="Guardrails">
          <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-2 text-[13px]">
            <dt className="text-muted">L2 acts only above confidence</dt>
            <dd className="font-mono text-ink">{p ? `${Math.round(p.auto_min_confidence * 100)}%` : "–"}</dd>
            <dt className="text-muted">Healthy for this long = recovered</dt>
            <dd className="font-mono text-ink">30 s</dd>
            <dt className="text-muted">Revert if not recovered within</dt>
            <dd className="font-mono text-ink">3 min</dd>
            <dt className="text-muted">Failed mitigations before escalating</dt>
            <dd className="font-mono text-ink">2</dd>
            <dt className="text-muted">Restarting Redis or Postgres</dt>
            <dd className="font-mono text-warn">medium risk</dd>
          </dl>
          <div className="mt-4 border-t border-line pt-3 text-[13px]">
            <span className="text-muted">Allowed actions: </span>
            <span className="font-mono text-ink">rollback · restart · scale · config revert · flag off</span>
          </div>
        </Panel>
      </div>

      <Panel title="Models">
        <div className="flex items-center gap-3 text-[13px]">
          <Dot tone={health.data?.llm === "live" ? "ok" : "warn"} />
          {health.data?.llm === "live" ? (
            <span className="text-ink-2">Meta models connected: Muse Spark Open for triage, Muse Spark for the investigation.</span>
          ) : (
            <span className="text-ink-2">
              Running on stand-in models (<span className="font-mono">SHERLOG_FAKE_LLM=true</span>). The pipeline, tools and
              streaming are real; the diagnosis is not. Set the <span className="font-mono">META_*</span> and{" "}
              <span className="font-mono">OPEN_MODEL_*</span> variables to connect Meta's models.
            </span>
          )}
        </div>
      </Panel>
    </div>
  );
}
