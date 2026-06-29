"""
WebLens — 搜 + 筛 + 抓 编排引擎。

Agent Reach 负责"找 URL"，UniversalExtractor 负责"拿内容"，
WebLens 把两者拼起来，加上智能筛选。

用法:
    from universal_extractor.weblens import WebLens

    wl = WebLens(headless=True)
    result = wl.search_and_extract("三体 小说 全文")
    # → {"url": "...", "text": "...", "score": 0.92, "source": "brave"}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .extractor import UniversalExtractor
from .completeness import completeness_score
from .search import search_urls
from .classifier import classify_url, score_content, match_keywords

logger = logging.getLogger(__name__)


@dataclass
class WebLensResult:
    url: str = ""
    text: str = ""
    score: float = 0.0
    method: str = ""           # search backend that found this URL
    source_layer: int = 0       # which UniversalExtractor layer succeeded
    candidates_scanned: int = 0  # how many URLs were quick-scanned
    candidates_total: int = 0    # how many URLs were found by search


class WebLens:
    """搜 + 筛 + 抓 一体化引擎。

    Parameters:
        headless: 浏览器是否无头模式
        timeout: 浏览器超时（毫秒）
        quick_scan_timeout: 快速扫描每个候选 URL 的超时（毫秒）
        min_score: 最低完整性分数（低于此值继续尝试下一个候选）
        max_candidates: 最多扫描多少个候选 URL
    """

    def __init__(
        self,
        headless: bool = True,
        timeout: int = 120_000,
        quick_scan_timeout: int = 15_000,
        min_score: float = 0.5,
        max_candidates: int = 10,
    ):
        self.headless = headless
        self.timeout = timeout
        self.quick_scan_timeout = quick_scan_timeout
        self.min_score = min_score
        self.max_candidates = max_candidates

        # 延迟初始化（避免 import 时启动浏览器）
        self._extractor: Optional[UniversalExtractor] = None

    @property
    def extractor(self) -> UniversalExtractor:
        if self._extractor is None:
            self._extractor = UniversalExtractor(
                headless=self.headless,
                timeout=self.timeout,
            )
        return self._extractor

    # --------------------------------------------------------
    # 核心：搜 + 筛 + 抓
    # --------------------------------------------------------

    def search_and_extract(
        self,
        query: str,
        *,
        site_filter: Optional[str] = None,
        keyword_hint: Optional[str] = None,
    ) -> WebLensResult:
        """
        搜索 → 快速筛选 → 全链抓取的最佳结果。

        Args:
            query: 搜索关键词（如 "三体 小说 全文"）
            site_filter: 限定站点
            keyword_hint: 候选 URL 内容中必须包含的关键词（如 "三体"），
                          用于过滤"标题匹配但内容不相关"的页面

        Returns:
            WebLensResult
        """
        result = WebLensResult()

        # ---- Step 1: Search ----
        print(f"[WebLens] Searching: {query}")
        urls = search_urls(query, max_results=20, site_filter=site_filter)

        # 过滤：用分类器判断哪些 URL 像正文页
        filtered = []
        for u in urls:
            verdict = classify_url(u)
            if verdict["is_content"]:
                filtered.append(u)
                continue
            # 目录页也保留——快扫时如果只有目录会降分
            if verdict["type"] == "novel_index":
                filtered.append(u)

        logger.info("URL filter: %d/%d passed", len(filtered), len(urls))
        result.candidates_total = len(filtered)

        if not filtered:
            logger.warning("WebLens: all URLs filtered out as noise")
            return result

        print(f"[WebLens] Found {len(filtered)} candidate URLs "
              f"(filtered from {len(urls)} total)")

        # ---- Step 2: Quick Scan ----
        candidates = self._quick_scan(filtered[:self.max_candidates], keyword_hint)
        print(f"[WebLens] Quick-scanned {len(candidates)} candidates, "
              f"{len([c for c in candidates if c['score'] >= self.min_score])} passed threshold")

        if not candidates:
            logger.warning("WebLens: all candidates failed quick scan")
            return result

        # ---- Step 3: Full Extract from best candidate ----
        best = candidates[0]
        print(f"[WebLens] Best candidate: {best['url'][:100]} (score={best['score']:.2f})")

        try:
            full_text = self.extractor.extract(best["url"])
            result.url = best["url"]
            result.text = full_text
            result.score = completeness_score(full_text) if full_text else 0.0
            result.method = best.get("source", "unknown")
            result.candidates_scanned = len(candidates)
            print(f"[WebLens] Full extract: {len(full_text)} chars, "
                  f"final score={result.score:.2f}")
        except Exception as exc:
            logger.error("WebLens: full extract failed for %s: %s", best["url"], exc)
            # Return the quick-scan text as fallback
            result.url = best["url"]
            result.text = best.get("preview", "")
            result.score = best["score"]

        return result

    def _quick_scan(
        self,
        urls: list[str],
        keyword_hint: Optional[str] = None,
    ) -> list[dict]:
        """
        对候选 URL 快速扫描（只跑 Layer ① DOM），返回按分数排序的列表。

        每个候选扫描耗时约 3-5 秒（轻量 fetch，无完整浏览器启动）。
        """
        candidates = []

        for i, url in enumerate(urls):
            print(f"  [{i + 1}/{len(urls)}] Quick scan: {url[:80]}...")
            try:
                preview = self._fast_dom_scan(url)
            except Exception as exc:
                logger.debug("Quick scan failed for %s: %s", url, exc)
                continue

            if not preview or len(preview.strip()) < 50:
                continue

            # 用分类器评分：关键词匹配 + 内容类型 + 质量
            scored = score_content(preview, url=url, keyword=keyword_hint)
            quality = scored["quality"]
            content_type = scored["type"]

            # 目录页不直接拒绝，但严重降分
            if content_type == "novel_index":
                quality -= 0.30

            # 关键词完全没命中 → 跳过
            if keyword_hint and scored["keyword_density"] == 0.0:
                continue

            if quality <= 0.0:
                continue

            candidates.append({
                "url": url,
                "preview": preview[:2000],
                "score": round(max(0.0, quality), 3),
                "length": len(preview),
                "type": content_type,
                "keyword_match": scored["keyword_density"] > 0,
            })

            # 早停：质量高且关键词在开头
            if quality >= 0.7 and scored["keyword_head_bonus"]:
                break

        # 按分数降序
        candidates.sort(key=lambda c: c["score"], reverse=True)
        return candidates

    def _fast_dom_scan(self, url: str) -> str:
        """
        快速 DOM 扫描——两级降级：

        ① Jina Reader（免费、快、~1 秒/URL）
           → 适合静态 HTML 页面，Agent Reach 的主力读取方式
        ② StealthyFetcher 轻量 fetch（5-8 秒/URL）
           → Jina 拿到空壳时（SPA/JS渲染/反爬）才启用
        """
        # ---- Level 1: Jina Reader ----
        text = self._via_jina(url)
        if text and len(text.strip()) > 200:
            return text[:5000]

        # ---- Level 2: StealthyFetcher ----
        if text and len(text.strip()) > 50:
            # Jina 拿到了一点内容但不够——可能是 SPA，用真浏览器
            logger.debug("Jina returned only %d chars, falling back to browser", len(text))

        return self._via_browser(url)

    @staticmethod
    def _via_jina(url: str) -> str:
        """
        Jina Reader — 免费网页转 Markdown 服务。

        Agent Reach 的主力读取方式，不需要 API Key，1 秒内返回。
        只对静态 HTML 有效，SPA/画布页面返回空壳。
        """
        try:
            import urllib.request

            req = urllib.request.Request(
                f"https://r.jina.ai/{url}",
                headers={
                    "Accept": "text/markdown",
                    "User-Agent": "WebLens/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                # Jina 的响应编码在 header 里
                charset = resp.headers.get_content_charset() or "utf-8"
                text = resp.read().decode(charset, errors="replace")

            # Jina 返回的 Markdown 前几行可能是元信息，跳过
            if text.startswith("Title:"):
                lines = text.split("\n")
                # 找到第一个空行后的内容
                for i, line in enumerate(lines):
                    if line.strip() == "" and i > 0:
                        text = "\n".join(lines[i + 1:])
                        break

            logger.debug("Jina: %d chars from %s", len(text), url[:60])
            return text.strip() if text else ""
        except Exception as exc:
            logger.debug("Jina failed for %s: %s", url[:60], exc)
            return ""

    def _via_browser(self, url: str) -> str:
        """
        StealthyFetcher 轻量扫——Jina 拿不到内容时的降级。
        启动真浏览器，能处理 SPA/反爬页面。
        """
        from scrapling import StealthyFetcher

        collected = []

        def action(page):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
                page.wait_for_timeout(1000)
                text = page.evaluate("""() => {
                    var body = document.body;
                    if (!body || !body.innerText) return '';
                    return body.innerText.trim().slice(0, 5000);
                }""")
                if text:
                    collected.append(text)
            except Exception:
                pass

        try:
            StealthyFetcher.fetch(
                url,
                headless=self.headless,
                timeout=self.quick_scan_timeout,
                page_action=action,
                network_idle=False,
            )
        except Exception:
            pass

        return collected[0] if collected else ""

    # --------------------------------------------------------
    # 便捷方法
    # --------------------------------------------------------

    def search_and_extract_multi(
        self,
        query: str,
        max_results: int = 3,
        **kwargs,
    ) -> list[WebLensResult]:
        """
        搜 + 筛 + 抓 多个最佳结果（不只取第一个）。
        """
        results = []
        used_urls = set()

        for _ in range(max_results):
            result = self.search_and_extract(query, **kwargs)
            if result.text and result.url not in used_urls:
                used_urls.add(result.url)
                results.append(result)

            if result.score < self.min_score:
                break

        return results
