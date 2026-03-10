# Workflow

See [architecture.md](architecture.md) first if you want the visual version of this pipeline before reading the detailed state and resume rules.

## Persisted State

The pipeline writes all durable state under `artifacts/string-find-nil/`.

- `manifest.json`: run metadata, progress counters, shard statuses, and lock owner
- `manifest.json -> prepare_progress`: real-time counters for trace enrichment and shard building
- `manifest.json -> candidate_overview`: compact candidate/path summary for visible findings
- `manifest.json -> finding_preview`: top visible findings with `candidate_summary`, scenario branches, and uncertainty reasons
- `files.jsonl`: one entry per Lua file with file hash, analysis status, and analysis artifact path
- `analysis/<file_id>.json`: per-file findings and parse status
- `symbol_index/files/<file_id>.json`: per-file symbol facts
- `symbol_index/modules/*.json`: collision-aware logical module index
- `symbol_slices/*.txt`: cached function logic slices for jump and trace
- `trace_bundles/<finding_id>.json`: persisted bounded trace results plus any pre-claim agentic retry metadata
- `findings/<shard_id>.jsonl`: active review shards
- `reviews/<shard_id>.json`: completed shard reviews
- `final/summary.json` and `final/report.md`: merged outputs

## Entry Points

- `scripts/run_review_cycle.py` is the highest-level wrapper and the preferred entry for CodeAgent.
- The wrapper also exposes `status`, and `refresh/claim` accept `--progress` to stream manifest-backed progress updates to stderr.
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

- `analyze` now enumerates `string.find(arg1, ...)` sinks across expression contexts, not just standalone call statements. Assignment RHS, return expressions, `if` tests, and nested expression trees all feed the same finding pipeline.
- Local `non_nil` evidence still suppresses a sink immediately, but `nil`, `maybe_nil`, and `unknown` all become findings so agentic investigation can start.
- `prepare` enriches every unsuppressed non-deterministic finding with a persisted trace bundle before sharding.
- Level `3` findings include unverified function returns and parameter-origin sinks. The tracer now follows both callee returns and caller argument chains within the configured budget.
- `unknown` values are retained as low-confidence obligations. They are not dropped at analysis time; they stay visible unless later trace proves them safe.
- If the first trace still ends in `uncertain` or `budget_exhausted`, `prepare` runs one more strategic pass before claim with a larger trace budget and frontier `jump` summaries attached to the trace bundle.
- Any unsuppressed finding stays human-visible unless trace proves it safe and there is no unresolved external module dependency.
- Safe traces are auto-silenced and counted in `manifest.json -> trace_summary -> auto_silenced`.
- `manifest.json -> trace_summary -> auto_filtered_low_confidence` is a legacy compatibility counter and should normally remain `0` under the evidence-based dismissal policy.
- `manifest.json -> trace_summary` also records `agentic_retraced`, `agentic_improved`, `agentic_promoted_safe`, and `agentic_frontier_jumps`.
- While `stage=sharding`, `manifest.json -> prepare_progress` tells you whether the workflow is still in `trace_enrichment` or has moved to `building_shards`. It is normal for `shards_total` to remain `0` until trace enrichment finishes.
- Every visible finding now carries explicit investigation fields: `candidate_summary`, `candidate_count`, `top_candidate_paths`, `scenario_branches`, `why_still_uncertain`, and `investigation_leads`.
- `final/report.md` surfaces collision branch outcomes as `[path] -> status` lines so scenario-dependent results stay explicit.
