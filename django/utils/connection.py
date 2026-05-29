import asyncio
from contextlib import asynccontextmanager

from asgiref.local import Local

from django.conf import settings as django_settings
from django.utils.functional import cached_property


class ConnectionProxy:
    """Proxy for accessing a connection object's attributes."""

    def __init__(self, connections, alias):
        self.__dict__["_connections"] = connections
        self.__dict__["_alias"] = alias

    def __getattr__(self, item):
        return getattr(self._connections[self._alias], item)

    def __setattr__(self, name, value):
        return setattr(self._connections[self._alias], name, value)

    def __delattr__(self, name):
        return delattr(self._connections[self._alias], name)

    def __contains__(self, key):
        return key in self._connections[self._alias]

    def __eq__(self, other):
        return self._connections[self._alias] == other


class ConnectionDoesNotExist(Exception):
    pass


class BaseConnectionHandler:
    settings_name = None
    exception_class = ConnectionDoesNotExist
    thread_critical = False

    def __init__(self, settings=None):
        self._settings = settings
        self._connections = Local(self.thread_critical)

    @cached_property
    def settings(self):
        self._settings = self.configure_settings(self._settings)
        return self._settings

    def configure_settings(self, settings):
        if settings is None:
            settings = getattr(django_settings, self.settings_name)
        return settings

    def create_connection(self, alias):
        raise NotImplementedError("Subclasses must implement create_connection().")

    def __getitem__(self, alias):
        try:
            return getattr(self._connections, alias)
        except AttributeError:
            if alias not in self.settings:
                raise self.exception_class(f"The connection '{alias}' doesn't exist.")
        conn = self.create_connection(alias)
        setattr(self._connections, alias, conn)
        return conn

    def __setitem__(self, key, value):
        setattr(self._connections, key, value)

    def __delitem__(self, key):
        delattr(self._connections, key)

    @asynccontextmanager
    async def aindependent_connection(self, using, timeout=None):
        """Bind ``using`` to a fresh connection for the duration of the block.

        The storage is task-local (a contextvar), so each branch of an
        ``asyncio.gather`` that enters this gets its own connection and the
        branches' queries can run genuinely in parallel instead of serializing
        on one shared connection. On exit the fresh connection is closed
        (returned to the pool if pooled) and the previous binding restored.

        If ``timeout`` is set and a connection can't be established within it
        (e.g. a pool whose connections are all checked out), this yields
        ``None`` and leaves the existing binding in place, so the caller can
        fall back to the connection it already holds instead of blocking. That
        is what keeps a wide prefetch fan-out from deadlocking: every pooled
        connection can be held by a request's main connection, leaving none for
        the fan-out, and waiting for one that only frees when the fan-out
        finishes would never resolve.

        The caller is responsible for not entering this inside an atomic block:
        an independent connection is a separate session and would not see the
        transaction's uncommitted state.
        """
        fresh = self.create_connection(using)
        try:
            if timeout is None:
                await fresh.aensure_connection()
            else:
                await asyncio.wait_for(fresh.aensure_connection(), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            await fresh.aclose()
            yield None
            return

        had_previous = hasattr(self._connections, using)
        previous = getattr(self._connections, using) if had_previous else None
        setattr(self._connections, using, fresh)
        try:
            yield fresh
        finally:
            try:
                await fresh.aclose()
            finally:
                if had_previous:
                    setattr(self._connections, using, previous)
                else:
                    try:
                        delattr(self._connections, using)
                    except AttributeError:
                        pass

    def __iter__(self):
        return iter(self.settings)

    def all(self, initialized_only=False):
        return [
            self[alias]
            for alias in self
            # If initialized_only is True, return only initialized connections.
            if not initialized_only or hasattr(self._connections, alias)
        ]

    def close_all(self):
        for conn in self.all(initialized_only=True):
            conn.close()
