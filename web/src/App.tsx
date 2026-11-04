import { Mark } from "./components/Logo";
import { Toasts } from "./components/Toasts";
import { cx, Dot } from "./components/ui";
import { API, get } from "./lib/api";
import { usePoll } from "./lib/hooks";
import { href, useRoute } from "./lib/router";
import { useStream, useStreamStatus } from "./lib/stream";
import type { Health } from "./lib/types";
import { ChaosLab } from "./pages/ChaosLab";
import { HowItWorks } from "./pages/HowItWorks";
import { IncidentPage } from "./pages/Incident";
import { Live } from "./pages/Live";
import { Postmortems } from "./pages/Postmortems";
import { Settings } from "./pages/Settings";

const NAV = [
  { path: "live", label: "Live" },
  { path: "chaos", label: "Chaos Lab" },
  { path: "postmortems", label: "Postmortems" },
  { path: "how", label: "How it works" },
  { path: "settings", label: "Settings" },
];

export function App() {
  const route = useRoute();
  const section = route[0] ?? "live";
  const health = usePoll(() => get<Health>(`${API}/health`), 10000);
  const connected = useStreamStatus();
  useStream((m) => m.type === "settings" && health.refresh());

  let page;
  if (section === "incidents" && route[1]) page = <IncidentPage id={Number(route[1])} />;
  else if (section === "chaos") page = <ChaosLab />;
  else if (section === "postmortems") page = <Postmortems id={route[1] ? Number(route[1]) : null} />;
  else if (section === "how") page = <HowItWorks />;
  else if (section === "settings") page = <Settings />;
  else page = <Live />;

  const h = health.data;
  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-30 border-b border-line bg-bg">
        <div className="flex h-13 w-full items-center gap-6 px-5">
          <a href={href("live")} className="flex items-center gap-2.5">
            <Mark />
            <span className="text-[17px] font-semibold tracking-tight text-ink">Sherlog</span>
            <span className="hidden text-[13px] text-muted italic xl:inline">Elementary, my dear SRE.</span>
          </a>
          <nav className="flex h-full items-stretch">
            {NAV.map((n) => {
              const active = section === n.path || (n.path === "live" && section === "incidents");
              return (
                <a
                  key={n.path}
                  href={href(n.path)}
                  className={cx(
                    "-mb-px flex items-center border-b-2 px-3 text-[13px]",
                    active ? "border-brass text-ink" : "border-transparent text-muted hover:text-ink",
                  )}
                >
                  {n.label}
                </a>
              );
            })}
          </nav>
          <div className="ml-auto flex items-center gap-4 text-xs text-muted">
            {h?.learning && (
              <span className="flex items-center gap-1.5 text-warn">
                <Dot tone="warn" live /> Learning baselines
              </span>
            )}
            <span className="flex items-center gap-1.5" title="Live event stream from Sherlog">
              <Dot tone={connected ? "ok" : "bad"} live={!connected} />
              {connected ? "Streaming" : "Reconnecting"}
            </span>
            {h && (
              <a
                href={href("settings")}
                className="flex items-center gap-3 border border-line px-2 py-1 font-mono text-[11px] hover:border-line-strong"
              >
                <span className={h.llm === "fake" ? "text-warn" : "text-ink-2"}>
                  {h.llm === "fake" ? "STAND-IN MODELS" : "MUSE SPARK"}
                </span>
                <span className="text-faint">|</span>
                <span className="text-ink-2">AUTONOMY {h.autonomy}</span>
                {h.dry_run && <span className="text-info">DRY RUN</span>}
              </a>
            )}
          </div>
        </div>
      </header>
      <main className="w-full flex-1 px-5 py-5">{page}</main>
      <Toasts />
    </div>
  );
}
