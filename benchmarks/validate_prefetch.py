#!/usr/bin/env python
"""Validate native async prefetch against the sync implementation.

Runs the same prefetch_related() over the db_heavy graph three ways and
asserts the prefetched graphs are byte-for-byte identical:

  * sync                       -> reference result
  * async, no pool             -> native, sequential fan-out (parallel=False)
  * async, pooled              -> native, parallel fan-out over independent
                                  pooled connections (parallel=True)

Both async runs must make ZERO sync_to_async calls (i.e. ran natively, not
via the thread-pool fallback). A relation the async path failed to cache would
hit the sync ORM during serialization and raise SynchronousOnlyOperation.

Run:  .venv/bin/python benchmarks/validate_prefetch.py
"""

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from app import verify_full_async  # noqa: E402

verify_full_async.install()

from django.conf import settings  # noqa: E402

_PG = {
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
_POOLED = {**_PG, "OPTIONS": {"pool": {"min_size": 2, "max_size": 8}}}

settings.configure(
    DEBUG=False,
    DATABASES={"default": _PG, "pooled": _POOLED},
    INSTALLED_APPS=["app"],
    USE_TZ=True,
)

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.db import connections  # noqa: E402
from django.db.models import Prefetch  # noqa: E402

from app.models import HEAVY_PREFETCH_LOOKUPS, Author, Review  # noqa: E402
from app.seed import seed_heavy  # noqa: E402

# A mix of plain string lookups plus a Prefetch() with a custom queryset, to
# exercise the Prefetch path (custom queryset on a nested relation).
LOOKUPS = HEAVY_PREFETCH_LOOKUPS + [
    Prefetch(
        "books__reviews",
        queryset=Review.objects.filter(rating__gte=2).order_by("pk"),
        to_attr="good_reviews",
    ),
]


def serialize(author):
    books = list(author.books.all())
    articles = list(author.articles.all())
    return {
        "name": author.name,
        "followers": sorted(f.id for f in author.followers.all()),
        "tags": sorted(t.id for t in author.tags.all()),
        "awards": sorted(a.id for a in author.awards.all()),
        "addresses": sorted(a.id for a in author.addresses.all()),
        "profile": author.profile.id,
        "profile.avatar": author.profile.avatar.id,
        "contracts": sorted(c.id for c in author.contracts.all()),
        "contracts.agent": sorted(c.agent.id for c in author.contracts.all()),
        "articles": sorted(a.id for a in articles),
        "articles.category": sorted(a.category.id for a in articles),
        "articles.comments": sorted(c.id for a in articles for c in a.comments.all()),
        "books": sorted(b.id for b in books),
        "books.publisher": sorted(b.publisher.id for b in books),
        "books.genres": sorted(g.id for b in books for g in b.genres.all()),
        "books.reviews": sorted(rv.id for b in books for rv in b.reviews.all()),
        "books.reviews.reviewer": sorted(
            rv.reviewer.id for b in books for rv in b.reviews.all()
        ),
        "books.good_reviews": sorted(
            rv.id for b in books for rv in b.good_reviews
        ),
    }


def sync_fetch():
    qs = Author.objects.prefetch_related(*LOOKUPS).order_by("pk")
    return [serialize(a) for a in qs]


async def async_fetch(using):
    qs = Author.objects.using(using).prefetch_related(*LOOKUPS).order_by("pk")
    await qs._afetch_all()
    result = [serialize(a) for a in qs._result_cache]
    # Close the pool inside the loop so its background maintenance task doesn't
    # keep asyncio.run() from shutting down cleanly. (A long-lived server closes
    # the pool on shutdown instead.)
    if getattr(connections[using], "async_pool", None) is not None:
        await connections[using].aclose_pool()
    return result


def _check(label, reference, result, s2a_calls):
    ok = True
    if len(reference) != len(result):
        ok = False
        print(f"[FAIL] {label}: count sync={len(reference)} got={len(result)}")
    for i, (s, a) in enumerate(zip(reference, result)):
        if s != a:
            ok = False
            diff = {k: (s.get(k), a.get(k)) for k in s if s.get(k) != a.get(k)}
            print(f"[FAIL] {label}: author {i} mismatch: {diff}")
    if s2a_calls:
        ok = False
        print(f"[FAIL] {label}: {len(s2a_calls)} sync_to_async call(s):")
        for c in s2a_calls:
            print(f"        {c}")
    if ok:
        print(f"[OK] {label}: {len(result)} authors match sync, 0 sync_to_async.")
    return ok


def main():
    call_command("migrate", run_syncdb=True, verbosity=0)
    seed_heavy(n_authors=20)

    reference = sync_fetch()

    ok = True
    for label, using in [("async sequential (no pool)", "default"),
                         ("async parallel (pooled)", "pooled")]:
        verify_full_async.reset()
        result = asyncio.run(async_fetch(using))
        ok &= _check(label, reference, result, verify_full_async.report())

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
