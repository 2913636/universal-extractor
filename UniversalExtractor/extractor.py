"""
通用网页内容提取器 — 6 层降级链。

① DOM 提取 → ② API 拦截 → ③ Canvas Hook → ④ CDP 内存扫描 → ⑤ 截图 OCR → ⑥ Vision LLM 全页

用法:
    from universal_extractor import UniversalExtractor

    ue = UniversalExtractor(headless=False)
    text = ue.extract("https://www.kdocs.cn/l/chtgPO02obP9")
    print(text)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# --- Scrapling imports ---
from scrapling import StealthyFetcher

# --- Internal modules ---
from .canvas_hook import CANVAS_HOOK_JS, inject_canvas_hook
from .completeness import completeness_score, is_complete
from .ocr_providers import (
    VisionProvider,
    auto_configure_providers,
)
from .screenshot import capture_views, dedup_screenshots, stitch_vertical
from .scrolling import scroll_viewport, find_canvas_rect

logger = logging.getLogger(__name__)

# --- Encoding fallback chain ---
_ENCODINGS = ["utf-8", "gbk", "gb2312", "gb18030", "big5"]


# ============================================================
# Data classes
# ============================================================

@dataclass
class ExtractionResult:
    text: str
    source_layer: int       # 1–6
    method: str             # 'dom' | 'api' | 'canvas_hook' | 'cdp_heap' | 'ocr' | 'vision_llm'
    confidence: float = 0.5
    completeness: float = 0.0  # cached completeness_score()


class ExtractionError(Exception):
    """All layers returned empty or insufficient text."""


# ============================================================
# Utility functions (kept here — too small to justify a module)
# ============================================================

def _decode(body: bytes, hint: str | None = None) -> str:
    """Decode bytes to string with multi-encoding fallback."""
    encodings = [hint] if hint else []
    encodings += _ENCODINGS
    for enc in encodings:
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def _dig_for_text(data: Any, depth: int = 0, max_depth: int = 5) -> str | None:
    """Recursively walk JSON looking for the longest text field."""
    if depth > max_depth or data is None:
        return None
    candidates = []
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, str) and len(v) > 50 and not _looks_like_code(v):
                candidates.append(v)
            else:
                sub = _dig_for_text(v, depth + 1, max_depth)
                if sub:
                    candidates.append(sub)
    elif isinstance(data, list) and len(data) < 500:
        for item in data:
            sub = _dig_for_text(item, depth + 1, max_depth)
            if sub:
                candidates.append(sub)
    return max(candidates, key=len) if candidates else None


def _looks_like_code(text: str) -> bool:
    """Quick check: is this JS/JSON/CSS rather than human content?"""
    code_marks = ["function(", "=>", "=== ", "typeof", "import {", "export ",
                  "constructor(", "super(", "require("]
    return any(m in text for m in code_marks)


def _clean_ocr_text(text: str) -> str:
    """Post-process OCR output: remove common artifacts, merge broken lines."""
    text = re.sub(r" {3,}", "  ", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


# ============================================================
# Stream detection helpers
# ============================================================

_STREAMING_APP_MARKERS = [
    "kdocs.cn", "feishu.cn", "docs.qq.com",
    "wps.cn", "shimo.im", "dingtalk.com",
]


def _detect_streaming_app(page) -> bool:
    """Heuristic: is this a WebSocket-streaming document app?"""
    try:
        result = page.evaluate("""() => {
            // Known doc-app domains
            const host = location.hostname;
            const isDocApp = /kdocs|feishu|docs\\.qq|wps|shimo|dingtalk/i.test(host);
            if (isDocApp) return true;
            // Check for active WebSocket connections
            try {
                const entries = performance.getEntriesByType('resource') || [];
                for (const e of entries) {
                    if (e.name.startsWith('ws:') || e.name.startsWith('wss:'))
                        return true;
                }
            } catch(e) {}
            // Check for canvas presence (indirect signal)
            const canvases = document.querySelectorAll('canvas');
            return canvases.length > 0 && document.body
                && (document.body.innerText || '').trim().length < 500;
        }""")
        return bool(result)
    except Exception:
        return False


# ============================================================
# Main class
# ============================================================

class UniversalExtractor:
    """Six-layer fallback web page text extractor.

    Parameters:
        headless: Run browser headless (True) or visible (False).
        timeout: Browser session timeout in milliseconds.
        vision_providers: List of VisionProvider instances.
                          Pass None to auto-configure from environment variables.
        max_passes: Max retry passes for streaming apps (default 3).
        pass_delays: Wait seconds between passes (default [3, 8, 15]).
        screenshot_overlap: Overlap ratio for scrolling screenshots (0.0-0.5).
    """

    def __init__(
        self,
        headless: bool = True,
        timeout: int = 120_000,
        vision_providers: list[VisionProvider] | None = None,
        max_passes: int = 3,
        pass_delays: list[int] | None = None,
        screenshot_overlap: float = 0.2,
    ):
        self.headless = headless
        self.timeout = timeout
        self.max_passes = max_passes
        self.pass_delays = pass_delays or [3, 8, 15]
        self.screenshot_overlap = screenshot_overlap

        # --- Vision providers ---
        if vision_providers is None:
            # Auto-configure from environment
            # Try to load .env first
            self._try_load_dotenv()
            self.vision_providers = auto_configure_providers()
        else:
            self.vision_providers = vision_providers

        if not self.vision_providers:
            logger.warning(
                "No vision providers configured. "
                "Layers ⑤ (OCR) and ⑥ (Vision LLM) will be unavailable. "
                "Set one of: OPENAI_API_KEY, ANTHROPIC_API_KEY, DASHSCOPE_API_KEY."
            )

        # --- Canvas scroll rect cache ---
        self._canvas_rect: dict[str, float] | None = None

        # Temp files
        self._temp_root: Path | None = None

    @staticmethod
    def _try_load_dotenv() -> None:
        """Try loading .env from common locations."""
        try:
            from dotenv import load_dotenv
            for env_path in ["D:/job-hunter/.env", ".env"]:
                if os.path.exists(env_path):
                    load_dotenv(env_path)
                    break
        except ImportError:
            pass

    # --------------------------------------------------------
    # Public API
    # --------------------------------------------------------

    def extract(self, url: str) -> str:
        """Run the full fallback chain and return the best text.

        Delegates to Pipeline.run() for the 7-stage fallback chain.
        Falls back to the original 6-layer chain on Pipeline failure.
        """
        # Try new Pipeline first
        try:
            from .pipeline import Pipeline, PipelineConfig
            pipeline = Pipeline(PipelineConfig(
                headless=self.headless,
                timeout=self.timeout,
            ))
            result = pipeline.run(url=url, mode="extract_only")
            if result.success and result.text:
                print(f"Pipeline: {result.winning_stage} (score={result.score:.2f})")
                return result.text.strip()
        except Exception as exc:
            logger.debug("Pipeline failed, falling back to legacy chain: %s", exc)

        # Legacy fallback: original 6-layer chain
        return self._extract_legacy(url)

    def _extract_legacy(self, url: str) -> str:
        self._temp_root = Path(tempfile.mkdtemp(prefix="ue_"))
        profile_dir = Path(tempfile.mkdtemp(prefix="ue_profile_"))
        results: list[ExtractionResult] = []

        try:
            # ---- Phase 1: Quick DOM-only attempt (fast path) ----
            print("Layer ①: Quick DOM extraction...")
            dom_text = self._quick_dom(url, profile_dir)
            score = completeness_score(dom_text) if dom_text else 0.0
            if dom_text:
                results.append(ExtractionResult(
                    dom_text, 1, "dom",
                    confidence=0.9 if score >= 0.5 else 0.3,
                    completeness=score,
                ))
            if score >= 0.5:
                print(f"Layer ① complete (score={score:.2f}) — returning early.")
                return dom_text.strip()

            # ---- Phase 2: Full extraction with all hooks ----
            print(f"Layers ②–⑥: Full extraction...")
            self._full_extraction(url, profile_dir, results)

        except Exception as exc:
            logger.error("Extraction error: %s", exc)
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)
            if self._temp_root is not None:
                shutil.rmtree(str(self._temp_root), ignore_errors=True)

        return self._pick_best(results)

    # --------------------------------------------------------
    # Layer ① — Quick DOM
    # --------------------------------------------------------

    def _quick_dom(self, url: str, profile_dir: Path) -> str:
        """Lightweight fetch: run a single page_action, return DOM text."""
        collected: list[str] = []

        def action(page):
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)
            text = self._dom_extract(page)
            if text:
                collected.append(text)

        try:
            StealthyFetcher.fetch(
                url,
                headless=self.headless,
                timeout=30000,
                user_data_dir=str(profile_dir),
                page_action=action,
                network_idle=True,
            )
        except Exception:
            pass

        return collected[0] if collected else ""

    def _dom_extract(self, page) -> str:
        """Extract text from DOM: deep traverse (Shadow DOM, iframes, SVG, rich-text editors)."""
        return page.evaluate("""() => {
            // ==============================================
            // Step 0: Trigger lazy content
            // ==============================================
            // Expand collapsed sections
            document.querySelectorAll('[aria-expanded="false"]')
                .forEach(function(el) { try { el.click() } catch(e) {} });
            // Trigger lazy images/content at bottom
            window.scrollTo(0, document.body.scrollHeight);
            window.scrollTo(0, 0);

            // ==============================================
            // Step 1: Deep text extraction (Shadow DOM aware)
            // ==============================================
            function deepText(node) {
                if (!node) return '';
                var text = '';
                var children = (node.shadowRoot || node).childNodes;
                for (var i = 0; i < children.length; i++) {
                    var c = children[i];
                    if (c.nodeType === 3) {          // Text node
                        text += c.textContent;
                    } else if (c.nodeType === 1) {   // Element
                        // Recurse into shadow DOM of custom elements
                        if (c.shadowRoot) {
                            text += deepText(c.shadowRoot);
                        }
                        text += deepText(c);
                    }
                }
                return text;
            }

            // ==============================================
            // Step 2: Multi-selector content extraction
            // ==============================================
            var selectors = [
                'article', 'main', '.content', '.article-content',
                '#content', '.post-content', '[class*="doc"]',
                '[class*="editor"]', '[class*="page-content"]',
                '[role="main"]', '[class*="article"]', '[class*="post"]'
            ];
            var best = '';
            for (var s = 0; s < selectors.length; s++) {
                var el = document.querySelector(selectors[s]);
                if (el) {
                    var t = deepText(el).trim();
                    if (t.length > best.length) best = t;
                }
            }

            // ==============================================
            // Step 3: Rich-text editor content
            // ==============================================
            var editorSelectors = [
                '.ql-editor', '.ProseMirror', '.tox-edit-area__iframe',
                '[contenteditable="true"]', '.CodeMirror-code',
                '[class*="editor"] [class*="content"]',
                '.monaco-editor .view-lines'
            ];
            for (var e = 0; e < editorSelectors.length; e++) {
                var ed = document.querySelector(editorSelectors[e]);
                if (ed && ed.innerText) {
                    var et = ed.innerText.trim();
                    if (et.length > best.length) best = et;
                }
            }

            // ==============================================
            // Step 4: SVG text nodes
            // ==============================================
            try {
                var svgTexts = document.querySelectorAll('svg text');
                var svgContent = '';
                svgTexts.forEach(function(t) {
                    svgContent += t.textContent + '\\n';
                });
                if (svgContent.trim().length > best.length) best = svgContent.trim();
            } catch(e) {}

            // ==============================================
            // Step 5: Same-origin iframe content
            // ==============================================
            try {
                var iframes = document.querySelectorAll('iframe');
                for (var f = 0; f < iframes.length; f++) {
                    try {
                        var doc = iframes[f].contentDocument;
                        if (doc && doc.body && doc.body.innerText) {
                            var ft = doc.body.innerText.trim();
                            if (ft.length > best.length) best = ft;
                        }
                    } catch(e) {}  // cross-origin → skip
                }
            } catch(e) {}

            // ==============================================
            // Step 6: Fallback — full body deep traversal
            // ==============================================
            if (best.length < 200 && document.body) {
                best = deepText(document.body).trim();
            }
            if (!best && document.body && document.body.innerText) {
                best = document.body.innerText.trim();
            }

            return best;
        }""")

    # --------------------------------------------------------
    # Phase 2 — Full extraction orchestration
    # --------------------------------------------------------

    def _full_extraction(
        self, url: str, profile_dir: Path, results: list[ExtractionResult]
    ) -> None:
        """Set up all hooks via page_setup, run extraction via page_action."""
        captured_api_texts: list[str] = []

        # Write Canvas Hook JS to temp file for init_script injection
        hook_script_path = str(self._temp_root / "canvas_hook.js")
        Path(hook_script_path).write_text(CANVAS_HOOK_JS, encoding="utf-8")

        def setup(page):
            """page_setup: register interceptors + inject hooks before navigation."""
            # Layer ② — intercept API responses
            page.on("response", self._make_api_handler(captured_api_texts))

            # Layer ③ — multi-vector Canvas Hook injection
            inject_canvas_hook(page, hook_script_path)

            # Pre-compute canvas rect for scrolling
            self._canvas_rect = find_canvas_rect(page)

        def action(page):
            """page_action: run all layers after page loads."""
            # Wait for full render
            page.wait_for_load_state("networkidle", timeout=30000)
            page.wait_for_timeout(5000)

            # Dismiss modals
            page.keyboard.press("Escape")
            page.wait_for_timeout(1000)

            # ---- First pass: collect all layers ----
            self._run_extraction_layers(page, captured_api_texts, results)

            # ---- Multi-pass for streaming apps ----
            if self.max_passes > 0 and _detect_streaming_app(page):
                logger.info("Streaming app detected — multi-pass extraction")
                current_best = self._get_best_text(results)
                current_score = (
                    completeness_score(current_best) if current_best else 0.0
                )

                for pass_num in range(min(self.max_passes, len(self.pass_delays))):
                    if current_score >= 0.85:
                        logger.info(
                            "Pass %d: score=%.2f >= 0.85, stopping",
                            pass_num + 1, current_score,
                        )
                        break

                    delay = self.pass_delays[pass_num]
                    logger.info(
                        "Pass %d: score=%.2f, waiting %ds...",
                        pass_num + 1, current_score, delay,
                    )
                    page.wait_for_timeout(delay * 1000)

                    # Re-collect DOM and Canvas (incremental)
                    dom_text = self._dom_extract(page)
                    if dom_text:
                        results.append(ExtractionResult(
                            dom_text, 1, f"dom_pass{pass_num + 2}",
                            confidence=0.85,
                        ))

                    canvas_text = self._canvas_collect(page)
                    if canvas_text:
                        results.append(ExtractionResult(
                            canvas_text, 3, f"canvas_hook_pass{pass_num + 2}",
                            confidence=0.75,
                        ))

                    # Update score
                    current_best = self._get_best_text(results)
                    current_score = (
                        completeness_score(current_best) if current_best else 0.0
                    )

        try:
            StealthyFetcher.fetch(
                url,
                headless=self.headless,
                timeout=self.timeout,
                user_data_dir=str(profile_dir),
                page_setup=setup,
                page_action=action,
                network_idle=True,
                disable_resources=False,
            )
        except Exception as exc:
            logger.warning("Full extraction fetch error: %s", exc)

    def _run_extraction_layers(
        self,
        page,
        captured_api_texts: list[str],
        results: list[ExtractionResult],
    ) -> None:
        """Execute all extraction layers and append to results."""
        # Layer ① — DOM
        dom_text = self._dom_extract(page)
        if dom_text:
            score = completeness_score(dom_text)
            results.append(ExtractionResult(
                dom_text, 1, "dom",
                confidence=0.9 if score >= 0.5 else 0.3,
                completeness=score,
            ))

        # Layer ② — API responses
        for api_text in captured_api_texts:
            if api_text and len(api_text) > 50:
                score = completeness_score(api_text)
                results.append(ExtractionResult(
                    api_text, 2, "api",
                    confidence=0.7 if score >= 0.5 else 0.4,
                    completeness=score,
                ))

        # Layer ③ — Canvas Hook
        canvas_text = self._canvas_collect(page)
        if canvas_text:
            results.append(ExtractionResult(
                canvas_text, 3, "canvas_hook", confidence=0.8,
            ))

        # Layer ④ — CDP heap scan
        cdp_text = self._cdp_scan(page)
        if cdp_text:
            results.append(ExtractionResult(
                cdp_text, 4, "cdp_heap", confidence=0.5,
            ))

        # Layer ⑤ — Screenshot + OCR
        ocr_text = self._ocr_extract(page)
        if ocr_text:
            results.append(ExtractionResult(
                ocr_text, 5, "ocr", confidence=0.6,
            ))

        # Layer ⑥ — Vision LLM full-page
        vision_text = self._vision_llm_fullpage(page)
        if vision_text:
            results.append(ExtractionResult(
                vision_text, 6, "vision_llm", confidence=0.75,
            ))

    # --------------------------------------------------------
    # Layer ② — API Response Interception
    # --------------------------------------------------------

    def _make_api_handler(self, captured: list[str]) -> Callable:
        """Return a response handler that captures text from JSON/HTML APIs."""
        def on_response(response):
            try:
                url = response.url
                ctype = response.headers.get("content-type", "")

                if "json" in ctype:
                    body = response.body()
                    text = body.decode("utf-8", errors="replace")
                    try:
                        data = json.loads(text)
                        extracted = _dig_for_text(data)
                        if extracted:
                            captured.append(extracted)
                    except (json.JSONDecodeError, ValueError):
                        pass

                if "/api/" in url and "html" in ctype:
                    body = response.body()
                    decoded = _decode(body)
                    if len(decoded) > 200:
                        captured.append(decoded)
            except Exception:
                pass

        return on_response

    # --------------------------------------------------------
    # Layer ③ — Canvas Hook Collection
    # --------------------------------------------------------

    def _canvas_collect(self, page, poll_ms: int = 3000) -> str:
        """Collect Canvas text — rAF poll for streaming renders, then dedup."""
        try:
            texts = page.evaluate(f"""(async () => {{
                // ---- Poll phase: rAF loop for {poll_ms}ms ----
                // Many Canvas apps (WPS) stream text in over multiple frames.
                // Wait and let requestAnimationFrame fire a few times
                // so the hook can capture incremental fillText calls.
                if (window.__ueCanvasTexts && window.__ueCanvasTexts.length > 0) {{
                    var before = window.__ueCanvasTexts.length;
                    await new Promise(function(resolve) {{
                        var start = Date.now();
                        var maxWait = {poll_ms};
                        function tick() {{
                            if (Date.now() - start >= maxWait) {{
                                resolve();
                            }} else {{
                                requestAnimationFrame(tick);
                            }}
                        }}
                        requestAnimationFrame(tick);
                    }});
                    var after = window.__ueCanvasTexts.length;
                    if (after > before) {{
                        console.log('[UE] Canvas hook: collected ' + (after - before)
                                    + ' new texts during rAF poll (total=' + after + ')');
                    }}
                }}

                // ---- Collect phase ----
                var ts = window.__ueCanvasTexts;
                if (!ts || ts.length === 0) return '';
                var valid = ts.filter(function(t) {{
                    return t.length > 3 || /[一-鿿]/.test(t);
                }});
                // Dedup consecutive repeats (Canvas re-renders same text)
                var deduped = [];
                var last = '';
                for (var i = 0; i < valid.length; i++) {{
                    if (valid[i] !== last) {{
                        deduped.push(valid[i]);
                        last = valid[i];
                    }}
                }}
                // Second pass: remove substrings (longer text contains shorter)
                var final = [];
                for (var j = 0; j < deduped.length; j++) {{
                    var isSub = false;
                    for (var k = 0; k < deduped.length; k++) {{
                        if (j !== k && deduped[k].length > deduped[j].length
                            && deduped[k].indexOf(deduped[j]) !== -1) {{
                            isSub = true; break;
                        }}
                    }}
                    if (!isSub) final.push(deduped[j]);
                }}
                return final.join('\\n');
            }})()""")
            if texts is None:
                return ""
            return texts.strip() if texts else ""
        except Exception as exc:
            logger.warning("Layer ③ error: %s", exc)
            return ""

    # --------------------------------------------------------
    # Layer ④ — CDP Memory Scan
    # --------------------------------------------------------

    def _cdp_scan(self, page) -> str:
        """Scan JS heap via CDP for text objects."""
        try:
            cdp = page.context.new_cdp_session(page)
            result = cdp.send("Runtime.evaluate", {
                "expression": """
                    (function() {
                        var found = [];
                        function walk(obj, depth) {
                            if (depth > 3 || !obj || typeof obj !== 'object') return;
                            try {
                                var keys = Object.getOwnPropertyNames(obj).slice(0, 50);
                                for (var i = 0; i < keys.length; i++) {
                                    try {
                                        var val = obj[keys[i]];
                                        if (typeof val === 'string' && val.length > 100 && val.length < 50000) {
                                            found.push(val);
                                        } else if (typeof val === 'object' && val !== null && !Array.isArray(val)) {
                                            walk(val, depth + 1);
                                        }
                                    } catch(e) {}
                                }
                            } catch(e) {}
                        }
                        walk(window, 0);
                        try {
                            for (var j = 0; j < localStorage.length; j++) {
                                var v = localStorage.getItem(localStorage.key(j));
                                if (v && v.length > 100) found.push(v);
                            }
                        } catch(e) {}
                        var globals = ['__NEXT_DATA__', '__NUXT__', '__INITIAL_STATE__'];
                        for (var k = 0; k < globals.length; k++) {
                            try {
                                var gv = window[globals[k]];
                                if (gv) found.push(JSON.stringify(gv).slice(0, 10000));
                            } catch(e) {}
                        }
                        return found.sort(function(a,b) { return b.length - a.length; }).slice(0, 5);
                    })()
                """,
                "returnByValue": True,
                "timeout": 10000,
            })
            cdp.detach()

            strings = result.get("result", {}).get("value", [])
            if strings and isinstance(strings, list):
                for s in strings:
                    if not isinstance(s, str) or len(s) < 200:
                        continue
                    if s.strip().startswith("{") and ('"version"' in s[:200] or '"disable"' in s[:500]):
                        continue
                    if '"encryptAttrs"' in s[:500] or '"events"' in s[:500]:
                        continue
                    if s.strip().startswith("PG") and len(s) > 500:
                        continue
                    if s.strip().startswith("<") and ("</" in s or "/>" in s):
                        continue
                    return s
        except Exception as exc:
            logger.warning("Layer ④ error: %s", exc)
        return ""

    # --------------------------------------------------------
    # Layer ⑤ — Screenshot + OCR
    # --------------------------------------------------------

    def _ocr_extract(self, page) -> str:
        """Scroll through page, take screenshots, OCR via first capable provider."""
        if not self.vision_providers:
            logger.warning("Layer ⑤ skipped: no vision providers.")
            return ""

        # Scroll function: re-detect canvas rect each call (SPA pages may
        # add Canvas after setup phase), then try 4-level fallback chain.
        def scroll_fn(pg, delta_y: int) -> bool:
            rect = find_canvas_rect(pg) or self._canvas_rect
            return scroll_viewport(pg, delta_y, canvas_rect=rect) != "none"

        screenshot_paths = capture_views(
            page,
            self._temp_root,
            max_views=8,
            overlap=self.screenshot_overlap,
            scroll_fn=scroll_fn,
        )
        if not screenshot_paths:
            return ""

        all_text: list[str] = []
        prompt = (
            "请提取这张截图中的所有文字内容，包括中文和英文。"
            "按照原文顺序输出，保留标题层级和段落结构。"
            "不要添加任何解释，只输出图片中的文字。"
        )

        import base64
        for path in screenshot_paths:
            try:
                b64 = base64.b64encode(Path(path).read_bytes()).decode()
            except Exception as exc:
                logger.warning("OCR: cannot read %s: %s", path, exc)
                continue

            # Try providers in order
            for provider in self.vision_providers:
                if not provider.can_handle():
                    continue
                try:
                    text = provider.extract_text(b64, prompt)
                    if text and len(text.strip()) > 20:
                        all_text.append(text.strip())
                        break  # next screenshot
                except Exception as exc:
                    logger.debug(
                        "Provider '%s' failed for screenshot: %s",
                        provider.name, exc,
                    )

        return _clean_ocr_text("\n\n".join(all_text))

    # --------------------------------------------------------
    # Layer ⑥ — Vision LLM Full-Page Extraction
    # --------------------------------------------------------

    def _vision_llm_fullpage(self, page) -> str:
        """
        Stitch all screenshots vertically, send to Vision LLM for
        structured full-page extraction. More coherent than per-snapshot OCR.
        """
        if not self.vision_providers or not self._temp_root:
            return ""

        # Capture with more views for full-page stitch
        def scroll_fn(pg, delta_y: int) -> bool:
            rect = find_canvas_rect(pg) or self._canvas_rect
            return scroll_viewport(pg, delta_y, canvas_rect=rect) != "none"

        screenshot_paths = capture_views(
            page,
            self._temp_root,
            max_views=10,
            overlap=self.screenshot_overlap,
            scroll_fn=scroll_fn,
        )

        # Remove the full-page backup (last item), we'll make our own
        non_full = [p for p in screenshot_paths if not p.endswith("ocr_full.png")]
        if len(non_full) < 2:
            return ""  # Single page, Layer ⑤ OCR is sufficient

        # Dedup and stitch
        unique = dedup_screenshots(non_full, threshold=12)
        stitched_path = str(self._temp_root / "stitched_fullpage.png")
        result = stitch_vertical(unique, stitched_path)
        if not result:
            return ""

        import base64
        try:
            b64 = base64.b64encode(Path(stitched_path).read_bytes()).decode()
        except Exception as exc:
            logger.warning("Layer ⑥: cannot read stitched image: %s", exc)
            return ""

        prompt = (
            "You are a document extraction system. Extract ALL visible text "
            "from this full-page screenshot, preserving:\n"
            "- Document structure (headings, subheadings, paragraphs)\n"
            "- Lists and tables\n"
            "- All Chinese and English text\n"
            "- Page numbers and footnotes\n\n"
            "Output ONLY the extracted text, no commentary."
        )

        # Try providers in order
        for provider in self.vision_providers:
            if not provider.can_handle():
                continue
            try:
                text = provider.extract_text(b64, prompt, max_tokens=4096)
                if text and len(text.strip()) > 100:
                    logger.info(
                        "Layer ⑥: got %d chars from %s",
                        len(text), provider.name,
                    )
                    return _clean_ocr_text(text)
            except Exception as exc:
                logger.debug(
                    "Layer ⑥ provider '%s' failed: %s",
                    provider.name, exc,
                )

        return ""

    # --------------------------------------------------------
    # Result Selection
    # --------------------------------------------------------

    def _pick_best(self, results: list[ExtractionResult]) -> str:
        """Pick the best extraction result by layer priority and completeness."""
        # Compute completeness for results that don't have it yet
        for r in results:
            if r.completeness == 0.0 and r.text:
                r.completeness = completeness_score(r.text)

        # Diagnostic
        for r in results:
            print(
                f"  [Layer {r.source_layer}] {r.method}: "
                f"{len(r.text)} chars, score={r.completeness:.2f}, "
                f"complete={is_complete(r.text)}, conf={r.confidence}"
            )

        # Priority: DOM > Canvas Hook > Vision LLM > API > CDP > OCR
        priority_order = [1, 3, 6, 2, 4, 5]
        for layer in priority_order:
            for r in results:
                if r.source_layer == layer and is_complete(r.text):
                    print(
                        f"  => Selected layer {r.source_layer} ({r.method}) "
                        f"— {len(r.text)} chars"
                    )
                    return r.text.strip()

        # Fallback chain
        # First: DOM or Canvas Hook text > 100 chars
        dom_or_hook = [
            r for r in results
            if r.text and len(r.text.strip()) > 100
            and r.source_layer in (1, 3, 6)
        ]
        if dom_or_hook:
            best = max(dom_or_hook, key=lambda r: len(r.text))
            print(f"  => Fallback DOM/hook/vision layer {best.source_layer}")
            return best.text.strip()

        # Next: API text that isn't JSON
        text_api = [
            r for r in results
            if r.text and len(r.text.strip()) > 100
            and r.source_layer == 2
            and not r.text.strip().startswith("{")
        ]
        if text_api:
            best = max(text_api, key=lambda r: len(r.text))
            print(f"  => Fallback API text — {len(best.text)} chars")
            return best.text.strip()

        # Last resort: longest non-empty > 50 chars
        valid = [r for r in results if r.text and len(r.text.strip()) > 50]
        if valid:
            best = max(valid, key=lambda r: len(r.text))
            print(f"  => Last resort layer {best.source_layer}")
            return best.text.strip()

        raise ExtractionError(
            f"All 6 layers failed to extract meaningful text. "
            f"(got {len(results)} results: "
            f"{[f'L{r.source_layer}={len(r.text)}c' for r in results]})"
        )

    def _get_best_text(self, results: list[ExtractionResult]) -> str:
        """Internal: get best text from current results, or '' if none."""
        # Simplified version for multi-pass, doesn't raise
        priority_order = [1, 3, 6, 2, 4, 5]
        for layer in priority_order:
            for r in results:
                if r.source_layer == layer and r.text and len(r.text.strip()) > 50:
                    return r.text.strip()
        return ""
