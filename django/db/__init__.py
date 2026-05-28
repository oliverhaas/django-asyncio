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


# Reset saved queries when a Django request is started. The clear is a pure
# in-memory operation, so the async sibling does the same work directly on the
# event loop (no sync_to_async hop). reset_queries is sync-only and
# areset_queries async-only so each dispatch context runs exactly one of them.
def reset_queries(**kwargs):
    for conn in connections.all(initialized_only=True):
        conn.queries_log.clear()


async def areset_queries(**kwargs):
    for conn in connections.all(initialized_only=True):
        conn.queries_log.clear()


signals.request_started.connect(reset_queries, run_async=False)
signals.request_started.connect(areset_queries, run_sync=False)


# Reset transaction state and close connections past their lifetime.
# close_old_connections evicts the sync connection slot; aclose_old_connections
# evicts the async slot (`connection.async_connection`) natively on the event
# loop. Registered sync-only / async-only respectively so the ASGI request path
# pays no sync_to_async hop here.
def close_old_connections(**kwargs):
    for conn in connections.all(initialized_only=True):
        conn.close_if_unusable_or_obsolete()


async def aclose_old_connections(**kwargs):
    for conn in connections.all(initialized_only=True):
        await conn.aclose_if_unusable_or_obsolete()


signals.request_started.connect(close_old_connections, run_async=False)
signals.request_started.connect(aclose_old_connections, run_sync=False)
signals.request_finished.connect(close_old_connections, run_async=False)
signals.request_finished.connect(aclose_old_connections, run_sync=False)
