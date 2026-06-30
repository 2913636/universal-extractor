"""
Pipeline — 闭环爬虫编排引擎。

Search → Verify → Extract(fallback chain) → Validate → Return

用法:
    from UniversalExtractor.pipeline import Pipeline

    pipeline = Pipeline()
    result = pipeline.run("三体 小说 全文")
    # result.text → 提取的正文
    # result.score → 质量评分
    # result.extraction_chain → 每个阶段的结果
"""

from __future__ import annotations

import asyncio
import re
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Any, Callable

from .completeness import completeness_score, text_density_curve
from .classifier import classify_url, score_content

logger = logging.getLogger(__name__)


# ============================================================
# Data classes
# ============================================================

@dataclass
class PipelineConfig:
    """Pipeline 全局配置。"""

    # --- Browser ---
    headless: bool = True
    timeout: int = 120_000          # 浏览器超时 (ms)
    quick_scan_timeout: int = 15_000

    # --- Search ---
    search_backends: Optional[list[str]] = None   # ["brave","exa","duckduckgo"]
    search_max_results: int = 20
    site_filter: Optional[str] = None

    # --- Verify ---
    max_candidates: int = 10        # 最多快扫候选数
    min_preview_chars: int = 50     # 快扫最低字符数

    # --- Extract ---
    min_completeness: float = 0.5   # 停 fallback 的最低分数
    enabled_stages: Optional[list[str]] = None  # None = all
    max_extract_attempts: int = 3   # 最多尝试几个候选 URL

    # --- Validate ---
    require_keyword: bool = True    # 是否要求关键词命中
    max_front_heavy_ratio: float = 0.3  # 密度曲线前重后轻的最大比值

    # --- Cross Validation ---
    enable_cross_validation: bool = False
    max_cross_sources: int = 3

    # --- Rate Limiting ---
    rate_limit_delay: float = 2.0   # 域名间最小间隔 (s)

    # --- Proxy ---
    proxy: Optional[str] = None     # 代理 URL


@dataclass
class PipelineStageResult:
    """单个提取阶段的结果。"""

    stage_name: str = ""
    stage_index: int = 0
    success: bool = False
    text: str = ""
    char_count: int = 0
    completeness: float = 0.0
    timing_ms: int = 0
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.text:
            self.char_count = len(self.text)
            self.success = True
        if self.text and self.completeness == 0.0:
            try:
                self.completeness = completeness_score(self.text)
            except Exception:
                pass


@dataclass
class PipelineResult:
    """一次完整 pipeline 运行的结果。"""

    # --- Input ---
    query: str = ""
    url: Optional[str] = None

    # --- Output ---
    text: str = ""
    score: float = 0.0
    success: bool = False

    # --- Search ---
    search_backends_used: list[str] = field(default_factory=list)
    search_candidates_total: int = 0
    search_candidates_scanned: int = 0

    # --- Verify ---
    url_verdict: dict = field(default_factory=dict)
    content_verdict: dict = field(default_factory=dict)

    # --- Extract ---
    extraction_chain: list[PipelineStageResult] = field(default_factory=list)
    winning_stage: str = ""
    url_failures: list[dict] = field(default_factory=list)

    # --- Validate ---
    validation_details: dict = field(default_factory=dict)
    cross_validated: bool = False
    cross_validation_result: Optional[dict] = None

    # --- Timing ---
    total_time_ms: int = 0
    search_time_ms: int = 0
    extract_time_ms: int = 0
    validate_time_ms: int = 0

    @property
    def stages_attempted(self) -> int:
        return len(self.extraction_chain)

    @property
    def stages_succeeded(self) -> int:
        return sum(1 for s in self.extraction_chain if s.success)

    def stage_result(self, name: str) -> Optional[PipelineStageResult]:
        for s in self.extraction_chain:
            if s.stage_name == name:
                return s
        return None

    def best_stage(self) -> Optional[PipelineStageResult]:
        candidates = [s for s in self.extraction_chain if s.success]
        if not candidates:
            return None
        return max(candidates, key=lambda s: (s.completeness, s.char_count))


@dataclass
class StageContext:
    """阶段间共享上下文。"""

    url: str = ""
    original_query: str = ""
    keyword_hint: str = ""
    config: PipelineConfig = field(default_factory=PipelineConfig)

    # --- Cached artifacts ---
    html_body: str = ""
    html_headers: dict = field(default_factory=dict)
    screenshot_paths: list[str] = field(default_factory=list)
    stitched_image_path: str = ""
    detected_content_type: str = ""
    font_encryption_detected: bool = False

    # --- Browser lifecycle (shared by stages 2-6) ---
    _browser_session: Any = None
    _page: Any = None
    _page_setup_done: bool = False

    # --- Tracked results ---
    stage_results: list[PipelineStageResult] = field(default_factory=list)

    # --- Pipeline back-reference (set by Pipeline.run()) ---
    _pipeline: Any = None

    @property
    def best_completeness(self) -> float:
        scores = [s.completeness for s in self.stage_results if s.success]
        return max(scores) if scores else 0.0

    @property
    def browser_available(self) -> bool:
        return self._page is not None


# ============================================================
# ExtractionStage ABC + Registry
# ============================================================

class ExtractionStage(ABC):
    """提取技术的抽象基类。"""

    stage_name: str = ""
    stage_index: int = 0
    description: str = ""

    def __init__(self, config: Optional[dict] = None):
        self._stage_config = config or {}

    @abstractmethod
    def extract(self, url: str, context: StageContext) -> PipelineStageResult:
        """执行此阶段的提取。"""
        ...

    def can_handle(self, url: str, context: StageContext) -> bool:
        """预检查：此阶段是否可能对这个 URL 有效。"""
        return True


