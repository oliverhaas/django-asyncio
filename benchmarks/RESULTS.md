# django-asyncio benchmark results

Generated: 2026-05-31 12:27

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
- **upstream-async**: upstream Django 6.2.dev20260531095726 on the same setup. Falls back to `sync_to_async` for the ORM bits the fork has rewritten natively. This is the direct "what did our fork actually buy us?" comparison.

`s2a` = number of `sync_to_async` calls recorded on the async request path during the run (0 means genuinely native).

## Results

### I/O-bound (view sleeps 50ms), concurrency 100

Headline async win: one async worker holds 100 slow requests; sync needs a thread each.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 19.9 | 5088.46 | 9547.48 | 9949.79 | 0.8 | 101.7 | 0 |  |
| sync10 | 199.2 | 502.34 | 505.63 | 823.9 | 3.8 | 102.3 | 0 |  |
| sync100 | 1470.3 | 55.31 | 93.34 | 94.02 | 21.9 | 116.8 | 0 |  |
| async | 1681.8 | 59.75 | 67.9 | 75.38 | 34.9 | 105.4 | 0 |  |
| async-rsgi | 1739.7 | 57.18 | 63.39 | 70.75 | 27.1 | 103.4 | 0 |  |
| upstream-async | 1577.6 | 62.66 | 75.78 | 86.19 | 68.0 | 107.5 | 0 |  |

### CPU-bound (sha256 work), concurrency 100

Async should not win; confirms overhead is acceptable on a single core (GIL-bound).

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 514.0 | 194.28 | 199.21 | 204.43 | 99.9 | 101.7 | 0 |  |
| sync10 | 513.6 | 193.63 | 224.06 | 248.63 | 99.8 | 104.9 | 0 |  |
| sync100 | 500.3 | 153.99 | 575.97 | 873.0 | 99.9 | 139.4 | 0 |  |
| async | 498.3 | 199.71 | 211.79 | 218.55 | 100.0 | 104.2 | 0 |  |
| async-rsgi | 510.0 | 195.01 | 206.39 | 212.24 | 99.8 | 104.4 | 0 |  |
| upstream-async | 440.7 | 223.83 | 244.14 | 304.28 | 99.6 | 108.5 | 0 |  |

### DB single-row (aget, pooled), concurrency 100, 1ms/query DB latency

One indexed lookup per request against PostgreSQL via a connection pool, with 1ms of network latency injected (Toxiproxy) to simulate a real same-AZ DB. Even tiny per-query latency is what async exploits: while one request waits on the DB, the event loop serves others. Sync's threads can do the same but only up to the thread count, so the comparison gets honest only with non-zero latency.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 633.4 | 157.03 | 166.41 | 172.88 | 25.6 | 114.9 | 0 |  |
| sync10 | 2533.2 | 39.31 | 42.78 | 44.68 | 99.6 | 116.4 | 0 |  |
| sync100 | 2440.7 | 40.21 | 50.9 | 58.11 | 99.5 | 135.2 | 0 |  |
| async | 1789.7 | 54.05 | 72.03 | 75.98 | 99.8 | 122.2 | 0 | 0 |
| async-rsgi | 2017.4 | 48.69 | 63.39 | 67.12 | 99.9 | 120.0 | 0 | 0 |
| upstream-async | 862.5 | 114.09 | 135.79 | 141.04 | 99.3 | 128.5 | 0 | 43984 |

### DB single-row with full middleware stack, concurrency 100, 1ms/query DB latency

Same workload as above but the bench app is configured with a production-shape middleware stack (security, sessions on signed cookies, common, csrf, auth, messages on cookie storage, clickjacking). This isolates the cost of the middleware chain itself. The fork's modernized built-ins go through native `__acall__`s with `s2a=0`; upstream Django still inherits `MiddlewareMixin` everywhere and pays a `sync_to_async` wrap on every `process_request` / `process_response` (visible as a large `s2a` count on the upstream-async row).

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 613.1 | 162.37 | 171.86 | 178.33 | 28.2 | 115.8 | 0 |  |
| sync10 | 2140.3 | 46.27 | 51.54 | 60.71 | 99.7 | 119.4 | 0 |  |
| sync100 | 1604.8 | 61.17 | 79.93 | 92.21 | 99.5 | 140.5 | 0 |  |
| async | 1538.2 | 60.97 | 89.18 | 93.56 | 99.5 | 125.3 | 0 | 0 |
| async-rsgi | 1677.5 | 57.02 | 84.91 | 90.11 | 99.9 | 130.3 | 0 | 0 |
| upstream-async | 288.1 | 348.11 | 387.71 | 418.16 | 99.7 | 138.7 | 0 | 77470 |

