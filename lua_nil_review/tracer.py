from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import luaparser.astnodes as N
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency 'luaparser'. Install dependencies with `python -m pip install -r requirements.txt`."
    ) from exc

from .common import atomic_write_json, load_json, normalize_whitespace
from .config import ReviewConfig, load_config
from .parsed_lua import ParsedLuaFile, SourceIndex, parse_lua_file
from .state import StateLayout, build_layout
from .symbol_index import SymbolRepository
from .symbol_models import FunctionSymbol
from .symbol_query import jump_to_definition


@dataclass
class BoundExpression:
    expr: Any
    scope: "FunctionScope"
    before_line: int


@dataclass
class FunctionScope:
    file: str
    parsed: ParsedLuaFile
    source: SourceIndex
    symbol: FunctionSymbol
    node: Any
    param_bindings: dict[str, BoundExpression] = field(default_factory=dict)


@dataclass
class TraceRecorder:
    max_expanded_nodes: int
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    exhausted: bool = False

    def add_node(
        self,
        *,
        kind: str,
        file: str,
        line: int | None,
        expression: str,
        depth: int,
        status: str,
        summary: str,
    ) -> str:
        node_id = f"node-{len(self.nodes) + 1}"
        self.nodes.append(
            {
                "node_id": node_id,
                "kind": kind,
                "file": file,
                "line": line,
                "expression": expression,
                "depth": depth,
                "status": status,
                "summary": summary,
            }
        )
        if len(self.nodes) >= self.max_expanded_nodes:
            self.exhausted = True
        return node_id

    def add_edge(self, parent_id: str | None, child_id: str, *, label: str) -> None:
        if not parent_id:
            return
        self.edges.append({"from": parent_id, "to": child_id, "label": label})