class StageRegistry:
    """按索引排序管理提取阶段。"""

    def __init__(self):
        self._stages: dict[str, ExtractionStage] = {}
        self._ordered: list[ExtractionStage] = []

    def register(self, stage: ExtractionStage) -> None:
        self._stages[stage.stage_name] = stage
        self._rebuild_order()

    def register_defaults(self) -> None:
        """注册所有内置阶段。"""
        self.register(JinaReaderStage())
        self.register(CurlCffiStage())
        self.register(BrowserDomStage())
        self.register(CanvasHookStage())
        self.register(CdpHeapStage())
        self.register(ScreenshotOcrStage())
        self.register(VisionLlmStage())

    def get_chain(
        self, enabled_only: Optional[list[str]] = None
    ) -> list[ExtractionStage]:
        """返回按 stage_index 排序的阶段列表。"""
        if enabled_only is not None:
            return [s for s in self._ordered if s.stage_name in enabled_only]
        return list(self._ordered)

    def _rebuild_order(self) -> None:
        self._ordered = sorted(
            self._stages.values(), key=lambda s: s.stage_index
        )


# ============================================================
# Stage 0: Jina Reader
# ============================================================

class JinaReaderStage(ExtractionStage):
    stage_name = "jina_reader"
    stage_index = 0
    description = "Jina Reader API — 免费网页转 Markdown，仅限静态 HTML。"

    def extract(self, url: str, context: StageContext) -> PipelineStageResult:
        t0 = time.time()
        result = PipelineStageResult(stage_name=self.stage_name,
                                      stage_index=self.stage_index)

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
                charset = resp.headers.get_content_charset() or "utf-8"
                text = resp.read().decode(charset, errors="replace")

            # Skip Jina metadata headers
            if text.startswith("Title:"):
                lines = text.split("\n")
                for i, line in enumerate(lines):
                    if line.strip() == "" and i > 0:
                        text = "\n".join(lines[i + 1:])
                        break

            text = text.strip() if text else ""

            # Font encryption check
            if text and self._has_font_encryption(text):
                result.success = False
                result.error = "font_encryption_detected"
                context.font_encryption_detected = True
                result.timing_ms = int((time.time() - t0) * 1000)
                return result

            if text and len(text) > 50:
                result.text = text
                result.success = True
                context.html_body = text

        except Exception as exc:
            result.error = str(exc)[:200]
            logger.debug("JinaReaderStage failed for %s: %s", url[:60], exc)

        result.timing_ms = int((time.time() - t0) * 1000)
        return result

    @staticmethod
    def _has_font_encryption(text: str) -> bool:
        sample = text[:2000]
        ranges = [(0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD)]
        for start, end in ranges:
            if any(ord(c) >= start and ord(c) <= end for c in sample):
                return True
        return False


# ============================================================
# Stage 1: curl_cffi HTTP
# ============================================================

