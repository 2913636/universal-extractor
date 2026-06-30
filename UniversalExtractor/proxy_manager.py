"""
Proxy configuration and rotation manager.

Reads proxy settings from environment variables or explicit config,
supports rotation across multiple proxies with health checking.

Usage:
    from UniversalExtractor.proxy_manager import ProxyManager

    pm = ProxyManager()
    proxy = pm.get_proxy()  # {'server': 'http://proxy:8080'} or None
"""

from __future__ import annotations

import os
import time
import random
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Scrapling Playwright proxy format: {'server': '...', 'username': '...', 'password': '...'}
# Scrapling Fetcher proxy format: 'http://user:pass@host:port'


class ProxyManager:
    """Manage proxy configuration and rotation.

    Proxy sources (in priority order):
        1. Explicit proxy_urls parameter
        2. HTTP_PROXY / HTTPS_PROXY environment variables
        3. .env file (via python-dotenv, if installed)

    Parameters:
        proxy_urls: List of proxy URLs, or single URL string
        validate: Whether to check proxy health on init
    """

    def __init__(
        self,
        proxy_urls: Optional[str | list[str]] = None,
        validate: bool = False,
    ):
        self._proxies: list[str] = []
        self._index: int = 0
        self._failed: dict[str, float] = {}  # proxy → fail timestamp
        self._cooldown: float = 300  # 5 min cooldown after failure

        # Load proxies
        if proxy_urls:
            if isinstance(proxy_urls, str):
                self._proxies = [proxy_urls]
            else:
                self._proxies = list(proxy_urls)
        else:
            self._proxies = self._load_from_env()

        if validate:
            self._validate_all()

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def get_proxy(self) -> Optional[dict]:
        """Get the next healthy proxy in Playwright/StealthyFetcher format.

        Returns:
            {'server': 'http://host:port'} or None if no proxy configured.
        """
        proxy_url = self._next_healthy()
        if proxy_url is None:
            return None
        return self._to_playwright_format(proxy_url)

    def get_proxy_string(self) -> Optional[str]:
        """Get proxy as a URL string (for curl_cffi Fetcher).

        Returns:
            'http://user:pass@host:port' or None.
        """
        return self._next_healthy()

    def rotate(self) -> Optional[str]:
        """Switch to next proxy manually. Returns new proxy URL or None."""
        if not self._proxies:
            return None
        self._index = (self._index + 1) % len(self._proxies)
        return self._proxies[self._index]

    def mark_failed(self, proxy_url: str) -> None:
        """Mark a proxy as failed. It won't be returned until cooldown expires."""
        self._failed[proxy_url] = time.monotonic()
        logger.warning("Proxy marked failed: %s", self._mask_proxy(proxy_url))

    @property
    def proxy_count(self) -> int:
        return len(self._proxies)

    @property
    def healthy_count(self) -> int:
        return sum(1 for p in self._proxies if not self._is_cooling_down(p))

    # ----------------------------------------------------------------
    # Internal
    # ----------------------------------------------------------------

    @staticmethod
    def _load_from_env() -> list[str]:
        """Load proxy URLs from environment variables."""
        proxies = []
        for var in ["HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"]:
            val = os.getenv(var, "")
            if val and val not in proxies:
                proxies.append(val)

        # Try .env file
        if not proxies:
            try:
                from dotenv import load_dotenv
                load_dotenv()
                for var in ["HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"]:
                    val = os.getenv(var, "")
                    if val and val not in proxies:
                        proxies.append(val)
            except ImportError:
                pass

        return proxies

    def _next_healthy(self) -> Optional[str]:
        """Get next proxy that is not in cooldown."""
        if not self._proxies:
            return None

        # Single proxy: just return it (don't rotate)
        if len(self._proxies) == 1:
            return self._proxies[0]

        # Try each proxy starting from current index
        for _ in range(len(self._proxies)):
            proxy = self._proxies[self._index]
            self._index = (self._index + 1) % len(self._proxies)
            if not self._is_cooling_down(proxy):
                return proxy

        # All in cooldown: return least recently failed
        return min(self._proxies, key=lambda p: self._failed.get(p, 0))

    def _is_cooling_down(self, proxy: str) -> bool:
        """Check if proxy is in failure cooldown."""
        if proxy not in self._failed:
            return False
        elapsed = time.monotonic() - self._failed[proxy]
        return elapsed < self._cooldown

    def _validate_all(self) -> None:
        """Check all proxies with a test request."""
        for proxy in self._proxies:
            if not self._validate_proxy(proxy):
                self.mark_failed(proxy)

    @staticmethod
    def _validate_proxy(proxy_url: str) -> bool:
        """Test if a proxy is working."""
        try:
            import urllib.request

            proxy_handler = urllib.request.ProxyHandler({
                "http": proxy_url,
                "https": proxy_url,
            })
            opener = urllib.request.build_opener(proxy_handler)
            req = urllib.request.Request(
                "https://httpbin.org/ip", headers={"User-Agent": "ProxyValidator/1.0"}
            )
            with opener.open(req, timeout=10) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def _to_playwright_format(proxy_url: str) -> dict:
        """Convert proxy URL to Playwright format.

        'http://user:pass@host:8080' → {'server': 'http://host:8080', 'username': 'user', 'password': 'pass'}
        """
        from urllib.parse import urlparse, unquote

        parsed = urlparse(proxy_url)
        result: dict = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 8080}"}
        if parsed.username:
            result["username"] = unquote(parsed.username)
        if parsed.password:
            result["password"] = unquote(parsed.password)
        return result

    @staticmethod
    def _mask_proxy(proxy_url: str) -> str:
        """Hide credentials in proxy URL for logging."""
        from urllib.parse import urlparse

        parsed = urlparse(proxy_url)
        if parsed.username:
            return f"{parsed.scheme}://***:***@{parsed.hostname}:{parsed.port}"
        return proxy_url
