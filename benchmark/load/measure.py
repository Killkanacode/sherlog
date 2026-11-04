"""Measure Sherlog under a log flood: its CPU, what it read, and how far behind it is.

  python3 benchmark/load/measure.py --label before --seconds 150 [--token-env SHERLOG_ADMIN_TOKEN]

Samples `docker stats` for Sherlog and Loki every 3 s, and Sherlog's own
counters (GET /status, or /detectors/logs on versions without it). Writes a
JSON summary to benchmark/results/load-<label>.json.
"""

import argparse
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

SHERLOG = "http://localhost:8200"


def get(path: str, token: str | None):
    request = urllib.request.Request(SHERLOG + path, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())
    except Exception:  # noqa: BLE001
        return None


def counters(token: str | None) -> dict:
    status = get("/status", token)
    if status and "logs" in status:
        logs = status["logs"]
        return {"parsed": logs["lines_parsed"], "seen": logs["lines_seen"], "lag_s": logs["lag_s"]}
    old = get("/detectors/logs", None)  # before the change: every line was parsed
    return {"parsed": old["lines_seen"], "seen": old["lines_seen"], "lag_s": None} if old else {}


def cpu(containers: list[str]) -> dict:
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}} {{.MemUsage}}", *containers],
        capture_output=True, text=True,
    ).stdout
    result = {}
    for line in out.splitlines():
        name, perc, mem = line.split(" ", 2)
        result[name] = {"cpu": float(perc.rstrip("%")), "mem": mem.split(" / ")[0]}
    return result


def cpu_seconds(container: str) -> float | None:
    """Exact CPU time the container has used so far (cgroup v2)."""
    out = subprocess.run(
        ["docker", "exec", container, "cat", "/sys/fs/cgroup/cpu.stat"], capture_output=True, text=True
    ).stdout
    for line in out.splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1]) / 1e6
    return None


def env_token(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    env = Path(__file__).resolve().parents[2] / ".env"
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith(name + "="):
            return line.split("=", 1)[1].split("#")[0].strip() or None
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--seconds", type=int, default=150)
    parser.add_argument("--token-env", default="SHERLOG_ADMIN_TOKEN")
    args = parser.parse_args()
    token = env_token(args.token_env)
    containers = ["sherlog-sherlog-api-1", "sherlog-loki-1"]

    first = counters(token)
    cpu_start = {c: cpu_seconds(c) for c in containers}
    samples = []
    started = time.monotonic()
    while time.monotonic() - started < args.seconds:
        samples.append({"t": round(time.monotonic() - started, 1), **counters(token), "stats": cpu(containers)})
        time.sleep(3)
    last = counters(token)
    elapsed = time.monotonic() - started
    cpu_used = {
        c: round(cpu_seconds(c) - cpu_start[c], 1) if cpu_start[c] is not None else None for c in containers
    }

    def avg(container):
        values = [s["stats"].get(container, {}).get("cpu") for s in samples]
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 1) if values else None

    lags = [s.get("lag_s") for s in samples if s.get("lag_s") is not None]
    summary = {
        "label": args.label,
        "seconds": round(elapsed),
        "sherlog_cpu_seconds": cpu_used[containers[0]],
        "sherlog_cores_avg": round(cpu_used[containers[0]] / elapsed, 3) if cpu_used[containers[0]] is not None else None,
        "loki_cpu_seconds": cpu_used[containers[1]],
        "sherlog_cpu_avg_pct": avg(containers[0]),
        "sherlog_cpu_max_pct": max((s["stats"].get(containers[0], {}).get("cpu", 0) for s in samples), default=None),
        "loki_cpu_avg_pct": avg(containers[1]),
        "lines_seen_per_s": round((last.get("seen", 0) - first.get("seen", 0)) / elapsed) if first else None,
        "lines_parsed_per_s": round((last.get("parsed", 0) - first.get("parsed", 0)) / elapsed) if first else None,
        "lag_s_max": max(lags) if lags else None,
        "sherlog_mem_end": samples[-1]["stats"].get(containers[0], {}).get("mem") if samples else None,
    }
    out = Path(__file__).resolve().parents[1] / "results" / f"load-{args.label}.json"
    out.write_text(json.dumps({"summary": summary, "samples": samples}, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
