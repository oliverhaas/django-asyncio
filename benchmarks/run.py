#!/usr/bin/env python
"""Benchmark runner for django-asyncio.

Launches the benchmark app under Granian in a given configuration, drives
it with a concurrent HTTP load, samples CPU and memory, and writes a CSV
plus a markdown summary per scenario.

Configurations (see PLAN.md > Benchmarks):
  sync1 / sync10 / sync100  WSGI, 1 worker, N blocking threads
  async                     ASGI, 1 worker, 1 runtime thread

Scenarios:
  io   I/O-bound  (sleep; async is expected to win under concurrency)
  cpu  CPU-bound  (sha256 work; async is expected to roughly match sync)

The async build runs against whatever Django the *server* interpreter
imports. To compare the fork against upstream, point --server-python at a
venv that has stock Django installed (plus granian); the default uses the
same interpreter running this script (the fork).

Example:
  python benchmarks/run.py --scenario all --config all --duration 20
  python benchmarks/run.py --config async --verify-full-async
"""

import argparse
import asyncio
import csv
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from loadgen import ProcessSampler, run_load  # noqa: E402

CONFIGS = {
    "sync1": {"interface": "wsgi", "blocking_threads": 1, "path": "sync"},
    "sync10": {"interface": "wsgi", "blocking_threads": 10, "path": "sync"},
    "sync100": {"interface": "wsgi", "blocking_threads": 100, "path": "sync"},
    "async": {"interface": "asgi", "runtime_threads": 1, "path": "async"},
}
SCENARIOS = ("io", "cpu", "db")


def build_granian_cmd(python, config, host, port):
    cfg = CONFIGS[config]
    target = f"app.{cfg['interface']}:application"
    cmd = [
        python,
        "-m",
        "granian",
        "--interface",
        cfg["interface"],
        "--host",
        host,
        "--port",
        str(port),
        "--workers",
        "1",
        "--no-access-log",
        target,
    ]
    if "blocking_threads" in cfg:
        cmd += ["--blocking-threads", str(cfg["blocking_threads"])]
    if "runtime_threads" in cfg:
        cmd += ["--runtime-threads", str(cfg["runtime_threads"])]
    return cmd


def wait_for_health(base_url, timeout=20.0):
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/healthz/", timeout=1.0)
            if resp.status_code == 200:
                return True
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(0.2)
    raise RuntimeError(f"server did not become healthy: {last_err}")


def check_full_async(base_url):
    resp = httpx.get(f"{base_url}/__verify__/", timeout=5.0)
    count = int(resp.headers.get("x-sync-to-async-calls", "0"))
    return count, resp.text


_SEED_SCRIPT = """
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")
import django
django.setup()
from django.core.management import call_command
call_command("migrate", run_syncdb=True, verbosity=0)
from app.models import Widget
Widget.objects.all().delete()
Widget.objects.create(pk=1, name="bench", value=42)
print("seeded")
"""


def seed_db(python, env):
    """Migrate and seed the Widget row for the db scenario."""
    subprocess.run(
        [python, "-c", _SEED_SCRIPT],
        cwd=str(HERE),
        env=env,
        check=True,
    )


def run_one(python, config, scenario, host, port, duration, concurrency, verify, env):
    base_url = f"http://{host}:{port}"
    env = {**env}
    if scenario == "db":
        # The db scenario needs an async-capable backend; the sync builds use
        # the same postgres so the comparison is apples-to-apples.
        env["BENCH_DB"] = "postgres"
        seed_db(python, env)
    cmd = build_granian_cmd(python, config, host, port)
    proc = subprocess.Popen(cmd, cwd=str(HERE), env=env)
    try:
        wait_for_health(base_url)
        url = f"{base_url}/{scenario}/{CONFIGS[config]['path']}/"
        with ProcessSampler(proc.pid) as sampler:
            result = asyncio.run(
                run_load(url, concurrency=concurrency, duration_s=duration)
            )
        res = sampler.result()
        sync_calls = None
        if verify and CONFIGS[config]["interface"] == "asgi":
            sync_calls, _ = check_full_async(base_url)
        return result, res, sync_calls
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    parser.add_argument("--config", choices=(*CONFIGS, "all"), default="all")
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument(
        "--server-python",
        default=sys.executable,
        help="Interpreter that runs Granian (point at an upstream-Django venv "
        "to benchmark async-official).",
    )
    parser.add_argument(
        "--verify-full-async",
        action="store_true",
        help="Assert async builds make zero sync_to_async calls on the hot path.",
    )
    parser.add_argument("--label", default=None, help="Tag for this result set.")
    args = parser.parse_args()

    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    configs = tuple(CONFIGS) if args.config == "all" else (args.config,)

    env = {**os.environ}
    if args.verify_full_async:
        env["BENCH_VERIFY_FULL_ASYNC"] = "1"

    rows = []
    for scenario in scenarios:
        for config in configs:
            print(f"[run] scenario={scenario} config={config} ...", flush=True)
            result, res, sync_calls = run_one(
                args.server_python,
                config,
                scenario,
                args.host,
                args.port,
                args.duration,
                args.concurrency,
                args.verify_full_async,
                env,
            )
            row = {
                "scenario": scenario,
                "config": config,
                "rps": round(result.rps, 1),
                "p50_ms": round(result.p50, 2),
                "p95_ms": round(result.p95, 2),
                "p99_ms": round(result.p99, 2),
                "requests": result.requests,
                "errors": result.errors,
                "cpu_mean_pct": round(res.cpu_mean, 1),
                "cpu_peak_pct": round(res.cpu_peak, 1),
                "rss_mean_mb": round(res.rss_mean_mb, 1),
                "rss_peak_mb": round(res.rss_peak_mb, 1),
                "sync_to_async_calls": "" if sync_calls is None else sync_calls,
            }
            rows.append(row)
            print(
                f"      rps={row['rps']} p99={row['p99_ms']}ms "
                f"cpu_mean={row['cpu_mean_pct']}% rss_peak={row['rss_peak_mb']}MB "
                f"errors={row['errors']} s2a={row['sync_to_async_calls']}",
                flush=True,
            )

    write_results(rows, args.label)


def write_results(rows, label):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    name = f"{stamp}_{label}" if label else stamp
    outdir = HERE / "results" / name
    outdir.mkdir(parents=True, exist_ok=True)

    fields = list(rows[0].keys())
    csv_path = outdir / "results.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    md_path = outdir / "summary.md"
    with md_path.open("w") as f:
        by_scenario = {}
        for row in rows:
            by_scenario.setdefault(row["scenario"], []).append(row)
        for scenario, srows in by_scenario.items():
            f.write(f"## {scenario}-bound\n\n")
            f.write(
                "| config | rps | p50 ms | p95 ms | p99 ms | cpu mean % | "
                "rss peak MB | errors | s2a |\n"
            )
            f.write("|---|---|---|---|---|---|---|---|---|\n")
            for r in srows:
                f.write(
                    f"| {r['config']} | {r['rps']} | {r['p50_ms']} | {r['p95_ms']} "
                    f"| {r['p99_ms']} | {r['cpu_mean_pct']} | {r['rss_peak_mb']} "
                    f"| {r['errors']} | {r['sync_to_async_calls']} |\n"
                )
            f.write("\n")

    print(f"[done] wrote {csv_path} and {md_path}", flush=True)


if __name__ == "__main__":
    main()
