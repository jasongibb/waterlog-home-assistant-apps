"""Bounded JSON-over-HTTP transport with redirects deliberately disabled."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any

from .models import HttpResponse


MAX_RESPONSE_BYTES = 1_048_576


class TransportError(ConnectionError):
    """A request failed before an HTTP response was available."""


class ResponseTooLargeError(TransportError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class HttpTransport:
    """Minimal injectable transport; it never logs headers or request bodies."""

    def __init__(self) -> None:
        # Home Assistant does not inject a user-configurable proxy. Ignore ambient
        # proxy variables so neither the Supervisor nor Waterlog bearer token can
        # be forwarded to an unexpected proxy endpoint.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def get(self, url: str, *, headers: dict[str, str], timeout: int) -> HttpResponse:
        request = urllib.request.Request(url, headers=headers, method="GET")
        return self._open(request, timeout)

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: int,
    ) -> HttpResponse:
        body = json.dumps(
            payload, allow_nan=False, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request_headers = {"Content-Type": "application/json", **headers}
        request = urllib.request.Request(
            url, data=body, headers=request_headers, method="POST"
        )
        return self._open(request, timeout)

    def _open(self, request: urllib.request.Request, timeout: int) -> HttpResponse:
        try:
            with self._opener.open(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ResponseTooLargeError("HTTP response exceeded the size limit")
                return HttpResponse(
                    status=int(response.status),
                    headers={
                        key.lower(): value for key, value in response.headers.items()
                    },
                    body=body,
                )
        except urllib.error.HTTPError as error:
            body = error.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ResponseTooLargeError(
                    "HTTP error response exceeded the size limit"
                )
            return HttpResponse(
                status=int(error.code),
                headers={key.lower(): value for key, value in error.headers.items()},
                body=body,
            )
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
        ) as error:
            raise TransportError("HTTP request failed") from error
