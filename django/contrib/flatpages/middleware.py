from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from django.conf import settings
from django.contrib.flatpages.views import flatpage, render_flatpage
from django.contrib.flatpages.models import FlatPage
from django.contrib.sites.shortcuts import aget_current_site
from django.http import Http404, HttpResponsePermanentRedirect


class FlatpageFallbackMiddleware:
    async_capable = True
    sync_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(get_response):
            markcoroutinefunction(self)

    def __call__(self, request):
        if iscoroutinefunction(self):
            return self.__acall__(request)
        response = self.get_response(request)
        return self.process_response(request, response)

    async def __acall__(self, request):
        response = await self.get_response(request)
        return await self._aprocess_response(request, response)

    def process_response(self, request, response):
        if response.status_code != 404:
            return response  # No need to check for a flatpage for non-404 responses.
        try:
            return flatpage(request, request.path_info)
        # Return the original response if any errors happened. Because this
        # is a middleware, we can't assume the errors will be caught elsewhere.
        except Http404:
            return response
        except Exception:
            if settings.DEBUG:
                raise
            return response

    async def _aprocess_response(self, request, response):
        if response.status_code != 404:
            return response  # No need to check for a flatpage for non-404 responses.
        try:
            return await self._aflatpage(request, request.path_info)
        # Return the original response if any errors happened. Because this
        # is a middleware, we can't assume the errors will be caught elsewhere.
        except Http404:
            return response
        except Exception:
            if settings.DEBUG:
                raise
            return response

    async def _aflatpage(self, request, url):
        """
        Async mirror of django.contrib.flatpages.views.flatpage using aget on
        the ORM. Template rendering is CPU-bound and stays sync.
        """
        if not url.startswith("/"):
            url = "/" + url
        site_id = (await aget_current_site(request)).id
        try:
            f = await FlatPage.objects.aget(url=url, sites=site_id)
        except FlatPage.DoesNotExist:
            if not url.endswith("/") and settings.APPEND_SLASH:
                url += "/"
                # If this lookup also misses, DoesNotExist propagates and is
                # converted to Http404 below.
                try:
                    f = await FlatPage.objects.aget(url=url, sites=site_id)
                except FlatPage.DoesNotExist:
                    raise Http404
                return HttpResponsePermanentRedirect("%s/" % request.path)
            else:
                raise Http404
        return render_flatpage(request, f)
