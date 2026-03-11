# Configuration

## `.lua-nil-review.json`

The analyzer accepts a JSON config file with these keys:

```json
{
  "include": ["*.lua", "**/*.lua"],
  "exclude": ["vendor/**", "third_party/**"],
  "nil_guards": ["assert", "ensure_string"],
  "safe_wrappers": ["safe_string"],
  "symbol_tracing": {
    "enabled": true,
    "flatten_require_mode": "basename",
    "max_depth": 5,
    "min_required_trace_depth": 3,
    "max_branch_count": 16,
    "max_expanded_nodes": 64,
    "max_unique_slices": 12,
    "slice_mode": "logic_slice",
    "max_slice_lines": 60,
    "agentic_retrace_enabled": true,
    "agentic_retrace_depth_bonus": 4,
    "agentic_retrace_max_branch_count": 32,
    "agentic_retrace_max_expanded_nodes": 192,
    "agentic_frontier_jump_limit": 4,
    "module_resolution_priority": ["src/ui", "src/common"],
    "module_resolution_overrides": {
      "config": ["src/ui/config.lua", "src/net/config.lua"]
    }
  },
  "suppressions": [
    {"file": "foo.lua", "line": 12, "rule_id": "lua.string-find-first-arg-nil"},
    "stable-finding-id"
  ],
  "baseline": "artifacts/string-find-nil/final/summary.json"
}
```

## Risk Tiering Defaults

- `Level 1 / high`: deterministic nil, including literal `nil` and local table reads that provably miss a key.
- `Level 2 / medium`: local unguarded indexed reads such as `info.user.email`.
- `Level 3 / low`: function-return, parameter-origin, or locally unresolved (`unknown`) findings that are unverified before symbol tracing.

Evidence-based dismissal is the current default:

- `analyze` discovers `string.find` sinks in expression contexts, not only standalone call statements
- every unsuppressed finding is retained for review unless trace proves it safe
- bounded tracing runs before sharding so CodeAgent can dismiss safe findings automatically
- if a finding is still `uncertain` or `budget_exhausted`, CodeAgent runs one agentic retry before claim with a deeper trace budget and targeted frontier `jump` expansion
- unresolved or uncertain Level `3` findings stay visible with their trace evidence instead of being hidden

`auto_silence_depth` and `default_visible_risk_levels` are still accepted in config for fingerprint compatibility, but they do not override the evidence-based dismissal policy above.

## Agentic Retrace Controls

- `agentic_retrace_enabled`: run the pre-claim strategic retry for uncertain findings.
- `agentic_retrace_depth_bonus`: how many extra trace levels the second pass receives.
- `agentic_retrace_max_branch_count`: higher branch cap used only during the second pass.
- `agentic_retrace_max_expanded_nodes`: higher node budget used only during the second pass.
- `agentic_frontier_jump_limit`: how many uncertain frontier callsites get an automatic `jump` summary attached to the trace bundle.

## Scenario Resolution

- `module_resolution_overrides` is the strongest control. It pins one logical module key to an ordered list of exact files.
- `module_resolution_priority` is a softer scenario injector. It chooses the first matching path prefix among collision candidates, which is useful for simulating package precedence such as `["src/ui", "src/common"]`.
- When neither rule resolves a collision and different physical paths remain possible, the trace bundle marks `external_config_dependency=true` and keeps per-path branch outcomes.

## Shortest CLI Usage

Use the wrapper when an agent wants a single command entry point. With a workspace-scoped CodeAgent skill install, the wrapper path is usually `.codeagent/skills/lua-nil-review/scripts/run_review_cycle.py`.

```bash
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py refresh --progress
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py claim
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py status
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py complete --review-json review.json
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py build-symbol-index
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py jump --file foo.lua --line 88 --expr Config.get
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py trace --finding-id <finding_id>
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py trace --file foo.lua --line 88 --expr Config.get
```

For direct repository use without skill installation:

```bash
python scripts/run_review_cycle.py refresh --root /path/to/repo --state-dir artifacts/string-find-nil
```

## Dependencies

Install the pinned parser dependency before the first run:

```bash
python -m pip install -r requirements.txt
```

## Review JSON

`scripts/review_shard.py --claim-next` writes a template JSON. Fill it with:

```json
{
  "shard_id": "abc123",
  "reviewer": "codeagent",
  "summary": "Short shard summary.",
  "finding_reviews": [
    {
      "finding_id": "finding-id",
      "decision": "confirm",
      "rationale": "Why the finding is valid.",
      "severity": "medium"
    }
  ]
}
```

Valid decisions for MVP are `confirm`, `dismiss`, and `needs_source_escalation`.

## Output Files

- `final/summary.json` contains counts plus confirmed and escalated findings.
- `final/report.md` is the human-readable review output.
- `suppressed` findings stay in per-file analysis artifacts but do not become review shards.
- `analysis/<file_id>.json` stores `risk_level`, `risk_tier`, `human_review_visible`, and direct investigation fields such as `candidate_summary`, `candidate_count`, `top_candidate_paths`, `scenario_branches`, `why_still_uncertain`, and `investigation_leads`.
- A trace failure on one finding is stored inline as `trace_status=trace_error` plus `trace_error_message`; it no longer aborts the whole prepare phase.
- `trace_bundles/<finding_id>.json` stores bounded cross-function trace output, frontier leads, and pre-claim agentic retry metadata for active findings.
- `trace_bundles/callsite-*.json` stores direct callsite trace output triggered via `trace --file/--line/--expr`.
- `symbol_index/` stores per-file symbol facts and aggregated module collision data.
- `symbol_slices/` stores cached function logic slices used by jump and trace payloads.
- `manifest.json -> analyze_progress` stores live scan feedback such as `findings_discovered`, `parse_errors`, `current_file`, and `recent_findings`.
- `manifest.json -> prepare_progress` stores live counters for `trace_enrichment` and `building_shards`.
- `manifest.json -> trace_summary`, `candidate_overview`, and `finding_preview` now refresh during prepare, so `status` can show partial results before sharding finishes.
