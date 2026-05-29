from django.http import JsonResponse
from django.urls import path

from . import views


async def healthz(request):
    # Async so it doesn't trip the sync_to_async tripwire: a sync view would be
    # wrapped by the async handler, polluting the full-async verification.
    return JsonResponse({"ok": True})


urlpatterns = [
    path("healthz/", healthz),
    path("io/sync/", views.io_sync),
    path("io/async/", views.io_async),
    path("cpu/sync/", views.cpu_sync),
    path("cpu/async/", views.cpu_async),
    path("db/sync/", views.db_sync),
    path("db/async/", views.db_async),
    path("db_heavy/sync/", views.db_heavy_sync),
    path("db_heavy/async/", views.db_heavy_async),
]
