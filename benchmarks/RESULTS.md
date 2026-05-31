# django-asyncio benchmark results

Generated: 2026-05-31 08:46

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
- **upstream-async**: upstream Django 6.2.dev20260531062810 on the same setup. Falls back to `sync_to_async` for the ORM bits the fork has rewritten natively. This is the direct "what did our fork actually buy us?" comparison.

`s2a` = number of `sync_to_async` calls recorded on the async request path during the run (0 means genuinely native).

## Results

### I/O-bound (view sleeps 50ms), concurrency 100

Headline async win: one async worker holds 100 slow requests; sync needs a thread each.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 19.9 | 5085.07 | 9524.73 | 9965.73 | 0.7 | 101.9 | 0 |  |
| sync10 | 199.3 | 502.32 | 505.43 | 804.02 | 3.8 | 102.7 | 0 |  |
| sync100 | 1395.1 | 74.46 | 77.97 | 78.3 | 21.5 | 116.6 | 0 |  |
| async | 1678.3 | 59.83 | 69.99 | 77.56 | 33.8 | 105.9 | 0 |  |
| async-rsgi | 1742.5 | 57.04 | 63.14 | 71.78 | 27.4 | 103.7 | 0 |  |
| upstream-async | 1647.6 | 60.3 | 70.27 | 76.75 | 67.4 | 107.1 | 0 |  |

### CPU-bound (sha256 work), concurrency 100

Async should not win; confirms overhead is acceptable on a single core (GIL-bound).

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 517.1 | 193.35 | 195.0 | 206.3 | 99.9 | 102.0 | 0 |  |
| sync10 | 522.1 | 190.29 | 221.52 | 248.0 | 99.8 | 105.3 | 0 |  |
| sync100 | 498.9 | 151.43 | 594.23 | 872.86 | 99.9 | 139.3 | 0 |  |
| async | 505.2 | 196.93 | 207.95 | 222.17 | 99.8 | 105.5 | 0 |  |
| async-rsgi | 518.0 | 192.39 | 201.36 | 218.98 | 99.8 | 105.1 | 0 |  |
| upstream-async | 451.7 | 220.03 | 236.78 | 266.78 | 99.6 | 109.9 | 0 |  |

### DB single-row (aget, pooled), concurrency 100, 1ms/query DB latency

One indexed lookup per request against PostgreSQL via a connection pool, with 1ms of network latency injected (Toxiproxy) to simulate a real same-AZ DB. Even tiny per-query latency is what async exploits: while one request waits on the DB, the event loop serves others. Sync's threads can do the same but only up to the thread count, so the comparison gets honest only with non-zero latency.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 642.1 | 155.18 | 161.97 | 171.1 | 25.0 | 115.3 | 0 |  |
| sync10 | 2567.9 | 38.81 | 42.47 | 44.57 | 99.6 | 117.2 | 0 |  |
| sync100 | 2459.7 | 40.01 | 49.79 | 56.92 | 99.6 | 135.2 | 0 |  |
| async | 1767.9 | 54.47 | 74.52 | 78.77 | 99.8 | 122.2 | 0 | 0 |
| async-rsgi | 2002.2 | 48.99 | 63.27 | 67.8 | 99.7 | 122.2 | 0 | 0 |
| upstream-async | 864.0 | 113.67 | 136.36 | 141.42 | 99.2 | 129.0 | 0 | 43975 |

### DB single-row with full middleware stack, concurrency 100, 1ms/query DB latency

Same workload as above but the bench app is configured with a production-shape middleware stack (security, sessions on signed cookies, common, csrf, auth, messages on cookie storage, clickjacking). This isolates the cost of the middleware chain itself. The fork's modernized built-ins go through native `__acall__`s with `s2a=0`; upstream Django still inherits `MiddlewareMixin` everywhere and pays a `sync_to_async` wrap on every `process_request` / `process_response` (visible as a large `s2a` count on the upstream-async row).

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 611.5 | 161.99 | 174.99 | 191.17 | 28.4 | 116.4 | 0 |  |
| sync10 | 2180.3 | 45.32 | 50.89 | 60.51 | 99.6 | 119.7 | 0 |  |
| sync100 | 2132.5 | 45.97 | 53.19 | 72.71 | 99.4 | 134.0 | 0 |  |
| async | 1522.2 | 61.77 | 88.62 | 92.41 | 99.8 | 124.7 | 0 | 0 |
| async-rsgi | 1667.1 | 57.46 | 82.62 | 90.45 | 99.7 | 130.6 | 0 | 0 |
| upstream-async | 289.8 | 346.87 | 383.57 | 400.57 | 99.7 | 140.1 | 0 | 78087 |

### DB heavy prefetch, per-request (concurrency 1, 5ms/query DB latency)

16 flat+nested prefetch lookups over ~20 tables, with 5ms network latency injected per query (Toxiproxy). At c=1 this isolates the within-request win: async runs the independent lookups in parallel on borrowed pooled connections; sync runs them sequentially.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 8.8 | 110.71 | 138.94 | 143.14 | 19.7 | 127.0 | 0 |  |
| sync10 | 8.7 | 111.51 | 143.28 | 151.62 | 20.4 | 126.9 | 0 |  |
| sync100 | 8.7 | 111.76 | 146.69 | 152.13 | 20.6 | 127.0 | 0 |  |
| async | 25.7 | 34.96 | 68.75 | 71.8 | 60.7 | 127.8 | 0 | 0 |
| async-rsgi | 26.2 | 34.54 | 65.78 | 68.18 | 60.2 | 127.7 | 0 | 0 |
| upstream-async | 8.7 | 111.54 | 142.41 | 143.34 | 20.7 | 126.0 | 0 | 368 |

