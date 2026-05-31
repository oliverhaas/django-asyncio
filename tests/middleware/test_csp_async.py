from utils_tests.test_csp import basic_config, basic_policy

from django.http import HttpResponse
from django.middleware.csp import ContentSecurityPolicyMiddleware, get_nonce
from django.test import AsyncRequestFactory, SimpleTestCase, override_settings
from django.utils.csp import CSP


async def get_response_empty(request):
    return HttpResponse()


class ContentSecurityPolicyMiddlewareAsyncTests(SimpleTestCase):
    """
    Async tests for ContentSecurityPolicyMiddleware.

    Mirrors a few critical scenarios from the sync test suite but drives the
    middleware via an async ``get_response`` and ``AsyncRequestFactory`` so we
    exercise the native ``__acall__`` path with zero ``sync_to_async`` hops.
    """

    def setUp(self):
        self.async_request_factory = AsyncRequestFactory()

    @override_settings(SECURE_CSP=basic_config, SECURE_CSP_REPORT_ONLY=None)
    async def test_nonce_attached_to_request_and_enforce_header_set(self):
        """
        The async path attaches a LazyNonce to the request and sets the
        Content-Security-Policy header from SECURE_CSP.
        """
        captured = {}

        async def get_response(request):
            captured["nonce"] = get_nonce(request)
            return HttpResponse()

        request = self.async_request_factory.get("/")
        response = await ContentSecurityPolicyMiddleware(get_response)(request)

        self.assertIsNotNone(captured["nonce"])
        self.assertEqual(response.headers[CSP.HEADER_ENFORCE], basic_policy)
        self.assertNotIn(CSP.HEADER_REPORT_ONLY, response.headers)

    @override_settings(SECURE_CSP=None, SECURE_CSP_REPORT_ONLY=basic_config)
    async def test_report_only_header_set(self):
        """
        With only SECURE_CSP_REPORT_ONLY configured the async path sets the
        Content-Security-Policy-Report-Only header and skips the enforce one.
        """
        request = self.async_request_factory.get("/")
        response = await ContentSecurityPolicyMiddleware(get_response_empty)(request)

        self.assertEqual(response.headers[CSP.HEADER_REPORT_ONLY], basic_policy)
        self.assertNotIn(CSP.HEADER_ENFORCE, response.headers)

    @override_settings(SECURE_CSP={"default-src": [CSP.SELF, CSP.NONCE]})
    async def test_nonce_rendered_into_enforce_header(self):
        """
        When the view accesses ``request.csp_nonce`` the materialized nonce is
        spliced into the Content-Security-Policy header by ``build_policy``.
        """

        async def get_response_with_nonce(request):
            # Force the LazyNonce to materialize, mirroring template access.
            nonce = str(get_nonce(request))
            return HttpResponse(nonce)

        request = self.async_request_factory.get("/")
        response = await ContentSecurityPolicyMiddleware(get_response_with_nonce)(
            request
        )

        nonce = response.content.decode()
        self.assertTrue(nonce)
        self.assertEqual(
            response.headers[CSP.HEADER_ENFORCE],
            f"default-src 'self' 'nonce-{nonce}'",
        )
