"""
CLI entry point: python -m UniversalExtractor <command>

Commands:
  run     <query>          Full closed-loop pipeline (default)
  search  <query>          Search only
  extract <url>            Extract from URL only
  batch   <file>           Batch process (one query/URL per line)

Options:
  --headless / --no-headless   Browser mode
  --output <path>              Output file
  --format json|text           Output format (default: text)
  --stages <n>                 Max stages to use (default: all 7)
  --min-score <0-1>            Minimum completeness score (default: 0.5)
  --cross-validate             Enable cross-source validation
  --max-candidates <n>         Max candidate URLs to scan (default: 10)
"""

from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def cmd_run(args):
    """Full closed-loop: search -> verify -> extract -> validate."""
    from UniversalExtractor.pipeline import Pipeline, PipelineConfig

    config = PipelineConfig(
        headless=args.headless,
        min_completeness=args.min_score,
        max_candidates=args.max_candidates,
        enable_cross_validation=args.cross_validate,
    )
    if args.stages:
        # Enable only first N stages
        from UniversalExtractor.pipeline import StageRegistry
        reg = StageRegistry()
        reg.register_defaults()
        all_names = [s.stage_name for s in reg.get_chain()]
        config.enabled_stages = all_names[:args.stages]

    pipeline = Pipeline(config)
    print(f"[Pipeline] Running: {args.query}")
    result = pipeline.run(query=args.query)

    if args.format == "json":
        _output_json(result, args)
    else:
        _output_text(result, args)


def cmd_search(args):
    """Search only — return URLs."""
    from UniversalExtractor.search import search_with_metadata

    meta = search_with_metadata(args.query, max_results=args.max_candidates)
    if args.format == "json":
        print(json.dumps(meta, ensure_ascii=False, indent=2))
    else:
        print(f"Backends: {meta['backends_used']}")
        print(f"Results: {meta['total_raw']} raw → {meta['total_unique']} unique\n")
        for i, item in enumerate(meta["results"], 1):
            print(f"  [{i}] {item['url'][:100]}")
            print(f"      backends: {item['backends']}  (cross_hits={item['cross_hits']})")


def cmd_extract(args):
    """Extract from a single URL."""
    from UniversalExtractor.pipeline import Pipeline, PipelineConfig

    config = PipelineConfig(
        headless=args.headless,
        min_completeness=args.min_score,
    )
    if args.stages:
        from UniversalExtractor.pipeline import StageRegistry
        reg = StageRegistry()
        reg.register_defaults()
        all_names = [s.stage_name for s in reg.get_chain()]
        config.enabled_stages = all_names[:args.stages]

    pipeline = Pipeline(config)
    print(f"[Pipeline] Extracting: {args.url}")
    result = pipeline.run(url=args.url, mode="extract_only")

    if args.format == "json":
        _output_json(result, args)
    else:
        _output_text(result, args)


def cmd_batch(args):
    """Batch process a file."""
    input_path = Path(args.file)
    if not input_path.exists():
        print(f"Error: file not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    from UniversalExtractor.pipeline import Pipeline, PipelineConfig

    config = PipelineConfig(
        headless=args.headless,
        min_completeness=args.min_score,
        max_candidates=args.max_candidates,
    )
    pipeline = Pipeline(config)

    lines = input_path.read_text(encoding="utf-8").strip().split("\n")
    results = []
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        print(f"\n[{i}/{len(lines)}] {line[:80]}...")
        if line.startswith("http://") or line.startswith("https://"):
            result = pipeline.run(url=line, mode="extract_only")
        else:
            result = pipeline.run(query=line)

        results.append({
            "input": line,
            "success": result.success,
            "score": result.score,
            "url": result.url,
            "chars": len(result.text),
            "stage": result.winning_stage,
        })

    # Summary
    ok = sum(1 for r in results if r["success"])
    print(f"\nBatch complete: {ok}/{len(results)} succeeded")

    if args.output:
        # Write all results to output
        out_path = Path(args.output)
        out_path.write_text(
            "\n\n=====\n\n".join(
                r.get("text", "") if isinstance(r, dict) else ""
                for r in results
            ),
            encoding="utf-8",
        )
        print(f"Output written to: {out_path}")


def _output_text(result, args):
    """Output PipelineResult as text."""
    print(f"\nSuccess: {result.success}")
    print(f"Score: {result.score:.2f}")
    print(f"URL: {result.url}")
    print(f"Method: {result.winning_stage or 'none'}")
    print(f"Stages: {result.stages_attempted} attempted, {result.stages_succeeded} succeeded")
    print(f"Time: {result.total_time_ms}ms")
    print(f"Validation: {result.validation_details.get('result', 'N/A')}")

    if result.text:
        if args.output:
            Path(args.output).write_text(result.text, encoding="utf-8")
            print(f"\nOutput written to: {args.output}")
        else:
            print(f"\n--- Content ({len(result.text)} chars) ---")
            preview = result.text[:2000]
            if len(result.text) > 2000:
                preview += f"\n... ({len(result.text) - 2000} more chars)"
            print(preview)


def _output_json(result, args):
    """Output PipelineResult as JSON."""
    data = {
        "success": result.success,
        "score": result.score,
        "url": result.url,
        "text": result.text[:args.max_chars] if hasattr(args, 'max_chars') else result.text,
        "winning_stage": result.winning_stage,
        "stages_attempted": result.stages_attempted,
        "stages_succeeded": result.stages_succeeded,
        "total_time_ms": result.total_time_ms,
        "search_candidates_total": result.search_candidates_total,
        "search_candidates_scanned": result.search_candidates_scanned,
        "validation": result.validation_details,
    }
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(json_str, encoding="utf-8")
        print(f"Output written to: {args.output}")
    else:
        print(json_str)


def main():
    parser = argparse.ArgumentParser(
        description="UniversalExtractor — closed-loop web scraping",
        usage="python -m UniversalExtractor <command> [options]",
    )
    sub = parser.add_subparsers(dest="command", help="Command")

    # run
    p_run = sub.add_parser("run", help="Full pipeline: search + extract + validate")
    p_run.add_argument("query", help="Search query")
    p_run.set_defaults(func=cmd_run)

    # search
    p_search = sub.add_parser("search", help="Search only")
    p_search.add_argument("query", help="Search query")
    p_search.set_defaults(func=cmd_search)

    # extract
    p_extract = sub.add_parser("extract", help="Extract from URL only")
    p_extract.add_argument("url", help="Target URL")
    p_extract.set_defaults(func=cmd_extract)

    # batch
    p_batch = sub.add_parser("batch", help="Batch process from file")
    p_batch.add_argument("file", help="Input file (one query/URL per line)")
    p_batch.set_defaults(func=cmd_batch)

    # Common options
    for p in [p_run, p_search, p_extract, p_batch]:
        p.add_argument("--headless", action="store_true", default=True,
                       help="Headless browser (default)")
        p.add_argument("--no-headless", dest="headless", action="store_false",
                       help="Show browser window")
        p.add_argument("--output", "-o", help="Output file path")
        p.add_argument("--format", choices=["json", "text"], default="text",
                       help="Output format")
        p.add_argument("--stages", type=int, default=0,
                       help="Max extraction stages (0=all)")
        p.add_argument("--min-score", type=float, default=0.5,
                       help="Minimum completeness score")
        p.add_argument("--max-candidates", type=int, default=10,
                       help="Max candidate URLs to scan")

    p_run.add_argument("--cross-validate", action="store_true",
                       help="Enable cross-source validation")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
