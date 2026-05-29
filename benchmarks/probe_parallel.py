#!/usr/bin/env python
"""Empirical probe: do concurrent async queries actually run in parallel?

Question this answers (see PLAN.md / prefetch parallelization design):
the fork keeps one async connection per DatabaseWrapper, and `connections`
is thread-critical, so every asyncio task on the event-loop thread shares
that one connection. A single psycopg AsyncConnection serializes concurrent
operations. So does `asyncio.gather` over N queries actually parallelize, or
does it serialize on the shared connection? And do independent connections
(one per branch, optionally pooled) recover real parallelism?

Each "query" is `SELECT pg_sleep(LAT)`, which occupies a backend for LAT
seconds. That holds the connection, exactly like a real round trip would.

Run:  .venv/bin/python benchmarks/probe_parallel.py
"""

import asyncio
import os
import time

import django
from django.conf import settings

N = int(os.environ.get("PROBE_N", "10"))
LAT = float(os.environ.get("PROBE_LAT", "0.05"))  # seconds per query

PG = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "djangoasync",
    "USER": "djangoasync",
    "PASSWORD": "djangoasync",
    "HOST": "127.0.0.1",
    "PORT": "55432",
    "CONN_MAX_AGE": 0,
    "AUTOCOMMIT": True,
    "TIME_ZONE": None,
    "CONN_HEALTH_CHECKS": False,
    "OPTIONS": {},
}
POOLED = {**PG, "OPTIONS": {"pool": {"min_size": N, "max_size": N}}}

settings.configure(
    DEBUG=False,
    DATABASES={"default": PG, "pooled": POOLED},
    INSTALLED_APPS=[],
    USE_TZ=True,
)
django.setup()

from django.db import connections  # noqa: E402


async def _run_one(wrapper, pids):
    await wrapper.aensure_connection()
    async with await wrapper.acursor() as cur:
        await cur.execute("SELECT pg_backend_pid(), pg_sleep(%s)", (LAT,))
        row = await cur.fetchone()
        pids.append(row[0])


async def serial_shared(pids):
    conn = connections["default"]
    for _ in range(N):
        await _run_one(conn, pids)
    await conn.aclose()


async def gather_shared(pids):
    conn = connections["default"]
    await asyncio.gather(*(_run_one(conn, pids) for _ in range(N)))
    await conn.aclose()


async def _gather_independent(alias, pids):
    wrappers = [connections.create_connection(alias) for _ in range(N)]
    try:
        await asyncio.gather(*(_run_one(w, pids) for w in wrappers))
    finally:
        await asyncio.gather(*(w.aclose() for w in wrappers))


async def gather_independent_nopool(pids):
    await _gather_independent("default", pids)


async def gather_independent_pool(pids):
    await _gather_independent("pooled", pids)


async def _time(label, coro_fn):
    pids = []
    t0 = time.perf_counter()
    await coro_fn(pids)
    dt = time.perf_counter() - t0
    ideal_serial = N * LAT
    ideal_parallel = LAT
    verdict = "PARALLEL" if dt < ideal_serial * 0.6 else "SERIALIZED"
    print(
        f"{label:<28} {dt*1000:8.1f} ms   "
        f"(serial~{ideal_serial*1000:.0f}ms parallel~{ideal_parallel*1000:.0f}ms)  "
        f"{len(set(pids))} distinct backend(s)  -> {verdict}"
    )


async def main():
    print(f"N={N} queries, each SELECT pg_sleep({LAT}s)\n")
    await _time("serial / shared conn", serial_shared)
    await _time("gather / shared conn", gather_shared)
    await _time("gather / independent conns", gather_independent_nopool)
    await _time("gather / independent (pool)", gather_independent_pool)


if __name__ == "__main__":
    asyncio.run(main())
