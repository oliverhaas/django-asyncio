import django
from django.core.handlers.rsgi import RSGIHandler


def get_rsgi_application():
    """
    The public interface to Django's RSGI support. Return a Granian-compatible
    RSGI callable.

    RSGI is Granian's native HTTP protocol, an alternative to ASGI that
    collapses per-request awaits (body read, response send, disconnect listen)
    into single calls. Use this when running under Granian and you want lower
    per-request overhead than ASGI.

    Avoids making django.core.handlers.RSGIHandler a public API, in case the
    internal implementation changes or moves in the future.
    """
    django.setup(set_prefix=False)
    return RSGIHandler()
