// Shapes returned by the Sherlog API, the chaos controller and platform-api.

export type Autonomy = "L0" | "L1" | "L2";

export type Health = {
  status: string;
  learning: boolean;
  llm: "fake" | "live";
  investigate: boolean;
  autonomy: Autonomy;
  dry_run: boolean;
  auto_min_confidence: number;
};

export type Policy = { autonomy: Autonomy; dry_run: boolean; auto_min_confidence: number };

export type Signal = {
  id: string;
  key: string;
  type: string;
  service: string;
  severity: string;
  started_at: string;
  ended_at?: string | null;
  active?: boolean;
  details: Record<string, any>;
};

export type RootCause = { service: string; category: string; summary: string; confidence: number };

export type Problem = {
  service: string;
  metric?: string;
  peer?: string | null;
  value?: number;
  baseline?: number;
  signal?: string;
  exception?: string;
  fingerprint?: string;
  per_minute?: number;
};

export type Verification = {
  outcome: "recovered" | "not_recovered";
  started_at: string;
  recovered_at?: string | null;
  finished_at: string;
  seconds: number;
  checks: number;
  remaining?: Problem[];
  before?: Problem[];
};

export type Action = {
  id: number;
  type: string;
  params: Record<string, any>;
  risk: string;
  description: string;
  state: string;
  proposed_by: string;
  proposed_at: string;
  approved_by: string | null;
  approved_at: string | null;
  executed_at: string | null;
  dry_run: boolean;
  result: unknown;
  undo: { description?: string } | null;
  verification: Verification | null;
  error: string | null;
  reason?: string;
};

export type MemoryMatch = {
  incident_id: number;
  closed_at: string;
  title: string;
  service: string | null;
  category: string | null;
  summary: string | null;
  outcome: string;
  action: { type: string; params: Record<string, any>; description?: string } | null;
  resolve_s: number | null;
  shared_fingerprints?: number;
  shared_templates?: number;
};

export type Investigation = {
  state: "running" | "done" | "failed";
  mode: "fake" | "live";
  started_at: string;
  finished_at?: string;
  steps?: number;
  error?: string;
};

export type Usage = {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
  latency_ms?: number;
};

export type IncidentSummary = {
  id: number;
  status: string;
  title: string;
  services: string[];
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  active_signals?: number;
  signal_count?: number;
  root_cause_hint?: string | null;
  root_cause?: RootCause | null;
  investigation?: Investigation | null;
  pending_action?: Action | null;
  seen_before?: MemoryMatch | null;
  has_postmortem?: boolean;
  llm_usage?: Usage | null;
};

export type TimelineEntry = { ts: string; kind: string; text: string; data: Record<string, any> };

export type ChangeEvent = {
  id: string;
  type: string;
  actor: string;
  service?: string;
  timestamp: string;
  commit_sha?: string;
  to_version?: string;
  from_version?: string;
  changed_files?: string[];
  commit_message?: string;
  details?: Record<string, any>;
};

export type LogTemplate = {
  id: string;
  service: string;
  template: string;
  level: string;
  new: boolean;
  first_seen: string;
  per_minute: number;
  baseline_per_minute: number;
  count_10m?: number;
  examples?: { ts: string; line: string; trace_id?: string }[];
};

export type ExceptionInfo = {
  fingerprint: string;
  service: string;
  type: string;
  message: string;
  new: boolean;
  first_seen: string;
  per_minute: number;
  total: number;
  version: string;
  frames: string[];
  stack?: string;
};

export type Span = {
  service: string;
  name: string;
  kind: string;
  duration_ms: number;
  error: boolean;
  offset_ms: number;
};

export type TraceInfo = {
  trace_id: string;
  root: string;
  duration_ms: number;
  span_count: number;
  error: boolean;
  critical_spans: Span[];
};

export type EvidenceStats = {
  log_lines_in_window: number;
  templates_known: number;
  templates_selected: number;
  exceptions_selected: number;
  traces_selected: number;
  evidence_chars: number;
  estimated_tokens: number;
};

export type Evidence = {
  window: { start: string; end: string };
  suspect_services: string[];
  root_cause_hint: { service: string; reason: string } | null;
  signals: Signal[];
  changes: ChangeEvent[];
  log_templates: LogTemplate[];
  exceptions: ExceptionInfo[];
  traces: TraceInfo[];
  stats: EvidenceStats;
  topology?: TopologyEdge[];
  past_incidents: MemoryMatch[];
};

export type Diagnosis = {
  root_cause: RootCause;
  evidence: { type: string; ref: string; detail: string }[];
  ruled_out: { hypothesis: string; reason: string }[];
  recommended_action: { type: string; params: Record<string, any>; risk: string } | null;
};

export type LlmCall = {
  ts: string;
  role: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  cost_usd: number | null;
};

export type IncidentDetail = IncidentSummary & {
  signals: Signal[];
  timeline: TimelineEntry[];
  evidence: Evidence | null;
  triage: { overview: string; services: { service: string; assessment: string; what_changed: string[]; new_errors: string[]; likely_noise: string[] }[] } | null;
  diagnosis: Diagnosis | null;
  actions: Action[];
  past_incidents: MemoryMatch[];
  postmortem: string | null;
  llm_calls: LlmCall[];
};

export type TopologyEdge = { source: string; target: string; rps: number; error_rate: number };
export type Topology = {
  nodes: { id: string; kind: "service" | "dependency"; status: "ok" | "anomalous" }[];
  edges: TopologyEdge[];
};

export type Stats = {
  incidents_total: number;
  incidents_open: number;
  log_lines_seen: number;
  log_lines_parsed?: number;
  log_templates: number;
  exception_fingerprints: number;
  funnel: { log_lines_in_windows: number; estimated_tokens: number };
  llm: { mode: string; by_model: { model: string; calls: number; input_tokens: number; output_tokens: number; cost_usd: number | null }[] };
};

export type GroundTruth = {
  service: string;
  category: string;
  acceptable_actions: string[];
  services_accepted?: string[];
  categories_accepted?: string[];
};

export type Scenario = {
  id: string;
  title: string;
  description: string;
  inject: { type: string; service?: string; [key: string]: unknown }[];
  ground_truth: GroundTruth;
};

export type ChaosRun = {
  id: string;
  scenario_id: string;
  started_at: string;
  ended_at: string | null;
  ground_truth: GroundTruth;
  status: string;
};

export type PlatformService = {
  service: string;
  version: string;
  commit_sha: string;
  deployed_at: string;
  replicas?: number;
  containers: { name: string; status: string; health?: string; restart_count: number }[];
};

export type PostmortemRow = {
  id: number;
  status: string;
  title: string;
  services: string[];
  created_at: string;
  resolved_at: string | null;
};
