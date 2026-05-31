"""
Cache middleware. If enabled, each Django-powered page will be cached based on
URL. The canonical way to enable cache middleware is to set
``UpdateCacheMiddleware`` as your first piece of middleware, and
``FetchFromCacheMiddleware`` as the last::

    MIDDLEWARE = [
        'django.middleware.cache.UpdateCacheMiddleware',
        ...
        'django.middleware.cache.FetchFromCacheMiddleware'
    ]

This is counterintuitive, but correct: ``UpdateCacheMiddleware`` needs to run
last during the response phase, which processes middleware bottom-up;
``FetchFromCacheMiddleware`` needs to run last during the request phase, which
processes middleware top-down.

The single-class ``CacheMiddleware`` can be used for some simple sites.
However, if any other piece of middleware needs to affect the cache key, you'll
need to use the two-part ``UpdateCacheMiddleware`` and
``FetchFromCacheMiddleware``. This'll most often happen when you're using
Django's ``LocaleMiddleware``.

More details about how the caching works:

* Only GET or HEAD-requests with status code 200 are cached.

* The number of seconds each page is stored for is set by the "max-age" section
  of the response's "Cache-Control" header, falling back to the
  CACHE_MIDDLEWARE_SECONDS setting if the section was not found.

* This middleware expects that a HEAD request is answered with the same
  response headers exactly like the corresponding GET request.

* When a hit occurs, a shallow copy of the original response object is returned
  from process_request.

* Pages will be cached based on the contents of the request headers listed in
  the response's "Vary" header.

* This middleware also sets ETag, Last-Modified, Expires and Cache-Control
  headers on the response object.

"""

import time

from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from django.conf import settings
from django.core.cache import DEFAULT_CACHE_ALIAS, caches
from django.utils.cache import (
    _generate_cache_header_key,
    _generate_cache_key,
    cc_delim_re,
    get_cache_key,
    get_max_age,
    has_vary_header,
    learn_cache_key,
    patch_response_headers,
)
from django.utils.http import parse_http_date_safe


async def _alearn_cache_key(request, response, cache_timeout, key_prefix, cache):
    """
    Async mirror of :func:`django.utils.cache.learn_cache_key`. Uses
    ``cache.aset`` so the response phase never hops to a thread pool.
    """
    if key_prefix is None:
        key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
    if cache_timeout is None:
        cache_timeout = settings.CACHE_MIDDLEWARE_SECONDS
    cache_key = _generate_cache_header_key(key_prefix, request)
    if cache is None:
        cache = caches[settings.CACHE_MIDDLEWARE_ALIAS]
    if response.has_header("Vary"):
        is_accept_language_redundant = settings.USE_I18N
        # If i18n is used, the generated cache key will be suffixed with the
        # current locale. Adding the raw value of Accept-Language is redundant
        # in that case and would result in storing the same content under
        # multiple keys in the cache. See #18191 for details.
        headerlist = []
        for header in cc_delim_re.split(response.headers["Vary"]):
            header = header.upper().replace("-", "_")
            if header != "ACCEPT_LANGUAGE" or not is_accept_language_redundant:
                headerlist.append("HTTP_" + header)
        headerlist.sort()
        await cache.aset(cache_key, headerlist, cache_timeout)
        return _generate_cache_key(request, request.method, headerlist, key_prefix)
    else:
        # If there is no Vary header, we still need a cache key for the
        # request.build_absolute_uri().
        await cache.aset(cache_key, [], cache_timeout)
        return _generate_cache_key(request, request.method, [], key_prefix)


async def _aget_cache_key(request, key_prefix, method, cache):
    """
    Async mirror of :func:`django.utils.cache.get_cache_key`. Uses
    ``cache.aget`` to look up the header list registered for the URL.
    """
    if key_prefix is None:
        key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
    cache_key = _generate_cache_header_key(key_prefix, request)
    if cache is None:
        cache = caches[settings.CACHE_MIDDLEWARE_ALIAS]
    headerlist = await cache.aget(cache_key)
    if headerlist is not None:
        return _generate_cache_key(request, method, headerlist, key_prefix)
    return None


