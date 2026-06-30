"""
URL 分类 + 内容类型识别 + 关键词匹配。

用法:
    from .classifier import classify_url, score_content, match_keywords

    # URL 是否值得抓
    verdict = classify_url("https://example.com/article/123")
    # → {"is_content": True, "type": "article", "confidence": 0.8}

    # 内容质量评分
    score = score_content(text, url="...", keyword="三体")
    # → {"quality": 0.75, "type": "novel", "signals": [...]}

    # 关键词精准匹配
    hits = match_keywords(text, "三体 小说")
    # → {"matches": ["三体", "小说"], "density": 0.03, "head_bonus": True}
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

# ============================================================
# 共享常量
# ============================================================

# 统一 boilerplate 关键词——同时被 score_content() 和 completeness.py 使用
BOILERPLATE_KEYWORDS = [
    "登录", "login", "注册", "register", "copyright", "cookie",
    "隐私", "privacy", "条款", "Sign in",
    "验证码", "验证", "请登录后", "登录后查看", "立即登录",
    "请先登录", "需要登录", "密码", "password",
]

# ============================================================
# URL 噪声模式
# ============================================================

# 高置信度噪声——这些页面不可能是正文
NOISE_PATTERNS_HIGH = [
    # 功能页面
    r"/login[/?#]?", r"/signup[/?#]?", r"/register[/?#]?",
    r"/logout[/?#]?", r"/auth[/?#]?", r"/oauth[/?#]?",
    # 搜索页
    r"/search[/?#]?", r"/find[/?#]?", r"\?s=", r"\?q=", r"\?search=",
    r"\?keyword=", r"\?query=", r"/s/", r"/search/",
    # 管理后台
    r"/admin[/?#]?", r"/wp-admin", r"/dashboard[/?#]?",
    r"/settings[/?#]?", r"/account[/?#]?", r"/profile[/?#]?",
    # 购物车/支付
    r"/cart[/?#]?", r"/checkout[/?#]?", r"/payment[/?#]?",
    r"/order[/?#]?", r"/billing[/?#]?",
    # 静态文件
    r"\.(png|jpg|jpeg|gif|svg|webp|ico)(\?|$)",
    r"\.(css|js|json|xml|rss|atom)(\?|$)",
    r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|zip|tar|gz)(\?|$)",
    r"\.(mp4|mp3|avi|mov|wmv|flv|webm)(\?|$)",
    # 邮件/电话链接
    r"^mailto:", r"^tel:", r"^javascript:",
    # 锚点/空链接
    r"^#$", r"^#.*",
]

# 中置信度噪声——可能是正文但也可能是导航
NOISE_PATTERNS_MID = [
    # 归档/分类/标签
    r"/tag(s)?/", r"/category/", r"/categories/", r"/archives?/",
    r"/author/", r"/user/",
    # 日期归档
    r"/\d{4}/\d{2}/$", r"/\d{4}/\d{2}/\d{2}/$",
    # 评论
    r"/comment", r"/trackback", r"/pingback",
    # 分享
    r"/share[/?#]?", r"/embed[/?#]?",
    # 打印版本
    r"\?print=", r"/print[/?#]?", r"/amp[/?#]?",
    # 分页（不是第1页）
    r"/page/[2-9]\d*", r"\?paged?=[2-9]\d*",
    # RSS
    r"/feed[/?#]?", r"/rss[/?#]?", r"/atom[/?#]?",
    # 法律/隐私
    r"/privacy[/?#]?", r"/terms[/?#]?", r"/tos[/?#]?",
    r"/legal[/?#]?", r"/disclaimer[/?#]?",
    # 关于/联系
    r"/about[/?#]?", r"/contact[/?#]?", r"/help[/?#]?",
    r"/faq[/?#]?", r"/support[/?#]?",
]

# ============================================================
# 内容类型签名
# ============================================================

class ContentType(Enum):
    UNKNOWN = "unknown"
    NOVEL_CHAPTER = "novel_chapter"      # 小说章节页
    NOVEL_INDEX = "novel_index"          # 小说目录页
    BLOG_ARTICLE = "blog_article"        # 博客文章
    DOCUMENTATION = "documentation"      # 技术文档
    NEWS = "news"                        # 新闻
    PRODUCT = "product"                  # 商品页
    JOB = "job"                          # 招聘页
    FORUM = "forum"                      # 论坛帖子

# 内容类型检测规则：(类型, 必须匹配, 奖励匹配, 惩罚匹配)
TYPE_SIGNATURES = {
    ContentType.NOVEL_CHAPTER: {
        "must": [
            (r"第[一二三四五六七八九十百千\d]+章", 10),
            (r"第[一二三四五六七八九十百千\d]+[节回卷]", 8),
            (r"(章|节)[\s\n]", 5),
        ],
        "bonus": [
            (r"[。！？……\n]{3,}", 15),       # 密集标点 → 小说特征
            (r"(说道|说道|喊道|想到|心道|暗想)", 8),  # 小说对话标记
            (r"(只见|忽然|顿时|当下|接着|随后)", 5),
            (r"[一-鿿]{100,}", 3),   # 长段中文
        ],
        "penalty": [
            (r"(代码|函数|安装|配置|部署|npm|pip|import)", -15),
            (r"(薪资|岗位|招聘|经验|学历)", -15),
        ],
        "min_length": 500,
    },
    ContentType.NOVEL_INDEX: {
        "must": [
            (r"第[一二三四五六七八九十百千\d]+章", 5),
        ],
        "bonus": [
            (r"(章节目录|正文卷|作品相关|分卷)", 10),
            (r"(最新章节|最近更新|加入书架|开始阅读)", 8),
            (r"作者[:：]", 5),
        ],
        "penalty": [
            (r"[。！？……\n]{5,}", -10),  # 标点密度高 → 不是目录
        ],
        "min_length": 200,
    },
    ContentType.BLOG_ARTICLE: {
        "must": [
            (r"(发布于|发表于|\d{4}[-/]\d{1,2}[-/]\d{1,2})", 5),
        ],
        "bonus": [
            (r"(分享到|点赞|收藏|打赏|关注)", 8),
            (r"(版权|转载|原创|作者)", 6),
        ],
        "penalty": [],
        "min_length": 300,
    },
    ContentType.DOCUMENTATION: {
        "must": [
            (r"```|`[^`]+`", 5),
        ],
        "bonus": [
            (r"(安装|配置|使用|参数|返回|示例|注意|警告)", 8),
            (r"(npm|pip|import|require|export|function|class)", 10),
        ],
        "penalty": [],
        "min_length": 200,
    },
    ContentType.JOB: {
        "must": [
            (r"(薪资|工资|待遇|薪酬)", 10),
        ],
        "bonus": [
            (r"(岗位|职位|招聘|JD)", 8),
            (r"(要求|职责|经验|学历|福利)", 6),
            (r"(Boss|HR|猎头|内推)", 4),
        ],
        "penalty": [],
        "min_length": 100,
    },
    ContentType.NEWS: {
        "must": [
            (r"(\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2})", 8),
        ],
        "bonus": [
            (r"(记者|报道|消息|据悉|近日|目前)", 6),
            (r"(来源[:：]|编辑[:：]|责任编辑)", 5),
        ],
        "penalty": [],
        "min_length": 200,
    },
}


# ============================================================
# Public API
# ============================================================

def classify_url(url: str) -> dict:
    """
    判断 URL 是否值得抓取 + 推断内容类型。

    Returns:
        {
            "is_content": bool,     # 是否像正文页
            "type": str,            # 内容类型
            "confidence": float,    # 0.0-1.0
            "reason": str,          # 判定原因
        }
    """
    # 高置信度噪声 → 直接拒
    for pattern in NOISE_PATTERNS_HIGH:
        if re.search(pattern, url, re.IGNORECASE):
            return {"is_content": False, "type": "noise_high",
                    "confidence": 0.95, "reason": f"matched:{pattern}"}

    # 中置信度噪声 → 可疑但不确定
    mid_hits = 0
    for pattern in NOISE_PATTERNS_MID:
        if re.search(pattern, url, re.IGNORECASE):
            mid_hits += 1

    if mid_hits >= 3:
        return {"is_content": False, "type": "noise_mid_multi",
                "confidence": 0.7, "reason": f"matched {mid_hits} noise patterns"}
    if mid_hits >= 1:
        return {"is_content": True, "type": "uncertain",
                "confidence": 0.4, "reason": f"matched {mid_hits} noise pattern(s)"}

    # URL 结构推断内容类型
    inferred_type = _infer_type_from_url(url)

    return {"is_content": True, "type": inferred_type,
            "confidence": 0.65, "reason": "clean URL"}


def score_content(
    text: str,
    *,
    url: str = "",
    keyword: Optional[str] = None,
    expected_type: Optional[str] = None,
) -> dict:
    """
    对抓取到的内容打分——质量 + 类型匹配 + 关键词相关性。

    Returns:
        {
            "quality": float,       # 0.0-1.0 综合质量分
            "type": str,            # 检测到的内容类型
            "type_confidence": float,
            "signals": [str],       # 触发信号列表
            "keyword_density": float,
            "keyword_head_bonus": bool,
        }
    """
    if not text or len(text.strip()) < 50:
        return {"quality": 0.0, "type": "empty", "type_confidence": 0.0,
                "signals": [], "keyword_density": 0.0, "keyword_head_bonus": False}

    text = text.strip()
    result = {
        "quality": 0.0,
        "type": "unknown",
        "type_confidence": 0.0,
        "signals": [],
        "keyword_density": 0.0,
        "keyword_head_bonus": False,
    }

    # ---- 1. 内容类型检测 ----
    best_type, best_score = _detect_content_type(text)
    result["type"] = best_type.value
    result["type_confidence"] = min(best_score / 40, 1.0)  # 归一化

    # ---- 2. 基础质量分 ----
    quality = 0.0
    length = len(text)

    # 长度分
    if length >= 2000:
        quality += 0.35
    elif length >= 1000:
        quality += 0.25
    elif length >= 500:
        quality += 0.15
    elif length >= 200:
        quality += 0.08

    # 结构分
    para_count = len(re.findall(r"\n\s*\n", text))
    if para_count >= 10:
        quality += 0.20
    elif para_count >= 5:
        quality += 0.12
    elif para_count >= 2:
        quality += 0.06

    # 完整句子分
    sentences = len(re.findall(r"[。！？.!?\n]", text))
    if sentences >= 20:
        quality += 0.15
    elif sentences >= 5:
        quality += 0.08

    # 噪声惩罚
    top = text[:500].lower()
    hits = sum(1 for kw in BOILERPLATE_KEYWORDS if kw.lower() in top)
    if hits >= 3:
        quality -= 0.25
        result["signals"].append("boilerplate_dense")
    elif hits >= 1:
        quality -= 0.10

    # 导航/目录页惩罚
    if best_type == ContentType.NOVEL_INDEX:
        quality -= 0.15
        result["signals"].append("is_index_page")

    result["quality"] = round(max(0.0, min(quality, 1.0)), 3)

    # ---- 3. 关键词匹配 ----
    if keyword:
        kw_result = match_keywords(text, keyword)
        result["keyword_density"] = kw_result["density"]
        result["keyword_head_bonus"] = kw_result["head_bonus"]

        # 关键词加分
        if kw_result["head_bonus"]:
            quality += 0.10
        quality += min(kw_result["density"] * 8, 0.15)  # 密度加分
        # 关键词太少 → 可能是误匹配
        if kw_result["matches"] == 0 and len(keyword) >= 2:
            quality -= 0.20
            result["signals"].append("keyword_not_found")

    result["quality"] = round(max(0.0, min(quality, 1.0)), 3)

    return result


def match_keywords(text: str, keyword: str) -> dict:
    """
    精准关键词匹配——支持多关键词、词边界、标题优先。

    Args:
        text: 正文
        keyword: 空格分隔的多关键词，如 "三体 刘慈欣"

    Returns:
        {
            "matches": ["三体", "刘慈欣"],
            "density": 0.03,
            "head_bonus": True,     # 关键词出现在前 20%
            "positions": [45, 234], # 各关键词首次出现位置
        }
    """
    if not keyword or not text:
        return {"matches": [], "density": 0.0, "head_bonus": False, "positions": []}

    keywords = [kw.strip() for kw in keyword.split() if len(kw.strip()) >= 2]
    if not keywords:
        return {"matches": [], "density": 0.0, "head_bonus": False, "positions": []}

    matched = []
    positions = []
    total_hits = 0
    text_lower = text.lower()

    for kw in keywords:
        found = False
        kw_lower = kw.lower()
        # 判断是否主要为 CJK 字符
        cjk_count = sum(1 for c in kw if "一" <= c <= "鿿")
        is_cjk = cjk_count >= len(kw) * 0.5

        if is_cjk:
            # CJK：用子串匹配，中文没有空格分词
            # 但要排除明显误匹配（关键词是更长的复合词的一部分且语义不同）
            for m in re.finditer(re.escape(kw_lower), text_lower):
                # 检查是否被其他 CJK 字包围构成更长的词
                # 如果匹配位置的上下文在同一条 CJK 字串中，视为合法匹配
                total_hits += 1
                if not found:
                    matched.append(kw)
                    positions.append(m.start())
                    found = True
        else:
            # 英文/混合：用词边界避免 "AI" 匹配到 "MAIL"
            pattern = re.compile(
                r"(?<![a-zA-Z])" + re.escape(kw_lower) + r"(?![a-zA-Z])"
            )
            for m in pattern.finditer(text_lower):
                total_hits += 1
                if not found:
                    matched.append(kw)
                    positions.append(m.start())
                    found = True

    length = max(len(text), 1)
    density = total_hits / (length / 100)  # 每 100 字中的命中次数

    # 是否在开头就出现
    head_bonus = any(p < length * 0.2 for p in positions) if positions else False

    return {
        "matches": matched,
        "density": round(density, 4),
        "head_bonus": head_bonus,
        "positions": positions[:5],
    }


# ============================================================
# Internal helpers
# ============================================================

def _infer_type_from_url(url: str) -> str:
    """从 URL 结构推断内容类型。"""
    url_lower = url.lower()

    # 小说站特征
    novel_domains = [
        "qidian", "zongheng", "17k", "biquge", "xbiquge", "shuqi",
        "qiego", "xiaoshuo", "novel", "read", "chapter", "book",
        "69shu", "paoshu8", "xbiquge", "biqu", "du1du",
    ]
    if any(d in url_lower for d in novel_domains):
        if re.search(r"/(\d+)(_?\d+)?\.html?", url_lower):
            return "novel_chapter"
        if re.search(r"/(index|list|catalog|menu|dir)", url_lower):
            return "novel_index"
        return "novel_chapter"  # 默认

    # 博客
    blog_domains = ["blog", "medium", "dev.to", "hashnode", "cnblogs", "jianshu",
                    "zhihu", "weixin", "mp.weixin", "juejin", "segmentfault"]
    if any(d in url_lower for d in blog_domains):
        return "blog_article"

    # 文档
    doc_domains = ["docs", "doc", "wiki", "readthedocs", "guide", "tutorial",
                   "manual", "reference"]
    if any(d in url_lower for d in doc_domains):
        return "documentation"

    # 招聘
    job_domains = ["zhipin", "liepin", "lagou", "51job", "job", "zhaopin",
                   "linkedin.com/jobs", "indeed", "glassdoor"]
    if any(d in url_lower for d in job_domains):
        return "job"

    # 新闻
    news_domains = ["news", "toutiao", "163.com", "sina", "sohu", "ifeng",
                    "thepaper", "huxiu", "36kr", "geekbang"]
    if any(d in url_lower for d in news_domains):
        return "news"

    return "unknown"


def _detect_content_type(text: str) -> tuple[ContentType, float]:
    """根据文本内容检测类型。返回 (最佳类型, 分数)。"""
    best_type = ContentType.UNKNOWN
    best_score = 0.0

    for ctype, sig in TYPE_SIGNATURES.items():
        score = 0.0

        # 长度检查
        if len(text) < sig.get("min_length", 0):
            continue

        # 必须匹配项
        must_ok = False
        for pattern, weight in sig["must"]:
            if re.search(pattern, text):
                score += weight
                must_ok = True

        if not must_ok:
            continue

        # 奖励匹配项
        for pattern, weight in sig["bonus"]:
            if re.search(pattern, text):
                score += weight

        # 惩罚匹配项
        for pattern, weight in sig["penalty"]:
            if re.search(pattern, text):
                score += weight

        if score > best_score:
            best_score = score
            best_type = ctype

    return best_type, best_score
