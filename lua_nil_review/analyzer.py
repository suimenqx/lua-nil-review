from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import luaparser.astnodes as N
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency 'luaparser'. Install dependencies with `python -m pip install -r requirements.txt`."
    ) from exc

from .common import BUILTIN_NON_NIL_CALLS, RULE_ID, RULE_VERSION, SNIPPET_MAX_LINES, SNIPPET_RADIUS, normalize_whitespace, sha256_hex
from .config import ReviewConfig
from .parsed_lua import ParsedLuaFile, SourceIndex, parse_lua_file
from .ast_utils import iter_call_expressions, iter_statement_expression_roots

NilState = str
MISSING = object()


@dataclass
class EvidenceEvent:
    kind: str
    state: NilState
    message: str
    line: int | None
    column: int | None
    code: str | None = None
    snippet_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "state": self.state,
            "message": self.message,
            "line": self.line,
            "column": self.column,
            "code": self.code,
            "snippet_path": self.snippet_path,
        }


@dataclass
class ValueInfo:
    nil_state: NilState
    trace: list[EvidenceEvent] = field(default_factory=list)
    origin_kind: str = "unknown"
    table_shape: dict[str, "ValueInfo"] | None = None

    def clone(self) -> "ValueInfo":
        return ValueInfo(self.nil_state, deepcopy(self.trace), self.origin_kind, deepcopy(self.table_shape))


@dataclass
class FunctionContext:
    function_name: str
    function_anchor: str
    start_line: int
    signature_line: int
    signature_column: int
    signature_text: str


@dataclass
class FileAnalysis:
    relative_path: str
    file_id: str
    content_hash: str
    analysis_fingerprint: str
    parse_status: str
    parse_error: str | None
    findings: list[dict[str, Any]]
    suppressed_findings: int
    functions_analyzed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_fingerprint": self.analysis_fingerprint,
            "content_hash": self.content_hash,
            "file": self.relative_path,
            "file_id": self.file_id,
            "findings": self.findings,
            "functions_analyzed": self.functions_analyzed,
            "parse_error": self.parse_error,
            "parse_status": self.parse_status,
            "suppressed_findings": self.suppressed_findings,
        }


def join_states(left: NilState, right: NilState) -> NilState:
    if left == right:
        return left
    if "maybe_nil" in {left, right}:
        return "maybe_nil"
    if {left, right} == {"nil", "non_nil"}:
        return "maybe_nil"
    if "unknown" in {left, right}:
        other = right if left == "unknown" else left
        if other in {"nil", "maybe_nil"}:
            return "maybe_nil"
        return "unknown"
    return "unknown"


def merge_envs(*envs: dict[str, ValueInfo]) -> dict[str, ValueInfo]:
    merged: dict[str, ValueInfo] = {}
    for env in envs:
        for name, info in env.items():
            if name not in merged:
                merged[name] = info.clone()
                continue
            merged[name] = ValueInfo(
                join_states(merged[name].nil_state, info.nil_state),
                merged[name].trace,
                merged[name].origin_kind if merged[name].origin_kind == info.origin_kind else "merged",
                deepcopy(merged[name].table_shape) if merged[name].table_shape == info.table_shape else None,
            )
    return merged


