from django.http import JsonResponse
from django.urls import path

from . import views


def healthz(request):
    return JsonResponse({"ok": True})


urlpatterns = [
    path("healthz/", healthz),
    path("io/sync/", views.io_sync),
    path("io/async/", views.io_async),
    path("cpu/sync/", views.cpu_sync),
    path("cpu/async/", views.cpu_async),
]
