import re

from django.conf import settings
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpResponseNotFound
from django.middleware.common import BrokenLinkEmailsMiddleware, CommonMiddleware
from django.test import AsyncRequestFactory, SimpleTestCase, override_settings


async def get_response_empty(request):
    return HttpResponse()


async def get_response_404(request):
    return HttpResponseNotFound()


@override_settings(ROOT_URLCONF="middleware.urls")
class CommonMiddlewareAsyncTests(SimpleTestCase):
    """
    Async tests for CommonMiddleware.

    Mirrors a few critical scenarios from the sync test suite, but drives the
    middleware via an async ``get_response`` and ``AsyncRequestFactory`` so we
    exercise the native ``__acall__`` path with zero ``sync_to_async`` hops.
    """

    async_request_factory = AsyncRequestFactory()

    @override_settings(APPEND_SLASH=True)
    async def test_append_slash_redirects_slashless_known_url(self):
        """
        A known slashless URL with APPEND_SLASH gets a 301 to the slashed
        variant via the async path.
        """
        request = self.async_request_factory.get("/slash")
        response = await CommonMiddleware(get_response_404)(request)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response.headers["Location"], "/slash/")

    @override_settings(
        DISALLOWED_USER_AGENTS=[re.compile(r"foo")],
    )
    async def test_disallowed_user_agent_raises(self):
        """
        Requests with a disallowed User-Agent are short-circuited on the async
        path with PermissionDenied before ``get_response`` is awaited.
        """
        request = self.async_request_factory.get(
            "/slash/", headers={"user-agent": "foo"}
        )

        async def fail_if_called(request):  # pragma: no cover
            raise AssertionError("get_response should not be called")

        with self.assertRaises(PermissionDenied):
            await CommonMiddleware(fail_if_called)(request)

    @override_settings(PREPEND_WWW=True, APPEND_SLASH=False)
    async def test_prepend_www_redirects(self):
        """
        PREPEND_WWW issues a 301 to the www. variant of the host on the async
        path.
        """
        request = self.async_request_factory.get("/path/")
        response = await CommonMiddleware(get_response_empty)(request)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response.headers["Location"],
            f"{request.scheme}://www.testserver/path/",
        )

    async def test_content_length_set_on_async_response(self):
        """
        Non-streaming responses returned through the async path get a
        Content-Length header set by _process_response.
        """
        body = b"async hello"

        async def get_response(request):
            return HttpResponse(body)

        request = self.async_request_factory.get("/slash/")
        response = await CommonMiddleware(get_response)(request)
        self.assertEqual(response.headers["Content-Length"], str(len(body)))


@override_settings(
    IGNORABLE_404_URLS=[re.compile(r"foo")],
    MANAGERS=["manager@example.com"],
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
)
class BrokenLinkEmailsMiddlewareAsyncTests(SimpleTestCase):
    """
    Async tests for BrokenLinkEmailsMiddleware.

    The mail send remains synchronous (wrapped via sync_to_async on the rare
    404 branch only); these tests confirm the dispatching behavior matches the
    sync middleware on the async path.
    """

    async_request_factory = AsyncRequestFactory()

    async def test_404_with_referer_sends_mail(self):
        """
        A 404 response with a HTTP_REFERER triggers a broken-link email on the
        async path.
        """
        request = self.async_request_factory.get(
            "/regular_url/that/does/not/exist",
            headers={"referer": "/another/url/"},
        )
        await BrokenLinkEmailsMiddleware(get_response_404)(request)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Broken", mail.outbox[0].subject)

    async def test_200_does_not_send_mail(self):
        """
        A successful response does not trigger any mail on the async path,
        even with a referer set.
        """
        request = self.async_request_factory.get(
            "/anything/", headers={"referer": "/another/url/"}
        )
        await BrokenLinkEmailsMiddleware(get_response_empty)(request)
        self.assertEqual(len(mail.outbox), 0)
