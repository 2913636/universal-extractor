"""
Regression test suite. Run after any code change to verify core integrity.

Usage: py -3 tests/run_regression.py
"""
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def run(cmd: str, desc: str) -> bool:
    """Run a command and return True if it succeeds."""
    print(f"\n[{desc}]")
    result = subprocess.run(
        [PYTHON, "-c", cmd],
        cwd=str(ROOT),
        capture_output=True,
        timeout=30,
    )
    if result.returncode == 0:
        print(f"  PASS: {desc}")
        return True
    else:
        print(f"  FAIL: {desc}")
        print(f"  stderr: {result.stderr.decode()[:500]}")
        return False


def run_module(module: str, desc: str) -> bool:
    """Run a Python module test and return True if it succeeds."""
    print(f"\n[{desc}]")
    result = subprocess.run(
        [PYTHON, "-m", module],
        cwd=str(ROOT),
        capture_output=True,
        timeout=30,
    )
    if result.returncode == 0:
        print(f"  PASS: {desc}")
        return True
    else:
        print(f"  FAIL: {desc}")
        print(f"  stderr: {result.stderr.decode()[:500]}")
        return False


def main():
    TESTS = [
        # Core imports
        ("from UniversalExtractor import Pipeline, PipelineConfig, PipelineResult", "Pipeline import"),
        ("from UniversalExtractor import UniversalExtractor, WebLens, WebLensResult", "Legacy API import"),
        ("from UniversalExtractor import HTTPClient, RateLimiter, ProxyManager, SessionManager", "Infrastructure import"),
        ("from UniversalExtractor import search_urls, classify_url, score_content, is_likely_content_url", "Search+Classify import"),
        ("from UniversalExtractor.completeness import completeness_score", "Completeness import"),
        ("from UniversalExtractor.cross_validator import cross_validate, merge_chapters, source_diff_report", "Cross-validator import"),
        # Stage registry
        ("from UniversalExtractor.pipeline import StageRegistry; r=StageRegistry(); r.register_defaults(); assert len(r.get_chain())==7", "7 stages registered"),
        # Classifier
        ("from UniversalExtractor.classifier import classify_url; r=classify_url('https://example.com/login'); assert not r['is_content']", "classify_url(noise)"),
        ("from UniversalExtractor.classifier import classify_url; r=classify_url('https://www.biquge.com/book/1.html'); assert r['is_content']", "classify_url(content)"),
        # Completeness
        ("from UniversalExtractor.completeness import completeness_score; s=completeness_score(''); assert s<0.2", "completeness(empty)"),
        ("from UniversalExtractor.completeness import completeness_score; s=completeness_score('a'*5000); assert s>0.3", "completeness(long)"),
        # Search
        ("from UniversalExtractor.search import search_urls; assert len(search_urls('test',max_results=2))>=0", "search_urls"),
        # Pipeline instantiation
        ("from UniversalExtractor import Pipeline; p=Pipeline(); assert p.http is not None; assert p.limiter is not None", "Pipeline infra"),
        # HTTPClient
        ("from UniversalExtractor.http_client import HTTPClient; c=HTTPClient(); print(type(c).__name__)", "HTTPClient exists"),
        # RateLimiter
        ("from UniversalExtractor.rate_limiter import RateLimiter; r=RateLimiter(); r.wait('test.com')", "RateLimiter works"),
    ]

    passed = 0
    failed = 0
    for cmd, desc in TESTS:
        if run(cmd, desc):
            passed += 1
        else:
            failed += 1

    # Run unit test files
    for test_file, desc in [
        ("tests.test_stages", "Stage unit tests"),
        ("tests.test_classifier_completeness", "Classifier+Completeness tests"),
    ]:
        if run_module(test_file, desc):
            passed += 1
        else:
            failed += 1

    print(f"\n{'='*60}")
    print(f"Regression: {passed}/{len(TESTS)+2} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
