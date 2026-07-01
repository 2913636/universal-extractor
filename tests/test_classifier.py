"""Focused URL-classification regression tests."""

from UniversalExtractor.classifier import classify_url


def test_classify_url_novel() -> None:
    result = classify_url("https://www.biquge.com/book/123.html")
    assert result["is_content"] is True


def test_classify_url_noise() -> None:
    result = classify_url("https://example.com/login")
    assert result["is_content"] is False
