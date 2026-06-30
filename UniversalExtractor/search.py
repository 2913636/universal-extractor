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

import asyncio
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
    Uses HTTPClient for retry + backoff + urllib fallback.

    Returns response body as bytes, or None on failure.
    """
    from .http_client import HTTPClient

    client = HTTPClient(timeout=timeout)
    if method == "POST":
        resp = client.post(url, data=data, headers=headers)
    else:
        resp = client.get(url, headers=headers)

    if resp.ok and resp.content:
        return resp.content
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


def _search_searxng(query: str, max_results: int = 10) -> list[str]:
    """
    SearXNG — self-hosted meta search engine.
    Aggregates results from Google, Bing, DuckDuckGo, etc.

    Set SEARXNG_URL env var to your instance, e.g.:
        SEARXNG_URL=https://searx.example.com

    Requires zero API keys. The instance owner controls which
    upstream engines are queried.
    """
    searx_url = os.getenv("SEARXNG_URL", "")
    if not searx_url:
        logger.debug("SearXNG: no SEARXNG_URL configured, skipping")
        return []

    try:
        import json

        api_url = f"{searx_url.rstrip('/')}/search"
        params = urllib.parse.urlencode({
            "q": query,
            "format": "json",
            "categories": "general",
            "language": "zh-CN",
            "pageno": 1,
        })
        body = _http_fetch(
            f"{api_url}?{params}",
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if not body:
            return []

        data = json.loads(body)

        urls = []
        seen = set()
        for item in data.get("results", []):
            url = item.get("url", "")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

        logger.info("SearXNG: %d results for '%s'", len(urls), query[:50])
        return urls[:max_results]
    except Exception as exc:
        logger.debug("SearXNG error: %s", exc)
        return []


def search_urls(
    query: str,
    max_results: int = 15,
    *,
    backends: Optional[list[str]] = None,
    site_filter: Optional[str] = None,
) -> list[str]:
    """聚合多引擎搜索结果，按交叉命中数排序后返回 URL 列表。"""
    meta = search_with_metadata(query, max_results, backends=backends, site_filter=site_filter)
    return [item["url"] for item in meta["results"]]


def search_with_metadata(
    query: str,
    max_results: int = 15,
    *,
    backends: Optional[list[str]] = None,
    site_filter: Optional[str] = None,
) -> dict:
    """
    聚合多引擎搜索，返回完整元数据。

    增强:
      - 多引擎交叉对比: 同一 URL 被 >= 2 个引擎发现 → 提升排名
      - 每个 URL 标注来源引擎 + 排名位置
      - 返回各引擎统计信息

    Returns:
        {
            "results": [{"url": "...", "backends": ["brave","duckduckgo"], "cross_hits": 2, "ranks": {...}}, ...],
            "backends_used": ["brave", "duckduckgo"],
            "backend_stats": {"brave": 15, "duckduckgo": 10},
            "total_raw": 25,
            "total_unique": 18,
        }
    """
    if backends is None:
        backends = ["brave", "exa", "duckduckgo", "searxng"]

    search_query = query
    if site_filter:
        search_query = f"site:{site_filter} {query}"

    # Collect per-backend results with rank tracking
    url_sources: dict[str, list[str]] = {}  # url → [backend, ...]
    url_ranks: dict[str, dict[str, int]] = {}  # url → {backend: rank}
    backend_stats: dict[str, int] = {}
    total_raw = 0

    for backend in backends:
        try:
            if backend == "brave":
                results = _search_brave(search_query, max_results)
            elif backend == "exa":
                results = _search_exa(query, max_results)
            elif backend == "duckduckgo":
                results = _search_duckduckgo(search_query, max_results)
            elif backend == "searxng":
                results = _search_searxng(query, max_results)
            else:
                logger.warning("Unknown search backend: %s", backend)
                continue

            backend_stats[backend] = len(results)
            total_raw += len(results)

            for rank, url in enumerate(results):
                if url not in url_sources:
                    url_sources[url] = []
                    url_ranks[url] = {}
                url_sources[url].append(backend)
                url_ranks[url][backend] = rank

        except Exception as exc:
            logger.warning("Backend '%s' error: %s", backend, exc)
            backend_stats[backend] = 0

    # Score & sort: cross_hits (desc) → avg rank (asc)
    scored = []
    for url, sources in url_sources.items():
        cross_hits = len(sources)
        avg_rank = sum(url_ranks[url].values()) / len(url_ranks[url])
        scored.append((cross_hits, -avg_rank, url))

    scored.sort(reverse=True)

    results = [
        {
            "url": url,
            "backends": url_sources[url],
            "cross_hits": cross_hits,
            "ranks": url_ranks[url],
        }
        for cross_hits, _, url in scored[:max_results]
    ]

    logger.info(
        "Search: %d raw → %d unique from %d backends, top cross-hit=%d",
        total_raw, len(url_sources), len(backends),
        results[0]["cross_hits"] if results else 0,
    )

    return {
        "results": results,
        "backends_used": [b for b in backends if backend_stats.get(b, 0) > 0],
        "backend_stats": backend_stats,
        "total_raw": total_raw,
        "total_unique": len(url_sources),
    }


async def search_with_metadata_async(
    query: str,
    max_results: int = 15,
    *,
    backends: Optional[list[str]] = None,
    site_filter: Optional[str] = None,
) -> dict:
    """
    Async version of search_with_metadata.
    Runs all backends in parallel via asyncio.gather().

    2-3x faster than the sync version when multiple backends are used.
    """
    if backends is None:
        backends = ["brave", "exa", "duckduckgo", "searxng"]

    search_query = query
    if site_filter:
        search_query = f"site:{site_filter} {query}"

    # Run all backends in parallel
    async def _run_backend(backend: str) -> tuple[str, list[str]]:
        loop = asyncio.get_running_loop()
        try:
            if backend == "brave":
                results = await loop.run_in_executor(
                    None, _search_brave, search_query, max_results)
            elif backend == "exa":
                results = await loop.run_in_executor(
                    None, _search_exa, query, max_results)
            elif backend == "duckduckgo":
                results = await loop.run_in_executor(
                    None, _search_duckduckgo, search_query, max_results)
            elif backend == "searxng":
                results = await loop.run_in_executor(
                    None, _search_searxng, query, max_results)
            else:
                logger.warning("Unknown search backend: %s", backend)
                return backend, []
        except Exception as exc:
            logger.warning("Backend '%s' error: %s", backend, exc)
            return backend, []
        return backend, results

    tasks = [_run_backend(b) for b in backends]
    backend_results = await asyncio.gather(*tasks)

    # Merge results (same logic as sync version)
    url_sources: dict[str, list[str]] = {}
    url_ranks: dict[str, dict[str, int]] = {}
    backend_stats: dict[str, int] = {}
    total_raw = 0

    for backend, results in backend_results:
        backend_stats[backend] = len(results)
        total_raw += len(results)
        for rank, url in enumerate(results):
            if url not in url_sources:
                url_sources[url] = []
                url_ranks[url] = {}
            url_sources[url].append(backend)
            url_ranks[url][backend] = rank

    # Score & sort
    scored = []
    for url, sources in url_sources.items():
        cross_hits = len(sources)
        avg_rank = sum(url_ranks[url].values()) / len(url_ranks[url])
        scored.append((cross_hits, -avg_rank, url))
    scored.sort(reverse=True)

    results = [
        {
            "url": url,
            "backends": url_sources[url],
            "cross_hits": cross_hits,
            "ranks": url_ranks[url],
        }
        for cross_hits, _, url in scored[:max_results]
    ]

    return {
        "results": results,
        "backends_used": [b for b in backends if backend_stats.get(b, 0) > 0],
        "backend_stats": backend_stats,
        "total_raw": total_raw,
        "total_unique": len(url_sources),
    }


def is_likely_content_url(url: str) -> bool:
    """判断 URL 是否像是正文页面（而非导航页/搜索页/登录页）。"""
    noise_patterns = [
        r"/search[/?]", r"/login", r"/signup", r"/register",
        r"/tag/", r"/category/", r"/author/", r"/about",
        r"/contact", r"/privacy", r"/terms", r"\.(png|jpg|gif|svg|css|js)$",
    ]
    return not any(re.search(p, url, re.IGNORECASE) for p in noise_patterns)
