import os

from django.http import HttpResponse, HttpResponseNotFound
from django.middleware.locale import LocaleMiddleware
from django.test import AsyncRequestFactory, SimpleTestCase, override_settings
from django.urls import clear_url_caches


ASYNC_OVERRIDES = dict(
    USE_I18N=True,
    LOCALE_PATHS=[
        os.path.join(os.path.dirname(__file__), "patterns", "locale"),
    ],
    LANGUAGE_CODE="en-us",
    LANGUAGES=[
        ("nl", "Dutch"),
        ("en", "English"),
        ("pt-br", "Brazilian Portuguese"),
    ],
    MIDDLEWARE=[
        "django.middleware.locale.LocaleMiddleware",
        "django.middleware.common.CommonMiddleware",
    ],
    ROOT_URLCONF="i18n.patterns.urls.default",
)


@override_settings(**ASYNC_OVERRIDES)
class AsyncLocaleMiddlewareTests(SimpleTestCase):
    """
    Native async path through LocaleMiddleware: covers language activation
    from the URL prefix, the Content-Language response header, and the
    redirect that adds the language prefix when the unprefixed path 404s.
    """

    arf = AsyncRequestFactory()

    def setUp(self):
        clear_url_caches()
        self.addCleanup(clear_url_caches)

    def test_async_capable_when_get_response_is_coroutine(self):
        async def get_response(request):
            return HttpResponse()

        middleware = LocaleMiddleware(get_response)
        from asgiref.sync import iscoroutinefunction

        self.assertTrue(iscoroutinefunction(middleware))

    async def test_language_activated_from_url_prefix(self):
        captured = {}

        async def get_response(request):
            captured["language_code"] = request.LANGUAGE_CODE
            return HttpResponse()

        request = self.arf.get("/nl/prefixed/")
        middleware = LocaleMiddleware(get_response)
        response = await middleware(request)

        self.assertEqual(captured["language_code"], "nl")
        self.assertEqual(response.headers["Content-Language"], "nl")

    async def test_content_language_header_from_accept_language(self):
        async def get_response(request):
            return HttpResponse()

        request = self.arf.get(
            "/not-prefixed/", headers={"accept-language": "nl"}
        )
        middleware = LocaleMiddleware(get_response)
        response = await middleware(request)

        self.assertEqual(response.headers["Content-Language"], "nl")
        # Unprefixed paths should add Accept-Language to Vary.
        self.assertIn("Accept-Language", response.headers.get("Vary", ""))

    async def test_redirect_adds_language_prefix_on_404(self):
        async def get_response(request):
            # Simulate the unprefixed URL not resolving: the resolver would
            # return 404 for "/account/register/" because the path lives
            # under the i18n prefix in i18n.patterns.urls.default.
            return HttpResponseNotFound()

        request = self.arf.get(
            "/account/register/", headers={"accept-language": "en"}
        )
        middleware = LocaleMiddleware(get_response)
        response = await middleware(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/en/account/register/")
        # The redirect varies on Accept-Language and Cookie so caches do
        # not serve the wrong language.
        vary = response.headers.get("Vary", "")
        self.assertIn("Accept-Language", vary)
        self.assertIn("Cookie", vary)
