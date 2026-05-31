from django.conf import settings
from django.contrib.redirects.middleware import RedirectFallbackMiddleware
from django.contrib.redirects.models import Redirect
from django.contrib.sites.models import Site
from django.http import HttpResponse
from django.test import AsyncRequestFactory, TestCase, override_settings


@override_settings(APPEND_SLASH=False, ROOT_URLCONF="redirects_tests.urls")
class AsyncRedirectFallbackMiddlewareTests(TestCase):
    """Cover RedirectFallbackMiddleware on the native async path (__acall__)."""

    request_factory = AsyncRequestFactory()

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.get(pk=settings.SITE_ID)

    @staticmethod
    def _make_404_get_response():
        async def get_response(request):
            return HttpResponse(status=404)

        return get_response

    @staticmethod
    def _make_200_get_response():
        async def get_response(request):
            return HttpResponse("hello", status=200)

        return get_response

    async def test_async_redirect_match(self):
        await Redirect.objects.acreate(
            site=self.site, old_path="/initial", new_path="/new_target"
        )
        request = self.request_factory.get("/initial")
        middleware = RedirectFallbackMiddleware(self._make_404_get_response())

        response = await middleware(request)

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/new_target")

    async def test_async_redirect_no_match_passes_through_404(self):
        request = self.request_factory.get("/no-such-path")
        middleware = RedirectFallbackMiddleware(self._make_404_get_response())

        response = await middleware(request)

        self.assertEqual(response.status_code, 404)

    async def test_async_non_404_response_passes_through(self):
        # Even if a matching Redirect exists, a non-404 response is returned
        # unchanged.
        await Redirect.objects.acreate(
            site=self.site, old_path="/initial", new_path="/new_target"
        )
        request = self.request_factory.get("/initial")
        middleware = RedirectFallbackMiddleware(self._make_200_get_response())

        response = await middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"hello")

    @override_settings(APPEND_SLASH=True)
    async def test_async_redirect_with_append_slash(self):
        await Redirect.objects.acreate(
            site=self.site, old_path="/initial/", new_path="/new_target/"
        )
        request = self.request_factory.get("/initial")
        middleware = RedirectFallbackMiddleware(self._make_404_get_response())

        response = await middleware(request)

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/new_target/")

    async def test_async_response_gone_when_new_path_empty(self):
        await Redirect.objects.acreate(
            site=self.site, old_path="/initial", new_path=""
        )
        request = self.request_factory.get("/initial")
        middleware = RedirectFallbackMiddleware(self._make_404_get_response())

        response = await middleware(request)

        self.assertEqual(response.status_code, 410)
