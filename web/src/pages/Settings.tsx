import { useState } from "react";
import { Button, cx, Dot, Empty, Panel } from "../components/ui";
import { API, del, get, post, put } from "../lib/api";
import { ago } from "../lib/format";
import { useNow, usePoll } from "../lib/hooks";
import { useSession, type Role } from "../lib/session";
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
  const health = usePoll(() => get<Health>(`${API}/status`), 0);
  const { can } = useSession();
  const isAdmin = can("admin");
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
        <p className="mt-1 text-[13px] text-muted">
          How far Sherlog may go on its own. Changes apply immediately and survive restarts
          {isAdmin ? "." : "; only an admin can change them."}
        </p>
      </div>
      {error && <div className="border border-bad/40 bg-bad/10 px-4 py-2 text-[13px] text-bad">{error}</div>}

      <Panel title="Autonomy">
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
          {LEVELS.map((level) => {
            const active = p?.autonomy === level.id;
            return (
              <button
                key={level.id}
                disabled={saving || !p || !isAdmin}
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
              disabled={saving || !p || !isAdmin}
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
      {isAdmin && <AccessPanel />}
      {isAdmin && <AuditPanel />}
    </div>
  );
}

type Key = { name: string; role: Role; source: string; created_at: number | null };

function AccessPanel() {
  const keys = usePoll(() => get<Key[]>(`${API}/auth/keys`), 0);
  const { me } = useSession();
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("operator");
  const [created, setCreated] = useState<{ name: string; token: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const create = async () => {
    setError(null);
    try {
      const result = await post<{ name: string; token: string }>(`${API}/auth/keys`, { name: name.trim(), role });
      setCreated(result);
      setName("");
      keys.refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const revoke = async (keyName: string) => {
    setError(null);
    try {
      await del(`${API}/auth/keys/${encodeURIComponent(keyName)}`);
      keys.refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const field = "h-8 border border-line-strong bg-bg px-2 text-[13px] text-ink outline-none focus:border-brass";
  return (
    <Panel title="API keys" aside="viewer reads · operator acts · admin configures" bodyClass="">
      <table className="w-full text-[13px]">
        <tbody>
          {(keys.data ?? []).map((k) => (
            <tr key={k.name} className="border-b border-line">
              <td className="px-4 py-2 text-ink">{k.name}</td>
              <td className="py-2 font-mono text-xs text-muted uppercase">{k.role}</td>
              <td className="py-2 text-xs text-faint">{k.source === "api" ? "created here" : "from the environment"}</td>
              <td className="px-4 py-2 text-right">
                {k.source === "api" && k.name !== me?.name && (
                  <Button size="sm" variant="danger" onClick={() => revoke(k.name)}>
                    Revoke
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="flex flex-wrap items-center gap-2 px-4 py-3">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="name, e.g. ana" className={field} />
        <select value={role} onChange={(e) => setRole(e.target.value as Role)} className={field}>
          <option value="viewer">viewer</option>
          <option value="operator">operator</option>
          <option value="admin">admin</option>
        </select>
        <Button variant="primary" disabled={!name.trim()} onClick={create}>
          Create key
        </Button>
        {error && <span className="text-xs text-bad">{error}</span>}
      </div>
      {created && (
        <div className="border-t border-line px-4 py-3 text-[13px]">
          <div className="text-ink-2">
            Key for <span className="text-ink">{created.name}</span>. Copy it now: it is not shown again.
          </div>
          <div className="mt-2 flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate border border-line bg-bg px-2 py-1.5 font-mono text-xs text-brass">
              {created.token}
            </code>
            <Button size="sm" onClick={() => navigator.clipboard.writeText(created.token)}>
              Copy
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setCreated(null)}>
              Done
            </Button>
          </div>
        </div>
      )}
    </Panel>
  );
}

type AuditRow = { id: number; ts: string; actor: string | null; role: string | null; method: string; path: string; status: number; ip: string | null };

function AuditPanel() {
  const now = useNow(15000);
  const rows = usePoll(() => get<AuditRow[]>(`${API}/audit?limit=50`), 15000);
  return (
    <Panel title="Audit log" aside="every write and every refused request" bodyClass="">
      {!rows.data?.length ? (
        <Empty>Nothing recorded yet.</Empty>
      ) : (
        <div className="max-h-80 overflow-y-auto">
          <table className="w-full text-[13px]">
            <tbody>
              {rows.data.map((r) => (
                <tr key={r.id} className="border-b border-line last:border-0">
                  <td className="w-28 px-4 py-1.5 text-xs whitespace-nowrap text-faint">{ago(r.ts, now)}</td>
                  <td className="py-1.5 text-ink-2">{r.actor ?? <span className="text-faint">anonymous</span>}</td>
                  <td className="py-1.5 font-mono text-xs text-muted">{r.method}</td>
                  <td className="py-1.5 font-mono text-xs break-all text-ink-2">{r.path}</td>
                  <td className={cx("px-4 py-1.5 text-right font-mono text-xs", r.status >= 400 ? "text-bad" : "text-ok")}>{r.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
