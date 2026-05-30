import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")

# Install the sync_to_async tripwire before the application is built, so it
# wraps any sync fallback on the request path. Gated so normal runs pay
# nothing.
if os.environ.get("BENCH_VERIFY_FULL_ASYNC"):
    from app import verify_full_async

    verify_full_async.install()

from django.core.rsgi import get_rsgi_application  # noqa: E402

_django_app = get_rsgi_application()


class Application:
    """RSGI target. Granian dispatches via ``__rsgi__`` if present."""

    async def __rsgi__(self, scope, proto):
        # Tiny out-of-band endpoint to read the tripwire tally without going
        # through Django, so reading the report never itself trips
        # sync_to_async.
        if scope.proto == "http" and scope.path == "/__verify__/":
            from app import verify_full_async

            calls = verify_full_async.report()
            body = ("\n".join(calls)).encode() or b""
            proto.response_bytes(
                200,
                [
                    ("content-type", "text/plain"),
                    ("x-sync-to-async-calls", str(len(calls))),
                ],
                body,
            )
            return
        await _django_app.__rsgi__(scope, proto)


application = Application()
