# django-asyncio plan

Velocity-first fork of Django to finish async. Edit core directly instead of working around it from a sibling package.

## Status

Phases 1-6 are landed: the ORM *and* the request/response/signal lifecycle are genuinely async on PostgreSQL (psycopg 3), with no `sync_to_async` on the steady-state hot path.

- **Phase 1** connection layer: `BaseDatabaseWrapper` and the postgresql backend have a full async API (`aconnect`/`acursor`/`acommit`/savepoints/...), async connections close at the ASGI request boundary.
- **Phase 2** QuerySet: `aget`, `afirst`/`alast`, `aiterator`, `async for`, `values`/`values_list`, `acount`/`aexists`/`aaggregate`, `aupdate`, `abulk_create`/`abulk_update`, `aget_or_create`/`aupdate_or_create`, `ain_bulk`, `aearliest`/`alatest` all run natively.
- **Phase 3** Model: `asave`/`acreate`/`arefresh_from_db`/`adelete` native, including an async deletion `Collector` (CASCADE/SET_NULL/PROTECT, `m2m_changed`/`pre_delete`/`post_delete` via `asend`).
- **Phase 4** related managers: reverse FK and M2M `aadd`/`aremove`/`aclear`/`aset`/`acreate`/`aget_or_create`/`aupdate_or_create` native.
- **Phase 5** `transaction.atomic` is async-aware (`async with atomic()`), with savepoints and nesting; native multi-table-inheritance saves work.
- **Phase 6** request lifecycle: `Signal.connect()` / `@receiver` take `run_sync` / `run_async` flags; `asend` only dispatches `run_async` receivers and `send` only `run_sync` ones (defaults True/True, so existing receivers are unchanged). Django's built-in lifecycle receivers are split into sync-only + async-only pairs (`close_old_connections`/`aclose_old_connections`, `reset_queries`/`areset_queries`, `close_caches`/`aclose_caches`, `reset_urlconf`/`areset_urlconf`), and `HttpResponseBase.aclose()` drives `request_finished` under `asend`. So `request_started`/`request_finished` run natively on the event loop with zero `sync_to_async` hops.

Every async ORM method is gated by `_use_native_async()`: it runs the native driver only on an async-capable backend AND when not under an `async_to_sync` wrapper (TestCase, sync-calling-async); otherwise it falls back to the existing `sync_to_async` path. So SQLite and the full sync suite (19563 tests) are unchanged, and the existing async ORM tests pass on real PostgreSQL.

### Benchmark results (fork vs upstream)

Measured with `oha` (a multi-threaded load tool; see the load-generator note below), Granian 1 worker, 100 concurrent connections, CPython 3.12, psycopg pool, against the `pre-asyncio-fork` upstream commit vs this fork. Async config:

| scenario | fork rps | upstream rps | speedup | fork `sync_to_async` | upstream `sync_to_async` |
|---|---|---|---|---|---|
| io (`asyncio.sleep` 50ms) | ~1461 | ~847 | 1.7x | ~0 | ~29k |
| cpu (sha256) | ~104 | ~84 | 1.25x | ~0 | ~2.8k |
| db (`aget`, pooled) | ~776 | ~362 | 2.15x | ~0 | ~18.6k |

The fork wins on every async scenario and uses less CPU and RAM, because upstream makes ~2.3-2.5 `sync_to_async` calls per request (lifecycle hops + `aget` wrapping the sync ORM) that serialize through asgiref's thread-sensitive executor, while the fork makes zero. The io win is purely the Phase 6 lifecycle work (no ORM); the db win is the native async ORM. On a single worker, sync (Granian thread pool) still wins raw throughput on cheap CPU-bound work (the GIL makes async one-core-bound there); async's advantage is I/O-bound concurrency without a thread per request.

**Load-generator caveat (important):** an earlier "async is capped at ~108 rps / the request lifecycle is the bottleneck" finding was wrong. It was an artifact of the original single-process asyncio+httpx load generator, whose own event loop throttled to ~105 rps and starved the server. A real load tool (`oha`) shows the async server sustaining ~1400+ rps. The harness now defaults to `oha` (`run.py --loadgen`), falling back to the httpx generator with a printed warning only when `oha` is absent.

