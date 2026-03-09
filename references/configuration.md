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
    "auto_silence_depth": 3,
    "min_required_trace_depth": 3,
    "max_branch_count": 16,
    "max_expanded_nodes": 64,
    "max_unique_slices": 12,
    "slice_mode": "logic_slice",
    "max_slice_lines": 60,
    "module_resolution_priority": ["src/ui", "src/common"],
    "module_resolution_overrides": {
      "config": ["src/ui/config.lua", "src/net/config.lua"]
    },
    "default_visible_risk_levels": [1, 2]
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
- `Level 3 / low`: function-return findings that are unverified before symbol tracing.

By default, only Levels `1` and `2` are human-visible before trace filtering. Level `3` findings must survive bounded tracing with a risky or mixed result before they become review shards.

## Scenario Resolution

- `module_resolution_overrides` is the strongest control. It pins one logical module key to an ordered list of exact files.
- `module_resolution_priority` is a softer scenario injector. It chooses the first matching path prefix among collision candidates, which is useful for simulating package precedence such as `["src/ui", "src/common"]`.
- When neither rule resolves a collision and different physical paths remain possible, the trace bundle marks `external_config_dependency=true` and keeps per-path branch outcomes.

## Shortest CLI Usage

Use the wrapper when an agent wants a single command entry point. With a workspace-scoped Gemini skill install, the wrapper path is usually `.gemini/skills/lua-nil-review/scripts/run_review_cycle.py` or `.agents/skills/lua-nil-review/scripts/run_review_cycle.py`.

```bash
python .gemini/skills/lua-nil-review/scripts/run_review_cycle.py claim
python .gemini/skills/lua-nil-review/scripts/run_review_cycle.py complete --review-json review.json
python .gemini/skills/lua-nil-review/scripts/run_review_cycle.py build-symbol-index
python .gemini/skills/lua-nil-review/scripts/run_review_cycle.py jump --file foo.lua --line 88 --expr Config.get
python .gemini/skills/lua-nil-review/scripts/run_review_cycle.py trace --finding-id <finding_id>
python .gemini/skills/lua-nil-review/scripts/run_review_cycle.py trace --file foo.lua --line 88 --expr Config.get
```

## Review JSON

`scripts/review_shard.py --claim-next` writes a template JSON. Fill it with:

```json
{
  "shard_id": "abc123",
  "reviewer": "codex",
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
- `trace_bundles/<finding_id>.json` stores bounded cross-function trace output for active findings.
- `trace_bundles/callsite-*.json` stores direct callsite trace output triggered via `trace --file/--line/--expr`.
- `symbol_index/` stores per-file symbol facts and aggregated module collision data.
- `symbol_slices/` stores cached function logic slices used by jump and trace payloads.
