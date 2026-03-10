from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lua_nil_review.symbol_query import jump_to_definition
from lua_nil_review.tracer import trace_finding
from lua_nil_review.workflow import claim_next_shard, complete_shard, load_status_snapshot, run_analyze, run_merge, run_prepare_shards


def common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".")
    parser.add_argument("--state-dir", default="artifacts/string-find-nil")
    parser.add_argument("--config")


def progress_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--progress", action="store_true", help="Print progress updates to stderr while work is running.")
    parser.add_argument("--progress-interval", type=float, default=1.0, help="Progress poll interval in seconds.")


def _format_progress_line(status: dict[str, object]) -> str:
    stage = status.get("stage") or "idle"
    files_done = int(status.get("files_done") or 0)
    files_total = int(status.get("files_total") or 0)
    if stage == "analyzing":
        analyze = status.get("analyze_progress")
        if isinstance(analyze, dict):
            analyzed = int(analyze.get("analyzed_files") or 0)
            reused = int(analyze.get("reused_files") or 0)
            findings = int(analyze.get("findings_discovered") or 0)
            files_with_findings = int(analyze.get("files_with_findings") or 0)
            parse_errors = int(analyze.get("parse_errors") or 0)
            current_file = analyze.get("current_file")
            current_status = analyze.get("current_status")
            current = f" current={current_file}({current_status})" if current_file and current_status else ""
            latest = ""
            recent = analyze.get("recent_findings")
            if isinstance(recent, list) and recent:
                last = recent[-1]
                if isinstance(last, dict):
                    latest = f" latest={last.get('file')}:{last.get('line')}"
            return (
                f"[lua-nil-review] stage=analyzing files={files_done}/{files_total} "
                f"analyzed={analyzed} reused={reused} findings={findings} "
                f"hit_files={files_with_findings} parse_errors={parse_errors}{current}{latest}"
            )
        return f"[lua-nil-review] stage=analyzing files={files_done}/{files_total}"
    prepare = status.get("prepare_progress")
    if isinstance(prepare, dict):
        phase = prepare.get("phase")
        if stage == "sharding" or phase in {"trace_enrichment", "building_shards", "completed"}:
            findings_done = int(prepare.get("findings_done") or 0)
            findings_total = int(prepare.get("findings_total") or 0)
            trace_done = int(prepare.get("trace_candidates_done") or 0)
            trace_total = int(prepare.get("trace_candidates_total") or 0)
            visible = int(prepare.get("visible_after_trace") or 0)
            shards_built = int(prepare.get("shards_built") or 0)
            current_file = prepare.get("current_file")
            current_line = prepare.get("current_line")
            current = f" current={current_file}:{current_line}" if current_file and current_line else ""
            current_summary = prepare.get("current_candidate_summary")
            summary_suffix = ""
            if isinstance(current_summary, str) and current_summary:
                compact = current_summary.strip()
                if len(compact) > 84:
                    compact = compact[:81] + "..."
                summary_suffix = f" note={compact}"
            trace_summary = status.get("trace_summary")
            live = ""
            if isinstance(trace_summary, dict) and trace_summary:
                traced = int(trace_summary.get("traced") or 0)
                silenced = int(trace_summary.get("auto_silenced") or 0)
                live = f" traced={traced} safe={silenced}"
            return (
                f"[lua-nil-review] stage={stage} phase={phase} "
                f"findings={findings_done}/{findings_total} traces={trace_done}/{trace_total} "
                f"visible={visible} shards={shards_built}{live}{current}{summary_suffix}"
            )
    if stage == "sharded":
        overview = status.get("candidate_overview")
        if isinstance(overview, dict) and overview:
            visible = int(overview.get("visible_findings") or 0)
            uncertain = int(overview.get("uncertain_findings") or 0)
            top_paths = overview.get("top_candidate_paths") or []
            top_hint = f" top={top_paths[0]}" if isinstance(top_paths, list) and top_paths else ""
            return (
                f"[lua-nil-review] stage=sharded shards={int(status.get('shards_total') or 0)} "
                f"visible={visible} uncertain={uncertain}{top_hint}"
            )
        return f"[lua-nil-review] stage=sharded shards={int(status.get('shards_total') or 0)}"
    if stage == "reviewing":
        return f"[lua-nil-review] stage=reviewing shards={int(status.get('shards_total') or 0)} reviewed={int(status.get('shards_reviewed') or 0)}"
    return f"[lua-nil-review] stage={stage}"


def _start_progress_monitor(*, root: Path, state_dir: Path, enabled: bool, interval: float) -> tuple[threading.Event | None, threading.Thread | None]:
    if not enabled:
        return None, None
    stop = threading.Event()

    def run() -> None:
        last_line = ""
        while not stop.is_set():
            try:
                status = load_status_snapshot(root=root, state_dir=state_dir)
                line = _format_progress_line(status)
                if line != last_line:
                    print(line, file=sys.stderr, flush=True)
                    last_line = line
            except Exception:
                pass
            stop.wait(max(interval, 0.1))

    thread = threading.Thread(target=run, name="lua-nil-review-progress", daemon=True)
    thread.start()
    return stop, thread


