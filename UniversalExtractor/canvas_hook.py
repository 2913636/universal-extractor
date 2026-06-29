"""
Canvas Hook JS + 多向量注入。

提供：
  - CANVAS_HOOK_JS           : 注入页面的 Canvas fillText/strokeText Hook 脚本
  - inject_canvas_hook       : 多向量并行注入函数（CDP + init_script + HTML 拦截）

用法:
    from .canvas_hook import CANVAS_HOOK_JS, inject_canvas_hook

    inject_canvas_hook(page, hook_script_path, temp_dir)
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ============================================================
# Canvas Hook JavaScript
# ============================================================

CANVAS_HOOK_JS = r"""
// UniversalExtractor — Canvas text interceptor (v2)
(function() {
    if (window.__ueCanvasTexts) return;  // already injected
    window.__ueCanvasTexts = [];
    window.__ueCanvasReady = false;
    window.__ueLastRenderTime = 0;

    // ==============================================
    // Text recording
    // ==============================================
    function record(text) {
        if (typeof text === 'string' && text.trim().length > 0) {
            window.__ueCanvasTexts.push(text.trim());
            window.__ueLastRenderTime = Date.now();
            if (window.__ueCanvasTexts.length > 50000) {
                window.__ueCanvasTexts = window.__ueCanvasTexts.slice(-25000);
            }
        }
    }

    // ==============================================
    // rAF wrapper — detect Canvas rendering activity
    // ==============================================
    var _origRAF = window.requestAnimationFrame;
    var _rafCount = 0;
    window.requestAnimationFrame = function(cb) {
        _rafCount++;
        return _origRAF.call(window, function(ts) {
            // Mark that a render frame is happening
            window.__ueIsRendering = true;
            try { cb(ts); } finally {
                window.__ueIsRendering = false;
            }
        });
    };
    // Expose rAF counter for external polling
    window.__ueRAFCount = function() { return _rafCount; };

    // ==============================================
    // Hook CanvasRenderingContext2D.fillText
    // ==============================================
    var origFillText = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y, maxWidth) {
        record(text);
        return origFillText.call(this, text, x, y, maxWidth);
    };

    // Hook CanvasRenderingContext2D.strokeText
    var origStrokeText = CanvasRenderingContext2D.prototype.strokeText;
    CanvasRenderingContext2D.prototype.strokeText = function(text, x, y, maxWidth) {
        record(text);
        return origStrokeText.call(this, text, x, y, maxWidth);
    };

    // Hook fillText on the prototype chain (some polyfills replace it)
    if (CanvasRenderingContext2D.prototype.fillText.toString().indexOf('record') === -1) {
        // Re-hook attempt — some apps replace the prototype after load
        var _orig = CanvasRenderingContext2D.prototype.fillText;
        CanvasRenderingContext2D.prototype.fillText = function(text, x, y, maxWidth) {
            record(text);
            return _orig.call(this, text, x, y, maxWidth);
        };
    }

    // ==============================================
    // Hook HTMLCanvasElement.getContext — detect
    // Canvas context creation (including in Workers
    // via OffscreenCanvas transfer)
    // ==============================================
    if (typeof HTMLCanvasElement !== 'undefined') {
        var _origGetContext = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function() {
            var ctx = _origGetContext.apply(this, arguments);
            if (ctx && arguments[0] === '2d') {
                // Re-hook the context's fillText in case the app
                // replaced the prototype after our initial hook
                if (ctx.fillText && ctx.fillText.toString().indexOf('record') === -1) {
                    var _f = ctx.fillText;
                    ctx.fillText = function(text, x, y, maxWidth) {
                        record(text);
                        return _f.call(this, text, x, y, maxWidth);
                    };
                }
            }
            return ctx;
        };
    }

    // ==============================================
    // Hook OffscreenCanvasRenderingContext2D
    // (used in Web Workers)
    // ==============================================
    if (typeof OffscreenCanvasRenderingContext2D !== 'undefined') {
        var origOffFill = OffscreenCanvasRenderingContext2D.prototype.fillText;
        OffscreenCanvasRenderingContext2D.prototype.fillText = function(text, x, y, maxWidth) {
            record(text);
            return origOffFill.call(this, text, x, y, maxWidth);
        };
        var origOffStroke = OffscreenCanvasRenderingContext2D.prototype.strokeText;
        OffscreenCanvasRenderingContext2D.prototype.strokeText = function(text, x, y, maxWidth) {
            record(text);
            return origOffStroke.call(this, text, x, y, maxWidth);
        };
    }

    // Hook OffscreenCanvas.prototype.getContext (for Worker canvas setup)
    if (typeof OffscreenCanvas !== 'undefined') {
        try {
            var _origOffGetCtx = OffscreenCanvas.prototype.getContext;
            OffscreenCanvas.prototype.getContext = function() {
                var ctx = _origOffGetCtx.apply(this, arguments);
                if (ctx && ctx.fillText && ctx.fillText.toString().indexOf('record') === -1) {
                    var _f = ctx.fillText;
                    ctx.fillText = function(text, x, y, maxWidth) {
                        record(text);
                        return _f.call(this, text, x, y, maxWidth);
                    };
                }
                return ctx;
            };
        } catch(e) {}
    }

    // ==============================================
    // Periodic re-hook timer (catches late-loaded
    // canvas engines that replace prototypes)
    // ==============================================
    setInterval(function() {
        try {
            if (CanvasRenderingContext2D.prototype.fillText.toString().indexOf('record') === -1) {
                var f = CanvasRenderingContext2D.prototype.fillText;
                CanvasRenderingContext2D.prototype.fillText = function(text, x, y, maxWidth) {
                    record(text);
                    return f.call(this, text, x, y, maxWidth);
                };
            }
        } catch(e) {}
    }, 2000);  // Check every 2 seconds

    window.__ueCanvasReady = true;
    console.log('[UniversalExtractor] Canvas hook v2 active');
})();
"""


# ============================================================
# 多向量注入
# ============================================================

def inject_canvas_hook(
    page,
    hook_script_path: str,
) -> dict[str, bool]:
    """
    多向量并行注入 Canvas Hook，返回每个向量的成功/失败状态。

    注入向量（全部并行执行，不互斥）：
      A. CDP Page.addScriptToEvaluateOnNewDocument（浏览器级，最早）
      B. page.add_init_script（Playwright 级，标准方式）
      C. HTML 响应拦截（CDP Network.setRequestInterception，在 HTML 解析前注入）

    Returns:
        ``{"cdp": True, "init_script": False, "html_intercept": True}``
    """
    results: dict[str, bool] = {
        "cdp": False,
        "init_script": False,
        "html_intercept": False,
    }

    # ---- Vector A: CDP addScriptToEvaluateOnNewDocument ----
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Page.addScriptToEvaluateOnNewDocument", {
            "source": CANVAS_HOOK_JS,
        })
        cdp.detach()
        results["cdp"] = True
        logger.debug("Canvas Hook: CDP injection OK")
    except Exception as exc:
        logger.debug("Canvas Hook: CDP injection failed — %s", exc)

    # ---- Vector B: page.add_init_script ----
    try:
        page.add_init_script(path=hook_script_path)
        results["init_script"] = True
        logger.debug("Canvas Hook: add_init_script OK")
    except Exception as exc:
        logger.debug("Canvas Hook: add_init_script failed — %s", exc)

    # ---- Vector C: HTML 响应拦截（CDP Network 层） ----
    try:
        _inject_via_cdp_interception(page)
        results["html_intercept"] = True
        logger.debug("Canvas Hook: CDP Network interception OK")
    except Exception as exc:
        logger.debug("Canvas Hook: CDP Network interception failed — %s", exc)

    # Summary
    injected_count = sum(1 for v in results.values() if v)
    if injected_count == 0:
        logger.warning("Canvas Hook: ALL injection vectors failed!")
    else:
        logger.info("Canvas Hook: %d/%d vectors succeeded %s",
                     injected_count, len(results), results)

    return results


def _inject_via_cdp_interception(page) -> None:
    """
    通过 CDP Fetch domain 在 HTML 解析前注入 Hook。

    使用 Fetch.enable + Fetch.requestPaused（替代已弃用的
    Network.setRequestInterception）。拦截所有 Document 请求，
    在响应 body 最前面插入 <script> 标签。
    """
    import base64

    cdp = page.context.new_cdp_session(page)

    # Enable Fetch domain
    cdp.send("Fetch.enable", {
        "patterns": [{
            "urlPattern": "*",
            "resourceType": "Document",
            "requestStage": "Response",
        }],
    })

    def _on_request_paused(event: dict):
        request_id = event.get("requestId", "")
        response_status_code = event.get("responseStatusCode", 200)
        response_headers = event.get("responseHeaders", [])

        # Get response body via Fetch.getResponseBody
        body_text = ""
        try:
            body_result = cdp.send("Fetch.getResponseBody", {
                "requestId": request_id,
            })
            body_text = body_result.get("body", "")
            # body may be base64-encoded
            if body_result.get("base64Encoded", False):
                body_text = base64.b64decode(body_text).decode("utf-8", errors="replace")
        except Exception:
            # Can't get body — fulfil unmodified
            try:
                cdp.send("Fetch.fulfillRequest", {"requestId": request_id})
            except Exception:
                pass
            return

        # Inject Hook script before any other content
        if "<head>" in body_text:
            modified = body_text.replace(
                "<head>",
                f"<head><script>{CANVAS_HOOK_JS}</script>",
                1,
            )
        elif "<html" in body_text:
            modified = body_text.replace(
                "<html",
                f"<html><script>{CANVAS_HOOK_JS}</script>",
                1,
            )
        elif "<body" in body_text:
            modified = body_text.replace(
                "<body",
                f"<body><script>{CANVAS_HOOK_JS}</script>",
                1,
            )
        else:
            modified = f"<script>{CANVAS_HOOK_JS}</script>{body_text}"

        # Fulfil with modified body
        try:
            # Convert response_headers from CDP format to Fetch.fulfillRequest format
            fulfill_headers = []
            for h in response_headers:
                fulfill_headers.append({
                    "name": h.get("name", ""),
                    "value": h.get("value", ""),
                })

            cdp.send("Fetch.fulfillRequest", {
                "requestId": request_id,
                "responseCode": response_status_code,
                "responseHeaders": fulfill_headers,
                "body": base64.b64encode(modified.encode("utf-8")).decode(),
            })
        except Exception:
            # Fallback: fulfil unmodified
            try:
                cdp.send("Fetch.fulfillRequest", {"requestId": request_id})
            except Exception:
                pass

    cdp.on("Fetch.requestPaused", _on_request_paused)
    cdp.detach()
