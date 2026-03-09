from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class RequireBinding:
    alias_name: str
    module_keys: list[str]
    binding_kind: str
    line: int
    scope_function_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExportBinding:
    module_key: str
    member_name: str
    binding_kind: str
    function_id: str
    source_line: int
    receiver_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CallEdge:
    caller_function_id: str
    line: int
    callee_expr: str
    callee_kind: str
    receiver_name: str | None
    member_name: str | None
    arg_exprs: list[str]
    returns_used: bool
    resolved_targets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReturnDependency:
    kind: str
    expression: str
    line: int
    callee_expr: str | None = None
    param_name: str | None = None
    param_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReturnSummary:
    state: str
    reason: str
    line: int | None = None
    dependencies: list[ReturnDependency] = field(default_factory=list)
    guards: list[str] = field(default_factory=list)
    confidence: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["dependencies"] = [item.to_dict() for item in self.dependencies]
        return payload


@dataclass
class FunctionSymbol:
    function_id: str
    file: str
    local_name: str
    qualified_name: str
    visibility: str
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int
    signature_line: int
    signature_column: int
    logic_hash: str
    slice_id: str
    param_names: list[str]
    receiver_name: str | None = None
    member_name: str | None = None
    exported_as: list[str] = field(default_factory=list)
    key_lines: list[int] = field(default_factory=list)
    return_summary: ReturnSummary | None = None
    call_edges: list[CallEdge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["call_edges"] = [edge.to_dict() for edge in self.call_edges]
        payload["return_summary"] = self.return_summary.to_dict() if self.return_summary else None
        return payload


@dataclass
class FileSymbolFacts:
    file: str
    file_id: str
    content_hash: str
    symbol_fingerprint: str
    parse_status: str
    parse_error: str | None
    logical_module_keys: list[str]
    declared_module_keys: list[str]
    module_table_names: list[str]
    require_bindings: list[RequireBinding]
    export_bindings: list[ExportBinding]
    functions: list[FunctionSymbol]
    global_functions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "file_id": self.file_id,
            "content_hash": self.content_hash,
            "symbol_fingerprint": self.symbol_fingerprint,
            "parse_status": self.parse_status,
            "parse_error": self.parse_error,
            "logical_module_keys": list(self.logical_module_keys),
            "declared_module_keys": list(self.declared_module_keys),
            "module_table_names": list(self.module_table_names),
            "require_bindings": [item.to_dict() for item in self.require_bindings],
            "export_bindings": [item.to_dict() for item in self.export_bindings],
            "functions": [item.to_dict() for item in self.functions],
            "global_functions": list(self.global_functions),
        }


def file_symbol_facts_from_dict(payload: dict[str, Any]) -> FileSymbolFacts:
    functions: list[FunctionSymbol] = []
    for item in payload.get("functions", []):
        return_summary = item.get("return_summary")
        functions.append(
            FunctionSymbol(
                function_id=item["function_id"],
                file=item["file"],
                local_name=item["local_name"],
                qualified_name=item["qualified_name"],
                visibility=item["visibility"],
                start_line=item["start_line"],
                end_line=item["end_line"],
                start_offset=item["start_offset"],
                end_offset=item["end_offset"],
                signature_line=item["signature_line"],
                signature_column=item["signature_column"],
                logic_hash=item["logic_hash"],
                slice_id=item["slice_id"],
                param_names=list(item.get("param_names", [])),
                receiver_name=item.get("receiver_name"),
                member_name=item.get("member_name"),
                exported_as=list(item.get("exported_as", [])),
                key_lines=list(item.get("key_lines", [])),
                return_summary=ReturnSummary(
                    state=return_summary["state"],
                    reason=return_summary["reason"],
                    line=return_summary.get("line"),
                    dependencies=[
                        ReturnDependency(
                            kind=dep["kind"],
                            expression=dep["expression"],
                            line=dep["line"],
                            callee_expr=dep.get("callee_expr"),
                            param_name=dep.get("param_name"),
                            param_index=dep.get("param_index"),
                        )
                        for dep in return_summary.get("dependencies", [])
                    ],
                    guards=list(return_summary.get("guards", [])),
                    confidence=return_summary.get("confidence", "medium"),
                )
                if isinstance(return_summary, dict)
                else None,
                call_edges=[
                    CallEdge(
                        caller_function_id=edge["caller_function_id"],
                        line=edge["line"],
                        callee_expr=edge["callee_expr"],
                        callee_kind=edge["callee_kind"],
                        receiver_name=edge.get("receiver_name"),
                        member_name=edge.get("member_name"),
                        arg_exprs=list(edge.get("arg_exprs", [])),
                        returns_used=bool(edge.get("returns_used")),
                        resolved_targets=list(edge.get("resolved_targets", [])),
                    )
                    for edge in item.get("call_edges", [])
                ],
            )
        )
    return FileSymbolFacts(
        file=payload["file"],
        file_id=payload["file_id"],
        content_hash=payload["content_hash"],
        symbol_fingerprint=payload["symbol_fingerprint"],
        parse_status=payload.get("parse_status", "ok"),
        parse_error=payload.get("parse_error"),
        logical_module_keys=list(payload.get("logical_module_keys", [])),
        declared_module_keys=list(payload.get("declared_module_keys", [])),
        module_table_names=list(payload.get("module_table_names", [])),
        require_bindings=[
            RequireBinding(
                alias_name=item["alias_name"],
                module_keys=list(item.get("module_keys", [])),
                binding_kind=item["binding_kind"],
                line=int(item["line"]),
                scope_function_id=item.get("scope_function_id"),
            )
            for item in payload.get("require_bindings", [])
        ],
        export_bindings=[
            ExportBinding(
                module_key=item["module_key"],
                member_name=item["member_name"],
                binding_kind=item["binding_kind"],
                function_id=item["function_id"],
                source_line=int(item["source_line"]),
                receiver_name=item.get("receiver_name"),
            )
            for item in payload.get("export_bindings", [])
        ],
        functions=functions,
        global_functions=list(payload.get("global_functions", [])),
    )