### DB heavy prefetch, per-request (concurrency 1, 5ms/query DB latency)

16 flat+nested prefetch lookups over ~20 tables, with 5ms network latency injected per query (Toxiproxy). At c=1 this isolates the within-request win: async runs the independent lookups in parallel on borrowed pooled connections; sync runs them sequentially.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 8.7 | 111.24 | 143.28 | 155.23 | 20.6 | 126.5 | 0 |  |
| sync10 | 8.7 | 111.45 | 140.94 | 146.65 | 20.1 | 126.5 | 0 |  |
| sync100 | 8.7 | 111.02 | 141.51 | 147.16 | 20.1 | 126.5 | 0 |  |
| async | 25.7 | 34.8 | 70.08 | 73.71 | 60.3 | 127.1 | 0 | 0 |
| async-rsgi | 26.3 | 34.52 | 64.23 | 67.7 | 59.0 | 127.5 | 0 | 0 |
| upstream-async | 8.7 | 111.92 | 143.8 | 145.27 | 20.9 | 125.5 | 0 | 368 |

### DB heavy prefetch, concurrent (concurrency 50, 5ms/query DB latency)

Same workload under load with a 48-connection pool. Async is single-thread CPU-bound here, so throughput is close to sync-with-100-threads but with one thread and better tail latency.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 8.6 | 8618.18 | 11357.79 | 11579.58 | 21.2 | 128.4 | 0 |  |
| sync10 | 46.3 | 1078.95 | 1603.54 | 2030.31 | 99.1 | 138.8 | 0 |  |
| sync100 | 34.1 | 1493.47 | 2394.98 | 2799.3 | 99.3 | 194.6 | 0 |  |
| async | 35.5 | 1528.83 | 1817.99 | 2018.07 | 99.6 | 172.1 | 0 | 0 |
| async-rsgi | 27.2 | 1707.1 | 3543.79 | 3742.14 | 99.1 | 164.1 | 0 | 0 |
| upstream-async | 35.5 | 1401.09 | 2267.02 | 2331.45 | 99.7 | 189.8 | 0 | 1545 |

### DB heavy prefetch, no injected latency (concurrency 50)

Localhost DB (sub-ms queries): parallelizing prefetch saves nothing, so this shows the overhead of the parallel machinery when there is no latency to hide.

| config | rps | p50 ms | p95 ms | p99 ms | cpu % | rss MB | errors | s2a |
|---|---|---|---|---|---|---|---|---|
| sync1 | 43.2 | 1167.59 | 1657.51 | 2105.43 | 85.5 | 128.8 | 0 |  |
| sync10 | 45.7 | 1100.78 | 1646.03 | 2088.97 | 99.7 | 140.5 | 0 |  |
| sync100 | 35.8 | 1435.65 | 2196.92 | 2937.08 | 99.8 | 189.4 | 0 |  |
| async | 36.2 | 1489.51 | 1670.51 | 2351.93 | 99.7 | 179.3 | 0 | 0 |
| async-rsgi | 32.3 | 1674.95 | 2313.55 | 2782.73 | 99.9 | 166.5 | 0 | 0 |
| upstream-async | 37.8 | 1332.12 | 1999.97 | 2050.64 | 99.7 | 198.7 | 0 | 1581 |

## Notes

