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
from .tracer import TraceEngine, load_trace_bundle


def analysis_fingerprint(config: ReviewConfig) -> str:
    return sha256_hex("|".join([RULE_VERSION, ANALYZER_VERSION, PARSER_VERSION, config.fingerprint()]))


def symbol_fingerprint(config: ReviewConfig) -> str:
    return sha256_hex("|".join([SYMBOL_INDEX_VERSION, PARSER_VERSION, config.fingerprint()]))


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
        save_manifest(layout, manifest)

        current_paths = discover_lua_files(root, config, layout.state_dir)
        entries: list[dict[str, Any]] = []
        symbol_docs = []
        current_ids = set()
        manifest["files_total"] = len(current_paths)
        manifest["files_done"] = 0

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
            if finding.get("suppressed") or finding.get("trace_auto_silenced"):
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


def _enrich_findings_with_traces(layout, root: Path, config: ReviewConfig) -> dict[str, int]:
    if not config.symbol_tracing.enabled:
        return {"traced": 0, "auto_silenced": 0, "escalated": 0}
    engine = TraceEngine(root, layout, config)
    traced = 0
    silenced = 0
    escalated = 0
    active_finding_ids: set[str] = set()
    for analysis_path in sorted(layout.analysis_dir.glob("*.json")):
        analysis_doc = load_json(analysis_path, default={})
        changed = False
        findings = analysis_doc.get("findings", [])
        for finding in findings:
            if finding.get("finding_id"):
                active_finding_ids.add(finding["finding_id"])
            if finding.get("suppressed"):
                continue
            if finding.get("nil_state") == "nil":
                finding["trace_status"] = "risky"
                finding["trace_summary"] = "Local analysis already proves a nil value reaches the sink."
                finding["trace_auto_silenced"] = False
                finding["trace_branch_outcomes"] = []
                continue
            bundle = engine.trace_finding(finding)
            traced += 1
            max_depth_used = max((node.get("depth", 0) for node in bundle.get("nodes", [])), default=0)
            auto_silenced = bundle.get("overall") == "safe" and max_depth_used <= config.symbol_tracing.auto_silence_depth + 1
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
            finding["trace_definition_slice_paths"] = [
                item.get("slice_path")
                for item in bundle.get("branch_outcomes", [])
                if item.get("slice_path")
            ][: config.symbol_tracing.max_unique_slices]
            changed = True
        if changed:
            atomic_write_json(analysis_path, analysis_doc)
    for bundle_path in layout.trace_bundles_dir.glob("*.json"):
        if bundle_path.stem in active_finding_ids or bundle_path.stem.startswith("callsite-"):
            continue
        bundle_path.unlink(missing_ok=True)
    return {"traced": traced, "auto_silenced": silenced, "escalated": escalated}


def run_prepare_shards(*, root: Path, state_dir: Path, resume: bool, config_path: str | None = None) -> dict[str, Any]:
    layout = build_layout(root, state_dir)
    owner = acquire_lock(layout, "prepare")
    try:
        manifest = load_or_rebuild_manifest(layout)
        config = _load_prepare_config(root, manifest, config_path)
        manifest["stage"] = "sharding"
        manifest["lock_owner"] = owner
        save_manifest(layout, manifest)
        for shard in manifest.get("shards", {}).values():
            if shard.get("status") == "in_review" and is_stale(shard.get("heartbeat_at")):
                shard["status"] = "pending"
                shard["heartbeat_at"] = None

        trace_summary = _enrich_findings_with_traces(layout, root, config)

        active, suppressed = _active_findings(layout)
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
            touch_lock(layout, owner)
            save_manifest(layout, manifest)

        for shard_path in layout.findings_dir.glob("*.jsonl"):
            if shard_path.stem not in active_shard_ids:
                shard_path.unlink()

        manifest["shards"] = new_manifest_shards
        manifest["shards_total"] = len(new_manifest_shards)
        manifest["shards_reviewed"] = len([item for item in new_manifest_shards.values() if item["status"] in {"reviewed", "merged"}])
        manifest["suppressed_findings"] = suppressed
        manifest["trace_summary"] = trace_summary
        manifest["stage"] = "sharded"
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
            },
            "confirmed_findings": confirmed,
            "needs_source_escalation": escalated,
            "pending_shards": pending_shards,
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
            "",
        ]
        if confirmed:
            report_lines.extend(["## Confirmed Findings", ""])
            for finding in confirmed:
                report_lines.extend(
                    [
                        f"### {finding['file']}:{finding['line']}",
                        finding["message"],
                        f"- Confidence: {finding['confidence']}",
                        f"- Decision: {finding['decision']}",
                        f"- Trace: {finding.get('trace_status', 'n/a')}",
                        f"- New vs baseline: {'yes' if finding['is_new'] else 'no'}",
                        f"- Rationale: {finding['rationale'] or 'n/a'}",
                        f"- Trace summary: {finding.get('trace_summary', 'n/a')}",
                        "",
                    ]
                )
        if escalated:
            report_lines.extend(["## Needs Source Escalation", ""])
            report_lines.extend([f"- {finding['file']}:{finding['line']} `{normalize_whitespace(finding['call_text'])}`" for finding in escalated])
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
