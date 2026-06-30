"""
搜索层 — 聚合多个搜索后端，返回去重排序后的 URL 列表。

后端：
  - Brave Search API（免费 2000 次/月，中文好）
  - Exa Search（MCP 方式，免费额度）
  - 搜索引擎 fallback（DuckDuckGo / Google）

用法:
    from .search import search_urls

    urls = search_urls("三体 小说 全文", max_results=15)
    # → ["https://...", "https://...", ...]
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from typing import Optional

logger = logging.getLogger(__name__)


def _http_fetch(
    url: str,
    method: str = "GET",
    headers: Optional[dict] = None,
    data: Optional[bytes] = None,
    timeout: int = 15,
) -> Optional[bytes]:
    """
    HTTP request with TLS fingerprint impersonation (curl_cffi).
    Falls back to urllib if curl_cffi is unavailable.

    Returns response body as bytes, or None on failure.
    """
    # Try curl_cffi first (browser TLS fingerprint)
    try:
        from scrapling import Fetcher

        fetcher = Fetcher()
        fetcher.configure(auto_referer=False, keep_alive=True)
        if method == "POST":
            resp = fetcher.post(url, headers=headers or {}, data=data or b"")
        else:
            resp = fetcher.get(url, headers=headers or {})

        if resp and resp.content:
            return resp.content
    except Exception:
        pass

    # Fallback to urllib
    try:
        import urllib.request

        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


# Brave Search 免费 API：https://api.search.brave.com
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"


def _search_brave(query: str, max_results: int = 10) -> list[str]:
    """Brave Search API — free, good Chinese search quality."""
    api_key = os.getenv("BRAVE_API_KEY", "")
    if not api_key:
        logger.debug("Brave Search: no API key, skipping")
        return []

    try:
        import json

        params = urllib.parse.urlencode({
            "q": query,
            "count": min(max_results, 20),
            "search_lang": "zh",
        })
        body = _http_fetch(
            f"{BRAVE_API_URL}?{params}",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
            timeout=10,
        )
        if not body:
            return []

        data = json.loads(body)

        urls = []
        for item in data.get("web", {}).get("results", []):
            url = item.get("url", "")
            if url:
                urls.append(url)
        logger.info("Brave Search: %d results for '%s'", len(urls), query[:50])
        return urls
    except Exception as exc:
        logger.warning("Brave Search error: %s", exc)
        return []


def _search_exa(query: str, max_results: int = 10) -> list[str]:
    """Exa Search — semantic search with TLS fingerprint impersonation."""
    api_key = os.getenv("EXA_API_KEY", "")
    if not api_key:
        logger.debug("Exa Search: no API key, skipping")
        return []

    try:
        import json

        body = _http_fetch(
            "https://api.exa.ai/search",
            method="POST",
            data=json.dumps({
                "query": query,
                "numResults": min(max_results, 10),
                "useAutoprompt": True,
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
            },
            timeout=15,
        )
        if not body:
            return []

        data = json.loads(body)

        urls = []
        for item in data.get("results", []):
            url = item.get("url", "")
            if url:
                urls.append(url)
        logger.info("Exa Search: %d results for '%s'", len(urls), query[:50])
        return urls
    except Exception as exc:
        logger.warning("Exa Search error: %s", exc)
        return []


def _search_duckduckgo(query: str, max_results: int = 10) -> list[str]:
    """
    DuckDuckGo HTML search — free, no API key needed.
    Uses TLS fingerprint impersonation via curl_cffi.
    """
    try:
        params = urllib.parse.urlencode({"q": query})
        body = _http_fetch(
            f"https://html.duckduckgo.com/html/?{params}",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html",
            },
            timeout=10,
        )
        if not body:
            return []

        html = body.decode("utf-8", errors="replace")

        # DuckDuckGo 用 uddg= 参数编码目标 URL
        # 格式: //duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com
        encoded_urls = re.findall(r"uddg=([^\"'&\s]+)", html)

        # URL 解码并去重（保持顺序）
        seen = set()
        urls = []
        for eu in encoded_urls:
            try:
                decoded = urllib.parse.unquote(eu)
                if decoded not in seen:
                    seen.add(decoded)
                    urls.append(decoded)
            except Exception:
                pass

        logger.info("DuckDuckGo: %d results for '%s'", len(urls), query[:50])
        return urls[:max_results]
    except Exception as exc:
        logger.debug("DuckDuckGo error: %s", exc)
        return []


def search_urls(
    query: str,
    max_results: int = 15,
    *,
    backends: Optional[list[str]] = None,
    site_filter: Optional[str] = None,
) -> list[str]:
    """
    聚合多个搜索后端，返回去重排序后的 URL 列表。

    Parameters:
        query: 搜索关键词
        max_results: 最多返回多少条
        backends: 指定后端列表，默认全部启用：
                  ``["brave", "exa", "duckduckgo"]``
        site_filter: 限定站点，如 ``"bilibili.com"``

    Returns:
        URL 列表（去重，每个后端的结果交替排列）
    """
    if backends is None:
        backends = ["brave", "exa", "duckduckgo"]

    # 附加站点限定
    search_query = query
    if site_filter:
        search_query = f"site:{site_filter} {query}"

    # 并行收集（简单串行，未来可改 asyncio）
    all_urls: list[str] = []
    seen: set[str] = set()

    for backend in backends:
        try:
            if backend == "brave":
                results = _search_brave(search_query, max_results)
            elif backend == "exa":
                results = _search_exa(query, max_results)  # Exa 支持语义，不加 site:
            elif backend == "duckduckgo":
                results = _search_duckduckgo(search_query, max_results)
            else:
                logger.warning("Unknown search backend: %s", backend)
                continue

            # 去重
            for url in results:
                if url not in seen:
                    seen.add(url)
                    all_urls.append(url)
        except Exception as exc:
            logger.warning("Backend '%s' error: %s", backend, exc)

    logger.info("Search total: %d unique URLs from %d backends",
                len(all_urls), len(backends))
    return all_urls[:max_results]


def is_likely_content_url(url: str) -> bool:
    """判断 URL 是否像是正文页面（而非导航页/搜索页/登录页）。"""
    noise_patterns = [
        r"/search[/?]", r"/login", r"/signup", r"/register",
        r"/tag/", r"/category/", r"/author/", r"/about",
        r"/contact", r"/privacy", r"/terms", r"\.(png|jpg|gif|svg|css|js)$",
    ]
    return not any(re.search(p, url, re.IGNORECASE) for p in noise_patterns)
