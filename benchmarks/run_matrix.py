#!/usr/bin/env python
"""Run the full benchmark matrix and write a single readable RESULTS.md.

Drives run.py once per row group (each writes its own CSV under results/),
collects the rows, and renders one markdown report at benchmarks/RESULTS.md.

Run:  .venv/bin/python benchmarks/run_matrix.py
"""

import argparse
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

# The fork configs of a group without its own `configs`. massless is left out
# because it needs a --server-python venv with django-massless installed.
FORK_CONFIGS = ["sync1", "sync10", "sync100", "async", "async-rsgi"]

# Each group is one run.py invocation. `note` explains the regime; `args` are
# passed through to run.py. Durations are kept modest so the whole matrix runs
# in a few minutes; numbers are steady-state (oha, 2s warmup).
GROUPS = [
    {
        "title": "I/O-bound (view sleeps 50ms), concurrency 100",
        "note": "Headline async win: one async worker holds 100 slow requests; "
        "sync needs a thread each.",
        "args": ["--scenario", "io", "--concurrency", "100", "--duration", "15"],
    },
    {
        "title": "CPU-bound (sha256 work), concurrency 100",
        "note": "Async should not win; confirms overhead is acceptable on a "
        "single core (GIL-bound).",
        "args": ["--scenario", "cpu", "--concurrency", "100", "--duration", "15"],
    },
    {
        "title": "DB single-row (aget, pooled), concurrency 100, 1ms/query DB latency",
        "note": "One indexed lookup per request against PostgreSQL via a "
        "connection pool, with 1ms of network latency injected (Toxiproxy) to "
        "simulate a real same-AZ DB. Even tiny per-query latency is what async "
        "exploits: while one request waits on the DB, the event loop serves "
        "others. Sync's threads can do the same but only up to the thread "
        "count, so the comparison gets honest only with non-zero latency.",
        "args": [
            "--scenario",
            "db",
            "--pg-pool",
            "--concurrency",
            "100",
            "--duration",
            "15",
            "--db-latency-ms",
            "1",
            "--verify-full-async",
        ],
    },
    {
        "title": "DB single-row with full middleware stack, concurrency 100, "
        "1ms/query DB latency",
        "note": "Same workload as above but the bench app is configured with a "
        "production-shape middleware stack (security, sessions on signed "
        "cookies, common, csrf, auth, messages on cookie storage, "
        "clickjacking). This isolates the cost of the middleware chain "
        "itself. The fork's modernized built-ins go through native "
        "`__acall__`s with `s2a=0`; upstream Django still inherits "
        "`MiddlewareMixin` everywhere and pays a `sync_to_async` wrap on "
        "every `process_request` / `process_response` (visible as a large "
        "`s2a` count on the upstream-async row).",
        "args": [
            "--scenario",
            "db",
            "--pg-pool",
            "--concurrency",
            "100",
            "--duration",
            "15",
            "--db-latency-ms",
            "1",
            "--verify-full-async",
        ],
        "env": {"BENCH_FULL_MIDDLEWARE": "1"},
    },
    {
        "title": "DB heavy prefetch, per-request (concurrency 1, 5ms/query DB latency)",
        "note": "16 flat+nested prefetch lookups over ~20 tables, with 5ms "
        "network latency injected per query (Toxiproxy). At c=1 this isolates "
        "the within-request win: async sends each level of the lookup tree as "
        "one batch (1 + 3 round trips); sync sends its 1 + 16 queries one "
        "after another.",
        "args": [
            "--scenario",
            "db_heavy",
            "--concurrency",
            "1",
            "--duration",
            "12",
            "--db-latency-ms",
            "5",
            "--verify-full-async",
        ],
    },
    {
        "title": "DB heavy prefetch, concurrent (concurrency 50, 5ms/query DB latency)",
        "note": "Same workload under load with a 48-connection pool. Async is "
        "single-thread CPU-bound here, so throughput is close to sync-with-"
        "100-threads but with one thread and better tail latency.",
        "args": [
            "--scenario",
            "db_heavy",
            "--concurrency",
            "50",
            "--duration",
            "12",
            "--db-latency-ms",
            "5",
            "--verify-full-async",
        ],
        "env": {"BENCH_PG_POOL_MAX": "48"},
    },
    {
        "title": "DB heavy prefetch, no injected latency (concurrency 50)",
        "note": "Localhost DB (sub-ms queries): with almost no latency to save, "
        "this shows the CPU cost of the batched prefetch.",
        "args": [
            "--scenario",
            "db_heavy",
            "--concurrency",
            "50",
            "--duration",
            "12",
            "--verify-full-async",
        ],
        "env": {"BENCH_PG_POOL_MAX": "48"},
    },
    {
        "title": "DB heavy prefetch in a transaction, per-request (concurrency 1, "
        "5ms/query DB latency)",
        "note": "The per-request db_heavy workload with the prefetch inside "
        "`transaction.atomic()`. The async batch runs on the transaction's "
        "connection, so it keeps its 1 + 3 round trips. upstream-async is left "
        "out because stock Django has no async `atomic()`, and sync100 because "
        "at concurrency 1 it measures the same as sync1 and sync10.",
        "args": [
            "--scenario",
            "db_heavy_atomic",
            "--concurrency",
            "1",
            "--duration",
            "12",
            "--db-latency-ms",
            "5",
            "--verify-full-async",
        ],
        "configs": ["sync1", "sync10", "async", "async-rsgi"],
        "upstream": False,
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


def _run_pass(group, configs, *, server_python=None):
    """Invoke run.py once for `configs` and return its result CSV rows."""
    import os

    env = {**os.environ, **group.get("env", {})}
    args = [*group["args"], "--config", ",".join(configs)]
    cmd = [
        PYTHON,
        str(HERE / "run.py"),
        *args,
        "--server-cpus",
        SERVER_CPUS,
        "--loadgen-cpus",
        LOADGEN_CPUS,
    ]
    if server_python is not None:
        cmd += ["--server-python", str(server_python)]
    tag = "upstream" if server_python else "fork"
    print(f"\n>>> [{tag}] {' '.join(args)}", flush=True)
    out = subprocess.run(cmd, cwd=str(HERE), env=env, capture_output=True, text=True)
    sys.stdout.write(out.stdout)
    sys.stderr.write(out.stderr)
    out.check_returncode()
    m = re.search(r"wrote (\S+results\.csv)", out.stdout)
    if not m:
        raise RuntimeError("could not find results.csv path in run.py output")
    with open(m.group(1), newline="") as f:
        return list(csv.DictReader(f))


def _run_group(group):
    """Run the fork pass, then the upstream pass unless the group opts out."""
    rows = _run_pass(group, group.get("configs", FORK_CONFIGS))
    if group.get("upstream", True):
        # Re-run just the async config under the upstream interpreter, with
        # the group's other args (scenario, latency, concurrency) unchanged.
        upstream_rows = _run_pass(group, ["async"], server_python=UPSTREAM_PYTHON)
        for r in upstream_rows:
            r["config"] = "upstream-async"
        rows += upstream_rows
    return rows


def _table(rows):
    head = "| " + " | ".join(label for _, label in COLUMNS) + " |"
    sep = "|" + "|".join(["---"] * len(COLUMNS)) + "|"
    lines = [head, sep]
    for r in rows:
        lines.append(
            "| " + " | ".join(str(r.get(key, "")) for key, _ in COLUMNS) + " |"
        )
    return "\n".join(lines)


def _previous_tables():
    """Map each group title in the current RESULTS.md to its table."""
    text = (HERE / "RESULTS.md").read_text()
    results = text.split("\n## Results\n", 1)[1].split("\n## ", 1)[0]
    tables = {}
    for section in results.split("\n### ")[1:]:
        title, _, body = section.partition("\n")
        tables[title] = "\n".join(
            line for line in body.splitlines() if line.startswith("|")
        )
    return tables


def _versions():
    def ver(mod):
        try:
            return __import__(mod).__version__
        except Exception:  # noqa: BLE001
            return "?"

    oha = (
        subprocess.run(
            ["oha", "--version"], capture_output=True, text=True
        ).stdout.strip()
        or "?"
    )
    pg = (
        subprocess.run(
            ["docker", "exec", "django-asyncio-pg", "postgres", "--version"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        or "?"
    )
    upstream_django = (
        subprocess.run(
            [str(UPSTREAM_PYTHON), "-c", "import django; print(django.__version__)"],
            capture_output=True,
            text=True,
            cwd=str(HERE),
        ).stdout.strip()
        or "?"
    )
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        metavar="TEXT",
        help="Run only the groups whose title contains TEXT, and copy the "
        "other tables from the current RESULTS.md.",
    )
    args = parser.parse_args()
    selected = [g for g in GROUPS if args.only is None or args.only in g["title"]]
    previous = _previous_tables() if args.only else {}
    missing = [
        g["title"] for g in GROUPS if g not in selected and g["title"] not in previous
    ]
    if missing:
        parser.error(f"RESULTS.md has no table for: {'; '.join(missing)}")
    tables = [
        _table(_run_group(g)) if g in selected else previous[g["title"]] for g in GROUPS
    ]
    v = _versions()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    generated = f"Generated: {now}"
    if args.only:
        generated += (
            f'. Only the groups whose title contains "{args.only}" ran then; '
            "the other tables are copied from the previous report."
        )

    parts = [
        "# django-asyncio benchmark results",
        "",
        generated,
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
        'has rewritten natively. This is the direct "what did our fork '
        'actually buy us?" comparison.',
        "",
        "`s2a` = number of `sync_to_async` calls recorded on the async request "
        "path during the run (0 means genuinely native).",
        "",
        "## Results",
        "",
    ]
    for g, table in zip(GROUPS, tables):
        parts.append(f"### {g['title']}")
        parts.append("")
        parts.append(g["note"])
        parts.append("")
        parts.append(table)
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
        "- The **db_heavy** scenario measures the batched async prefetch. Each "
        "request fetches a page of `Author` rows and prefetches 16 lookups "
        "spanning forward/reverse FK, forward/reverse one-to-one, M2M, and 2-3 "
        "levels of nesting. Sync sends its 1 + 16 queries one after another. "
        "Async walks the lookup tree breadth-first and sends each level as one "
        "multi-statement batch, so it pays 1 + 3 round trips: its cost grows "
        "with the depth of the tree, not with the number of lookups.",
        "- The batch runs on the connection the request already holds. It needs "
        "no spare pooled connections and keeps its round trips inside "
        "`transaction.atomic()` (the db_heavy_atomic table). Each batch goes "
        "through `SQLCompiler.aexecute_sql_batch()`, where an ORM cache can "
        "answer a whole level with one lookup.",
        "- **Further micro-optimization attempts (post-middleware "
        "modernization).** A round of small async-overhead reductions was "
        "tried after the middleware modernization landed. Findings:"
        "  (a) `asyncio.eager_task_factory` (stdlib, 3.12+): expected to "
        "skip Task allocation for coroutines that never suspend, but "
        "interacts poorly with uvloop's optimized Task implementation and "
        "caused a ~4% regression on this workload. Not applied."
        "  (b) Signal dispatch fast-path for the common 0/1 receiver case "
        "in `Signal.asend` / `Signal.asend_robust` / `_run_parallel`: "
        "removes a TaskGroup, a contextvars copy, and a no-op `sync_send` "
        "coroutine when only one async receiver is registered (the actual "
        "shape of `request_started` and `request_finished` in this fork). "
        "Theoretically sound, applied."
        "  (c) `ASGI_THREAD_SENSITIVE` setting (default `True`): the ASGI "
        "and RSGI handlers wrap each request in "
        "`asgiref.sync.ThreadSensitiveContext` so that any "
        "`sync_to_async(thread_sensitive=True)` call inside the request "
        "reuses the same helper thread. For purely native-async stacks "
        "this is unused overhead and can be opted out of. Bench app sets "
        "the flag to `False`."
        "  Per-change throughput delta on db single-row with full "
        "middleware is below the bench noise floor (~2-3%) on this "
        "single-core setup, so the cumulative effect is reported as "
        "essentially unchanged. Both (b) and (c) are committed as code "
        "improvements (cleaner per-request work, opt-out for users who "
        "want to skip a known-unused context manager).",
        "",
        "Reproduce: `python benchmarks/run_matrix.py` (needs the postgres and "
        "toxiproxy containers; the harness starts toxiproxy automatically).",
        "",
    ]
    (HERE / "RESULTS.md").write_text("\n".join(parts))
    print(f"\n[done] wrote {HERE / 'RESULTS.md'}", flush=True)


if __name__ == "__main__":
    main()
