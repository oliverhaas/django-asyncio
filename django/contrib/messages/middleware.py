from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async

from django.conf import settings
from django.contrib.messages.storage import default_storage


class MessageMiddleware:
    """
    Middleware that handles temporary messages.
    """

    async_capable = True
    sync_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(get_response):
            markcoroutinefunction(self)

    def __call__(self, request):
        if iscoroutinefunction(self):
            return self.__acall__(request)
        self.process_request(request)
        response = self.get_response(request)
        return self.process_response(request, response)

    async def __acall__(self, request):
        self.process_request(request)
        response = await self.get_response(request)
        return await self._aprocess_response(request, response)

    def process_request(self, request):
        request._messages = default_storage(request)

    def process_response(self, request, response):
        """
        Update the storage backend (i.e., save the messages).

        Raise ValueError if not all messages could be stored and DEBUG is True.
        """
        # A higher middleware layer may return a request which does not contain
        # messages storage, so make no assumption that it will be there.
        if hasattr(request, "_messages"):
            unstored_messages = request._messages.update(response)
            if unstored_messages and settings.DEBUG:
                raise ValueError("Not all temporary messages could be stored.")
        return response

    async def _aprocess_response(self, request, response):
        """
        Async mirror of ``process_response``.

        Storage backends in this fork do not yet expose an ``aupdate``; fall
        back to ``sync_to_async`` for that single call. The lazy storage
        attach in ``process_request`` is pure CPU and stays synchronous.
        """
        if hasattr(request, "_messages"):
            storage = request._messages
            aupdate = getattr(storage, "aupdate", None)
            if aupdate is not None:
                unstored_messages = await aupdate(response)
            else:
                unstored_messages = await sync_to_async(
                    storage.update, thread_sensitive=True
                )(response)
            if unstored_messages and settings.DEBUG:
                raise ValueError("Not all temporary messages could be stored.")
        return response
