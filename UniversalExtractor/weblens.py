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

import re
import logging
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

from .extractor import UniversalExtractor
from .completeness import completeness_score
from .search import search_urls
from .classifier import classify_url, score_content, match_keywords

logger = logging.getLogger(__name__)

# ============================================================
# Shared constants（跨模块复用，减少选择器/JS 重复）
# ============================================================

# 小说章节正文的 CSS 选择器优先级列表
_CHAPTER_SELECTORS = [
    "#chaptercontent", "#content", ".chapter-content",
    "#booktxt", "#txt", ".showtxt", ".read-content", "#TextContent",
    "article", "#htmlContent", ".article-content",
    ".novel-content", "#nr1", "#booktext",
]

# 页面导航检测 JS（区分"下一页"和"下一章"）
_PAGE_NAV_JS = """() => {
    var r = {next_page: '', next_chapter: ''};
    var links = document.querySelectorAll('a');
    for (var i = 0; i < links.length; i++) {
        var a = links[i];
        var t = (a.innerText || a.textContent || '').trim();
        var h = a.getAttribute('href') || '';
        if (!h || h.startsWith('javascript:') || h === '#') continue;
        if (/^下一页$|^下一頁$/.test(t) && !r.next_page) r.next_page = h;
        if (/下一章|下一节/.test(t) && !r.next_chapter) r.next_chapter = h;
    }
    return r;
}"""

# 字体加密检测：Unicode 私用区 (PUA) 区间
# 网站用自定义字体把中文字符映射到 PUA 码点来反爬
_FONT_ENCRYPT_RANGES = [
    (0xE000, 0xF8FF),   # BMP Private Use Area
    (0xF0000, 0xFFFFD), # Supplementary PUA-A
    (0x100000, 0x10FFFD), # Supplementary PUA-B
]


# ============================================================
# Helpers
# ============================================================

_CN_NUMS = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10, "百": 100, "千": 1000,
}


def _cn_num_to_int(s: str) -> int:
    """中文数字 → 整数。'三十五' → 35, '一百二十三' → 123。"""
    s = s.strip()
    # 先试纯数字
    try:
        return int(s)
    except ValueError:
        pass

    total = 0
    section = 0
    for ch in s:
        if ch in _CN_NUMS:
            val = _CN_NUMS[ch]
            if val >= 10:
                section = (section or 1) * val
                total += section
                section = 0
            else:
                section = val
        else:
            break
    total += section
    return total if total > 0 else int(s) if s.isdigit() else 1


def _guess_chapter_url(base_url: str, chapter_num: int) -> str:
    """
    推测章节 URL——常见模式：
      /book/123.html → /book/124.html (递增)
      /book/123/ → /book/1/ (用章节号)
    """
    # 模式 1：URL 以数字结尾 → 替换为章节号
    m = re.match(r"(.+/)(\d+)(\.html?)?$", base_url)
    if m:
        return f"{m.group(1)}{chapter_num}{m.group(3) or ''}"

    # 模式 2：URL 含数字 → 替换
    replaced = re.sub(r"/(\d+)(\.html?)?$", f"/{chapter_num}\\2", base_url)
    if replaced != base_url:
        return replaced

    # 模式 3：拼接 chapter/N
    base = base_url.rstrip("/")
    return f"{base}/{chapter_num}.html"


