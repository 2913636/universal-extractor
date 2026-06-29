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

# Brave Search 免费 API：https://api.search.brave.com
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"


def _search_brave(query: str, max_results: int = 10) -> list[str]:
    """Brave Search API — 免费，中文搜索质量好。"""
    api_key = os.getenv("BRAVE_API_KEY", "")
    if not api_key:
        logger.debug("Brave Search: no API key, skipping")
        return []

    try:
        import urllib.request
        import json

        params = urllib.parse.urlencode({
            "q": query,
            "count": min(max_results, 20),
            "search_lang": "zh",
        })
        req = urllib.request.Request(
            f"{BRAVE_API_URL}?{params}",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

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
    """
    Exa Search — 语义搜索，Agent Reach 用的主力引擎。

    需要 Exa MCP Server 已启动，或设置 EXA_API_KEY 环境变量。
    """
    api_key = os.getenv("EXA_API_KEY", "")
    if not api_key:
        logger.debug("Exa Search: no API key, skipping")
        return []

    try:
        import urllib.request
        import json

        req = urllib.request.Request(
            "https://api.exa.ai/search",
            data=json.dumps({
                "query": query,
                "numResults": min(max_results, 10),
                "useAutoprompt": True,
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

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
    DuckDuckGo HTML 搜索 — 免费，不需要 API Key。

    用 html.duckduckgo.com 获取搜索结果，解析 uddg= 重定向链接
    提取真实 URL。DuckDuckGo 用 uddg 参数编码目标 URL。
    """
    try:
        import urllib.request

        params = urllib.parse.urlencode({"q": query})
        req = urllib.request.Request(
            f"https://html.duckduckgo.com/html/?{params}",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")

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