class UpdateCacheMiddleware:
    """
    Response-phase cache middleware that updates the cache if the response is
    cacheable.

    Must be used as part of the two-part update/fetch cache middleware.
    UpdateCacheMiddleware must be the first piece of middleware in MIDDLEWARE
    so that it'll get called last during the response phase.
    """

    async_capable = True
    sync_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(get_response):
            markcoroutinefunction(self)
        self.cache_timeout = settings.CACHE_MIDDLEWARE_SECONDS
        self.page_timeout = None
        self.key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
        self.cache_alias = settings.CACHE_MIDDLEWARE_ALIAS

    def __call__(self, request):
        if iscoroutinefunction(self):
            return self.__acall__(request)
        response = self.get_response(request)
        return self.process_response(request, response)

    async def __acall__(self, request):
        response = await self.get_response(request)
        return await self._aprocess_response(request, response)

    @property
    def cache(self):
        return caches[self.cache_alias]

    def _should_update_cache(self, request, response):
        return hasattr(request, "_cache_update_cache") and request._cache_update_cache

    def _resolved_timeout(self, response):
        """
        Compute the cache timeout for ``response`` or return a sentinel that
        signals the response must not be cached.

        Returns the resolved timeout (int/float seconds), or ``None`` to mean
        "do not cache" (used both for max-age=0 and for early-bail conditions
        captured by the caller).
        """
        timeout = self.page_timeout
        if timeout is None:
            # The timeout from the "max-age" section of the "Cache-Control"
            # header takes precedence over the default cache timeout.
            timeout = get_max_age(response)
            if timeout is None:
                timeout = self.cache_timeout
            elif timeout == 0:
                # max-age was set to 0, don't cache.
                return None
        return timeout

    def _is_cacheable(self, request, response):
        """Return True if the response should be written to the cache."""
        if not self._should_update_cache(request, response):
            return False
        if response.streaming or response.status_code not in (200, 304):
            return False
        # Don't cache responses that set a user-specific (and maybe security
        # sensitive) cookie in response to a cookie-less request.
        if (
            not request.COOKIES
            and response.cookies
            and has_vary_header(response, "Cookie")
        ):
            return False
        # Don't cache responses when the Cache-Control header is set to
        # private, no-cache, or no-store.
        cache_control = response.get("Cache-Control", ())
        if any(
            directive in cache_control
            for directive in ("private", "no-cache", "no-store")
        ):
            return False
        # Don't cache responses when the Vary header contains '*'.
        if has_vary_header(response, "*"):
            return False
        return True

    def process_response(self, request, response):
        """Set the cache, if needed."""
        if not self._is_cacheable(request, response):
            return response

        timeout = self._resolved_timeout(response)
        if timeout is None:
            return response
        patch_response_headers(response, timeout)
        if timeout and response.status_code == 200:
            cache_key = learn_cache_key(
                request, response, timeout, self.key_prefix, cache=self.cache
            )
            if hasattr(response, "render") and callable(response.render):
                response.add_post_render_callback(
                    lambda r: self.cache.set(cache_key, r, timeout)
                )
            else:
                self.cache.set(cache_key, response, timeout)
        return response

    async def _aprocess_response(self, request, response):
        """Async mirror of :meth:`process_response`."""
        if not self._is_cacheable(request, response):
            return response

        timeout = self._resolved_timeout(response)
        if timeout is None:
            return response
        patch_response_headers(response, timeout)
        if timeout and response.status_code == 200:
            cache = self.cache
            cache_key = await _alearn_cache_key(
                request, response, timeout, self.key_prefix, cache
            )
            if hasattr(response, "render") and callable(response.render):
                # TemplateResponse.add_post_render_callback only accepts sync
                # callables; schedule a sync set on render completion. This
                # path is rare in async views.
                response.add_post_render_callback(
                    lambda r: cache.set(cache_key, r, timeout)
                )
            else:
                await cache.aset(cache_key, response, timeout)
        return response


