"""
Browser session persistence manager.

Reuses browser profiles across extraction sessions to preserve
cookies, localStorage, and other browser state. This reduces the
chance of being detected as a bot and speeds up repeated visits.

Usage:
    from UniversalExtractor.session_manager import SessionManager

    sm = SessionManager()
    profile_path = sm.get_profile("example.com")
    # Pass to StealthyFetcher as user_data_dir=str(profile_path)
"""

from __future__ import annotations

import os
import time
import shutil
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SessionManager:
    """Manage persistent browser profiles per domain.

    Parameters:
        base_dir: Root directory for session storage.
                  Default: ~/.universal_extractor_sessions
        max_age_days: Profiles unused for longer than this are cleaned up.
        max_profile_size_mb: Max profile size before cleanup (per domain).
    """

    def __init__(
        self,
        base_dir: Optional[str] = None,
        max_age_days: int = 7,
        max_profile_size_mb: int = 200,
        *,
        persist_dir: Optional[str] = None,
    ):
        self.base_dir = Path(base_dir or persist_dir or self._default_dir())
        self.max_age_days = max_age_days
        self.max_profile_size_mb = max_profile_size_mb
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def get_profile(self, domain: str) -> Path:
        """Get (or create) a persistent browser profile directory for a domain.

        Returns:
            Path to the profile directory, ready to pass as
            user_data_dir to StealthyFetcher.fetch().
        """
        # Sanitize domain for use as directory name
        safe_name = self._sanitize(domain)
        profile_dir = self.base_dir / safe_name
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Touch timestamp for cleanup tracking
        timestamp_file = profile_dir / ".last_used"
        timestamp_file.write_text(str(time.time()))

        return profile_dir

    def cleanup(self, max_age_days: Optional[int] = None) -> int:
        """Remove profiles that haven't been used recently.

        Args:
            max_age_days: Override the instance default.

        Returns:
            Number of profiles removed.
        """
        threshold = max_age_days or self.max_age_days
        removed = 0
        now = time.time()

        for entry in self.base_dir.iterdir():
            if not entry.is_dir():
                continue
            timestamp_file = entry / ".last_used"
            if timestamp_file.exists():
                try:
                    last_used = float(timestamp_file.read_text().strip())
                    age_days = (now - last_used) / 86400
                    if age_days > threshold:
                        shutil.rmtree(entry, ignore_errors=True)
                        removed += 1
                        logger.debug("Cleaned up session: %s (%.1f days old)",
                                     entry.name, age_days)
                except (ValueError, OSError):
                    pass

        return removed

    def get_profile_size(self, domain: str) -> int:
        """Get the size of a domain's profile in bytes."""
        profile_dir = self.base_dir / self._sanitize(domain)
        if not profile_dir.exists():
            return 0
        return sum(
            f.stat().st_size
            for f in profile_dir.rglob("*")
            if f.is_file()
        )

    def clear(self, domain: str) -> bool:
        """Clear a specific domain's profile."""
        profile_dir = self.base_dir / self._sanitize(domain)
        if profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)
            return True
        return False

    def clear_all(self) -> int:
        """Clear all profiles. Returns count of removed directories."""
        count = 0
        for entry in self.base_dir.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
                count += 1
        return count

    # ----------------------------------------------------------------
    # Internal
    # ----------------------------------------------------------------

    @staticmethod
    def _default_dir() -> str:
        """Default session directory: ~/.universal_extractor_sessions"""
        home = os.path.expanduser("~")
        return os.path.join(home, ".universal_extractor_sessions")

    @staticmethod
    def _sanitize(domain: str) -> str:
        """Convert domain to safe directory name."""
        # Remove scheme if present
        if "://" in domain:
            from urllib.parse import urlparse
            domain = urlparse(domain).hostname or domain
        # Replace unsafe chars
        safe = ""
        for ch in domain:
            if ch.isalnum() or ch in ".-_":
                safe += ch
            else:
                safe += "_"
        return safe or "unknown"