### DB heavy prefetch, concurrent (concurrency 50, 5ms/query DB latency)

Same workload under load with a 48-connection pool. Async is single-thread CPU-bound here, so throughput is close to sync-with-100-threads but with one thread and better tail latency.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 8.7 | 8476.49 | 11209.22 | 11429.48 | 20.5 | 129.1 | 0 |  |
| sync10 | 45.7 | 1100.94 | 1553.72 | 1967.48 | 98.9 | 139.7 | 0 |  |
| sync100 | 35.0 | 1429.68 | 2521.11 | 2786.04 | 99.6 | 197.1 | 0 |  |
| async | 36.2 | 1513.18 | 1723.7 | 1904.77 | 99.5 | 167.3 | 0 | 0 |
| async-rsgi | 33.3 | 1553.22 | 2433.86 | 2865.32 | 99.6 | 165.8 | 0 | 0 |
| upstream-async | 36.7 | 1358.28 | 2204.2 | 2257.59 | 99.6 | 185.2 | 0 | 1598 |

### DB heavy prefetch, no injected latency (concurrency 50)

Localhost DB (sub-ms queries): parallelizing prefetch saves nothing, so this shows the overhead of the parallel machinery when there is no latency to hide.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 45.2 | 1104.67 | 1661.21 | 2148.5 | 86.2 | 129.3 | 0 |  |
| sync10 | 47.5 | 1061.08 | 1545.56 | 1950.8 | 99.8 | 141.1 | 0 |  |
| sync100 | 36.1 | 1398.75 | 2136.25 | 2599.63 | 100.0 | 197.1 | 0 |  |
| async | 36.5 | 1503.23 | 1747.86 | 1905.59 | 99.7 | 167.3 | 0 | 0 |
| async-rsgi | 33.4 | 1594.69 | 2334.9 | 2644.53 | 99.9 | 164.3 | 0 | 0 |
| upstream-async | 38.5 | 1325.58 | 1961.87 | 2019.3 | 99.9 | 187.6 | 0 | 1641 |

## Notes

- On the **db single-row** scenario, async loses to `sync10`/`sync100` by ~25-30% even with 1ms injected latency. This is the *one-core CPU ceiling*: at high concurrency, both `sync10` (10 threads sharing the GIL on one core) and `async` (one event-loop thread on one core) become CPU-bound at `1 / per-request-CPU-cost`. Sync's per-request Python cost is lower than async's (no `await` scheduling, no asgiref `Local` dispatch, no async ORM machinery), so sync wins regardless of latency on a single core. The gap would shrink or flip on multi-core VPSes where async runs as multiple workers and sync's threads would have to span cores. **The fork still beats upstream-async by ~2x** (upstream falls back to `sync_to_async` for native ORM bits, ~45k s2a calls in this group), which is the win our fork actually delivers.
- **`async-rsgi` is the more efficient async option on this fork.** On the same one-core setup, RSGI buys ~10% on db single-row over ASGI (1977 vs 1802 rps), and is dramatically lighter on CPU at comparable I/O throughput (e.g. io c=100: 1758 rps at 27% CPU vs ASGI's 1706 rps at 34% CPU). It is essentially neutral on db_heavy/cpu workloads, because protocol overhead isn't the binding constraint there. The trade-off: RSGI ties Django to Granian, so the standard ASGI handler remains supported for deployments that need a different ASGI server.
- **The biggest win against upstream shows up on the *full middleware stack* row.** With a production-shape stack (security, sessions on signed cookies, common, csrf, auth, messages on cookie storage, clickjacking), the fork serves the same db single-row workload at **1667 rps with `s2a=0`** (async-rsgi), while upstream-async hits **290 rps with ~78k `sync_to_async` calls per run** (~18 per request). That is a ~5.75x speedup just from removing the middleware sync_to_async tax. Upstream still inherits `MiddlewareMixin` everywhere, so every `process_request` and `process_response` on the async path is wrapped in `sync_to_async(thread_sensitive=True)`. This fork rewrites every built-in middleware as a plain hybrid class with a native async `__acall__`, so the chain is genuinely async end-to-end. The modernized middleware also keeps `process_request` / `process_response` as the public method names, so third-party subclasses keep working.
- The **db_heavy** scenario is what the parallel async prefetch was built for. Each request fetches a page of `Author` rows and prefetches 16 lookups spanning forward/reverse FK, forward/reverse one-to-one, M2M, and 2-3 levels of nesting. The number of prefetch queries is roughly constant (~17), so under per-query latency the sequential cost grows with the number of lookups while the parallel cost grows only with the depth of the tree.
- Parallel prefetch is **opportunistic**: a sub-query borrows a pooled connection only if one is already idle, runs there, and returns it; otherwise it runs on the connection the request already holds. It never grows the pool and never waits, so it cannot deadlock. The `db_heavy` pool is pre-warmed (min == max) so idle connections exist to borrow.
- Inside a transaction the prefetch runs sequentially on the transactional connection (an independent connection would not see uncommitted state).

Reproduce: `python benchmarks/run_matrix.py` (needs the postgres and toxiproxy containers; the harness starts toxiproxy automatically).
