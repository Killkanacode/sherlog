import { useState, type FormEvent } from "react";
import { Mark } from "../components/Logo";
import { Button } from "../components/ui";
import { API, post } from "../lib/api";
import type { Me } from "../lib/session";

export function Login({ onSignedIn }: { onSignedIn: (me: Me) => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await post<Me>(`${API}/auth/login`, { token }));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center px-5">
      <form onSubmit={submit} className="w-full border border-line bg-panel sm:w-[420px]">
        <div className="border-b border-line px-6 py-5">
          <div className="flex items-center gap-2.5">
            <Mark size={26} />
            <span className="text-[20px] font-semibold tracking-tight text-ink">Sherlog</span>
          </div>
          <div className="mt-1 text-[13px] text-muted italic">Elementary, my dear SRE.</div>
        </div>
        <div className="flex flex-col gap-3 px-6 py-5">
          <label className="text-[11px] tracking-[0.09em] text-muted uppercase" htmlFor="token">
            API key
          </label>
          <input
            id="token"
            type="password"
            autoComplete="current-password"
            autoFocus
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="slg_…"
            className="h-10 border border-line-strong bg-bg px-3 font-mono text-[13px] text-ink outline-none focus:border-brass"
          />
          {error && <div className="text-[13px] text-bad">{error}</div>}
          <Button type="submit" variant="primary" size="lg" disabled={busy || !token.trim()}>
            {busy ? "Signing in…" : "Sign in"}
          </Button>
          <p className="text-xs leading-relaxed text-muted">
            On the first start Sherlog writes an admin key to its log once:{" "}
            <span className="font-mono text-ink-2">docker compose logs sherlog-api | grep "admin key"</span>. Or set{" "}
            <span className="font-mono text-ink-2">SHERLOG_ADMIN_TOKEN</span> in <span className="font-mono">.env</span>.
          </p>
        </div>
      </form>
    </div>
  );
}
