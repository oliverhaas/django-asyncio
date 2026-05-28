import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")

# Install the sync_to_async tripwire before the application is built, so it
# wraps any sync fallback on the request path. Gated so the sync/WSGI build
# and normal async runs pay nothing.
if os.environ.get("BENCH_VERIFY_FULL_ASYNC"):
    from app import verify_full_async

    verify_full_async.install()

from django.core.asgi import get_asgi_application  # noqa: E402

_django_app = get_asgi_application()


async def application(scope, receive, send):
    # A tiny out-of-band endpoint to read the tripwire tally without going
    # through Django, so reading the report never itself trips sync_to_async.
    if scope["type"] == "http" and scope["path"] == "/__verify__/":
        from app import verify_full_async

        calls = verify_full_async.report()
        body = ("\n".join(calls)).encode() or b""
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/plain"),
                    (b"x-sync-to-async-calls", str(len(calls)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
        return
    await _django_app(scope, receive, send)
