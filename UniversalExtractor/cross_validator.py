"""
多源交叉校验 — 同本书从多个站抓取 → 对齐合并 → 最优输出。

用法:
    from .cross_validator import cross_validate

    result = cross_validate("落不下 尤萨阿里塔")
    # 自动搜索 → 多站抓取 → 章节对齐 → 择优合并
"""

from __future__ import annotations

import re
import logging
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict
from urllib.parse import urljoin

from .search import search_urls
from .classifier import classify_url
from .weblens import (
    WebLens,
    _CHAPTER_SELECTORS,
    _PAGE_NAV_JS,
    _FONT_ENCRYPT_RANGES,
    _cn_num_to_int,
    _has_font_encryption,
)

logger = logging.getLogger(__name__)


@dataclass
class CrawledChapter:
    num: int           # 章节号（该源内的编号）
    title: str
    text: str
    source_url: str    # 来源 URL
    source_name: str   # 来源站名
    char_count: int = 0

    def __post_init__(self):
        self.char_count = len(self.text) if self.text else 0


@dataclass
class MergedChapter:
    num: int
    title: str = ""
    text: str = ""
    sources: list[str] = field(default_factory=list)  # 哪些源贡献了内容
    confidence: float = 0.0  # 0-1 对齐置信度


# ============================================================
# 1. 多源发现与抓取
# ============================================================

def discover_sources(query: str, max_sources: int = 3) -> list[dict]:
    """
    搜索多个小说源站，返回可抓取的站点列表。

    用 WebLens 搜索 + Jina Reader 快扫，挑出有内容的站。
    """
    urls = search_urls(query, max_results=15)

    sources = []
    wl = WebLens(headless=True, max_candidates=5)

    for url in urls:
        # 只看内容页
        verdict = classify_url(url)
        if not verdict["is_content"] and verdict["type"] != "novel_index":
            continue

        # Jina 快扫（一级）
        preview = wl._via_jina(url)
        # 字体加密检测（用共享函数）
        if _has_font_encryption(preview):
            logger.info("Skipping %s: font encryption detected", url[:60])
            continue

        # Jina 拿不到时降级到浏览器（二级）
        if not preview or len(preview) < 500:
            logger.debug("Jina returned %d chars for %s, falling back to browser",
                         len(preview) if preview else 0, url[:60])
            preview = wl._via_browser(url)
            if not preview or len(preview) < 500:
                continue

        # 检查是否有章节标记
        chapter_count = len(re.findall(r'第[一二三四五六七八九十百千\d]+章', preview))
        if chapter_count < 5:
            continue

        sources.append({
            "url": url,
            "chapter_count": chapter_count,
            "preview_chars": len(preview),
        })

        if len(sources) >= max_sources:
            break

    logger.info("Discovered %d clean sources for '%s'", len(sources), query[:30])
    return sources


# ============================================================
# 2. 章节对齐
# ============================================================

def align_chapters(
    source_chapters: list[list[CrawledChapter]],
    min_similarity: float = 0.60,
) -> list[list[Optional[CrawledChapter]]]:
    """
    对齐多个源的章节。

    用文本相似度（前 200 字符）匹配跨源的同名章节。
    返回对齐矩阵：行=章节，列=源。

    Example:
        source_A: [Ch1, Ch2, Ch3]
        source_B: [Ch1, Ch2, Ch3]  (可能是不同编号)
        → [[A1,B1], [A2,B2], [A3,B3]]
    """
    if not source_chapters:
        return []

    num_sources = len(source_chapters)
    # 找到最大章节数
    max_chapters = max(len(chs) for chs in source_chapters)

    # 为每个源建立"指纹列表"：每章前 200 字符
    fingerprints = []
    for src_idx, chapters in enumerate(source_chapters):
        fps = []
        for ch in chapters:
            # 取前 200 字作为指纹（跳过标题和空行）
            cleaned = re.sub(r'\s+', '', ch.text[:300])
            fps.append(cleaned[:200])
        fingerprints.append(fps)

    # 对齐：对每个源的每章，在其他源中找最佳匹配
    # 用贪心+相似度阈值

    # 先取最长源作为基准
    base_idx = max(range(num_sources), key=lambda i: len(source_chapters[i]))
    base_chapters = source_chapters[base_idx]

    aligned = []  # list of lists, each inner list = [ch_or_None, ...] per source

    for base_ch in base_chapters:
        row = [None] * num_sources
        row[base_idx] = base_ch

        base_fp = re.sub(r'\s+', '', base_ch.text[:300])[:200]

        # 在其他源中找最佳匹配
        for src_idx in range(num_sources):
            if src_idx == base_idx:
                continue

            best_sim = 0.0
            best_ch = None
            for ch in source_chapters[src_idx]:
                ch_fp = re.sub(r'\s+', '', ch.text[:300])[:200]
                sim = SequenceMatcher(None, base_fp, ch_fp).ratio()
                if sim > best_sim and sim >= min_similarity:
                    best_sim = sim
                    best_ch = ch

            if best_ch:
                row[src_idx] = best_ch

        aligned.append(row)

    # 补充：有些章节只在非基准源中存在
    for src_idx in range(num_sources):
        if src_idx == base_idx:
            continue
        for ch in source_chapters[src_idx]:
            # 检查是否已被对齐到某行
            aligned_chapters = {row[src_idx] for row in aligned if row[src_idx]}
            if ch not in aligned_chapters:
                row = [None] * num_sources
                row[src_idx] = ch
                aligned.append(row)

    logger.info("Aligned %d chapter rows across %d sources", len(aligned), num_sources)
    return aligned


