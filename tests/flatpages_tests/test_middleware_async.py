from django.conf import settings
from django.contrib.flatpages.middleware import FlatpageFallbackMiddleware
from django.contrib.flatpages.models import FlatPage
from django.contrib.sites.models import Site
from django.http import HttpResponse
from django.test import (
    AsyncRequestFactory,
    TestCase,
    modify_settings,
    override_settings,
)

from .settings import FLATPAGES_TEMPLATES


@modify_settings(INSTALLED_APPS={"append": "django.contrib.flatpages"})
@override_settings(
    ROOT_URLCONF="flatpages_tests.urls",
    TEMPLATES=FLATPAGES_TEMPLATES,
)
class AsyncFlatpageFallbackMiddlewareTests(TestCase):
    """Cover FlatpageFallbackMiddleware on the native async path (__acall__)."""

    request_factory = AsyncRequestFactory()

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.get(pk=settings.SITE_ID)
        cls.flatpage = FlatPage.objects.create(
            url="/flatpage/",
            title="A Flatpage",
            content="Isn't it flat!",
            enable_comments=False,
            template_name="",
            registration_required=False,
        )
        cls.flatpage.sites.add(cls.site)

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

    async def test_async_flatpage_match_renders_flatpage(self):
        request = self.request_factory.get("/flatpage/")
        middleware = FlatpageFallbackMiddleware(self._make_404_get_response())

        response = await middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Isn't it flat!", response.content)

    async def test_async_no_match_passes_through_404(self):
        request = self.request_factory.get("/no_such_flatpage/")
        middleware = FlatpageFallbackMiddleware(self._make_404_get_response())

        response = await middleware(request)

        self.assertEqual(response.status_code, 404)

    async def test_async_non_404_response_passes_through(self):
        # Even when a matching flatpage exists, a non-404 response is returned
        # unchanged.
        request = self.request_factory.get("/flatpage/")
        middleware = FlatpageFallbackMiddleware(self._make_200_get_response())

        response = await middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"hello")

    @override_settings(APPEND_SLASH=True)
    async def test_async_append_slash_redirects(self):
        request = self.request_factory.get("/flatpage")
        middleware = FlatpageFallbackMiddleware(self._make_404_get_response())

        response = await middleware(request)

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/flatpage/")
