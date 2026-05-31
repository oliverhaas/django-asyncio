from django.conf import settings
from django.contrib.sites.middleware import CurrentSiteMiddleware
from django.contrib.sites.models import Site
from django.http import HttpResponse
from django.test import AsyncRequestFactory, TestCase, modify_settings


@modify_settings(INSTALLED_APPS={"append": "django.contrib.sites"})
class CurrentSiteMiddlewareAsyncTests(TestCase):
    """
    Async tests for CurrentSiteMiddleware.

    Mirrors the critical sync scenarios but drives the middleware via an
    async ``get_response`` and ``AsyncRequestFactory`` so we exercise the
    native ``__acall__`` path with zero ``sync_to_async`` hops.
    """

    async_request_factory = AsyncRequestFactory()

    @classmethod
    def setUpTestData(cls):
        cls.site = Site(id=settings.SITE_ID, domain="example.com", name="example.com")
        cls.site.save()

    def setUp(self):
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

    async def test_site_attached_to_request(self):
        """
        ``request.site`` is attached on the native async path.
        """
        captured = {}

        async def get_response(request):
            captured["site"] = getattr(request, "site", None)
            return HttpResponse("ok")

        request = self.async_request_factory.get("/")
        response = await CurrentSiteMiddleware(get_response)(request)

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(captured["site"])
        self.assertIsInstance(captured["site"], Site)

    async def test_site_matches_configured_site_id(self):
        """
        The attached ``Site`` matches ``settings.SITE_ID``.
        """

        async def get_response(request):
            return HttpResponse(str(request.site.id))

        request = self.async_request_factory.get("/")
        response = await CurrentSiteMiddleware(get_response)(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), str(settings.SITE_ID))
