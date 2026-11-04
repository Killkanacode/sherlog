// Every backend is reached through the same origin: Vite's proxy in
// development, nginx in the container.
export const API = "/api";
export const CHAOS = "/chaos";
export const PROM = "/prom";
export const PLATFORM = "/platform";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  // The session cookie rides along (same origin); X-Requested-With is what
  // Sherlog requires on cookie-authenticated writes, against CSRF.
  const headers: Record<string, string> = { "x-requested-with": "sherlog" };
  if (init?.body) headers["content-type"] = "application/json";
  const response = await fetch(url, { ...init, headers });
  const text = await response.text();
  if (response.status === 401 && !url.endsWith("/auth/login")) {
    window.dispatchEvent(new Event("sherlog:unauthorized"));
  }
  if (!response.ok) {
    let detail: unknown = text;
    try {
      detail = JSON.parse(text).detail ?? text;
    } catch {
      // plain-text error
    }
    throw new ApiError(response.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  const type = response.headers.get("content-type") ?? "";
  return (type.includes("json") ? JSON.parse(text) : text) as T;
}

export const get = <T>(url: string) => request<T>(url);

export const post = <T>(url: string, body?: unknown) =>
  request<T>(url, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const put = <T>(url: string, body: unknown) => request<T>(url, { method: "PUT", body: JSON.stringify(body) });

export const del = <T>(url: string) => request<T>(url, { method: "DELETE" });

export type Point = { t: number; v: number | null };
export type Series = { labels: Record<string, string>; points: Point[] };

/** A Prometheus range query; t is in milliseconds. */
export async function promRange(query: string, start: number, end: number, step: number): Promise<Series[]> {
  const params = new URLSearchParams({
    query,
    start: String(Math.floor(start / 1000)),
    end: String(Math.floor(end / 1000)),
    step: String(step),
  });
  const body = await get<{ data: { result: { metric: Record<string, string>; values: [number, string][] }[] } }>(
    `${PROM}/api/v1/query_range?${params}`,
  );
  return body.data.result.map((r) => ({
    labels: r.metric,
    points: r.values.map(([t, v]) => {
      const n = Number(v);
      return { t: t * 1000, v: Number.isFinite(n) ? n : null };
    }),
  }));
}

/** RED queries per service (job), the same ones Sherlog's detectors watch. */
export const RED = {
  rate: "sum by (job) (rate(http_server_duration_seconds_count[1m]))",
  errors:
    '(sum by (job) (rate(http_server_duration_seconds_count{status_code=~"5.."}[1m])) or sum by (job) (rate(http_server_duration_seconds_count[1m])) * 0) / sum by (job) (rate(http_server_duration_seconds_count[1m]))',
  p95: "histogram_quantile(0.95, sum by (job, le) (rate(http_server_duration_seconds_bucket[1m])))",
};
