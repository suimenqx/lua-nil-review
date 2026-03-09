from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import normalize_whitespace
from .config import ReviewConfig, load_config
from .parsed_lua import SourceIndex
from .state import build_layout
from .symbol_index import SymbolRepository
from .symbol_slices import ensure_slice_file

DEFAULT_JUMP_CANDIDATES = 3


def jump_to_definition(
    *,
    root: Path,
    state_dir: Path,
    symbol: str | None = None,
    file: str | None = None,
    line: int | None = None,
    expression: str | None = None,
    config: ReviewConfig | None = None,
    include_all: bool = False,
    expand_token: str | None = None,
) -> dict[str, Any]:
    layout = build_layout(root, state_dir)
    repository = SymbolRepository.load(layout)
    active_config = config or load_config(root, None)[0]

    if symbol:
        refs, used_override, external_config_dependency, resolution_strategy, matched_priority_prefix = _resolve_logical_symbol(
            repository,
            symbol,
            active_config=active_config,
        )
        resolution_kind = "logical_symbol"
        module_key = symbol.rsplit(".", 1)[0] if "." in symbol else symbol
    else:
        if not file or line is None or not expression:
            raise RuntimeError("Jump requires either --symbol or --file/--line/--expr.")
        refs, resolution_kind, module_key, used_override, external_config_dependency, resolution_strategy, matched_priority_prefix = _resolve_callsite_expr(
            repository,
            file,
            line,
            expression,
            active_config=active_config,
        )

    candidates, suppressed, overflow_count = _materialize_candidates(
        repository,
        refs,
        active_config=active_config,
        include_all=include_all or bool(expand_token),
    )
    payload: dict[str, Any] = {
        "resolution_kind": resolution_kind if candidates else "not_found",
        "module_key": module_key,
        "candidates": candidates,
        "suppressed_duplicates": suppressed,
        "used_override": used_override,
        "applied_priority": bool(matched_priority_prefix),
        "matched_priority_prefix": matched_priority_prefix,
        "resolution_strategy": resolution_strategy,
        "external_config_dependency": external_config_dependency,
        "overflow_candidates": overflow_count,
    }
    if overflow_count:
        payload["expand_token"] = f"{module_key or 'symbol'}:all"
        payload["overflow_summary"] = f"{overflow_count} additional candidates omitted by the default jump budget."
    return payload


def _resolve_logical_symbol(
    repository: SymbolRepository,
    symbol: str,
    *,
    active_config: ReviewConfig,
) -> tuple[list[dict[str, Any]], bool, bool, str, str | None]:
    if "." not in symbol:
        globals_doc = repository.globals_doc()
        return list(globals_doc.get(symbol, [])), False, False, "global_symbol", None
    module_key, member_name = symbol.rsplit(".", 1)
    module_doc = repository.module_doc(module_key)
    refs = list(module_doc.get("members", {}).get(member_name, []))
    return _apply_module_resolution(refs, module_key, active_config=active_config)


def _resolve_callsite_expr(
    repository: SymbolRepository,
    file: str,
    line: int,
    expression: str,
    *,
    active_config: ReviewConfig,
) -> tuple[list[dict[str, Any]], str, str | None, bool, bool, str, str | None]:
    normalized = expression.replace(":", ".").strip()
    facts = repository.file_facts(file)
    if facts is None:
        return [], "unknown_file", None, False, False, "unknown_file", None

    function_id = None
    for function in facts.functions:
        if function.start_line <= line <= function.end_line:
            function_id = function.function_id
            break

    if "." not in normalized:
        local_refs = [
            _function_ref(function)
            for function in facts.functions
            if function.local_name == normalized or function.qualified_name == normalized
        ]
        if local_refs:
            return local_refs, "same_file_local", None, False, False, "same_file_local", None
        return list(repository.globals_doc().get(normalized, [])), "global_function", None, False, False, "global_function", None

    receiver_name, member_name = normalized.rsplit(".", 1)
    if receiver_name in facts.module_table_names:
        refs = [
            _function_ref(function)
            for function in facts.functions
            if function.receiver_name == receiver_name and function.member_name == member_name
        ]
        return refs, "same_file_module_table", facts.logical_module_keys[0] if facts.logical_module_keys else None, False, False, "same_file_module_table", None

    visible_module_keys: list[str] = []
    for binding in facts.require_bindings:
        if binding.alias_name != receiver_name or binding.line > line:
            continue
        if binding.scope_function_id and binding.scope_function_id != function_id:
            continue
        visible_module_keys.extend(binding.module_keys)
    visible_module_keys = list(dict.fromkeys(visible_module_keys))

    refs: list[dict[str, Any]] = []
    used_override = False
    external_config_dependency = False
    resolution_strategy = "require_alias"
    matched_priority_prefix: str | None = None
    for module_key in visible_module_keys:
        resolved, module_override, module_external, module_strategy, matched_prefix = _resolve_logical_symbol(
            repository,
            f"{module_key}.{member_name}",
            active_config=active_config,
        )
        refs.extend(resolved)
        used_override = used_override or module_override
        external_config_dependency = external_config_dependency or module_external
        if module_strategy != "direct":
            resolution_strategy = module_strategy
        matched_priority_prefix = matched_priority_prefix or matched_prefix
    if refs:
        resolution_kind = "collision_multi_candidate" if len({item['file'] for item in refs}) > 1 else "require_alias"
        return refs, resolution_kind, visible_module_keys[0], used_override, external_config_dependency, resolution_strategy, matched_priority_prefix

    refs, used_override, external_config_dependency, resolution_strategy, matched_priority_prefix = _resolve_logical_symbol(
        repository,
        normalized,
        active_config=active_config,
    )
    if refs:
        return refs, "logical_symbol", receiver_name, used_override, external_config_dependency, resolution_strategy, matched_priority_prefix
    return [], "unresolved", receiver_name, False, False, "unresolved", None


