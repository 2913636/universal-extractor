"""
Per-domain request rate limiter with jitter.

Prevents being blocked by adding controlled delays between
requests to the same domain.

Usage:
    from UniversalExtractor.rate_limiter import RateLimiter

    limiter = RateLimiter(min_interval=2.0, jitter=0.3)
    limiter.wait("example.com")   # blocks if needed
    # ... make request to example.com ...
"""

from __future__ import annotations

import time
import random
import logging
from urllib.parse import urlparse
from typing import Optional

logger = logging.getLogger(__name__)


class RateLimiter:
    """Track request timing per domain. Add delay if requests are too fast.

    Parameters:
        min_interval: Minimum seconds between requests to the same domain
        jitter: Random jitter ratio (±). 0.3 = ±30% random variation.
        max_concurrent: Max concurrent requests globally (0 = unlimited)
    """

    def __init__(
        self,
        min_interval: float = 2.0,
        jitter: float = 0.3,
        max_concurrent: int = 0,
    ):
        self.min_interval = min_interval
        self.jitter = jitter
        self.max_concurrent = max_concurrent

        self._last_request: dict[str, float] = {}
        self._in_flight: int = 0

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def wait(self, domain: str) -> float:
        """Wait if needed to respect min_interval for this domain.

        Returns:
            Actual seconds waited (0 if no wait needed).
        """
        now = time.monotonic()

        # Concurrency gate
        if self.max_concurrent > 0:
            while self._in_flight >= self.max_concurrent:
                time.sleep(0.1)

        # Domain rate gate
        waited = 0.0
        if domain in self._last_request:
            elapsed = now - self._last_request[domain]
            if elapsed < self.min_interval:
                base_delay = self.min_interval - elapsed
                # Add random jitter to avoid mechanical patterns
                jitter_amount = base_delay * self.jitter
                delay = base_delay + random.uniform(-jitter_amount, jitter_amount)
                delay = max(0, delay)
                if delay > 0.001:
                    time.sleep(delay)
                    waited = delay

        self._last_request[domain] = time.monotonic()
        return waited

    def enter(self, domain: str) -> None:
        """Call before request: waits + increments in-flight counter."""
        self.wait(domain)
        self._in_flight += 1

    def exit(self) -> None:
        """Call after request completes: decrements in-flight counter."""
        self._in_flight = max(0, self._in_flight - 1)

    def wait_for_url(self, url: str) -> float:
        """Convenience: extract domain from URL and wait."""
        return self.wait(RateLimiter.extract_domain(url))

    # ----------------------------------------------------------------
    # Utilities
    # ----------------------------------------------------------------

    @staticmethod
    def extract_domain(url: str) -> str:
        """Extract domain from URL.

        >>> RateLimiter.extract_domain('https://www.example.com/path')
        'www.example.com'
        """
        parsed = urlparse(url)
        return parsed.hostname or url

    @property
    def domains_tracked(self) -> int:
        """Number of domains with recorded request times."""
        return len(self._last_request)
