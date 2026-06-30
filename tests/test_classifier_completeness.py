"""Unit tests for classifier + completeness modules."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_classify_url_novel():
    from UniversalExtractor.classifier import classify_url
    r = classify_url("https://www.biquge.com/book/123.html")
    assert r["is_content"] is True
    assert r["type"] == "novel_chapter"


def test_classify_url_login():
    from UniversalExtractor.classifier import classify_url
    r = classify_url("https://example.com/login")
    assert r["is_content"] is False
    assert "noise" in r["type"]


def test_classify_url_search():
    from UniversalExtractor.classifier import classify_url
    r = classify_url("https://example.com/search?q=test")
    assert r["is_content"] is False


def test_classify_url_cart():
    from UniversalExtractor.classifier import classify_url
    r = classify_url("https://shop.com/cart")
    assert r["is_content"] is False


def test_classify_url_clean():
    from UniversalExtractor.classifier import classify_url
    r = classify_url("https://blog.example.com/how-to-code")
    assert r["is_content"] is True


def test_score_content_quality():
    from UniversalExtractor.classifier import score_content
    long_text = ("第一章 开端\n" + "这是一段很长的中文正文内容，" * 300)
    r = score_content(long_text, url="https://novel.com/chapter1.html")
    assert r["quality"] > 0.3, f"Quality too low: {r['quality']}"
    assert r["type"] == "novel_chapter"


def test_score_content_empty():
    from UniversalExtractor.classifier import score_content
    r = score_content("", url="https://example.com")
    assert r["quality"] < 0.3


def test_score_content_keyword():
    from UniversalExtractor.classifier import score_content
    text = "三体 第一章 科学边界\n" + "正文内容关于三体问题。" * 100
    r = score_content(text, url="https://novel.com/1.html", keyword="三体")
    assert r["keyword_density"] > 0
    assert r["keyword_head_bonus"] is True


def test_completeness_empty():
    from UniversalExtractor.completeness import completeness_score
    s = completeness_score("")
    assert s < 0.2


def test_completeness_short():
    from UniversalExtractor.completeness import completeness_score
    s = completeness_score("短文本")
    assert s < 0.3


def test_completeness_long():
    from UniversalExtractor.completeness import completeness_score
    long_text = "第一章 开端\n" + "这是一段很长的正文内容，" * 500
    s = completeness_score(long_text)
    assert s > 0.3, f"Score too low: {s:.4f}"
    print(f"  Score: {s:.3f}")


def test_completeness_boilerplate():
    from UniversalExtractor.completeness import completeness_score
    text = "登录 注册 copyright cookie 隐私 privacy " + "正文" * 200
    s = completeness_score(text)
    # Heavily boiled text should score lower than a clean long text
    assert s < 0.8


def run_all():
    tests = [
        test_classify_url_novel,
        test_classify_url_login,
        test_classify_url_search,
        test_classify_url_cart,
        test_classify_url_clean,
        test_score_content_quality,
        test_score_content_empty,
        test_score_content_keyword,
        test_completeness_empty,
        test_completeness_short,
        test_completeness_long,
        test_completeness_boilerplate,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS: {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {t.__name__}: {e}")
            failed += 1
    print(f"\nResults: {passed}/{len(tests)} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