def _has_font_encryption(text: str) -> bool:
    """
    检测文本中是否含字体加密字符（Unicode PUA 码点）。

    很多小说站用自定义字体把中文字符映射到私用区（PUA），
    导致提取到的文本是一堆乱码符号。此函数检测这种情况。
    """
    if not text:
        return False
    # 快速抽样：检查前 2000 字符
    sample = text[:2000]
    for start, end in _FONT_ENCRYPT_RANGES:
        if any(ord(c) >= start and ord(c) <= end for c in sample):
            return True
    return False


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

        Delegates to Pipeline.run() for the full closed-loop pipeline.
        Falls back to legacy WebLens logic on Pipeline failure.

        Args:
            query: 搜索关键词（如 "三体 小说 全文"）
            site_filter: 限定站点
            keyword_hint: 候选 URL 内容中必须包含的关键词（如 "三体"），
                          用于过滤"标题匹配但内容不相关"的页面

        Returns:
            WebLensResult
        """
        # Try new Pipeline first
        try:
            from .pipeline import Pipeline, PipelineConfig
            pipeline = Pipeline(PipelineConfig(
                headless=self.headless,
                timeout=self.timeout,
                quick_scan_timeout=self.quick_scan_timeout,
                min_completeness=self.min_score,
                max_candidates=self.max_candidates,
                site_filter=site_filter,
            ))
            pr = pipeline.run(query=query, keyword_hint=keyword_hint)
            if pr.success and pr.text:
                return WebLensResult(
                    url=pr.url or "",
                    text=pr.text,
                    score=pr.score,
                    method=pr.winning_stage,
                    source_layer=next(
                        (s.stage_index for s in pr.extraction_chain
                         if s.stage_name == pr.winning_stage), 0),
                    candidates_scanned=pr.search_candidates_scanned,
                    candidates_total=pr.search_candidates_total,
                )
        except Exception as exc:
            logger.debug("Pipeline failed, using legacy WebLens: %s", exc)

        # Legacy fallback
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
        print(f"[WebLens] Best candidate: {best['url'][:100]} "
              f"(score={best['score']:.2f}, type={best.get('type', '?')})")

        try:
            full_text = self.extractor.extract(best["url"])
            result.url = best["url"]
            result.text = full_text
            result.score = completeness_score(full_text) if full_text else 0.0
            result.method = best.get("source", "unknown")
            result.candidates_scanned = len(candidates)
            print(f"[WebLens] Full extract: {len(full_text)} chars, "
                  f"score={result.score:.2f}")
        except Exception as exc:
            logger.error("WebLens: full extract failed for %s: %s", best["url"], exc)
            result.url = best["url"]
            result.text = best.get("preview", "")
            result.score = best["score"]
            return result

        # ---- Step 4: If we got a novel index, follow first chapter ----
        content_type = best.get("type", "")
        if content_type == "novel_index" or self._looks_like_index(full_text):
            print(f"[WebLens] Detected index page — following first chapter...")
            chapter_text, chapter_url = self._extract_first_chapter(
                full_text, base_url=best["url"], keyword=keyword_hint or query
            )
            if chapter_text and len(chapter_text) > len(full_text) * 0.5:
                result.url = chapter_url or best["url"]
                result.text = chapter_text
                result.score = completeness_score(chapter_text)
                result.source_layer = 1
                print(f"[WebLens] Chapter extract: {len(chapter_text)} chars, "
                      f"score={result.score:.2f}")

        return result

    # --------------------------------------------------------
    # Intra-chapter pagination
    # --------------------------------------------------------

    @staticmethod
    def follow_chapter_pages(page, max_pages: int = 10) -> list[str]:
        """
        跟踪章节内分页：检测当前页是否有"下一页"（同章翻页），
        有则持续跟随直到遇到"下一章"（跨章）或到底。

        返回每页的正文列表。

        Args:
            page: Playwright Page 对象（已加载的页面）
            max_pages: 最大跟踪页数，防止死循环

        Returns:
            每页正文文本的列表
        """
        pages_text = []

        for _ in range(max_pages):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                page.wait_for_timeout(1000)
            except Exception:
                pass

            # Extract text（使用统一的共享选择器）
            sels_js = ",".join(_CHAPTER_SELECTORS)
            text = page.evaluate(f"""() => {{
                var sels = {sels_js!r}.split(',');
                for (var j = 0; j < sels.length; j++) {{
                    var el = document.querySelector(sels[j]);
                    if (el && el.innerText && el.innerText.trim().length > 50)
                        return el.innerText.trim();
                }}
                return (document.body && document.body.innerText)
                    ? document.body.innerText.trim() : '';
            }}""") or ""

            if text:
                text = re.sub(
                    r'(加入书架|收藏|推荐|分享|打赏|举报|书签|手机版|电脑版|下载|安装).*',
                    '', text,
                )
                pages_text.append(text)

            # Check navigation（使用统一的共享 JS）
            nav = page.evaluate(_PAGE_NAV_JS)
            next_page = nav.get("next_page", "")
            next_chapter = nav.get("next_chapter", "")

            # 优先检查"下一章"——有就说明当前章节结束
            if next_chapter:
                break
            # 只有"下一页"（同章翻页）才继续
            if next_page:
                try:
                    new_url = urljoin(page.url, next_page)
                    page.goto(new_url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    break
            else:
                break

        return pages_text

    # --------------------------------------------------------
    # Novel chapter following
    # --------------------------------------------------------

    @staticmethod
    def _looks_like_index(text: str) -> bool:
        """
        判断文本是否像章节目录页。

        特征：大量"第X章"链接、章节数 > 段落数、单行长度短。
        """
        if not text or len(text) < 200:
            return False

        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

        # 章节标记数量
        chapter_marks = len(re.findall(
            r"第[一二三四五六七八九十百千\d]+[章节回卷部]",
            text,
        ))

        # 高密度章节标记 → 目录页
        if chapter_marks >= 20:
            return True

        # 行数多但平均行长短 → 很可能是链接列表
        if len(lines) >= 15:
            avg_len = sum(len(ln) for ln in lines) / len(lines)
            if avg_len < 40 and chapter_marks >= 5:
                return True

        return False

    @staticmethod
    def _extract_chapter_links(
        text: str, base_url: str,
    ) -> list[dict]:
        """
        从目录页文本中提取章节链接。

        文本中通常有 "第X章  标题  链接URL" 这样的模式。
        我们从文本中找 URL + 章节号，按章节号排序。

        Returns:
            [{"num": 1, "title": "疯狂年代", "url": "https://..."}, ...]
        """

        chapters = []

        # 从完整页面文本中提取所有 URL
        all_urls = re.findall(r"https?://[^\s\n\"']+", text)
        # 也提取相对路径
        relative_urls = re.findall(
            r'(?:href|src|打开|阅读|链接)[=:"]*(/\S+?\.html?)',
            text, re.IGNORECASE,
        )

        # 找到章节标记所在行，提取章节号和标题
        chapter_pattern = re.compile(
            r"第\s*([一二三四五六七八九十百千\d]+)\s*[章节回卷部]\s*(.*?)(?:\s|$)"
        )
        lines = text.split("\n")

        for i, line in enumerate(lines):
            m = chapter_pattern.search(line)
            if not m:
                continue

            num_str = m.group(1)
            title = m.group(2).strip()[:60]

            # 中文数字转整数
            try:
                num = _cn_num_to_int(num_str)
            except ValueError:
                continue

            # 在同一行或下一行找 URL
            url = ""
            for offset in [0, 1, 2]:
                idx = i + offset
                if 0 <= idx < len(lines):
                    found = re.findall(r"(https?://[^\s\n\"']+)", lines[idx])
                    if found:
                        url = found[0]
                        break
                    found_rel = re.findall(
                        r'href\s*=\s*"([^"]+)"', lines[idx], re.IGNORECASE,
                    )
                    if found_rel:
                        url = found_rel[0]
                        break

            # 没有显式 URL：尝试用章节号构造
            if not url and base_url:
                url = _guess_chapter_url(base_url, num)
            if not url:
                url = base_url

            # 补全相对 URL
            if not url.startswith("http"):
                url = urljoin(base_url, url)

            chapters.append({
                "num": num,
                "title": title,
                "url": url,
            })

        # 按章节号排序
        chapters.sort(key=lambda c: c["num"])
        return chapters

    def _extract_first_chapter(
        self,
        index_text: str,
        base_url: str,
        keyword: str = "",
    ) -> tuple[str, str]:
        """
        从目录页跟进第一个章节，抓取正文。

        用真浏览器加载目录页 → 从 DOM 提取章节链接
        → 跟进第一章 → 提取正文。
        """
        # Step 1: 用真浏览器加载目录页，从 DOM 提取章节链接
        from scrapling import StealthyFetcher
        chapter_links = []

        def collect_links(page):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                page.wait_for_timeout(3000)  # 等 SPA 渲染
                links = page.evaluate("""() => {
                    var chapters = [];
                    var allLinks = document.querySelectorAll('a[href]');
                    var chPattern = /第[一二三四五六七八九十百千\\d]+[章节回卷部]/;
                    for (var i = 0; i < allLinks.length; i++) {
                        var a = allLinks[i];
                        var text = (a.innerText || a.textContent || '').trim();
                        var href = a.getAttribute('href') || '';
                        if (chPattern.test(text) && href) {
                            chapters.push({
                                text: text.slice(0, 100),
                                href: href
                            });
                        }
                    }
                    return chapters;
                }""")
                chapter_links.extend(links or [])
            except Exception:
                pass

        try:
            StealthyFetcher.fetch(
                base_url,
                headless=self.headless,
                timeout=30000,
                page_action=collect_links,
                network_idle=False,
            )
        except Exception as exc:
            logger.warning("Link extraction failed: %s", exc)

        if not chapter_links:
            # Fallback: from text
            chapters = self._extract_chapter_links(index_text, base_url)
            if chapters:
                chapter_links = [
                    {"text": f"第{ch['num']}章 {ch['title']}", "href": ch["url"]}
                    for ch in chapters[:1]
                ]

        if not chapter_links:
            logger.debug("No chapter links found")
            return "", ""

        first = chapter_links[0]
        chapter_url = first["href"]
        chapter_title = first.get("text", "")[:60]

        # Resolve relative URL
        if not chapter_url.startswith("http"):
            chapter_url = urljoin(base_url, chapter_url)

        print(f"  Chapter: {chapter_title}")
        print(f"  URL: {chapter_url[:100]}")

        # Step 2: 抓第一章正文（含章节内分页跟踪）
        try:
            collected_pages = []

            def extract_content(page):
                # 使用统一的章节分页跟踪，自动处理多页
                pages = WebLens.follow_chapter_pages(page)
                collected_pages.extend(pages)

            StealthyFetcher.fetch(
                chapter_url,
                headless=self.headless,
                timeout=30000,
                page_action=extract_content,
                network_idle=False,
            )

            chapter_text = "\n\n".join(collected_pages) if collected_pages else ""
            return chapter_text, chapter_url
        except Exception as exc:
            logger.warning("Chapter extract failed: %s", exc)
            return "", ""

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

            # 字体加密检测：PUA 码点 → 返回空触发浏览器降级
            if _has_font_encryption(text):
                logger.debug("Jina: font encryption detected for %s, returning empty", url[:60])
                return ""

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

        text = collected[0] if collected else ""
        if _has_font_encryption(text):
            logger.warning("Browser: font encryption detected for %s", url[:60])
        return text

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