## Why this fork exists

[django-async-backend](https://github.com/oliverhaas/django-async-backend) was the previous attempt. It works, but every change is a workaround for Django's sync internals: shadow modules, subclasses of private classes, monkey-patching, parallel `AsyncManager` / `AsyncModel` hierarchies. That tax is what kept async coverage incomplete.

This fork removes the tax. We change Django's code wherever we need to.

## Non-goals (explicit)

- Upstream-ability of individual commits. Will be considered later, once the shape is clear.
- Backwards compatibility shims. If a sync API needs to change to make async work cleanly, change it.
- Renames, refactors, or cleanups unrelated to async.
- Anything outside the async story (templates, admin UI, forms, etc.).

## Source material

Port the work from `~/e1+/django-async-backend`. Roughly 5400 lines across:

- `django_async_backend/db/models/base.py` (AsyncModel mixin, asave/adelete/arefresh)
- `django_async_backend/db/models/query.py` (async QuerySet, ~2600 lines)
- `django_async_backend/db/models/related_managers.py` (async reverse FK / M2M managers)
- `django_async_backend/db/models/deletion.py` (async Collector, CASCADE/SET_NULL/PROTECT)
- `django_async_backend/db/models/manager.py` (AsyncManager)
- `django_async_backend/db/backends/base/base.py` (async connection lifecycle)
- `django_async_backend/db/backends/postgresql/async_base.py` (psycopg async wrapper)
- `django_async_backend/db/transaction.py` (`async_atomic`, savepoints, cross-task detection)
- `django_async_backend/middleware.py` (`close_async_connections`)
- Tests under `tests/` (ported aggregation, annotation, select_for_update, dates, prefetch_related)

The package version is the source of truth for what already works. Anything not in there is genuinely new work.

## Strategy

Port in layers, bottom up. Each layer should be working and tested before moving up.

### Phase 1: connection layer

Goal: async connections are first-class citizens in `django.db.backends.base.BaseDatabaseWrapper` and `django.db.backends.postgresql.base.DatabaseWrapper`, not a separate subclass.

- Merge async connection state into `BaseDatabaseWrapper` (no more `AsyncBaseDatabaseWrapper`).
- Merge async psycopg wrapper into the standard `postgresql` backend.
- Make `connection.aclose()`, `connection.acursor()`, async pool ownership work out of the box.
- Fold `close_async_connections` into the request lifecycle so it just works under ASGI without manual middleware.

### Phase 2: ORM execution

Goal: `QuerySet` itself runs queries either sync or async, no `AsyncQuerySet` shadow.

- Push async execution paths into `django.db.models.query.QuerySet`.
- Push async compilation paths into `django.db.models.sql.compiler.SQLCompiler`.
- All `aXxx()` methods live on the real `QuerySet` / `Manager`.

### Phase 3: model layer

Goal: every `Model` has working `asave` / `adelete` / `arefresh_from_db` and the right signal dispatch, with no `AsyncModel` mixin requirement.

- Move async instance methods onto `django.db.models.base.Model`.
- Switch deletion `Collector` to support both sync and async traversal in one class.
- Auto-generated `aget_next_by_FOO` / `aget_previous_by_FOO` for date fields.

### Phase 4: related managers

Goal: reverse FK and M2M managers expose async methods without separate classes.

- Add `aadd`, `aremove`, `aclear`, `aset`, `acreate`, `aget_or_create`, `aupdate_or_create` to `ForeignRelatedObjectsDescriptor` / `ManyRelatedManager` directly.
- Async `m2m_changed` dispatch.

### Phase 5: transactions

Goal: `transaction.atomic` works in both sync and async contexts. No separate `async_atomic`.

- Make `Atomic` async-aware (detect calling context).
- Keep cross-task transaction detection from the package.
- Savepoint support in async paths.

### Phase 6: gap-filling

Things not yet in the package, in priority order:

- Async signal dispatch consistency across the ORM (audit every `send()` call in the model layer).
- Async cache backend integration with the ORM (`select_related` cache, prefetch cache).
- Async migrations runner (or at least async schema introspection).
- Async test infrastructure (`AsyncTestCase` improvements, transaction rollback in async).
- SQLite async backend (currently postgresql-only).

## Tests

Use Django's own test suite as the harness. Each phase ports the relevant async test files from `django-async-backend/tests/` and adapts them to live under `tests/` in Django's tree.

CI: keep Django's existing test setup. Add an async-postgres job that runs the new tests against a real Postgres (testcontainers).

## Benchmarks

The fork is velocity-first but the *reason* anyone cares is throughput under load. We need a benchmark harness that proves async actually wins on I/O-bound workloads and doesn't regress on CPU-bound ones, and that our fork at least matches official async Django.

### Configurations

Three Django builds, all served by Granian:

- **sync**: upstream Django on WSGI, with Granian's thread pool sized 1 / 10 / 100.
- **async-official**: upstream Django on ASGI, single async worker.
- **async-fork**: this fork on ASGI, single async worker.

Five runs per scenario (sync × 3 thread counts + 2 async builds).

### Scenarios

- **I/O-bound**: view sleeps for ~50 ms to simulate a downstream call. Sync views use `time.sleep(0.05)`; async views use `await asyncio.sleep(0.05)`. This is the headline async win. Async should hold ~100 concurrent slow requests on a single thread, sync needs 100 threads to do the same.
- **CPU-bound**: no sleep. View does a fixed chunk of work (e.g. hash a ~64 KB payload, render a small template, or run a tight Python loop calibrated to ~5 ms wall time) and returns a JSON response. Async should *not* win here. We want to confirm the overhead is acceptable.

### Load

- Client: a single HTTP load generator (`wrk` or `oha`) on the same host, 100 concurrent connections, fixed duration (e.g. 30 s) per run with a short warmup.
- Granian config: 1 worker process throughout. We are measuring per-worker behaviour, not horizontal scaling. Threads vary only for the sync builds.

### Metrics

Captured per run:

- requests/sec, p50 / p95 / p99 latency (from the load tool).
- CPU%, sampled (e.g. `psutil` at 100 ms), peak and mean over the run window.
- RSS memory, peak and mean.
- Errors / timeouts.

Output: a single CSV plus a small markdown summary table per scenario. Keep the harness in `benchmarks/` at the repo root, runnable as `python benchmarks/run.py`. Results land in `benchmarks/results/<date>/`.

### Full-async verification

A benchmark of "async Django" that secretly wraps things in `sync_to_async` is measuring the wrong thing. Before each async-build run, the harness asserts there is no `sync_to_async` call active on the request hot path. Concretely:

- Monkey-patch `asgiref.sync.sync_to_async` (and `SyncToAsync.__call__`) during the run to record every call site with a short stack frame; abort if any are recorded under our code paths.
- Allowlist only the calls we *know* are upstream-of-our-fork and not yet ported (each entry should be tied to a phase that will remove it).
- Same check during the ORM test suite: any test marked `@requires_full_async` fails if `sync_to_async` runs underneath it.

The goal of the fork is full async on the hot path. The benchmark is the proof.

### When to run

- After Phase 1 lands (sanity baseline).
- After Phase 2 lands (ORM execution is the most likely place to regress).
- After Phase 5 lands (transactions add overhead per request).
- Before declaring the fork "done."

## Open questions (deferred)

- Distribution: keep as a long-lived fork, slim down into a leaner [django-async-backend] release, or land changes upstream piece by piece. Defer until phases 1 through 5 are working.
- Free-threading interaction: how does this play with the [django-freethreading-research] work. Defer until phase 3.
- ORM-level cancellation semantics for `asyncio.CancelledError` during in-flight queries. Defer until phase 1 lands.

## First concrete steps

1. Sync `main` with upstream and tag the starting point (`pre-asyncio-fork`).
2. Set up local dev environment for the Django test suite (postgres via testcontainers).
3. Phase 1, commit 1: merge `AsyncBaseDatabaseWrapper` connection state into `BaseDatabaseWrapper`.