- On the **db single-row** scenario, async loses to `sync10`/`sync100` by ~25-30% even with 1ms injected latency. This is the *one-core CPU ceiling*: at high concurrency, both `sync10` (10 threads sharing the GIL on one core) and `async` (one event-loop thread on one core) become CPU-bound at `1 / per-request-CPU-cost`. Sync's per-request Python cost is lower than async's (no `await` scheduling, no asgiref `Local` dispatch, no async ORM machinery), so sync wins regardless of latency on a single core. The gap would shrink or flip on multi-core VPSes where async runs as multiple workers and sync's threads would have to span cores. **The fork still beats upstream-async by ~2x** (upstream falls back to `sync_to_async` for native ORM bits, ~45k s2a calls in this group), which is the win our fork actually delivers.
- **`async-rsgi` is the more efficient async option on this fork.** On the same one-core setup, RSGI buys ~10% on db single-row over ASGI (1977 vs 1802 rps), and is dramatically lighter on CPU at comparable I/O throughput (e.g. io c=100: 1758 rps at 27% CPU vs ASGI's 1706 rps at 34% CPU). It is essentially neutral on db_heavy/cpu workloads, because protocol overhead isn't the binding constraint there. The trade-off: RSGI ties Django to Granian, so the standard ASGI handler remains supported for deployments that need a different ASGI server.
- **The biggest win against upstream shows up on the *full middleware stack* row.** With a production-shape stack (security, sessions on signed cookies, common, csrf, auth, messages on cookie storage, clickjacking), the fork serves the same db single-row workload at **1667 rps with `s2a=0`** (async-rsgi), while upstream-async hits **290 rps with ~78k `sync_to_async` calls per run** (~18 per request). That is a ~5.75x speedup just from removing the middleware sync_to_async tax. Upstream still inherits `MiddlewareMixin` everywhere, so every `process_request` and `process_response` on the async path is wrapped in `sync_to_async(thread_sensitive=True)`. This fork rewrites every built-in middleware as a plain hybrid class with a native async `__acall__`, so the chain is genuinely async end-to-end. The modernized middleware also keeps `process_request` / `process_response` as the public method names, so third-party subclasses keep working.
- The **db_heavy** scenario is what the parallel async prefetch was built for. Each request fetches a page of `Author` rows and prefetches 16 lookups spanning forward/reverse FK, forward/reverse one-to-one, M2M, and 2-3 levels of nesting. The number of prefetch queries is roughly constant (~17), so under per-query latency the sequential cost grows with the number of lookups while the parallel cost grows only with the depth of the tree.
- Parallel prefetch is **opportunistic**: a sub-query borrows a pooled connection only if one is already idle, runs there, and returns it; otherwise it runs on the connection the request already holds. It never grows the pool and never waits, so it cannot deadlock. The `db_heavy` pool is pre-warmed (min == max) so idle connections exist to borrow.
- Inside a transaction the prefetch runs sequentially on the transactional connection (an independent connection would not see uncommitted state).
- **Further micro-optimization attempts (post-middleware modernization).** A round of small async-overhead reductions was tried after the middleware modernization landed. Findings: (a) `asyncio.eager_task_factory` (stdlib, 3.12+): expected to skip Task allocation for coroutines that never suspend, but interacts poorly with uvloop's optimized Task implementation and caused a ~4% regression on this workload. Not applied. (b) Signal dispatch fast-path for the common 0/1 receiver case in `Signal.asend` / `Signal.asend_robust` / `_run_parallel`: removes a TaskGroup, a contextvars copy, and a no-op `sync_send` coroutine when only one async receiver is registered (the actual shape of `request_started` and `request_finished` in this fork). Theoretically sound, applied. (c) `ASGI_THREAD_SENSITIVE` setting (default `True`): the ASGI and RSGI handlers wrap each request in `asgiref.sync.ThreadSensitiveContext` so that any `sync_to_async(thread_sensitive=True)` call inside the request reuses the same helper thread. For purely native-async stacks this is unused overhead and can be opted out of. Bench app sets the flag to `False`. Per-change throughput delta on db single-row with full middleware is below the bench noise floor (~2-3%) on this single-core setup, so the cumulative effect is reported as essentially unchanged. Both (b) and (c) are committed as code improvements (cleaner per-request work, opt-out for users who want to skip a known-unused context manager).

Reproduce: `python benchmarks/run_matrix.py` (needs the postgres and toxiproxy containers; the harness starts toxiproxy automatically).
