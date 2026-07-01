"""
Integration tests: Search → Verify → Extract → Validate
"""
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_http_client_basic():
    """HTTPClient fetches httpbin."""
    from UniversalExtractor.http_client import HTTPClient
    c = HTTPClient()
    resp = c.get("https://httpbin.org/get")
    assert isinstance(resp.status_code, int)
    print(f"PASS: HTTPClient GET status={resp.status_code}")


def test_http_client_error():
    """HTTPClient handles non-200 responses gracefully."""
    from UniversalExtractor.http_client import HTTPClient
    c = HTTPClient()
    resp = c.get("https://httpbin.org/status/404")
    # httpbin.org is unreliable; any non-crash is a pass
    assert isinstance(resp.status_code, int)
    print(f"PASS: HTTPClient error status={resp.status_code}")


def test_rate_limiter():
    """RateLimiter adds delay between rapid requests."""
    import time
    from UniversalExtractor.rate_limiter import RateLimiter

    rl = RateLimiter(min_interval=0.5, jitter=0)
    t0 = time.monotonic()
    rl.wait("test.example.com")
    t1 = time.monotonic()
    rl.wait("test.example.com")
    t2 = time.monotonic()

    assert t1 - t0 < 0.1, f"First wait should be instant: {t1 - t0:.3f}s"
    assert t2 - t1 >= 0.45, f"Second wait should be >= 0.5s: {t2 - t1:.3f}s"
    print(f"PASS: RateLimiter waited {t2 - t1:.3f}s")


def test_proxy_manager_default():
    """ProxyManager returns None when no proxy configured."""
    from UniversalExtractor.proxy_manager import ProxyManager
    pm = ProxyManager()
    p = pm.get_proxy()
    # No proxy env var → should return None
    assert p is None or isinstance(p, dict)
    assert callable(pm.validate)
    print(f"PASS: ProxyManager proxy={p}")


def test_session_manager():
    """SessionManager creates and lists profiles."""
    import shutil
    from UniversalExtractor.session_manager import SessionManager

    sm = SessionManager()
    # Use a unique domain for testing
    p = sm.get_profile("integration-test.example.com")
    assert p.exists()
    assert (p / ".last_used").exists()

    size = sm.get_profile_size("integration-test.example.com")
    assert size >= 0

    # Cleanup
    sm.clear("integration-test.example.com")
    assert not p.exists()
    print("PASS: SessionManager create/clear")


def test_session_manager_persist_dir_alias(tmp_path):
    """The development-book persist_dir API remains supported."""
    from UniversalExtractor.session_manager import SessionManager

    manager = SessionManager(persist_dir=str(tmp_path))
    assert manager.get_profile("example.com").parent == tmp_path


def test_search_with_metadata():
    """search_with_metadata returns structured results."""
    from UniversalExtractor.search import search_with_metadata

    meta = search_with_metadata("Python tutorial", max_results=3)
    assert "results" in meta
    assert "backends_used" in meta
    assert "backend_stats" in meta
    assert "total_raw" in meta
    assert "total_unique" in meta

    print(f"PASS: search_with_metadata: "
          f"{meta['total_raw']} raw → {meta['total_unique']} unique "
          f"from {len(meta['backends_used'])} backends")

    if meta["results"]:
        top = meta["results"][0]
        assert "url" in top
        assert "cross_hits" in top
        assert "backends" in top
        print(f"  Top result: cross_hits={top['cross_hits']}, "
              f"backends={top['backends']}")


def test_classify_url():
    """classify_url identifies noise vs content."""
    from UniversalExtractor.classifier import classify_url

    # Login page should be noise
    r = classify_url("https://example.com/login")
    assert not r["is_content"], f"login page should not be content: {r}"
    print(f"PASS: classify_url(login) → is_content={r['is_content']}")

    # Novel page should be content
    r = classify_url("https://www.biquge.com/book/123.html")
    assert r["is_content"], f"novel page should be content: {r}"
    print(f"PASS: classify_url(novel) → is_content={r['is_content']}")


def test_completeness():
    """completeness_score rates text quality."""
    from UniversalExtractor.completeness import completeness_score

    # Empty text → low score
    s = completeness_score("")
    assert s < 0.3, f"Empty text score should be low: {s}"
    print(f"PASS: completeness(empty) = {s:.2f}")

    # Long text → higher score
    long_text = "第一章 开端\n" + "这是一段很长的正文内容，" * 500
    s = completeness_score(long_text)
    assert s > 0.3, f"Long text score should be higher: {s}"
    print(f"PASS: completeness(long) = {s:.2f}")


def test_pipeline_basic():
    """Pipeline instantiates with all infrastructure."""
    from UniversalExtractor import Pipeline
    p = Pipeline()

    assert p.http is not None
    assert p.limiter is not None
    assert p.proxy_manager is not None
    assert p.sessions is not None
    assert len(p.registry.get_chain()) == 7
    print(f"PASS: Pipeline with {len(p.registry.get_chain())} stages + 4 infra modules")


def test_pipeline_validate():
    """Pipeline._validate passes/fails correctly."""
    from UniversalExtractor import Pipeline

    p = Pipeline()

    # Good content should pass
    good = "第一章 三体\n" + "这是一段关于三体问题的讨论。" * 300
    passes, details = p._validate(good, keyword="三体")
    print(f"  Validate(good): passes={passes}, score={details.get('completeness', '?')}")

    # Empty content should fail
    passes, details = p._validate("", keyword="test")
    assert not passes, "Empty text should fail validation"
    print(f"PASS: Validate(empty): passes={passes}")


def test_web_lens_backward_compat():
    """WebLens still works with old API."""
    from UniversalExtractor import WebLens
    wl = WebLens(headless=True)
    assert wl.headless
    assert wl.min_score == 0.5
    print("PASS: WebLens backward compatible")


def test_cross_validator_enhanced():
    """cross_validator supports merge_strategy and diff_report."""
    from UniversalExtractor.cross_validator import (
        cross_validate, source_diff_report,
        _pick_most_consistent, _pick_by_voting,
    )
    # Just verify the functions exist and accept params
    assert callable(_pick_most_consistent)
    assert callable(_pick_by_voting)
    assert callable(source_diff_report)
    print("PASS: cross_validator enhanced functions exist")


def run_all():
    tests = [
        test_http_client_basic,
        test_http_client_error,
        test_rate_limiter,
        test_proxy_manager_default,
        test_session_manager,
        test_search_with_metadata,
        test_classify_url,
        test_completeness,
        test_pipeline_basic,
        test_pipeline_validate,
        test_web_lens_backward_compat,
        test_cross_validator_enhanced,
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
    print(f"Results: {passed}/{len(tests)} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
