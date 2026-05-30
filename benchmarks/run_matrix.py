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
PYTHON = sys.executable

# Simulate a 1-vCPU VPS: pin the app server to a single core so sync100's
# thread pool can't escape to other cores while async stays single-threaded.
# Pin the load generator to disjoint cores so it doesn't steal the server's
# one core and pollute the cpu% measurement. Postgres runs in its own
# container on the remaining cores (a realistic "DB is a separate resource").
SERVER_CPUS = "0"
LOADGEN_CPUS = "1-8"

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
        "title": "DB single-row (aget, pooled), concurrency 100",
        "note": "One indexed lookup per request against PostgreSQL via a "
        "connection pool.",
        "args": ["--scenario", "db", "--config", "all", "--pg-pool",
                 "--concurrency", "100", "--duration", "15",
                 "--verify-full-async"],
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


def _run_group(group):
    import os

    env = {**os.environ, **group.get("env", {})}
    cmd = [
        PYTHON, str(HERE / "run.py"), *group["args"],
        "--server-cpus", SERVER_CPUS, "--loadgen-cpus", LOADGEN_CPUS,
    ]
    print(f"\n>>> {' '.join(group['args'])}", flush=True)
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
    return {
        "python": platform.python_version(),
        "granian": ver("granian"),
        "oha": oha,
        "postgres": pg,
        "platform": platform.platform(),
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
        "pool of 1 / 10 / 100. One thread serves one request at a time.",
        "- **async**: this fork on ASGI, single async worker, native async ORM "
        "(no `sync_to_async` on the hot path).",
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
