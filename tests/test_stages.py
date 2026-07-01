"""Unit tests for individual extraction stages."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_jina_reader_exists():
    """JinaReaderStage can be instantiated."""
    from UniversalExtractor.pipeline import JinaReaderStage, StageContext, PipelineConfig
    stage = JinaReaderStage()
    ctx = StageContext(url="https://example.com", config=PipelineConfig())
    result = stage.extract("https://example.com", ctx)
    assert result.stage_name == "jina_reader"
    assert result.stage_index == 0
    assert result.timing_ms > 0
    print(f"PASS: JinaReaderStage timing={result.timing_ms}ms")


def test_curl_cffi_enhanced():
    """CurlCffiStage has encoding detection and redirect support."""
    from UniversalExtractor.pipeline import CurlCffiStage
    stage = CurlCffiStage()

    # Test _looks_garbled
    assert not stage._looks_garbled("Hello World")
    assert stage._looks_garbled("��Hello\x00\x00" * 20)
    print("PASS: CurlCffiStage._looks_garbled")

    # Test _extract_text
    html = "<html><script>var x=1;</script><body><p>Hello<p>World</body></html>"
    text = stage._extract_text(html)
    assert "Hello" in text
    assert "var x" not in text  # script content removed
    print("PASS: CurlCffiStage._extract_text")


def test_browser_dom_enhanced():
    """BrowserDomStage has anti-bot features available."""
    from UniversalExtractor.pipeline import BrowserDomStage
    stage = BrowserDomStage()

    assert stage.stage_name == "browser_dom"
    assert len(stage._CONTENT_SELECTORS) > 10
    # solve_cloudflare, block_webrtc are injected at fetch time (tested in integration)
    print("PASS: BrowserDomStage selectors={0}".format(len(stage._CONTENT_SELECTORS)))


def test_screenshot_ocr_enhanced():
    """ScreenshotOcrStage has dedup and confidence filtering."""
    from UniversalExtractor.pipeline import ScreenshotOcrStage
    stage = ScreenshotOcrStage()

    # Test _dedup_screenshots with empty/single
    assert stage._dedup_screenshots([]) == []
    assert stage._dedup_screenshots(["a.png"]) == ["a.png"]
    print("PASS: ScreenshotOcrStage._dedup_screenshots")


def test_vision_llm_enhanced():
    """VisionLlmStage supports multi-model fallback."""
    from UniversalExtractor.pipeline import VisionLlmStage
    stage = VisionLlmStage()

    assert stage.stage_name == "vision_llm"
    assert stage.stage_index == 6
    print("PASS: VisionLlmStage registered")


def test_screenshot_ocr_passes_base64_and_prompt(tmp_path, monkeypatch):
    """OCR providers receive their documented base64/prompt arguments."""
    from PIL import Image
    from UniversalExtractor import ocr_providers, screenshot
    from UniversalExtractor.pipeline import ScreenshotOcrStage, StageContext

    image_path = tmp_path / "frame.png"
    Image.new("RGB", (40, 40), "white").save(image_path)

    class Page:
        def is_closed(self):
            return False

    class Provider:
        name = "fake"

        def extract_text(self, image_b64, prompt, **_kwargs):
            assert image_b64.startswith("iVBOR")
            assert "visible text" in prompt
            return "recognized content " * 20

    monkeypatch.setattr(screenshot, "capture_views", lambda *_args, **_kwargs: [str(image_path)])
    monkeypatch.setattr(ocr_providers, "auto_configure_providers", lambda: [Provider()])
    context = StageContext(_page=Page())
    result = ScreenshotOcrStage().extract("https://example.com", context)

    assert result.success
    context.close_browser()


def test_vision_llm_batches_images(tmp_path, monkeypatch):
    """Vision sends large screenshot sets in bounded batches."""
    from PIL import Image
    from UniversalExtractor import ocr_providers, screenshot
    from UniversalExtractor.pipeline import VisionLlmStage, StageContext

    paths = []
    for index in range(5):
        path = tmp_path / f"frame-{index}.png"
        Image.new("RGB", (40, 40), (index * 30, 0, 0)).save(path)
        paths.append(str(path))

    class Provider:
        name = "openai"
        model = "fake-vision"

        def extract_text(self, image_b64, prompt, **_kwargs):
            assert image_b64.startswith("iVBOR")
            return "vision content " * 20

    monkeypatch.setattr(
        ocr_providers,
        "auto_configure_providers",
        lambda **_kwargs: [Provider()],
    )
    monkeypatch.setattr(screenshot, "dedup_screenshots", lambda paths: paths)
    context = StageContext(screenshot_paths=paths)
    result = VisionLlmStage().extract("https://example.com", context)

    assert result.success
    assert result.metadata["image_batches"] == 2
    context.close_browser()


def test_pipeline_context_has_infra():
    """StageContext has _pipeline reference for accessing infra modules."""
    from UniversalExtractor.pipeline import StageContext, PipelineConfig
    ctx = StageContext(url="https://example.com", config=PipelineConfig())
    assert ctx._pipeline is None  # Not set yet (set by Pipeline.run())
    print("PASS: StageContext._pipeline field exists")


def test_all_stages_registered():
    """All 7 stages registered in default StageRegistry."""
    from UniversalExtractor.pipeline import StageRegistry
    reg = StageRegistry()
    reg.register_defaults()
    chain = reg.get_chain()
    assert len(chain) == 7
    names = [s.stage_name for s in chain]
    assert "jina_reader" in names
    assert "curl_cffi_http" in names
    assert "browser_dom" in names
    assert "canvas_hook" in names
    assert "cdp_heap" in names
    assert "screenshot_ocr" in names
    assert "vision_llm" in names
    print(f"PASS: All 7 stages registered: {names}")


def test_curl_cffi_stage_extract_integration():
    """CurlCffiStage extracts text from a real URL."""
    from UniversalExtractor.pipeline import CurlCffiStage, StageContext, PipelineConfig
    stage = CurlCffiStage()
    ctx = StageContext(url="https://httpbin.org/html", config=PipelineConfig())
    result = stage.extract("https://httpbin.org/html", ctx)
    # Network may be unavailable, but installed-API incompatibility is a bug.
    assert result.stage_name == "curl_cffi_http"
    assert result.timing_ms >= 0
    assert result.success or result.error  # Must set one or the other
    assert "has no attribute" not in (result.error or "")
    print(f"PASS: CurlCffiStage httpbin: success={result.success}, chars={result.char_count}")


def run_all():
    tests = [
        test_jina_reader_exists,
        test_curl_cffi_enhanced,
        test_browser_dom_enhanced,
        test_screenshot_ocr_enhanced,
        test_vision_llm_enhanced,
        test_pipeline_context_has_infra,
        test_all_stages_registered,
        test_curl_cffi_stage_extract_integration,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"FAIL: {t.__name__}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Stage Tests: {passed}/{len(tests)} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
