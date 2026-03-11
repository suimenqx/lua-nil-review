from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .analyzer import analyze_lua_file
from .common import (
    ANALYZER_VERSION,
    PARSER_VERSION,
    RULE_ID,
    RULE_VERSION,
    SYMBOL_INDEX_VERSION,
    atomic_write_text,
    SHARD_MAX_BYTES,
    SHARD_MAX_FINDINGS,
    atomic_write_json,
    atomic_write_jsonl,
    is_stale,
    load_json,
    load_jsonl,
    normalize_whitespace,
    rel_posix,
    sha1_hex,
    sha256_bytes,
    sha256_hex,
    utc_now,
)
from .config import ReviewConfig, load_config
from .parsed_lua import parse_lua_file
from .state import (
    acquire_lock,
    build_layout,
    default_analyze_progress,
    default_prepare_progress,
    load_files_index,
    load_or_rebuild_manifest,
    release_lock,
    reset_outputs_for_new_fingerprint,
    save_files_index,
    save_manifest,
    touch_lock,
)
from .symbol_extractor import extract_file_symbols
from .symbol_index import build_symbol_index, load_file_symbols_by_artifact
from .symbol_query import candidate_slice_content
from .tracer import _trace_finding_with_strategy, load_trace_bundle


def analysis_fingerprint(config: ReviewConfig) -> str:
    return sha256_hex("|".join([RULE_VERSION, ANALYZER_VERSION, PARSER_VERSION, config.fingerprint()]))


def symbol_fingerprint(config: ReviewConfig) -> str:
    return sha256_hex("|".join([SYMBOL_INDEX_VERSION, PARSER_VERSION, config.fingerprint()]))


def _finding_preview_item(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "finding_id": finding.get("finding_id"),
        "file": finding.get("file"),
        "line": finding.get("line"),
        "message": finding.get("message"),
        "risk_level": finding.get("risk_level"),
        "risk_tier": finding.get("risk_tier"),
        "risk_category": finding.get("risk_category"),
        "nil_state": finding.get("nil_state"),
    }


def _extend_recent_findings(progress: dict[str, Any], findings: list[dict[str, Any]], *, limit: int = 5) -> None:
    recent = list(progress.get("recent_findings", []))
    for finding in findings[:2]:
        recent.append(_finding_preview_item(finding))
    progress["recent_findings"] = recent[-limit:]


def discover_lua_files(root: Path, config: ReviewConfig, state_dir: Path) -> list[Path]:
    files: list[Path] = []
    state_prefix = str(state_dir.resolve())
    for path in root.rglob("*.lua"):
        if not path.is_file():
            continue
        if str(path.resolve()).startswith(state_prefix):
            continue
        relative = rel_posix(path, root)
        if config.matches(relative):
            files.append(path)
    return sorted(files, key=lambda item: rel_posix(item, root))


