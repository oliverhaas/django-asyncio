"""Tests for the *native* async ORM execution path (Phase 2).

Django runs every ``async def test_*`` through async_to_sync, which makes
the QuerySet async methods take the thread-sensitive sync fallback (so
they observe the test's transaction). To exercise the genuine async path
instead, these tests run their async body with ``asyncio.run`` from a
plain *sync* test method: no async_to_sync wrapper is on the stack, so
``_use_native_async`` selects the native driver. TransactionTestCase is
required because the native async connection is a separate session that
only sees committed rows.

Gated on connection.vendor since the native path is postgresql-only for
now.
"""

import asyncio
import unittest

from asgiref import sync as asgiref_sync
from django.db import connection
from django.test import TransactionTestCase

from .models import SimpleModel


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "Native async execution path is currently postgresql-only.",
)
class NativeAsyncReadTests(TransactionTestCase):
    available_apps = ["async"]

    def setUp(self):
        # Committed (TransactionTestCase) so the separate async session sees it.
        SimpleModel.objects.create(field=1)
        SimpleModel.objects.create(field=2)
        SimpleModel.objects.create(field=3)

    def _run_native(self, coro_func):
        """Run an async body with no async_to_sync ancestor, counting any
        sync_to_async calls that fire during it. The native async connection
        is closed inside the same event loop it was opened in."""
        calls = []
        original = asgiref_sync.SyncToAsync.__call__

        async def tracking(self, *args, **kwargs):
            calls.append(True)
            return await original(self, *args, **kwargs)

        async def wrapper():
            try:
                return await coro_func()
            finally:
                await connection.aclose()

        asgiref_sync.SyncToAsync.__call__ = tracking
        try:
            result = asyncio.run(wrapper())
        finally:
            asgiref_sync.SyncToAsync.__call__ = original
        return result, len(calls)

    def test_async_for_is_native(self):
        async def body():
            return [m.field async for m in SimpleModel.objects.order_by("field")]

        fields, s2a = self._run_native(body)
        self.assertEqual(fields, [1, 2, 3])
        self.assertEqual(s2a, 0)

    def test_aget_is_native(self):
        async def body():
            obj = await SimpleModel.objects.aget(field=2)
            return obj.field

        field, s2a = self._run_native(body)
        self.assertEqual(field, 2)
        self.assertEqual(s2a, 0)

    def test_aget_does_not_exist(self):
        async def body():
            try:
                await SimpleModel.objects.aget(field=999)
            except SimpleModel.DoesNotExist:
                return "missing"
            return "found"

        result, s2a = self._run_native(body)
        self.assertEqual(result, "missing")
        self.assertEqual(s2a, 0)

    def test_aget_multiple_returned(self):
        async def body():
            try:
                await SimpleModel.objects.aget(field__in=[1, 2])
            except SimpleModel.MultipleObjectsReturned:
                return "multiple"
            return "single"

        result, _ = self._run_native(body)
        self.assertEqual(result, "multiple")

    def test_afirst_alast_native(self):
        async def body():
            qs = SimpleModel.objects.order_by("field")
            first = await qs.afirst()
            last = await qs.alast()
            return first.field, last.field

        (first, last), s2a = self._run_native(body)
        self.assertEqual((first, last), (1, 3))
        self.assertEqual(s2a, 0)

    def test_aiterator_native(self):
        async def body():
            return [
                m.field
                async for m in SimpleModel.objects.order_by("field").aiterator()
            ]

        fields, s2a = self._run_native(body)
        self.assertEqual(fields, [1, 2, 3])
        self.assertEqual(s2a, 0)

    def test_values_and_values_list_native(self):
        async def body():
            qs = SimpleModel.objects.order_by("field")
            dicts = [d async for d in qs.values("field")]
            tuples = [t async for t in qs.values_list("field")]
            flat = [v async for v in qs.values_list("field", flat=True)]
            return dicts, tuples, flat

        (dicts, tuples, flat), s2a = self._run_native(body)
        self.assertEqual(dicts, [{"field": 1}, {"field": 2}, {"field": 3}])
        self.assertEqual(tuples, [(1,), (2,), (3,)])
        self.assertEqual(flat, [1, 2, 3])
        self.assertEqual(s2a, 0)
