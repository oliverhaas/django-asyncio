from http import cookies

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore as DatabaseSession
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.sessions.models import Session
from django.http import HttpResponse
from django.test import AsyncRequestFactory, TestCase, override_settings


class AsyncSessionMiddlewareTests(TestCase):
    """Cover SessionMiddleware on the native async path (__acall__)."""

    request_factory = AsyncRequestFactory()

    @staticmethod
    async def get_response_touching_session(request):
        await request.session.aset("hello", "world")
        return HttpResponse("Session test")

    async def test_session_loaded_from_cookie_and_saved(self):
        # Seed a session row to be loaded from the cookie.
        seed = DatabaseSession()
        await seed.aset("greeting", "hi")
        await seed.asave()
        session_key = seed.session_key

        async def view(request):
            value = await request.session.aget("greeting")
            self.assertEqual(value, "hi")
            await request.session.aset("greeting", "bye")
            return HttpResponse("ok")

        request = self.request_factory.get("/")
        request.COOKIES[settings.SESSION_COOKIE_NAME] = session_key

        middleware = SessionMiddleware(view)
        response = await middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Vary"], "Cookie")
        # The same session key is reused and saved with the new value.
        self.assertIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertEqual(
            response.cookies[settings.SESSION_COOKIE_NAME].value, session_key
        )
        reloaded = DatabaseSession(session_key)
        self.assertEqual(await reloaded.aget("greeting"), "bye")

    async def test_session_saved_creates_cookie_on_response(self):
        request = self.request_factory.get("/")
        middleware = SessionMiddleware(self.get_response_touching_session)
        response = await middleware(request)

        self.assertEqual(response.headers["Vary"], "Cookie")
        self.assertIn(settings.SESSION_COOKIE_NAME, response.cookies)
        session_key = response.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertGreater(len(session_key), 8)
        # Persisted to the backend.
        stored = await Session.objects.aget(session_key=session_key)
        self.assertEqual(stored.get_decoded(), {"hello": "world"})

    async def test_session_cookie_deleted_when_emptied(self):
        async def response_ending_session(request):
            await request.session.aflush()
            return HttpResponse("Session test")

        request = self.request_factory.get("/")
        # Pretend the client already had a session cookie.
        request.COOKIES[settings.SESSION_COOKIE_NAME] = "abc"

        middleware = SessionMiddleware(response_ending_session)
        response = await middleware(request)

        morsel = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertEqual(morsel.value, "")
        # The Set-Cookie clears the cookie (Max-Age=0, epoch expiry).
        cookie_str = str(morsel)
        self.assertIn("Max-Age=0", cookie_str)
        self.assertIn("expires=Thu, 01 Jan 1970 00:00:00 GMT", cookie_str)
        self.assertEqual(response.headers["Vary"], "Cookie")

    async def test_session_update_error_raises_session_interrupted(self):
        async def response_delete_session(request):
            request.session = DatabaseSession()
            await request.session.asave(must_create=True)
            await request.session.adelete()
            return HttpResponse()

        request = self.request_factory.get("/foo/")
        middleware = SessionMiddleware(response_delete_session)

        msg = (
            "The request's session was deleted before the request completed. "
            "The user may have logged out in a concurrent request, for example."
        )
        with self.assertRaisesMessage(SessionInterrupted, msg):
            await middleware(request)

    async def test_session_not_saved_on_5xx(self):
        async def response_500(request):
            response = HttpResponse("Horrible error")
            response.status_code = 500
            await request.session.aset("hello", "world")
            return response

        request = self.request_factory.get("/")
        await SessionMiddleware(response_500)(request)

        # The value wasn't persisted to the backend.
        reloaded = DatabaseSession(request.session.session_key)
        self.assertNotIn("hello", await reloaded.aload())

    @override_settings(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True)
    async def test_cookie_flags_applied_on_async_path(self):
        request = self.request_factory.get("/")
        middleware = SessionMiddleware(self.get_response_touching_session)
        response = await middleware(request)

        morsel = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertIs(morsel["secure"], True)
        self.assertIn(cookies.Morsel._reserved["httponly"], str(morsel))
