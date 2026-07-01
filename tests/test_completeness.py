"""Focused completeness-scoring regression tests."""

from UniversalExtractor.completeness import completeness_score


def test_completeness_empty() -> None:
    assert completeness_score("") < 0.2


def test_completeness_good() -> None:
    text = "第一章\n" + "这是一段很长的正文内容。" * 500
    assert completeness_score(text) > 0.5
