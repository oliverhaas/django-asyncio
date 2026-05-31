"""RSGI handler for Django.

RSGI is Granian's native HTTP protocol, a simpler alternative to ASGI that
collapses several per-request awaits into single calls:

- One ``await proto()`` returns the full request body (no read-body loop).
- One ``proto.response_bytes(...)`` sends the response (no chunked send loop).
- No separate ``receive`` / ``send`` callables; just a single protocol object.

The trade-off: this handler only works under Granian (or any server that
implements the RSGI protocol). It cannot replace the ASGI handler for
deployments that need ASGI-server agility.

The view layer is unchanged. Middleware, the ORM, and signals behave exactly
as under ASGI; the only difference is the protocol adapter between the
network and ``BaseHandler.get_response_async``.
"""

import io
import logging
import sys
import traceback
from collections import defaultdict
from contextlib import aclosing

from asgiref.sync import ThreadSensitiveContext

from django.conf import settings
from django.core import signals
from django.core.exceptions import RequestDataTooBig
from django.core.handlers import base
from django.http import (
    FileResponse,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseServerError,
    QueryDict,
    parse_cookie,
)
from django.urls import set_script_prefix
from django.utils.functional import cached_property

logger = logging.getLogger("django.request")


def _script_prefix(scope):
    if settings.FORCE_SCRIPT_NAME:
        return settings.FORCE_SCRIPT_NAME
    # RSGI scopes don't carry a root_path; mirror ASGI's empty default.
    return ""


class RSGIRequest(HttpRequest):
    """HttpRequest built from a granian RSGI scope and a fully-read body."""

    def __init__(self, scope, body):
        self.scope = scope
        self._post_parse_error = False
        self._read_started = False
        self.resolver_match = None
        self.path = scope.path
        self.script_name = _script_prefix(scope)
        if self.script_name:
            script_name = self.script_name.rstrip("/")
            if self.path.startswith(script_name + "/") or self.path == script_name:
                self.path_info = self.path[len(script_name):]
            else:
                self.path_info = self.path
        else:
            self.path_info = self.path
        self.method = scope.method.upper()
        query_string = scope.query_string or ""
        self.META = {
            "REQUEST_METHOD": self.method,
            "QUERY_STRING": query_string,
            "SCRIPT_NAME": self.script_name,
            "PATH_INFO": self.path_info,
            "wsgi.multithread": True,
            "wsgi.multiprocess": True,
        }
        # RSGI exposes client/server as "host:port" strings (or None).
        client = getattr(scope, "client", None)
        if client:
            host, _, port = client.rpartition(":")
            self.META["REMOTE_ADDR"] = host or client
            self.META["REMOTE_HOST"] = self.META["REMOTE_ADDR"]
            if port:
                self.META["REMOTE_PORT"] = port
        server = getattr(scope, "server", None)
        if server:
            host, _, port = server.rpartition(":")
            self.META["SERVER_NAME"] = host or server
            self.META["SERVER_PORT"] = port or "0"
        else:
            self.META["SERVER_NAME"] = "unknown"
            self.META["SERVER_PORT"] = "0"
        # Headers. RSGIHeaders.items() yields (str, str). Mirror ASGI's
        # underscore-spoof guard and Cookie joining.
        _headers = defaultdict(list)
        for name, value in scope.headers.items():
            name = name.lower()
            if "_" in name:
                continue
            if name == "content-length":
                corrected_name = "CONTENT_LENGTH"
            elif name == "content-type":
                corrected_name = "CONTENT_TYPE"
            else:
                corrected_name = "HTTP_%s" % name.upper().replace("-", "_")
            if corrected_name == "HTTP_COOKIE":
                value = value.rstrip("; ")
            _headers[corrected_name].append(value)
        if cookie_header := _headers.pop("HTTP_COOKIE", None):
            self.META["HTTP_COOKIE"] = "; ".join(cookie_header)
        self.META.update({name: ",".join(value) for name, value in _headers.items()})
        self._set_content_type_params(self.META)
        # Stash body as an in-memory stream. Body is already fully collected
        # by granian, so unlike ASGI we don't need a SpooledTemporaryFile.
        self._stream = io.BytesIO(body)
        self._body = body
        self.resolver_match = None

    @cached_property
    def GET(self):
        return QueryDict(self.META["QUERY_STRING"])

    def _get_scheme(self):
        return getattr(self.scope, "scheme", None) or super()._get_scheme()

    def _get_post(self):
        if not hasattr(self, "_post"):
            self._load_post_and_files()
        return self._post

    def _set_post(self, post):
        self._post = post

    def _get_files(self):
        if not hasattr(self, "_files"):
            self._load_post_and_files()
        return self._files

    POST = property(_get_post, _set_post)
    FILES = property(_get_files)

    @cached_property
    def COOKIES(self):
        return parse_cookie(self.META.get("HTTP_COOKIE", ""))

    def close(self):
        super().close()
        self._stream.close()


