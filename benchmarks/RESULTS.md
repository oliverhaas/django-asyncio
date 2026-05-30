# django-asyncio benchmark results

Generated: 2026-05-30 07:41

## Environment

- CPython 3.12.3 (Linux-6.17.0-29-generic-x86_64-with-glibc2.39)
- Granian 2.7.5, 1 worker process throughout
- Load generator: oha 1.14.0
- Database: postgres (PostgreSQL) 17.2 (Debian 17.2-1.pgdg120+1) (Docker, local)
- DB network latency injected with Toxiproxy (a `latency` toxic on the PostgreSQL proxy)
- **Simulated 1-vCPU VPS**: the app server is pinned with `taskset` to a single core (cpu 0); the load generator is pinned to separate cores (cpu 1-8) so it cannot steal the server's core. This caps every build at one core of CPU, so `sync100`'s thread pool contends on one core instead of spreading across the host.

## Builds compared

- **sync1 / sync10 / sync100**: WSGI on Granian with a blocking-thread pool of 1 / 10 / 100. One thread serves one request at a time.
- **async**: this fork on ASGI, single async worker, native async ORM (no `sync_to_async` on the hot path).

`s2a` = number of `sync_to_async` calls recorded on the async request path during the run (0 means genuinely native).

## Results

### I/O-bound (view sleeps 50ms), concurrency 100

Headline async win: one async worker holds 100 slow requests; sync needs a thread each.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 19.9 | 5034.8 | 9539.35 | 9941.0 | 0.7 | 100.3 | 0 |  |
| sync10 | 198.6 | 503.11 | 506.35 | 827.6 | 3.4 | 101.1 | 0 |  |
| sync100 | 1375.2 | 72.87 | 82.56 | 83.89 | 20.8 | 112.3 | 0 |  |
| async | 1751.7 | 56.77 | 63.42 | 69.89 | 40.3 | 101.8 | 0 |  |

### CPU-bound (sha256 work), concurrency 100

Async should not win; confirms overhead is acceptable on a single core (GIL-bound).

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 513.6 | 194.1 | 201.65 | 213.21 | 99.8 | 98.8 | 0 |  |
| sync10 | 519.2 | 191.15 | 222.0 | 244.72 | 99.9 | 101.7 | 0 |  |
| sync100 | 502.4 | 148.41 | 583.97 | 888.1 | 99.9 | 136.3 | 0 |  |
| async | 498.4 | 199.49 | 211.45 | 224.51 | 99.7 | 102.0 | 0 |  |

### DB single-row (aget, pooled), concurrency 100

One indexed lookup per request against PostgreSQL via a connection pool.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 2360.5 | 42.28 | 44.01 | 45.73 | 88.5 | 112.0 | 0 |  |
| sync10 | 2626.4 | 37.93 | 42.21 | 44.45 | 99.8 | 113.8 | 0 |  |
| sync100 | 2643.5 | 37.77 | 45.91 | 50.49 | 99.9 | 124.1 | 0 |  |
| async | 1726.8 | 55.55 | 75.05 | 78.52 | 99.7 | 120.5 | 0 | 0 |

### DB heavy prefetch, per-request (concurrency 1, 5ms/query DB latency)

16 flat+nested prefetch lookups over ~20 tables, with 5ms network latency injected per query (Toxiproxy). At c=1 this isolates the within-request win: async runs the independent lookups in parallel on borrowed pooled connections; sync runs them sequentially.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 8.7 | 111.23 | 140.5 | 146.36 | 19.8 | 124.2 | 0 |  |
| sync10 | 8.7 | 110.94 | 139.41 | 143.21 | 19.7 | 123.7 | 0 |  |
| sync100 | 8.7 | 110.72 | 139.48 | 147.33 | 19.9 | 124.5 | 0 |  |
| async | 25.8 | 34.88 | 65.83 | 72.89 | 60.7 | 123.1 | 0 | 0 |

### DB heavy prefetch, concurrent (concurrency 50, 5ms/query DB latency)

Same workload under load with a 48-connection pool. Async is single-thread CPU-bound here, so throughput is close to sync-with-100-threads but with one thread and better tail latency.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 8.5 | 8815.49 | 11441.62 | 11662.03 | 21.3 | 125.6 | 0 |  |
| sync10 | 45.8 | 1078.45 | 1567.73 | 1997.72 | 99.2 | 136.7 | 0 |  |
| sync100 | 34.4 | 1463.54 | 2468.22 | 2781.23 | 99.5 | 187.8 | 0 |  |
| async | 34.2 | 1618.18 | 1894.91 | 1946.06 | 99.5 | 163.2 | 0 | 0 |

### DB heavy prefetch, no injected latency (concurrency 50)

Localhost DB (sub-ms queries): parallelizing prefetch saves nothing, so this shows the overhead of the parallel machinery when there is no latency to hide.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 44.8 | 1116.57 | 1663.0 | 2110.39 | 86.4 | 126.5 | 0 |  |
| sync10 | 44.7 | 1124.37 | 1688.27 | 2168.87 | 99.8 | 138.3 | 0 |  |
| sync100 | 34.4 | 1466.5 | 2295.96 | 2599.31 | 99.8 | 188.6 | 0 |  |
| async | 35.7 | 1507.99 | 1814.94 | 1865.63 | 99.7 | 165.6 | 0 | 0 |

## Notes

- The **db_heavy** scenario is what the parallel async prefetch was built for. Each request fetches a page of `Author` rows and prefetches 16 lookups spanning forward/reverse FK, forward/reverse one-to-one, M2M, and 2-3 levels of nesting. The number of prefetch queries is roughly constant (~17), so under per-query latency the sequential cost grows with the number of lookups while the parallel cost grows only with the depth of the tree.
- Parallel prefetch is **opportunistic**: a sub-query borrows a pooled connection only if one is already idle, runs there, and returns it; otherwise it runs on the connection the request already holds. It never grows the pool and never waits, so it cannot deadlock. The `db_heavy` pool is pre-warmed (min == max) so idle connections exist to borrow.
- Inside a transaction the prefetch runs sequentially on the transactional connection (an independent connection would not see uncommitted state).

Reproduce: `python benchmarks/run_matrix.py` (needs the postgres and toxiproxy containers; the harness starts toxiproxy automatically).
