---
name: lua-nil-review
description: Incremental nil-risk code review for very large Lua repositories with persisted state, resumable shards, and Markdown plus JSON outputs. Use when CodeAgent needs to review 3000+ Lua files, 3000+ line Lua files, or continue a long-running Lua audit across sessions, especially for string.find first-argument nil risks, shard-based review, symbol tracing, and resumable Lua analysis.
---

# Lua Nil Review

## Overview

Run the persisted pipeline instead of reading a huge Lua repository directly into context. Use the scripts to analyze files, build review shards, claim one shard at a time, and merge reviewed results.

## Installed Skill Paths

Prefer a workspace-scope CodeAgent install or link. When installed that way, the skill scripts are usually available at this path from the target repository root:

- `.codeagent/skills/lua-nil-review/scripts/`

If that path does not exist, resolve the installed skill location with `codeagent skills list --all` and use the reported absolute path to `SKILL.md` to find the sibling `scripts/` directory.

## Workflow

1. Resolve the wrapper path. Prefer `python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py`.
2. In normal use, let the user talk to CodeAgent in natural language, for example asking it to refresh the current repository, claim a shard, and deepen review with `jump` or `trace` when needed.
3. Use the shortest operator path when possible: `<wrapper> claim`
4. Review only the claimed shard and its snippets. Do not open the whole repository or full giant files unless a finding explicitly needs source escalation.
5. Write a review JSON with `shard_id`, `summary`, and `finding_reviews`, then complete it with `<wrapper> complete --review-json <review.json>`
6. Use the lower-level scripts only when you need to split phases manually:
   - `python <skill-script-dir>/analyze_string_find_nil.py --resume`
   - `python <skill-script-dir>/prepare_review_shards.py --resume`
   - `python <skill-script-dir>/review_shard.py --claim-next`
   - `python <skill-script-dir>/review_shard.py --complete <review.json>`
   - `python <skill-script-dir>/merge_review_results.py`

## Rules

- Review only direct `string.find(arg1, ...)` calls.
- Risk tiering is enabled by default: deterministic nil is Level 1, local unguarded indexed reads are Level 2, and cross-function return values start as Level 3 until tracing proves risk.
- Level 3 findings do not enter human review by default. The pipeline must trace them before deciding whether they stay visible.
- Treat `assert(x)` and configured `nil_guards` as narrowing `x` to `non_nil`.
- Use `jump` and `trace` instead of free-reading the repository when a symbol or return path needs follow-up.
- Keep the review high precision. If the evidence is incomplete, mark the finding as `needs_source_escalation` instead of free-reading a larger code region.

## References

- Start with [references/codeagent_getting_started.md](references/codeagent_getting_started.md) if you want a beginner-friendly install and usage guide.
- Read [references/architecture.md](references/architecture.md) for visual diagrams of the runtime layers, persisted artifacts, and adapter surfaces.
- Read [references/workflow.md](references/workflow.md) for the persisted state machine, install mode, trace gating, and resume behavior.
- Read [references/configuration.md](references/configuration.md) for config keys, wrapper usage, review JSON shape, and output files.
