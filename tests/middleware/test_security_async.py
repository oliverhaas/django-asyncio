from django.http import HttpResponse
from django.middleware.security import SecurityMiddleware
from django.test import AsyncRequestFactory, SimpleTestCase, override_settings


async def get_response_empty(request):
    return HttpResponse()


class SecurityMiddlewareAsyncTests(SimpleTestCase):
    """
    Async tests for SecurityMiddleware.

    Mirrors a few critical scenarios from the sync test suite, but drives the
    middleware via an async ``get_response`` and ``AsyncRequestFactory`` so we
    exercise the native ``__acall__`` path with zero ``sync_to_async`` hops.
    """

    def setUp(self):
        self.async_request_factory = AsyncRequestFactory()

    @override_settings(SECURE_SSL_REDIRECT=True)
    async def test_ssl_redirect_short_circuits_before_view(self):
        """
        With SECURE_SSL_REDIRECT True, the async middleware returns a 301
        redirect to the https:// version of the URL without calling the inner
        view.
        """
        view_called = False

        async def get_response(request):
            nonlocal view_called
            view_called = True
            return HttpResponse()

        request = self.async_request_factory.get("/some/url?query=string")
        response = await SecurityMiddleware(get_response)(request)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response["Location"], "https://testserver/some/url?query=string"
        )
        self.assertFalse(view_called)

    @override_settings(SECURE_HSTS_SECONDS=3600, SECURE_HSTS_INCLUDE_SUBDOMAINS=True)
    async def test_hsts_header_set_on_secure_async_request(self):
        """
        With SECURE_HSTS_SECONDS non-zero and SECURE_HSTS_INCLUDE_SUBDOMAINS
        True, the async middleware adds the expected Strict-Transport-Security
        header on a secure request.
        """
        request = self.async_request_factory.get("/", secure=True)
        response = await SecurityMiddleware(get_response_empty)(request)
        self.assertEqual(
            response.headers["Strict-Transport-Security"],
            "max-age=3600; includeSubDomains",
        )

    @override_settings(SECURE_CONTENT_TYPE_NOSNIFF=True)
    async def test_content_type_nosniff_header_set(self):
        """
        With SECURE_CONTENT_TYPE_NOSNIFF True, the async middleware adds the
        X-Content-Type-Options: nosniff header.
        """
        request = self.async_request_factory.get("/")
        response = await SecurityMiddleware(get_response_empty)(request)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    @override_settings(SECURE_REFERRER_POLICY="strict-origin")
    async def test_referrer_policy_header_set(self):
        """
        With SECURE_REFERRER_POLICY set to a valid value, the async middleware
        adds a Referrer-Policy header to the response.
        """
        request = self.async_request_factory.get("/")
        response = await SecurityMiddleware(get_response_empty)(request)
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin")
