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
