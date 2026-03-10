from __future__ import annotations

from typing import Any, Iterator

try:
    import luaparser.astnodes as N
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency 'luaparser'. Install dependencies with `python -m pip install -r requirements.txt`."
    ) from exc


_PRIMITIVE_TYPES = (str, int, float, bool, bytes)


def iter_statement_expression_roots(statement: Any) -> list[Any]:
    roots: list[Any] = []
    if isinstance(statement, N.Call):
        roots.append(statement)
    elif isinstance(statement, (N.Assign, N.LocalAssign)):
        roots.extend(getattr(statement, "targets", []))
        roots.extend(getattr(statement, "values", []))
    elif isinstance(statement, N.Return):
        roots.extend(getattr(statement, "values", []))
    elif isinstance(statement, (N.If, N.ElseIf, N.While, N.Repeat)):
        test = getattr(statement, "test", None)
        if test is not None:
            roots.append(test)
    elif isinstance(statement, N.Fornum):
        for attr in ("start", "stop", "step"):
            value = getattr(statement, attr, None)
            if value is not None:
                roots.append(value)
    elif isinstance(statement, N.Forin):
        for attr in ("iter", "iterators", "values"):
            value = getattr(statement, attr, None)
            if isinstance(value, list):
                roots.extend(value)
            elif value is not None:
                roots.append(value)
    return roots


def iter_call_expressions(node: Any) -> Iterator[Any]:
    seen: set[int] = set()
    yield from _iter_call_expressions(node, seen)


def _iter_call_expressions(node: Any, seen: set[int]) -> Iterator[Any]:
    if node is None or isinstance(node, _PRIMITIVE_TYPES):
        return
    if isinstance(node, (list, tuple, set)):
        for item in node:
            yield from _iter_call_expressions(item, seen)
        return
    if isinstance(node, (N.Function, N.LocalFunction, N.AnonymousFunction)):
        return
    if not hasattr(node, "__dict__"):
        return
    marker = id(node)
    if marker in seen:
        return
    seen.add(marker)
    if isinstance(node, N.Call):
        yield node
    for value in vars(node).values():
        yield from _iter_call_expressions(value, seen)