def _stop_progress_monitor(stop: threading.Event | None, thread: threading.Thread | None, *, root: Path, state_dir: Path, enabled: bool) -> None:
    if not enabled:
        return
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout=1.0)
    try:
        status = load_status_snapshot(root=root, state_dir=state_dir)
        print(_format_progress_line(status), file=sys.stderr, flush=True)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="High-level wrapper for the persisted Lua nil review workflow.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh = subparsers.add_parser("refresh", help="Run analyze and prepare shards.")
    common_args(refresh)
    progress_args(refresh)

    claim = subparsers.add_parser("claim", help="Refresh state, then claim one review shard.")
    common_args(claim)
    progress_args(claim)

    complete = subparsers.add_parser("complete", help="Complete one shard review and merge the latest summary.")
    common_args(complete)
    complete.add_argument("--review-json", required=True)
    complete.add_argument("--skip-merge", action="store_true")

    merge = subparsers.add_parser("merge", help="Merge reviewed shards into final outputs.")
    common_args(merge)

    status = subparsers.add_parser("status", help="Show the current persisted workflow status.")
    common_args(status)

    build_symbol_index = subparsers.add_parser("build-symbol-index", help="Refresh analysis artifacts and rebuild the symbol index.")
    common_args(build_symbol_index)

    jump = subparsers.add_parser("jump", help="Resolve a logical symbol or callsite expression to definition slices.")
    common_args(jump)
    jump.add_argument("--symbol")
    jump.add_argument("--file")
    jump.add_argument("--line", type=int)
    jump.add_argument("--expr")
    jump.add_argument("--include-all", action="store_true")
    jump.add_argument("--expand-token")

    trace = subparsers.add_parser("trace", help="Trace one finding through bounded symbol and return dependencies.")
    common_args(trace)
    trace.add_argument("--finding-id")
    trace.add_argument("--file")
    trace.add_argument("--line", type=int)
    trace.add_argument("--expr")
    trace.add_argument("--expand-node")

    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    state_dir = Path(args.state_dir)

    if args.command == "refresh":
        stop, thread = _start_progress_monitor(
            root=root,
            state_dir=state_dir,
            enabled=bool(args.progress),
            interval=float(args.progress_interval),
        )
        try:
            analyze_result = run_analyze(root=root, config_path=args.config, state_dir=state_dir, resume=True)
            prepare_result = run_prepare_shards(root=root, state_dir=state_dir, resume=True, config_path=args.config)
        finally:
            _stop_progress_monitor(stop, thread, root=root, state_dir=state_dir, enabled=bool(args.progress))
        print(json.dumps({"analyze": analyze_result, "prepare": prepare_result}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "claim":
        stop, thread = _start_progress_monitor(
            root=root,
            state_dir=state_dir,
            enabled=bool(args.progress),
            interval=float(args.progress_interval),
        )
        try:
            analyze_result = run_analyze(root=root, config_path=args.config, state_dir=state_dir, resume=True)
            prepare_result = run_prepare_shards(root=root, state_dir=state_dir, resume=True, config_path=args.config)
        finally:
            _stop_progress_monitor(stop, thread, root=root, state_dir=state_dir, enabled=bool(args.progress))
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

    if args.command == "status":
        print(json.dumps({"status": load_status_snapshot(root=root, state_dir=state_dir)}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "build-symbol-index":
        analyze_result = run_analyze(root=root, config_path=args.config, state_dir=state_dir, resume=True)
        print(json.dumps({"build_symbol_index": analyze_result.get("symbol_index", {}), "symbol_fingerprint": analyze_result.get("symbol_fingerprint")}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "jump":
        from lua_nil_review.config import load_config

        config, _ = load_config(root, args.config)
        jump_result = jump_to_definition(
            root=root,
            state_dir=state_dir,
            symbol=args.symbol,
            file=args.file,
            line=args.line,
            expression=args.expr,
            config=config,
            include_all=args.include_all,
            expand_token=args.expand_token,
        )
        print(json.dumps({"jump": jump_result}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "trace":
        if not args.finding_id and not (args.file and args.line is not None and args.expr):
            parser.error("trace requires --finding-id or --file/--line/--expr")
            return 2
        trace_result = trace_finding(
            root=root,
            state_dir=state_dir,
            finding_id=args.finding_id or "",
            config_path=args.config,
            file=args.file,
            line=args.line,
            expression=args.expr,
            expand_node=args.expand_node,
        )
        print(json.dumps({"trace": trace_result}, ensure_ascii=False, indent=2))
        return 0

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