def run_analyze(*, root: Path, config_path: str | None, state_dir: Path, resume: bool) -> dict[str, Any]:
    config, loaded_config_path = load_config(root, config_path)
    layout = build_layout(root, state_dir)
    owner = acquire_lock(layout, "analyze")
    try:
        manifest = load_or_rebuild_manifest(layout)
        fingerprint = analysis_fingerprint(config)
        symbols_fp = symbol_fingerprint(config)
        previous_index = load_files_index(layout) if resume else {}
        if (
            not resume
            or manifest.get("analysis_fingerprint") != fingerprint
            or manifest.get("symbol_fingerprint") != symbols_fp
        ):
            reset_outputs_for_new_fingerprint(layout)
            manifest = load_or_rebuild_manifest(layout)
            previous_index = {}
        manifest["analysis_fingerprint"] = fingerprint
        manifest["symbol_fingerprint"] = symbols_fp
        manifest["config_path"] = str(loaded_config_path.resolve()) if loaded_config_path else None
        manifest["stage"] = "analyzing"
        manifest["lock_owner"] = owner
        manifest["trace_summary"] = {}
        manifest["analyze_progress"] = default_analyze_progress()
        manifest["prepare_progress"] = default_prepare_progress()
        manifest["candidate_overview"] = {}
        manifest["finding_preview"] = []
        save_manifest(layout, manifest)

        current_paths = discover_lua_files(root, config, layout.state_dir)
        entries: list[dict[str, Any]] = []
        symbol_docs = []
        current_ids = set()
        manifest["files_total"] = len(current_paths)
        manifest["files_done"] = 0
        analyze_progress = default_analyze_progress()
        analyze_progress["phase"] = "scanning_files"
        analyze_progress["files_total"] = len(current_paths)
        manifest["analyze_progress"] = analyze_progress
        save_manifest(layout, manifest)

        for index, path in enumerate(current_paths, start=1):
            relative = rel_posix(path, root)
            file_id = sha1_hex(relative)
            current_ids.add(file_id)
            content = path.read_bytes()
            content_hash = sha256_bytes(content)
            analysis_path = layout.analysis_dir / f"{file_id}.json"
            symbol_path = layout.symbol_files_dir / f"{file_id}.json"
            previous = previous_index.get(file_id)
            reusable = (
                resume
                and previous is not None
                and previous.get("content_hash") == content_hash
                and previous.get("analysis_fingerprint") == fingerprint
                and previous.get("symbol_fingerprint") == symbols_fp
                and analysis_path.exists()
                and symbol_path.exists()
            )
            if reusable:
                analysis_doc = load_json(analysis_path, default={})
                symbol_doc = load_file_symbols_by_artifact(layout, previous.get("symbol_artifact_path"))
                analysis_status = "reused"
                symbol_status = "reused"
                finding_count = len(analysis_doc.get("findings", []))
                suppressed_findings = int(analysis_doc.get("suppressed_findings", 0))
                parse_status = analysis_doc.get("parse_status", "ok")
                parse_error = analysis_doc.get("parse_error")
                symbol_parse_status = symbol_doc.parse_status if symbol_doc is not None else "error"
                findings_doc = list(analysis_doc.get("findings", []))
            else:
                decoded = content.decode("utf-8", errors="replace")
                parsed = None
                try:
                    parsed = parse_lua_file(relative, decoded)
                except Exception:
                    parsed = None
                result = analyze_lua_file(
                    relative,
                    decoded,
                    config,
                    file_id=file_id,
                    content_hash=content_hash,
                    analysis_fingerprint=fingerprint,
                    snippets_dir=layout.snippets_dir,
                    parsed_file=parsed,
                )
                atomic_write_json(analysis_path, result.to_dict())
                symbol_doc = extract_file_symbols(
                    relative,
                    decoded,
                    config,
                    file_id=file_id,
                    content_hash=content_hash,
                    symbol_fingerprint=symbols_fp,
                    parsed_file=parsed,
                )
                atomic_write_json(symbol_path, symbol_doc.to_dict())
                analysis_status = "analyzed"
                symbol_status = "analyzed"
                finding_count = len(result.findings)
                suppressed_findings = result.suppressed_findings
                parse_status = result.parse_status
                parse_error = result.parse_error
                symbol_parse_status = symbol_doc.parse_status
                findings_doc = list(result.findings)
            if symbol_doc is None:
                symbol_doc = extract_file_symbols(
                    relative,
                    content.decode("utf-8", errors="replace"),
                    config,
                    file_id=file_id,
                    content_hash=content_hash,
                    symbol_fingerprint=symbols_fp,
                )
                atomic_write_json(symbol_path, symbol_doc.to_dict())
                symbol_status = "analyzed"
                symbol_parse_status = symbol_doc.parse_status
            entry = {
                "analysis_fingerprint": fingerprint,
                "analysis_path": analysis_path.relative_to(layout.state_dir).as_posix(),
                "analysis_status": analysis_status,
                "content_hash": content_hash,
                "file": relative,
                "file_id": file_id,
                "finding_count": finding_count,
                "parse_error": parse_error,
                "parse_status": parse_status,
                "suppressed_findings": suppressed_findings,
                "symbol_artifact_path": symbol_path.relative_to(layout.state_dir).as_posix(),
                "symbol_fingerprint": symbols_fp,
                "symbol_parse_status": symbol_parse_status,
                "symbol_status": symbol_status,
            }
            entries.append(entry)
            symbol_docs.append(symbol_doc)
            save_files_index(layout, entries)
            manifest["files_done"] = index
            analyze_progress["files_done"] = index
            analyze_progress["current_file"] = relative
            analyze_progress["current_status"] = analysis_status
            analyze_progress["current_findings_in_file"] = finding_count
            if analysis_status == "reused":
                analyze_progress["reused_files"] = int(analyze_progress.get("reused_files", 0)) + 1
            else:
                analyze_progress["analyzed_files"] = int(analyze_progress.get("analyzed_files", 0)) + 1
            if finding_count:
                analyze_progress["files_with_findings"] = int(analyze_progress.get("files_with_findings", 0)) + 1
                analyze_progress["findings_discovered"] = int(analyze_progress.get("findings_discovered", 0)) + finding_count
                analyze_progress["suppressed_findings"] = int(analyze_progress.get("suppressed_findings", 0)) + suppressed_findings
                _extend_recent_findings(analyze_progress, findings_doc)
            if parse_status != "ok":
                analyze_progress["parse_errors"] = int(analyze_progress.get("parse_errors", 0)) + 1
            manifest["analyze_progress"] = analyze_progress
            touch_lock(layout, owner)
            save_manifest(layout, manifest)

        for file_id, previous in previous_index.items():
            if file_id in current_ids:
                continue
            analysis_path = layout.state_dir / previous.get("analysis_path", "")
            if analysis_path.exists():
                analysis_path.unlink()
            symbol_artifact = layout.state_dir / previous.get("symbol_artifact_path", "")
            if symbol_artifact.exists():
                symbol_artifact.unlink()

        symbol_summary = build_symbol_index(layout, symbol_docs, symbol_fingerprint=symbols_fp)
        manifest["stage"] = "analyzed"
        manifest["files_total"] = len(entries)
        manifest["files_done"] = len(entries)
        manifest["symbol_index"] = symbol_summary
        analyze_progress["phase"] = "completed"
        analyze_progress["files_total"] = len(entries)
        analyze_progress["files_done"] = len(entries)
        analyze_progress["current_file"] = None
        analyze_progress["current_status"] = None
        analyze_progress["current_findings_in_file"] = 0
        manifest["analyze_progress"] = analyze_progress
        save_files_index(layout, entries)
        save_manifest(layout, manifest)
        return {
            "files_total": len(entries),
            "analysis_fingerprint": fingerprint,
            "symbol_fingerprint": symbols_fp,
            "symbol_index": symbol_summary,
        }
    finally:
        release_lock(layout, owner)


def _active_findings(layout) -> tuple[list[dict[str, Any]], int]:
    entries = load_jsonl(layout.files_index_path)
    active: list[dict[str, Any]] = []
    suppressed = 0
    for entry in entries:
        analysis_doc = load_json(layout.state_dir / entry["analysis_path"], default={})
        for finding in analysis_doc.get("findings", []):
            if finding.get("suppressed") or finding.get("trace_auto_silenced") or finding.get("human_review_visible") is False:
                suppressed += 1
                continue
            active.append(finding)
    active.sort(key=lambda item: (item["file"], item["line"], item["finding_id"]))
    return active, suppressed


def _shard_byte_size(layout, findings: list[dict[str, Any]]) -> int:
    total = 0
    for finding in findings:
        total += len(json.dumps(finding, ensure_ascii=False))
        for relative in finding.get("snippet_paths", []):
            path = layout.state_dir / relative
            if path.exists():
                total += len(path.read_text(encoding="utf-8"))
    return total


def _load_prepare_config(root: Path, manifest: dict[str, Any], explicit_config_path: str | None) -> ReviewConfig:
    config_path = explicit_config_path or manifest.get("config_path")
    config, _ = load_config(root, config_path)
    return config


def _count_prepare_candidates(layout) -> tuple[int, int]:
    findings_total = 0
    trace_candidates_total = 0
    for analysis_path in sorted(layout.analysis_dir.glob("*.json")):
        analysis_doc = load_json(analysis_path, default={})
        for finding in analysis_doc.get("findings", []):
            if finding.get("suppressed"):
                continue
            findings_total += 1
            if finding.get("nil_state") != "nil":
                trace_candidates_total += 1
    return findings_total, trace_candidates_total


def _save_prepare_progress(layout, manifest: dict[str, Any], owner: str, progress: dict[str, Any]) -> None:
    manifest["prepare_progress"] = progress
    touch_lock(layout, owner)
    save_manifest(layout, manifest)


