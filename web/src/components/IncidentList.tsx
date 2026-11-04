import { useState } from "react";
import { API, post } from "../lib/api";
import { ago, between, duration } from "../lib/format";
import { useNow } from "../lib/hooks";
import { categoryOf, statusOf } from "../lib/labels";
import { href } from "../lib/router";
import { useSession } from "../lib/session";
import type { IncidentSummary } from "../lib/types";
import { Badge, Button, cx, Empty } from "./ui";

export function LiveIncidentCard({ incident, onChange }: { incident: IncidentSummary; onChange?: () => void }) {
  const now = useNow();
  const { can } = useSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const status = statusOf(incident.status);
  const pending = incident.pending_action;
  const cause = incident.root_cause && incident.root_cause.confidence > 0 ? incident.root_cause : null;
  const running = incident.investigation?.state === "running";

  const approve = async () => {
    setBusy(true);
    setError(null);
    try {
      await post(`${API}/incidents/${incident.id}/approve`, {});
      onChange?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="border border-line bg-raised">
      <a href={href(`incidents/${incident.id}`)} className="block px-4 pt-3 pb-3 hover:bg-hover">
        <div className="flex items-center gap-2">
          <Badge tone={status.tone} live={incident.status !== "escalated"}>
            {status.label}
          </Badge>
          <span className="font-mono text-xs text-muted">#{incident.id}</span>
          <span className="num ml-auto font-mono text-xs text-ink-2">
            {duration(between(incident.created_at, now))}
          </span>
        </div>
        <div className="mt-2 text-[14px] font-medium text-ink">{incident.title}</div>
        <div className="mt-1 text-[13px] text-muted">
          {cause ? (
            <>
              Root cause <span className="text-ink-2">{cause.service}</span> · {categoryOf(cause.category)} ·{" "}
              {Math.round(cause.confidence * 100)}%
            </>
          ) : running ? (
            <span className="text-info">Investigating…</span>
          ) : incident.root_cause_hint ? (
            <>
              Graph points at <span className="text-ink-2">{incident.root_cause_hint}</span>
            </>
          ) : (
            "Collecting evidence"
          )}
        </div>
        {incident.seen_before && (
          <div className="mt-1 text-xs text-brass">Seen before in #{incident.seen_before.incident_id}</div>
        )}
      </a>
      {pending && (
        <div className="flex items-center gap-2 border-t border-line px-4 py-2.5">
          <div className="min-w-0 flex-1 truncate text-[13px] text-ink-2">
            <span className="text-brass">Proposed:</span> {pending.description}
          </div>
          {pending.state === "awaiting_approval" && can("operator") && (
            <Button variant="go" size="sm" disabled={busy} onClick={approve}>
              Approve
            </Button>
          )}
        </div>
      )}
      {error && <div className="border-t border-line px-4 py-2 text-xs text-bad">{error}</div>}
    </div>
  );
}

export function IncidentRows({ incidents, limit = 8 }: { incidents: IncidentSummary[]; limit?: number }) {
  const now = useNow(15000);
  if (!incidents.length) return <Empty>No incidents yet.</Empty>;
  return (
    <ul>
      {incidents.slice(0, limit).map((i) => {
        const status = statusOf(i.status);
        return (
          <li key={i.id}>
            <a
              href={href(`incidents/${i.id}`)}
              className="grid grid-cols-[44px_1fr_auto] items-center gap-3 border-b border-line px-4 py-2 last:border-0 hover:bg-hover"
            >
              <span className="font-mono text-xs text-muted">#{i.id}</span>
              <span className="min-w-0">
                <span className="block truncate text-[13px] text-ink-2">{i.title}</span>
                <span className="block text-xs text-faint">
                  {ago(i.created_at, now)}
                  {i.resolved_at && ` · lasted ${duration(between(i.created_at, i.resolved_at))}`}
                </span>
              </span>
              <span className={cx("text-xs", status.tone === "ok" ? "text-ok" : "text-warn")}>{status.label}</span>
            </a>
          </li>
        );
      })}
    </ul>
  );
}
