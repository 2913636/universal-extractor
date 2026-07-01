"""Command-line interface for UniversalExtractor."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from .pipeline import Pipeline, PipelineConfig, PipelineResult, StageRegistry
from .search import search_compare


def _config(args: argparse.Namespace) -> PipelineConfig:
    """Build a pipeline configuration from parsed CLI options."""
    config = PipelineConfig(
        headless=args.headless,
        min_completeness=args.min_score,
        max_candidates=args.max_candidates,
        enable_cross_validation=getattr(args, "cross_validate", False),
    )
    if args.stages:
        registry = StageRegistry()
        registry.register_defaults()
        config.enabled_stages = [s.stage_name for s in registry.get_chain()][:args.stages]
    return config


def _result_dict(result: PipelineResult) -> dict[str, Any]:
    """Convert a pipeline result to a JSON-safe diagnostic payload."""
    return {
        "success": result.success,
        "score": result.score,
        "url": result.url,
        "text": result.text,
        "winning_stage": result.winning_stage,
        "stages_attempted": result.stages_attempted,
        "stages_succeeded": result.stages_succeeded,
        "total_time_ms": result.total_time_ms,
        "search_candidates_total": result.search_candidates_total,
        "search_candidates_scanned": result.search_candidates_scanned,
        "validation": result.validation_details,
        "extraction_chain": [asdict(item) for item in result.extraction_chain],
    }


def _format_result(result: PipelineResult, output_format: str) -> str:
    """Render a pipeline result as text, Markdown, or JSON."""
    if output_format == "json":
        return json.dumps(_result_dict(result), ensure_ascii=False, indent=2)
    if output_format == "md":
        status = "success" if result.success else "best-effort"
        return (
            f"# UniversalExtractor result\n\n"
            f"- Status: {status}\n- Score: {result.score:.3f}\n"
            f"- URL: {result.url or ''}\n- Stage: {result.winning_stage or 'none'}\n"
            f"- Time: {result.total_time_ms} ms\n\n## Content\n\n{result.text}\n"
        )
    return result.text


def _emit(content: str, output: str | None) -> None:
    """Write content to the selected destination."""
    if output:
        Path(output).write_text(content, encoding="utf-8")
    else:
        # Reconfigure for UTF-8 to avoid UnicodeEncodeError on non-UTF-8
        # terminals (e.g. Git Bash with GBK).
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
        print(content)


def _run_quietly(call: Callable[[], PipelineResult], quiet: bool) -> PipelineResult:
    """Keep JSON stdout machine-readable by moving progress output to stderr."""
    if quiet:
        with contextlib.redirect_stdout(sys.stderr):
            return call()
    return call()


def cmd_run(args: argparse.Namespace) -> int:
    """Run search, extraction, and validation."""
    pipeline = Pipeline(_config(args))
    result = _run_quietly(
        lambda: pipeline.run(query=args.query),
        args.format == "json",
    )
    _emit(_format_result(result, args.format), args.output)
    return 0 if result.text else 2


def cmd_extract(args: argparse.Namespace) -> int:
    """Extract a directly supplied URL."""
    pipeline = Pipeline(_config(args))
    result = _run_quietly(
        lambda: pipeline.run(url=args.url, mode="extract_only"),
        args.format == "json",
    )
    _emit(_format_result(result, args.format), args.output)
    return 0 if result.text else 2


def cmd_search(args: argparse.Namespace) -> int:
    """Search and report cross-engine discovery metadata."""
    metadata = search_compare(args.query, max_results=args.max_candidates)
    if args.format == "json":
        content = json.dumps(metadata, ensure_ascii=False, indent=2)
    elif args.format == "md":
        rows = ["# Search results", ""]
        rows.extend(
            f"{index}. [{item['url']}]({item['url']}) — {', '.join(item['backends'])}"
            for index, item in enumerate(metadata["results"], 1)
        )
        content = "\n".join(rows)
    else:
        content = "\n".join(item["url"] for item in metadata["results"])
    _emit(content, args.output)
    return 0 if metadata["results"] else 2


def cmd_batch(args: argparse.Namespace) -> int:
    """Process one query or URL per input-file line."""
    input_path = Path(args.file)
    if not input_path.is_file():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        return 2
    pipeline = Pipeline(_config(args))
    items: list[dict[str, Any]] = []
    for raw in input_path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if value.startswith(("http://", "https://")):
            call = lambda value=value: pipeline.run(url=value, mode="extract_only")
        else:
            call = lambda value=value: pipeline.run(query=value)
        result = _run_quietly(call, args.format == "json")
        items.append({"input": value, **_result_dict(result)})
    content = _format_batch(items, args.format)
    _emit(content, args.output)
    return 0 if items and all(item["text"] for item in items) else 2


def _format_batch(items: list[dict[str, Any]], output_format: str) -> str:
    """Render all batch results without dropping extracted text."""
    if output_format == "json":
        return json.dumps(items, ensure_ascii=False, indent=2)
    if output_format == "md":
        sections = []
        for item in items:
            sections.append(
                f"## {item['input']}\n\n- Score: {item['score']:.3f}\n"
                f"- URL: {item['url'] or ''}\n\n{item['text']}"
            )
        return "# Batch results\n\n" + "\n\n".join(sections)
    return "\n\n=====\n\n".join(item["text"] for item in items)


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(description="UniversalExtractor closed-loop scraper")
    commands = parser.add_subparsers(dest="command", required=True)
    specs = {
        "run": ("query", cmd_run),
        "search": ("query", cmd_search),
        "extract": ("url", cmd_extract),
        "batch": ("file", cmd_batch),
    }
    for name, (argument, handler) in specs.items():
        command = commands.add_parser(name)
        command.add_argument(argument)
        command.add_argument("--headless", action="store_true", default=True)
        command.add_argument("--no-headless", dest="headless", action="store_false")
        command.add_argument("--output", "-o")
        command.add_argument("--format", choices=("json", "text", "md"), default="text")
        command.add_argument("--stages", type=int, choices=range(1, 8))
        command.add_argument("--min-score", type=float, default=0.5)
        command.add_argument("--max-candidates", type=int, default=10)
        command.set_defaults(handler=handler)
    commands.choices["run"].add_argument("--cross-validate", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute the CLI and return a process exit code."""
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
