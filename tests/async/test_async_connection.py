"""Tests for the async sibling API on BaseDatabaseWrapper.

Phase 1 of the django-asyncio fork added aXxx() methods on
BaseDatabaseWrapper and an async psycopg path on the postgresql backend,
plus an ASGI hook that closes the async connection slot at request
boundaries. These tests exercise the parts reachable on any backend;
postgresql-specific behaviour is gated on ``connection.vendor``.

Async connections opened by a test must be closed inside the same
coroutine: Django runs ``async def test_*`` via async_to_sync, whose
event loop is torn down when the method returns, so a connection left
open is bound to a dead loop and blocks test-database destruction. The
``_aclosing`` helper enforces that.
"""

import contextlib
import unittest

from django.db import (
    aclose_old_connections,
    close_old_connections,
    connection,
    connections,
)
from django.test import SimpleTestCase, TransactionTestCase


@contextlib.asynccontextmanager
async def _aclosing(conn, *, close_pool=False):
    """Yield `conn`, then close its async slot (and optionally pool)."""
    try:
        yield conn
    finally:
        await conn.aclose()
        if close_pool:
            await conn.aclose_pool()


class AsyncConnectionStateTests(SimpleTestCase):
    """The new self.async_connection slot and basic guard rails.

    These don't touch the database, so SimpleTestCase is enough.
    """

    databases = {"default"}

    def test_async_connection_slot_starts_none(self):
        self.assertIsNone(connection.async_connection)

    async def test_aclose_is_noop_when_no_async_connection(self):
        self.assertIsNone(connection.async_connection)
        await connection.aclose()
        self.assertIsNone(connection.async_connection)

    async def test_aclose_if_unusable_or_obsolete_no_async_connection(self):
        self.assertIsNone(connection.async_connection)
        await connection.aclose_if_unusable_or_obsolete()
        self.assertIsNone(connection.async_connection)

    async def test_aclose_old_connections_skips_when_async_slot_empty(self):
        for conn in connections.all(initialized_only=True):
            self.assertIsNone(conn.async_connection)
        await aclose_old_connections()
        for conn in connections.all(initialized_only=True):
            self.assertIsNone(conn.async_connection)


class SyncCloseOldConnectionsUnchangedTests(TransactionTestCase):
    """The sync close_old_connections() must still close the sync slot."""

    available_apps = []

    def test_sync_path_unchanged(self):
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        self.assertIsNotNone(connection.connection)
        close_old_connections()


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "Async DB driver path is currently postgresql-only.",
)
class AsyncPostgresConnectionTests(TransactionTestCase):
    """Round-trip the new async API against a real psycopg AsyncConnection."""

    available_apps = []

    async def test_aconnect_populates_async_slot(self):
        async with _aclosing(connection):
            await connection.aensure_connection()
            self.assertIsNotNone(connection.async_connection)
            # The async path must not touch the sync slot.
            self.assertIsNot(connection.async_connection, connection.connection)

    async def test_aget_database_version_returns_tuple(self):
        async with _aclosing(connection):
            version = await connection.aget_database_version()
        self.assertIsInstance(version, tuple)
        self.assertEqual(len(version), 2)
        self.assertGreater(version[0], 0)

    async def test_acursor_round_trip(self):
        async with _aclosing(connection):
            async with await connection.acursor() as cur:
                await cur.execute("SELECT 17, 42")
                row = await cur.fetchone()
        self.assertEqual(row, (17, 42))

    async def test_acursor_executemany(self):
        async with _aclosing(connection):
            async with await connection.acursor() as cur:
                await cur.execute("CREATE TEMPORARY TABLE async_em (x int)")
                await cur.executemany(
                    "INSERT INTO async_em VALUES (%s)", [(1,), (2,), (3,)]
                )
                await cur.execute("SELECT COUNT(*) FROM async_em")
                (count,) = await cur.fetchone()
        self.assertEqual(count, 3)

    async def test_ais_usable_after_connect(self):
        async with _aclosing(connection):
            await connection.aensure_connection()
            self.assertTrue(await connection.ais_usable())

    async def test_aclose_clears_async_slot(self):
        await connection.aensure_connection()
        self.assertIsNotNone(connection.async_connection)
        await connection.aclose()
        self.assertIsNone(connection.async_connection)

    async def test_aclose_old_connections_evicts_obsolete(self):
        await connection.aensure_connection()
        self.assertIsNotNone(connection.async_connection)
        # Force the connection past its max-age so cleanup evicts it.
        connection.close_at = 0  # already in the past per time.monotonic()
        await aclose_old_connections()
        self.assertIsNone(connection.async_connection)

    async def test_aset_autocommit_round_trip(self):
        async with _aclosing(connection):
            await connection.aensure_connection()
            self.assertTrue(await connection.aget_autocommit())
            await connection.aset_autocommit(False)
            self.assertFalse(await connection.aget_autocommit())
            # Roll back the implicit transaction before flipping back.
            await connection.arollback()
            await connection.aset_autocommit(True)
            self.assertTrue(await connection.aget_autocommit())


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "Async pool path is currently postgresql-only.",
)
class AsyncPostgresPoolTests(TransactionTestCase):
    """The async psycopg_pool path: AsyncConnectionPool getconn / putconn."""

    available_apps = []

    async def test_async_pool_returns_async_connections(self):
        try:
            from psycopg_pool import AsyncConnectionPool  # noqa: F401
        except ImportError:
            self.skipTest("psycopg_pool not installed")
        original_options = connection.settings_dict["OPTIONS"]
        connection.settings_dict["OPTIONS"] = {
            **original_options,
            "pool": {"min_size": 1, "max_size": 2},
        }
        try:
            async with _aclosing(connection, close_pool=True):
                await connection.aensure_connection()
                self.assertIsNotNone(connection.async_pool)
                async with await connection.acursor() as cur:
                    await cur.execute("SELECT 99")
                    (val,) = await cur.fetchone()
                self.assertEqual(val, 99)
        finally:
            connection.settings_dict["OPTIONS"] = original_options
