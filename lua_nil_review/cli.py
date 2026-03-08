from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    result = run_prepare_shards(root=Path(args.root).resolve(), state_dir=Path(args.state_dir), resume=args.resume)
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
