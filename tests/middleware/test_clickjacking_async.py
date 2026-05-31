from django.http import HttpResponse
from django.middleware.clickjacking import XFrameOptionsMiddleware
from django.test import AsyncRequestFactory, SimpleTestCase, override_settings


async def get_response_empty(request):
    return HttpResponse()


class XFrameOptionsMiddlewareAsyncTests(SimpleTestCase):
    """
    Async tests for the X-Frame-Options clickjacking prevention middleware.

    Mirrors a few critical scenarios from the sync test suite, but drives the
    middleware via an async ``get_response`` and ``AsyncRequestFactory`` so we
    exercise the native ``__acall__`` path with zero ``sync_to_async`` hops.
    """

    def setUp(self):
        self.async_request_factory = AsyncRequestFactory()

    async def test_default_deny(self):
        """
        With no X_FRAME_OPTIONS setting the async path still defaults to DENY.
        """
        request = self.async_request_factory.get("/")
        with override_settings(X_FRAME_OPTIONS=None):
            from django.conf import settings

            del settings.X_FRAME_OPTIONS  # restored by override_settings
            response = await XFrameOptionsMiddleware(get_response_empty)(request)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    async def test_same_origin_setting(self):
        """
        The X_FRAME_OPTIONS setting is honored on the async path and normalized
        to upper case.
        """
        request = self.async_request_factory.get("/")
        with override_settings(X_FRAME_OPTIONS="sameorigin"):
            response = await XFrameOptionsMiddleware(get_response_empty)(request)
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")

    async def test_dont_override_existing_header(self):
        """
        If the inner async view already set X-Frame-Options, the middleware
        leaves it alone.
        """

        async def same_origin_response(request):
            response = HttpResponse()
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            return response

        request = self.async_request_factory.get("/")
        with override_settings(X_FRAME_OPTIONS="DENY"):
            response = await XFrameOptionsMiddleware(same_origin_response)(request)
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")

    async def test_xframe_options_exempt(self):
        """
        Responses flagged with ``xframe_options_exempt = True`` do not get the
        header set on the async path.
        """

        async def exempt_response(request):
            response = HttpResponse()
            response.xframe_options_exempt = True
            return response

        request = self.async_request_factory.get("/")
        with override_settings(X_FRAME_OPTIONS="SAMEORIGIN"):
            response = await XFrameOptionsMiddleware(exempt_response)(request)
        self.assertIsNone(response.headers.get("X-Frame-Options"))
