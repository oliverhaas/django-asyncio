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

from .models import RelatedModel, SimpleModel


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

    def test_acount_native(self):
        async def body():
            return await SimpleModel.objects.acount()

        count, s2a = self._run_native(body)
        self.assertEqual(count, 3)
        self.assertEqual(s2a, 0)

    def test_aexists_native(self):
        async def body():
            present = await SimpleModel.objects.filter(field=1).aexists()
            absent = await SimpleModel.objects.filter(field=999).aexists()
            return present, absent

        (present, absent), s2a = self._run_native(body)
        self.assertIs(present, True)
        self.assertIs(absent, False)
        self.assertEqual(s2a, 0)

    def test_aaggregate_native(self):
        from django.db.models import Sum

        async def body():
            return await SimpleModel.objects.aaggregate(total=Sum("field"))

        result, s2a = self._run_native(body)
        self.assertEqual(result, {"total": 6})
        self.assertEqual(s2a, 0)

    def test_aupdate_native(self):
        async def body():
            n = await SimpleModel.objects.filter(field__in=[1, 2]).aupdate(field=99)
            remaining = [
                f async for f in SimpleModel.objects.order_by("field").values_list(
                    "field", flat=True
                )
            ]
            return n, remaining

        (n, remaining), s2a = self._run_native(body)
        self.assertEqual(n, 2)
        self.assertEqual(remaining, [3, 99, 99])
        self.assertEqual(s2a, 0)

    def test_acreate_native(self):
        async def body():
            obj = await SimpleModel.objects.acreate(field=42)
            return obj.pk, obj.field

        (pk, field), s2a = self._run_native(body)
        self.assertIsNotNone(pk)
        self.assertEqual(field, 42)
        self.assertEqual(s2a, 0)
        # Visible to a fresh sync read (committed).
        self.assertTrue(SimpleModel.objects.filter(field=42).exists())

    def test_asave_insert_and_update_native(self):
        async def body():
            obj = SimpleModel(field=7)
            await obj.asave()
            inserted_pk = obj.pk
            obj.field = 8
            await obj.asave()
            partial = SimpleModel(field=100)
            await partial.asave()
            partial.field = 101
            await partial.asave(update_fields=["field"])
            return inserted_pk, obj.field, partial.field

        (inserted_pk, updated, partial), s2a = self._run_native(body)
        self.assertIsNotNone(inserted_pk)
        self.assertEqual(updated, 8)
        self.assertEqual(partial, 101)
        self.assertEqual(s2a, 0)
        self.assertEqual(SimpleModel.objects.get(pk=inserted_pk).field, 8)

    def test_arefresh_from_db_native(self):
        async def body():
            obj = await SimpleModel.objects.acreate(field=5)
            await SimpleModel.objects.filter(pk=obj.pk).aupdate(field=6)
            await obj.arefresh_from_db()
            return obj.field

        field, s2a = self._run_native(body)
        self.assertEqual(field, 6)
        self.assertEqual(s2a, 0)

    def test_arefresh_from_db_fields_native(self):
        async def body():
            obj = await SimpleModel.objects.acreate(field=5)
            await SimpleModel.objects.filter(pk=obj.pk).aupdate(field=6)
            await obj.arefresh_from_db(fields=["field"])
            return obj.field

        field, s2a = self._run_native(body)
        self.assertEqual(field, 6)
        self.assertEqual(s2a, 0)

    def test_model_adelete_native(self):
        async def body():
            obj = await SimpleModel.objects.acreate(field=11)
            pk = obj.pk
            result = await obj.adelete()
            still_there = await SimpleModel.objects.filter(pk=pk).aexists()
            return result, still_there

        (result, still_there), s2a = self._run_native(body)
        self.assertEqual(result, (1, {"async.SimpleModel": 1}))
        self.assertIs(still_there, False)
        self.assertEqual(s2a, 0)

    def test_queryset_adelete_native(self):
        async def body():
            await SimpleModel.objects.acreate(field=20)
            await SimpleModel.objects.acreate(field=21)
            n, per_model = await SimpleModel.objects.filter(
                field__in=[20, 21]
            ).adelete()
            remaining = await SimpleModel.objects.acount()
            return n, per_model, remaining

        (n, per_model, remaining), s2a = self._run_native(body)
        self.assertEqual(n, 2)
        self.assertEqual(per_model, {"async.SimpleModel": 2})
        # setUp created 3 rows; 2 of the new ones removed leaves the original 3.
        self.assertEqual(remaining, 3)
        self.assertEqual(s2a, 0)

    def test_adelete_cascade_native(self):
        async def body():
            s = await SimpleModel.objects.acreate(field=50)
            await RelatedModel.objects.acreate(simple=s)
            await RelatedModel.objects.acreate(simple=s)
            before = await RelatedModel.objects.acount()
            n, per_model = await s.adelete()
            after = await RelatedModel.objects.acount()
            return before, n, per_model, after

        (before, n, per_model, after), s2a = self._run_native(body)
        self.assertEqual(before, 2)
        # 1 SimpleModel + 2 cascaded RelatedModel.
        self.assertEqual(n, 3)
        self.assertEqual(per_model.get("async.RelatedModel"), 2)
        self.assertEqual(after, 0)
        self.assertEqual(s2a, 0)

    def test_abulk_create_native(self):
        async def body():
            created = await SimpleModel.objects.abulk_create(
                [SimpleModel(field=v) for v in (100, 200, 300)]
            )
            pks_set = all(obj.pk is not None for obj in created)
            total = await SimpleModel.objects.filter(field__gte=100).acount()
            return len(created), pks_set, total

        (n, pks_set, total), s2a = self._run_native(body)
        self.assertEqual(n, 3)
        self.assertIs(pks_set, True)
        self.assertEqual(total, 3)
        self.assertEqual(s2a, 0)

    def test_abulk_update_native(self):
        async def body():
            objs = await SimpleModel.objects.abulk_create(
                [SimpleModel(field=v) for v in (100, 200, 300)]
            )
            for obj in objs:
                obj.field += 1
            rows = await SimpleModel.objects.abulk_update(objs, ["field"])
            values = sorted(
                [
                    v
                    async for v in SimpleModel.objects.filter(
                        field__gte=100
                    ).values_list("field", flat=True)
                ]
            )
            return rows, values

        (rows, values), s2a = self._run_native(body)
        self.assertEqual(rows, 3)
        self.assertEqual(values, [101, 201, 301])
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
