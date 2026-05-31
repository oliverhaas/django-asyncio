"""
Tests for the native async path of CsrfViewMiddleware.

These exercise CsrfViewMiddleware as a fully-async middleware (``__acall__``)
to ensure ``process_request`` and ``process_response`` run correctly on the
async request hot path without any sync_to_async hop.
"""

from django.conf import settings
from django.http import HttpResponse
from django.middleware.csrf import (
    REASON_CSRF_TOKEN_MISSING,
    REASON_NO_CSRF_COOKIE,
    CsrfViewMiddleware,
)
from django.test import AsyncRequestFactory, SimpleTestCase

from .tests import MASKED_TEST_SECRET1, MASKED_TEST_SECRET2, TEST_SECRET
from .views import post_form_view


async def _async_ok(request):
    return HttpResponse("OK")


class CsrfAsyncMiddlewareTests(SimpleTestCase):
    """
    Validate that CsrfViewMiddleware is async-capable and dispatches via
    ``__acall__`` when wrapping an async ``get_response``.
    """

    def setUp(self):
        self.factory = AsyncRequestFactory()

    def test_middleware_marked_async_when_get_response_is_async(self):
        from asgiref.sync import iscoroutinefunction

        mw = CsrfViewMiddleware(_async_ok)
        self.assertTrue(iscoroutinefunction(mw))

    def test_middleware_not_marked_async_when_get_response_is_sync(self):
        from asgiref.sync import iscoroutinefunction

        def sync_get_response(request):
            return HttpResponse("OK")

        mw = CsrfViewMiddleware(sync_get_response)
        self.assertFalse(iscoroutinefunction(mw))

    async def test_async_get_passes_and_sets_no_cookie_without_get_token(self):
        """
        A GET request through the async path returns the wrapped response and
        does not set the CSRF cookie if get_token() was never called.
        """
        request = self.factory.get("/")
        mw = CsrfViewMiddleware(_async_ok)
        response = await mw(request)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(settings.CSRF_COOKIE_NAME, response.cookies)

    async def test_async_post_without_csrf_cookie_rejected(self):
        """
        A POST request without a CSRF cookie is rejected with 403 by
        process_view, which is invoked directly (process_view is not part of
        the async __acall__ hot path).
        """
        request = self.factory.post("/", data={})
        mw = CsrfViewMiddleware(_async_ok)
        mw.process_request(request)
        with self.assertLogs("django.security.csrf", "WARNING") as cm:
            response = mw.process_view(request, post_form_view, (), {})

        self.assertEqual(response.status_code, 403)
        self.assertIn(REASON_NO_CSRF_COOKIE, cm.records[0].getMessage())

    async def test_async_post_with_valid_token_passes(self):
        """
        A POST request with a valid CSRF cookie + matching token passes
        process_view, and the full async pipeline reaches the wrapped view.
        """
        request = self.factory.post(
            "/",
            data={"csrfmiddlewaretoken": MASKED_TEST_SECRET2},
            HTTP_X_CSRFTOKEN=MASKED_TEST_SECRET2,
        )
        request.COOKIES[settings.CSRF_COOKIE_NAME] = MASKED_TEST_SECRET1
        mw = CsrfViewMiddleware(_async_ok)
        mw.process_request(request)
        view_resp = mw.process_view(request, post_form_view, (), {})
        self.assertIsNone(view_resp)

        # The masked secret got unmasked into request.META["CSRF_COOKIE"].
        self.assertEqual(request.META["CSRF_COOKIE"], TEST_SECRET)

        response = await mw(request)
        self.assertEqual(response.status_code, 200)

    async def test_async_post_with_cookie_but_no_token_rejected(self):
        """
        A POST with a CSRF cookie but no matching token in POST or header is
        rejected with the missing-token reason.
        """
        request = self.factory.post("/", data={})
        request.COOKIES[settings.CSRF_COOKIE_NAME] = MASKED_TEST_SECRET1
        mw = CsrfViewMiddleware(_async_ok)
        mw.process_request(request)
        with self.assertLogs("django.security.csrf", "WARNING") as cm:
            response = mw.process_view(request, post_form_view, (), {})

        self.assertEqual(response.status_code, 403)
        self.assertIn(REASON_CSRF_TOKEN_MISSING, cm.records[0].getMessage())

    async def test_async_response_sets_csrf_cookie_when_flagged(self):
        """
        If process_request flags CSRF_COOKIE_NEEDS_UPDATE (e.g. via get_token),
        the async path's process_response sets the outgoing CSRF cookie.
        """

        async def get_response_setting_token(request):
            # Trigger the same code path get_token() does: flag the cookie for
            # update so process_response writes it to the outgoing response.
            request.META["CSRF_COOKIE_NEEDS_UPDATE"] = True
            return HttpResponse("OK")

        request = self.factory.get("/")
        request.COOKIES[settings.CSRF_COOKIE_NAME] = MASKED_TEST_SECRET1
        mw = CsrfViewMiddleware(get_response_setting_token)
        response = await mw(request)

        self.assertEqual(response.status_code, 200)
        self.assertIn(settings.CSRF_COOKIE_NAME, response.cookies)
        self.assertEqual(
            response.cookies[settings.CSRF_COOKIE_NAME].value, TEST_SECRET
        )
        # Flag was reset so a second middleware instance would not rewrite.
        self.assertFalse(request.META["CSRF_COOKIE_NEEDS_UPDATE"])
