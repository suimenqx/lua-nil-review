from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lua_nil_review.workflow import claim_next_shard, complete_shard, run_analyze, run_merge, run_prepare_shards


def common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".")
    parser.add_argument("--state-dir", default="artifacts/string-find-nil")
    parser.add_argument("--config")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="High-level wrapper for the persisted Lua nil review workflow.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh = subparsers.add_parser("refresh", help="Run analyze and prepare shards.")
    common_args(refresh)

    claim = subparsers.add_parser("claim", help="Refresh state, then claim one review shard.")
    common_args(claim)

    complete = subparsers.add_parser("complete", help="Complete one shard review and merge the latest summary.")
    common_args(complete)
    complete.add_argument("--review-json", required=True)
    complete.add_argument("--skip-merge", action="store_true")

    merge = subparsers.add_parser("merge", help="Merge reviewed shards into final outputs.")
    common_args(merge)

    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    state_dir = Path(args.state_dir)

    if args.command == "refresh":
        analyze_result = run_analyze(root=root, config_path=args.config, state_dir=state_dir, resume=True)
        prepare_result = run_prepare_shards(root=root, state_dir=state_dir, resume=True)
        print(json.dumps({"analyze": analyze_result, "prepare": prepare_result}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "claim":
        analyze_result = run_analyze(root=root, config_path=args.config, state_dir=state_dir, resume=True)
        prepare_result = run_prepare_shards(root=root, state_dir=state_dir, resume=True)
        claim_result = claim_next_shard(root=root, state_dir=state_dir)
        print(json.dumps({"analyze": analyze_result, "prepare": prepare_result, "claim": claim_result}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "complete":
        complete_result = complete_shard(root=root, state_dir=state_dir, review_path=Path(args.review_json))
        if args.skip_merge:
            print(json.dumps({"complete": complete_result}, ensure_ascii=False, indent=2))
            return 0
        merge_result = run_merge(root=root, state_dir=state_dir, config_path=args.config)
        print(json.dumps({"complete": complete_result, "merge": merge_result}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "merge":
        merge_result = run_merge(root=root, state_dir=state_dir, config_path=args.config)
        print(json.dumps({"merge": merge_result}, ensure_ascii=False, indent=2))
        return 0

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
