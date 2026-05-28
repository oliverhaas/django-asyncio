"""sync_to_async tripwire for the async benchmark builds.

The async builds are only meaningful if the request hot path is genuinely
async. asgiref's `sync_to_async` is the seam where async code falls back
to a thread pool; if it fires during an async request, something on the
path is still synchronous.

When installed, this patches asgiref so every `sync_to_async` *call*
(i.e. actually invoking the wrapped callable) records the call site. The
ASGI server exposes the tally; run.py reads it after a load run and fails
the async build if anything fired on the hot path.

Install by importing and calling install() before the app handles
traffic (see asgi.py, gated on BENCH_VERIFY_FULL_ASYNC).
"""

import threading
import traceback

_lock = threading.Lock()
_calls = []
_installed = False

# Frames inside asgiref/django test/loadgen plumbing that are never part of
# the request hot path. Calls whose nearest app frame matches these are
# ignored so the report only flags real fork-side fallbacks.
_IGNORE_SUBSTRINGS = (
    "/asgiref/",
)


def _record():
    stack = traceback.extract_stack()
    # Drop the frames inside this module.
    frames = [f for f in stack if "verify_full_async.py" not in f.filename]
    if not frames:
        return
    top = frames[-1]
    if any(s in top.filename for s in _IGNORE_SUBSTRINGS):
        return
    with _lock:
        _calls.append(f"{top.filename}:{top.lineno} in {top.name}")


def install():
    global _installed
    if _installed:
        return
    from asgiref import sync as _sync

    original_call = _sync.SyncToAsync.__call__

    async def patched_call(self, *args, **kwargs):
        _record()
        return await original_call(self, *args, **kwargs)

    _sync.SyncToAsync.__call__ = patched_call
    _installed = True


def report():
    with _lock:
        return list(_calls)


def reset():
    with _lock:
        _calls.clear()