# ============================================================
# 3. 择优合并
# ============================================================

def merge_chapters(
    aligned: list[list[Optional[CrawledChapter]]],
) -> list[MergedChapter]:
    """
    从对齐矩阵中择优合并：每行选最长版本作为正文。

    策略：
      1. 如果只有一个源有此章 → 直接使用
      2. 如果多个源有 → 选字数最多的
      3. 如果字数差距 >30% → 合并两个版本（拼接补充内容）
    """
    merged = []

    for ch_num, row in enumerate(aligned, 1):
        valid = [c for c in row if c is not None]
        if not valid:
            continue

        sources_used = []
        if len(valid) == 1:
            best = valid[0]
            merged_text = best.text
            sources_used = [best.source_name]
            confidence = 1.0
        else:
            # 选最长的
            best = max(valid, key=lambda c: c.char_count)
            merged_text = best.text
            sources_used = [best.source_name]
            confidence = 0.9

            # 检查其他版本是否有补充内容
            for other in valid:
                if other is best:
                    continue
                # 如果另一个版本字数明显更多 → 可能包含额外内容
                if other.char_count > best.char_count * 1.3:
                    # 拼接到末尾
                    merged_text += "\n\n[补充内容 from " + other.source_name + "]\n" + other.text
                    sources_used.append(other.source_name)
                    confidence = 0.7
                    logger.debug(
                        "Ch%d: merged longer version from %s (%d vs %d chars)",
                        ch_num, other.source_name, other.char_count, best.char_count,
                    )

        # 清理
        merged_text = re.sub(r'\n{4,}', '\n\n\n', merged_text)

        merged.append(MergedChapter(
            num=ch_num,
            title=best.title if best.title else f"第{ch_num}章",
            text=merged_text.strip(),
            sources=sources_used,
            confidence=confidence,
        ))

    logger.info("Merged %d chapters from %d sources",
                 len(merged), len(set(s for r in aligned if r for c in r if c for s in [c.source_name])))
    return merged


# ============================================================
# 4. 全文清洗
# ============================================================

def auto_clean(text: str) -> str:
    """
    自动检测并删除跨章节重复的块（如站内推荐、导航）。

    算法：把文本按章节切分，找每个章节末尾重复出现的块，删除。
    """
    chapters = re.split(r'(={10,}\n第\d+章\n={10,})', text)

    if len(chapters) < 6:
        return text  # 太少章，不检测

    # 收集每章尾部 1000 字符
    tails = []
    for i, part in enumerate(chapters):
        if part.startswith("====="):
            continue
        tail = part.strip()[-800:] if len(part.strip()) > 800 else part.strip()
        tails.append(tail)

    # 找在 >50% 章节中都出现的重复尾部
    from collections import Counter
    tail_counts = Counter(tails)
    for block, count in tail_counts.most_common(3):
        if count >= max(3, len(tails) * 0.5):
            text = text.replace(block, '')
            logger.info("Auto-clean: removed repeating block in %d/%d chapters (%d chars)",
                         count, len(tails), len(block))

    text = re.sub(r'\n{4,}', '\n\n\n', text)
    return text


# ============================================================
# 5. 主入口
# ============================================================

