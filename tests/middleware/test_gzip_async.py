import zlib

from django.http import HttpResponse, StreamingHttpResponse
from django.middleware.gzip import GZipMiddleware
from django.test import AsyncRequestFactory, SimpleTestCase


def _decompress(gzipped_string):
    # Use zlib to ensure gzipped_string contains exactly one gzip stream.
    return zlib.decompress(gzipped_string, zlib.MAX_WBITS | 16)


class GZipMiddlewareAsyncTest(SimpleTestCase):
    """
    GZipMiddleware works as a native hybrid (no MiddlewareMixin) under ASGI.
    """

    compressible_string = b"a" * 500
    sequence = [b"a" * 500, b"b" * 200, b"a" * 300]
    request_factory = AsyncRequestFactory()

    def setUp(self):
        self.req = self.request_factory.get("/")
        self.req.META["HTTP_ACCEPT_ENCODING"] = "gzip, deflate"

    async def test_compress_response_async(self):
        """
        Compression is performed on a non-streaming response via __acall__.
        """

        async def get_response(request):
            resp = HttpResponse(self.compressible_string)
            resp["Content-Type"] = "text/html; charset=UTF-8"
            return resp

        middleware = GZipMiddleware(get_response)
        r = await middleware(self.req)

        self.assertEqual(_decompress(r.content), self.compressible_string)
        self.assertEqual(r.get("Content-Encoding"), "gzip")
        self.assertEqual(r.get("Content-Length"), str(len(r.content)))

    async def test_no_compress_when_accept_encoding_missing_async(self):
        """
        Without a gzip Accept-Encoding header the response is left untouched.
        """
        req = self.request_factory.get("/")  # No HTTP_ACCEPT_ENCODING.

        async def get_response(request):
            return HttpResponse(self.compressible_string)

        middleware = GZipMiddleware(get_response)
        r = await middleware(req)

        self.assertEqual(r.content, self.compressible_string)
        self.assertIsNone(r.get("Content-Encoding"))

    async def test_compress_async_streaming_response(self):
        """
        Async streaming responses are compressed via acompress_sequence.
        """

        async def get_response(request):
            async def iterator():
                for chunk in self.sequence:
                    yield chunk

            resp = StreamingHttpResponse(iterator())
            resp["Content-Type"] = "text/html; charset=UTF-8"
            return resp

        middleware = GZipMiddleware(get_response)
        r = await middleware(self.req)

        body = b"".join([chunk async for chunk in r])
        self.assertEqual(_decompress(body), b"".join(self.sequence))
        self.assertEqual(r.get("Content-Encoding"), "gzip")
        self.assertFalse(r.has_header("Content-Length"))

    def test_sync_call_path_still_works(self):
        """
        With a sync get_response the middleware uses the sync __call__ path.
        """

        def get_response(request):
            return HttpResponse(self.compressible_string)

        sync_req = self.request_factory.get("/")
        sync_req.META["HTTP_ACCEPT_ENCODING"] = "gzip, deflate"

        middleware = GZipMiddleware(get_response)
        r = middleware(sync_req)

        self.assertEqual(_decompress(r.content), self.compressible_string)
        self.assertEqual(r.get("Content-Encoding"), "gzip")
