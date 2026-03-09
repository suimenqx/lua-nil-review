from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from luaparser import ast
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency 'luaparser'. Install dependencies with `python -m pip install -r requirements.txt`."
    ) from exc


class SourceIndex:
    def __init__(self, relative_path: str, text: str) -> None:
        self.relative_path = relative_path
        self.text = text
        self.lines = text.splitlines()

    def line_text(self, line_number: int) -> str:
        if not self.lines:
            return ""
        index = max(1, min(line_number, len(self.lines))) - 1
        return self.lines[index]

    def node_text(self, node: Any) -> str:
        start = self.node_start(node)
        stop = self.node_stop(node)
        if start is None or stop is None:
            return ""
        return self.text[start : stop + 1]

    def node_start(self, node: Any) -> int | None:
        token = getattr(node, "first_token", None)
        return token.start if token else None

    def node_stop(self, node: Any) -> int | None:
        token = getattr(node, "last_token", None)
        return token.stop if token else None

    def line_column_for_node(self, node: Any) -> tuple[int, int]:
        token = getattr(node, "first_token", None)
        if token:
            return int(token.line), int(token.column) + 1
        return 1, 1

    def line_range_for_node(self, node: Any) -> tuple[int, int]:
        start = getattr(getattr(node, "first_token", None), "line", 1)
        stop = getattr(getattr(node, "last_token", None), "line", start)
        return int(start), int(stop)

    def snippet(self, center_line: int, *, radius: int, max_lines: int, label: str | None = None) -> str:
        if not self.lines:
            return ""
        start_line = max(1, center_line - radius)
        end_line = min(len(self.lines), center_line + radius)
        if end_line - start_line + 1 > max_lines:
            end_line = start_line + max_lines - 1
        header = f"# {self.relative_path}:{start_line}-{end_line}"
        if label:
            header = f"{header} {label}"
        body = [f"{line_no:>6} | {self.line_text(line_no)}" for line_no in range(start_line, end_line + 1)]
        return "\n".join([header, *body]) + "\n"


@dataclass(frozen=True)
class ParsedLuaFile:
    relative_path: str
    text: str
    root: Any
    source: SourceIndex


def parse_lua_file(relative_path: str, text: str) -> ParsedLuaFile:
    return ParsedLuaFile(
        relative_path=relative_path,
        text=text,
        root=ast.parse(text),
        source=SourceIndex(relative_path, text),
    )
