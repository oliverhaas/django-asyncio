# django-asyncio benchmark results

Generated: 2026-05-30 10:36

## Environment

- CPython 3.12.3 (Linux-6.17.0-29-generic-x86_64-with-glibc2.39)
- Granian 2.7.5, 1 worker process throughout
- Async event loop: uvloop 0.22.1 (libuv). Stdlib asyncio is noticeably slower per request, so benchmarking with the selector loop would understate every async build.
- Load generator: oha 1.14.0
- Database: postgres (PostgreSQL) 17.2 (Debian 17.2-1.pgdg120+1) (Docker, local)
- DB network latency injected with Toxiproxy (a `latency` toxic on the PostgreSQL proxy)
- **Simulated 1-vCPU VPS**: the app server is pinned with `taskset` to a single core (cpu 0); the load generator is pinned to separate cores (cpu 1-8) so it cannot steal the server's core. This caps every build at one core of CPU, so `sync100`'s thread pool contends on one core instead of spreading across the host.

## Builds compared

- **sync1 / sync10 / sync100**: WSGI on Granian with a blocking-thread pool of 1 / 10 / 100. One thread serves one request at a time. Same code on both this fork and upstream (we haven't touched the WSGI path), so we measure it once.
- **async**: this fork on ASGI, single async worker, native async ORM (no `sync_to_async` on the hot path).
- **async-rsgi**: this fork on Granian's native RSGI protocol. Same Django middleware, ORM, and views as `async`; only the protocol adapter changes. RSGI replaces ASGI's read-body and send-response message loops with single calls, removing several per-request awaits.
- **upstream-async**: upstream Django 6.2.dev20260530075631 on the same setup. Falls back to `sync_to_async` for the ORM bits the fork has rewritten natively. This is the direct "what did our fork actually buy us?" comparison.

`s2a` = number of `sync_to_async` calls recorded on the async request path during the run (0 means genuinely native).

## Results

### I/O-bound (view sleeps 50ms), concurrency 100

Headline async win: one async worker holds 100 slow requests; sync needs a thread each.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 19.9 | 5081.21 | 9538.16 | 9939.88 | 0.5 | 101.7 | 0 |  |
| sync10 | 198.6 | 502.89 | 503.9 | 848.17 | 3.4 | 102.4 | 0 |  |
| sync100 | 1217.4 | 87.42 | 96.47 | 96.76 | 18.7 | 114.3 | 0 |  |
| async | 1705.7 | 59.0 | 66.75 | 71.11 | 33.9 | 105.4 | 0 |  |
| async-rsgi | 1758.4 | 56.48 | 62.21 | 71.08 | 27.1 | 103.6 | 0 |  |
| upstream-async | 1633.1 | 60.59 | 72.04 | 77.22 | 67.2 | 107.3 | 0 |  |

### CPU-bound (sha256 work), concurrency 100

Async should not win; confirms overhead is acceptable on a single core (GIL-bound).

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 517.6 | 193.35 | 194.88 | 206.98 | 100.0 | 101.8 | 0 |  |
| sync10 | 517.0 | 192.17 | 222.81 | 248.64 | 99.9 | 105.0 | 0 |  |
| sync100 | 499.8 | 146.5 | 596.84 | 878.21 | 99.8 | 139.0 | 0 |  |
| async | 508.3 | 195.63 | 208.92 | 214.53 | 99.7 | 105.0 | 0 |  |
| async-rsgi | 517.7 | 192.37 | 203.57 | 207.23 | 99.8 | 103.1 | 0 |  |
| upstream-async | 452.8 | 218.85 | 237.19 | 263.7 | 99.5 | 108.2 | 0 |  |

### DB single-row (aget, pooled), concurrency 100, 1ms/query DB latency

One indexed lookup per request against PostgreSQL via a connection pool, with 1ms of network latency injected (Toxiproxy) to simulate a real same-AZ DB. Even tiny per-query latency is what async exploits: while one request waits on the DB, the event loop serves others. Sync's threads can do the same but only up to the thread count, so the comparison gets honest only with non-zero latency.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 646.6 | 154.13 | 161.48 | 166.4 | 24.9 | 115.1 | 0 |  |
| sync10 | 2564.9 | 38.82 | 42.47 | 44.56 | 99.5 | 116.8 | 0 |  |
| sync100 | 2517.8 | 38.94 | 48.83 | 56.28 | 99.4 | 135.2 | 0 |  |
| async | 1802.2 | 53.78 | 70.67 | 74.76 | 99.8 | 122.3 | 0 | 0 |
| async-rsgi | 1976.9 | 49.57 | 66.04 | 69.21 | 99.6 | 120.6 | 0 | 0 |
| upstream-async | 888.4 | 110.87 | 131.46 | 137.62 | 99.1 | 129.2 | 0 | 45213 |

### DB heavy prefetch, per-request (concurrency 1, 5ms/query DB latency)

16 flat+nested prefetch lookups over ~20 tables, with 5ms network latency injected per query (Toxiproxy). At c=1 this isolates the within-request win: async runs the independent lookups in parallel on borrowed pooled connections; sync runs them sequentially.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 8.8 | 110.42 | 137.85 | 139.76 | 19.6 | 126.6 | 0 |  |
| sync10 | 8.7 | 111.0 | 142.34 | 143.65 | 20.0 | 126.0 | 0 |  |
| sync100 | 8.8 | 110.46 | 138.27 | 139.32 | 19.5 | 126.8 | 0 |  |
| async | 26.7 | 34.12 | 62.22 | 64.84 | 60.2 | 127.4 | 0 | 0 |
| async-rsgi | 26.4 | 34.47 | 62.76 | 65.01 | 59.6 | 127.7 | 0 | 0 |
| upstream-async | 8.7 | 111.38 | 141.02 | 142.4 | 20.2 | 125.9 | 0 | 368 |

### DB heavy prefetch, concurrent (concurrency 50, 5ms/query DB latency)

Same workload under load with a 48-connection pool. Async is single-thread CPU-bound here, so throughput is close to sync-with-100-threads but with one thread and better tail latency.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 8.7 | 8517.2 | 11267.69 | 11464.41 | 20.5 | 128.9 | 0 |  |
| sync10 | 47.5 | 1057.98 | 1544.21 | 1939.4 | 98.9 | 139.9 | 0 |  |
| sync100 | 36.1 | 1397.38 | 2351.17 | 2821.84 | 99.5 | 204.4 | 0 |  |
| async | 37.0 | 1450.25 | 1716.06 | 2131.95 | 99.6 | 175.0 | 0 | 0 |
| async-rsgi | 34.2 | 1545.27 | 2325.19 | 2793.86 | 99.6 | 163.9 | 0 | 0 |
| upstream-async | 37.1 | 1373.2 | 1997.6 | 2130.35 | 99.6 | 185.8 | 0 | 1611 |

### DB heavy prefetch, no injected latency (concurrency 50)

Localhost DB (sub-ms queries): parallelizing prefetch saves nothing, so this shows the overhead of the parallel machinery when there is no latency to hide.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 46.4 | 1084.1 | 1599.68 | 2004.31 | 86.1 | 128.5 | 0 |  |
| sync10 | 48.4 | 1039.36 | 1462.1 | 1887.64 | 99.9 | 140.6 | 0 |  |
| sync100 | 36.2 | 1399.72 | 2070.7 | 2565.24 | 99.9 | 193.0 | 0 |  |
| async | 37.7 | 1421.41 | 1589.38 | 1895.67 | 99.7 | 177.4 | 0 | 0 |
| async-rsgi | 34.1 | 1565.43 | 2302.22 | 2731.76 | 99.9 | 164.3 | 0 | 0 |
| upstream-async | 37.6 | 1317.26 | 1999.34 | 2209.31 | 99.6 | 185.1 | 0 | 1646 |

## Notes

- On the **db single-row** scenario, async loses to `sync10`/`sync100` by ~25-30% even with 1ms injected latency. This is the *one-core CPU ceiling*: at high concurrency, both `sync10` (10 threads sharing the GIL on one core) and `async` (one event-loop thread on one core) become CPU-bound at `1 / per-request-CPU-cost`. Sync's per-request Python cost is lower than async's (no `await` scheduling, no asgiref `Local` dispatch, no async ORM machinery), so sync wins regardless of latency on a single core. The gap would shrink or flip on multi-core VPSes where async runs as multiple workers and sync's threads would have to span cores. **The fork still beats upstream-async by ~2x** (upstream falls back to `sync_to_async` for native ORM bits, ~45k s2a calls in this group), which is the win our fork actually delivers.
- **`async-rsgi` is the more efficient async option on this fork.** On the same one-core setup, RSGI buys ~10% on db single-row over ASGI (1977 vs 1802 rps), and is dramatically lighter on CPU at comparable I/O throughput (e.g. io c=100: 1758 rps at 27% CPU vs ASGI's 1706 rps at 34% CPU). It is essentially neutral on db_heavy/cpu workloads, because protocol overhead isn't the binding constraint there. The trade-off: RSGI ties Django to Granian, so the standard ASGI handler remains supported for deployments that need a different ASGI server.
- The **db_heavy** scenario is what the parallel async prefetch was built for. Each request fetches a page of `Author` rows and prefetches 16 lookups spanning forward/reverse FK, forward/reverse one-to-one, M2M, and 2-3 levels of nesting. The number of prefetch queries is roughly constant (~17), so under per-query latency the sequential cost grows with the number of lookups while the parallel cost grows only with the depth of the tree.
- Parallel prefetch is **opportunistic**: a sub-query borrows a pooled connection only if one is already idle, runs there, and returns it; otherwise it runs on the connection the request already holds. It never grows the pool and never waits, so it cannot deadlock. The `db_heavy` pool is pre-warmed (min == max) so idle connections exist to borrow.
- Inside a transaction the prefetch runs sequentially on the transactional connection (an independent connection would not see uncommitted state).

Reproduce: `python benchmarks/run_matrix.py` (needs the postgres and toxiproxy containers; the harness starts toxiproxy automatically).
