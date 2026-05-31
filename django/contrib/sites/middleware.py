from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from .shortcuts import aget_current_site, get_current_site


class CurrentSiteMiddleware:
    """
    Middleware that sets `site` attribute to request object.
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
        return self.get_response(request)

    async def __acall__(self, request):
        await self._aprocess_request(request)
        return await self.get_response(request)

    def process_request(self, request):
        request.site = get_current_site(request)

    async def _aprocess_request(self, request):
        request.site = await aget_current_site(request)