def _apply_module_resolution(
    refs: list[dict[str, Any]],
    module_key: str,
    *,
    active_config: ReviewConfig,
) -> tuple[list[dict[str, Any]], bool, bool, str, str | None]:
    if not refs:
        return [], False, False, "unresolved", None
    overrides = active_config.symbol_tracing.module_resolution_overrides.get(module_key, [])
    if overrides:
        order = {path: index for index, path in enumerate(overrides)}
        filtered = [ref for ref in refs if ref["file"] in order]
        if filtered:
            filtered.sort(key=lambda item: (order[item["file"]], item["file"], item["function_id"]))
            return filtered, True, False, "override", None
    for prefix in active_config.symbol_tracing.module_resolution_priority:
        filtered = [ref for ref in refs if _matches_priority_prefix(ref["file"], prefix)]
        if filtered:
            filtered.sort(key=lambda item: (item["file"], item["qualified_name"], item["function_id"]))
            return filtered, False, False, "priority_prefix", prefix
    refs.sort(key=lambda item: (item["file"], item["qualified_name"], item["function_id"]))
    if len({item["file"] for item in refs}) > 1:
        return refs, False, True, "collision_ambiguous", None
    return refs, False, False, "direct", None


def _matches_priority_prefix(relative_path: str, prefix: str) -> bool:
    normalized = prefix.rstrip("/")
    return relative_path == normalized or relative_path.startswith(f"{normalized}/")


def _materialize_candidates(
    repository: SymbolRepository,
    refs: list[dict[str, Any]],
    *,
    active_config: ReviewConfig,
    include_all: bool,
) -> tuple[list[dict[str, Any]], int, int]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for ref in refs:
        grouped.setdefault(ref.get("logic_hash") or ref["function_id"], []).append(ref)

    all_groups = list(grouped.values())
    visible_groups = all_groups if include_all else all_groups[:DEFAULT_JUMP_CANDIDATES]
    overflow_count = max(0, len(all_groups) - len(visible_groups))

    candidates: list[dict[str, Any]] = []
    suppressed = 0
    for group in visible_groups:
        representative = group[0]
        function = repository.function_symbol(representative["file"], representative["function_id"])
        if function is None:
            continue
        source_text = (repository.layout.root / representative["file"]).read_text(encoding="utf-8", errors="replace")
        slice_path = ensure_slice_file(
            repository.layout.symbol_slices_dir,
            function,
            SourceIndex(representative["file"], source_text),
            mode=active_config.symbol_tracing.slice_mode,
            max_lines=active_config.symbol_tracing.max_slice_lines,
        )
        duplicate_files = sorted({item["file"] for item in group})
        suppressed += max(0, len(duplicate_files) - 1)
        candidates.append(
            {
                "file": representative["file"],
                "function_id": representative["function_id"],
                "qualified_name": representative.get("qualified_name", representative.get("local_name", "")),
                "exported_as": representative.get("exported_as", []),
                "slice_path": slice_path,
                "logic_hash": representative.get("logic_hash"),
                "duplicate_files": duplicate_files,
                "return_summary": representative.get("return_summary"),
            }
        )
    candidates.sort(key=lambda item: (item["file"], item["qualified_name"]))
    return candidates, suppressed, overflow_count


def _function_ref(function) -> dict[str, Any]:
    return {
        "function_id": function.function_id,
        "file": function.file,
        "qualified_name": function.qualified_name,
        "local_name": function.local_name,
        "logic_hash": function.logic_hash,
        "start_line": function.start_line,
        "end_line": function.end_line,
        "exported_as": list(function.exported_as),
        "slice_id": function.slice_id,
        "receiver_name": function.receiver_name,
        "member_name": function.member_name,
        "return_summary": function.return_summary.to_dict() if function.return_summary else None,
        "key_lines": list(function.key_lines),
    }


def candidate_slice_content(root: Path, state_dir: Path, relative_slice_path: str) -> str | None:
    layout = build_layout(root, state_dir)
    path = layout.state_dir / relative_slice_path
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def summarize_jump_target(candidate: dict[str, Any]) -> str:
    exported = ", ".join(candidate.get("exported_as", [])) or candidate.get("qualified_name", "")
    return normalize_whitespace(f"{candidate['file']} {exported}")
