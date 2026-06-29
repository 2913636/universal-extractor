"""
UniversalExtractor — 通用网页内容提取器。

6 层降级链，自动选择最佳策略提取正文：
  ① DOM 提取 → ② API 拦截 → ③ Canvas Hook → ④ CDP 扫描 → ⑤ 截图 OCR → ⑥ Vision LLM 全页

用法:
    from universal_extractor import UniversalExtractor, ExtractionError

    ue = UniversalExtractor(headless=False)  # 自动检测可用的 Vision 后端
    text = ue.extract("https://example.com/article")
    print(text)

JD 抓取引擎:
    from universal_extractor import JDEngine

    engine = JDEngine(headless=False)
    jd = engine.fetch_jd("https://www.zhipin.com/job_detail/xxx.html")
    print(jd.title, jd.requirements)

WebLens — 搜 + 筛 + 抓:
    from universal_extractor import WebLens

    wl = WebLens(headless=True)
    result = wl.search_and_extract("三体 小说 全文")
    print(result.text)
"""

from .extractor import UniversalExtractor, ExtractionError, ExtractionResult
from .jd_engine import JDEngine, JDResult, PLATFORM_CONFIG
from .ocr_providers import (
    VisionProvider,
    OpenAIProvider,
    AnthropicProvider,
    QwenProvider,
    DeepSeekProvider,
    TesseractProvider,
    auto_configure_providers,
)
from .search import search_urls, is_likely_content_url
from .classifier import classify_url, score_content, match_keywords
from .weblens import WebLens, WebLensResult

__all__ = [
    "UniversalExtractor",
    "ExtractionError",
    "ExtractionResult",
    "JDEngine",
    "JDResult",
    "PLATFORM_CONFIG",
    "WebLens",
    "WebLensResult",
    "search_urls",
    "is_likely_content_url",
    "VisionProvider",
    "OpenAIProvider",
    "AnthropicProvider",
    "QwenProvider",
    "DeepSeekProvider",
    "TesseractProvider",
    "auto_configure_providers",
]
