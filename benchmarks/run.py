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
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from loadgen import (  # noqa: E402
    ProcessSampler,
    find_oha,
    run_load,
    run_load_oha,
)

CONFIGS = {
    "sync1": {"interface": "wsgi", "blocking_threads": 1, "path": "sync"},
    "sync10": {"interface": "wsgi", "blocking_threads": 10, "path": "sync"},
    "sync100": {"interface": "wsgi", "blocking_threads": 100, "path": "sync"},
    # Default to uvloop (libuv): stdlib asyncio's selector loop adds enough
    # per-request overhead to noticeably understate async throughput, so it's
    # not a representative comparison. Granian falls back to asyncio if uvloop
    # isn't installed.
    # task-impl=rust uses granian's Rust-backed asyncio.Task implementation
    # instead of CPython's. Cheap to try, may shave a bit of the asyncio task
    # allocation/scheduling tax the profile showed.
    "async": {"interface": "asgi", "runtime_threads": 1, "path": "async",
              "loop": "uvloop"},
    # RSGI is granian's native protocol. Same Django ORM/middleware as async,
    # but the request/response adapter avoids ASGI's read-body + send-response
    # message loops and the disconnect TaskGroup, cutting per-request awaits.
    "async-rsgi": {"interface": "rsgi", "runtime_threads": 1, "path": "async",
                   "loop": "uvloop"},
    # massless: the Cython drop-in server (its own protocol, not granian). Serves
    # the same async views/middleware/ORM as `async`; selected with --server-python
    # pointing at a venv that has django-massless installed.
    "massless": {"interface": "massless", "path": "async"},
}
SCENARIOS = ("io", "cpu", "db", "db_heavy")


def taskset_prefix(cpus):
    """Return a ``taskset -c <cpus>`` command prefix, or [] when unset.

    `cpus` is a taskset CPU list (e.g. "0", "0-1", "0,2"). Pinning the server
    to a fixed CPU budget makes the sync-vs-async comparison fair: sync100 can
    otherwise spread across every host core while async is single-threaded.
    Pin the load generator to *different* cores so it doesn't steal the budget.
    """
    if not cpus:
        return []
    if shutil.which("taskset") is None:
        raise SystemExit(
            "taskset not found (install util-linux) but --server-cpus/"
            "--loadgen-cpus was given."
        )
    return ["taskset", "-c", str(cpus)]


