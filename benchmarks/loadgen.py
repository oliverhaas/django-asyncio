"""Self-contained async HTTP load generator + process resource sampler.

No external binary (wrk/oha) needed: a fixed number of asyncio workers
hammer a URL for a fixed duration over a shared httpx connection pool,
and a background thread samples CPU% and RSS of the server process tree
via psutil.
"""

import asyncio
import statistics
import threading
import time
from dataclasses import dataclass, field

import httpx
import psutil


@dataclass
class LoadResult:
    requests: int
    errors: int
    duration_s: float
    rps: float
    latencies_ms: list = field(repr=False)

    def percentile(self, p):
        if not self.latencies_ms:
            return float("nan")
        ordered = sorted(self.latencies_ms)
        k = max(0, min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1)))))
        return ordered[k]

    @property
    def p50(self):
        return self.percentile(50)

    @property
    def p95(self):
        return self.percentile(95)

    @property
    def p99(self):
        return self.percentile(99)


async def _worker(client, url, deadline, latencies, errors):
    while time.monotonic() < deadline:
        start = time.perf_counter()
        try:
            resp = await client.get(url)
            elapsed = (time.perf_counter() - start) * 1000
            if resp.status_code == 200:
                latencies.append(elapsed)
            else:
                errors[0] += 1
        except Exception:
            errors[0] += 1


async def run_load(url, *, concurrency, duration_s, warmup_s=2.0):
    """Drive `concurrency` workers against `url` for `duration_s` seconds."""
    limits = httpx.Limits(
        max_connections=concurrency, max_keepalive_connections=concurrency
    )
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        if warmup_s > 0:
            warm_deadline = time.monotonic() + warmup_s
            await asyncio.gather(
                *(
                    _worker(client, url, warm_deadline, [], [0])
                    for _ in range(concurrency)
                )
            )

        latencies = []
        errors = [0]
        start = time.monotonic()
        deadline = start + duration_s
        await asyncio.gather(
            *(
                _worker(client, url, deadline, latencies, errors)
                for _ in range(concurrency)
            )
        )
        actual = time.monotonic() - start

    total = len(latencies) + errors[0]
    return LoadResult(
        requests=total,
        errors=errors[0],
        duration_s=actual,
        rps=(total / actual) if actual > 0 else 0.0,
        latencies_ms=latencies,
    )


@dataclass
class ResourceSample:
    cpu_mean: float
    cpu_peak: float
    rss_mean_mb: float
    rss_peak_mb: float


class ProcessSampler:
    """Sample CPU% and RSS of a process tree on a background thread."""

    def __init__(self, pid, interval=0.1):
        self.proc = psutil.Process(pid)
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._cpu = []
        self._rss = []
        # Cache Process objects by pid. cpu_percent(None) measures CPU since
        # the previous call *on that same object*, so objects must be reused
        # across samples. Fresh objects always report 0.0.
        self._procs = {}

    def _tree(self):
        seen = {self.proc.pid: self.proc}
        try:
            for child in self.proc.children(recursive=True):
                seen.setdefault(child.pid, child)
        except psutil.Error:
            pass
        # Reuse previously-seen objects so cpu_percent accumulates.
        for pid, proc in seen.items():
            self._procs.setdefault(pid, proc)
        # Drop processes that have exited.
        live = {pid: p for pid, p in self._procs.items() if p.is_running()}
        self._procs = live
        return list(self._procs.values())

    def _run(self):
        # Prime cpu_percent on the initial tree to establish a baseline.
        for p in self._tree():
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass
        while not self._stop.wait(self.interval):
            cpu = 0.0
            rss = 0
            for p in self._tree():
                try:
                    cpu += p.cpu_percent(None)
                    rss += p.memory_info().rss
                except psutil.Error:
                    pass
            self._cpu.append(cpu)
            self._rss.append(rss)

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def result(self):
        mb = 1024 * 1024
        return ResourceSample(
            cpu_mean=statistics.mean(self._cpu) if self._cpu else 0.0,
            cpu_peak=max(self._cpu) if self._cpu else 0.0,
            rss_mean_mb=(statistics.mean(self._rss) / mb) if self._rss else 0.0,
            rss_peak_mb=(max(self._rss) / mb) if self._rss else 0.0,
        )
