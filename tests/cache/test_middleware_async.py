"""
Async cache middleware tests for the native-async rewrite of
``django.middleware.cache``.

These tests cover the request and response phases of
``FetchFromCacheMiddleware``, ``UpdateCacheMiddleware``, and the combined
``CacheMiddleware`` when called through their ASGI entrypoints
(``__acall__``). They use ``AsyncRequestFactory`` and an ``async def
get_response`` so Django's middleware framework marks the instance as a
coroutine function and routes through the async path. The locmem cache
backend provides real ``aget`` / ``aset`` implementations under the hood.
"""

from django.core.cache import cache
from django.http import HttpResponse
from django.middleware.cache import (
    CacheMiddleware,
    FetchFromCacheMiddleware,
    UpdateCacheMiddleware,
)
from django.test import AsyncRequestFactory, SimpleTestCase, override_settings


async def _empty_response(request):
    return HttpResponse()


@override_settings(
    CACHE_MIDDLEWARE_KEY_PREFIX="testasync",
    CACHE_MIDDLEWARE_SECONDS=60,
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        },
    },
)
class AsyncCacheMiddlewareTests(SimpleTestCase):
    path = "/cache/test/"
    factory = AsyncRequestFactory()

    def tearDown(self):
        cache.clear()

    async def test_response_is_cached_on_first_request(self):
        """
        A cache miss falls through to the view, then the response phase
        writes the page into the cache.
        """
        content = "hello async"

        async def get_response(request):
            return HttpResponse(content)

        request = self.factory.get(self.path)
        mw = CacheMiddleware(get_response)
        response = await mw(request)

        self.assertEqual(response.content, content.encode())
        # After the response phase, Cache-Control/Expires should be set.
        self.assertIn("Expires", response)
        self.assertIn("Cache-Control", response)
        self.assertIs(request._cache_update_cache, True)

    async def test_cache_hit_short_circuits_view(self):
        """
        Once a response is cached, a subsequent request returns the cached
        copy from ``FetchFromCacheMiddleware`` without invoking the view.
        """
        content = "from cache"

        async def writer(request):
            return HttpResponse(content)

        # Prime the cache via UpdateCacheMiddleware.
        seed_request = self.factory.get(self.path)
        seed_request._cache_update_cache = True
        await UpdateCacheMiddleware(writer)(seed_request)

        view_called = False

        async def view(request):
            nonlocal view_called
            view_called = True
            return HttpResponse("should not be served")

        request = self.factory.get(self.path)
        mw = FetchFromCacheMiddleware(view)
        response = await mw(request)

        self.assertIs(view_called, False)
        self.assertEqual(response.content, content.encode())
        self.assertIs(request._cache_update_cache, False)

    async def test_non_get_requests_are_not_cached(self):
        """
        POST requests bypass the cache lookup, mark the request as not
        cacheable, and do not write to the cache.
        """
        async def view(request):
            return HttpResponse("posted")

        request = self.factory.post(self.path, data={})
        mw = CacheMiddleware(view)
        response = await mw(request)

        self.assertEqual(response.content, b"posted")
        self.assertIs(request._cache_update_cache, False)

        # Confirm nothing was stored: a fresh GET still misses.
        get_request = self.factory.get(self.path)
        miss = await FetchFromCacheMiddleware(_empty_response)._aprocess_request(
            get_request
        )
        self.assertIsNone(miss)
        self.assertIs(get_request._cache_update_cache, True)

    async def test_private_response_is_not_cached(self):
        """
        Responses carrying ``Cache-Control: private`` are not stored, so a
        follow-up GET misses and still hits the view.
        """
        async def view(request):
            response = HttpResponse("private content")
            response["Cache-Control"] = "private"
            return response

        request = self.factory.get(self.path)
        request._cache_update_cache = True
        await UpdateCacheMiddleware(view)(request)

        follow_up = self.factory.get(self.path)
        cached = await FetchFromCacheMiddleware(_empty_response)._aprocess_request(
            follow_up
        )
        self.assertIsNone(cached)

    async def test_combined_middleware_serves_subsequent_hit(self):
        """
        ``CacheMiddleware`` (the combined fetch+update class) writes on the
        first request and serves from the cache on the second.
        """
        calls = 0

        async def view(request):
            nonlocal calls
            calls += 1
            return HttpResponse(f"call {calls}")

        first_request = self.factory.get(self.path)
        first = await CacheMiddleware(view)(first_request)
        self.assertEqual(first.content, b"call 1")

        second_request = self.factory.get(self.path)
        second = await CacheMiddleware(view)(second_request)
        # View was not entered a second time.
        self.assertEqual(calls, 1)
        self.assertEqual(second.content, b"call 1")
