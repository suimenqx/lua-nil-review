from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import luaparser.astnodes as N
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency 'luaparser'. Install dependencies with `python -m pip install -r requirements.txt`."
    ) from exc

from .common import normalize_whitespace, sha256_hex
from .config import ReviewConfig
from .parsed_lua import ParsedLuaFile, parse_lua_file
from .symbol_models import CallEdge, ExportBinding, FileSymbolFacts, FunctionSymbol, RequireBinding, ReturnDependency, ReturnSummary

_MODULE_DECLARATION_RE = re.compile(
    r"^\s*module\s*\(\s*(['\"])([^'\"]+)\1(?:\s*,\s*package\.seeall)?\s*\)\s*$"
)


@dataclass
class _PendingReceiverExport:
    receiver_name: str
    member_name: str
    function_id: str
    line: int
    binding_kind: str


class SymbolExtractionError(RuntimeError):
    pass


class SymbolExtractor:
    def __init__(self, parsed_file: ParsedLuaFile, config: ReviewConfig) -> None:
        self.parsed_file = parsed_file
        self.relative_path = parsed_file.relative_path
        self.source = parsed_file.source
        self.config = config

        self.declared_module_keys = self._detect_declared_module_keys(parsed_file.text)
        basename = Path(self.relative_path).stem
        exact_module_key = self.relative_path[:-4].replace("/", ".") if self.relative_path.endswith(".lua") else self.relative_path.replace("/", ".")
        default_keys = [exact_module_key] if self.config.symbol_tracing.flatten_require_mode == "exact" else [basename]
        self.logical_module_keys = list(dict.fromkeys([*default_keys, basename, *self.declared_module_keys]))
        self.module_table_names: set[str] = set()
        self.require_bindings: list[RequireBinding] = []
        self.export_bindings: list[ExportBinding] = []
        self.functions: list[FunctionSymbol] = []
        self.global_functions: set[str] = set()

        self._function_nodes: dict[str, Any] = {}
        self._functions_by_local_name: dict[str, list[str]] = {}
        self._functions_by_qualified_name: dict[str, list[str]] = {}
        self._pending_receiver_exports: list[_PendingReceiverExport] = []
        self._top_level_returns: list[Any] = []
        self._seen_exports: set[tuple[str, str, str]] = set()

    def extract(self, *, file_id: str, content_hash: str, symbol_fingerprint: str) -> FileSymbolFacts:
        self._scan_block(self.parsed_file.root.body.body, scope_function_id=None, top_level=True)
        self._finalize_exports()
        self._finalize_function_details()
        return FileSymbolFacts(
            file=self.relative_path,
            file_id=file_id,
            content_hash=content_hash,
            symbol_fingerprint=symbol_fingerprint,
            parse_status="ok",
            parse_error=None,
            logical_module_keys=self.logical_module_keys,
            declared_module_keys=self.declared_module_keys,
            module_table_names=sorted(self.module_table_names),
            require_bindings=self.require_bindings,
            export_bindings=self.export_bindings,
            functions=self.functions,
            global_functions=sorted(self.global_functions),
        )

    def _detect_declared_module_keys(self, text: str) -> list[str]:
        keys: list[str] = []
        for line in text.splitlines():
            code = line.split("--", 1)[0].strip()
            if not code:
                continue
            match = _MODULE_DECLARATION_RE.match(code)
            if match is not None:
                keys.append(match.group(2))
        return list(dict.fromkeys(keys))

    def _scan_block(self, statements: list[Any], *, scope_function_id: str | None, top_level: bool) -> None:
        for statement in statements:
            if isinstance(statement, N.LocalFunction):
                function_id = self._register_function(
                    statement,
                    local_name=statement.name.id,
                    qualified_name=statement.name.id,
                    visibility="local",
                )
                self._scan_block(statement.body.body, scope_function_id=function_id, top_level=False)
                continue
            if isinstance(statement, N.Function):
                qualified_name = self._qualified_name(statement.name) or self._fallback_function_name(statement.name)
                receiver_name, member_name = self._receiver_and_member(statement.name)
                function_id = self._register_function(
                    statement,
                    local_name=self._short_name(qualified_name),
                    qualified_name=qualified_name,
                    visibility="global" if isinstance(statement.name, N.Name) else "table_member",
                    receiver_name=receiver_name,
                    member_name=member_name,
                )
                if receiver_name and member_name and top_level:
                    self._pending_receiver_exports.append(
                        _PendingReceiverExport(
                            receiver_name=receiver_name,
                            member_name=member_name,
                            function_id=function_id,
                            line=self.source.line_column_for_node(statement)[0],
                            binding_kind="table_field_export",
                        )
                    )
                self._scan_block(statement.body.body, scope_function_id=function_id, top_level=False)
                continue
            if isinstance(statement, (N.LocalAssign, N.Assign)):
                self._collect_assignment_bindings(statement, scope_function_id=scope_function_id, top_level=top_level)
                continue
            if top_level and isinstance(statement, N.Call):
                binding = self._require_binding_for_bare_call(statement)
                if binding is not None:
                    self.require_bindings.append(binding)
                continue
            if isinstance(statement, N.Return):
                if top_level:
                    self._top_level_returns.append(statement)
                continue
            if isinstance(statement, N.If):
                self._scan_nested_block(statement.body, scope_function_id=scope_function_id)
                self._scan_else(statement.orelse, scope_function_id=scope_function_id)
                continue
            if isinstance(statement, N.Do):
                self._scan_nested_block(statement.body, scope_function_id=scope_function_id)
                continue
            if isinstance(statement, (N.While, N.Repeat, N.Fornum, N.Forin)):
                body = getattr(statement, "body", None)
                self._scan_nested_block(body, scope_function_id=scope_function_id)

    def _scan_nested_block(self, body: Any, *, scope_function_id: str | None) -> None:
        if isinstance(body, N.Block):
            self._scan_block(body.body, scope_function_id=scope_function_id, top_level=False)

    def _scan_else(self, node: Any, *, scope_function_id: str | None) -> None:
        if node is None:
            return
        if isinstance(node, N.Block):
            self._scan_block(node.body, scope_function_id=scope_function_id, top_level=False)
            return
        if isinstance(node, N.ElseIf):
            self._scan_block(node.body.body, scope_function_id=scope_function_id, top_level=False)
            self._scan_else(node.orelse, scope_function_id=scope_function_id)

    def _collect_assignment_bindings(self, statement: Any, *, scope_function_id: str | None, top_level: bool) -> None:
        targets = getattr(statement, "targets", [])
        values = getattr(statement, "values", [])
        for index, target in enumerate(targets):
            value = values[index] if index < len(values) else None
            if top_level and isinstance(target, N.Name) and isinstance(value, N.Table):
                self.module_table_names.add(target.id)
            binding = self._require_binding_for_assignment(target, value, scope_function_id)
            if binding is not None:
                self.require_bindings.append(binding)
            if isinstance(value, N.AnonymousFunction):
                receiver_name = None
                member_name = None
                qualified_name = ""
                visibility = "local"
                if isinstance(target, N.Name):
                    qualified_name = target.id
                    visibility = "local" if isinstance(statement, N.LocalAssign) else "global"
                elif isinstance(target, N.Index):
                    qualified_name = self._qualified_name(target) or self._fallback_function_name(target)
                    receiver_name, member_name = self._receiver_and_member(target)
                    visibility = "table_member"
                else:
                    qualified_name = self._fallback_function_name(value)
                function_id = self._register_function(
                    value,
                    local_name=self._short_name(qualified_name),
                    qualified_name=qualified_name,
                    visibility=visibility,
                    receiver_name=receiver_name,
                    member_name=member_name,
                )
                if top_level and receiver_name and member_name:
                    line, _column = self.source.line_column_for_node(target)
                    self._pending_receiver_exports.append(
                        _PendingReceiverExport(
                            receiver_name=receiver_name,
                            member_name=member_name,
                            function_id=function_id,
                            line=line,
                            binding_kind="table_field_export",
                        )
                    )
                self._scan_block(value.body.body, scope_function_id=function_id, top_level=False)
                continue
            if top_level and isinstance(target, N.Index) and isinstance(value, N.Name):
                receiver_name, member_name = self._receiver_and_member(target)
                if receiver_name and member_name:
                    function_id = self._first_function_id_for_name(value.id)
                    if function_id:
                        line, _column = self.source.line_column_for_node(target)
                        self._pending_receiver_exports.append(
                            _PendingReceiverExport(
                                receiver_name=receiver_name,
                                member_name=member_name,
                                function_id=function_id,
                                line=line,
                                binding_kind="table_field_export",
                            )
                        )

    def _require_binding_for_assignment(self, target: Any, value: Any, scope_function_id: str | None) -> RequireBinding | None:
        if not isinstance(target, N.Name):
            return None
        if not isinstance(value, N.Call):
            return None
        if self._qualified_name(value.func) != "require" or not value.args:
            return None
        first = value.args[0]
        if not isinstance(first, N.String):
            return None
        line, _column = self.source.line_column_for_node(value)
        module_key = first.raw or first.s.decode("utf-8", errors="replace")
        return RequireBinding(
            alias_name=target.id,
            module_keys=self._module_keys_for_require(module_key),
            binding_kind="local_require" if scope_function_id else "top_level_require",
            line=line,
            scope_function_id=scope_function_id,
        )

    def _require_binding_for_bare_call(self, call: Any) -> RequireBinding | None:
        if self._qualified_name(call.func) != "require" or not call.args:
            return None
        first = call.args[0]
        if not isinstance(first, N.String):
            return None
        module_key = first.raw or first.s.decode("utf-8", errors="replace")
        line, _column = self.source.line_column_for_node(call)
        return RequireBinding(
            alias_name=module_key.split(".")[-1],
            module_keys=self._module_keys_for_require(module_key),
            binding_kind="bare_require",
            line=line,
            scope_function_id=None,
        )

    def _module_keys_for_require(self, module_key: str) -> list[str]:
        if self.config.symbol_tracing.flatten_require_mode == "exact":
            return [module_key]
        return list(dict.fromkeys([module_key.split(".")[-1], module_key]))

    def _register_function(
        self,
        node: Any,
        *,
        local_name: str,
        qualified_name: str,
        visibility: str,
        receiver_name: str | None = None,
        member_name: str | None = None,
    ) -> str:
        start_line, end_line = self.source.line_range_for_node(node)
        line, column = self.source.line_column_for_node(node)
        start_offset = self.source.node_start(node) or 0
        end_offset = self.source.node_stop(node) or start_offset
        normalized = normalize_whitespace(self.source.node_text(node))
        logic_hash = self._logic_hash(node, normalized)
        function_id = sha256_hex(f"{self.relative_path}|{qualified_name}|{start_line}|{start_offset}")
        symbol = FunctionSymbol(
            function_id=function_id,
            file=self.relative_path,
            local_name=local_name,
            qualified_name=qualified_name,
            visibility=visibility,
            start_line=start_line,
            end_line=end_line,
            start_offset=start_offset,
            end_offset=end_offset,
            signature_line=line,
            signature_column=column,
            logic_hash=logic_hash,
            slice_id=sha256_hex(f"{function_id}|logic_slice"),
            param_names=[arg.id for arg in getattr(node, "args", []) if isinstance(arg, N.Name)],
            receiver_name=receiver_name,
            member_name=member_name,
            key_lines=[line],
        )
        self.functions.append(symbol)
        self._function_nodes[function_id] = node
        self._functions_by_local_name.setdefault(local_name, []).append(function_id)
        self._functions_by_qualified_name.setdefault(qualified_name, []).append(function_id)
        if visibility == "global" and "." not in qualified_name:
            self.global_functions.add(local_name)
        return function_id

    def _finalize_exports(self) -> None:
        for statement in self._top_level_returns:
            if not statement.values:
                continue
            returned = statement.values[0]
            if isinstance(returned, N.Name):
                self._export_receiver_table(returned.id)
                continue
            if isinstance(returned, N.Table):
                self._export_return_table(returned)
        if self.declared_module_keys:
            for function in self.functions:
                if function.visibility != "global" or "." in function.qualified_name:
                    continue
                for module_key in self.logical_module_keys:
                    self._add_export(
                        function_id=function.function_id,
                        module_key=module_key,
                        member_name=function.local_name,
                        binding_kind="global_export",
                        line=function.start_line,
                        receiver_name=None,
                    )

    def _export_receiver_table(self, receiver_name: str) -> None:
        for pending in self._pending_receiver_exports:
            if pending.receiver_name != receiver_name:
                continue
            for module_key in self.logical_module_keys:
                self._add_export(
                    function_id=pending.function_id,
                    module_key=module_key,
                    member_name=pending.member_name,
                    binding_kind=pending.binding_kind,
                    line=pending.line,
                    receiver_name=receiver_name,
                )

    def _export_return_table(self, table_node: N.Table) -> None:
        for field in getattr(table_node, "fields", []):
            if not isinstance(field, N.Field):
                continue
            if not isinstance(field.key, N.Name):
                continue
            member_name = field.key.id
            function_id = None
            if isinstance(field.value, N.Name):
                function_id = self._first_function_id_for_name(field.value.id)
            elif isinstance(field.value, N.AnonymousFunction):
                function_id = self._register_function(
                    field.value,
                    local_name=member_name,
                    qualified_name=member_name,
                    visibility="local",
                )
                self._scan_block(field.value.body.body, scope_function_id=function_id, top_level=False)
            elif isinstance(field.value, N.Index):
                qualified = self._qualified_name(field.value)
                if qualified:
                    function_ids = self._functions_by_qualified_name.get(qualified, [])
                    function_id = function_ids[0] if function_ids else None
            if not function_id:
                continue
            line, _column = self.source.line_column_for_node(field)
            for module_key in self.logical_module_keys:
                self._add_export(
                    function_id=function_id,
                    module_key=module_key,
                    member_name=member_name,
                    binding_kind="return_table_export",
                    line=line,
                    receiver_name=None,
                )

    def _add_export(
        self,
        *,
        function_id: str,
        module_key: str,
        member_name: str,
        binding_kind: str,
        line: int,
        receiver_name: str | None,
    ) -> None:
        key = (function_id, module_key, member_name)
        if key in self._seen_exports:
            return
        self._seen_exports.add(key)
        self.export_bindings.append(
            ExportBinding(
                module_key=module_key,
                member_name=member_name,
                binding_kind=binding_kind,
                function_id=function_id,
                source_line=line,
                receiver_name=receiver_name,
            )
        )
        function = next((item for item in self.functions if item.function_id == function_id), None)
        if function is not None:
            exported_name = f"{module_key}.{member_name}"
            if exported_name not in function.exported_as:
                function.exported_as.append(exported_name)
            if line not in function.key_lines:
                function.key_lines.append(line)

    def _finalize_function_details(self) -> None:
        for function in self.functions:
            node = self._function_nodes[function.function_id]
            function.call_edges = self._collect_call_edges(function.function_id, node)
            return_summary = self._summarize_function_returns(node, function)
            function.return_summary = return_summary
            for edge in function.call_edges:
                if edge.line not in function.key_lines:
                    function.key_lines.append(edge.line)
            if return_summary and return_summary.line and return_summary.line not in function.key_lines:
                function.key_lines.append(return_summary.line)
            function.key_lines = sorted(set(function.key_lines))

    def _collect_call_edges(self, function_id: str, node: Any) -> list[CallEdge]:
        calls: list[CallEdge] = []
        self._collect_calls_from_block(node.body, function_id=function_id, calls=calls)
        return sorted(calls, key=lambda item: item.line)

    def _collect_calls_from_block(self, block: Any, *, function_id: str, calls: list[CallEdge]) -> None:
        statements = block.body if isinstance(block, N.Block) else []
        for statement in statements:
            self._collect_calls_from_statement(statement, function_id=function_id, calls=calls)

    def _collect_calls_from_statement(self, statement: Any, *, function_id: str, calls: list[CallEdge]) -> None:
        if isinstance(statement, N.Call):
            edge = self._call_edge(function_id, statement, returns_used=False)
            if edge is not None:
                calls.append(edge)
            return
        if isinstance(statement, (N.Assign, N.LocalAssign)):
            for value in getattr(statement, "values", []):
                self._collect_calls_from_expr(value, function_id=function_id, calls=calls, returns_used=True)
            return
        if isinstance(statement, N.Return):
            for value in getattr(statement, "values", []):
                self._collect_calls_from_expr(value, function_id=function_id, calls=calls, returns_used=True)
            return
        if isinstance(statement, N.If):
            self._collect_calls_from_expr(statement.test, function_id=function_id, calls=calls, returns_used=False)
            self._collect_calls_from_block(statement.body, function_id=function_id, calls=calls)
            self._collect_calls_from_else(statement.orelse, function_id=function_id, calls=calls)
            return
        if isinstance(statement, N.Do):
            self._collect_calls_from_block(statement.body, function_id=function_id, calls=calls)
            return
        if isinstance(statement, (N.While, N.Repeat)):
            test = getattr(statement, "test", None)
            if test is not None:
                self._collect_calls_from_expr(test, function_id=function_id, calls=calls, returns_used=False)
            self._collect_calls_from_block(statement.body, function_id=function_id, calls=calls)
            return
        if isinstance(statement, (N.Fornum, N.Forin)):
            self._collect_calls_from_block(statement.body, function_id=function_id, calls=calls)

    def _collect_calls_from_else(self, node: Any, *, function_id: str, calls: list[CallEdge]) -> None:
        if node is None:
            return
        if isinstance(node, N.Block):
            self._collect_calls_from_block(node, function_id=function_id, calls=calls)
            return
        if isinstance(node, N.ElseIf):
            self._collect_calls_from_expr(node.test, function_id=function_id, calls=calls, returns_used=False)
            self._collect_calls_from_block(node.body, function_id=function_id, calls=calls)
            self._collect_calls_from_else(node.orelse, function_id=function_id, calls=calls)

    def _collect_calls_from_expr(self, expr: Any, *, function_id: str, calls: list[CallEdge], returns_used: bool) -> None:
        if isinstance(expr, N.Call):
            edge = self._call_edge(function_id, expr, returns_used=returns_used)
            if edge is not None:
                calls.append(edge)
            for arg in getattr(expr, "args", []):
                self._collect_calls_from_expr(arg, function_id=function_id, calls=calls, returns_used=False)
            return
        for child in self._expr_children(expr):
            self._collect_calls_from_expr(child, function_id=function_id, calls=calls, returns_used=False)

    def _expr_children(self, expr: Any) -> list[Any]:
        children: list[Any] = []
        for attr in ("left", "right", "value", "idx", "operand"):
            child = getattr(expr, attr, None)
            if child is not None:
                children.append(child)
        for attr in ("values", "args", "fields"):
            child = getattr(expr, attr, None)
            if isinstance(child, list):
                for item in child:
                    if isinstance(item, N.Field):
                        children.append(item.value)
                    else:
                        children.append(item)
        return children

    def _call_edge(self, function_id: str, call: N.Call, *, returns_used: bool) -> CallEdge | None:
        line, _column = self.source.line_column_for_node(call)
        callee_expr = self._qualified_name(call.func) or normalize_whitespace(self.source.node_text(call.func))
        receiver_name, member_name = self._receiver_and_member(call.func)
        if receiver_name and member_name:
            callee_kind = "module_member"
        elif isinstance(call.func, N.Name):
            callee_kind = "local_function"
        else:
            callee_kind = "dynamic_unknown"
        resolved_targets = self._resolved_targets_for_call(function_id, line, receiver_name, member_name, callee_expr, callee_kind)
        return CallEdge(
            caller_function_id=function_id,
            line=line,
            callee_expr=callee_expr,
            callee_kind=callee_kind,
            receiver_name=receiver_name,
            member_name=member_name,
            arg_exprs=[normalize_whitespace(self.source.node_text(arg)) for arg in getattr(call, "args", [])],
            returns_used=returns_used,
            resolved_targets=resolved_targets,
        )

    def _resolved_targets_for_call(
        self,
        function_id: str,
        line: int,
        receiver_name: str | None,
        member_name: str | None,
        callee_expr: str,
        callee_kind: str,
    ) -> list[str]:
        targets: list[str] = []
        if callee_kind == "local_function":
            for candidate_id in self._functions_by_local_name.get(callee_expr, []):
                function = next((item for item in self.functions if item.function_id == candidate_id), None)
                if function is not None and function.file == self.relative_path:
                    targets.append(f"{self.relative_path}::{function.qualified_name}")
        if receiver_name and member_name:
            if receiver_name in self.module_table_names:
                for module_key in self.logical_module_keys:
                    targets.append(f"{module_key}.{member_name}")
            for binding in self.require_bindings:
                if binding.alias_name != receiver_name or binding.line > line:
                    continue
                if binding.scope_function_id and binding.scope_function_id != function_id:
                    continue
                for module_key in binding.module_keys:
                    targets.append(f"{module_key}.{member_name}")
        return list(dict.fromkeys(targets))

    def _summarize_function_returns(self, node: Any, function: FunctionSymbol) -> ReturnSummary:
        returns = self._collect_return_statements(node.body)
        guards = self._collect_guard_summaries(node)
        if not returns:
            return ReturnSummary(state="always_nil", reason="Function has no explicit return statement.", line=function.end_line, guards=guards)
        summaries = [self._summarize_return_statement(item, function, seen_names=set()) for item in returns]
        combined = self._combine_return_summaries(summaries)
        combined.guards = guards
        return combined

    def _collect_return_statements(self, block: Any) -> list[Any]:
        returns: list[Any] = []
        statements = block.body if isinstance(block, N.Block) else []
        for statement in statements:
            if isinstance(statement, N.Return):
                returns.append(statement)
                continue
            if isinstance(statement, N.If):
                returns.extend(self._collect_return_statements(statement.body))
                returns.extend(self._collect_else_returns(statement.orelse))
                continue
            if isinstance(statement, N.Do):
                returns.extend(self._collect_return_statements(statement.body))
                continue
            if isinstance(statement, (N.While, N.Repeat, N.Fornum, N.Forin)):
                returns.extend(self._collect_return_statements(statement.body))
        return returns

    def _collect_else_returns(self, node: Any) -> list[Any]:
        if node is None:
            return []
        if isinstance(node, N.Block):
            return self._collect_return_statements(node)
        if isinstance(node, N.ElseIf):
            return self._collect_return_statements(node.body) + self._collect_else_returns(node.orelse)
        return []

    def _summarize_return_statement(self, statement: Any, function: FunctionSymbol, *, seen_names: set[str]) -> ReturnSummary:
        line, _column = self.source.line_column_for_node(statement)
        if not statement.values:
            return ReturnSummary(state="always_nil", reason="Return without values yields nil.", line=line)
        return self._summarize_return_expr(statement.values[0], function, before_line=line, seen_names=seen_names)

    def _summarize_return_expr(self, expr: Any, function: FunctionSymbol, *, before_line: int, seen_names: set[str]) -> ReturnSummary:
        line, _column = self.source.line_column_for_node(expr)
        text = normalize_whitespace(self.source.node_text(expr))
        if isinstance(expr, N.Nil):
            return ReturnSummary(state="always_nil", reason="Returns literal nil.", line=line)
        if isinstance(expr, (N.String, N.Number, N.Table, N.TrueExpr, N.FalseExpr, N.AnonymousFunction, N.BinaryOp, N.UnaryOp)):
            return ReturnSummary(state="always_non_nil", reason=f"Returns non-nil expression '{text}'.", line=line)
        if isinstance(expr, N.Index):
            return ReturnSummary(
                state="field_dependent",
                reason=f"Returns indexed expression '{text}'.",
                line=line,
                dependencies=[ReturnDependency(kind="field", expression=text, line=line)],
            )
        if isinstance(expr, N.Name):
            if expr.id in function.param_names:
                return ReturnSummary(
                    state="param_passthrough",
                    reason=f"Returns parameter '{expr.id}'.",
                    line=line,
                    dependencies=[
                        ReturnDependency(
                            kind="parameter",
                            expression=expr.id,
                            line=line,
                            param_name=expr.id,
                            param_index=function.param_names.index(expr.id),
                        )
                    ],
                )
            if expr.id in seen_names:
                return ReturnSummary(state="unknown", reason=f"Cyclic local reference '{expr.id}'.", line=line)
            assignment = self._find_latest_assignment(self._function_nodes[function.function_id], expr.id, before_line)
            if assignment is None:
                return ReturnSummary(state="unknown", reason=f"Unable to resolve local '{expr.id}'.", line=line)
            seen_names.add(expr.id)
            nested = self._summarize_return_expr(assignment["value"], function, before_line=assignment["line"], seen_names=seen_names)
            seen_names.remove(expr.id)
            if assignment["line"] not in nested.dependencies and assignment["line"] not in function.key_lines:
                function.key_lines.append(assignment["line"])
            return ReturnSummary(
                state=nested.state,
                reason=f"Returns local '{expr.id}' assigned from line {assignment['line']}: {nested.reason}",
                line=line,
                dependencies=nested.dependencies,
                confidence=nested.confidence,
            )
        if isinstance(expr, N.Call):
            callee_expr = self._qualified_name(expr.func) or text
            return ReturnSummary(
                state="call_dependent",
                reason=f"Returns result of '{callee_expr}'.",
                line=line,
                dependencies=[ReturnDependency(kind="call", expression=text, line=line, callee_expr=callee_expr)],
            )
        if isinstance(expr, (N.OrLoOp, getattr(N, "LOrOp", N.OrLoOp))):
            right = self._summarize_return_expr(expr.right, function, before_line=before_line, seen_names=seen_names)
            if right.state == "always_non_nil":
                return ReturnSummary(
                    state="always_non_nil",
                    reason=f"Defaulting expression '{text}' guarantees a non-nil fallback.",
                    line=line,
                    dependencies=right.dependencies,
                )
            return ReturnSummary(state="maybe_nil", reason=f"Logical or expression '{text}' may still yield nil.", line=line)
        if isinstance(expr, (N.AndLoOp, getattr(N, "LAndOp", N.AndLoOp))):
            return ReturnSummary(state="maybe_nil", reason=f"Logical and expression '{text}' may yield nil.", line=line)
        return ReturnSummary(state="unknown", reason=f"Unsupported return expression '{type(expr).__name__}'.", line=line)

    def _combine_return_summaries(self, summaries: list[ReturnSummary]) -> ReturnSummary:
        if not summaries:
            return ReturnSummary(state="always_nil", reason="No returns observed.")
        states = {item.state for item in summaries}
        line = summaries[0].line
        if states == {"always_non_nil"}:
            return ReturnSummary(
                state="always_non_nil",
                reason="All return paths produce non-nil values.",
                line=line,
                dependencies=[dep for item in summaries for dep in item.dependencies],
            )
        if states == {"always_nil"}:
            return ReturnSummary(
                state="always_nil",
                reason="All return paths produce nil.",
                line=line,
                dependencies=[dep for item in summaries for dep in item.dependencies],
            )
        if len(states) == 1:
            only = summaries[0]
            return ReturnSummary(
                state=only.state,
                reason=f"All return paths are classified as {only.state}.",
                line=line,
                dependencies=[dep for item in summaries for dep in item.dependencies],
            )
        if "always_non_nil" in states and "always_nil" in states:
            return ReturnSummary(state="maybe_nil", reason="Return paths mix nil and non-nil values.", line=line)
        if "unknown" in states:
            return ReturnSummary(state="unknown", reason="At least one return path is unresolved.", line=line)
        if "field_dependent" in states and len(states) == 1:
            return ReturnSummary(
                state="field_dependent",
                reason="All return paths depend on indexed values.",
                line=line,
                dependencies=[dep for item in summaries for dep in item.dependencies],
            )
        if "call_dependent" in states and len(states) == 1:
            return ReturnSummary(
                state="call_dependent",
                reason="All return paths depend on downstream calls.",
                line=line,
                dependencies=[dep for item in summaries for dep in item.dependencies],
            )
        if "param_passthrough" in states and len(states) == 1:
            return ReturnSummary(
                state="param_passthrough",
                reason="All return paths pass through a parameter.",
                line=line,
                dependencies=[dep for item in summaries for dep in item.dependencies],
            )
        return ReturnSummary(state="maybe_nil", reason="Return paths have mixed nullability.", line=line)

    def _collect_guard_summaries(self, function_node: Any) -> list[str]:
        guards: list[str] = []

        def visit_block(block: Any) -> None:
            statements = block.body if isinstance(block, N.Block) else []
            for statement in statements:
                if isinstance(statement, N.Call) and self._qualified_name(statement.func) == "assert" and statement.args:
                    guards.append(normalize_whitespace(self.source.node_text(statement)))
                elif isinstance(statement, N.If):
                    guards.append(normalize_whitespace(self.source.node_text(statement.test)))
                    visit_block(statement.body)
                    visit_else(statement.orelse)
                elif isinstance(statement, N.Do):
                    visit_block(statement.body)
                elif isinstance(statement, (N.While, N.Repeat, N.Fornum, N.Forin)):
                    test = getattr(statement, "test", None)
                    if test is not None:
                        guards.append(normalize_whitespace(self.source.node_text(test)))
                    visit_block(statement.body)

        def visit_else(node: Any) -> None:
            if node is None:
                return
            if isinstance(node, N.Block):
                visit_block(node)
            elif isinstance(node, N.ElseIf):
                guards.append(normalize_whitespace(self.source.node_text(node.test)))
                visit_block(node.body)
                visit_else(node.orelse)

        visit_block(function_node.body)
        return list(dict.fromkeys(item for item in guards if item))

    def _find_latest_assignment(self, function_node: Any, name: str, before_line: int) -> dict[str, Any] | None:
        latest: dict[str, Any] | None = None

        def visit_block(block: Any) -> None:
            nonlocal latest
            statements = block.body if isinstance(block, N.Block) else []
            for statement in statements:
                line, _column = self.source.line_column_for_node(statement)
                if line >= before_line:
                    continue
                if isinstance(statement, (N.Assign, N.LocalAssign)):
                    for index, target in enumerate(getattr(statement, "targets", [])):
                        if not isinstance(target, N.Name) or target.id != name:
                            continue
                        values = getattr(statement, "values", [])
                        value = values[index] if index < len(values) else None
                        if value is None:
                            continue
                        latest = {"line": line, "value": value}
                elif isinstance(statement, N.LocalFunction) and statement.name.id == name:
                    latest = {"line": line, "value": statement}
                elif isinstance(statement, N.If):
                    visit_block(statement.body)
                    visit_else(statement.orelse)
                elif isinstance(statement, N.Do):
                    visit_block(statement.body)
                elif isinstance(statement, (N.While, N.Repeat, N.Fornum, N.Forin)):
                    visit_block(statement.body)

        def visit_else(node: Any) -> None:
            if node is None:
                return
            if isinstance(node, N.Block):
                visit_block(node)
            elif isinstance(node, N.ElseIf):
                visit_block(node.body)
                visit_else(node.orelse)

        visit_block(function_node.body)
        return latest

    def _first_function_id_for_name(self, name: str) -> str | None:
        function_ids = self._functions_by_local_name.get(name, [])
        if function_ids:
            return function_ids[0]
        function_ids = self._functions_by_qualified_name.get(name, [])
        if function_ids:
            return function_ids[0]
        return None

    def _receiver_and_member(self, node: Any) -> tuple[str | None, str | None]:
        if isinstance(node, N.Index):
            receiver = self._qualified_name(node.value)
            member = self._qualified_name(node.idx)
            if receiver and member:
                return receiver, member
        return None, None

    def _qualified_name(self, node: Any) -> str | None:
        if isinstance(node, N.Name):
            return node.id
        if isinstance(node, N.Index):
            notation = getattr(node, "notation", None)
            notation_name = getattr(notation, "name", str(notation))
            if notation_name != "DOT":
                return None
            left = self._qualified_name(node.value)
            right = self._qualified_name(node.idx)
            if left and right:
                return f"{left}.{right}"
        return None

    def _fallback_function_name(self, node: Any) -> str:
        line, _column = self.source.line_column_for_node(node)
        return f"anonymous@{line}"

    def _short_name(self, qualified_name: str) -> str:
        if "." not in qualified_name:
            return qualified_name
        return qualified_name.rsplit(".", 1)[-1]

    def _logic_hash(self, node: Any, fallback_text: str) -> str:
        try:
            canonical = self._canonical_ast(node.body if hasattr(node, "body") else node)
            return sha256_hex(json.dumps(canonical, ensure_ascii=False, sort_keys=True))
        except Exception:
            return sha256_hex(fallback_text)

    def _canonical_ast(self, node: Any) -> Any:
        if node is None:
            return None
        if isinstance(node, (str, int, float, bool)):
            return node
        if isinstance(node, bytes):
            return node.decode("utf-8", errors="replace")
        if isinstance(node, list):
            return [self._canonical_ast(item) for item in node]
        if isinstance(node, tuple):
            return [self._canonical_ast(item) for item in node]
        if hasattr(node, "name") and not hasattr(node, "__dict__"):
            return getattr(node, "name")
        if isinstance(node, N.String):
            return {"type": "String", "raw": node.raw}
        if isinstance(node, N.Name):
            return {"type": "Name", "id": node.id}
        if isinstance(node, N.Number):
            return {"type": "Number", "n": getattr(node, "n", None)}
        if hasattr(node, "__dict__"):
            payload = {"type": type(node).__name__}
            for key, value in sorted(vars(node).items()):
                if key in {"first_token", "last_token", "comments", "wrapped"}:
                    continue
                payload[key] = self._canonical_ast(value)
            return payload
        return repr(node)


def extract_file_symbols(
    relative_path: str,
    text: str,
    config: ReviewConfig,
    *,
    file_id: str,
    content_hash: str,
    symbol_fingerprint: str,
    parsed_file: ParsedLuaFile | None = None,
) -> FileSymbolFacts:
    try:
        parsed = parsed_file or parse_lua_file(relative_path, text)
    except Exception as exc:
        return FileSymbolFacts(
            file=relative_path,
            file_id=file_id,
            content_hash=content_hash,
            symbol_fingerprint=symbol_fingerprint,
            parse_status="error",
            parse_error=str(exc),
            logical_module_keys=[Path(relative_path).stem],
            declared_module_keys=[],
            module_table_names=[],
            require_bindings=[],
            export_bindings=[],
            functions=[],
            global_functions=[],
        )
    try:
        return SymbolExtractor(parsed, config).extract(
            file_id=file_id,
            content_hash=content_hash,
            symbol_fingerprint=symbol_fingerprint,
        )
    except Exception as exc:
        raise SymbolExtractionError(f"Failed extracting symbols for {relative_path}: {exc}") from exc