class RSGIHandler(base.BaseHandler):
    """Handler for RSGI (Granian-native) requests."""

    request_class = RSGIRequest
    # Stream-response chunk size (bytes per send_bytes call).
    chunk_size = 2**16

    def __init__(self):
        super().__init__()
        self.load_middleware(is_async=True)

    async def __rsgi__(self, scope, proto):
        if scope.proto != "http":
            raise ValueError(
                f"Django's RSGI handler only handles HTTP, not {scope.proto!r}."
            )
        if settings.ASGI_THREAD_SENSITIVE:
            async with ThreadSensitiveContext():
                await self._handle(scope, proto)
        else:
            # Skipping the ThreadSensitiveContext is safe for stacks that
            # never call sync_to_async(thread_sensitive=True). Avoids one
            # context manager (with two awaits) per request.
            await self._handle(scope, proto)

    async def _handle(self, scope, proto):
        # Collect the full body in one await. Granian has the entire body
        # buffered server-side when __rsgi__ is dispatched.
        body = await proto()
        set_script_prefix(_script_prefix(scope))
        await signals.request_started.asend(sender=self.__class__, scope=scope)
        request, error_response = self._create_request(scope, body)
        if request is None:
            await self._send(error_response, proto)
            await error_response.aclose()
            return
        response = await self.get_response_async(request)
        response._handler_class = self.__class__
        if isinstance(response, FileResponse):
            response.block_size = self.chunk_size
        try:
            await self._send(response, proto)
        finally:
            await response.aclose()

    def _create_request(self, scope, body):
        try:
            return self.request_class(scope, body), None
        except UnicodeDecodeError:
            logger.warning(
                "Bad Request (UnicodeDecodeError)",
                exc_info=sys.exc_info(),
                extra={"status_code": 400},
            )
            return None, HttpResponseBadRequest()
        except RequestDataTooBig:
            return None, HttpResponse("413 Payload too large", status=413)

    def handle_uncaught_exception(self, request, resolver, exc_info):
        try:
            return super().handle_uncaught_exception(request, resolver, exc_info)
        except Exception:
            return HttpResponseServerError(
                traceback.format_exc() if settings.DEBUG else "Internal Server Error",
                content_type="text/plain",
            )

    async def _send(self, response, proto):
        """Encode and dispatch the response via RSGI."""
        headers = []
        for header, value in response.items():
            if isinstance(header, bytes):
                header = header.decode("ascii")
            if isinstance(value, bytes):
                value = value.decode("latin1")
            headers.append((header, value))
        for c in response.cookies.values():
            headers.append(("Set-Cookie", c.OutputString()))
        status = response.status_code
        if response.streaming:
            transport = proto.response_stream(status, headers)
            async with aclosing(aiter(response)) as parts:
                async for part in parts:
                    if isinstance(part, str):
                        await transport.send_str(part)
                    elif part:
                        # Chunk only if larger than chunk_size; small chunks
                        # pass through unchanged to keep boundary count low.
                        if len(part) <= self.chunk_size:
                            await transport.send_bytes(bytes(part))
                        else:
                            for offset in range(0, len(part), self.chunk_size):
                                await transport.send_bytes(
                                    bytes(part[offset:offset + self.chunk_size])
                                )
        else:
            content = response.content
            if isinstance(content, str):
                proto.response_str(status, headers, content)
            else:
                proto.response_bytes(status, headers, bytes(content))
