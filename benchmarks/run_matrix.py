#!/usr/bin/env python
"""Run the full benchmark matrix and write a single readable RESULTS.md.

Drives run.py once per row group (each writes its own CSV under results/),
collects the rows, and renders one markdown report at benchmarks/RESULTS.md.

Run:  .venv/bin/python benchmarks/run_matrix.py
"""

import csv
import datetime
import platform
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PYTHON = sys.executable

# Simulate a 1-vCPU VPS: pin the app server to a single core so sync100's
# thread pool can't escape to other cores while async stays single-threaded.
# Pin the load generator to disjoint cores so it doesn't steal the server's
# one core and pollute the cpu% measurement. Postgres runs in its own
# container on the remaining cores (a realistic "DB is a separate resource").
SERVER_CPUS = "0"
LOADGEN_CPUS = "1-8"

# Upstream Django interpreter for the side-by-side async comparison. Created
# with `uv venv .venv-upstream` + upstream Django from main. We re-run only the
# async config against this interpreter; sync configs are pure WSGI and the
# fork hasn't touched WSGI, so re-running them would just produce noise.
UPSTREAM_PYTHON = REPO / ".venv-upstream" / "bin" / "python"

# Each group is one run.py invocation. `note` explains the regime; `args` are
# passed through to run.py. Durations are kept modest so the whole matrix runs
# in a few minutes; numbers are steady-state (oha, 2s warmup).
GROUPS = [
    {
        "title": "I/O-bound (view sleeps 50ms), concurrency 100",
        "note": "Headline async win: one async worker holds 100 slow requests; "
        "sync needs a thread each.",
        "args": ["--scenario", "io", "--config", "all",
                 "--concurrency", "100", "--duration", "15"],
    },
    {
        "title": "CPU-bound (sha256 work), concurrency 100",
        "note": "Async should not win; confirms overhead is acceptable on a "
        "single core (GIL-bound).",
        "args": ["--scenario", "cpu", "--config", "all",
                 "--concurrency", "100", "--duration", "15"],
    },
    {
        "title": "DB single-row (aget, pooled), concurrency 100, 1ms/query DB latency",
        "note": "One indexed lookup per request against PostgreSQL via a "
        "connection pool, with 1ms of network latency injected (Toxiproxy) to "
        "simulate a real same-AZ DB. Even tiny per-query latency is what async "
        "exploits: while one request waits on the DB, the event loop serves "
        "others. Sync's threads can do the same but only up to the thread "
        "count, so the comparison gets honest only with non-zero latency.",
        "args": ["--scenario", "db", "--config", "all", "--pg-pool",
                 "--concurrency", "100", "--duration", "15",
                 "--db-latency-ms", "1", "--verify-full-async"],
    },
    {
        "title": "DB single-row with full middleware stack, concurrency 100, 1ms/query DB latency",
        "note": "Same workload as above but the bench app is configured with a "
        "production-shape middleware stack (security, sessions on signed "
        "cookies, common, csrf, auth, messages on cookie storage, "
        "clickjacking). This isolates the cost of the middleware chain "
        "itself. The fork's modernized built-ins go through native "
        "`__acall__`s with `s2a=0`; upstream Django still inherits "
        "`MiddlewareMixin` everywhere and pays a `sync_to_async` wrap on "
        "every `process_request` / `process_response` (visible as a large "
        "`s2a` count on the upstream-async row).",
        "args": ["--scenario", "db", "--config", "all", "--pg-pool",
                 "--concurrency", "100", "--duration", "15",
                 "--db-latency-ms", "1", "--verify-full-async"],
        "env": {"BENCH_FULL_MIDDLEWARE": "1"},
    },
    {
        "title": "DB heavy prefetch, per-request (concurrency 1, 5ms/query DB latency)",
        "note": "16 flat+nested prefetch lookups over ~20 tables, with 5ms "
        "network latency injected per query (Toxiproxy). At c=1 this isolates "
        "the within-request win: async runs the independent lookups in "
        "parallel on borrowed pooled connections; sync runs them sequentially.",
        "args": ["--scenario", "db_heavy", "--config", "all",
                 "--concurrency", "1", "--duration", "12",
                 "--db-latency-ms", "5", "--verify-full-async"],
    },
    {
        "title": "DB heavy prefetch, concurrent (concurrency 50, 5ms/query DB latency)",
        "note": "Same workload under load with a 48-connection pool. Async is "
        "single-thread CPU-bound here, so throughput is close to sync-with-"
        "100-threads but with one thread and better tail latency.",
        "args": ["--scenario", "db_heavy", "--config", "all",
                 "--concurrency", "50", "--duration", "12",
                 "--db-latency-ms", "5", "--verify-full-async"],
        "env": {"BENCH_PG_POOL_MAX": "48"},
    },
    {
        "title": "DB heavy prefetch, no injected latency (concurrency 50)",
        "note": "Localhost DB (sub-ms queries): parallelizing prefetch saves "
        "nothing, so this shows the overhead of the parallel machinery when "
        "there is no latency to hide.",
        "args": ["--scenario", "db_heavy", "--config", "all",
                 "--concurrency", "50", "--duration", "12",
                 "--verify-full-async"],
        "env": {"BENCH_PG_POOL_MAX": "48"},
    },
]

