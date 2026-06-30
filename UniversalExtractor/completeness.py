"""
完整性评分模块 — 将二元判断升级为连续打分（0.0~1.0）。

用法:
    from .completeness import completeness_score, is_complete

    score = completeness_score(text, metadata={"expected_length": 5000})
    if is_complete(text):
        ...
"""

from __future__ import annotations

import re
from typing import Dict, Optional

# --- 阈值常量 ---
from .classifier import BOILERPLATE_KEYWORDS  # 统一噪声关键词

MIN_TEXT_LENGTH = 100
MIN_PARAGRAPHS = 3
MIN_SENTENCE_MARKERS = 10

# 常见 loading 骨架文案（长度短，多是 UI 框架默认文本）
SKELETON_PATTERNS = [
    re.compile(r"loading", re.IGNORECASE),
    re.compile(r"加载中"),
    re.compile(r"请稍候"),
    re.compile(r"正在加载"),
]


def completeness_score(
    text: str,
    metadata: Optional[Dict] = None,
) -> float:
    """
    估算提取文本的完整性，返回 0.0（完全残缺）~ 1.0（基本完整）。

    评分因子（权重之和 = 1.0）：
      1. 长度因子（0.30）：相对期望长度的比例
      2. 段落结构（0.20）：自然段数量和密度
      3. 句子多样性（0.15）：标点密度
      4. 长行比例（0.10）：>80 字符的文本行占比
      5. 样板文本惩罚（-0.25 ~ 0）：登录页/cookie 墙/loading 骨架
      6. 字符多样性（0.10）：唯一字符占比
      7. 表格/列表奖励（0.05）：含表格边框或列表标记

    Parameters:
        text: 提取的文本
        metadata: 可选上下文，如 ``{"expected_length": 8000}``
    """
    if not text or len(text.strip()) < MIN_TEXT_LENGTH:
        return 0.0

    text = text.strip()
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return 0.0

    meta = metadata or {}
    score = 0.0

    # ---- 1. 长度因子 (0.30) ----
    expected = meta.get("expected_length", 5000)
    length_ratio = min(len(text) / max(expected, 1), 1.0)
    score += length_ratio * 0.30

    # ---- 2. 段落结构 (0.20) ----
    para_count = len(re.findall(r"\n\s*\n", text))
    para_score = min(para_count / 15, 1.0)
    score += para_score * 0.20

    # ---- 3. 句子多样性 (0.15) ----
    sentence_ends = len(re.findall(r"[。！？.!?\n]", text))
    density = sentence_ends / max(len(lines), 1)
    score += min(density, 1.0) * 0.15

    # ---- 4. 长行比例 (0.10) ----
    long_lines = sum(1 for ln in lines if len(ln) > 80)
    long_ratio = long_lines / max(len(lines), 1)
    score += min(long_ratio * 2, 1.0) * 0.10

    # ---- 5. 样板文本惩罚 (-0.25 ~ 0) ----
    top_text = text[:800]
    boilerplate_hits = sum(
        1 for kw in BOILERPLATE_KEYWORDS if kw.lower() in top_text.lower()
    )
    # loading skeleton 检测
    skeleton_hits = sum(
        1 for pat in SKELETON_PATTERNS if pat.search(top_text)
    )
    penalty = (boilerplate_hits * 0.04) + (skeleton_hits * 0.06)
    score -= min(penalty, 0.25)

    # ---- 6. 字符多样性 (0.10) ----
    unique_ratio = len(set(text)) / max(len(text), 1)
    score += min(unique_ratio * 5, 1.0) * 0.10

    # ---- 7. 表格/列表奖励 (0.05) ----
    if re.search(r"[│┌├└─|]", text):
        score += 0.05
    if re.search(r"^\s*[\-\*]\s", text, re.MULTILINE):
        score += 0.03

    return round(max(0.0, min(score, 1.0)), 4)


def is_complete(
    text: str,
    min_score: float = 0.5,
    metadata: Optional[Dict] = None,
) -> bool:
    """
    便捷包装器：完整性分数 >= min_score 判定为完整。

    Parameters:
        text: 提取的文本
        min_score: 判定阈值（默认 0.5）
        metadata: 传递给 ``completeness_score`` 的上下文
    """
    return completeness_score(text, metadata) >= min_score


def text_density_curve(text: str, segments: int = 5) -> list[float]:
    """
    按字数均匀分段，返回每段的文本密度曲线。

    可用于判断文本是否"前密后疏"（只提取了开头）。

    Returns:
        每段归一化密度列表，值域 [0.0, 1.0]
    """
    if not text or segments <= 0:
        return []

    chunk_size = max(len(text) // segments, 1)
    densities: list[float] = []

    for i in range(segments):
        chunk = text[i * chunk_size : (i + 1) * chunk_size]
        # 密度 = 有效字符占比（排除空白）
        effective = len(re.sub(r"\s", "", chunk))
        density = effective / max(chunk_size, 1)
        densities.append(round(min(density, 1.0), 4))

    return densities
