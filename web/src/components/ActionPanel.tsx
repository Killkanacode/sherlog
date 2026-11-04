import { useEffect, useState } from "react";
import { API, get, PLATFORM, post } from "../lib/api";
import { between, duration } from "../lib/format";
import { useNow } from "../lib/hooks";
import { ACTION_TYPES, actionStateOf, DATASTORES, describeProblem, SERVICES } from "../lib/labels";
import { useStream } from "../lib/stream";
import type { Action, IncidentDetail, Problem } from "../lib/types";
import { Badge, Button, cx, Note } from "./ui";

const VERIFY_TIMEOUT_S = 180;
const VERIFY_HEALTHY_S = 30;
const STEPS = ["approved", "executing", "verifying", "recovered"] as const;

export function ActionPanel({ incident, onChange }: { incident: IncidentDetail; onChange: () => void }) {
  const actions = incident.actions;
  const current =
    actions.find((a) => ["awaiting_approval", "suggested", "approved", "executing", "verifying", "reverting"].includes(a.state)) ??
    [...actions].reverse().find((a) => a.executed_at || a.state === "simulated");
  const recommended = incident.diagnosis?.recommended_action;
  const escalated = incident.status === "escalated";

  return (
    <div className="flex flex-col gap-4">
      {current ? (
        <CurrentAction incident={incident} action={current} onChange={onChange} />
      ) : incident.resolved_at ? (
        <div className="text-[13px] text-muted">
          {incident.status === "escalated"
            ? "Escalated to a human; the services recovered without an action from Sherlog."
            : "Closed without an action from Sherlog."}
        </div>
      ) : recommended && ["escalate", "open_fix_pr"].includes(recommended.type) ? (
        <Note tone="warn">
          {recommended.type === "open_fix_pr"
            ? "The investigator recommends a code fix rather than a mitigation; a human takes it from here."
            : "The investigator recommends a human look at this. You can still have Sherlog run an action below; it will be verified and reverted if it doesn't help."}
        </Note>
      ) : (
        !incident.resolved_at && (
          <div className="text-[13px] text-muted">
            {incident.investigation?.state === "running"
              ? "Sherlog will propose an action when the investigation ends."
              : "No action proposed yet."}
          </div>
        )
      )}

      {!incident.resolved_at && !actions.some((a) => ["approved", "executing", "verifying", "reverting"].includes(a.state)) && (
        <ManualAction incident={incident} onChange={onChange} defaultOpen={escalated && !current} />
      )}

      {actions.length > 1 && (
        <div>
          <div className="mb-1.5 text-[11px] tracking-[0.09em] text-muted uppercase">History</div>
          <ul className="border border-line">
            {actions.map((a) => {
              const s = actionStateOf(a.state);
              return (
                <li key={a.id} className="flex items-center gap-3 border-b border-line px-3 py-2 text-[13px] last:border-0">
                  <span className="font-mono text-[11px] text-faint">{a.id}</span>
                  <span className="min-w-0 flex-1 truncate text-ink-2">{a.description}</span>
                  <Badge tone={s.tone}>{s.label}</Badge>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

function CurrentAction({ incident, action, onChange }: { incident: IncidentDetail; action: Action; onChange: () => void }) {
  const now = useNow();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [problems, setProblems] = useState<Problem[] | null>(null);
  const [healthySince, setHealthySince] = useState<number | null>(null);
  const state = actionStateOf(action.state);

  useStream((m) => {
    if (m.type === "verification" && m.incident_id === incident.id && m.action_id === action.id) {
      const found = (m.problems ?? []) as Problem[];
      setProblems(found);
      setHealthySince((prev) => (found.length ? null : (prev ?? Date.now())));
    }
  });
  useEffect(() => {
    setProblems(null);
    setHealthySince(null);
  }, [action.id]);

  const call = async (path: string, body: object) => {
    setBusy(true);
    setError(null);
    try {
      await post(`${API}/incidents/${incident.id}/${path}`, body);
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const pending = action.state === "awaiting_approval" || action.state === "suggested";
  const stepIndex = STEPS.indexOf(action.state as (typeof STEPS)[number]);
  const elapsed = action.executed_at ? between(action.executed_at, now) : 0;

  return (
    <div className={cx("border", pending ? "border-brass" : "border-line-strong")}>
      <div className="flex items-center gap-2 border-b border-line px-4 py-2.5">
        <span className="text-[11px] tracking-[0.09em] text-muted uppercase">
          {action.proposed_by === "sherlog" ? "Recommended mitigation" : `Requested by ${action.proposed_by}`}
        </span>
        <span className="ml-auto">
          <Badge tone={state.tone} live={["executing", "verifying", "reverting"].includes(action.state)}>
            {state.label}
          </Badge>
        </span>
      </div>
      <div className="px-4 py-3">
        <div className="text-[17px] font-semibold text-ink first-letter:uppercase">{action.description}</div>
        <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
          <span>
            Risk <span className={action.risk === "low" ? "text-ok" : "text-warn"}>{action.risk}</span>
          </span>
          {action.approved_by && action.approved_by !== "sherlog" && <span>Approved by {action.approved_by}</span>}
          {action.approved_by === "sherlog" && <span>Run automatically (autonomy L2)</span>}
          {action.undo?.description && <span>Undo: {action.undo.description}</span>}
        </div>

        {pending && (
          <>
            <p className="mt-3 text-[13px] text-ink-2">
              On approval Sherlog runs it through the deploy platform, then watches the incident's services. Healthy for{" "}
              {VERIFY_HEALTHY_S} s means recovered; still broken after {VERIFY_TIMEOUT_S / 60} min and it reverts the
              change and investigates again.
            </p>
            {action.state === "awaiting_approval" ? (
              <div className="mt-3 flex gap-2">
                <Button variant="go" size="lg" disabled={busy} onClick={() => call("approve", { actor: "on-call" })}>
                  Approve and run
                </Button>
                <Button variant="danger" size="lg" disabled={busy} onClick={() => call("reject", { actor: "on-call" })}>
                  Reject
                </Button>
              </div>
            ) : (
              <div className="mt-3 text-[13px] text-muted">Autonomy L0: Sherlog only suggests. Run it yourself below if you agree.</div>
            )}
          </>
        )}

        {!pending && stepIndex >= 0 && (
          <ol className="mt-4 grid grid-cols-4 gap-1">
            {STEPS.map((s, i) => (
              <li key={s}>
                <div className={cx("h-1", i < stepIndex ? "bg-ok" : i === stepIndex ? (s === "recovered" ? "bg-ok" : "bg-info blink") : "bg-line")} />
                <div className={cx("mt-1.5 text-[11px] uppercase tracking-[0.08em]", i <= stepIndex ? "text-ink-2" : "text-faint")}>{s}</div>
              </li>
            ))}
          </ol>
        )}

        {action.state === "verifying" && (
          <div className="mt-4 border border-line bg-bg p-3">
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Verifying recovery</span>
              <span className="num font-mono text-ink-2">
                {duration(elapsed)} / {duration(VERIFY_TIMEOUT_S)}
              </span>
            </div>
            <div className="mt-2 h-1 bg-line">
              <div className="h-1 bg-info" style={{ width: `${Math.min(100, (elapsed / VERIFY_TIMEOUT_S) * 100)}%` }} />
            </div>
            <div className="mt-3 text-[13px]">
              {problems == null ? (
                <span className="text-muted">First check in a few seconds…</span>
              ) : problems.length === 0 ? (
                <span className="text-ok">
                  All checks healthy for {duration(healthySince ? (now - healthySince) / 1000 : 0)} (needs {VERIFY_HEALTHY_S} s)
                </span>
              ) : (
                <ul className="flex flex-col gap-1">
                  {problems.slice(0, 5).map((p, i) => (
                    <li key={i} className="flex gap-2 text-ink-2">
                      <span className="mt-[7px] size-1.5 shrink-0 bg-bad" />
                      {describeProblem(p)}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}

        {action.state === "recovered" && action.verification && (
          <Note tone="ok">
            Back to baseline {action.verification.seconds} s after the action, held for {VERIFY_HEALTHY_S} s across{" "}
            {action.verification.checks} checks.
          </Note>
        )}
        {["reverted", "not_recovered"].includes(action.state) && (
          <Note tone="warn">
            Did not restore service
            {action.verification?.remaining?.length ? ` (${action.verification.remaining.map(describeProblem).slice(0, 2).join("; ")})` : ""}.
            {action.state === "reverted" ? " Sherlog reverted it." : ""}
          </Note>
        )}
        {action.state === "simulated" && <Note tone="info">Dry run: nothing was changed.</Note>}
        {action.error && <div className="mt-3 text-xs text-bad">{action.error}</div>}
        {error && <div className="mt-3 text-xs text-bad">{error}</div>}
      </div>
    </div>
  );
}

function ManualAction({ incident, onChange, defaultOpen }: { incident: IncidentDetail; onChange: () => void; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const suggestion = incident.diagnosis?.root_cause?.service ?? incident.root_cause_hint ?? incident.services[0];
  const [type, setType] = useState("rollback");
  const [service, setService] = useState(SERVICES.includes(suggestion ?? "") ? suggestion! : "orders");
  const [replicas, setReplicas] = useState(2);
  const [flag, setFlag] = useState("");
  const [flags, setFlags] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const spec = ACTION_TYPES.find((a) => a.type === type)!;
  const targets = type === "restart" ? [...SERVICES, ...DATASTORES] : SERVICES;

  useEffect(() => {
    if (type !== "flag_off" || flags.length) return;
    get<Record<string, unknown> | { name: string }[]>(`${PLATFORM}/flags`)
      .then((data) => {
        const names = Array.isArray(data) ? data.map((f) => f.name) : Object.keys(data);
        setFlags(names);
        if (names.length && !flag) setFlag(names[0]);
      })
      .catch(() => setFlags([]));
  }, [type, flags.length, flag]);

  useEffect(() => setOpen(defaultOpen), [defaultOpen]);

  const run = async () => {
    const params: Record<string, unknown> = {};
    if (spec.needs.includes("service")) params.service = service;
    if (spec.needs.includes("replicas")) params.replicas = replicas;
    if (spec.needs.includes("flag")) params.flag = flag;
    setBusy(true);
    setError(null);
    try {
      await post(`${API}/incidents/${incident.id}/actions`, { type, params, actor: "on-call" });
      onChange();
      setOpen(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!open)
    return (
      <button onClick={() => setOpen(true)} className="self-start text-[13px] text-muted hover:text-ink">
        + Run a different action
      </button>
    );

  const field = "h-8 border border-line-strong bg-bg px-2 text-[13px] text-ink outline-none focus:border-brass";
  return (
    <div className="border border-line">
      <div className="border-b border-line px-4 py-2.5 text-[11px] tracking-[0.09em] text-muted uppercase">Run an action</div>
      <div className="flex flex-col gap-3 px-4 py-3">
        <div className="grid grid-cols-5 gap-1">
          {ACTION_TYPES.map((a) => (
            <button
              key={a.type}
              onClick={() => setType(a.type)}
              className={cx(
                "h-8 border text-xs",
                type === a.type ? "border-brass bg-brass/10 text-ink" : "border-line text-muted hover:border-line-strong hover:text-ink",
              )}
            >
              {a.label}
            </button>
          ))}
        </div>
        <div className="text-xs text-muted">{spec.hint}</div>
        <div className="flex flex-wrap items-center gap-2">
          {spec.needs.includes("service") && (
            <select value={service} onChange={(e) => setService(e.target.value)} className={field}>
              {targets.map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          )}
          {spec.needs.includes("replicas") && (
            <select value={replicas} onChange={(e) => setReplicas(Number(e.target.value))} className={field}>
              {[1, 2, 3, 4].map((n) => (
                <option key={n} value={n}>
                  {n} replica{n > 1 ? "s" : ""}
                </option>
              ))}
            </select>
          )}
          {spec.needs.includes("flag") &&
            (flags.length ? (
              <select value={flag} onChange={(e) => setFlag(e.target.value)} className={field}>
                {flags.map((f) => (
                  <option key={f}>{f}</option>
                ))}
              </select>
            ) : (
              <input value={flag} onChange={(e) => setFlag(e.target.value)} placeholder="flag name" className={field} />
            ))}
          <Button variant="primary" disabled={busy || (spec.needs.includes("flag") && !flag)} onClick={run}>
            Run with verification
          </Button>
          <Button variant="ghost" onClick={() => setOpen(false)}>
            Cancel
          </Button>
        </div>
        {error && <div className="text-xs text-bad">{error}</div>}
      </div>
    </div>
  );
}
