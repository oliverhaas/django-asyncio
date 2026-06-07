from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse

from .utils import get_view_name


class XViewMiddleware:
    """
    Add an X-View header to internal HEAD requests.
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        if get_response is None:
            raise ValueError("get_response must be provided.")
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)
        super().__init__()

    def __call__(self, request):
        if self.is_async:
            return self.__acall__(request)
        return self.get_response(request)

    async def __acall__(self, request):
        return await self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        """
        If the request method is HEAD and either the IP is internal or the
        user is a logged-in staff member, return a response with an x-view
        header indicating the view function. This is used to lookup the view
        function for an arbitrary page.
        """
        if not hasattr(request, "user"):
            raise ImproperlyConfigured(
                "The XView middleware requires authentication middleware to "
                "be installed. Edit your MIDDLEWARE setting to insert "
                "'django.contrib.auth.middleware.AuthenticationMiddleware'."
            )
        if request.method == "HEAD" and (
            request.META.get("REMOTE_ADDR") in settings.INTERNAL_IPS
            or (request.user.is_active and request.user.is_staff)
        ):
            response = HttpResponse()
            response.headers["X-View"] = get_view_name(view_func)
            return response
