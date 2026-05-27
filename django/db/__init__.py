from django.core import signals
from django.db.utils import (
    DEFAULT_DB_ALIAS,
    DJANGO_VERSION_PICKLE_KEY,
    ConnectionHandler,
    ConnectionRouter,
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
)
from django.utils.connection import ConnectionProxy

__all__ = [
    "aclose_old_connections",
    "close_old_connections",
    "connection",
    "connections",
    "reset_queries",
    "router",
    "DatabaseError",
    "IntegrityError",
    "InternalError",
    "ProgrammingError",
    "DataError",
    "NotSupportedError",
    "Error",
    "InterfaceError",
    "OperationalError",
    "DEFAULT_DB_ALIAS",
    "DJANGO_VERSION_PICKLE_KEY",
]

connections = ConnectionHandler()

router = ConnectionRouter()

# For backwards compatibility. Prefer connections['default'] instead.
connection = ConnectionProxy(connections, DEFAULT_DB_ALIAS)


# Register an event to reset saved queries when a Django request is started.
def reset_queries(**kwargs):
    for conn in connections.all(initialized_only=True):
        conn.queries_log.clear()


signals.request_started.connect(reset_queries)


# Register an event to reset transaction state and close connections past
# their lifetime.
def close_old_connections(**kwargs):
    for conn in connections.all(initialized_only=True):
        conn.close_if_unusable_or_obsolete()


async def aclose_old_connections():
    """Async sibling of close_old_connections.

    Closes the async DB connection slot (`connection.async_connection`) for
    every initialized wrapper, separate from the sync slot that
    `close_old_connections` handles. Called directly from the ASGI handler
    at request boundaries; not wired as a Signal receiver because
    `request_finished.send()` is invoked from async code paths (e.g. test
    client streaming response close) and Signal's sync->async wrapping
    breaks if called from a thread that already has a running event loop.
    """
    for conn in connections.all(initialized_only=True):
        await conn.aclose_if_unusable_or_obsolete()


signals.request_started.connect(close_old_connections)
signals.request_finished.connect(close_old_connections)
