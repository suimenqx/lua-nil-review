# Configuration

## `.lua-nil-review.json`

The analyzer accepts a JSON config file with these keys:

```json
{
  "include": ["*.lua", "**/*.lua"],
  "exclude": ["vendor/**", "third_party/**"],
  "nil_guards": ["assert", "ensure_string"],
  "safe_wrappers": ["safe_string"],
  "suppressions": [
    {"file": "foo.lua", "line": 12, "rule_id": "lua.string-find-first-arg-nil"},
    "stable-finding-id"
  ],
  "baseline": "artifacts/string-find-nil/final/summary.json"
}
```

## Shortest CLI Usage

Use the wrapper when an agent wants a single command entry point. With a workspace-scoped Gemini skill install, the wrapper path is usually `.gemini/skills/lua-nil-review/scripts/run_review_cycle.py` or `.agents/skills/lua-nil-review/scripts/run_review_cycle.py`.

```bash
python .gemini/skills/lua-nil-review/scripts/run_review_cycle.py claim
python .gemini/skills/lua-nil-review/scripts/run_review_cycle.py complete --review-json review.json
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