class LuaNilAnalyzer:
    def __init__(self, parsed_file: ParsedLuaFile, config: ReviewConfig) -> None:
        self.relative_path = parsed_file.relative_path
        self.text = parsed_file.text
        self.config = config
        self.parsed_file = parsed_file
        self.source = parsed_file.source
        self.findings: list[dict[str, Any]] = []
        self.functions_analyzed = 0

    def analyze(self, *, file_id: str, content_hash: str, analysis_fingerprint: str, snippets_dir: Path) -> FileAnalysis:
        self._analyze_chunk(self.parsed_file.root, snippets_dir)
        suppressed = sum(1 for finding in self.findings if finding["suppressed"])
        return FileAnalysis(
            relative_path=self.relative_path,
            file_id=file_id,
            content_hash=content_hash,
            analysis_fingerprint=analysis_fingerprint,
            parse_status="ok",
            parse_error=None,
            findings=self.findings,
            suppressed_findings=suppressed,
            functions_analyzed=self.functions_analyzed,
        )

    def _analyze_chunk(self, root: N.Chunk, snippets_dir: Path) -> None:
        context = FunctionContext(
            function_name="chunk",
            function_anchor="chunk@1",
            start_line=1,
            signature_line=1,
            signature_column=1,
            signature_text=self.source.line_text(1),
        )
        self.functions_analyzed += 1
        self._process_block(root.body.body, {}, context, snippets_dir)

    def _analyze_function(
        self,
        node: Any,
        function_name: str,
        snippets_dir: Path,
        *,
        captured_env: dict[str, ValueInfo] | None = None,
    ) -> None:
        line, column = self.source.line_column_for_node(node)
        context = FunctionContext(
            function_name=function_name,
            function_anchor=f"{function_name}@{line}",
            start_line=line,
            signature_line=line,
            signature_column=column,
            signature_text=self.source.line_text(line),
        )
        env: dict[str, ValueInfo] = {name: value.clone() for name, value in (captured_env or {}).items()}
        for arg in getattr(node, "args", []):
            if isinstance(arg, N.Name):
                env[arg.id] = ValueInfo(
                    "maybe_nil",
                    [
                        EvidenceEvent(
                            kind="parameter",
                            state="maybe_nil",
                            message=f"Function parameter '{arg.id}' is treated as maybe_nil at entry.",
                            line=line,
                            column=column,
                            code=context.signature_text,
                        )
                    ],
                    origin_kind="parameter",
                )
        self.functions_analyzed += 1
        self._process_block(node.body.body, env, context, snippets_dir)

    def _process_block(
        self,
        statements: list[Any],
        starting_env: dict[str, ValueInfo],
        context: FunctionContext,
        snippets_dir: Path,
    ) -> dict[str, ValueInfo]:
        env = {name: value.clone() for name, value in starting_env.items()}
        shadowed: dict[str, Any] = {}
        for statement in statements:
            self._inspect_statement_sinks(statement, env, context, snippets_dir)
            if isinstance(statement, N.LocalFunction):
                self._analyze_function(statement, self._name_for_function(statement.name), snippets_dir, captured_env=env)
                if statement.name.id not in shadowed:
                    shadowed[statement.name.id] = env.get(statement.name.id, MISSING)
                env[statement.name.id] = self._non_nil_from_node(statement, f"Local function '{statement.name.id}' is non_nil.")
                continue

            if isinstance(statement, N.Function):
                self._analyze_function(statement, self._name_for_function(statement.name), snippets_dir, captured_env=env)
                if isinstance(statement.name, N.Name):
                    env[statement.name.id] = self._non_nil_from_node(statement, f"Function '{statement.name.id}' is non_nil.")
                continue

            if isinstance(statement, N.LocalAssign):
                for target in statement.targets:
                    if isinstance(target, N.Name) and target.id not in shadowed:
                        shadowed[target.id] = env.get(target.id, MISSING)
                env = self._apply_assignment(statement.targets, statement.values, env)
                for value in statement.values:
                    if isinstance(value, N.AnonymousFunction):
                        self._analyze_function(
                            value,
                            f"anonymous@{self.source.line_column_for_node(value)[0]}",
                            snippets_dir,
                            captured_env=env,
                        )
                continue

            if isinstance(statement, N.Assign):
                env = self._apply_assignment(statement.targets, statement.values, env)
                for value in statement.values:
                    if isinstance(value, N.AnonymousFunction):
                        self._analyze_function(
                            value,
                            f"anonymous@{self.source.line_column_for_node(value)[0]}",
                            snippets_dir,
                            captured_env=env,
                        )
                continue

            if isinstance(statement, N.Call):
                env = self._apply_post_call_narrowing(statement, env)
                continue

            if isinstance(statement, N.If):
                env = self._process_if(statement, env, context, snippets_dir)
                continue

            if isinstance(statement, N.Do):
                body_env = self._process_block(statement.body.body, env, context, snippets_dir)
                env = merge_envs(env, body_env)
                continue

            if isinstance(statement, (N.While, N.Repeat, N.Fornum, N.Forin)):
                loop_env = self._process_loop(statement, env, context, snippets_dir)
                env = merge_envs(env, loop_env)
                continue

        result = {name: value.clone() for name, value in env.items()}
        for name, previous in shadowed.items():
            if previous is MISSING:
                result.pop(name, None)
            else:
                result[name] = previous.clone()
        return result

    def _process_loop(self, statement: Any, env: dict[str, ValueInfo], context: FunctionContext, snippets_dir: Path) -> dict[str, ValueInfo]:
        loop_env = {name: value.clone() for name, value in env.items()}
        test = getattr(statement, "test", None)
        if test is not None:
            loop_env = self._apply_guard(test, loop_env, truthy=True)
        body = getattr(statement, "body", None)
        if body is None:
            return loop_env
        statements = body.body if isinstance(body, N.Block) else getattr(body, "body", [])
        return self._process_block(statements, loop_env, context, snippets_dir)

    def _process_if(self, statement: N.If, env: dict[str, ValueInfo], context: FunctionContext, snippets_dir: Path) -> dict[str, ValueInfo]:
        positive = self._process_block(statement.body.body, self._apply_guard(statement.test, env, truthy=True), context, snippets_dir)
        negative = self._process_else(statement.orelse, self._apply_guard(statement.test, env, truthy=False), context, snippets_dir)
        return merge_envs(positive, negative)

    def _process_else(self, node: Any, env: dict[str, ValueInfo], context: FunctionContext, snippets_dir: Path) -> dict[str, ValueInfo]:
        if node is None:
            return env
        if isinstance(node, N.Block):
            return self._process_block(node.body, env, context, snippets_dir)
        if isinstance(node, N.ElseIf):
            positive = self._process_block(node.body.body, self._apply_guard(node.test, env, truthy=True), context, snippets_dir)
            negative = self._process_else(node.orelse, self._apply_guard(node.test, env, truthy=False), context, snippets_dir)
            return merge_envs(positive, negative)
        return env

    def _apply_assignment(self, targets: list[Any], values: list[Any], env: dict[str, ValueInfo]) -> dict[str, ValueInfo]:
        updated = {name: value.clone() for name, value in env.items()}
        evaluated = [self._eval_expression(value, updated) for value in values]
        for index, target in enumerate(targets):
            if not isinstance(target, N.Name):
                continue
            location_node = values[index] if index < len(values) else target
            line, column = self.source.line_column_for_node(location_node)
            if index < len(evaluated):
                info = evaluated[index].clone()
                origin_text = self.source.node_text(values[index])
                message = f"Assigned '{target.id}' from '{normalize_whitespace(origin_text)}'."
            else:
                info = ValueInfo(
                    "nil",
                    [EvidenceEvent(kind="assignment", state="nil", message=f"Assigned implicit nil to '{target.id}'.", line=line, column=column, code=self.source.line_text(line))],
                    origin_kind="literal_nil",
                )
                message = f"Assigned implicit nil to '{target.id}'."
            info.trace.append(EvidenceEvent(kind="assignment", state=info.nil_state, message=message, line=line, column=column, code=self.source.line_text(line)))
            updated[target.id] = self._compact_value(info)
        return updated

    def _apply_post_call_narrowing(self, statement: N.Call, env: dict[str, ValueInfo]) -> dict[str, ValueInfo]:
        updated = {name: value.clone() for name, value in env.items()}
        call_name = self._qualified_name(statement.func)
        if call_name not in set(self.config.nil_guards) or not statement.args:
            return updated
        target = statement.args[0]
        if not isinstance(target, N.Name):
            return updated
        line, column = self.source.line_column_for_node(statement)
        updated[target.id] = ValueInfo(
            "non_nil",
            [EvidenceEvent(kind="guard", state="non_nil", message=f"Call to nil guard '{call_name}' narrows '{target.id}' to non_nil.", line=line, column=column, code=self.source.line_text(line))],
            origin_kind="guarded_non_nil",
        )
        return updated

    def _apply_guard(self, test: Any, env: dict[str, ValueInfo], *, truthy: bool) -> dict[str, ValueInfo]:
        updated = {name: value.clone() for name, value in env.items()}
        name, state = self._guard_target_state(test, truthy)
        if name is None or state is None:
            return updated
        line, column = self.source.line_column_for_node(test)
        updated[name] = ValueInfo(
            state,
            [EvidenceEvent(kind="guard", state=state, message=f"Condition narrows '{name}' to {state}.", line=line, column=column, code=self.source.line_text(line))],
            origin_kind="guarded_nil" if state == "nil" else "guarded_non_nil",
        )
        return updated

    def _guard_target_state(self, test: Any, truthy: bool) -> tuple[str | None, NilState | None]:
        if isinstance(test, N.Name):
            if truthy:
                return test.id, "non_nil"
            return test.id, "maybe_nil"
        if isinstance(test, N.NotEqToOp):
            name = self._name_against_nil(test.left, test.right)
            if name:
                return name, "non_nil" if truthy else "nil"
        if isinstance(test, N.EqToOp):
            name = self._name_against_nil(test.left, test.right)
            if name:
                return name, "nil" if truthy else "non_nil"
        if isinstance(test, N.Call):
            call_name = self._qualified_name(test.func)
            if call_name in set(self.config.nil_guards) and test.args and isinstance(test.args[0], N.Name):
                return test.args[0].id, "non_nil" if truthy else "unknown"
        return None, None

    def _name_against_nil(self, left: Any, right: Any) -> str | None:
        if isinstance(left, N.Name) and isinstance(right, N.Nil):
            return left.id
        if isinstance(right, N.Name) and isinstance(left, N.Nil):
            return right.id
        return None

    def _inspect_statement_sinks(
        self,
        statement: Any,
        env: dict[str, ValueInfo],
        context: FunctionContext,
        snippets_dir: Path,
    ) -> None:
        for root in iter_statement_expression_roots(statement):
            for call in iter_call_expressions(root):
                self._inspect_call(call, env, context, snippets_dir)

    def _inspect_call(self, statement: N.Call, env: dict[str, ValueInfo], context: FunctionContext, snippets_dir: Path) -> None:
        if self._qualified_name(statement.func) != "string.find" or not statement.args:
            return
        first_arg = statement.args[0]
        arg_info = self._eval_expression(first_arg, env)
        if arg_info.nil_state == "non_nil":
            return
        line, column = self.source.line_column_for_node(statement)
        call_text = f"{self.source.node_text(statement.func)}({', '.join(self.source.node_text(arg) for arg in statement.args)})"
        arg_text = self.source.node_text(first_arg)
        trace = self._compact_trace(arg_info.trace)
        trace.append(EvidenceEvent(kind="sink", state=arg_info.nil_state, message=f"`string.find` receives '{arg_text}' as its first argument.", line=line, column=column, code=self.source.line_text(line)))
        finding_id = sha256_hex("|".join([RULE_ID, self.relative_path, context.function_anchor, str(line), normalize_whitespace(call_text), normalize_whitespace(arg_text)]))
        snippet_paths = self._write_snippets(finding_id, trace, snippets_dir)
        risk = self._risk_metadata(arg_info)
        state_label = {
            "nil": "is definitely",
            "maybe_nil": "may be",
            "unknown": "could not be proven non_nil and may still be",
        }.get(arg_info.nil_state, "may be")
        finding = {
            "finding_id": finding_id,
            "rule_id": RULE_ID,
            "rule_version": RULE_VERSION,
            "file": self.relative_path,
            "line": line,
            "column": column,
            "function_name": context.function_name,
            "function_anchor": context.function_anchor,
            "call_text": call_text,
            "arg_text": arg_text,
            "nil_state": arg_info.nil_state,
            "confidence": risk["confidence"],
            "risk_level": risk["risk_level"],
            "risk_tier": risk["risk_tier"],
            "risk_category": risk["risk_category"],
            "origin_kind": arg_info.origin_kind,
            "default_human_review": risk["default_human_review"],
            "trace_gate_required": risk["trace_gate_required"],
            "evidence_trace": [event.to_dict() for event in trace],
            "snippet_paths": snippet_paths,
            "needs_source_escalation": False,
            "suppressed": self._is_suppressed(finding_id, line),
            "message": f"`string.find` first argument '{arg_text}' {state_label} nil at this call site.",
        }
        self.findings.append(finding)

    def _is_suppressed(self, finding_id: str, line: int) -> bool:
        for entry in self.config.suppressions:
            if isinstance(entry, str):
                if entry == finding_id:
                    return True
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("finding_id") and entry["finding_id"] != finding_id:
                continue
            if entry.get("rule_id") and entry["rule_id"] != RULE_ID:
                continue
            if entry.get("file") and entry["file"] != self.relative_path:
                continue
            if entry.get("line") and int(entry["line"]) != line:
                continue
            return True
        return False

    def _write_snippets(self, finding_id: str, trace: list[EvidenceEvent], snippets_dir: Path) -> list[str]:
        snippets: list[str] = []
        for index, event in enumerate(trace[:4], start=1):
            if event.line is None:
                continue
            relative = f"snippets/{finding_id}--{index:02d}-{event.kind}.txt"
            target = snippets_dir / f"{finding_id}--{index:02d}-{event.kind}.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                self.source.snippet(
                    event.line,
                    radius=SNIPPET_RADIUS,
                    max_lines=SNIPPET_MAX_LINES,
                    label=f"{finding_id} {event.kind}",
                ),
                encoding="utf-8",
            )
            event.snippet_path = relative
            snippets.append(relative)
        return snippets

    def _compact_value(self, info: ValueInfo) -> ValueInfo:
        info.trace = self._compact_trace(info.trace)
        return info

    def _compact_trace(self, trace: list[EvidenceEvent], max_events: int = 6) -> list[EvidenceEvent]:
        compacted: list[EvidenceEvent] = []
        seen: set[tuple[Any, ...]] = set()
        for event in trace:
            key = (event.kind, event.state, event.line, event.column, event.message)
            if key in seen:
                continue
            seen.add(key)
            compacted.append(event)
        return compacted[-max_events:]

    def _eval_expression(self, expression: Any, env: dict[str, ValueInfo]) -> ValueInfo:
        if isinstance(expression, N.Name):
            info = env.get(expression.id)
            if info is not None:
                return info.clone()
            line, column = self.source.line_column_for_node(expression)
            return ValueInfo(
                "unknown",
                [EvidenceEvent(kind="name", state="unknown", message=f"Name '{expression.id}' is unresolved by the local-flow analyzer.", line=line, column=column, code=self.source.line_text(line))],
                origin_kind="unknown_name",
            )
        if isinstance(expression, N.Nil):
            line, column = self.source.line_column_for_node(expression)
            return ValueInfo("nil", [EvidenceEvent(kind="literal", state="nil", message="Literal nil.", line=line, column=column, code="nil")], origin_kind="literal_nil")
        if isinstance(expression, N.Table):
            return self._table_literal_value(expression, env)
        if isinstance(expression, (N.String, N.Number, N.TrueExpr, N.FalseExpr, N.AnonymousFunction)):
            return self._non_nil_from_node(expression, f"Expression '{normalize_whitespace(self.source.node_text(expression))}' is non_nil.")
        if isinstance(expression, N.Index):
            return self._eval_index_expression(expression, env)
        if isinstance(expression, N.Call):
            call_name = self._qualified_name(expression.func)
            if call_name in set(self.config.safe_wrappers) | BUILTIN_NON_NIL_CALLS:
                return self._non_nil_from_node(expression, f"Call '{call_name}' is treated as returning non_nil.")
            line, column = self.source.line_column_for_node(expression)
            return ValueInfo(
                "maybe_nil",
                [EvidenceEvent(kind="call", state="maybe_nil", message=f"Return value of '{normalize_whitespace(self.source.node_text(expression))}' is treated as maybe_nil.", line=line, column=column, code=self.source.line_text(line))],
                origin_kind="function_return",
            )
        if isinstance(expression, N.OrLoOp):
            left = self._eval_expression(expression.left, env)
            right = self._eval_expression(expression.right, env)
            if right.nil_state == "non_nil":
                return right
            return ValueInfo(
                join_states(left.nil_state, right.nil_state),
                left.trace + right.trace,
                left.origin_kind if left.origin_kind == right.origin_kind else "merged",
            )
        if isinstance(expression, N.AndLoOp):
            left = self._eval_expression(expression.left, env)
            right = self._eval_expression(expression.right, env)
            if left.nil_state in {"nil", "maybe_nil"}:
                return ValueInfo("maybe_nil", left.trace + right.trace, left.origin_kind)
            return right
        if isinstance(expression, N.BinaryOp):
            return self._non_nil_from_node(expression, f"Binary expression '{normalize_whitespace(self.source.node_text(expression))}' is treated as non_nil.")
        if isinstance(expression, N.UnaryOp):
            return self._non_nil_from_node(expression, f"Unary expression '{normalize_whitespace(self.source.node_text(expression))}' is treated as non_nil.")
        line, column = self.source.line_column_for_node(expression)
        return ValueInfo(
            "unknown",
            [EvidenceEvent(kind="expression", state="unknown", message=f"Unsupported expression '{type(expression).__name__}' is treated as unknown.", line=line, column=column, code=self.source.line_text(line))],
            origin_kind="unknown_expr",
        )

    def _non_nil_from_node(self, node: Any, message: str) -> ValueInfo:
        line, column = self.source.line_column_for_node(node)
        return ValueInfo("non_nil", [EvidenceEvent(kind="non_nil", state="non_nil", message=message, line=line, column=column, code=self.source.line_text(line))], origin_kind="non_nil")

    def _table_literal_value(self, table_node: N.Table, env: dict[str, ValueInfo]) -> ValueInfo:
        line, column = self.source.line_column_for_node(table_node)
        shape: dict[str, ValueInfo] = {}
        implicit_index = 1
        for field in getattr(table_node, "fields", []):
            if not isinstance(field, N.Field):
                continue
            key = self._table_key(getattr(field, "key", None))
            if key is None and getattr(field, "key", None) is None:
                key = str(implicit_index)
                implicit_index += 1
            if key is None:
                continue
            shape[key] = self._eval_expression(field.value, env).clone()
        return ValueInfo(
            "non_nil",
            [EvidenceEvent(kind="table", state="non_nil", message=f"Table literal '{normalize_whitespace(self.source.node_text(table_node))}' is non_nil.", line=line, column=column, code=self.source.line_text(line))],
            origin_kind="table_literal",
            table_shape=shape,
        )

    def _eval_index_expression(self, expression: N.Index, env: dict[str, ValueInfo]) -> ValueInfo:
        line, column = self.source.line_column_for_node(expression)
        expr_text = normalize_whitespace(self.source.node_text(expression))
        base_info = self._eval_expression(expression.value, env)
        key = self._table_key(expression.idx)
        if key is not None and base_info.table_shape is not None:
            resolved = base_info.table_shape.get(key)
            if resolved is None:
                return ValueInfo(
                    "nil",
                    self._compact_trace(
                        base_info.trace
                        + [
                            EvidenceEvent(
                                kind="index",
                                state="nil",
                                message=f"Field '{key}' is absent from local table expression '{normalize_whitespace(self.source.node_text(expression.value))}'.",
                                line=line,
                                column=column,
                                code=self.source.line_text(line),
                            )
                        ]
                    ),
                    origin_kind="missing_table_key",
                )
            value = resolved.clone()
            value.trace.append(
                EvidenceEvent(
                    kind="index",
                    state=value.nil_state,
                    message=f"Resolved local table field '{key}' from '{expr_text}'.",
                    line=line,
                    column=column,
                    code=self.source.line_text(line),
                )
            )
            if value.origin_kind in {"table_literal", "non_nil"}:
                value.origin_kind = "field_read"
            return self._compact_value(value)
        return ValueInfo(
            "maybe_nil",
            self._compact_trace(
                base_info.trace
                + [
                    EvidenceEvent(
                        kind="index",
                        state="maybe_nil",
                        message=f"Indexed expression '{expr_text}' is treated as maybe_nil without a visible guard.",
                        line=line,
                        column=column,
                        code=self.source.line_text(line),
                    )
                ]
            ),
            origin_kind="field_read",
        )

    def _table_key(self, node: Any) -> str | None:
        if isinstance(node, N.Name):
            return node.id
        if isinstance(node, N.String):
            return getattr(node, "s", None)
        if isinstance(node, N.Number):
            return str(getattr(node, "n", ""))
        return None

    def _risk_metadata(self, info: ValueInfo) -> dict[str, Any]:
        if info.nil_state == "nil":
            if info.origin_kind == "missing_table_key":
                category = "missing_local_table_key"
            else:
                category = "deterministic_nil"
            return {
                "risk_level": 1,
                "risk_tier": "high",
                "risk_category": category,
                "confidence": "high",
                "default_human_review": True,
                "trace_gate_required": False,
            }
        if info.nil_state == "unknown":
            if info.origin_kind == "unknown_name":
                category = "unresolved_name_unverified"
            elif info.origin_kind == "unknown_expr":
                category = "unknown_expression_unverified"
            else:
                category = "unknown_value_unverified"
            return {
                "risk_level": 3,
                "risk_tier": "low",
                "risk_category": category,
                "confidence": "low",
                "default_human_review": True,
                "trace_gate_required": True,
            }
        if info.origin_kind == "function_return":
            return {
                "risk_level": 3,
                "risk_tier": "low",
                "risk_category": "function_return_unverified",
                "confidence": "low",
                "default_human_review": True,
                "trace_gate_required": True,
            }
        if info.origin_kind == "parameter":
            return {
                "risk_level": 3,
                "risk_tier": "low",
                "risk_category": "parameter_unverified",
                "confidence": "low",
                "default_human_review": True,
                "trace_gate_required": True,
            }
        if info.origin_kind == "field_read":
            category = "local_unguarded_index"
        else:
            category = "local_maybe_nil"
        return {
            "risk_level": 2,
            "risk_tier": "medium",
            "risk_category": category,
            "confidence": "medium",
            "default_human_review": True,
            "trace_gate_required": False,
        }

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

    def _name_for_function(self, node: Any) -> str:
        qualified = self._qualified_name(node)
        if qualified:
            return qualified
        line, _column = self.source.line_column_for_node(node)
        return f"anonymous@{line}"


def analyze_lua_file(
    relative_path: str,
    text: str,
    config: ReviewConfig,
    *,
    file_id: str,
    content_hash: str,
    analysis_fingerprint: str,
    snippets_dir: Path,
    parsed_file: ParsedLuaFile | None = None,
) -> FileAnalysis:
    try:
        parsed = parsed_file or parse_lua_file(relative_path, text)
    except Exception as exc:
        return FileAnalysis(
            relative_path=relative_path,
            file_id=file_id,
            content_hash=content_hash,
            analysis_fingerprint=analysis_fingerprint,
            parse_status="error",
            parse_error=str(exc),
            findings=[],
            suppressed_findings=0,
            functions_analyzed=0,
        )
    return LuaNilAnalyzer(parsed, config).analyze(
        file_id=file_id,
        content_hash=content_hash,
        analysis_fingerprint=analysis_fingerprint,
        snippets_dir=snippets_dir,
    )
