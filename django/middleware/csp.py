from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from django.conf import settings
from django.utils.csp import CSP, LazyNonce, build_policy


def get_nonce(request):
    return getattr(request, "_csp_nonce", None)


class ContentSecurityPolicyMiddleware:
    """
    Add Content-Security-Policy and Content-Security-Policy-Report-Only
    headers to responses based on the SECURE_CSP and SECURE_CSP_REPORT_ONLY
    settings.
    """

    async_capable = True
    sync_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(get_response):
            markcoroutinefunction(self)

    def __call__(self, request):
        if iscoroutinefunction(self):
            return self.__acall__(request)
        self._process_request(request)
        response = self.get_response(request)
        return self._process_response(request, response)

    async def __acall__(self, request):
        self._process_request(request)
        response = await self.get_response(request)
        return self._process_response(request, response)

    def _process_request(self, request):
        request._csp_nonce = LazyNonce()

    def _process_response(self, request, response):
        nonce = get_nonce(request)

        sentinel = object()
        if (csp_config := getattr(response, "_csp_config", sentinel)) is sentinel:
            csp_config = settings.SECURE_CSP
        if (csp_ro_config := getattr(response, "_csp_ro_config", sentinel)) is sentinel:
            csp_ro_config = settings.SECURE_CSP_REPORT_ONLY

        for header, config in [
            (CSP.HEADER_ENFORCE, csp_config),
            (CSP.HEADER_REPORT_ONLY, csp_ro_config),
        ]:
            # If headers are already set on the response, don't overwrite them.
            # This allows for views to set their own CSP headers as needed.
            # An empty config means CSP headers are not added to the response.
            if config and header not in response:
                response.headers[str(header)] = build_policy(config, nonce)

        return response
