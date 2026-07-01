"""UniversalExtractor public API with lightweight, lazy imports."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    # Direct extraction
    "UniversalExtractor": (".extractor", "UniversalExtractor"),
    "ExtractionError": (".extractor", "ExtractionError"),
    "ExtractionResult": (".extractor", "ExtractionResult"),
    # Closed-loop pipeline
    "Pipeline": (".pipeline", "Pipeline"),
    "PipelineConfig": (".pipeline", "PipelineConfig"),
    "PipelineResult": (".pipeline", "PipelineResult"),
    "PipelineStageResult": (".pipeline", "PipelineStageResult"),
    "StageContext": (".pipeline", "StageContext"),
    "StageRegistry": (".pipeline", "StageRegistry"),
    "ExtractionStage": (".pipeline", "ExtractionStage"),
    "JinaReaderStage": (".pipeline", "JinaReaderStage"),
    "CurlCffiStage": (".pipeline", "CurlCffiStage"),
    "BrowserDomStage": (".pipeline", "BrowserDomStage"),
    "CanvasHookStage": (".pipeline", "CanvasHookStage"),
    "CdpHeapStage": (".pipeline", "CdpHeapStage"),
    "ScreenshotOcrStage": (".pipeline", "ScreenshotOcrStage"),
    "VisionLlmStage": (".pipeline", "VisionLlmStage"),
    # Legacy facades and JD
    "WebLens": (".weblens", "WebLens"),
    "WebLensResult": (".weblens", "WebLensResult"),
    "JDEngine": (".jd_engine", "JDEngine"),
    "JDResult": (".jd_engine", "JDResult"),
    "PLATFORM_CONFIG": (".jd_engine", "PLATFORM_CONFIG"),
    # Infrastructure
    "HTTPClient": (".http_client", "HTTPClient"),
    "HTTPResponse": (".http_client", "HTTPResponse"),
    "RateLimiter": (".rate_limiter", "RateLimiter"),
    "ProxyManager": (".proxy_manager", "ProxyManager"),
    "SessionManager": (".session_manager", "SessionManager"),
    "CaptchaSolver": (".captcha_solver", "CaptchaSolver"),
    "CaptchaResult": (".captcha_solver", "CaptchaResult"),
    # Search and classification
    "search_urls": (".search", "search_urls"),
    "search_compare": (".search", "search_compare"),
    "is_likely_content_url": (".search", "is_likely_content_url"),
    "classify_url": (".classifier", "classify_url"),
    "score_content": (".classifier", "score_content"),
    "match_keywords": (".classifier", "match_keywords"),
    # Vision/OCR providers
    "VisionProvider": (".ocr_providers", "VisionProvider"),
    "OpenAIProvider": (".ocr_providers", "OpenAIProvider"),
    "AnthropicProvider": (".ocr_providers", "AnthropicProvider"),
    "QwenProvider": (".ocr_providers", "QwenProvider"),
    "DeepSeekProvider": (".ocr_providers", "DeepSeekProvider"),
    "TesseractProvider": (".ocr_providers", "TesseractProvider"),
    "auto_configure_providers": (".ocr_providers", "auto_configure_providers"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Load a public symbol on first access and cache it in this module."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose lazy public symbols to introspection tools."""
    return sorted(set(globals()) | set(__all__))
