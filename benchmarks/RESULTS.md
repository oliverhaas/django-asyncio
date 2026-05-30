# django-asyncio benchmark results

Generated: 2026-05-30 09:54

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
- **upstream-async**: upstream Django 6.2.dev20260530055007 on the same setup. Falls back to `sync_to_async` for the ORM bits the fork has rewritten natively. This is the direct "what did our fork actually buy us?" comparison.

`s2a` = number of `sync_to_async` calls recorded on the async request path during the run (0 means genuinely native).

## Results

### I/O-bound (view sleeps 50ms), concurrency 100

Headline async win: one async worker holds 100 slow requests; sync needs a thread each.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 19.9 | 5028.31 | 9523.46 | 9902.59 | 0.5 | 103.2 | 0 |  |
| sync10 | 198.6 | 502.91 | 504.43 | 836.52 | 3.4 | 102.7 | 0 |  |
| sync100 | 1394.3 | 65.74 | 89.51 | 91.12 | 21.1 | 116.5 | 0 |  |
| async | 1692.2 | 59.0 | 69.8 | 75.99 | 33.4 | 106.0 | 0 |  |
| upstream-async | 1626.1 | 60.81 | 72.51 | 79.07 | 67.3 | 107.7 | 0 |  |

### CPU-bound (sha256 work), concurrency 100

Async should not win; confirms overhead is acceptable on a single core (GIL-bound).

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 520.5 | 192.16 | 194.9 | 207.91 | 99.9 | 102.0 | 0 |  |
| sync10 | 523.5 | 189.94 | 220.95 | 248.72 | 99.8 | 105.2 | 0 |  |
| sync100 | 505.4 | 148.44 | 575.51 | 832.31 | 100.0 | 137.1 | 0 |  |
| async | 507.0 | 196.07 | 208.5 | 217.28 | 100.0 | 104.9 | 0 |  |
| upstream-async | 451.2 | 220.01 | 240.95 | 276.4 | 99.7 | 107.8 | 0 |  |

### DB single-row (aget, pooled), concurrency 100, 1ms/query DB latency

One indexed lookup per request against PostgreSQL via a connection pool, with 1ms of network latency injected (Toxiproxy) to simulate a real same-AZ DB. Even tiny per-query latency is what async exploits: while one request waits on the DB, the event loop serves others. Sync's threads can do the same but only up to the thread count, so the comparison gets honest only with non-zero latency.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 639.3 | 156.0 | 160.81 | 178.89 | 25.3 | 115.1 | 0 |  |
| sync10 | 2517.5 | 39.59 | 43.38 | 45.73 | 99.5 | 116.7 | 0 |  |
| sync100 | 2409.5 | 40.68 | 51.56 | 59.53 | 99.7 | 135.4 | 0 |  |
| async | 1817.2 | 53.2 | 70.12 | 73.66 | 99.7 | 122.3 | 0 | 0 |
| upstream-async | 856.0 | 114.87 | 136.9 | 145.17 | 99.1 | 128.9 | 0 | 43553 |

### DB heavy prefetch, per-request (concurrency 1, 5ms/query DB latency)

16 flat+nested prefetch lookups over ~20 tables, with 5ms network latency injected per query (Toxiproxy). At c=1 this isolates the within-request win: async runs the independent lookups in parallel on borrowed pooled connections; sync runs them sequentially.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 8.8 | 110.46 | 139.26 | 146.54 | 19.9 | 126.7 | 0 |  |
| sync10 | 8.7 | 110.87 | 139.76 | 146.91 | 20.0 | 126.8 | 0 |  |
| sync100 | 8.7 | 111.12 | 142.1 | 151.83 | 20.2 | 126.8 | 0 |  |
| async | 25.7 | 34.82 | 69.91 | 73.47 | 60.7 | 127.5 | 0 | 0 |
| upstream-async | 8.7 | 111.34 | 138.97 | 141.56 | 20.4 | 126.1 | 0 | 368 |

### DB heavy prefetch, concurrent (concurrency 50, 5ms/query DB latency)

Same workload under load with a 48-connection pool. Async is single-thread CPU-bound here, so throughput is close to sync-with-100-threads but with one thread and better tail latency.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 8.7 | 8478.79 | 11195.9 | 11415.77 | 20.5 | 128.7 | 0 |  |
| sync10 | 47.0 | 1071.14 | 1506.45 | 1983.44 | 99.1 | 139.1 | 0 |  |
| sync100 | 34.7 | 1465.45 | 2357.1 | 2759.45 | 99.6 | 191.5 | 0 |  |
| async | 37.1 | 1461.24 | 1679.03 | 2225.01 | 99.6 | 174.2 | 0 | 0 |
| upstream-async | 36.4 | 1353.17 | 2091.81 | 2162.91 | 99.6 | 196.1 | 0 | 1590 |

### DB heavy prefetch, no injected latency (concurrency 50)

Localhost DB (sub-ms queries): parallelizing prefetch saves nothing, so this shows the overhead of the parallel machinery when there is no latency to hide.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 45.7 | 1105.2 | 1623.67 | 2070.89 | 86.5 | 129.0 | 0 |  |
| sync10 | 48.3 | 1039.85 | 1503.89 | 1904.15 | 99.9 | 141.8 | 0 |  |
| sync100 | 34.0 | 1510.62 | 2285.62 | 2749.27 | 99.6 | 194.4 | 0 |  |
| async | 36.5 | 1460.07 | 1792.22 | 1993.57 | 99.8 | 168.2 | 0 | 0 |
| upstream-async | 37.7 | 1317.78 | 2171.34 | 2212.33 | 99.7 | 188.4 | 0 | 1637 |

## Notes

- On the **db single-row** scenario, async loses to `sync10`/`sync100` by ~25-30% even with 1ms injected latency. This is the *one-core CPU ceiling*: at high concurrency, both `sync10` (10 threads sharing the GIL on one core) and `async` (one event-loop thread on one core) become CPU-bound at `1 / per-request-CPU-cost`. Sync's per-request Python cost is lower than async's (no `await` scheduling, no asgiref `Local` dispatch, no async ORM machinery), so sync wins regardless of latency on a single core. The gap would shrink or flip on multi-core VPSes where async runs as multiple workers and sync's threads would have to span cores. **The fork still beats upstream-async by ~2.1x** (upstream falls back to `sync_to_async` for native ORM bits, ~43k s2a calls in this group), which is the win our fork actually delivers.
- The **db_heavy** scenario is what the parallel async prefetch was built for. Each request fetches a page of `Author` rows and prefetches 16 lookups spanning forward/reverse FK, forward/reverse one-to-one, M2M, and 2-3 levels of nesting. The number of prefetch queries is roughly constant (~17), so under per-query latency the sequential cost grows with the number of lookups while the parallel cost grows only with the depth of the tree.
- Parallel prefetch is **opportunistic**: a sub-query borrows a pooled connection only if one is already idle, runs there, and returns it; otherwise it runs on the connection the request already holds. It never grows the pool and never waits, so it cannot deadlock. The `db_heavy` pool is pre-warmed (min == max) so idle connections exist to borrow.
- Inside a transaction the prefetch runs sequentially on the transactional connection (an independent connection would not see uncommitted state).

Reproduce: `python benchmarks/run_matrix.py` (needs the postgres and toxiproxy containers; the harness starts toxiproxy automatically).
