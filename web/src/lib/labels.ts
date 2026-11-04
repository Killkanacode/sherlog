import type { Problem, Signal } from "./types";

export type Tone = "ok" | "warn" | "bad" | "info" | "muted" | "brass";

export const STATUS: Record<string, { label: string; tone: Tone }> = {
  detected: { label: "Detected", tone: "bad" },
  investigating: { label: "Investigating", tone: "info" },
  mitigating: { label: "Mitigating", tone: "warn" },
  verifying: { label: "Verifying", tone: "info" },
  resolved: { label: "Resolved", tone: "ok" },
  escalated: { label: "Escalated", tone: "warn" },
};

export const statusOf = (status: string) => STATUS[status] ?? { label: status, tone: "muted" as Tone };

export const ACTION_STATE: Record<string, { label: string; tone: Tone }> = {
  proposed: { label: "Proposed", tone: "muted" },
  suggested: { label: "Suggested", tone: "brass" },
  awaiting_approval: { label: "Awaiting approval", tone: "brass" },
  approved: { label: "Approved", tone: "info" },
  executing: { label: "Executing", tone: "info" },
  verifying: { label: "Verifying", tone: "info" },
  recovered: { label: "Recovered", tone: "ok" },
  reverting: { label: "Reverting", tone: "warn" },
  reverted: { label: "Reverted", tone: "warn" },
  not_recovered: { label: "Did not recover", tone: "bad" },
  rejected: { label: "Rejected", tone: "muted" },
  superseded: { label: "Superseded", tone: "muted" },
  expired: { label: "Expired", tone: "muted" },
  simulated: { label: "Dry run", tone: "info" },
  error: { label: "Failed", tone: "bad" },
};

export const actionStateOf = (state: string) => ACTION_STATE[state] ?? { label: state, tone: "muted" as Tone };

export const CATEGORY: Record<string, string> = {
  bad_deploy_code_bug: "Bad deploy (code bug)",
  config_change: "Config change",
  memory_exhaustion: "Memory exhaustion",
  cpu_exhaustion: "CPU exhaustion",
  db_slow_query: "Slow database query",
  db_pool_exhaustion: "DB pool exhaustion",
  network_latency: "Network latency",
  network_partition: "Network partition",
  dependency_down: "Dependency down",
  process_crash: "Process crash",
  process_hang: "Process hang",
  retry_storm: "Retry storm",
  unknown: "Unknown",
};

export const categoryOf = (c: string | null | undefined) => (c ? (CATEGORY[c] ?? c.replace(/_/g, " ")) : "–");

export const ACTION_TYPES: { type: string; label: string; needs: ("service" | "replicas" | "flag")[]; hint: string }[] = [
  { type: "rollback", label: "Roll back", needs: ["service"], hint: "Redeploy the service's previous version" },
  { type: "restart", label: "Restart", needs: ["service"], hint: "Restart a service, Redis or Postgres" },
  { type: "config_revert", label: "Revert config", needs: ["service"], hint: "Undo the service's last config change" },
  { type: "flag_off", label: "Turn flag off", needs: ["flag"], hint: "Switch a feature flag off" },
  { type: "scale", label: "Scale", needs: ["service", "replicas"], hint: "Change the replica count (1–4)" },
];

export const SERVICES = ["gateway", "orders", "payments", "inventory"];
export const DATASTORES = ["redis", "postgres"];

export const KIND_OF_SIGNAL: Record<string, string> = {
  metric_anomaly: "metric",
  new_log_template: "log",
  log_spike: "log",
  log_silence: "log",
  new_exception: "exception",
  exception_spike: "exception",
  container_down: "container",
  container_restart: "container",
  not_ready: "container",
  oom_killed: "container",
};

const METRIC: Record<string, string> = {
  latency_p95: "p95 latency",
  error_rate: "error rate",
  traffic: "traffic",
  client_latency_p95: "client p95",
  client_failure_rate: "client failures",
  cpu_cores: "CPU",
  memory_growth: "memory growth",
  memory_limit: "memory vs limit",
  db_pool_saturation: "DB pool",
  datastore_failures: "datastore failures",
};

export const metricName = (m: string) => METRIC[m] ?? m.replace(/_/g, " ");

export function metricValue(metric: string, v: number | undefined): string {
  if (v == null) return "–";
  if (metric.includes("latency")) return v < 1 ? `${Math.round(v * 1000)} ms` : `${v.toFixed(2)} s`;
  if (metric.includes("rate") || metric.includes("saturation") || metric === "memory_limit")
    return `${(v * 100).toFixed(1)}%`;
  if (metric === "traffic") return `${v.toFixed(1)}/s`;
  return v.toFixed(2);
}

export function describeSignal(s: Signal): string {
  const d = s.details ?? {};
  if (s.type === "metric_anomaly") {
    const peer = d.peer ? ` → ${d.peer}` : "";
    return `${metricName(d.metric)}${peer} ${d.direction ?? ""} · ${metricValue(d.metric, d.value)} vs ${metricValue(d.metric, d.baseline)}`;
  }
  if (s.type === "new_log_template" || s.type === "log_spike")
    return `${s.type === "log_spike" ? "Log spike" : "New log line"}: ${d.template ?? ""}`;
  if (s.type === "new_exception" || s.type === "exception_spike")
    return `${s.type === "new_exception" ? "New exception" : "Exception spike"}: ${d.exception}: ${d.message ?? ""}`;
  return s.type.replace(/_/g, " ");
}

export function describeProblem(p: Problem): string {
  if (p.metric) {
    const peer = p.peer ? ` → ${p.peer}` : "";
    return `${p.service}: ${metricName(p.metric)}${peer} ${metricValue(p.metric, p.value)} (baseline ${metricValue(p.metric, p.baseline)})`;
  }
  if (p.signal) return `${p.service}: ${p.signal.replace(/_/g, " ")}`;
  return `${p.service}: ${p.exception} at ${p.per_minute}/min`;
}

export const TONE_TEXT: Record<Tone, string> = {
  ok: "text-ok",
  warn: "text-warn",
  bad: "text-bad",
  info: "text-info",
  muted: "text-muted",
  brass: "text-brass",
};

export const TONE_BG: Record<Tone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  bad: "bg-bad",
  info: "bg-info",
  muted: "bg-faint",
  brass: "bg-brass",
};

export const TONE_HEX: Record<Tone, string> = {
  ok: "#4fb87e",
  warn: "#e0a43a",
  bad: "#ee5b4d",
  info: "#74a7f2",
  muted: "#5c5f68",
  brass: "#d4a84b",
};
