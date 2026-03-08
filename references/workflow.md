# Workflow

## Persisted State

The pipeline writes all durable state under `artifacts/string-find-nil/`.

- `manifest.json`: run metadata, progress counters, shard statuses, and lock owner
- `files.jsonl`: one entry per Lua file with file hash, analysis status, and analysis artifact path
- `analysis/<file_id>.json`: per-file findings and parse status
- `findings/<shard_id>.jsonl`: active review shards
- `reviews/<shard_id>.json`: completed shard reviews
- `final/summary.json` and `final/report.md`: merged outputs

## Entry Points

- `scripts/run_review_cycle.py` is the highest-level wrapper and the preferred entry for Gemini CLI.
- `scripts/analyze_string_find_nil.py`, `scripts/prepare_review_shards.py`, `scripts/review_shard.py`, and `scripts/merge_review_results.py` remain available for manual phase control.
- `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md` are generated from a shared source by `python scripts/generate_adapter_docs.py`.
- `agents/openai.yaml` is OpenAI/Codex UI metadata. It is not a Gemini or Claude adapter file.

## Gemini Skill Install

Prefer `gemini skills link <repo> --scope workspace` or `gemini skills install <repo> --scope workspace` for this skill. Workspace scope keeps the skill scripts at predictable paths inside the target repository:

- `.gemini/skills/lua-nil-review/`
- `.agents/skills/lua-nil-review/`

If the skill is installed at user scope instead, resolve the actual install path with `gemini skills list --all` before invoking the bundled scripts.

## Resume Rules

- Reuse an analysis result when the file content hash and analysis fingerprint match.
- Reset `analysis/`, `findings/`, `snippets/`, and `final/` when the analysis fingerprint changes.
- Reclaim a shard from `in_review` back to `pending` when its heartbeat is older than 30 minutes.
- Reuse an existing review only when the shard ID is unchanged.

## Review Discipline

- Claim one shard at a time.
- Read only the shard payload and snippet files.
- Mark a finding as `needs_source_escalation` when the snippet set is insufficient.
- Do not read an entire 3000+ line file unless escalation is necessary.
