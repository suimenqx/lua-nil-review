# Workflow

See [architecture.md](architecture.md) first if you want the visual version of this pipeline before reading the detailed state and resume rules.

## Persisted State

The pipeline writes all durable state under `artifacts/string-find-nil/`.

- `manifest.json`: run metadata, progress counters, shard statuses, and lock owner
- `files.jsonl`: one entry per Lua file with file hash, analysis status, and analysis artifact path
- `analysis/<file_id>.json`: per-file findings and parse status
- `symbol_index/files/<file_id>.json`: per-file symbol facts
- `symbol_index/modules/*.json`: collision-aware logical module index
- `symbol_slices/*.txt`: cached function logic slices for jump and trace
- `trace_bundles/<finding_id>.json`: persisted bounded trace results
- `findings/<shard_id>.jsonl`: active review shards
- `reviews/<shard_id>.json`: completed shard reviews
- `final/summary.json` and `final/report.md`: merged outputs

## Entry Points

- `scripts/run_review_cycle.py` is the highest-level wrapper and the preferred entry for CodeAgent.
- The wrapper also exposes `build-symbol-index`, `jump`, and `trace` for collision-aware symbol navigation and bounded cross-function tracing.
- `scripts/analyze_string_find_nil.py`, `scripts/prepare_review_shards.py`, `scripts/review_shard.py`, and `scripts/merge_review_results.py` remain available for manual phase control.
- `CODEAGENT.md` is generated from a shared source by `python scripts/generate_adapter_docs.py`.

## CodeAgent Skill Install

Prefer `codeagent skills link <repo> --scope workspace` or `codeagent skills install <repo> --scope workspace` for this skill. Workspace scope keeps the skill scripts at a predictable path inside the target repository:

- `.codeagent/skills/lua-nil-review/`

If the skill is installed at user scope instead, resolve the actual install path with `codeagent skills list --all` before invoking the bundled scripts.

## Resume Rules

- Reuse an analysis result when the file content hash and analysis fingerprint match.
- Reuse file-level symbol facts when the file content hash and symbol fingerprint match.
- Reset `analysis/`, `findings/`, `snippets/`, and `final/` when the analysis fingerprint changes.
- Reclaim a shard from `in_review` back to `pending` when its heartbeat is older than 30 minutes.
- Reuse an existing review only when the shard ID is unchanged.

## Review Discipline

- Claim one shard at a time.
- Read only the shard payload and snippet files.
- Prefer the attached `trace_bundle` and `trace_slices` before opening full source files.
- Use `trace --file/--line/--expr` when a direct callsite needs manual expansion outside the normal shard flow.
- Mark a finding as `needs_source_escalation` when the snippet set is insufficient.
- Do not read an entire 3000+ line file unless escalation is necessary.

## Trace Gating

- `prepare` enriches every unsuppressed `maybe_nil` finding with a persisted trace bundle before sharding.
- Level `3` findings are not human-visible by default. They only enter shards when trace proves a `risky` or `mixed` branch.
- Safe traces are auto-silenced and counted in `manifest.json -> trace_summary -> auto_silenced`.
- Unconfirmed Level `3` traces are auto-filtered and counted in `manifest.json -> trace_summary -> auto_filtered_low_confidence`.
- `final/report.md` surfaces collision branch outcomes as `[path] -> status` lines so scenario-dependent results stay explicit.
