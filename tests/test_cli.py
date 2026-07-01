"""CLI output and batch-content regression tests."""

import argparse
import json

from UniversalExtractor import cli
from UniversalExtractor.pipeline import PipelineResult


class _FakePipeline:
    def __init__(self, _config) -> None:
        pass

    def run(self, query: str = "", url: str | None = None, **_kwargs) -> PipelineResult:
        value = url or query
        return PipelineResult(
            query=query,
            url=url or "https://example.com",
            text=f"content:{value}",
            score=0.8,
            success=True,
            winning_stage="fake",
        )


def test_batch_output_keeps_extracted_text(tmp_path, monkeypatch) -> None:
    source = tmp_path / "batch.txt"
    target = tmp_path / "result.json"
    source.write_text("query one\nhttps://example.com/page\n", encoding="utf-8")
    monkeypatch.setattr(cli, "Pipeline", _FakePipeline)
    args = argparse.Namespace(
        file=str(source), output=str(target), format="json", headless=True,
        min_score=0.5, max_candidates=2, stages=None,
    )

    assert cli.cmd_batch(args) == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload[0]["text"] == "content:query one"
    assert payload[1]["text"] == "content:https://example.com/page"


def test_parser_accepts_markdown_format() -> None:
    args = cli.build_parser().parse_args(["run", "demo", "--format", "md"])
    assert args.format == "md"
