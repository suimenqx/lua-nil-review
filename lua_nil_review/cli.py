from __future__ import annotations

import argparse
import json
from pathlib import Path

from .symbol_query import jump_to_definition
from .tracer import trace_finding
from .workflow import claim_next_shard, complete_shard, heartbeat_shard, run_analyze, run_merge, run_prepare_shards


def analyze_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze Lua files for string.find nil risks.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--config")
    parser.add_argument("--state-dir", default="artifacts/string-find-nil")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    result = run_analyze(root=Path(args.root).resolve(), config_path=args.config, state_dir=Path(args.state_dir), resume=args.resume)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare review shards from persisted findings.")
    parser.add_argument("--state-dir", default="artifacts/string-find-nil")
    parser.add_argument("--root", default=".")
    parser.add_argument("--config")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    result = run_prepare_shards(root=Path(args.root).resolve(), state_dir=Path(args.state_dir), resume=args.resume, config_path=args.config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def review_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claim, heartbeat, or complete a review shard.")
    parser.add_argument("--state-dir", default="artifacts/string-find-nil")
    parser.add_argument("--root", default=".")
    parser.add_argument("--claim-next", action="store_true")
    parser.add_argument("--heartbeat")
    parser.add_argument("--complete")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    state_dir = Path(args.state_dir)
    if args.claim_next:
        result = claim_next_shard(root=root, state_dir=state_dir)
    elif args.heartbeat:
        result = heartbeat_shard(root=root, state_dir=state_dir, shard_id=args.heartbeat)
    elif args.complete:
        result = complete_shard(root=root, state_dir=state_dir, review_path=Path(args.complete))
    else:
        parser.error("Choose one of --claim-next, --heartbeat, or --complete.")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def merge_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge reviewed shards into final Markdown and JSON reports.")
    parser.add_argument("--state-dir", default="artifacts/string-find-nil")
    parser.add_argument("--root", default=".")
    parser.add_argument("--config")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    result = run_merge(root=Path(args.root).resolve(), state_dir=Path(args.state_dir), config_path=args.config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def jump_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve a Lua symbol or callsite to one or more function definitions.")
    parser.add_argument("--state-dir", default="artifacts/string-find-nil")
    parser.add_argument("--root", default=".")
    parser.add_argument("--config")
    parser.add_argument("--symbol")
    parser.add_argument("--file")
    parser.add_argument("--line", type=int)
    parser.add_argument("--expr")
    parser.add_argument("--include-all", action="store_true")
    parser.add_argument("--expand-token")
    args = parser.parse_args(argv)
    from .config import load_config

    config, _ = load_config(Path(args.root).resolve(), args.config)
    result = jump_to_definition(
        root=Path(args.root).resolve(),
        state_dir=Path(args.state_dir),
        symbol=args.symbol,
        file=args.file,
        line=args.line,
        expression=args.expr,
        config=config,
        include_all=args.include_all,
        expand_token=args.expand_token,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def trace_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trace one finding across bounded symbol and return dependencies.")
    parser.add_argument("--state-dir", default="artifacts/string-find-nil")
    parser.add_argument("--root", default=".")
    parser.add_argument("--config")
    parser.add_argument("--finding-id")
    parser.add_argument("--file")
    parser.add_argument("--line", type=int)
    parser.add_argument("--expr")
    parser.add_argument("--expand-node")
    args = parser.parse_args(argv)
    if not args.finding_id and not (args.file and args.line is not None and args.expr):
        parser.error("Provide --finding-id or --file/--line/--expr.")
        return 2
    result = trace_finding(
        root=Path(args.root).resolve(),
        state_dir=Path(args.state_dir),
        finding_id=args.finding_id or "",
        config_path=args.config,
        file=args.file,
        line=args.line,
        expression=args.expr,
        expand_node=args.expand_node,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_symbol_index_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or refresh the persisted symbol index.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--config")
    parser.add_argument("--state-dir", default="artifacts/string-find-nil")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    result = run_analyze(root=Path(args.root).resolve(), config_path=args.config, state_dir=Path(args.state_dir), resume=args.resume)
    print(json.dumps({"symbol_index": result.get("symbol_index", {}), "symbol_fingerprint": result.get("symbol_fingerprint")}, ensure_ascii=False, indent=2))
    return 0