def _initial_finding_summary(finding: dict[str, Any]) -> dict[str, Any]:
    if finding.get("suppressed"):
        return {
            "candidate_summary": "Finding was suppressed before agentic investigation; no caller or callee candidates were scanned.",
            "candidate_count": 0,
            "top_candidate_paths": [],
            "scenario_branches": [
                {
                    "source": "suppression",
                    "status": "suppressed",
                    "file": finding.get("file"),
                    "line": finding.get("line"),
                    "expression": finding.get("arg_text"),
                    "summary": "Suppression matched before tracing started.",
                }
            ],
            "why_still_uncertain": None,
            "investigation_leads": [],
        }
    return {
        "candidate_summary": "Local flow already proves a nil value reaches the sink; no caller or callee candidates were needed.",
        "candidate_count": 0,
        "top_candidate_paths": [],
        "scenario_branches": [
            {
                "source": "local_flow",
                "status": "risky",
                "file": finding.get("file"),
                "line": finding.get("line"),
                "expression": finding.get("arg_text"),
                "summary": "Local analysis proved a nil value reaches the sink before cross-function tracing was needed.",
            }
        ],
        "why_still_uncertain": None,
        "investigation_leads": [],
    }


def _frontier_jump_branch_status(jump: dict[str, Any]) -> str:
    statuses = []
    for candidate in jump.get("candidates", []):
        return_state = candidate.get("return_state")
        if return_state == "always_non_nil":
            statuses.append("safe")
        elif return_state == "always_nil":
            statuses.append("risky")
        else:
            statuses.append("uncertain")
    normalized = {item for item in statuses if item}
    if not normalized:
        return "uncertain"
    if normalized == {"safe"}:
        return "safe"
    if normalized == {"risky"}:
        return "risky"
    if "safe" in normalized and "risky" in normalized:
        return "mixed"
    return "uncertain"


def _frontier_jump_summary(jump: dict[str, Any]) -> str:
    expression = jump.get("expression") or "unknown call"
    candidate_count = int(jump.get("candidate_count", 0))
    if candidate_count <= 0:
        return f"Frontier jump for '{expression}' found no concrete definition candidates."
    return f"Frontier jump for '{expression}' found {candidate_count} candidate definition(s)."


def _why_still_uncertain(bundle: dict[str, Any], scenario_branches: list[dict[str, Any]]) -> str | None:
    overall = bundle.get("overall")
    if overall not in {"uncertain", "budget_exhausted"}:
        return None
    reasons: list[str] = []
    if bundle.get("budget", {}).get("budget_exhausted"):
        reasons.append("trace budget exhausted before all branches were resolved")
    if bundle.get("external_config_dependency"):
        reasons.append("module resolution still depends on packaging priority or override selection")
    for branch in scenario_branches:
        summary = branch.get("summary")
        if isinstance(summary, str) and summary:
            reasons.append(summary)
    for lead in bundle.get("investigation_leads", []):
        summary = lead.get("summary")
        if isinstance(summary, str) and summary:
            reasons.append(summary)
    if not reasons and bundle.get("summary"):
        reasons.append(str(bundle["summary"]))
    return "; ".join(dict.fromkeys(reasons[:4])) if reasons else None


def _trace_candidate_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    scenario_branches: list[dict[str, Any]] = []
    top_paths: list[str] = []
    candidate_count = 0
    for branch in bundle.get("branch_outcomes", []):
        scenario = {
            "source": "trace_branch",
            "status": branch.get("status", "unknown"),
            "file": branch.get("file"),
            "line": branch.get("line"),
            "qualified_name": branch.get("qualified_name"),
            "function_id": branch.get("function_id"),
            "slice_path": branch.get("slice_path"),
            "expression": branch.get("qualified_name") or branch.get("argument_expression"),
            "summary": branch.get("summary"),
        }
        if branch.get("contract") is not None:
            scenario["contract"] = branch.get("contract")
        if branch.get("argument_expression") is not None:
            scenario["argument_expression"] = branch.get("argument_expression")
        scenario_branches.append(scenario)
        if branch.get("file"):
            top_paths.append(str(branch["file"]))
        if branch.get("file") or branch.get("qualified_name") or branch.get("argument_expression"):
            candidate_count += 1
    strategy = bundle.get("agentic_strategy", {})
    for jump in strategy.get("frontier_jumps", []):
        scenario = {
            "source": "frontier_jump",
            "status": _frontier_jump_branch_status(jump),
            "file": jump.get("file"),
            "line": jump.get("line"),
            "expression": jump.get("expression"),
            "summary": _frontier_jump_summary(jump),
            "candidate_count": int(jump.get("candidate_count", 0)),
            "candidates": list(jump.get("candidates", [])),
        }
        scenario_branches.append(scenario)
        candidate_count += int(jump.get("candidate_count", 0))
        for candidate in jump.get("candidates", []):
            if candidate.get("file"):
                top_paths.append(str(candidate["file"]))
    top_candidate_paths = list(dict.fromkeys(top_paths))[:5]
    branch_labels: list[str] = []
    for branch in scenario_branches[:4]:
        label = branch.get("file") or branch.get("expression") or "local"
        branch_labels.append(f"{label} -> {branch.get('status', 'unknown')}")
    if candidate_count > 0 and branch_labels:
        candidate_summary = f"Investigated {candidate_count} candidate branch(es): " + "; ".join(branch_labels) + "."
    else:
        lead_summaries = [
            lead.get("summary")
            for lead in bundle.get("investigation_leads", [])
            if isinstance(lead.get("summary"), str) and lead.get("summary")
        ]
        if lead_summaries:
            candidate_summary = f"No concrete candidate branches were recovered. Outstanding lead: {lead_summaries[0]}"
        else:
            candidate_summary = bundle.get("summary") or "Agentic tracing did not recover any concrete candidate branches."
    return {
        "candidate_summary": candidate_summary,
        "candidate_count": candidate_count,
        "top_candidate_paths": top_candidate_paths,
        "scenario_branches": scenario_branches[:8],
        "why_still_uncertain": _why_still_uncertain(bundle, scenario_branches),
        "investigation_leads": list(bundle.get("investigation_leads", []))[:8],
    }


def _apply_investigation_summary(finding: dict[str, Any], bundle: dict[str, Any] | None = None) -> None:
    summary = _initial_finding_summary(finding) if bundle is None else _trace_candidate_summary(bundle)
    finding.update(summary)


