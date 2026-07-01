"""
Unified HTTP client with TLS fingerprint impersonation.

Uses Scrapling's Fetcher (curl_cffi) for browser-grade TLS handshake,
with automatic retry + exponential backoff + urllib fallback.

Usage:
    from UniversalExtractor.http_client import HTTPClient

    client = HTTPClient()
    resp = client.get("https://example.com")
    print(resp.text)
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)
CRAWLER_USER_AGENT = "UniversalExtractorBot/0.2 (+local-content-extraction)"


@dataclass
class HTTPResponse:
    """Unified HTTP response."""

    status_code: int = 0
    text: str = ""
    content: bytes = b""
    headers: dict = field(default_factory=dict)
    url: str = ""
    elapsed_ms: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400


class HTTPClient:
    """Unified HTTP client with TLS fingerprint + retry + fallback.

    Parameters:
        proxy: Proxy URL (e.g. "http://user:pass@host:8080")
        timeout: Request timeout in seconds
        max_retries: Max retry attempts (exponential backoff: 1s, 2s, 4s)
        impersonate: Browser to impersonate ("chrome124", "safari17", etc.)
    """

    def __init__(
        self,
        proxy: Optional[str] = None,
        timeout: int = 30,
        max_retries: int = 3,
        impersonate: str = "chrome124",
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.max_retries = max_retries
        self.impersonate = impersonate

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def get(self, url: str, headers: Optional[dict] = None) -> HTTPResponse:
        """GET request with TLS fingerprint impersonation."""
        return self._request("GET", url, headers=headers)

    def post(
        self, url: str, data: Optional[bytes] = None, headers: Optional[dict] = None
    ) -> HTTPResponse:
        """POST request with TLS fingerprint impersonation."""
        return self._request("POST", url, data=data, headers=headers)

    # ----------------------------------------------------------------
    # Internal
    # ----------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        data: Optional[bytes] = None,
        headers: Optional[dict] = None,
    ) -> HTTPResponse:
        """Core request with retry + fallback chain."""
        t0 = time.time()

        # Level 1: curl_cffi via Scrapling Fetcher (TLS impersonation)
        result = self._via_curl_cffi(method, url, data, headers)
        if result is not None:
            result.elapsed_ms = int((time.time() - t0) * 1000)
            return result

        # Level 2: urllib fallback (no TLS impersonation, but always available)
        result = self._via_urllib(method, url, data, headers)
        result.elapsed_ms = int((time.time() - t0) * 1000)
        return result

    def _via_curl_cffi(
        self, method: str, url: str, data: Optional[bytes],
        headers: Optional[dict],
    ) -> Optional[HTTPResponse]:
        """Try curl_cffi with retry + exponential backoff."""
        try:
            from scrapling import Fetcher
        except ImportError:
            return None

        last_error = None
        for attempt in range(self.max_retries):
            try:
                request_headers = {"User-Agent": CRAWLER_USER_AGENT}
                request_headers.update(headers or {})
                request_options = {
                    "headers": request_headers,
                    "timeout": self.timeout,
                    "impersonate": self.impersonate,
                    "proxy": self.proxy,
                    "follow_redirects": True,
                    "max_redirects": 5,
                    "retries": 0,
                }

                if method == "POST":
                    resp = Fetcher.post(url, data=data or b"", **request_options)
                else:
                    resp = Fetcher.get(url, **request_options)

                if resp is not None:
                    status = getattr(resp, "status", getattr(resp, "status_code", 0))
                    content = getattr(resp, "body", b"") or b""
                    if isinstance(content, str):
                        content = content.encode("utf-8")
                    raw_text = getattr(resp, "html_content", "") or ""
                    text = str(raw_text) if raw_text else content.decode(
                        getattr(resp, "encoding", None) or "utf-8",
                        errors="replace",
                    )
                    return HTTPResponse(
                        status_code=status,
                        text=text,
                        content=content,
                        headers=dict(resp.headers) if resp.headers else {},
                        url=str(resp.url) if resp.url else url,
                    )
                last_error = Exception("Empty response from Fetcher")

            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    delay = 2 ** attempt  # 1s, 2s, 4s
                    logger.debug(
                        "curl_cffi attempt %d/%d failed for %s, "
                        "retrying in %ds: %s",
                        attempt + 1, self.max_retries, url[:60], delay, exc,
                    )
                    time.sleep(delay)

        logger.debug(
            "curl_cffi all %d attempts failed for %s: %s",
            self.max_retries, url[:60], last_error,
        )
        return None

    def _via_urllib(
        self, method: str, url: str, data: Optional[bytes],
        headers: Optional[dict],
    ) -> HTTPResponse:
        """Fallback: urllib.request (always available, no TLS impersonation)."""
        import urllib.request
        import urllib.error

        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={"User-Agent": CRAWLER_USER_AGENT, **(headers or {})},
                method=method,
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                content = resp.read()
                return HTTPResponse(
                    status_code=resp.status,
                    text=content.decode(
                        resp.headers.get_content_charset() or "utf-8",
                        errors="replace",
                    ),
                    content=content,
                    headers=dict(resp.headers),
                    url=resp.url or url,
                )
        except urllib.error.HTTPError as exc:
            return HTTPResponse(
                status_code=exc.code,
                error=str(exc),
                url=url,
            )
        except Exception as exc:
            return HTTPResponse(
                status_code=0,
                error=str(exc)[:200],
                url=url,
            )