class TraceEngine:
    def __init__(self, root: Path, layout: StateLayout, config: ReviewConfig) -> None:
        self.root = root
        self.layout = layout
        self.config = config
        self.repository = SymbolRepository.load(layout)
        self.recorder = TraceRecorder(max_expanded_nodes=config.symbol_tracing.max_expanded_nodes)
        self._parsed_cache: dict[str, ParsedLuaFile] = {}
        self._function_scope_cache: dict[tuple[str, str], FunctionScope] = {}
        self._visited_calls: set[tuple[str, str]] = set()

    def trace_finding(self, finding: dict[str, Any]) -> dict[str, Any]:
        self._reset_trace_state()
        file = finding["file"]
        facts = self.repository.file_facts(file)
        if facts is None:
            raise RuntimeError(f"Missing symbol facts for {file}.")
        function_symbol = next((item for item in facts.functions if item.start_line <= finding["line"] <= item.end_line), None)
        if function_symbol is None:
            raise RuntimeError(f"Unable to locate function scope for {file}:{finding['line']}.")
        scope = self._function_scope(file, function_symbol)
        sink_call = self._locate_sink_call(scope.node, finding["line"], scope.source)
        if sink_call is None or not sink_call.args:
            raise RuntimeError(f"Unable to locate sink call for finding {finding['finding_id']}.")

        root_expr = sink_call.args[0]
        root_text = normalize_whitespace(scope.source.node_text(root_expr))
        root_id = self.recorder.add_node(
            kind="sink_argument",
            file=file,
            line=finding["line"],
            expression=root_text,
            depth=0,
            status="pending",
            summary="Tracing sink argument origin.",
        )
        branch_outcomes = self._resolve_expr(scope, root_expr, before_line=finding["line"], depth=0, parent_id=root_id)
        return self._build_bundle(
            root_kind="sink_argument",
            root_file=file,
            root_line=finding["line"],
            root_expression=root_text,
            branch_outcomes=branch_outcomes,
            extra={"finding_id": finding["finding_id"]},
        )

    def trace_callsite(self, *, file: str, line: int, expression: str) -> dict[str, Any]:
        self._reset_trace_state()
        facts = self.repository.file_facts(file)
        if facts is None:
            raise RuntimeError(f"Missing symbol facts for {file}.")
        function_symbol = next((item for item in facts.functions if item.start_line <= line <= item.end_line), None)
        if function_symbol is None:
            raise RuntimeError(f"Unable to locate function scope for {file}:{line}.")
        scope = self._function_scope(file, function_symbol)
        call_expr = self._locate_call_expression(scope.node, line, expression, scope.source)
        if call_expr is None:
            raise RuntimeError(f"Unable to locate call expression '{expression}' at {file}:{line}.")
        root_expr = _qualified_name(call_expr.func) or normalize_whitespace(scope.source.node_text(call_expr.func))
        root_id = self.recorder.add_node(
            kind="callsite",
            file=file,
            line=line,
            expression=root_expr,
            depth=0,
            status="pending",
            summary="Tracing return value from the selected callsite.",
        )
        branch_outcomes = self._resolve_call(scope, call_expr, depth=0, parent_id=root_id)
        return self._build_bundle(
            root_kind="callsite",
            root_file=file,
            root_line=line,
            root_expression=root_expr,
            branch_outcomes=branch_outcomes,
            extra={"callsite_expr": expression},
        )

    def _reset_trace_state(self) -> None:
        self.recorder = TraceRecorder(max_expanded_nodes=self.config.symbol_tracing.max_expanded_nodes)
        self._visited_calls.clear()

    def _build_bundle(
        self,
        *,
        root_kind: str,
        root_file: str,
        root_line: int,
        root_expression: str,
        branch_outcomes: list[dict[str, Any]],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        overall = _aggregate_status([item["status"] for item in branch_outcomes])
        budget_exhausted = self.recorder.exhausted
        external_config_dependency = any(item.get("external_config_dependency") for item in branch_outcomes)
        frontier_node_ids = _frontier_nodes(self.recorder.nodes, self.recorder.edges)
        needs_source_escalation = budget_exhausted or overall == "uncertain" or external_config_dependency
        if budget_exhausted:
            overall = "budget_exhausted"
            needs_source_escalation = True
        auto_silenced = overall == "safe" and not external_config_dependency
        summary = _trace_summary(
            overall,
            branch_outcomes,
            budget_exhausted=budget_exhausted,
            external_config_dependency=external_config_dependency,
        )
        bundle = {
            "status": "completed",
            "overall": overall,
            "root": {
                "kind": root_kind,
                "file": root_file,
                "line": root_line,
                "expression": root_expression,
            },
            "nodes": self.recorder.nodes,
            "edges": self.recorder.edges,
            "branch_outcomes": branch_outcomes,
            "frontier_node_ids": frontier_node_ids,
            "max_depth": self.config.symbol_tracing.max_depth,
            "budget": {
                "max_expanded_nodes": self.config.symbol_tracing.max_expanded_nodes,
                "used_nodes": len(self.recorder.nodes),
                "budget_exhausted": budget_exhausted,
            },
            "summary": summary,
            "trace_auto_silenced": auto_silenced,
            "needs_source_escalation": needs_source_escalation,
            "external_config_dependency": external_config_dependency,
        }
        if extra:
            bundle.update(extra)
        return bundle

    def _resolve_expr(
        self,
        scope: FunctionScope,
        expr: Any,
        *,
        before_line: int,
        depth: int,
        parent_id: str | None,
    ) -> list[dict[str, Any]]:
        if self.recorder.exhausted or depth >= self.config.symbol_tracing.max_depth:
            node_id = self.recorder.add_node(
                kind="budget_limit",
                file=scope.file,
                line=before_line,
                expression=normalize_whitespace(scope.source.node_text(expr)),
                depth=depth,
                status="uncertain",
                summary="Trace budget exhausted before expression resolved.",
            )
            self.recorder.add_edge(parent_id, node_id, label="budget")
            return [{"status": "uncertain", "summary": "budget exhausted"}]

        line, _column = scope.source.line_column_for_node(expr)
        text = normalize_whitespace(scope.source.node_text(expr))
        if isinstance(expr, N.Nil):
            node_id = self.recorder.add_node(kind="literal_nil", file=scope.file, line=line, expression=text, depth=depth + 1, status="risky", summary="Literal nil reaches the sink.")
            self.recorder.add_edge(parent_id, node_id, label="origin")
            return [{"status": "risky", "summary": "literal nil"}]
        if isinstance(expr, (N.String, N.Number, N.Table, N.TrueExpr, N.FalseExpr, N.AnonymousFunction, N.BinaryOp, N.UnaryOp)):
            node_id = self.recorder.add_node(kind="non_nil_expr", file=scope.file, line=line, expression=text, depth=depth + 1, status="safe", summary="Expression is structurally non-nil.")
            self.recorder.add_edge(parent_id, node_id, label="origin")
            return [{"status": "safe", "summary": "non-nil expression"}]
        if isinstance(expr, (N.OrLoOp, getattr(N, "LOrOp", N.OrLoOp))):
            right_statuses = self._resolve_expr(scope, expr.right, before_line=before_line, depth=depth + 1, parent_id=parent_id)
            if all(item["status"] == "safe" for item in right_statuses):
                node_id = self.recorder.add_node(kind="defaulting", file=scope.file, line=line, expression=text, depth=depth + 1, status="safe", summary="Defaulting expression guarantees a non-nil fallback.")
                self.recorder.add_edge(parent_id, node_id, label="default")
                return [{"status": "safe", "summary": "defaulting fallback"}]
            left_statuses = self._resolve_expr(scope, expr.left, before_line=before_line, depth=depth + 1, parent_id=parent_id)
            return [{"status": _aggregate_status([*[_item["status"] for _item in left_statuses], *[_item["status"] for _item in right_statuses]]), "summary": "logical or expression"}]
        if isinstance(expr, N.Name):
            if expr.id in scope.param_bindings:
                node_id = self.recorder.add_node(kind="param_passthrough", file=scope.file, line=line, expression=expr.id, depth=depth + 1, status="pending", summary=f"Parameter '{expr.id}' maps to caller argument.")
                self.recorder.add_edge(parent_id, node_id, label="param")
                binding = scope.param_bindings[expr.id]
                return self._resolve_expr(binding.scope, binding.expr, before_line=binding.before_line, depth=depth + 1, parent_id=node_id)
            assignment = _find_latest_assignment(scope.node, expr.id, before_line, scope.source)
            if assignment is not None:
                node_id = self.recorder.add_node(
                    kind="local_assignment_origin",
                    file=scope.file,
                    line=assignment["line"],
                    expression=normalize_whitespace(scope.source.node_text(assignment["value"])),
                    depth=depth + 1,
                    status="pending",
                    summary=f"Resolved '{expr.id}' to the latest assignment before the sink.",
                )
                self.recorder.add_edge(parent_id, node_id, label="assignment")
                return self._resolve_expr(scope, assignment["value"], before_line=assignment["line"], depth=depth + 1, parent_id=node_id)
            node_id = self.recorder.add_node(kind="parameter_or_unknown", file=scope.file, line=line, expression=expr.id, depth=depth + 1, status="uncertain", summary=f"Name '{expr.id}' is unresolved in the local scope.")
            self.recorder.add_edge(parent_id, node_id, label="unknown")
            return [{"status": "uncertain", "summary": f"unresolved name {expr.id}"}]
        if isinstance(expr, N.Index):
            node_id = self.recorder.add_node(kind="field_read", file=scope.file, line=line, expression=text, depth=depth + 1, status="uncertain", summary="Indexed values are treated as maybe_nil.")
            self.recorder.add_edge(parent_id, node_id, label="field")
            return [{"status": "uncertain", "summary": "indexed expression"}]
        if isinstance(expr, N.Call):
            return self._resolve_call(scope, expr, depth=depth, parent_id=parent_id)

        node_id = self.recorder.add_node(kind="unknown_expr", file=scope.file, line=line, expression=text, depth=depth + 1, status="uncertain", summary=f"Unsupported expression {type(expr).__name__}.")
        self.recorder.add_edge(parent_id, node_id, label="unknown")
        return [{"status": "uncertain", "summary": type(expr).__name__}]

    def _resolve_call(self, scope: FunctionScope, expr: Any, *, depth: int, parent_id: str | None) -> list[dict[str, Any]]:
        line, _column = scope.source.line_column_for_node(expr)
        call_text = _qualified_name(expr.func) or normalize_whitespace(scope.source.node_text(expr.func))
        node_id = self.recorder.add_node(kind="call_target", file=scope.file, line=line, expression=call_text, depth=depth + 1, status="pending", summary="Resolving call target.")
        self.recorder.add_edge(parent_id, node_id, label="call")
        jump = jump_to_definition(
            root=self.root,
            state_dir=self.layout.state_dir,
            file=scope.file,
            line=line,
            expression=call_text,
            config=self.config,
            include_all=True,
        )
        candidates = jump.get("candidates", [])
        if not candidates:
            self.recorder.nodes[-1]["status"] = "uncertain"
            self.recorder.nodes[-1]["summary"] = "Unable to resolve call target."
            return [{"status": "uncertain", "summary": "unresolved call target"}]

        outcomes: list[dict[str, Any]] = []
        for candidate in candidates[: self.config.symbol_tracing.max_branch_count]:
            function = self.repository.function_symbol(candidate["file"], candidate["function_id"])
            if function is None:
                outcomes.append({"status": "uncertain", "summary": f"missing function {candidate['function_id']}"})
                continue
            branch_id = self.recorder.add_node(
                kind="module_collision_branch",
                file=candidate["file"],
                line=function.start_line,
                expression=candidate["qualified_name"],
                depth=depth + 2,
                status="pending",
                summary=f"Following candidate {candidate['file']}:{function.start_line}.",
            )
            self.recorder.add_edge(node_id, branch_id, label="candidate")
            outcome = self._trace_function_candidate(scope, expr, function, depth=depth + 1, parent_id=branch_id)
            outcome["file"] = candidate["file"]
            outcome["function_id"] = candidate["function_id"]
            outcome["qualified_name"] = candidate["qualified_name"]
            outcome["slice_path"] = candidate["slice_path"]
            outcome["external_config_dependency"] = bool(jump.get("external_config_dependency"))
            outcomes.append(outcome)
            self.recorder.nodes[-1]["status"] = outcome["status"]
        self.recorder.nodes[-1]["status"] = _aggregate_status([item["status"] for item in outcomes])
        self.recorder.nodes[-1]["summary"] = _trace_summary(
            self.recorder.nodes[-1]["status"],
            outcomes,
            budget_exhausted=False,
            external_config_dependency=bool(jump.get("external_config_dependency")),
        )
        return outcomes

    def _trace_function_candidate(
        self,
        caller_scope: FunctionScope,
        call_expr: Any,
        function: FunctionSymbol,
        *,
        depth: int,
        parent_id: str,
    ) -> dict[str, Any]:
        visit_key = (function.file, function.function_id)
        if visit_key in self._visited_calls:
            self.recorder.nodes[-1]["status"] = "uncertain"
            self.recorder.nodes[-1]["summary"] = "Recursive call detected."
            return {"status": "uncertain", "summary": "recursive call cycle"}
        self._visited_calls.add(visit_key)
        try:
            scope = self._function_scope(function.file, function)
            param_bindings = {}
            for index, param_name in enumerate(function.param_names):
                if index >= len(call_expr.args):
                    continue
                param_bindings[param_name] = BoundExpression(expr=call_expr.args[index], scope=caller_scope, before_line=caller_scope.source.line_column_for_node(call_expr)[0])
            scope = FunctionScope(
                file=scope.file,
                parsed=scope.parsed,
                source=scope.source,
                symbol=scope.symbol,
                node=scope.node,
                param_bindings=param_bindings,
            )
            returns = _collect_return_values(scope.node.body, scope.source)
            if not returns:
                self.recorder.nodes[-1]["status"] = "risky"
                self.recorder.nodes[-1]["summary"] = "Function has no explicit return and therefore yields nil."
                return {"status": "risky", "summary": "implicit nil return"}
            statuses: list[str] = []
            summaries: list[str] = []
            for return_value, return_line in returns:
                branch_results = self._resolve_expr(scope, return_value, before_line=return_line, depth=depth + 1, parent_id=parent_id)
                branch_status = _aggregate_status([item["status"] for item in branch_results])
                statuses.append(branch_status)
                summaries.append(_trace_summary(branch_status, branch_results, budget_exhausted=False))
            status = _branch_status_from_paths(statuses)
            return {"status": status, "summary": "; ".join(dict.fromkeys(summaries))}
        finally:
            self._visited_calls.discard(visit_key)

    def _parsed(self, relative_path: str) -> ParsedLuaFile:
        parsed = self._parsed_cache.get(relative_path)
        if parsed is not None:
            return parsed
        text = (self.root / relative_path).read_text(encoding="utf-8", errors="replace")
        parsed = parse_lua_file(relative_path, text)
        self._parsed_cache[relative_path] = parsed
        return parsed

    def _function_scope(self, relative_path: str, function: FunctionSymbol) -> FunctionScope:
        key = (relative_path, function.function_id)
        cached = self._function_scope_cache.get(key)
        if cached is not None:
            return cached
        parsed = self._parsed(relative_path)
        node = _find_function_node(parsed.root, parsed.source, function)
        if node is None:
            raise RuntimeError(f"Unable to reload function node {function.function_id} from {relative_path}.")
        scope = FunctionScope(file=relative_path, parsed=parsed, source=parsed.source, symbol=function, node=node)
        self._function_scope_cache[key] = scope
        return scope

    def _locate_sink_call(self, function_node: Any, line: int, source: SourceIndex) -> Any | None:
        stack = [function_node.body]
        while stack:
            node = stack.pop()
            statements = node.body if isinstance(node, N.Block) else []
            for statement in statements:
                if isinstance(statement, N.Call):
                    call_line, _column = source.line_column_for_node(statement)
                    if call_line == line and _qualified_name(statement.func) == "string.find":
                        return statement
                stack.extend(_nested_blocks(statement))
        return None

    def _locate_call_expression(self, function_node: Any, line: int, expression: str, source: SourceIndex) -> Any | None:
        target = expression.replace(":", ".").strip()
        stack = [function_node.body]
        while stack:
            node = stack.pop()
            statements = node.body if isinstance(node, N.Block) else []
            for statement in statements:
                if isinstance(statement, N.Call):
                    call_line, _column = source.line_column_for_node(statement)
                    if call_line == line and (_qualified_name(statement.func) or normalize_whitespace(source.node_text(statement.func))) == target:
                        return statement
                if isinstance(statement, (N.Assign, N.LocalAssign)):
                    for value in getattr(statement, "values", []):
                        if isinstance(value, N.Call):
                            call_line, _column = source.line_column_for_node(value)
                            if call_line == line and (_qualified_name(value.func) or normalize_whitespace(source.node_text(value.func))) == target:
                                return value
                if isinstance(statement, N.Return):
                    for value in getattr(statement, "values", []):
                        if isinstance(value, N.Call):
                            call_line, _column = source.line_column_for_node(value)
                            if call_line == line and (_qualified_name(value.func) or normalize_whitespace(source.node_text(value.func))) == target:
                                return value
                stack.extend(_nested_blocks(statement))
        return None


def trace_finding(
    *,
    root: Path,
    state_dir: Path,
    finding_id: str,
    config_path: str | None = None,
    file: str | None = None,
    line: int | None = None,
    expression: str | None = None,
    expand_node: str | None = None,
) -> dict[str, Any]:
    layout = build_layout(root, state_dir)
    config, _ = load_config(root, config_path)
    engine = TraceEngine(root, layout, config)
    if expand_node:
        existing = _existing_bundle_for_expand(layout, finding_id=finding_id, file=file, line=line, expression=expression)
        if existing is not None:
            return _expand_bundle_node(existing, expand_node)
    if file and line is not None and expression:
        bundle = engine.trace_callsite(file=file, line=line, expression=expression)
        bundle_name = f"callsite-{file.replace('/', '__')}-{line}-{expression.replace('.', '_').replace(':', '_')}"
        atomic_write_json(layout.trace_bundles_dir / f"{bundle_name}.json", bundle)
        return {**bundle, "trace_bundle_path": f"trace_bundles/{bundle_name}.json", "expanded_node": expand_node}

    finding = _load_finding(layout, finding_id)
    if finding is None:
        raise RuntimeError(f"Unknown finding '{finding_id}'.")
    bundle = engine.trace_finding(finding)
    bundle_path = layout.trace_bundles_dir / f"{finding_id}.json"
    atomic_write_json(bundle_path, bundle)
    return {
        **bundle,
        "trace_bundle_path": f"trace_bundles/{finding_id}.json",
        "expanded_node": expand_node,
    }


def load_trace_bundle(layout: StateLayout, finding_id: str) -> dict[str, Any] | None:
    path = layout.trace_bundles_dir / f"{finding_id}.json"
    payload = load_json(path, default={})
    return payload if isinstance(payload, dict) and payload else None


def _load_finding(layout: StateLayout, finding_id: str) -> dict[str, Any] | None:
    for path in sorted(layout.analysis_dir.glob("*.json")):
        payload = load_json(path, default={})
        for finding in payload.get("findings", []):
            if finding.get("finding_id") == finding_id:
                return finding
    return None


def _existing_bundle_for_expand(
    layout: StateLayout,
    *,
    finding_id: str,
    file: str | None,
    line: int | None,
    expression: str | None,
) -> dict[str, Any] | None:
    if finding_id:
        return load_trace_bundle(layout, finding_id)
    if file and line is not None and expression:
        bundle_name = f"callsite-{file.replace('/', '__')}-{line}-{expression.replace('.', '_').replace(':', '_')}"
        payload = load_json(layout.trace_bundles_dir / f"{bundle_name}.json", default={})
        return payload if isinstance(payload, dict) and payload else None
    return None


def _expand_bundle_node(bundle: dict[str, Any], node_id: str) -> dict[str, Any]:
    nodes = {node["node_id"]: node for node in bundle.get("nodes", []) if node.get("node_id")}
    target = nodes.get(node_id)
    if target is None:
        raise RuntimeError(f"Unknown trace node '{node_id}'.")
    outgoing = [edge for edge in bundle.get("edges", []) if edge.get("from") == node_id]
    incoming = [edge for edge in bundle.get("edges", []) if edge.get("to") == node_id]
    related_ids = {node_id}
    related_ids.update(edge["to"] for edge in outgoing if edge.get("to"))
    related_ids.update(edge["from"] for edge in incoming if edge.get("from"))
    subgraph_nodes = [node for node in bundle.get("nodes", []) if node.get("node_id") in related_ids]
    return {
        **bundle,
        "expanded_node": node_id,
        "expanded_node_detail": target,
        "expanded_edges_out": outgoing,
        "expanded_edges_in": incoming,
        "expanded_subgraph_nodes": subgraph_nodes,
    }


def _find_function_node(root: Any, source: SourceIndex, function: FunctionSymbol) -> Any | None:
    stack = [root.body]
    while stack:
        block = stack.pop()
        statements = block.body if isinstance(block, N.Block) else []
        for statement in statements:
            if isinstance(statement, (N.Function, N.LocalFunction, N.AnonymousFunction)):
                line, _column = source.line_column_for_node(statement)
                if line == function.start_line:
                    qualified = _qualified_name(getattr(statement, "name", None)) if not isinstance(statement, N.AnonymousFunction) else function.qualified_name
                    if isinstance(statement, N.AnonymousFunction) or qualified == function.qualified_name or function.local_name == qualified:
                        return statement
                if isinstance(statement, N.AnonymousFunction):
                    stack.append(statement.body)
                else:
                    stack.append(statement.body)
            else:
                stack.extend(_nested_blocks(statement))
    return None


def _nested_blocks(statement: Any) -> list[Any]:
    blocks: list[Any] = []
    for attr in ("body",):
        block = getattr(statement, attr, None)
        if isinstance(block, N.Block):
            blocks.append(block)
    orelse = getattr(statement, "orelse", None)
    if isinstance(orelse, N.Block):
        blocks.append(orelse)
    elif isinstance(orelse, N.ElseIf):
        blocks.append(orelse.body)
        blocks.extend(_nested_blocks(orelse))
    return blocks


def _collect_return_values(block: Any, source: SourceIndex) -> list[tuple[Any, int]]:
    values: list[tuple[Any, int]] = []
    statements = block.body if isinstance(block, N.Block) else []
    for statement in statements:
        if isinstance(statement, N.Return):
            line, _column = source.line_column_for_node(statement)
            if statement.values:
                values.append((statement.values[0], line))
            else:
                values.append((N.Nil(), line))
            continue
        if isinstance(statement, N.If):
            values.extend(_collect_return_values(statement.body, source))
            if isinstance(statement.orelse, N.Block):
                values.extend(_collect_return_values(statement.orelse, source))
            elif isinstance(statement.orelse, N.ElseIf):
                values.extend(_collect_return_values(statement.orelse.body, source))
            continue
        if isinstance(statement, N.Do):
            values.extend(_collect_return_values(statement.body, source))
            continue
        if isinstance(statement, (N.While, N.Repeat, N.Fornum, N.Forin)):
            values.extend(_collect_return_values(statement.body, source))
    return values


def _find_latest_assignment(function_node: Any, name: str, before_line: int, source: SourceIndex) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None

    def visit_block(block: Any) -> None:
        nonlocal latest
        statements = block.body if isinstance(block, N.Block) else []
        for statement in statements:
            line, _column = source.line_column_for_node(statement)
            if line >= before_line:
                continue
            if isinstance(statement, (N.Assign, N.LocalAssign)):
                for index, target in enumerate(getattr(statement, "targets", [])):
                    if isinstance(target, N.Name) and target.id == name:
                        values = getattr(statement, "values", [])
                        if index < len(values):
                            latest = {"line": line, "value": values[index]}
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


def _qualified_name(node: Any) -> str | None:
    if isinstance(node, N.Name):
        return node.id
    if isinstance(node, N.Index):
        notation = getattr(node, "notation", None)
        notation_name = getattr(notation, "name", str(notation))
        if notation_name != "DOT":
            return None
        left = _qualified_name(node.value)
        right = _qualified_name(node.idx)
        if left and right:
            return f"{left}.{right}"
    return None


def _aggregate_status(statuses: list[str]) -> str:
    normalized = {status for status in statuses if status}
    if not normalized:
        return "uncertain"
    if normalized == {"safe"}:
        return "safe"
    if "risky" in normalized and "safe" in normalized:
        return "mixed"
    if "risky" in normalized:
        return "risky"
    if "budget_exhausted" in normalized:
        return "budget_exhausted"
    if "mixed" in normalized:
        return "mixed"
    return "uncertain"


def _branch_status_from_paths(statuses: list[str]) -> str:
    if statuses and all(item == "safe" for item in statuses):
        return "safe"
    if any(item == "risky" for item in statuses):
        return "risky"
    if any(item == "mixed" for item in statuses):
        return "risky"
    return "uncertain"


def _trace_summary(status: str, branches: list[dict[str, Any]], *, budget_exhausted: bool, external_config_dependency: bool = False) -> str:
    if budget_exhausted:
        return "Trace budget exhausted before all branches were resolved."
    if external_config_dependency:
        return "Trace depends on unresolved external module selection or packaging priority."
    if status == "safe":
        return "All traced branches resolve to non-nil values."
    if status == "risky":
        return "At least one traced branch can yield nil at the sink."
    if status == "mixed":
        return "Different collision branches lead to different outcomes."
    return "Trace could not prove the sink safe or risky."


def _frontier_nodes(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[str]:
    parent_ids = {edge["from"] for edge in edges if edge.get("from")}
    frontiers: list[str] = []
    for node in nodes:
        if node["node_id"] in parent_ids:
            continue
        if node.get("status") in {"uncertain", "budget_exhausted", "mixed"}:
            frontiers.append(node["node_id"])
    return frontiers