class CurlCffiStage(ExtractionStage):
    stage_name = "curl_cffi_http"
    stage_index = 1
    description = "HTTP request + TLS fingerprint impersonation (curl_cffi). No browser. Impersonates Chrome TLS handshake."""

    def extract(self, url: str, context: StageContext) -> PipelineStageResult:
        t0 = time.time()
        result = PipelineStageResult(stage_name=self.stage_name,
                                      stage_index=self.stage_index)

        # Skip if Jina already got enough
        if context.best_completeness >= context.config.min_completeness:
            result.metadata["skipped"] = "previous stage sufficient"
            result.timing_ms = 0
            return result

        try:
            from scrapling import Fetcher
        except ImportError:
            result.error = "scrapling.Fetcher not available (need curl_cffi)"
            result.timing_ms = int((time.time() - t0) * 1000)
            return result

        try:
            fetcher = Fetcher()
            fetcher.configure(auto_referer=False, keep_alive=True)

            # Follow redirects (max 5 hops)
            current_url = url
            for _ in range(5):
                resp = fetcher.fetch(current_url)
                if resp and resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location") or resp.headers.get("location")
                    if loc:
                        from urllib.parse import urljoin as _urljoin
                        current_url = _urljoin(current_url, loc)
                        continue
                break

            if not resp or not resp.text:
                result.error = "empty response"
                result.timing_ms = int((time.time() - t0) * 1000)
                return result

            html = resp.text
            # Truncate oversized responses (>5MB → keep first 1MB)
            if len(html) > 5 * 1024 * 1024:
                logger.debug("CurlCffiStage: truncating %d bytes → 1MB", len(html))
                html = html[:1 * 1024 * 1024]

            context.html_body = html
            context.html_headers = dict(resp.headers) if resp.headers else {}

            # Auto encoding detection
            text = self._extract_text(html)
            if text and len(text) > 100:
                # Try chardet if text looks garbled
                if self._looks_garbled(text):
                    try:
                        import chardet
                        detected = chardet.detect(resp.content or b"")
                        if detected and detected.get("encoding"):
                            text = (resp.content or b"").decode(
                                detected["encoding"], errors="replace"
                            )
                            text = self._extract_text(text)
                    except ImportError:
                        pass

                if text and len(text) > 100:
                    result.text = text
                    result.success = True

        except Exception as exc:
            result.error = str(exc)[:200]
            logger.debug("CurlCffiStage failed for %s: %s", url[:60], exc)

        result.timing_ms = int((time.time() - t0) * 1000)
        return result

    @staticmethod
    def _looks_garbled(text: str) -> bool:
        """Check if text looks like mojibake (garbled encoding)."""
        sample = text[:500]
        # Count replacement chars and isolated high bytes
        bad = sum(1 for c in sample if c in '�\x00' or ord(c) > 0xFFFF)
        return bad > len(sample) * 0.1

    @staticmethod
    def _extract_text(html: str) -> str:
        """简易 HTML → 文本提取（无浏览器）。"""
        # 移除 script/style
        cleaned = re.sub(
            r'<(script|style|noscript|iframe|svg)[^>]*>.*?</\1>',
            '', html, flags=re.DOTALL | re.IGNORECASE,
        )
        # 移除标签
        cleaned = re.sub(r'<[^>]+>', ' ', cleaned)
        # 解码常见 HTML 实体
        cleaned = cleaned.replace('&nbsp;', ' ').replace('&amp;', '&')
        cleaned = cleaned.replace('&lt;', '<').replace('&gt;', '>')
        cleaned = cleaned.replace('&quot;', '"').replace('&#39;', "'")
        # 合并空白
        cleaned = re.sub(r'[ \t]+', ' ', cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()


# ============================================================
# Stage 2: Browser DOM
# ============================================================

class BrowserDomStage(ExtractionStage):
    stage_name = "browser_dom"
    stage_index = 2
    description = "Playwright 浏览器 DOM 提取 + API 响应拦截。处理 SPA/JS 页面。"

    # 正文选择器（与 weblens._CHAPTER_SELECTORS 保持一致）
    _CONTENT_SELECTORS = [
        "#chaptercontent", "#content", ".chapter-content",
        "#booktxt", "#txt", ".showtxt", ".read-content", "#TextContent",
        "article", "#htmlContent", ".article-content",
        ".novel-content", "#nr1", "#booktext",
        "main", ".post-content", '[class*="article"]',
        '[role="main"]',
    ]

    def extract(self, url: str, context: StageContext) -> PipelineStageResult:
        t0 = time.time()
        result = PipelineStageResult(stage_name=self.stage_name,
                                      stage_index=self.stage_index)

        # 如果之前已拿到足够内容
        if context.best_completeness >= context.config.min_completeness:
            result.metadata["skipped"] = "previous stage sufficient"
            result.timing_ms = 0
            return result

        try:
            from scrapling import StealthyFetcher

            collected: list[str] = []
            api_responses: list[str] = []

            def setup(page):
                """注入 API 拦截器。"""
                page.on("response", lambda resp: self._handle_response(resp, api_responses))

            def action(page):
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

                # DOM 提取
                sel_js = ",".join(self._CONTENT_SELECTORS)
                text = page.evaluate(f"""() => {{
                    var sels = {sel_js!r}.split(',');
                    for (var j = 0; j < sels.length; j++) {{
                        var el = document.querySelector(sels[j]);
                        if (el && el.innerText && el.innerText.trim().length > 50)
                            return el.innerText.trim();
                    }}
                    if (document.body && document.body.innerText)
                        return document.body.innerText.trim();
                    return '';
                }}""") or ""
                if text:
                    collected.append(text)
                # 保存 page 引用供后续阶段使用
                context._page = page

                # Captcha detection
                captcha_js = (
                    "(() => { return JSON.stringify({ detected: !!(document.querySelector('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]')), siteKey: (document.querySelector('[data-sitekey]') || {}).getAttribute?.('data-sitekey') || '' }); })()"
                )
                try:
                    captcha_info = page.evaluate(captcha_js)
                    captcha_data = __import__('json').loads(captcha_info)
                    if captcha_data.get("detected"):
                        result.metadata["captcha_detected"] = True
                        result.metadata["captcha_site_key"] = captcha_data.get("siteKey", "")
                        logger.info("BrowserDomStage: captcha detected at %s", url[:80])
                        # Try solving if solver is available
                        from .captcha_solver import CaptchaSolver
                        solver = CaptchaSolver()
                        if solver.available and captcha_data.get("siteKey"):
                            captcha_result = solver.solve_recaptcha_v2(
                                captcha_data["siteKey"], url,
                            )
                            if captcha_result.solved:
                                page.evaluate(f"""
                                    document.getElementById('g-recaptcha-response').value = '{captcha_result.token}';
                                    if (typeof ___grecaptcha_cfg !== 'undefined') {{
                                        var cb = ___grecaptcha_cfg.clients[0];
                                        if (cb) cb.callback('{captcha_result.token}');
                                    }}
                                """)
                                page.wait_for_timeout(1000)
                                # Re-extract after captcha
                                retry_text = page.evaluate(f"""() => {{
                                    var sels = {sel_js!r}.split(',');
                                    for (var j = 0; j < sels.length; j++) {{
                                        var el = document.querySelector(sels[j]);
                                        if (el && el.innerText && el.innerText.trim().length > 50)
                                            return el.innerText.trim();
                                    }}
                                    return '';
                                }}""") or ""
                                if retry_text:
                                    collected.append(retry_text)
                                    result.metadata["captcha_solved"] = True
                except Exception:
                    pass

            # Anti-bot hardening
            domain = url.split("/")[2] if "://" in url else url
            fetch_kwargs = {
                "headless": context.config.headless,
                "timeout": context.config.timeout,
                "page_setup": setup,
                "page_action": action,
                "network_idle": False,
                # Anti-bot features
                "solve_cloudflare": True,
                "block_webrtc": True,
                "dns_over_https": True,
            }

            # Proxy
            proxy = None
            if hasattr(context, '_pipeline') and context._pipeline:
                proxy = context._pipeline.proxy_manager.get_proxy()
            if proxy:
                fetch_kwargs["proxy"] = proxy

            # Session persistence
            session_dir = None
            if hasattr(context, '_pipeline') and context._pipeline:
                session_dir = context._pipeline.sessions.get_profile(domain)
            if session_dir:
                fetch_kwargs["user_data_dir"] = str(session_dir)

            StealthyFetcher.fetch(url, **fetch_kwargs)

            # 合并 DOM + API 响应
            all_text = "\n\n".join(collected)
            if api_responses:
                all_text += "\n\n" + "\n---\n".join(api_responses)

            if all_text.strip():
                result.text = all_text.strip()
                result.success = True

        except Exception as exc:
            result.error = str(exc)[:200]
            logger.debug("BrowserDomStage failed for %s: %s", url[:60], exc)

        result.timing_ms = int((time.time() - t0) * 1000)
        return result

    @staticmethod
    def _handle_response(resp, api_responses: list[str]) -> None:
        try:
            ct = resp.headers.get("content-type", "")
            if "json" in ct:
                body = resp.text()
                if body and len(body) > 200:
                    api_responses.append(body[:5000])
            elif "html" in ct:
                body = resp.text()
                if body and len(body) > 500:
                    api_responses.append(body[:10000])
        except Exception:
            pass


# ============================================================
# Stage 3: Canvas Hook
# ============================================================

class CanvasHookStage(ExtractionStage):
    stage_name = "canvas_hook"
    stage_index = 3
    description = "Canvas fillText/strokeText 拦截 — 对付字体加密/Canvas 渲染的页面。"

    def extract(self, url: str, context: StageContext) -> PipelineStageResult:
        t0 = time.time()
        result = PipelineStageResult(stage_name=self.stage_name,
                                      stage_index=self.stage_index)

        if not context.browser_available:
            result.error = "browser not available (need BrowserDomStage first)"
            result.timing_ms = int((time.time() - t0) * 1000)
            return result

        try:
            from .canvas_hook import inject_canvas_hook

            page = context._page
            inject_canvas_hook(page)

            # rAF 轮询等待 Canvas 渲染
            collected = []
            poll_ms = 3000
            deadline = time.time() + poll_ms / 1000

            while time.time() < deadline:
                texts = page.evaluate("""() => {
                    var arr = window.__ueCanvasTexts || [];
                    return arr.slice(-500);
                }""")
                if texts:
                    collected.extend(texts)
                page.wait_for_timeout(300)

            if collected:
                # 去重
                seen = set()
                unique = []
                for t in collected:
                    if t and len(t) > 3 and t not in seen:
                        seen.add(t)
                        unique.append(t)
                text = "\n".join(unique)
                if len(text) > 100:
                    result.text = text
                    result.success = True

        except Exception as exc:
            result.error = str(exc)[:200]
            logger.debug("CanvasHookStage failed: %s", exc)

        result.timing_ms = int((time.time() - t0) * 1000)
        return result


# ============================================================
# Stage 4: CDP Heap Scan
# ============================================================

class CdpHeapStage(ExtractionStage):
    stage_name = "cdp_heap"
    stage_index = 4
    description = "CDP JS 堆扫描 — 搜索内存中的长字符串（Next.js 状态/嵌入数据）。"

    def extract(self, url: str, context: StageContext) -> PipelineStageResult:
        t0 = time.time()
        result = PipelineStageResult(stage_name=self.stage_name,
                                      stage_index=self.stage_index)

        if not context.browser_available:
            result.error = "browser not available"
            result.timing_ms = int((time.time() - t0) * 1000)
            return result

        try:
            page = context._page
            cdp = page.context.new_cdp_session(page)

            js = """() => {
                var found = [];
                try {
                    var seen = new WeakSet();
                    function walk(obj, depth) {
                        if (depth > 4 || !obj) return;
                        if (seen.has(obj)) return;
                        seen.add(obj);
                        try {
                            var keys = Object.keys(obj);
                            for (var i = 0; i < keys.length; i++) {
                                var v;
                                try { v = obj[keys[i]]; } catch(e) { continue; }
                                if (typeof v === 'string' && v.length > 80) {
                                    found.push(v.slice(0, 2000));
                                    if (found.length > 100) return;
                                }
                                if (typeof v === 'object') walk(v, depth + 1);
                            }
                        } catch(e) {}
                    }
                    walk(window, 0);
                } catch(e) {}
                return found;
            }"""

            result_obj = cdp.send(
                "Runtime.evaluate",
                {"expression": f"({js})()", "awaitPromise": False}
            )
            cdp.detach()

            texts = []
            if result_obj.get("result", {}).get("value"):
                texts = result_obj["result"]["value"]

            if texts:
                text = "\n".join(texts)
                if len(text) > 200:
                    result.text = text
                    result.success = True

        except Exception as exc:
            result.error = str(exc)[:200]
            logger.debug("CdpHeapStage failed: %s", exc)

        result.timing_ms = int((time.time() - t0) * 1000)
        return result


# ============================================================
# Stage 5: Screenshot OCR
# ============================================================

class ScreenshotOcrStage(ExtractionStage):
    stage_name = "screenshot_ocr"
    stage_index = 5
    description = "Screenshot + OCR — for Canvas/image-rendered pages."

    def extract(self, url: str, context: StageContext) -> PipelineStageResult:
        t0 = time.time()
        result = PipelineStageResult(stage_name=self.stage_name,
                                      stage_index=self.stage_index)

        if not context.browser_available:
            result.error = "browser not available"
            result.timing_ms = int((time.time() - t0) * 1000)
            return result

        try:
            from .screenshot import capture_views
            from .ocr_providers import auto_configure_providers

            page = context._page
            screenshots = capture_views(page)

            # Pixel-hash dedup: remove near-duplicate screenshots
            screenshots = self._dedup_screenshots(screenshots)
            context.screenshot_paths = screenshots

            if not screenshots:
                result.error = "no screenshots captured"
                result.timing_ms = int((time.time() - t0) * 1000)
                return result

            providers = auto_configure_providers()
            if not providers:
                result.error = "no OCR providers configured"
                result.timing_ms = int((time.time() - t0) * 1000)
                return result

            all_text = []
            for path in screenshots[:5]:
                best_text = ""
                best_conf = 0
                for provider in providers[:2]:
                    try:
                        ocr_result = provider.extract_text(path)
                        if isinstance(ocr_result, tuple):
                            text, conf = ocr_result
                        else:
                            text, conf = ocr_result, 0.5
                        if text and len(text) > 50:
                            if conf > best_conf:
                                best_text = text
                                best_conf = conf
                            if conf > 0.6:  # Good enough, don't try other providers
                                break
                    except Exception:
                        continue
                if best_text and best_conf > 0.4:
                    all_text.append(best_text)

            if all_text:
                result.text = "\n---\n".join(all_text)
                result.success = True
                result.metadata["ocr_confidence"] = best_conf

        except Exception as exc:
            result.error = str(exc)[:200]
            logger.debug("ScreenshotOcrStage failed: %s", exc)

        result.timing_ms = int((time.time() - t0) * 1000)
        return result

    @staticmethod
    def _dedup_screenshots(paths: list[str]) -> list[str]:
        """Remove near-duplicate screenshots based on perceptual hash."""
        if len(paths) <= 1:
            return paths
        try:
            from PIL import Image
            kept = [paths[0]]
            for p in paths[1:]:
                is_dup = False
                for k in kept:
                    if ScreenshotOcrStage._images_similar(p, k):
                        is_dup = True
                        break
                if not is_dup:
                    kept.append(p)
            return kept
        except ImportError:
            return paths

    @staticmethod
    def _images_similar(path_a: str, path_b: str, threshold: float = 0.95) -> bool:
        """Check if two images are nearly identical by pixel comparison."""
        try:
            from PIL import Image
            a = Image.open(path_a).resize((64, 64)).convert("L")
            b = Image.open(path_b).resize((64, 64)).convert("L")
            pixels_a = list(a.getdata())
            pixels_b = list(b.getdata())
            same = sum(1 for pa, pb in zip(pixels_a, pixels_b) if abs(pa - pb) < 5)
            return same / len(pixels_a) > threshold
        except Exception:
            return False


# ============================================================
# Stage 6: Vision LLM
# ============================================================

class VisionLlmStage(ExtractionStage):
    stage_name = "vision_llm"
    stage_index = 6
    description = "Full-page stitch + Vision LLM (GPT-4V/Claude). Last resort, most expensive."

    def extract(self, url: str, context: StageContext) -> PipelineStageResult:
        t0 = time.time()
        result = PipelineStageResult(stage_name=self.stage_name,
                                      stage_index=self.stage_index)

        try:
            from .ocr_providers import auto_configure_providers
            from .screenshot import dedup_screenshots, stitch_vertical

            paths = context.screenshot_paths
            if not paths and context.browser_available:
                from .screenshot import capture_views
                paths = capture_views(context._page)

            if not paths:
                result.error = "no screenshots available"
                result.timing_ms = int((time.time() - t0) * 1000)
                return result

            paths = dedup_screenshots(paths) or paths
            stitched = stitch_vertical(paths)
            context.stitched_image_path = stitched

            providers = auto_configure_providers()
            vision_providers = [
                p for p in providers
                if hasattr(p, 'extract_from_image')
            ]

            # Multi-model fallback: try all vision providers
            models_tried = []
            for provider in vision_providers:
                provider_name = getattr(provider, 'model_name',
                                        provider.__class__.__name__)
                try:
                    text = provider.extract_from_image(stitched)
                    models_tried.append({"model": provider_name, "chars": len(text) if text else 0})
                    if text and len(text) > 200:
                        result.text = text
                        result.success = True
                        # Estimate cost
                        result.metadata["models_tried"] = models_tried
                        result.metadata["estimated_tokens"] = len(text) // 4  # rough estimate
                        break
                except Exception as exc:
                    models_tried.append({"model": provider_name, "error": str(exc)[:100]})
                    continue

            result.metadata["models_tried"] = models_tried

        except Exception as exc:
            result.error = str(exc)[:200]
            logger.debug("VisionLlmStage failed: %s", exc)

        result.timing_ms = int((time.time() - t0) * 1000)
        return result


# ============================================================
# Pipeline — 闭环编排器
# ============================================================

class Pipeline:
    """闭环编排器: Search → Verify → Extract(fallback) → Validate → Return。"""

    def __init__(self, config: Optional[PipelineConfig] = None):
        self.config = config or PipelineConfig()
        self.registry = StageRegistry()
        self.registry.register_defaults()

        # Infrastructure
        from .http_client import HTTPClient
        from .rate_limiter import RateLimiter
        from .proxy_manager import ProxyManager
        from .session_manager import SessionManager

        self.http = HTTPClient(
            proxy=self.config.proxy,
            timeout=self.config.timeout // 1000,  # ms → seconds
        )
        self.limiter = RateLimiter(
            min_interval=self.config.rate_limit_delay,
        )
        self.proxy_manager = ProxyManager(
            proxy_urls=self.config.proxy,
        )
        self.sessions = SessionManager()

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def run(
        self,
        query: str = "",
        url: Optional[str] = None,
        *,
        keyword_hint: Optional[str] = None,
        site_filter: Optional[str] = None,
        mode: str = "auto",
    ) -> PipelineResult:
        """
        执行完整的爬虫闭环。

        Args:
            query: 搜索关键词（如 "三体 小说 全文"）
            url: 直接指定 URL（跳过搜索阶段）
            keyword_hint: 内容关键词（用于验证阶段）
            site_filter: 限定站点
            mode: "auto"（搜索+提取）| "extract_only"（只提取）| "cross_validate"

        Returns:
            PipelineResult
        """
        t_total = time.time()
        result = PipelineResult(query=query, url=url)
        keyword = keyword_hint or query

        # --- Phase 1: Search ---
        if url:
            # 直接指定 URL，跳过搜索
            candidates = [{"url": url, "preview": "", "score": 1.0, "type": "unknown"}]
            result.search_candidates_total = 1
        elif mode == "extract_only" and not query:
            return result  # 无事可做
        else:
            t_search = time.time()
            candidates = self._phase_search(query, site_filter, result)
            result.search_time_ms = int((time.time() - t_search) * 1000)

        if not candidates:
            logger.warning("Pipeline: no candidates found for '%s'", query)
            result.total_time_ms = int((time.time() - t_total) * 1000)
            return result

        # --- Phase 2: Verify ---
        ranked = self._phase_verify(candidates, keyword, result)
        result.search_candidates_scanned = len(ranked)

        if not ranked:
            logger.warning("Pipeline: all candidates failed verification")
            result.total_time_ms = int((time.time() - t_total) * 1000)
            return result

        # --- Phase 3 & 4: Extract + Validate (loop) ---
        t_extract = time.time()
        best_text = ""
        best_score = 0.0

        for attempt_url in ranked[:self.config.max_extract_attempts]:
            url_str = attempt_url["url"]

            # 初始化阶段上下文
            context = StageContext(
                url=url_str,
                original_query=query,
                keyword_hint=keyword,
                config=self.config,
                detected_content_type=attempt_url.get("type", ""),
                _pipeline=self,
            )

            # 运行 fallback 链
            chain = self.registry.get_chain(self.config.enabled_stages)
            for stage in chain:
                if not stage.can_handle(url_str, context):
                    continue

                stage_result = stage.extract(url_str, context)
                context.stage_results.append(stage_result)

                if stage_result.success:
                    # 即时验证
                    passes, details = self._validate(
                        stage_result.text,
                        keyword=keyword,
                        content_type=context.detected_content_type,
                    )
                    result.validation_details = details

                    if passes:
                        if stage_result.completeness > best_score:
                            best_text = stage_result.text
                            best_score = stage_result.completeness
                            result.url = url_str
                            result.winning_stage = stage.stage_name

                        # 分数达标 → 立即返回
                        if stage_result.completeness >= self.config.min_completeness:
                            logger.info(
                                "Pipeline: %s succeeded (%.2f), stopping chain",
                                stage.stage_name, stage_result.completeness,
                            )
                            break
                    # 否则继续下一阶段
                # 否则继续下一阶段

            # 记录这个 URL 的尝试
            result.extraction_chain.extend(context.stage_results)
            if not best_text:
                result.url_failures.append({
                    "url": url_str,
                    "stages_tried": len(context.stage_results),
                    "best_completeness": context.best_completeness,
                })

            # 如果已经有好的结果，停止尝试更多 URL
            if best_score >= self.config.min_completeness:
                break

        result.text = best_text
        result.score = best_score
        result.success = best_score >= self.config.min_completeness
        result.extract_time_ms = int((time.time() - t_extract) * 1000)

        # --- Phase 5 (optional): Cross Validation ---
        if (
            result.success
            and self.config.enable_cross_validation
            and query
        ):
            try:
                from .cross_validator import cross_validate
                cv_result = cross_validate(
                    query, max_sources=self.config.max_cross_sources,
                    headless=self.config.headless,
                )
                result.cross_validated = True
                result.cross_validation_result = cv_result
                logger.info("Pipeline: cross-validated with %d sources",
                           len(cv_result.get("sources_used", [])))
            except Exception as exc:
                logger.warning("Cross-validation failed: %s", exc)

        result.total_time_ms = int((time.time() - t_total) * 1000)

        # Log summary
        logger.info(
            "Pipeline done: success=%s, score=%.2f, stage=%s, "
            "candidates=%d/%d, time=%dms",
            result.success, result.score, result.winning_stage,
            result.search_candidates_scanned, result.search_candidates_total,
            result.total_time_ms,
        )

        return result

    async def run_async(
        self,
        query: str = "",
        url: Optional[str] = None,
        *,
        keyword_hint: Optional[str] = None,
        site_filter: Optional[str] = None,
        mode: str = "auto",
    ) -> PipelineResult:
        """
        Async version of run().
        Parallelizes search (all backends) and verify (all candidates).

        2-5x faster than sync run() for typical queries.
        """
        t_total = time.time()
        result = PipelineResult(query=query, url=url)
        keyword = keyword_hint or query

        # --- Phase 1: Search (async, parallel backends) ---
        if url:
            candidates = [{"url": url, "preview": "", "score": 1.0, "type": "unknown"}]
            result.search_candidates_total = 1
        elif mode == "extract_only" and not query:
            result.total_time_ms = int((time.time() - t_total) * 1000)
            return result
        else:
            t_search = time.time()
            candidates = await self._phase_search_async(query, site_filter, result)
            result.search_time_ms = int((time.time() - t_search) * 1000)

        if not candidates:
            result.total_time_ms = int((time.time() - t_total) * 1000)
            return result

        # --- Phase 2: Verify (async, parallel candidates) ---
        ranked = await self._phase_verify_async(candidates, keyword, result)
        result.search_candidates_scanned = len(ranked)

        if not ranked:
            result.total_time_ms = int((time.time() - t_total) * 1000)
            return result

        # --- Phase 3 & 4: Extract + Validate (same as sync — stages share browser) ---
        t_extract = time.time()
        best_text = ""
        best_score = 0.0

        for attempt_url in ranked[:self.config.max_extract_attempts]:
            url_str = attempt_url["url"]
            context = StageContext(
                url=url_str, original_query=query, keyword_hint=keyword,
                config=self.config,
                detected_content_type=attempt_url.get("type", ""),
                _pipeline=self,
            )
            chain = self.registry.get_chain(self.config.enabled_stages)
            for stage in chain:
                if not stage.can_handle(url_str, context):
                    continue
                stage_result = stage.extract(url_str, context)
                context.stage_results.append(stage_result)
                if stage_result.success:
                    passes, details = self._validate(
                        stage_result.text, keyword=keyword,
                        content_type=context.detected_content_type,
                    )
                    result.validation_details = details
                    if passes:
                        if stage_result.completeness > best_score:
                            best_text = stage_result.text
                            best_score = stage_result.completeness
                            result.url = url_str
                            result.winning_stage = stage.stage_name
                        if stage_result.completeness >= self.config.min_completeness:
                            break
            result.extraction_chain.extend(context.stage_results)
            if best_score >= self.config.min_completeness:
                break

        result.text = best_text
        result.score = best_score
        result.success = best_score >= self.config.min_completeness
        result.extract_time_ms = int((time.time() - t_extract) * 1000)
        result.total_time_ms = int((time.time() - t_total) * 1000)

        logger.info("Pipeline async: success=%s score=%.2f stage=%s time=%dms",
                     result.success, result.score, result.winning_stage,
                     result.total_time_ms)
        return result

    async def _phase_search_async(
        self, query: str, site_filter: Optional[str], result: PipelineResult
    ) -> list[dict]:
        """Phase 1 (async): Parallel search + classify."""
        from .search import search_with_metadata_async

        meta = await search_with_metadata_async(
            query, max_results=self.config.search_max_results,
            site_filter=site_filter or self.config.site_filter,
            backends=self.config.search_backends,
        )
        result.search_candidates_total = meta["total_unique"]
        result.search_backends_used = meta["backends_used"]

        candidates = []
        for item in meta["results"]:
            u = item["url"]
            verdict = classify_url(u)
            if not result.url_verdict:
                result.url_verdict = verdict
            if verdict["is_content"] or verdict["type"] == "novel_index":
                candidates.append({
                    "url": u, "type": verdict.get("type", "unknown"),
                    "cross_hits": item["cross_hits"],
                    "backends": item["backends"],
                })
        return candidates

    async def _phase_verify_async(
        self, candidates: list[dict], keyword: str, result: PipelineResult
    ) -> list[dict]:
        """Phase 2 (async): Parallel quick-scan + score."""
        import urllib.request

        async def _scan_one(candidate: dict) -> dict | None:
            url = candidate["url"]
            try:
                loop = asyncio.get_running_loop()
                req = urllib.request.Request(
                    f"https://r.jina.ai/{url}",
                    headers={"Accept": "text/markdown", "User-Agent": "WebLens/1.0"},
                )

                def _fetch():
                    try:
                        with urllib.request.urlopen(req, timeout=8) as resp:
                            charset = resp.headers.get_content_charset() or "utf-8"
                            return resp.read().decode(charset, errors="replace")
                    except Exception:
                        return ""

                preview = await loop.run_in_executor(None, _fetch)

                if not preview or len(preview.strip()) < self.config.min_preview_chars:
                    # Check if it is a captcha page
                    from .captcha_solver import CaptchaSolver
                    captcha = CaptchaSolver.detect_captcha(preview or "")
                    if captcha.detected:
                        logger.info("Verify: captcha detected at %s (type=%s)",
                                    url[:60], captcha.captcha_type)
                    return None

                scored = score_content(preview, url=url, keyword=keyword)
                quality = scored["quality"]
                if scored["type"] == "novel_index":
                    quality -= 0.30
                if quality <= 0.0:
                    return None

                return {
                    "url": url, "preview": preview[:2000],
                    "score": round(max(0.0, quality), 3),
                    "type": scored["type"],
                }
            except Exception:
                return None

        tasks = [_scan_one(c) for c in candidates[:self.config.max_candidates]]
        results = await asyncio.gather(*tasks)

        scored = [r for r in results if r is not None]
        scored.sort(key=lambda c: c["score"], reverse=True)
        result.content_verdict = {
            "top_score": scored[0]["score"] if scored else 0.0,
            "top_type": scored[0]["type"] if scored else "",
        }
        return scored

    # ----------------------------------------------------------------
    # Phase implementations
    # ----------------------------------------------------------------

    def _phase_search(
        self, query: str, site_filter: Optional[str], result: PipelineResult
    ) -> list[dict]:
        """Phase 1: 多引擎搜索 + 交叉对比 + 分类过滤。"""
        from .search import search_with_metadata

        meta = search_with_metadata(
            query,
            max_results=self.config.search_max_results,
            site_filter=site_filter or self.config.site_filter,
            backends=self.config.search_backends,
        )

        result.search_candidates_total = meta["total_unique"]
        result.search_backends_used = meta["backends_used"]

        # 分类过滤（cross_hit 越高的 URL 排名越前）
        candidates = []
        for item in meta["results"]:
            u = item["url"]
            verdict = classify_url(u)
            if not result.url_verdict:
                result.url_verdict = verdict
            if verdict["is_content"] or verdict["type"] == "novel_index":
                candidates.append({
                    "url": u,
                    "type": verdict.get("type", "unknown"),
                    "cross_hits": item["cross_hits"],
                    "backends": item["backends"],
                })

        logger.info("Search: %d raw/%d unique → %d filtered for '%s'",
                     meta["total_raw"], meta["total_unique"],
                     len(candidates), query[:40])
        return candidates

    def _phase_verify(
        self, candidates: list[dict], keyword: str, result: PipelineResult
    ) -> list[dict]:
        """Phase 2: 快速验证——Jina 快扫 + 评分排序。"""
        import urllib.request

        scored = []
        for i, c in enumerate(candidates[:self.config.max_candidates]):
            url = c["url"]
            print(f"  [{i + 1}/{min(len(candidates), self.config.max_candidates)}] "
                  f"Quick scan: {url[:80]}...")

            # Jina 快扫
            preview = ""
            try:
                req = urllib.request.Request(
                    f"https://r.jina.ai/{url}",
                    headers={
                        "Accept": "text/markdown",
                        "User-Agent": "WebLens/1.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    charset = resp.headers.get_content_charset() or "utf-8"
                    preview = resp.read().decode(charset, errors="replace")
            except Exception:
                continue

            if not preview or len(preview.strip()) < self.config.min_preview_chars:
                from .captcha_solver import CaptchaSolver
                captcha = CaptchaSolver.detect_captcha(preview or "")
                if captcha.detected:
                    logger.info("Verify: captcha detected at %s (type=%s)",
                                url[:60], captcha.captcha_type)
                continue

            # 评分
            scored_result = score_content(preview, url=url, keyword=keyword)
            quality = scored_result["quality"]

            # 目录页降分
            if scored_result["type"] == "novel_index":
                quality -= 0.30

            if quality <= 0.0:
                continue

            scored.append({
                "url": url,
                "preview": preview[:2000],
                "score": round(max(0.0, quality), 3),
                "type": scored_result["type"],
            })

            # 早停
            if quality >= 0.7 and scored_result.get("keyword_head_bonus"):
                break

        scored.sort(key=lambda c: c["score"], reverse=True)
        result.content_verdict = {
            "top_score": scored[0]["score"] if scored else 0.0,
            "top_type": scored[0]["type"] if scored else "",
        }
        return scored

    # ----------------------------------------------------------------
    # Validation
    # ----------------------------------------------------------------

    def _validate(
        self,
        text: str,
        keyword: str = "",
        content_type: str = "",
    ) -> tuple[bool, dict]:
        """
        Phase 4: 质量检查。

        检查项:
          1. completeness_score >= min_completeness
          2. 关键词命中（如果 require_keyword）
          3. 密度曲线不过分前重后轻
          4. 无字体加密
          5. 无严重 boilerplate

        Returns:
            (passes, details_dict)
        """
        details: dict = {"checks": [], "passed": 0, "failed": 0}
        passes = True

        # Check 1: Completeness
        comp = 0.0
        try:
            comp = completeness_score(text)
        except Exception:
            pass
        details["completeness"] = round(comp, 3)
        if comp < self.config.min_completeness:
            details["checks"].append(f"completeness {comp:.2f} < {self.config.min_completeness}")
            details["failed"] += 1
            passes = False
        else:
            details["checks"].append(f"completeness {comp:.2f} ✓")
            details["passed"] += 1

        # Check 2: Keyword
        if self.config.require_keyword and keyword:
            kw_hit = keyword.lower() in text.lower()
            details["keyword_hit"] = kw_hit
            if not kw_hit:
                details["checks"].append("keyword not found")
                details["failed"] += 1
                passes = False
            else:
                details["checks"].append("keyword found ✓")
                details["passed"] += 1

        # Check 3: Density curve (not extremely front-heavy)
        try:
            curve = text_density_curve(text, segments=5)
            details["density_curve"] = [round(v, 3) for v in curve]
            if curve and len(curve) >= 3:
                first_third = sum(curve[: len(curve)//3]) / max(len(curve)//3, 1)
                last_third = sum(curve[-(len(curve)//3):]) / max(len(curve)//3, 1)
                if first_third > 0 and last_third / first_third < self.config.max_front_heavy_ratio:
                    details["checks"].append(f"front-heavy curve (ratio={last_third/first_third:.2f})")
                    details["failed"] += 1
                    passes = False
                else:
                    details["checks"].append("density curve balanced ✓")
                    details["passed"] += 1
        except Exception:
            pass

        # Check 4: Font encryption (PUA chars)
        has_pua = self._has_pua(text)
        details["font_encrypted"] = has_pua
        if has_pua:
            details["checks"].append("font encryption detected (PUA chars)")
            details["failed"] += 1
            passes = False

        # Check 5: Boilerplate
        boilerplate_hits = self._count_boilerplate(text[:500])
        details["boilerplate_hits"] = boilerplate_hits
        if boilerplate_hits >= 3:
            details["checks"].append(f"heavy boilerplate ({boilerplate_hits} hits)")
            details["failed"] += 1
            passes = False

        # Check 6: Language coherence
        # Chinese text should not contain large blocks of garbled ASCII
        cjk_ratio = self._cjk_ratio(text[:2000])
        details["cjk_ratio"] = round(cjk_ratio, 3)
        if cjk_ratio < 0.05 and len(text) > 500:
            details["checks"].append(f"very low CJK ratio ({cjk_ratio:.3f}) — possible garbled text")
            details["failed"] += 1
            passes = False

        # Check 7: Minimum real content
        # After stripping whitespace, must have enough meaningful chars
        stripped = re.sub(r'\s+', '', text)
        if len(stripped) < 100:
            details["checks"].append(f"too little real content ({len(stripped)} chars)")
            details["failed"] += 1
            passes = False

        details["result"] = "PASS" if passes else "FAIL"
        return passes, details

    @staticmethod
    def _cjk_ratio(text: str) -> float:
        """Ratio of CJK characters in text (0-1)."""
        if not text:
            return 0.0
        cjk = sum(1 for c in text if '一' <= c <= '鿿')
        return cjk / len(text)

    @staticmethod
    def _has_pua(text: str) -> bool:
        """检测 Unicode 私用区字符（字体加密标志）。"""
        sample = text[:2000]
        ranges = [(0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD)]
        for start, end in ranges:
            if any(ord(c) >= start and ord(c) <= end for c in sample):
                return True
        return False

    @staticmethod
    def _count_boilerplate(text: str) -> int:
        """统计 boilerplate 关键词命中数。"""
        from .classifier import BOILERPLATE_KEYWORDS
        lower = text.lower()
        return sum(1 for kw in BOILERPLATE_KEYWORDS if kw.lower() in lower)
