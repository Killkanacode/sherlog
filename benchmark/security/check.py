"""Access-control checks against the running stack, through the console's
nginx (as a browser would) and against Sherlog's own port.

  python3 benchmark/security/check.py

Uses SHERLOG_ADMIN_TOKEN from .env to create a viewer and an operator key,
runs the checks, revokes both keys and writes benchmark/results/security.json.
Exits non-zero if any check fails.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB, API = "http://localhost:8300", "http://localhost:8200"


def token_from_env(name: str) -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"{name} is not set in .env")


def call(method, url, token=None, body=None, headers=None, cookie=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    if cookie:
        h["Cookie"] = cookie
    data = json.dumps(body).encode() if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


results = []


def expect(name, got, want):
    ok = got in want if isinstance(want, (list, tuple, set)) else got == want
    results.append({"check": name, "got": got, "expected": want, "ok": ok})
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got}")


admin = token_from_env("SHERLOG_ADMIN_TOKEN")
suffix = str(int(time.time()))
viewer = json.loads(call("POST", f"{API}/auth/keys", admin, {"name": f"check-viewer-{suffix}", "role": "viewer"})[2])["token"]
operator = json.loads(call("POST", f"{API}/auth/keys", admin, {"name": f"check-operator-{suffix}", "role": "operator"})[2])["token"]

try:
    # No credentials: everything but liveness and the page itself is closed.
    expect("console page loads", call("GET", f"{WEB}/")[0], 200)
    expect("liveness is public", call("GET", f"{WEB}/api/health")[0], 200)
    for path in ("/api/incidents", "/api/status", "/api/stream", "/api/postmortems", "/chaos/scenarios",
                 "/prom/api/v1/query_range?query=up&start=0&end=1&step=1", "/platform/services"):
        expect(f"anonymous GET {path} refused", call("GET", WEB + path)[0], 401)
    expect("anonymous chaos reset refused", call("POST", f"{WEB}/chaos/reset")[0], 401)
    expect("anonymous approve on Sherlog's own port refused", call("POST", f"{API}/incidents/1/approve")[0], 401)
    expect("anonymous event push refused", call("POST", f"{API}/events", body={"id": "x", "type": "deploy", "timestamp": "t"})[0], 401)
    expect("a wrong key is refused", call("GET", f"{API}/incidents", "slg_not-a-real-key")[0], 401)

    # Roles.
    expect("viewer reads incidents", call("GET", f"{WEB}/api/incidents", viewer)[0], 200)
    expect("viewer reads chaos scenarios", call("GET", f"{WEB}/chaos/scenarios", viewer)[0], 200)
    expect("viewer can't reset chaos", call("POST", f"{WEB}/chaos/reset", viewer)[0], 403)
    expect("viewer can't approve", call("POST", f"{WEB}/api/incidents/1/approve", viewer)[0], 403)
    expect("viewer can't change settings", call("PUT", f"{WEB}/api/settings", viewer, {"dry_run": False})[0], 403)
    expect("operator can't change settings", call("PUT", f"{WEB}/api/settings", operator, {"dry_run": False})[0], 403)
    expect("operator can't read the audit log", call("GET", f"{WEB}/api/audit", operator)[0], 403)
    expect("operator may approve (404: no such incident)", call("POST", f"{WEB}/api/incidents/999999/approve", operator)[0], 404)
    expect("admin reads the audit log", call("GET", f"{WEB}/api/audit", admin)[0], 200)

    # Browser session: HttpOnly + SameSite=Strict cookie, CSRF header on writes.
    status, headers, _ = call("POST", f"{WEB}/api/auth/login", body={"token": operator})
    cookie_header = headers.get("set-cookie") or headers.get("Set-Cookie") or ""
    expect("login sets a session", status, 200)
    expect("cookie is HttpOnly and SameSite=Strict", "HttpOnly" in cookie_header and "SameSite=strict" in cookie_header, True)
    cookie = cookie_header.split(";", 1)[0]
    expect("session reads", call("GET", f"{WEB}/api/auth/me", cookie=cookie)[0], 200)
    expect("session write without CSRF header refused", call("POST", f"{WEB}/chaos/reset", cookie=cookie)[0], 403)
    expect("session API write without CSRF header refused", call("POST", f"{WEB}/api/incidents/999999/approve", cookie=cookie)[0], 403)
    expect("session write with CSRF header allowed", call("POST", f"{WEB}/api/incidents/999999/approve", cookie=cookie,
                                                          headers={"X-Requested-With": "sherlog"})[0], 404)
    expect("forged cookie refused", call("GET", f"{WEB}/api/auth/me", cookie=cookie[:-4] + "AAAA")[0], 401)

    # Headers and brute force.
    _, headers, _ = call("GET", f"{WEB}/")
    lower = {k.lower(): v for k, v in headers.items()}
    for header in ("content-security-policy", "x-frame-options", "x-content-type-options", "referrer-policy"):
        expect(f"{header} set", header in lower, True)
    expect("server version hidden", "nginx/" in lower.get("server", ""), False)
    codes = [call("POST", f"{WEB}/api/auth/login", body={"token": "slg_guess"})[0] for _ in range(20)]
    expect("login brute force is rate limited", 429 in codes, True)
finally:
    for name in (f"check-viewer-{suffix}", f"check-operator-{suffix}"):
        call("DELETE", f"{API}/auth/keys/{name}", admin)
    expect("revoked key no longer works", call("GET", f"{API}/incidents", viewer)[0], 401)

failed = [r for r in results if not r["ok"]]
(ROOT / "benchmark" / "results" / "security.json").write_text(json.dumps(results, indent=1))
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
