from asgiref.sync import iscoroutinefunction

from django.http import HttpResponse
from django.middleware.http import ConditionalGetMiddleware
from django.test import AsyncRequestFactory, SimpleTestCase


class ConditionalGetMiddlewareAsyncTest(SimpleTestCase):
    """Native async coverage for ConditionalGetMiddleware."""

    request_factory = AsyncRequestFactory()

    def setUp(self):
        self.req = self.request_factory.get("/")

    def _make_get_response(self, headers=None, status_code=200, body=b"content"):
        resp_headers = headers or {}

        async def get_response(request):
            response = HttpResponse(body, status=status_code)
            for key, value in resp_headers.items():
                response[key] = value
            return response

        return get_response

    async def test_middleware_marks_itself_async_for_async_get_response(self):
        middleware = ConditionalGetMiddleware(self._make_get_response())
        self.assertTrue(iscoroutinefunction(middleware))

    async def test_middleware_calculates_etag(self):
        middleware = ConditionalGetMiddleware(self._make_get_response())
        resp = await middleware(self.req)
        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual("", resp["ETag"])

    async def test_if_none_match_and_same_etag(self):
        self.req.META["HTTP_IF_NONE_MATCH"] = '"spam"'
        middleware = ConditionalGetMiddleware(
            self._make_get_response(headers={"ETag": '"spam"'})
        )
        resp = await middleware(self.req)
        self.assertEqual(resp.status_code, 304)

    async def test_if_modified_since_and_same_last_modified(self):
        self.req.META["HTTP_IF_MODIFIED_SINCE"] = "Sat, 12 Feb 2011 17:38:44 GMT"
        middleware = ConditionalGetMiddleware(
            self._make_get_response(
                headers={"Last-Modified": "Sat, 12 Feb 2011 17:38:44 GMT"}
            )
        )
        resp = await middleware(self.req)
        self.assertEqual(resp.status_code, 304)
