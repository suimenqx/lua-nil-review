---
name: lua-nil-review
description: Incremental nil-risk code review for very large Lua repositories with persisted state, resumable shards, and Markdown plus JSON outputs. Use when Gemini CLI, Codex, or Claude Code needs to review 3000+ Lua files, 3000+ line Lua files, or continue a long-running Lua audit across sessions, especially for string.find first-argument nil risks, shard-based review, and resumable Lua analysis.
---

# Lua Nil Review

## Overview

Run the persisted pipeline instead of reading a huge Lua repository directly into context. Use the scripts to analyze files, build review shards, claim one shard at a time, and merge reviewed results.

## Installed Skill Paths

Prefer a workspace-scope Gemini install or link. When installed that way, the skill scripts are usually available at one of these paths from the target repository root:

- `.gemini/skills/lua-nil-review/scripts/`
- `.agents/skills/lua-nil-review/scripts/`

If neither path exists, resolve the installed skill location with `gemini skills list --all` and use the reported absolute path to `SKILL.md` to find the sibling `scripts/` directory.

## Workflow

1. Resolve the wrapper path. Prefer `python .gemini/skills/lua-nil-review/scripts/run_review_cycle.py` and fall back to `.agents/skills/lua-nil-review/scripts/run_review_cycle.py`.
2. Use the shortest operator path when possible: `<wrapper> claim`
3. Review only the claimed shard and its snippets. Do not open the whole repository or full giant files unless a finding explicitly needs source escalation.
4. Write a review JSON with `shard_id`, `summary`, and `finding_reviews`, then complete it with `<wrapper> complete --review-json <review.json>`
5. Use the lower-level scripts only when you need to split phases manually:
   - `python <skill-script-dir>/analyze_string_find_nil.py --resume`
   - `python <skill-script-dir>/prepare_review_shards.py --resume`
   - `python <skill-script-dir>/review_shard.py --claim-next`
   - `python <skill-script-dir>/review_shard.py --complete <review.json>`
   - `python <skill-script-dir>/merge_review_results.py`

## Rules

- Review only direct `string.find(arg1, ...)` calls.
- Treat function parameters, function returns, table field reads, and indexed reads as `maybe_nil` unless a guard narrows them.
- Treat `assert(x)` and configured `nil_guards` as narrowing `x` to `non_nil`.
- Keep the review high precision. If the evidence is incomplete, mark the finding as `needs_source_escalation` instead of free-reading a larger code region.

## References

- Read [references/architecture.md](references/architecture.md) for visual diagrams of the runtime layers, persisted artifacts, and adapter surfaces.
- Read [references/workflow.md](references/workflow.md) for the persisted state machine, install modes, and resume behavior.
- Read [references/configuration.md](references/configuration.md) for config keys, wrapper usage, review JSON shape, and output files.
