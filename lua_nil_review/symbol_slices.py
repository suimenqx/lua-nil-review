from __future__ import annotations

from pathlib import Path

from .common import atomic_write_text, ensure_dir
from .parsed_lua import SourceIndex
from .symbol_models import FunctionSymbol


def build_slice_text(
    function: FunctionSymbol,
    source: SourceIndex,
    *,
    mode: str = "logic_slice",
    max_lines: int = 60,
) -> str:
    if not source.lines:
        return ""
    if mode == "contiguous_body" or function.end_line - function.start_line + 1 <= max_lines:
        return _render_ranges(source, [(function.start_line, min(function.end_line, function.start_line + max_lines - 1))], label=function.qualified_name)

    focus_lines = sorted(set([function.signature_line, *function.key_lines, function.end_line]))
    if mode == "return_focus":
        focus_lines = sorted(set([function.signature_line, *function.key_lines[-3:], function.end_line]))
    ranges = _focus_ranges(focus_lines, start=function.start_line, end=function.end_line, max_lines=max_lines)
    return _render_ranges(source, ranges, label=function.qualified_name)


def ensure_slice_file(
    slices_dir: Path,
    function: FunctionSymbol,
    source: SourceIndex,
    *,
    mode: str = "logic_slice",
    max_lines: int = 60,
) -> str:
    ensure_dir(slices_dir)
    filename = f"{function.slice_id}-{mode}-{max_lines}.txt"
    path = slices_dir / filename
    if not path.exists():
        atomic_write_text(path, build_slice_text(function, source, mode=mode, max_lines=max_lines))
    return f"symbol_slices/{filename}"


def _focus_ranges(lines: list[int], *, start: int, end: int, max_lines: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for line in lines:
        if line < start or line > end:
            continue
        ranges.append((max(start, line - 1), min(end, line + 1)))
    if not ranges:
        return [(start, min(end, start + max_lines - 1))]
    merged = _merge_ranges(ranges)
    total = sum(stop - begin + 1 for begin, stop in merged)
    if total <= max_lines:
        return merged
    trimmed: list[tuple[int, int]] = []
    remaining = max_lines
    for begin, stop in merged:
        if remaining <= 0:
            break
        size = stop - begin + 1
        if size <= remaining:
            trimmed.append((begin, stop))
            remaining -= size
            continue
        trimmed.append((begin, begin + remaining - 1))
        remaining = 0
    return trimmed


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [ordered[0]]
    for begin, stop in ordered[1:]:
        last_begin, last_stop = merged[-1]
        if begin <= last_stop + 1:
            merged[-1] = (last_begin, max(last_stop, stop))
            continue
        merged.append((begin, stop))
    return merged


def _render_ranges(source: SourceIndex, ranges: list[tuple[int, int]], *, label: str) -> str:
    header = f"# {source.relative_path} {label}"
    body: list[str] = []
    for index, (begin, stop) in enumerate(ranges, start=1):
        if index > 1:
            body.append("      | ...")
        for line_no in range(begin, stop + 1):
            body.append(f"{line_no:>6} | {source.line_text(line_no)}")
    return "\n".join([header, *body]) + "\n"
