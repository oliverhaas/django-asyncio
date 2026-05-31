from asgiref.sync import iscoroutinefunction

from django.conf import settings
from django.contrib.auth.middleware import (
    AuthenticationMiddleware,
    LoginRequiredMiddleware,
    PersistentRemoteUserMiddleware,
    RemoteUserMiddleware,
)
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import AsyncRequestFactory, TestCase, modify_settings, override_settings


async def _async_get_response(request):
    return HttpResponse()


def _sync_get_response(request):
    return HttpResponse()


class TestAuthenticationMiddlewareAsync(TestCase):
    """
    Native async dispatch path for AuthenticationMiddleware: __acall__ runs
    process_request inline (no sync_to_async hop) and awaits get_response.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            "test_user", "test@example.com", "test_password"
        )

    def test_sync_get_response_keeps_sync_dispatch(self):
        # Hybrid dispatch: a sync get_response keeps __call__ on the sync path.
        sync_mw = AuthenticationMiddleware(_sync_get_response)
        self.assertFalse(iscoroutinefunction(sync_mw))

    def test_async_get_response_marks_coroutine(self):
        # Hybrid dispatch: an async get_response marks the middleware instance,
        # so the ASGI handler will await __acall__ directly.
        async_mw = AuthenticationMiddleware(_async_get_response)
        self.assertTrue(iscoroutinefunction(async_mw))

    async def test_no_session_raises_on_async_path(self):
        middleware = AuthenticationMiddleware(_async_get_response)
        request = AsyncRequestFactory().get("/")
        msg = (
            "The Django authentication middleware requires session middleware "
            "to be installed. Edit your MIDDLEWARE setting to insert "
            "'django.contrib.sessions.middleware.SessionMiddleware' before "
            "'django.contrib.auth.middleware.AuthenticationMiddleware'."
        )
        with self.assertRaisesMessage(ImproperlyConfigured, msg):
            await middleware(request)

    @override_settings(ROOT_URLCONF="auth_tests.urls")
    async def test_auser_returns_anonymous_when_no_login(self):
        # End-to-end through the ASGI handler: AuthenticationMiddleware is in
        # the default test MIDDLEWARE stack, so request.user is populated.
        response = await self.async_client.get("/auth_processor_no_attr_access/")
        self.assertTrue(response.context["user"].is_anonymous)

    @override_settings(ROOT_URLCONF="auth_tests.urls")
    async def test_user_attached_when_logged_in(self):
        await self.async_client.aforce_login(self.user)
        response = await self.async_client.get("/auth_processor_user/")
        self.assertEqual(response.context["user"], self.user)


@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    MIDDLEWARE={"append": "django.contrib.auth.middleware.RemoteUserMiddleware"},
    AUTHENTICATION_BACKENDS={
        "append": "django.contrib.auth.backends.RemoteUserBackend"
    },
)
class TestRemoteUserMiddlewareAsync(TestCase):
    """REMOTE_USER header authenticates a user via the async path."""

    async def test_remote_user_header_authenticates(self):
        await User.objects.acreate(username="knownuser")
        response = await self.async_client.get(
            "/remote_user/", headers={"remote-user": "knownuser"}
        )
        self.assertEqual(response.context["user"].username, "knownuser")

    async def test_no_remote_user_header_anonymous(self):
        response = await self.async_client.get("/remote_user/")
        self.assertTrue(response.context["user"].is_anonymous)


@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    MIDDLEWARE={
        "append": "django.contrib.auth.middleware.PersistentRemoteUserMiddleware"
    },
    AUTHENTICATION_BACKENDS={
        "append": "django.contrib.auth.backends.RemoteUserBackend"
    },
)
class TestPersistentRemoteUserMiddlewareAsync(TestCase):
    """PersistentRemoteUserMiddleware inherits the async dispatch from its parent."""

    async def test_header_disappears_keeps_user(self):
        await User.objects.acreate(username="keepme")
        response = await self.async_client.get(
            "/remote_user/", headers={"remote-user": "keepme"}
        )
        self.assertEqual(response.context["user"].username, "keepme")

        # Header gone on the next request, but user stays authenticated.
        response = await self.async_client.get("/remote_user/")
        self.assertFalse(response.context["user"].is_anonymous)
        self.assertEqual(response.context["user"].username, "keepme")


@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    MIDDLEWARE={"append": "django.contrib.auth.middleware.LoginRequiredMiddleware"}
)
class TestLoginRequiredMiddlewareAsync(TestCase):
    """LoginRequiredMiddleware redirects unauthenticated requests via ASGI."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            "test_user", "test@example.com", "test_password"
        )

    async def test_protected_view_redirects_unauthenticated(self):
        response = await self.async_client.get("/protected_view/")
        self.assertEqual(response.status_code, 302)
        self.assertIn(settings.LOGIN_URL, response.headers["Location"])
        self.assertIn("next=/protected_view/", response.headers["Location"])

    async def test_public_view_allows_unauthenticated(self):
        response = await self.async_client.get("/public_view/")
        self.assertEqual(response.status_code, 200)

    async def test_protected_view_allows_authenticated(self):
        await self.async_client.aforce_login(self.user)
        response = await self.async_client.get("/protected_view/")
        self.assertEqual(response.status_code, 200)

    def test_async_get_response_marks_coroutine(self):
        # No process_request/process_response, but the hybrid scaffolding still
        # marks the instance for ASGI dispatch when get_response is async.
        mw = LoginRequiredMiddleware(_async_get_response)
        self.assertTrue(iscoroutinefunction(mw))

    def test_sync_get_response_keeps_sync_dispatch(self):
        mw = LoginRequiredMiddleware(_sync_get_response)
        self.assertFalse(iscoroutinefunction(mw))
