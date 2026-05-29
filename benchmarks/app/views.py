"""Benchmark views: two scenarios, each in a sync and an async flavor.

I/O-bound: sleep to simulate a downstream call. The sync view blocks a
thread (time.sleep); the async view yields the event loop
(asyncio.sleep). This is where async should win under concurrency.

CPU-bound: a fixed chunk of sha256 hashing. Async cannot win here (the
work blocks regardless); the goal is to confirm async overhead is
acceptable, not a regression.
"""

import asyncio
import hashlib
import time

from django.conf import settings
from django.http import JsonResponse

_CPU_BUFFER = b"x" * (64 * 1024)


def _burn_cpu():
    rounds = settings.BENCH_CPU_ROUNDS
    digest = _CPU_BUFFER
    for _ in range(rounds):
        digest = hashlib.sha256(digest + _CPU_BUFFER).digest()
    return digest.hex()


def io_sync(request):
    time.sleep(settings.BENCH_IO_SLEEP)
    return JsonResponse({"scenario": "io", "mode": "sync"})


async def io_async(request):
    await asyncio.sleep(settings.BENCH_IO_SLEEP)
    return JsonResponse({"scenario": "io", "mode": "async"})


def cpu_sync(request):
    digest = _burn_cpu()
    return JsonResponse({"scenario": "cpu", "mode": "sync", "digest": digest[:16]})


async def cpu_async(request):
    digest = _burn_cpu()
    return JsonResponse({"scenario": "cpu", "mode": "async", "digest": digest[:16]})


def db_sync(request):
    from .models import Widget

    obj = Widget.objects.get(pk=1)
    return JsonResponse({"scenario": "db", "mode": "sync", "value": obj.value})


async def db_async(request):
    from .models import Widget

    obj = await Widget.objects.aget(pk=1)
    return JsonResponse({"scenario": "db", "mode": "async", "value": obj.value})


def _summarize_authors(authors):
    """Walk the whole prefetched graph (cache reads only) so the prefetch is
    actually materialized, and return a checksum that depends on every
    relation, so a missing/incorrect prefetch would change the response."""
    total = 0
    for a in authors:
        total += (
            len(a.followers.all())
            + len(a.tags.all())
            + len(a.awards.all())
            + len(a.addresses.all())
        )
        total += a.profile.avatar.id
        for c in a.contracts.all():
            total += c.agent.id
        for art in a.articles.all():
            total += art.category.id + len(art.comments.all())
        for b in a.books.all():
            total += b.publisher.id + len(b.genres.all())
            for rv in b.reviews.all():
                total += rv.reviewer.id + rv.rating
    return total


def db_heavy_sync(request):
    from .models import HEAVY_PREFETCH_LOOKUPS, Author

    authors = list(
        Author.objects.prefetch_related(*HEAVY_PREFETCH_LOOKUPS).order_by("pk")[
            : settings.BENCH_HEAVY_AUTHORS
        ]
    )
    return JsonResponse(
        {
            "scenario": "db_heavy",
            "mode": "sync",
            "authors": len(authors),
            "checksum": _summarize_authors(authors),
        }
    )


async def db_heavy_async(request):
    from .models import HEAVY_PREFETCH_LOOKUPS, Author

    qs = Author.objects.prefetch_related(*HEAVY_PREFETCH_LOOKUPS).order_by("pk")[
        : settings.BENCH_HEAVY_AUTHORS
    ]
    authors = [a async for a in qs]
    return JsonResponse(
        {
            "scenario": "db_heavy",
            "mode": "async",
            "authors": len(authors),
            "checksum": _summarize_authors(authors),
        }
    )
