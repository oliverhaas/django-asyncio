# django-asyncio plan

Velocity-first fork of Django to finish async. Edit core directly instead of working around it from a sibling package.

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

## Open questions (deferred)

- Distribution: keep as a long-lived fork, slim down into a leaner [django-async-backend] release, or land changes upstream piece by piece. Defer until phases 1 through 5 are working.
- Free-threading interaction: how does this play with the [django-freethreading-research] work. Defer until phase 3.
- ORM-level cancellation semantics for `asyncio.CancelledError` during in-flight queries. Defer until phase 1 lands.

## First concrete steps

1. Sync `main` with upstream and tag the starting point (`pre-asyncio-fork`).
2. Set up local dev environment for the Django test suite (postgres via testcontainers).
3. Phase 1, commit 1: merge `AsyncBaseDatabaseWrapper` connection state into `BaseDatabaseWrapper`.
