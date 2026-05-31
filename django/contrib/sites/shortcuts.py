from django.apps import apps

from .requests import RequestSite


def get_current_site(request):
    """
    Check if contrib.sites is installed and return either the current
    ``Site`` object or a ``RequestSite`` object based on the request.
    """
    # Import is inside the function because its point is to avoid importing the
    # Site models when django.contrib.sites isn't installed.
    if apps.is_installed("django.contrib.sites"):
        from .models import Site

        return Site.objects.get_current(request)
    else:
        return RequestSite(request)


async def aget_current_site(request):
    """
    Async variant of ``get_current_site``.

    Returns the current ``Site`` (via the native async ORM) when
    ``django.contrib.sites`` is installed, or a ``RequestSite`` otherwise.
    """
    if apps.is_installed("django.contrib.sites"):
        from .models import Site

        return await Site.objects.aget_current(request)
    else:
        return RequestSite(request)
