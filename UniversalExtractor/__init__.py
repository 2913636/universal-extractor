"""
UniversalExtractor — 通用网页内容提取器。

7 级降级链 + 闭环搜索验证:
  Jina Reader → curl_cffi HTTP → Browser DOM → Canvas Hook
  → CDP Heap → Screenshot OCR → Vision LLM

Pipeline — 闭环编排:
    from universal_extractor import Pipeline

    pipeline = Pipeline()
    result = pipeline.run("三体 小说 全文")
    print(result.text)

UniversalExtractor — 直接提取:
    from universal_extractor import UniversalExtractor, ExtractionError

    ue = UniversalExtractor(headless=False)
    text = ue.extract("https://example.com/article")
    print(text)

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
from .http_client import HTTPClient, HTTPResponse
from .rate_limiter import RateLimiter
from .proxy_manager import ProxyManager
from .session_manager import SessionManager
from .captcha_solver import CaptchaSolver, CaptchaResult
from .pipeline import (
    Pipeline,
    PipelineConfig,
    PipelineResult,
    PipelineStageResult,
    StageContext,
    StageRegistry,
    ExtractionStage,
    JinaReaderStage,
    CurlCffiStage,
    BrowserDomStage,
    CanvasHookStage,
    CdpHeapStage,
    ScreenshotOcrStage,
    VisionLlmStage,
)

__all__ = [
    # Core
    "UniversalExtractor",
    "ExtractionError",
    "ExtractionResult",
    # Pipeline
    "Pipeline",
    "PipelineConfig",
    "PipelineResult",
    "PipelineStageResult",
    "StageContext",
    "StageRegistry",
    "ExtractionStage",
    "JinaReaderStage",
    "CurlCffiStage",
    "BrowserDomStage",
    "CanvasHookStage",
    "CdpHeapStage",
    "ScreenshotOcrStage",
    "VisionLlmStage",
    # JD
    "JDEngine",
    "JDResult",
    "PLATFORM_CONFIG",
    # WebLens
    "WebLens",
    "WebLensResult",
    # Infrastructure
    "HTTPClient",
    "HTTPResponse",
    "RateLimiter",
    "ProxyManager",
    "SessionManager",
    "CaptchaSolver",
    "CaptchaResult",
    # Search
    "search_urls",
    "is_likely_content_url",
    # Vision
    "VisionProvider",
    "OpenAIProvider",
    "AnthropicProvider",
    "QwenProvider",
    "DeepSeekProvider",
    "TesseractProvider",
    "auto_configure_providers",
]
