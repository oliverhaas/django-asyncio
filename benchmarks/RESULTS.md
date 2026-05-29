# django-asyncio benchmark results

Generated: 2026-05-29 12:50

## Environment

- CPython 3.12.3 (Linux-6.17.0-29-generic-x86_64-with-glibc2.39)
- Granian 2.7.5, 1 worker process throughout
- Load generator: oha 1.14.0
- Database: postgres (PostgreSQL) 17.2 (Debian 17.2-1.pgdg120+1) (Docker, local)
- DB network latency injected with Toxiproxy (a `latency` toxic on the PostgreSQL proxy)

## Builds compared

- **sync1 / sync10 / sync100**: WSGI on Granian with a blocking-thread pool of 1 / 10 / 100. One thread serves one request at a time.
- **async**: this fork on ASGI, single async worker, native async ORM (no `sync_to_async` on the hot path).

`s2a` = number of `sync_to_async` calls recorded on the async request path during the run (0 means genuinely native).

## Results

### I/O-bound (view sleeps 50ms), concurrency 100

Headline async win: one async worker holds 100 slow requests; sync needs a thread each.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 19.9 | 5029.01 | 9526.82 | 9928.7 | 0.6 | 101.3 | 0 |  |
| sync10 | 199.0 | 502.4 | 503.2 | 815.82 | 4.5 | 103.4 | 0 |  |
| sync100 | 1979.9 | 50.36 | 50.55 | 50.88 | 43.6 | 135.8 | 0 |  |
| async | 1776.5 | 56.53 | 60.86 | 66.69 | 43.1 | 102.8 | 0 |  |

### CPU-bound (sha256 work), concurrency 100

Async should not win; confirms overhead is acceptable on a single core (GIL-bound).

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 552.8 | 180.7 | 183.67 | 186.47 | 102.1 | 101.2 | 0 |  |
| sync10 | 2215.1 | 44.73 | 48.76 | 52.62 | 532.2 | 107.4 | 0 |  |
| sync100 | 1700.1 | 58.6 | 72.47 | 79.11 | 461.6 | 146.4 | 0 |  |
| async | 523.2 | 189.92 | 201.71 | 214.19 | 103.0 | 104.0 | 0 |  |

### DB single-row (aget, pooled), concurrency 100

One indexed lookup per request against PostgreSQL via a connection pool.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 2382.0 | 41.83 | 43.4 | 45.21 | 89.5 | 114.5 | 0 |  |
| sync10 | 2233.0 | 44.02 | 52.98 | 56.37 | 127.0 | 118.6 | 0 |  |
| sync100 | 1786.7 | 55.37 | 63.83 | 68.53 | 125.4 | 147.5 | 0 |  |
| async | 1841.5 | 52.72 | 71.37 | 74.1 | 110.6 | 121.2 | 0 | 0 |

### DB heavy prefetch, per-request (concurrency 1, 5ms/query DB latency)

16 flat+nested prefetch lookups over ~20 tables, with 5ms network latency injected per query (Toxiproxy). At c=1 this isolates the within-request win: async runs the independent lookups in parallel on borrowed pooled connections; sync runs them sequentially.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 8.8 | 110.17 | 139.83 | 141.16 | 19.7 | 123.9 | 0 |  |
| sync10 | 8.8 | 110.35 | 140.4 | 142.12 | 20.1 | 124.1 | 0 |  |
| sync100 | 8.8 | 110.27 | 137.64 | 139.5 | 19.8 | 124.1 | 0 |  |
| async | 25.7 | 35.24 | 66.45 | 71.38 | 62.2 | 124.3 | 0 | 0 |

### DB heavy prefetch, concurrent (concurrency 50, 5ms/query DB latency)

Same workload under load with a 48-connection pool. Async is single-thread CPU-bound here, so throughput is close to sync-with-100-threads but with one thread and better tail latency.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 8.7 | 8301.41 | 11111.51 | 11331.69 | 20.6 | 126.3 | 0 |  |
| sync10 | 40.1 | 1248.96 | 2037.47 | 2436.68 | 106.1 | 137.2 | 0 |  |
| sync100 | 29.1 | 1730.71 | 3039.55 | 3411.37 | 109.5 | 183.3 | 0 |  |
| async | 36.1 | 1513.61 | 1741.0 | 1916.97 | 99.8 | 164.8 | 0 | 0 |

### DB heavy prefetch, no injected latency (concurrency 50)

Localhost DB (sub-ms queries): parallelizing prefetch saves nothing, so this shows the overhead of the parallel machinery when there is no latency to hide.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 46.1 | 1093.86 | 1570.22 | 2000.11 | 87.8 | 126.3 | 0 |  |
| sync10 | 38.9 | 1320.87 | 2057.27 | 2497.77 | 107.1 | 137.7 | 0 |  |
| sync100 | 29.2 | 1729.17 | 3031.34 | 3327.61 | 109.9 | 180.1 | 0 |  |
| async | 37.2 | 1472.36 | 1675.64 | 1921.12 | 100.3 | 168.2 | 0 | 0 |

## Notes

- The **db_heavy** scenario is what the parallel async prefetch was built for. Each request fetches a page of `Author` rows and prefetches 16 lookups spanning forward/reverse FK, forward/reverse one-to-one, M2M, and 2-3 levels of nesting. The number of prefetch queries is roughly constant (~17), so under per-query latency the sequential cost grows with the number of lookups while the parallel cost grows only with the depth of the tree.
- Parallel prefetch is **opportunistic**: a sub-query borrows a pooled connection only if one is already idle, runs there, and returns it; otherwise it runs on the connection the request already holds. It never grows the pool and never waits, so it cannot deadlock. The `db_heavy` pool is pre-warmed (min == max) so idle connections exist to borrow.
- Inside a transaction the prefetch runs sequentially on the transactional connection (an independent connection would not see uncommitted state).

Reproduce: `python benchmarks/run_matrix.py` (needs the postgres and toxiproxy containers; the harness starts toxiproxy automatically).