class FetchFromCacheMiddleware:
    """
    Request-phase cache middleware that fetches a page from the cache.

    Must be used as part of the two-part update/fetch cache middleware.
    FetchFromCacheMiddleware must be the last piece of middleware in MIDDLEWARE
    so that it'll get called last during the request phase.
    """

    async_capable = True
    sync_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(get_response):
            markcoroutinefunction(self)
        self.key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
        self.cache_alias = settings.CACHE_MIDDLEWARE_ALIAS

    def __call__(self, request):
        if iscoroutinefunction(self):
            return self.__acall__(request)
        cached = self.process_request(request)
        if cached is not None:
            return cached
        return self.get_response(request)

    async def __acall__(self, request):
        cached = await self._aprocess_request(request)
        if cached is not None:
            return cached
        return await self.get_response(request)

    @property
    def cache(self):
        return caches[self.cache_alias]

    @staticmethod
    def _annotate_age(response):
        """
        Set the ``Age`` header on a cached response based on its ``Expires``
        and ``Cache-Control: max-age``. Mutates ``response`` in place.
        """
        if (max_age_seconds := get_max_age(response)) is not None and (
            expires_timestamp := parse_http_date_safe(response["Expires"])
        ) is not None:
            now_timestamp = int(time.time())
            remaining_seconds = expires_timestamp - now_timestamp
            # Use Age: 0 if local clock got turned back.
            response["Age"] = max(0, max_age_seconds - remaining_seconds)

    def process_request(self, request):
        """
        Check whether the page is already cached and return the cached
        version if available.
        """
        if request.method not in ("GET", "HEAD"):
            request._cache_update_cache = False
            return None  # Don't bother checking the cache.

        # Try and get the cached GET response.
        cache_key = get_cache_key(request, self.key_prefix, "GET", cache=self.cache)
        if cache_key is None:
            request._cache_update_cache = True
            return None  # No cache information available, need to rebuild.
        response = self.cache.get(cache_key)
        # If it wasn't found and we are looking for a HEAD, try looking just
        # for that.
        if response is None and request.method == "HEAD":
            cache_key = get_cache_key(
                request, self.key_prefix, "HEAD", cache=self.cache
            )
            response = self.cache.get(cache_key)

        if response is None:
            request._cache_update_cache = True
            return None  # No cache information available, need to rebuild.

        # Derive the age estimation of the cached response.
        self._annotate_age(response)

        # Hit, return cached response.
        request._cache_update_cache = False
        return response

    async def _aprocess_request(self, request):
        """Async mirror of :meth:`process_request`."""
        if request.method not in ("GET", "HEAD"):
            request._cache_update_cache = False
            return None

        cache = self.cache
        cache_key = await _aget_cache_key(request, self.key_prefix, "GET", cache)
        if cache_key is None:
            request._cache_update_cache = True
            return None
        response = await cache.aget(cache_key)
        if response is None and request.method == "HEAD":
            cache_key = await _aget_cache_key(
                request, self.key_prefix, "HEAD", cache
            )
            response = await cache.aget(cache_key)

        if response is None:
            request._cache_update_cache = True
            return None

        self._annotate_age(response)

        request._cache_update_cache = False
        return response


class CacheMiddleware(UpdateCacheMiddleware, FetchFromCacheMiddleware):
    """
    Cache middleware that provides basic behavior for many simple sites.

    Also used as the hook point for the cache decorator, which is generated
    using the decorator-from-middleware utility.
    """

    def __init__(self, get_response, cache_timeout=None, page_timeout=None, **kwargs):
        super().__init__(get_response)
        # We need to differentiate between "provided, but using default value",
        # and "not provided". If the value is provided using a default, then
        # we fall back to system defaults. If it is not provided at all,
        # we need to use middleware defaults.

        try:
            key_prefix = kwargs["key_prefix"]
            if key_prefix is None:
                key_prefix = ""
            self.key_prefix = key_prefix
        except KeyError:
            pass
        try:
            cache_alias = kwargs["cache_alias"]
            if cache_alias is None:
                cache_alias = DEFAULT_CACHE_ALIAS
            self.cache_alias = cache_alias
        except KeyError:
            pass

        if cache_timeout is not None:
            self.cache_timeout = cache_timeout
        self.page_timeout = page_timeout

    def __call__(self, request):
        if iscoroutinefunction(self):
            return self.__acall__(request)
        cached = self.process_request(request)
        if cached is not None:
            return cached
        response = self.get_response(request)
        return self.process_response(request, response)

    async def __acall__(self, request):
        cached = await self._aprocess_request(request)
        if cached is not None:
            return cached
        response = await self.get_response(request)
        return await self._aprocess_response(request, response)
