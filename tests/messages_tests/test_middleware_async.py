from django.contrib.messages import constants
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.messages.storage.base import BaseStorage, Message
from django.http import HttpResponse
from django.test import AsyncRequestFactory, SimpleTestCase, override_settings


async def get_response_empty(request):
    return HttpResponse()


class _RecordingStorage(BaseStorage):
    """
    Minimal storage stub that records the ``update`` call without needing
    sessions or cookies. Used to assert the async middleware actually
    routes ``process_response`` through the storage layer.
    """

    def __init__(self, request, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        self.update_calls = []

    def _get(self, *args, **kwargs):
        return [], True

    def update(self, response):
        self.update_calls.append(response)
        return []


class _UnstoredStorage(_RecordingStorage):
    """Storage stub that always reports an unstored message."""

    def update(self, response):
        super().update(response)
        return [Message(constants.INFO, "unstored")]


@override_settings(
    MESSAGE_STORAGE="django.contrib.messages.storage.cookie.CookieStorage",
)
class MessageMiddlewareAsyncTests(SimpleTestCase):
    """
    Async tests for ``MessageMiddleware``.

    Mirror the critical sync scenarios but drive the middleware through
    ``__acall__`` with ``AsyncRequestFactory`` so the native async path is
    exercised end to end. Cookie storage is used so the lazy
    ``default_storage(request)`` call in ``process_request`` does not
    require a session middleware in front of us.
    """

    def setUp(self):
        self.async_request_factory = AsyncRequestFactory()

    async def test_response_without_messages(self):
        """
        The async path is tolerant of a request that never had ``_messages``
        attached (e.g. a higher middleware short-circuited).
        """
        middleware = MessageMiddleware(get_response_empty)

        async def get_response(request):
            # Strip the storage that ``process_request`` attached so we
            # exercise the "no _messages" branch in ``_aprocess_response``.
            if hasattr(request, "_messages"):
                del request._messages
            return HttpResponse()

        middleware = MessageMiddleware(get_response)
        request = self.async_request_factory.get("/")
        response = await middleware(request)

        self.assertEqual(response.status_code, 200)

    async def test_storage_update_invoked_on_response(self):
        """
        On the async path the storage's ``update`` is invoked with the
        response, persisting any queued messages.
        """
        storage_holder = {}

        async def get_response(request):
            # Replace the lazy default storage with our recording stub so we
            # can assert ``update`` was actually called by the middleware.
            request._messages = _RecordingStorage(request)
            storage_holder["storage"] = request._messages
            return HttpResponse()

        middleware = MessageMiddleware(get_response)
        request = self.async_request_factory.get("/")
        response = await middleware(request)

        storage = storage_holder["storage"]
        self.assertEqual(len(storage.update_calls), 1)
        self.assertIs(storage.update_calls[0], response)

    @override_settings(DEBUG=True)
    async def test_unstored_messages_raise_in_debug(self):
        """
        If storage reports unstored messages and DEBUG is True, the async
        path raises ``ValueError`` just like the sync ``process_response``.
        """

        async def get_response(request):
            request._messages = _UnstoredStorage(request)
            return HttpResponse()

        middleware = MessageMiddleware(get_response)
        request = self.async_request_factory.get("/")

        with self.assertRaisesMessage(
            ValueError, "Not all temporary messages could be stored."
        ):
            await middleware(request)

    async def test_unstored_messages_silent_outside_debug(self):
        """
        Without DEBUG, unstored messages do not raise on the async path.
        """

        async def get_response(request):
            request._messages = _UnstoredStorage(request)
            return HttpResponse()

        middleware = MessageMiddleware(get_response)
        request = self.async_request_factory.get("/")
        response = await middleware(request)

        self.assertEqual(response.status_code, 200)
