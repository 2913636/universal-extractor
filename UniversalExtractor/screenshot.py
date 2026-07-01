"""
截帧与拼接模块 — 感知哈希去重 + 重叠滚动 + 垂直拼接。

用法:
    from .screenshot import capture_views, dedup_screenshots, stitch_vertical

    paths = capture_views(page, temp_dir, max_views=8)
    unique = dedup_screenshots(paths, threshold=10)
    combined = stitch_vertical(unique, output_dir / "fullpage.png")
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 感知哈希尺寸（越大越敏感）
PHASH_SIZE = 8


# ============================================================
# 感知哈希（Average Hash）
# ============================================================

def _phash(image_path: str, hash_size: int = PHASH_SIZE) -> int:
    """
    计算感知哈希（Average Hash），用于图片相似度比较。

    算法：缩放到 hash_size+1 × hash_size 灰度 → 逐行比较相邻像素 → 64-bit 整数。
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not available; pHash dedup disabled")
        return 0

    img = Image.open(image_path).convert("L")
    # resize to (hash_size+1, hash_size) for horizontal gradient comparison
    img = img.resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())

    # Compute difference hash: each row compares adjacent pixels
    bits = []
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            bits.append("1" if left < right else "0")

    return int("".join(bits), 2)


def _hamming(a: int, b: int) -> int:
    """汉明距离（两个哈希之间不同的 bit 数）。"""
    return (a ^ b).bit_count()


def dedup_screenshots(paths: list[str], threshold: int = 10) -> list[str]:
    """
    移除连续近重复截图。

    比较相邻帧的感知哈希汉明距离，
    <= threshold 视为重复 → 丢弃后一帧。

    Parameters:
        paths: 截图路径列表（按时间排序）
        threshold: 汉明距离阈值，<= 此值视为重复（默认 10，宽松去重）

    Returns:
        去重后的路径列表
    """
    if len(paths) <= 1:
        return paths

    hashes = [_phash(p) for p in paths]
    kept: list[str] = [paths[0]]

    for i in range(1, len(paths)):
        dist = _hamming(hashes[i], hashes[i - 1])
        if dist > threshold:
            kept.append(paths[i])
        else:
            logger.debug("Dedup: dropping frame %d (hamming=%d)", i, dist)

    if len(kept) < len(paths):
        logger.info("Dedup: kept %d/%d frames", len(kept), len(paths))
    return kept


# ============================================================
# 垂直拼接
# ============================================================

def stitch_vertical(paths: list[str], output_path: str) -> str | None:
    """
    垂直拼接多张截图为一张长图。

    Parameters:
        paths: 截图路径列表
        output_path: 输出文件路径

    Returns:
        拼接后的图片路径，失败返回 None
    """
    if not paths:
        return None

    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not available; cannot stitch screenshots")
        return None

    try:
        images = [Image.open(p) for p in paths]
        max_width = max(im.width for im in images)
        total_height = sum(im.height for im in images)

        canvas = Image.new("RGB", (max_width, total_height))
        y_offset = 0
        for im in images:
            canvas.paste(im, (0, y_offset))
            y_offset += im.height

        canvas.save(output_path, "PNG")
        logger.info("Stitched %d screenshots → %s (%dx%d)",
                     len(paths), output_path, max_width, total_height)
        return output_path
    except Exception as exc:
        logger.warning("Stitch failed: %s", exc)
        return None


# ============================================================
# 截帧主逻辑
# ============================================================

def capture_views(
    page,
    temp_dir: str | Path,
    *,
    max_views: int = 10,
    overlap: float = 0.2,
    scroll_fn=None,
) -> list[str]:
    """
    逐屏滚动截图，带重叠和去重。

    Parameters:
        page: Playwright Page 对象
        temp_dir: 临时目录（截图存这里）
        max_views: 最多截多少屏
        overlap: 相邻屏重叠比例（0.0~0.5，默认 0.2 = 20%）
        scroll_fn: 自定义滚动函数 ``fn(page, delta_y) -> bool``。
                   传 None 则用默认 ``window.scrollTo``。

    Returns:
        截图文件路径列表
    """
    temp_dir = Path(temp_dir)
    paths: list[str] = []

    try:
        dims = page.evaluate("""() => ({
            h: Math.max(document.body.scrollHeight, document.body.clientHeight, 5000),
            vh: window.innerHeight || 900
        })""")
        total_h = min(dims.get("h", 5000), 20000)
        vh = dims.get("vh", 900)
        step = int(vh * (1.0 - overlap))  # 重叠 20% 时步长 = 0.8 × vh
        step = max(step, 200)  # 最小步长 200px，防止无限循环

        last_y = -1  # 上一次 scrollY，用于检测是否到底

        for i in range(max_views):
            # 滚动
            target_y = i * step
            if scroll_fn:
                ok = scroll_fn(page, target_y)
            else:
                page.evaluate(f"window.scrollTo(0, {target_y})")
                ok = True

            page.wait_for_timeout(600)  # 等渲染

            # 检测是否卡住（滚不动了 = 到底）
            current_y = page.evaluate("window.scrollY")
            if abs(current_y - last_y) < 10 and i > 0:
                logger.debug("Scroll stuck at y=%d, stopping", current_y)
                break
            last_y = current_y

            # 截图
            filepath = str(temp_dir / f"ocr_{i:03d}.png")
            try:
                page.screenshot(path=filepath, full_page=False)
                paths.append(filepath)
            except Exception as exc:
                logger.warning("Screenshot %d failed: %s", i, exc)

    except Exception as exc:
        logger.warning("View capture error: %s", exc)

    # 去重
    if len(paths) > 1:
        paths = dedup_screenshots(paths)

    # 追加一张全页截图作为备用
    try:
        fullpath = str(temp_dir / "ocr_full.png")
        page.screenshot(path=fullpath, full_page=True)
        paths.append(fullpath)
    except Exception as exc:
        logger.debug("Full-page screenshot fallback failed: %s", exc)

    logger.info("Captured %d views (max_views=%d, overlap=%.0f%%)",
                len(paths), max_views, overlap * 100)
    return paths
