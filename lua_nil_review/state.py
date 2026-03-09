from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .common import (
    ANALYZER_VERSION,
    PARSER_VERSION,
    REPORT_TEMPLATE_VERSION,
    RULE_VERSION,
    SHARD_TIMEOUT,
    STATE_VERSION,
    atomic_write_json,
    atomic_write_jsonl,
    ensure_dir,
    is_stale,
    load_json,
    load_jsonl,
    owner_id,
    remove_children,
    utc_now,
)


@dataclass(frozen=True)
class StateLayout:
    root: Path
    state_dir: Path
    manifest_path: Path
    files_index_path: Path
    analysis_dir: Path
    findings_dir: Path
    reviews_dir: Path
    snippets_dir: Path
    symbol_index_dir: Path
    symbol_files_dir: Path
    symbol_modules_dir: Path
    symbol_slices_dir: Path
    trace_bundles_dir: Path
    final_dir: Path
    lock_path: Path


def build_layout(root: Path, state_dir: Path) -> StateLayout:
    state_dir = state_dir if state_dir.is_absolute() else root / state_dir
    ensure_dir(state_dir)
    return StateLayout(
        root=root,
        state_dir=state_dir,
        manifest_path=state_dir / "manifest.json",
        files_index_path=state_dir / "files.jsonl",
        analysis_dir=ensure_dir(state_dir / "analysis"),
        findings_dir=ensure_dir(state_dir / "findings"),
        reviews_dir=ensure_dir(state_dir / "reviews"),
        snippets_dir=ensure_dir(state_dir / "snippets"),
        symbol_index_dir=ensure_dir(state_dir / "symbol_index"),
        symbol_files_dir=ensure_dir(state_dir / "symbol_index" / "files"),
        symbol_modules_dir=ensure_dir(state_dir / "symbol_index" / "modules"),
        symbol_slices_dir=ensure_dir(state_dir / "symbol_slices"),
        trace_bundles_dir=ensure_dir(state_dir / "trace_bundles"),
        final_dir=ensure_dir(state_dir / "final"),
        lock_path=state_dir / "run.lock",
    )


def default_manifest(layout: StateLayout) -> dict[str, Any]:
    return {
        "state_version": STATE_VERSION,
        "run_id": str(uuid4()),
        "analysis_fingerprint": "",
        "symbol_fingerprint": "",
        "analyzer_version": ANALYZER_VERSION,
        "parser_version": PARSER_VERSION,
        "rule_version": RULE_VERSION,
        "report_template_version": REPORT_TEMPLATE_VERSION,
        "root": str(layout.root.resolve()),
        "state_dir": str(layout.state_dir.resolve()),
        "config_path": None,
        "stage": "idle",
        "files_total": 0,
        "files_done": 0,
        "shards_total": 0,
        "shards_reviewed": 0,
        "suppressed_findings": 0,
        "trace_summary": {},
        "updated_at": utc_now(),
        "lock_owner": "",
        "shards": {},
    }


def rebuild_manifest(layout: StateLayout) -> dict[str, Any]:
    manifest = default_manifest(layout)
    files_index = load_jsonl(layout.files_index_path)
    manifest["files_total"] = len(files_index)
    manifest["files_done"] = len([entry for entry in files_index if entry.get("analysis_status") in {"analyzed", "reused"}])
    shards: dict[str, Any] = {}
    for shard_file in sorted(layout.findings_dir.glob("*.jsonl")):
        shard_id = shard_file.stem
        review_path = layout.reviews_dir / f"{shard_id}.json"
        status = "reviewed" if review_path.exists() else "pending"
        shards[shard_id] = {
            "status": status,
            "heartbeat_at": None,
            "review_path": review_path.relative_to(layout.state_dir).as_posix() if review_path.exists() else None,
            "shard_path": shard_file.relative_to(layout.state_dir).as_posix(),
        }
    manifest["shards"] = shards
    manifest["shards_total"] = len(shards)
    manifest["shards_reviewed"] = len([item for item in shards.values() if item["status"] in {"reviewed", "merged"}])
    if manifest["shards_total"]:
        manifest["stage"] = "sharded"
    elif manifest["files_done"]:
        manifest["stage"] = "analyzed"
    return manifest


def load_or_rebuild_manifest(layout: StateLayout) -> dict[str, Any]:
    manifest = load_json(layout.manifest_path)
    if not isinstance(manifest, dict):
        manifest = rebuild_manifest(layout)
    manifest.setdefault("shards", {})
    manifest.setdefault("report_template_version", REPORT_TEMPLATE_VERSION)
    manifest.setdefault("symbol_fingerprint", "")
    manifest.setdefault("trace_summary", {})
    return manifest


def save_manifest(layout: StateLayout, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = utc_now()
    atomic_write_json(layout.manifest_path, manifest)


def load_files_index(layout: StateLayout) -> dict[str, dict[str, Any]]:
    return {entry["file_id"]: entry for entry in load_jsonl(layout.files_index_path) if "file_id" in entry}


def save_files_index(layout: StateLayout, entries: list[dict[str, Any]]) -> None:
    ordered = sorted(entries, key=lambda item: item["file"])
    atomic_write_jsonl(layout.files_index_path, ordered)


def acquire_lock(layout: StateLayout, label: str) -> str:
    owner = owner_id(label)
    existing = load_json(layout.lock_path, default={})
    if isinstance(existing, dict):
        existing_owner = existing.get("owner")
        if existing_owner and existing_owner != owner and not is_stale(existing.get("updated_at"), timeout=SHARD_TIMEOUT):
            raise RuntimeError(f"State directory is locked by {existing_owner}.")
    atomic_write_json(layout.lock_path, {"owner": owner, "updated_at": utc_now()})
    return owner


def touch_lock(layout: StateLayout, owner: str) -> None:
    atomic_write_json(layout.lock_path, {"owner": owner, "updated_at": utc_now()})


def release_lock(layout: StateLayout, owner: str) -> None:
    existing = load_json(layout.lock_path, default={})
    if isinstance(existing, dict) and existing.get("owner") == owner:
        layout.lock_path.unlink(missing_ok=True)


def reset_outputs_for_new_fingerprint(layout: StateLayout) -> None:
    remove_children(layout.analysis_dir, suffixes=(".json",))
    remove_children(layout.findings_dir, suffixes=(".jsonl",))
    remove_children(layout.snippets_dir, suffixes=(".txt",))
    remove_children(layout.symbol_files_dir, suffixes=(".json",))
    remove_children(layout.symbol_modules_dir, suffixes=(".json",))
    remove_children(layout.symbol_index_dir, suffixes=(".json",))
    remove_children(layout.symbol_slices_dir, suffixes=(".txt",))
    remove_children(layout.trace_bundles_dir, suffixes=(".json",))
    remove_children(layout.final_dir, suffixes=(".json", ".md"))
