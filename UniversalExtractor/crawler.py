"""
Crawler — link discovery + batch extraction + merged output.

Given a catalog/index page URL, discovers linked pages matching a pattern,
extracts each one through the Pipeline, writes a single merged Markdown file,
and supports progress-based resume.

Usage::

    from UniversalExtractor.crawler import Crawler

    c = Crawler(headless=False)
    c.crawl(
        url="http://www.daishuzw.com/daishu/96158.html",
        output="novel.md",
        title="那就让她们献上忠诚吧",
    )
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from .http_client import HTTPClient
from .pipeline import Pipeline, PipelineConfig

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------
# Auto-detection: patterns that signal a "chapter / article" link
# ----------------------------------------------------------------
_AUTO_PATTERNS = [
    # Chinese chapter numbering
    re.compile(r"第\s*\d+\s*章"),
    re.compile(r"第\s*\d+\s*节"),
    re.compile(r"第\s*\d+\s*回"),
    # English / mixed
    re.compile(r"Ch(apter)?[\.\s]*\d+", re.IGNORECASE),
    re.compile(r"Ep(isode)?[\.\s]*\d+", re.IGNORECASE),
    re.compile(r"Part[\.\s]*\d+", re.IGNORECASE),
    # Numeric-only (must be the whole link text)
    re.compile(r"^\d+$"),
    re.compile(r"^\d+[\.\-\s].+"),
]


@dataclass
class CrawlResult:
    """Result of a crawl operation."""

    title: str = ""
    url: str = ""
    total_links: int = 0
    extracted: int = 0
    failed: int = 0
    total_chars: int = 0
    output: str = ""


@dataclass
class _Link:
    """Internal: a discovered link."""

    key: str  # stable sort key
    label: str  # display label
    url: str


# ----------------------------------------------------------------
# Crawler
# ----------------------------------------------------------------


class Crawler:
    """Discover links on a page, extract every matching page, merge results."""

    def __init__(
        self,
        headless: bool = False,
        min_completeness: float = 0.3,
        timeout: int = 30_000,
        delay: float = 1.0,
    ):
        self._pipeline = Pipeline(
            PipelineConfig(
                headless=headless,
                min_completeness=min_completeness,
                timeout=timeout,
                max_candidates=1,
            )
        )
        self._http = HTTPClient(timeout=timeout / 1000)
        self._delay = delay

    # ----------------------------------------------------------
    # Link discovery
    # ----------------------------------------------------------

    def discover_links(
        self,
        url: str,
        pattern: str | None = None,
        max_links: int | None = None,
    ) -> list[tuple[str, str]]:
        """Fetch *url*, find all `<a>` tags whose text matches *pattern*.

        Returns a list of ``(label, absolute_url)`` tuples in the order
        they appear on the page.  When *pattern* is ``None`` the crawler
        auto-detects chapter/article links.
        """
        resp = self._http.get(url)
        html = resp.text
        base_url = self._resolve_base(url, html)

        # ---- compile matcher ----
        if pattern:
            matcher = re.compile(pattern)
        else:
            matcher = None

        # ---- extract all candidate links ----
        raw_links: list[_Link] = []
        tag_re = re.compile(
            r"<a\s[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
            re.IGNORECASE | re.DOTALL,
        )
        seen_urls: set[str] = set()

        for m in tag_re.finditer(html):
            href = m.group(1).strip()
            text = re.sub(r"<[^>]+>", "", m.group(2)).strip()

            if not text or not href or href.startswith("javascript:"):
                continue

            abs_url = urljoin(base_url, href)
            # Only follow http(s) URLs
            if not abs_url.startswith(("http://", "https://")):
                continue
            # De-duplicate
            if abs_url in seen_urls:
                continue
            seen_urls.add(abs_url)

            # Check against pattern or auto-detector
            if matcher:
                # Match against link text OR href, whichever hits first
                if not (matcher.search(text) or matcher.search(href)):
                    continue
                key = self._sort_key(text)
            else:
                if not self._is_chapter_link(text):
                    continue
                key = self._sort_key(text)

            raw_links.append(_Link(key=key, label=text, url=abs_url))

        # ---- sort by extracted number ----
        raw_links.sort(key=lambda x: x.key)
        if max_links:
            raw_links = raw_links[:max_links]

        return [(li.label, li.url) for li in raw_links]

    # ----------------------------------------------------------
    # Crawl entry point
    # ----------------------------------------------------------

    def crawl(
        self,
        url: str,
        output: str | Path,
        title: str = "",
        pattern: str | None = None,
        max_links: int | None = None,
    ) -> CrawlResult:
        """Discover links on *url*, extract each, and merge into *output*.

        Progress is saved to ``<output>.progress.json`` alongside the output
        file so interrupted crawls can be resumed.
        """
        output = Path(output)
        progress_file = output.with_suffix(output.suffix + ".progress.json")

        # ---- discover links ----
        logger.info("Discovering links on %s ...", url)
        links = self.discover_links(url, pattern=pattern, max_links=max_links)
        logger.info("  found %d links", len(links))

        # ---- load resume state ----
        done: dict[str, dict] = {}
        if progress_file.exists():
            try:
                done = json.loads(progress_file.read_text("utf-8"))
                logger.info("  resuming: %d already extracted", len(done))
            except Exception:
                pass

        # ---- extract ----
        total = len(links)
        failed = 0
        for idx, (label, link_url) in enumerate(links):
            ch_key = self._slug(label, idx)
            if ch_key in done:
                continue

            logger.info("[%d/%d] %s", idx + 1, total, label[:40])
            for attempt in range(3):
                try:
                    result = self._pipeline.run(url=link_url, mode="extract_only")
                    if result.text and len(result.text) > 100:
                        done[ch_key] = {
                            "idx": idx,
                            "label": label,
                            "url": link_url,
                            "text": result.text,
                        }
                        break
                    if self._delay:
                        time.sleep(self._delay * (attempt + 1))
                except Exception as exc:
                    logger.debug("  attempt %d failed: %s", attempt + 1, exc)
                    if self._delay:
                        time.sleep(self._delay * (attempt + 1))
            else:
                failed += 1
                logger.warning("  FAILED after 3 attempts")

            # Save progress periodically
            if idx % 10 == 0 or idx == total - 1:
                progress_file.write_text(
                    json.dumps(done, ensure_ascii=False, indent=2), "utf-8"
                )

            if self._delay:
                time.sleep(self._delay)

        # ---- merge & write ----
        sorted_items = sorted(done.values(), key=lambda x: x["idx"])
        total_chars = sum(len(it["text"]) for it in sorted_items)
        title = title or self._guess_title(url, sorted_items)

        lines = [f"# {title}\n\n"]
        if total_chars:
            lines.append(
                f"共 {len(sorted_items)} 篇 | 总字数约 {total_chars:,} | "
                f"来源：{urlparse(url).netloc}\n\n---\n\n"
            )
        for item in sorted_items:
            lines.append(f"## {item['label']}\n\n")
            lines.append(item["text"])
            lines.append("\n\n---\n\n")

        output.write_text("".join(lines), "utf-8")

        # Clean up progress file on success
        try:
            progress_file.unlink()
        except OSError:
            pass

        logger.info(
            "Done: %s  (%d items, ~%s chars, %d failed)",
            output,
            len(sorted_items),
            f"{total_chars:,}",
            failed,
        )

        return CrawlResult(
            title=title,
            url=url,
            total_links=total,
            extracted=len(sorted_items),
            failed=failed,
            total_chars=total_chars,
            output=str(output),
        )

    # ----------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------

    @staticmethod
    def _is_chapter_link(text: str) -> bool:
        """Return True if *text* looks like a chapter/article heading."""
        for pat in _AUTO_PATTERNS:
            if pat.search(text):
                return True
        return False

    @staticmethod
    def _sort_key(text: str) -> str:
        """Derive a sortable string from link text.

        Extracts the first numeric sequence and pads it to 8 digits so that
        ``"第9章"`` sorts before ``"第100章"``.
        """
        nums = re.findall(r"(\d+)", text)
        if nums:
            return nums[0].zfill(8)
        return text

    @staticmethod
    def _slug(label: str, idx: int) -> str:
        """Short stable key for progress tracking."""
        nums = re.findall(r"\d+", label)
        if nums:
            return f"n{nums[0].zfill(6)}"
        return f"i{idx:06d}"

    @staticmethod
    def _resolve_base(url: str, html: str) -> str:
        """Extract ``<base href>`` or fall back to the page URL."""
        m = re.search(r'<base\s[^>]*href\s*=\s*["\']([^"\']+)', html, re.IGNORECASE)
        if m:
            return m.group(1)
        return url

    @staticmethod
    def _guess_title(
        url: str,
        items: list[dict],
    ) -> str:
        """Guess a title from the first item's label or the URL."""
        if items:
            first = items[0].get("label", "")
            # Strip chapter number, keep the rest
            title = re.sub(r"第\s*\d+\s*章\s*", "", first).strip()
            if title:
                return title
        return urlparse(url).netloc