def _finding_preview_entries(findings: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    preview: list[dict[str, Any]] = []
    for finding in findings[:limit]:
        preview.append(
            {
                "finding_id": finding.get("finding_id"),
                "file": finding.get("file"),
                "line": finding.get("line"),
                "message": finding.get("message"),
                "risk_level": finding.get("risk_level"),
                "risk_tier": finding.get("risk_tier"),
                "trace_status": finding.get("trace_status"),
                "candidate_summary": finding.get("candidate_summary"),
                "candidate_count": finding.get("candidate_count", 0),
                "top_candidate_paths": list(finding.get("top_candidate_paths", [])),
                "scenario_branches": list(finding.get("scenario_branches", []))[:4],
                "why_still_uncertain": finding.get("why_still_uncertain"),
            }
        )
    return preview


def _candidate_overview(findings: list[dict[str, Any]], trace_summary: dict[str, Any]) -> dict[str, Any]:
    paths: list[str] = []
    uncertain = 0
    with_candidates = 0
    for finding in findings:
        if finding.get("candidate_count", 0):
            with_candidates += 1
        if finding.get("trace_status") in {"uncertain", "budget_exhausted"}:
            uncertain += 1
        for path in finding.get("top_candidate_paths", []):
            paths.append(path)
    return {
        "visible_findings": len(findings),
        "with_explicit_candidates": with_candidates,
        "uncertain_findings": uncertain,
        "auto_silenced": int(trace_summary.get("auto_silenced", 0)),
        "top_candidate_paths": list(dict.fromkeys(paths))[:8],
    }


def _running_trace_summary(
    *,
    traced: int,
    silenced: int,
    escalated: int,
    visible_after_trace: int,
    risk_counts: dict[int, int],
    trace_errors: int,
    agentic_retraced: int,
    agentic_improved: int,
    agentic_promoted_safe: int,
    agentic_frontier_jumps: int,
) -> dict[str, int]:
    return {
        "traced": traced,
        "auto_silenced": silenced,
        "auto_filtered_low_confidence": 0,
        "escalated": escalated,
        "visible_after_trace": visible_after_trace,
        "level_1": risk_counts[1],
        "level_2": risk_counts[2],
        "level_3": risk_counts[3],
        "trace_errors": trace_errors,
        "agentic_retraced": agentic_retraced,
        "agentic_improved": agentic_improved,
        "agentic_promoted_safe": agentic_promoted_safe,
        "agentic_frontier_jumps": agentic_frontier_jumps,
    }


def _enrich_findings_with_traces(layout, root: Path, config: ReviewConfig, manifest: dict[str, Any], owner: str) -> dict[str, int]:
    if not config.symbol_tracing.enabled:
        return {"traced": 0, "auto_silenced": 0, "auto_filtered_low_confidence": 0, "escalated": 0, "visible_after_trace": 0, "level_1": 0, "level_2": 0, "level_3": 0, "trace_errors": 0, "agentic_retraced": 0, "agentic_improved": 0, "agentic_promoted_safe": 0, "agentic_frontier_jumps": 0}
    findings_total, trace_candidates_total = _count_prepare_candidates(layout)
    progress = default_prepare_progress()
    progress["phase"] = "trace_enrichment"
    progress["findings_total"] = findings_total
    progress["trace_candidates_total"] = trace_candidates_total
    _save_prepare_progress(layout, manifest, owner, progress)
    traced = 0
    silenced = 0
    escalated = 0
    visible_after_trace = 0
    trace_errors = 0
    agentic_retraced = 0
    agentic_improved = 0
    agentic_promoted_safe = 0
    agentic_frontier_jumps = 0
    risk_counts = {1: 0, 2: 0, 3: 0}
    active_finding_ids: set[str] = set()
    visible_preview: list[dict[str, Any]] = []
    for analysis_path in sorted(layout.analysis_dir.glob("*.json")):
        analysis_doc = load_json(analysis_path, default={})
        changed = False
        findings = analysis_doc.get("findings", [])
        for finding in findings:
            risk_level = int(finding.get("risk_level", 1 if finding.get("nil_state") == "nil" else 2))
            if risk_level in risk_counts:
                risk_counts[risk_level] += 1
            if finding.get("finding_id"):
                active_finding_ids.add(finding["finding_id"])
            if finding.get("suppressed"):
                finding["human_review_visible"] = False
                finding["auto_filtered_reason"] = "suppressed"
                _apply_investigation_summary(finding)
                changed = True
                continue
            progress["current_finding_id"] = finding.get("finding_id")
            progress["current_file"] = finding.get("file")
            progress["current_line"] = finding.get("line")
            if finding.get("nil_state") == "nil":
                finding["trace_status"] = "risky"
                finding["trace_summary"] = "Local analysis already proves a nil value reaches the sink."
                finding["trace_auto_silenced"] = False
                finding["trace_branch_outcomes"] = []
                finding["trace_depth_policy"] = {
                    "min_required_trace_depth": config.symbol_tracing.min_required_trace_depth,
                    "configured_max_depth": config.symbol_tracing.max_depth,
                    "trace_gate_required": bool(finding.get("trace_gate_required")),
                }
                finding["trace_gate_passed"] = True
                finding["human_review_visible"] = True
                finding["auto_filtered_reason"] = None
                _apply_investigation_summary(finding)
                progress["current_candidate_summary"] = finding.get("candidate_summary")
                visible_after_trace += 1
                progress["findings_done"] += 1
                progress["visible_after_trace"] = visible_after_trace
                progress["auto_silenced"] = silenced
                visible_preview.append(finding)
                manifest["trace_summary"] = _running_trace_summary(
                    traced=traced,
                    silenced=silenced,
                    escalated=escalated,
                    visible_after_trace=visible_after_trace,
                    risk_counts=risk_counts,
                    trace_errors=trace_errors,
                    agentic_retraced=agentic_retraced,
                    agentic_improved=agentic_improved,
                    agentic_promoted_safe=agentic_promoted_safe,
                    agentic_frontier_jumps=agentic_frontier_jumps,
                )
                manifest["candidate_overview"] = _candidate_overview(visible_preview, manifest["trace_summary"])
                manifest["finding_preview"] = _finding_preview_entries(visible_preview)
                _save_prepare_progress(layout, manifest, owner, progress)
                changed = True
                continue
            try:
                bundle = _trace_finding_with_strategy(root=root, layout=layout, config=config, finding=finding)
                traced += 1
                progress["trace_candidates_done"] += 1
                strategy = bundle.get("agentic_strategy", {})
                if strategy.get("triggered"):
                    agentic_retraced += 1
                    if strategy.get("improved"):
                        agentic_improved += 1
                    if strategy.get("retry_overall") == "safe" and strategy.get("initial_overall") in {"uncertain", "budget_exhausted"}:
                        agentic_promoted_safe += 1
                agentic_frontier_jumps += len(strategy.get("frontier_jumps", []))
                max_depth_used = max((node.get("depth", 0) for node in bundle.get("nodes", [])), default=0)
                auto_silenced = bundle.get("overall") == "safe" and not bundle.get("external_config_dependency")
                if auto_silenced:
                    silenced += 1
                if bundle.get("needs_source_escalation"):
                    escalated += 1
                bundle["trace_auto_silenced"] = auto_silenced
                bundle["max_depth_used"] = max_depth_used
                atomic_write_json(layout.trace_bundles_dir / f"{finding['finding_id']}.json", bundle)
                finding["trace_status"] = bundle.get("overall")
                finding["trace_summary"] = bundle.get("summary")
                finding["trace_bundle_path"] = f"trace_bundles/{finding['finding_id']}.json"
                finding["trace_auto_silenced"] = auto_silenced
                finding["trace_branch_outcomes"] = bundle.get("branch_outcomes", [])
                finding["needs_source_escalation"] = bool(bundle.get("needs_source_escalation"))
                finding["trace_error"] = False
                finding["trace_error_message"] = None
                finding["agentic_trace"] = {
                    "triggered": bool(strategy.get("triggered")),
                    "initial_overall": strategy.get("initial_overall"),
                    "retry_overall": strategy.get("retry_overall"),
                    "improved": bool(strategy.get("improved")),
                    "frontier_jump_count": len(strategy.get("frontier_jumps", [])),
                }
                finding["trace_depth_policy"] = {
                    "min_required_trace_depth": config.symbol_tracing.min_required_trace_depth,
                    "configured_max_depth": config.symbol_tracing.max_depth,
                    "max_depth_used": max_depth_used,
                    "trace_gate_required": bool(finding.get("trace_gate_required")),
                }
                finding["trace_gate_passed"] = bool(
                    not finding.get("trace_gate_required")
                    or bundle.get("overall") != "safe"
                    or bundle.get("external_config_dependency")
                )
                finding["human_review_visible"] = not auto_silenced
                if auto_silenced:
                    finding["auto_filtered_reason"] = "trace_safe"
                else:
                    finding["auto_filtered_reason"] = None
                finding["trace_definition_slice_paths"] = [
                    item.get("slice_path")
                    for item in bundle.get("branch_outcomes", [])
                    if item.get("slice_path")
                ][: config.symbol_tracing.max_unique_slices]
                _apply_investigation_summary(finding, bundle)
                if finding["human_review_visible"]:
                    visible_after_trace += 1
                    visible_preview.append(finding)
            except Exception as exc:
                trace_errors += 1
                progress["trace_candidates_done"] += 1
                finding["trace_status"] = "trace_error"
                finding["trace_summary"] = "Trace failed before the sink could be fully reconstructed."
                finding["trace_bundle_path"] = None
                finding["trace_auto_silenced"] = False
                finding["trace_branch_outcomes"] = []
                finding["needs_source_escalation"] = True
                finding["trace_error"] = True
                finding["trace_error_message"] = str(exc)
                finding["agentic_trace"] = {
                    "triggered": False,
                    "initial_overall": "trace_error",
                    "retry_overall": "trace_error",
                    "improved": False,
                    "frontier_jump_count": 0,
                }
                finding["trace_depth_policy"] = {
                    "min_required_trace_depth": config.symbol_tracing.min_required_trace_depth,
                    "configured_max_depth": config.symbol_tracing.max_depth,
                    "max_depth_used": 0,
                    "trace_gate_required": bool(finding.get("trace_gate_required")),
                }
                finding["trace_gate_passed"] = False
                finding["human_review_visible"] = True
                finding["auto_filtered_reason"] = None
                finding["trace_definition_slice_paths"] = []
                finding["candidate_summary"] = "Trace failed before candidate branches could be reconstructed. Manual review is required for this finding."
                finding["candidate_count"] = 0
                finding["top_candidate_paths"] = []
                finding["scenario_branches"] = []
                finding["why_still_uncertain"] = str(exc)
                finding["investigation_leads"] = []
                visible_after_trace += 1
                visible_preview.append(finding)
            progress["findings_done"] += 1
            progress["visible_after_trace"] = visible_after_trace
            progress["auto_silenced"] = silenced
            progress["trace_errors"] = trace_errors
            progress["agentic_retraced"] = agentic_retraced
            progress["agentic_improved"] = agentic_improved
            progress["agentic_promoted_safe"] = agentic_promoted_safe
            progress["agentic_frontier_jumps"] = agentic_frontier_jumps
            progress["current_candidate_summary"] = finding.get("candidate_summary")
            manifest["trace_summary"] = _running_trace_summary(
                traced=traced,
                silenced=silenced,
                escalated=escalated,
                visible_after_trace=visible_after_trace,
                risk_counts=risk_counts,
                trace_errors=trace_errors,
                agentic_retraced=agentic_retraced,
                agentic_improved=agentic_improved,
                agentic_promoted_safe=agentic_promoted_safe,
                agentic_frontier_jumps=agentic_frontier_jumps,
            )
            manifest["candidate_overview"] = _candidate_overview(visible_preview, manifest["trace_summary"])
            manifest["finding_preview"] = _finding_preview_entries(visible_preview)
            _save_prepare_progress(layout, manifest, owner, progress)
            changed = True
        if changed:
            atomic_write_json(analysis_path, analysis_doc)
    for bundle_path in layout.trace_bundles_dir.glob("*.json"):
        if bundle_path.stem in active_finding_ids or bundle_path.stem.startswith("callsite-"):
            continue
        bundle_path.unlink(missing_ok=True)
    return _running_trace_summary(
        traced=traced,
        silenced=silenced,
        escalated=escalated,
        visible_after_trace=visible_after_trace,
        risk_counts=risk_counts,
        trace_errors=trace_errors,
        agentic_retraced=agentic_retraced,
        agentic_improved=agentic_improved,
        agentic_promoted_safe=agentic_promoted_safe,
        agentic_frontier_jumps=agentic_frontier_jumps,
    )


def run_prepare_shards(*, root: Path, state_dir: Path, resume: bool, config_path: str | None = None) -> dict[str, Any]:
    layout = build_layout(root, state_dir)
    owner = acquire_lock(layout, "prepare")
    try:
        manifest = load_or_rebuild_manifest(layout)
        config = _load_prepare_config(root, manifest, config_path)
        manifest["stage"] = "sharding"
        manifest["lock_owner"] = owner
        manifest["trace_summary"] = {}
        manifest["prepare_progress"] = default_prepare_progress()
        manifest["candidate_overview"] = {}
        manifest["finding_preview"] = []
        save_manifest(layout, manifest)
        for shard in manifest.get("shards", {}).values():
            if shard.get("status") == "in_review" and is_stale(shard.get("heartbeat_at")):
                shard["status"] = "pending"
                shard["heartbeat_at"] = None

        trace_summary = _enrich_findings_with_traces(layout, root, config, manifest, owner)

        active, suppressed = _active_findings(layout)
        progress = manifest.get("prepare_progress", default_prepare_progress())
        progress["phase"] = "building_shards"
        progress["current_finding_id"] = None
        progress["current_file"] = None
        progress["current_line"] = None
        progress["current_candidate_summary"] = None
        progress["active_findings_total"] = len(active)
        _save_prepare_progress(layout, manifest, owner, progress)
        shards: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for finding in active:
            candidate = current + [finding]
            if current and (len(candidate) > SHARD_MAX_FINDINGS or _shard_byte_size(layout, candidate) > SHARD_MAX_BYTES):
                shards.append(current)
                current = [finding]
            else:
                current = candidate
        if current:
            shards.append(current)

        existing_reviews = {}
        for review_path in layout.reviews_dir.glob("*.json"):
            review = load_json(review_path, default={})
            if isinstance(review, dict) and review.get("shard_id"):
                existing_reviews[review["shard_id"]] = review_path.relative_to(layout.state_dir).as_posix()

        active_shard_ids = set()
        new_manifest_shards: dict[str, Any] = {}
        for index, shard_findings in enumerate(shards):
            first_id = shard_findings[0]["finding_id"]
            last_id = shard_findings[-1]["finding_id"]
            shard_id = sha1_hex(f"{first_id}|{last_id}|{index}")
            active_shard_ids.add(shard_id)
            shard_path = layout.findings_dir / f"{shard_id}.jsonl"
            atomic_write_jsonl(shard_path, shard_findings)
            status = "reviewed" if shard_id in existing_reviews else "pending"
            new_manifest_shards[shard_id] = {
                "status": status,
                "heartbeat_at": None,
                "findings_count": len(shard_findings),
                "first_finding_id": first_id,
                "last_finding_id": last_id,
                "review_path": existing_reviews.get(shard_id),
                "shard_path": shard_path.relative_to(layout.state_dir).as_posix(),
            }
            manifest["shards"] = new_manifest_shards
            manifest["shards_total"] = len(new_manifest_shards)
            manifest["shards_reviewed"] = len([item for item in new_manifest_shards.values() if item["status"] in {"reviewed", "merged"}])
            manifest["suppressed_findings"] = suppressed
            progress["shards_built"] = len(new_manifest_shards)
            _save_prepare_progress(layout, manifest, owner, progress)

        for shard_path in layout.findings_dir.glob("*.jsonl"):
            if shard_path.stem not in active_shard_ids:
                shard_path.unlink()

        manifest["shards"] = new_manifest_shards
        manifest["shards_total"] = len(new_manifest_shards)
        manifest["shards_reviewed"] = len([item for item in new_manifest_shards.values() if item["status"] in {"reviewed", "merged"}])
        manifest["suppressed_findings"] = suppressed
        manifest["trace_summary"] = trace_summary
        manifest["candidate_overview"] = _candidate_overview(active, trace_summary)
        manifest["finding_preview"] = _finding_preview_entries(active)
        manifest["stage"] = "sharded"
        progress["phase"] = "completed"
        progress["active_findings_total"] = len(active)
        progress["shards_built"] = len(new_manifest_shards)
        progress["current_finding_id"] = None
        progress["current_file"] = None
        progress["current_line"] = None
        progress["current_candidate_summary"] = None
        manifest["prepare_progress"] = progress
        save_manifest(layout, manifest)
        return {
            "shards_total": len(new_manifest_shards),
            "suppressed_findings": suppressed,
            "trace_summary": trace_summary,
        }
    finally:
        release_lock(layout, owner)


def _shard_payload(layout, shard_id: str) -> dict[str, Any]:
    shard_path = layout.findings_dir / f"{shard_id}.jsonl"
    findings = load_jsonl(shard_path)
    payload_findings = []
    for finding in findings:
        snippets = []
        for relative in finding.get("snippet_paths", []):
            path = layout.state_dir / relative
            if path.exists():
                snippets.append({"path": relative, "content": path.read_text(encoding="utf-8")})
        trace_bundle = load_trace_bundle(layout, finding["finding_id"])
        trace_slices = []
        for relative in finding.get("trace_definition_slice_paths", []):
            content = candidate_slice_content(layout.root, layout.state_dir, relative)
            if content is not None:
                trace_slices.append({"path": relative, "content": content})
        payload_findings.append({**finding, "snippets": snippets, "trace_bundle": trace_bundle, "trace_slices": trace_slices})
    template = {
        "shard_id": shard_id,
        "reviewer": "",
        "summary": "",
        "finding_reviews": [{"finding_id": finding["finding_id"], "decision": "", "rationale": "", "severity": ""} for finding in findings],
    }
    template_path = layout.reviews_dir / f"{shard_id}.template.json"
    atomic_write_json(template_path, template)
    return {
        "status": "claimed",
        "shard_id": shard_id,
        "shard_path": shard_path.relative_to(layout.state_dir).as_posix(),
        "review_template_path": template_path.relative_to(layout.state_dir).as_posix(),
        "findings": payload_findings,
    }


def load_status_snapshot(*, root: Path, state_dir: Path) -> dict[str, Any]:
    layout = build_layout(root, state_dir)
    manifest = load_or_rebuild_manifest(layout)
    analyze_progress = manifest.get("analyze_progress") or default_analyze_progress()
    prepare_progress = manifest.get("prepare_progress") or default_prepare_progress()
    return {
        "stage": manifest.get("stage"),
        "updated_at": manifest.get("updated_at"),
        "lock_owner": manifest.get("lock_owner"),
        "files_total": int(manifest.get("files_total", 0)),
        "files_done": int(manifest.get("files_done", 0)),
        "shards_total": int(manifest.get("shards_total", 0)),
        "shards_reviewed": int(manifest.get("shards_reviewed", 0)),
        "suppressed_findings": int(manifest.get("suppressed_findings", 0)),
        "trace_summary": manifest.get("trace_summary", {}),
        "analyze_progress": analyze_progress,
        "prepare_progress": prepare_progress,
        "candidate_overview": manifest.get("candidate_overview", {}),
        "finding_preview": manifest.get("finding_preview", []),
    }


def claim_next_shard(*, root: Path, state_dir: Path) -> dict[str, Any]:
    layout = build_layout(root, state_dir)
    owner = acquire_lock(layout, "review")
    try:
        manifest = load_or_rebuild_manifest(layout)
        for shard in manifest.get("shards", {}).values():
            if shard.get("status") == "in_review" and is_stale(shard.get("heartbeat_at")):
                shard["status"] = "pending"
                shard["heartbeat_at"] = None
        pending = [shard_id for shard_id, shard in sorted(manifest.get("shards", {}).items()) if shard.get("status") == "pending"]
        if not pending:
            save_manifest(layout, manifest)
            return {"status": "empty"}
        shard_id = pending[0]
        manifest["shards"][shard_id]["status"] = "in_review"
        manifest["shards"][shard_id]["heartbeat_at"] = utc_now()
        manifest["stage"] = "reviewing"
        manifest["lock_owner"] = owner
        touch_lock(layout, owner)
        save_manifest(layout, manifest)
        return _shard_payload(layout, shard_id)
    finally:
        release_lock(layout, owner)


def heartbeat_shard(*, root: Path, state_dir: Path, shard_id: str) -> dict[str, Any]:
    layout = build_layout(root, state_dir)
    owner = acquire_lock(layout, "review-heartbeat")
    try:
        manifest = load_or_rebuild_manifest(layout)
        shard = manifest.get("shards", {}).get(shard_id)
        if shard is None:
            raise RuntimeError(f"Unknown shard '{shard_id}'.")
        shard["heartbeat_at"] = utc_now()
        touch_lock(layout, owner)
        save_manifest(layout, manifest)
        return {"status": "heartbeat", "shard_id": shard_id}
    finally:
        release_lock(layout, owner)


def complete_shard(*, root: Path, state_dir: Path, review_path: Path) -> dict[str, Any]:
    layout = build_layout(root, state_dir)
    owner = acquire_lock(layout, "review-complete")
    try:
        manifest = load_or_rebuild_manifest(layout)
        review_data = load_json(review_path, default={})
        shard_id = review_data.get("shard_id")
        if not shard_id:
            raise RuntimeError("Review JSON must contain 'shard_id'.")
        if shard_id not in manifest.get("shards", {}):
            raise RuntimeError(f"Unknown shard '{shard_id}'.")
        normalized = {
            "completed_at": utc_now(),
            "finding_reviews": review_data.get("finding_reviews", []),
            "reviewer": review_data.get("reviewer", ""),
            "shard_id": shard_id,
            "summary": review_data.get("summary", ""),
        }
        target = layout.reviews_dir / f"{shard_id}.json"
        atomic_write_json(target, normalized)
        shard = manifest["shards"][shard_id]
        shard["status"] = "reviewed"
        shard["heartbeat_at"] = utc_now()
        shard["review_path"] = target.relative_to(layout.state_dir).as_posix()
        manifest["shards_reviewed"] = len([item for item in manifest["shards"].values() if item["status"] in {"reviewed", "merged"}])
        save_manifest(layout, manifest)
        return {"status": "reviewed", "shard_id": shard_id}
    finally:
        release_lock(layout, owner)


def _load_baseline_ids(root: Path, config: ReviewConfig) -> set[str]:
    if not config.baseline:
        return set()
    baseline_path = Path(config.baseline)
    if not baseline_path.is_absolute():
        baseline_path = root / baseline_path
    if not baseline_path.exists():
        return set()
    if baseline_path.suffix == ".jsonl":
        return {item["finding_id"] for item in load_jsonl(baseline_path) if "finding_id" in item}
    payload = load_json(baseline_path, default={})
    if isinstance(payload, list):
        return {item["finding_id"] for item in payload if isinstance(item, dict) and "finding_id" in item}
    if isinstance(payload, dict):
        for key in ("findings", "confirmed_findings"):
            items = payload.get(key)
            if isinstance(items, list):
                return {item["finding_id"] for item in items if isinstance(item, dict) and "finding_id" in item}
    return set()


def run_merge(*, root: Path, state_dir: Path, config_path: str | None) -> dict[str, Any]:
    layout = build_layout(root, state_dir)
    owner = acquire_lock(layout, "merge")
    try:
        config, _ = load_config(root, config_path)
        baseline_ids = _load_baseline_ids(root, config)
        manifest = load_or_rebuild_manifest(layout)
        manifest["stage"] = "merging"
        manifest["lock_owner"] = owner
        save_manifest(layout, manifest)

        confirmed: list[dict[str, Any]] = []
        escalated: list[dict[str, Any]] = []
        pending_shards: list[str] = []
        dismissed = 0
        for shard_id, shard in sorted(manifest.get("shards", {}).items()):
            shard_findings = load_jsonl(layout.findings_dir / f"{shard_id}.jsonl")
            review_path = shard.get("review_path")
            review = load_json(layout.state_dir / review_path, default={}) if review_path else {}
            decisions = {item["finding_id"]: item for item in review.get("finding_reviews", []) if isinstance(item, dict) and item.get("finding_id")}
            if not decisions:
                pending_shards.append(shard_id)
                continue
            for finding in shard_findings:
                decision = decisions.get(finding["finding_id"])
                if not decision:
                    continue
                enriched = {
                    **finding,
                    "decision": decision.get("decision", ""),
                    "rationale": decision.get("rationale", ""),
                    "severity": decision.get("severity", ""),
                    "is_new": finding["finding_id"] not in baseline_ids,
                }
                if enriched["decision"] == "dismiss":
                    dismissed += 1
                    continue
                if enriched["decision"] == "needs_source_escalation":
                    escalated.append(enriched)
                else:
                    confirmed.append(enriched)
            shard["status"] = "merged"

        trace_summary = manifest.get("trace_summary", {})
        summary = {
            "generated_at": utc_now(),
            "rule_id": RULE_ID,
            "analysis_fingerprint": manifest.get("analysis_fingerprint"),
            "totals": {
                "files_total": manifest.get("files_total", 0),
                "shards_total": manifest.get("shards_total", 0),
                "shards_reviewed": manifest.get("shards_reviewed", 0),
                "pending_shards": len(pending_shards),
                "confirmed_findings": len(confirmed),
                "needs_source_escalation": len(escalated),
                "dismissed_findings": dismissed,
                "suppressed_findings": manifest.get("suppressed_findings", 0),
                "new_confirmed_findings": len([finding for finding in confirmed if finding["is_new"]]),
                "auto_filtered_safe": int(trace_summary.get("auto_silenced", 0)),
                "auto_filtered_low_confidence": int(trace_summary.get("auto_filtered_low_confidence", 0)),
                "risk_level_1": int(trace_summary.get("level_1", 0)),
                "risk_level_2": int(trace_summary.get("level_2", 0)),
                "risk_level_3": int(trace_summary.get("level_3", 0)),
                "human_review_candidates": int(trace_summary.get("visible_after_trace", 0)),
                "trace_errors": int(trace_summary.get("trace_errors", 0)),
                "agentic_retraced": int(trace_summary.get("agentic_retraced", 0)),
                "agentic_improved": int(trace_summary.get("agentic_improved", 0)),
                "agentic_promoted_safe": int(trace_summary.get("agentic_promoted_safe", 0)),
                "agentic_frontier_jumps": int(trace_summary.get("agentic_frontier_jumps", 0)),
            },
            "confirmed_findings": confirmed,
            "needs_source_escalation": escalated,
            "pending_shards": pending_shards,
            "trace_summary": trace_summary,
        }
        atomic_write_json(layout.final_dir / "summary.json", summary)
        report_lines = [
            "# Lua Nil Review Report",
            "",
            f"- Generated at: {summary['generated_at']}",
            f"- Rule: {RULE_ID}",
            f"- Files scanned: {summary['totals']['files_total']}",
            f"- Confirmed findings: {summary['totals']['confirmed_findings']}",
            f"- Needs source escalation: {summary['totals']['needs_source_escalation']}",
            f"- Pending shards: {summary['totals']['pending_shards']}",
            f"- Auto-filtered safe findings: {summary['totals']['auto_filtered_safe']}",
            f"- Legacy low-confidence auto-filter count: {summary['totals']['auto_filtered_low_confidence']}",
            f"- Trace errors isolated during prepare: {summary['totals']['trace_errors']}",
            f"- Agentic retraces before claim: {summary['totals']['agentic_retraced']}",
            f"- Agentic retraces that improved certainty: {summary['totals']['agentic_improved']}",
            f"- Agentic retraces that proved safe: {summary['totals']['agentic_promoted_safe']}",
            f"- Frontier jump expansions: {summary['totals']['agentic_frontier_jumps']}",
            f"- Risk level totals: L1={summary['totals']['risk_level_1']} L2={summary['totals']['risk_level_2']} L3={summary['totals']['risk_level_3']}",
            "",
        ]
        if confirmed:
            report_lines.extend(["## Confirmed Findings", ""])
            for finding in confirmed:
                report_lines.extend(
                    [
                        f"### {finding['file']}:{finding['line']}",
                        finding["message"],
                        f"- Risk level: Level {finding.get('risk_level', 'n/a')} ({finding.get('risk_tier', 'n/a')})",
                        f"- Risk category: {finding.get('risk_category', 'n/a')}",
                        f"- Confidence: {finding['confidence']}",
                        f"- Decision: {finding['decision']}",
                        f"- Trace: {finding.get('trace_status', 'n/a')}",
                        f"- New vs baseline: {'yes' if finding['is_new'] else 'no'}",
                        f"- Rationale: {finding['rationale'] or 'n/a'}",
                        f"- Trace summary: {finding.get('trace_summary', 'n/a')}",
                        f"- Candidate summary: {finding.get('candidate_summary', 'n/a')}",
                        f"- Why still uncertain: {finding.get('why_still_uncertain') or 'n/a'}",
                        "",
                    ]
                )
                for branch in finding.get("scenario_branches", []):
                    label = branch.get("file") or branch.get("expression") or "unknown path"
                    report_lines.append(f"  - [{label}] -> {branch.get('status', 'unknown')}")
                if finding.get("scenario_branches"):
                    report_lines.append("")
        if escalated:
            report_lines.extend(["## Needs Source Escalation", ""])
            for finding in escalated:
                report_lines.append(
                    f"- {finding['file']}:{finding['line']} `{normalize_whitespace(finding['call_text'])}` "
                    f"(Level {finding.get('risk_level', 'n/a')}, trace={finding.get('trace_status', 'n/a')})"
                )
                report_lines.append(f"  - Candidate summary: {finding.get('candidate_summary', 'n/a')}")
                if finding.get("why_still_uncertain"):
                    report_lines.append(f"  - Why still uncertain: {finding['why_still_uncertain']}")
                for branch in finding.get("scenario_branches", []):
                    label = branch.get("file") or branch.get("expression") or "unknown path"
                    report_lines.append(f"  - [{label}] -> {branch.get('status', 'unknown')}")
            report_lines.append("")
        if pending_shards:
            report_lines.extend(["## Pending Shards", ""])
            report_lines.extend([f"- {shard_id}" for shard_id in pending_shards])
            report_lines.append("")
        atomic_write_text(layout.final_dir / "report.md", "\n".join(report_lines) + "\n")
        manifest["stage"] = "merged"
        manifest["shards_reviewed"] = len([item for item in manifest["shards"].values() if item["status"] in {"reviewed", "merged"}])
        save_manifest(layout, manifest)
        return summary
    finally:
        release_lock(layout, owner)