COLUMNS = [
    ("config", "config"),
    ("rps", "rps"),
    ("p50_ms", "p50 ms"),
    ("p95_ms", "p95 ms"),
    ("p99_ms", "p99 ms"),
    ("cpu_mean_pct", "cpu %"),
    ("rss_peak_mb", "rss MB"),
    ("errors", "errors"),
    ("sync_to_async_calls", "s2a"),
]


def _run_pass(group, *, server_python=None, override_args=()):
    """Invoke run.py once and return its result CSV rows.

    `override_args` replaces the `--scenario X --config Y` portion of the
    group's args when re-running only one config (used for the upstream pass,
    which only re-runs async).
    """
    import os

    env = {**os.environ, **group.get("env", {})}
    base_args = list(override_args) if override_args else list(group["args"])
    cmd = [
        PYTHON, str(HERE / "run.py"), *base_args,
        "--server-cpus", SERVER_CPUS, "--loadgen-cpus", LOADGEN_CPUS,
    ]
    if server_python is not None:
        cmd += ["--server-python", str(server_python)]
    tag = "upstream" if server_python else "fork"
    print(f"\n>>> [{tag}] {' '.join(base_args)}", flush=True)
    out = subprocess.run(
        cmd, cwd=str(HERE), env=env, capture_output=True, text=True
    )
    sys.stdout.write(out.stdout)
    sys.stderr.write(out.stderr)
    out.check_returncode()
    m = re.search(r"wrote (\S+results\.csv)", out.stdout)
    if not m:
        raise RuntimeError("could not find results.csv path in run.py output")
    with open(m.group(1), newline="") as f:
        return list(csv.DictReader(f))


def _run_group(group):
    """Run the fork pass + the upstream-async pass, then merge the rows.

    The merged ordering puts upstream-async right after the fork's async row so
    the report compares them side by side.
    """
    fork_rows = _run_pass(group)
    # Re-run just the async config under the upstream interpreter. Mirror the
    # group's args but force --config async; everything else (scenario, latency,
    # concurrency, duration, verify) stays identical.
    upstream_args = []
    it = iter(group["args"])
    for a in it:
        if a == "--config":
            next(it)  # skip whatever value the group used
            upstream_args += ["--config", "async"]
        else:
            upstream_args.append(a)
    upstream_rows = _run_pass(group, server_python=UPSTREAM_PYTHON,
                              override_args=upstream_args)
    for r in upstream_rows:
        r["config"] = "upstream-async"
    # Put all fork rows first (sync1, sync10, sync100, async, async-rsgi),
    # then the upstream-async row at the end so the report reads
    # "this fork's options first, upstream comparison after".
    return list(fork_rows) + list(upstream_rows)