def cross_validate(
    query: str,
    *,
    max_sources: int = 3,
    headless: bool = True,
) -> dict:
    """
    多源交叉校验主流程。

    Args:
        query: 搜索关键词（如 "落不下 尤萨阿里塔"）
        max_sources: 最多抓取几个源
        headless: 浏览器是否无头

    Returns:
        {
            "chapters": [MergedChapter, ...],
            "total_chars": int,
            "sources_used": [str, ...],
            "confidence": float,  # 整体置信度
        }
    """
    # Step 1: 发现源
    sources = discover_sources(query, max_sources)
    if not sources:
        logger.warning("No clean sources found for '%s'", query)
        return {"chapters": [], "total_chars": 0, "sources_used": [], "confidence": 0.0}

    print(f"\n[Cross-Validate] Found {len(sources)} clean sources")
    for s in sources:
        print(f"  {s['url'][:80]}...  ({s['chapter_count']} chapters estimated)")

    # Step 2: 从每个源抓取章节
    wl = WebLens(headless=headless)
    all_source_chapters = []

    for src in sources:
        print(f"\n  Crawling: {src['url'][:60]}...")
        chapters = _crawl_one_source(wl, src["url"])
        if chapters:
            all_source_chapters.append(chapters)
            print(f"    Got {len(chapters)} chapters, {sum(c.char_count for c in chapters)} chars")

    if len(all_source_chapters) < 2:
        # 只有一个源，不需要对齐
        if all_source_chapters:
            chs = all_source_chapters[0]
            merged = [MergedChapter(num=c.num, title=c.title, text=c.text,
                                     sources=[c.source_name], confidence=1.0)
                      for c in chs]
            return {
                "chapters": merged,
                "total_chars": sum(c.char_count for c in chs),
                "sources_used": [chs[0].source_name] if chs else [],
                "confidence": 0.5,
            }
        return {"chapters": [], "total_chars": 0, "sources_used": [], "confidence": 0.0}

    # Step 3: 对齐
    print(f"\n[Cross-Validate] Aligning chapters...")
    aligned = align_chapters(all_source_chapters)
    print(f"  {len(aligned)} aligned rows")

    # Step 4: 合并
    merged = merge_chapters(aligned)

    total = sum(len(m.text) for m in merged)

    result = {
        "chapters": merged,
        "total_chars": total,
        "sources_used": list(set(
            name for m in merged for name in m.sources
        )),
        "confidence": sum(m.confidence for m in merged) / len(merged) if merged else 0.0,
    }

    print(f"\n[Cross-Validate] Done: {len(merged)} chapters, {total} chars, "
          f"from {result['sources_used']}, confidence={result['confidence']:.2f}")

    return result


def _crawl_one_source(wl: WebLens, toc_url: str) -> list[CrawledChapter]:
    """
    从单个源抓取所有章节。
    使用 WebLens 的抓取能力 + 章内分页跟踪。
    """
    from scrapling import StealthyFetcher

    # 从 URL 提取域名作为源名
    domain = re.search(r'https?://(?:www\.)?([^/]+)', toc_url)
    source_name = domain.group(1) if domain else toc_url[:40]

    # 获取章节链接
    chapter_urls = {}

    def get_links(page):
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(3000)
        links = page.evaluate("""()=>{
            return Array.from(document.querySelectorAll('a[href]')).map(function(a){
                return [(a.innerText||a.textContent||'').trim(), a.getAttribute('href')||''];
            });
        }""")
        for text, href in links:
            # 找章节链接：支持阿拉伯数字（"第12章"）和中文数字（"第十二章"）
            m = re.search(r'第\s*([一二三四五六七八九十百千\d]+)\s*章', text)
            if m and href and not href.startswith('javascript:'):
                try:
                    ch_num = _cn_num_to_int(m.group(1))
                except (ValueError, TypeError):
                    continue
                if ch_num not in chapter_urls:
                    chapter_urls[ch_num] = urljoin(toc_url, href)

    try:
        StealthyFetcher.fetch(toc_url, headless=True, timeout=30000,
                               page_action=get_links, network_idle=True)
    except Exception as e:
        logger.warning("Failed to get links from %s: %s", toc_url, e)
        return []

    if len(chapter_urls) < 3:
        logger.warning("Too few chapters found: %d", len(chapter_urls))
        return []

    # 抓取每章
    chapters = []
    sorted_urls = sorted(chapter_urls.items())

    for ch_num, ch_url in sorted_urls[:100]:
        pages_text = []

        data = {"text": "", "next_page": "", "next_chapter": ""}
        current_url = ch_url
        visited = set()

        for _ in range(10):
            if current_url in visited:
                break
            visited.add(current_url)

            def extract(page):
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                    page.wait_for_timeout(1500)
                    # 使用统一的共享选择器
                    sels_js = ",".join(_CHAPTER_SELECTORS)
                    data["text"] = page.evaluate(f"""() => {{
                        var sels = {sels_js!r}.split(',');
                        for (var j = 0; j < sels.length; j++) {{
                            var el = document.querySelector(sels[j]);
                            if (el && el.innerText && el.innerText.trim().length > 50)
                                return el.innerText.trim();
                        }}
                        return (document.body && document.body.innerText)
                            ? document.body.innerText.trim() : '';
                    }}""") or ""
                    # 使用统一的共享导航检测 JS
                    nav = page.evaluate(_PAGE_NAV_JS)
                    for k, v in nav.items():
                        data[k] = v or ""
                except:
                    pass

            try:
                StealthyFetcher.fetch(current_url, headless=True, timeout=15000,
                                       page_action=extract, network_idle=False)
            except:
                pass

            text = (data["text"] or "").strip()
            if text and len(text) > 30:
                pages_text.append(text)

            np_val = data["next_page"]
            nc_val = data["next_chapter"]
            if np_val and not nc_val:
                current_url = urljoin(current_url, np_val)
                continue
            else:
                break

        if pages_text:
            full_text = "\n\n".join(pages_text)
            chapters.append(CrawledChapter(
                num=ch_num,
                title=f"第{ch_num}章",
                text=full_text,
                source_url=ch_url,
                source_name=source_name,
            ))

    return chapters