def build_granian_cmd(python, config, host, port, server_cpus=None):
    cfg = CONFIGS[config]
    target = f"app.{cfg['interface']}:application"
    cmd = taskset_prefix(server_cpus) + [
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
    if "loop" in cfg:
        cmd += ["--loop", cfg["loop"]]
    if "task_impl" in cfg:
        cmd += ["--task-impl", cfg["task_impl"]]
    return cmd


def build_massless_cmd(python, config, host, port, server_cpus=None):
    # massless is its own server (python -m massless), not granian. `-m` puts cwd
    # (the benchmarks dir) on sys.path so `app` / `app.settings` import.
    return taskset_prefix(server_cpus) + [
        python,
        "-m",
        "massless",
        "--settings",
        "app.settings",
        "--host",
        host,
        "--port",
        str(port),
        "--processes",
        "1",
    ]


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
    if count:
        print(f"[verify] {count} sync_to_async call site(s):\n{resp.text}", flush=True)
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


_SEED_HEAVY_SCRIPT = """
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")
import django
django.setup()
from django.core.management import call_command
call_command("migrate", run_syncdb=True, verbosity=0)
from app.seed import seed_heavy
seed_heavy(n_authors=%d)
print("seeded heavy")
"""


def seed_db(python, env, script=_SEED_SCRIPT):
    """Migrate and seed the rows for a db scenario."""
    subprocess.run(
        [python, "-c", script],
        cwd=str(HERE),
        env=env,
        check=True,
    )


def run_one(
    python, config, scenario, host, port, duration, concurrency, verify, env, oha_bin,
    db_latency_ms=0.0, db_jitter_ms=0.0, server_cpus=None, loadgen_cpus=None,
):
    base_url = f"http://{host}:{port}"
    env = {**env}
    if scenario in ("db", "db_heavy"):
        env["BENCH_DB"] = "postgres"
        if scenario == "db_heavy":
            # Heavy prefetch needs a pool so the async path can borrow idle
            # connections to run independent prefetch queries in parallel. Pre-
            # warm it (min == max) so spare connections already exist for the
            # opportunistic borrow: the prefetch only uses connections that are
            # already idle, it never grows the pool itself.
            env["BENCH_PG_POOL"] = "1"
            env.setdefault("BENCH_PG_POOL_MAX", "16")
            env["BENCH_PG_POOL_MIN"] = env["BENCH_PG_POOL_MAX"]
        # Seed against postgres directly (no injected latency, so seeding is
        # fast even when the run itself goes through a latency proxy).
        seed_env = {**env, "BENCH_PG_PORT": env.get("BENCH_PG_PORT", "55432")}
        if scenario == "db_heavy":
            n = int(env.get("BENCH_HEAVY_AUTHORS", "25"))
            seed_db(python, seed_env, script=_SEED_HEAVY_SCRIPT % max(n, 50))
        else:
            seed_db(python, seed_env)
        # Route the server's DB traffic through Toxiproxy to add per-query
        # network latency, so the async parallel prefetch can overlap it.
        if db_latency_ms > 0:
            import toxiproxy

            proxy_port = toxiproxy.configure(
                latency_ms=db_latency_ms,
                jitter_ms=db_jitter_ms,
                upstream_port=int(seed_env["BENCH_PG_PORT"]),
            )
            env["BENCH_PG_PORT"] = str(proxy_port)
    if CONFIGS[config]["interface"] == "massless":
        cmd = build_massless_cmd(python, config, host, port, server_cpus)
    else:
        cmd = build_granian_cmd(python, config, host, port, server_cpus)
    # Run granian in its own process group so we can kill the worker subprocess
    # along with the main on cleanup. Without this, a SIGTERM to the main can
    # leave the worker orphaned and holding the listen port, which wedges the
    # next run.
    proc = subprocess.Popen(cmd, cwd=str(HERE), env=env, start_new_session=True)
    try:
        wait_for_health(base_url)
        url = f"{base_url}/{scenario}/{CONFIGS[config]['path']}/"
        with ProcessSampler(proc.pid) as sampler:
            if oha_bin:
                result = run_load_oha(
                    url,
                    concurrency=concurrency,
                    duration_s=duration,
                    oha_bin=oha_bin,
                    cpus=loadgen_cpus,
                )
            else:
                result = asyncio.run(
                    run_load(url, concurrency=concurrency, duration_s=duration)
                )
        res = sampler.result()
        sync_calls = None
        if verify and CONFIGS[config]["interface"] in ("asgi", "rsgi"):
            sync_calls, _ = check_full_async(base_url)
        return result, res, sync_calls
    finally:
        _terminate_group(proc)


def _terminate_group(proc):
    """SIGTERM the granian process group, then SIGKILL if it doesn't exit."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10 if sig is signal.SIGTERM else 5)
            return
        except subprocess.TimeoutExpired:
            continue


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
        # Make path absolute but DON'T follow symlinks. A venv's bin/python is
        # a symlink to the system interpreter, and resolving it loses the venv.
        type=lambda p: str(Path(p).absolute()),
        help="Interpreter that runs Granian (point at an upstream-Django venv "
        "to benchmark async-official). Relative paths are resolved against "
        "the current working directory before granian is launched.",
    )
    parser.add_argument(
        "--verify-full-async",
        action="store_true",
        help="Assert async builds make zero sync_to_async calls on the hot path.",
    )
    parser.add_argument(
        "--loadgen",
        choices=("auto", "oha", "httpx"),
        default="auto",
        help="Load generator. 'auto' uses oha if installed, else the httpx "
        "fallback. The single-process httpx fallback cannot saturate an async "
        "server and understates async throughput; install oha for real numbers "
        "(cargo install oha).",
    )
    parser.add_argument(
        "--pg-pool",
        action="store_true",
        help="Enable a psycopg connection pool for the db scenario. Without it "
        "every request opens a fresh connection, so the db scenario measures "
        "connection setup rather than query execution.",
    )
    parser.add_argument(
        "--db-latency-ms",
        type=float,
        default=0.0,
        help="Inject this much per-query network latency (ms) between the app "
        "and postgres via Toxiproxy, for the db/db_heavy scenarios. This is "
        "what lets async parallel prefetch overlap latency the sync path pays "
        "serially. Requires the Toxiproxy container (auto-started if absent).",
    )
    parser.add_argument(
        "--db-jitter-ms",
        type=float,
        default=0.0,
        help="Random jitter (ms) added to --db-latency-ms per query.",
    )
    parser.add_argument(
        "--server-cpus",
        default=None,
        help="Pin the Granian server to this taskset CPU list (e.g. '0', "
        "'0-1', '0,2'). Caps its CPU budget so sync100 can't spread across "
        "every core while async stays single-threaded. Makes the comparison "
        "fair. Requires taskset (util-linux).",
    )
    parser.add_argument(
        "--loadgen-cpus",
        default=None,
        help="Pin the oha load generator to this taskset CPU list. Use cores "
        "disjoint from --server-cpus so the generator doesn't steal the "
        "server's CPU budget and pollute the cpu%% measurement.",
    )
    parser.add_argument("--label", default=None, help="Tag for this result set.")
    args = parser.parse_args()

    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    configs = tuple(CONFIGS) if args.config == "all" else (args.config,)

    if args.loadgen == "httpx":
        oha_bin = None
    else:
        oha_bin = find_oha()
        if oha_bin is None and args.loadgen == "oha":
            raise SystemExit(
                "oha not found on PATH or ~/.cargo/bin. Install it with "
                "`cargo install oha`, or pass --loadgen httpx (understates async)."
            )
    if oha_bin is None:
        print(
            "[warn] using the single-process httpx load generator; it cannot "
            "saturate an async server and UNDERSTATES async throughput. Install "
            "oha (cargo install oha) for trustworthy numbers.",
            flush=True,
        )
    else:
        print(f"[info] load generator: oha ({oha_bin})", flush=True)

    env = {**os.environ}
    if args.verify_full_async:
        env["BENCH_VERIFY_FULL_ASYNC"] = "1"
    if args.pg_pool:
        env["BENCH_PG_POOL"] = "1"

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
                oha_bin,
                args.db_latency_ms,
                args.db_jitter_ms,
                args.server_cpus,
                args.loadgen_cpus,
            )
            db_lat = (
                args.db_latency_ms if scenario in ("db", "db_heavy") else 0.0
            )
            row = {
                "scenario": scenario,
                "config": config,
                "db_latency_ms": db_lat,
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