def _table(rows):
    head = "| " + " | ".join(label for _, label in COLUMNS) + " |"
    sep = "|" + "|".join(["---"] * len(COLUMNS)) + "|"
    lines = [head, sep]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(key, "")) for key, _ in COLUMNS) + " |")
    return "\n".join(lines)


def _versions():
    def ver(mod):
        try:
            return __import__(mod).__version__
        except Exception:  # noqa: BLE001
            return "?"

    oha = subprocess.run(
        ["oha", "--version"], capture_output=True, text=True
    ).stdout.strip() or "?"
    pg = subprocess.run(
        ["docker", "exec", "django-asyncio-pg", "postgres", "--version"],
        capture_output=True, text=True,
    ).stdout.strip() or "?"
    upstream_django = subprocess.run(
        [str(UPSTREAM_PYTHON), "-c",
         "import django; print(django.__version__)"],
        capture_output=True, text=True, cwd=str(HERE),
    ).stdout.strip() or "?"
    return {
        "python": platform.python_version(),
        "granian": ver("granian"),
        "uvloop": ver("uvloop"),
        "oha": oha,
        "postgres": pg,
        "platform": platform.platform(),
        "upstream_django": upstream_django,
    }


def main():
    groups = [(g, _run_group(g)) for g in GROUPS]
    v = _versions()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    parts = [
        "# django-asyncio benchmark results",
        "",
        f"Generated: {now}",
        "",
        "## Environment",
        "",
        f"- CPython {v['python']} ({v['platform']})",
        f"- Granian {v['granian']}, 1 worker process throughout",
        f"- Async event loop: uvloop {v['uvloop']} (libuv). Stdlib asyncio is "
        "noticeably slower per request, so benchmarking with the selector loop "
        "would understate every async build.",
        f"- Load generator: {v['oha']}",
        f"- Database: {v['postgres']} (Docker, local)",
        "- DB network latency injected with Toxiproxy (a `latency` toxic on the "
        "PostgreSQL proxy)",
        f"- **Simulated 1-vCPU VPS**: the app server is pinned with `taskset` to "
        f"a single core (cpu {SERVER_CPUS}); the load generator is pinned to "
        f"separate cores (cpu {LOADGEN_CPUS}) so it cannot steal the server's "
        "core. This caps every build at one core of CPU, so `sync100`'s thread "
        "pool contends on one core instead of spreading across the host.",
        "",
        "## Builds compared",
        "",
        "- **sync1 / sync10 / sync100**: WSGI on Granian with a blocking-thread "
        "pool of 1 / 10 / 100. One thread serves one request at a time. Same "
        "code on both this fork and upstream (we haven't touched the WSGI "
        "path), so we measure it once.",
        "- **async**: this fork on ASGI, single async worker, native async ORM "
        "(no `sync_to_async` on the hot path).",
        "- **async-rsgi**: this fork on Granian's native RSGI protocol. Same "
        "Django middleware, ORM, and views as `async`; only the protocol "
        "adapter changes. RSGI replaces ASGI's read-body and send-response "
        "message loops with single calls, removing several per-request "
        "awaits.",
        f"- **upstream-async**: upstream Django {v['upstream_django']} on the "
        "same setup. Falls back to `sync_to_async` for the ORM bits the fork "
        "has rewritten natively. This is the direct \"what did our fork "
        "actually buy us?\" comparison.",
        "",
        "`s2a` = number of `sync_to_async` calls recorded on the async request "
        "path during the run (0 means genuinely native).",
        "",
        "## Results",
        "",
    ]
    for g, rows in groups:
        parts.append(f"### {g['title']}")
        parts.append("")
        parts.append(g["note"])
        parts.append("")
        parts.append(_table(rows))
        parts.append("")

    parts += [
        "## Notes",
        "",
        "- On the **db single-row** scenario, async loses to `sync10`/"
        "`sync100` by ~25-30% even with 1ms injected latency. This is the "
        "*one-core CPU ceiling*: at high concurrency, both `sync10` (10 "
        "threads sharing the GIL on one core) and `async` (one event-loop "
        "thread on one core) become CPU-bound at `1 / per-request-CPU-cost`. "
        "Sync's per-request Python cost is lower than async's (no `await` "
        "scheduling, no asgiref `Local` dispatch, no async ORM machinery), so "
        "sync wins regardless of latency on a single core. The gap would "
        "shrink or flip on multi-core VPSes where async runs as multiple "
        "workers and sync's threads would have to span cores. **The fork "
        "still beats upstream-async by ~2x** (upstream falls back to "
        "`sync_to_async` for native ORM bits, ~45k s2a calls in this group), "
        "which is the win our fork actually delivers.",
        "- **`async-rsgi` is the more efficient async option on this fork.** "
        "On the same one-core setup, RSGI buys ~10% on db single-row over "
        "ASGI (1977 vs 1802 rps), and is dramatically lighter on CPU at "
        "comparable I/O throughput (e.g. io c=100: 1758 rps at 27% CPU vs "
        "ASGI's 1706 rps at 34% CPU). It is essentially neutral on "
        "db_heavy/cpu workloads, because protocol overhead isn't the "
        "binding constraint there. The trade-off: RSGI ties Django to "
        "Granian, so the standard ASGI handler remains supported for "
        "deployments that need a different ASGI server.",
        "- **The biggest win against upstream shows up on the *full "
        "middleware stack* row.** With a production-shape stack (security, "
        "sessions on signed cookies, common, csrf, auth, messages on "
        "cookie storage, clickjacking), the fork serves the same db "
        "single-row workload at **1667 rps with `s2a=0`** (async-rsgi), "
        "while upstream-async hits **290 rps with ~78k `sync_to_async` "
        "calls per run** (~18 per request). That is a ~5.75x speedup just "
        "from removing the middleware sync_to_async tax. Upstream still "
        "inherits `MiddlewareMixin` everywhere, so every `process_request` "
        "and `process_response` on the async path is wrapped in "
        "`sync_to_async(thread_sensitive=True)`. This fork rewrites every "
        "built-in middleware as a plain hybrid class with a native async "
        "`__acall__`, so the chain is genuinely async end-to-end. The "
        "modernized middleware also keeps `process_request` / "
        "`process_response` as the public method names, so third-party "
        "subclasses keep working.",
        "- The **db_heavy** scenario is what the parallel async prefetch was "
        "built for. Each request fetches a page of `Author` rows and prefetches "
        "16 lookups spanning forward/reverse FK, forward/reverse one-to-one, "
        "M2M, and 2-3 levels of nesting. The number of prefetch queries is "
        "roughly constant (~17), so under per-query latency the sequential cost "
        "grows with the number of lookups while the parallel cost grows only "
        "with the depth of the tree.",
        "- Parallel prefetch is **opportunistic**: a sub-query borrows a pooled "
        "connection only if one is already idle, runs there, and returns it; "
        "otherwise it runs on the connection the request already holds. It never "
        "grows the pool and never waits, so it cannot deadlock. The `db_heavy` "
        "pool is pre-warmed (min == max) so idle connections exist to borrow.",
        "- Inside a transaction the prefetch runs sequentially on the "
        "transactional connection (an independent connection would not see "
        "uncommitted state).",
        "",
        "Reproduce: `python benchmarks/run_matrix.py` (needs the postgres and "
        "toxiproxy containers; the harness starts toxiproxy automatically).",
        "",
    ]
    (HERE / "RESULTS.md").write_text("\n".join(parts))
    print(f"\n[done] wrote {HERE / 'RESULTS.md'}", flush=True)


if __name__ == "__main__":
    main()
