"""
Canvas 滚动降级链 — 4 层策略，按优先级尝试。

策略层级：
  1. JS window.scrollTo（兼容最广）
  2. JS Canvas 父容器 scrollTop（Canvas 页面常见）
  3. CDP Input.dispatchMouseEvent mouseWheel（直接往 Canvas 坐标发送）
  4. CDP 拖拽模拟 mousedown → mousemove → mouseup

用法:
    from .scrolling import scroll_viewport, find_canvas_rect

    rect = find_canvas_rect(page)
    ok = scroll_viewport(page, delta_y=500, canvas_rect=rect)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def find_canvas_rect(page) -> Optional[dict[str, float]]:
    """
    找到页面中最大 Canvas 元素的边界矩形。

    Returns:
        ``{"x": float, "y": float, "width": float, "height": float}`` 或 None
    """
    try:
        rect = page.evaluate("""() => {
            const canvases = document.querySelectorAll('canvas');
            let best = null;
            let bestArea = 0;
            for (const c of canvases) {
                const r = c.getBoundingClientRect();
                const area = r.width * r.height;
                if (area > bestArea) { best = r; bestArea = area; }
            }
            if (!best) return null;
            return {x: best.x, y: best.y, width: best.width, height: best.height};
        }""")
        if rect:
            return {
                "x": float(rect["x"]),
                "y": float(rect["y"]),
                "width": float(rect["width"]),
                "height": float(rect["height"]),
            }
    except Exception as exc:
        logger.debug("find_canvas_rect: %s", exc)
    return None


# ============================================================
# 策略 1：JS window.scrollTo
# ============================================================

def _scroll_js_window(page, delta_y: int) -> bool:
    """标准浏览器滚动。"""
    try:
        before = page.evaluate("window.scrollY")
        page.evaluate(f"window.scrollTo(0, {delta_y})")
        after = page.evaluate("window.scrollY")
        return abs(after - before) >= 1
    except Exception:
        return False


# ============================================================
# 策略 2：JS Canvas 父容器 scrollTop
# ============================================================

def _scroll_canvas_container_js(page, delta_y: int) -> bool:
    """
    找到 Canvas 的滚动父容器并设置其 scrollTop。

    很多 Canvas 应用（WPS/飞书/腾讯文档）将 Canvas 放在
    一个 overflow:auto/scroll 的 div 里，浏览器滚动无效，
    但操作这个容器的 scrollTop 可以。
    """
    try:
        result = page.evaluate(f"""() => {{
            const canvases = document.querySelectorAll('canvas');
            for (const c of canvases) {{
                // 向上找 5 层，看是否有滚动容器
                let el = c;
                for (let depth = 0; depth < 5 && el; depth++) {{
                    el = el.parentElement;
                    if (!el) break;
                    const cs = getComputedStyle(el);
                    const hasScroll = (
                        cs.overflow === 'auto' || cs.overflow === 'scroll' ||
                        cs.overflowY === 'auto' || cs.overflowY === 'scroll'
                    );
                    if (hasScroll && el.scrollHeight > el.clientHeight) {{
                        const before = el.scrollTop;
                        el.scrollTop += {delta_y};
                        return el.scrollTop !== before;
                    }}
                }}
                // Canvas 自身也可能是滚动容器（WebGL 应用）
                if (c.scrollHeight && c.scrollHeight > c.clientHeight) {{
                    const before = c.scrollTop;
                    c.scrollTop += {delta_y};
                    return c.scrollTop !== before;
                }}
            }}
            return false;
        }}""")
        return bool(result)
    except Exception:
        return False


# ============================================================
# 策略 3：CDP Input.dispatchMouseEvent mouseWheel
# ============================================================

def _scroll_cdp_wheel(
    page,
    delta_y: int,
    canvas_rect: Optional[dict[str, float]] = None,
) -> bool:
    """
    通过 CDP 在 Canvas 中心坐标派发 mouseWheel 事件。

    Playwright 的 page.mouse.wheel() 只能往页面 viewport 发，
    CDP 允许指定精确坐标，对 Canvas 内部滚动更有效。
    """
    rect = canvas_rect or find_canvas_rect(page)
    if not rect:
        return False

    cx = rect["x"] + rect["width"] / 2
    cy = rect["y"] + rect["height"] / 2

    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": cx,
            "y": cy,
            "deltaX": 0,
            "deltaY": delta_y,
            "pointerType": "mouse",
        })
        cdp.detach()
        return True
    except Exception as exc:
        logger.debug("CDP wheel scroll failed: %s", exc)
        return False


# ============================================================
# 策略 4：CDP 拖拽模拟
# ============================================================

def _scroll_cdp_drag(
    page,
    delta_y: int,
    canvas_rect: Optional[dict[str, float]] = None,
) -> bool:
    """
    通过 CDP 模拟鼠标拖拽：按下 → 移动 → 释放。

    Canvas 内部滚动条有时只响应拖拽，不响应滚轮。
    """
    rect = canvas_rect or find_canvas_rect(page)
    if not rect:
        return False

    cx = rect["x"] + rect["width"] / 2
    cy = rect["y"] + rect["height"] / 2

    try:
        cdp = page.context.new_cdp_session(page)

        # mousedown
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": cx, "y": cy,
            "button": "left",
            "clickCount": 1,
        })

        # mousemove (with multiple intermediate positions for smooth drag)
        steps = 10
        for s in range(1, steps + 1):
            cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": cx,
                "y": cy + (delta_y * s // steps),
            })

        # mouseup
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": cx,
            "y": cy + delta_y,
            "button": "left",
        })

        cdp.detach()
        return True
    except Exception as exc:
        logger.debug("CDP drag scroll failed: %s", exc)
        return False


# ============================================================
# 降级链主入口
# ============================================================

def scroll_viewport(
    page,
    delta_y: int,
    *,
    canvas_rect: Optional[dict[str, float]] = None,
    strategies: Optional[list[str]] = None,
) -> str:
    """
    按优先级尝试滚动，返回第一个成功的策略名。

    Parameters:
        page: Playwright Page 对象
        delta_y: 滚动量（正=向下，负=向上）
        canvas_rect: 预计算的 Canvas 矩形（传 None 则自动探测）
        strategies: 策略名列表，默认全部尝试：
                    ``["js_window", "canvas_container", "cdp_wheel", "cdp_drag"]``

    Returns:
        成功的策略名，全部失败返回 ``"none"``
    """
    if strategies is None:
        strategies = ["js_window", "canvas_container", "cdp_wheel", "cdp_drag"]

    rect = canvas_rect or find_canvas_rect(page)

    for name in strategies:
        try:
            if name == "js_window":
                if _scroll_js_window(page, delta_y):
                    return name
            elif name == "canvas_container":
                if _scroll_canvas_container_js(page, delta_y):
                    logger.debug("Scrolled via canvas_container (delta=%d)", delta_y)
                    return name
            elif name == "cdp_wheel":
                if _scroll_cdp_wheel(page, delta_y, rect):
                    logger.debug("Scrolled via CDP mouseWheel (delta=%d)", delta_y)
                    return name
            elif name == "cdp_drag":
                if _scroll_cdp_drag(page, delta_y, rect):
                    logger.debug("Scrolled via CDP drag (delta=%d)", delta_y)
                    return name
        except Exception as exc:
            logger.debug("Strategy '%s' error: %s", name, exc)
            continue

    logger.debug("All scroll strategies failed for delta=%d", delta_y)
    return "none"
