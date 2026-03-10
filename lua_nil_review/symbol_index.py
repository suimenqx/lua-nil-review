from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import SYMBOL_INDEX_VERSION, atomic_write_json, load_json, sha1_hex
from .state import StateLayout
from .symbol_models import FileSymbolFacts, FunctionSymbol, file_symbol_facts_from_dict


def module_artifact_name(module_key: str) -> str:
    return f"{sha1_hex(module_key)}.json"


def build_symbol_index(layout: StateLayout, file_docs: list[FileSymbolFacts], *, symbol_fingerprint: str) -> dict[str, Any]:
    modules: dict[str, dict[str, Any]] = {}
    globals_map: dict[str, list[dict[str, Any]]] = {}
    files_by_path: dict[str, dict[str, Any]] = {}

    for doc in file_docs:
        artifact_name = f"{doc.file_id}.json"
        atomic_write_json(layout.symbol_files_dir / artifact_name, doc.to_dict())
        files_by_path[doc.file] = {
            "file_id": doc.file_id,
            "artifact_path": f"symbol_index/files/{artifact_name}",
            "logical_module_keys": list(doc.logical_module_keys),
            "parse_status": doc.parse_status,
        }
        if doc.parse_status != "ok":
            continue
        function_by_id = {item.function_id: item for item in doc.functions}
        for binding in doc.export_bindings:
            function = function_by_id.get(binding.function_id)
            if function is None:
                continue
            module_doc = modules.setdefault(
                binding.module_key,
                {
                    "module_key": binding.module_key,
                    "candidate_files": [],
                    "members": {},
                },
            )
            if doc.file not in module_doc["candidate_files"]:
                module_doc["candidate_files"].append(doc.file)
            module_doc["members"].setdefault(binding.member_name, []).append(_function_ref(function))
        for function in doc.functions:
            if function.visibility == "global" and "." not in function.qualified_name:
                globals_map.setdefault(function.local_name, []).append(_function_ref(function))

    module_artifacts: dict[str, str] = {}
    collisions: dict[str, list[str]] = {}
    for module_key, module_doc in sorted(modules.items()):
        if len(module_doc["candidate_files"]) > 1:
            collisions[module_key] = sorted(module_doc["candidate_files"])
        filename = module_artifact_name(module_key)
        module_path = layout.symbol_modules_dir / filename
        atomic_write_json(module_path, module_doc)
        module_artifacts[module_key] = f"symbol_index/modules/{filename}"

    active_module_filenames = {Path(path).name for path in module_artifacts.values()}
    for existing in layout.symbol_modules_dir.glob("*.json"):
        if existing.name not in active_module_filenames:
            existing.unlink(missing_ok=True)

    atomic_write_json(layout.symbol_index_dir / "globals.json", globals_map)
    atomic_write_json(layout.symbol_index_dir / "collisions.json", collisions)
    manifest = {
        "version": SYMBOL_INDEX_VERSION,
        "symbol_fingerprint": symbol_fingerprint,
        "files_by_path": files_by_path,
        "module_artifacts": module_artifacts,
        "collisions_path": "symbol_index/collisions.json",
        "globals_path": "symbol_index/globals.json",
    }
    atomic_write_json(layout.symbol_index_dir / "manifest.json", manifest)
    return {
        "files_indexed": len(file_docs),
        "modules_indexed": len(module_artifacts),
        "collision_groups": len(collisions),
    }


def load_symbol_manifest(layout: StateLayout) -> dict[str, Any]:
    return load_json(layout.symbol_index_dir / "manifest.json", default={}) or {}


def load_file_symbols(layout: StateLayout, relative_path: str) -> FileSymbolFacts | None:
    manifest = load_symbol_manifest(layout)
    entry = manifest.get("files_by_path", {}).get(relative_path)
    if not isinstance(entry, dict):
        return None
    return load_file_symbols_by_artifact(layout, entry.get("artifact_path"))


def load_file_symbols_by_artifact(layout: StateLayout, artifact_path: str | None) -> FileSymbolFacts | None:
    if not artifact_path:
        return None
    payload = load_json(layout.state_dir / artifact_path, default={})
    if not isinstance(payload, dict) or not payload:
        return None
    return file_symbol_facts_from_dict(payload)


@dataclass
class SymbolRepository:
    layout: StateLayout
    manifest: dict[str, Any]
    _file_cache: dict[str, FileSymbolFacts | None] = field(default_factory=dict)
    _caller_index: dict[str, list[dict[str, Any]]] | None = None

    @classmethod
    def load(cls, layout: StateLayout) -> "SymbolRepository":
        return cls(layout=layout, manifest=load_symbol_manifest(layout))

    def file_facts(self, relative_path: str) -> FileSymbolFacts | None:
        if relative_path in self._file_cache:
            return self._file_cache[relative_path]
        entry = self.manifest.get("files_by_path", {}).get(relative_path)
        if not isinstance(entry, dict):
            self._file_cache[relative_path] = None
            return None
        facts = load_file_symbols_by_artifact(self.layout, entry.get("artifact_path"))
        self._file_cache[relative_path] = facts
        return facts

    def module_doc(self, module_key: str) -> dict[str, Any]:
        artifact = self.manifest.get("module_artifacts", {}).get(module_key)
        if not artifact:
            return {}
        return load_json(self.layout.state_dir / artifact, default={}) or {}

    def globals_doc(self) -> dict[str, Any]:
        path = self.manifest.get("globals_path")
        if not path:
            return {}
        return load_json(self.layout.state_dir / path, default={}) or {}

    def function_symbol(self, relative_path: str, function_id: str) -> FunctionSymbol | None:
        facts = self.file_facts(relative_path)
        if facts is None:
            return None
        for function in facts.functions:
            if function.function_id == function_id:
                return function
        return None

    def function_by_id(self, function_id: str) -> FunctionSymbol | None:
        for relative_path in self.manifest.get("files_by_path", {}):
            function = self.function_symbol(relative_path, function_id)
            if function is not None:
                return function
        return None

    def incoming_call_edges(self, function: FunctionSymbol) -> list[dict[str, Any]]:
        caller_index = self._build_caller_index()
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, int, str]] = set()
        for target_key in self.function_target_keys(function):
            for edge in caller_index.get(target_key, []):
                key = (
                    edge["caller_file"],
                    edge["caller_function_id"],
                    int(edge["line"]),
                    edge["callee_expr"],
                )
                if key in seen:
                    continue
                seen.add(key)
                refs.append(dict(edge))
        refs.sort(key=lambda item: (item["caller_file"], item["line"], item["caller_function_id"]))
        return refs

    def function_target_keys(self, function: FunctionSymbol) -> list[str]:
        keys = [f"{function.file}::{function.qualified_name}"]
        keys.extend(function.exported_as)
        return list(dict.fromkeys(keys))

    def _build_caller_index(self) -> dict[str, list[dict[str, Any]]]:
        if self._caller_index is not None:
            return self._caller_index
        index: dict[str, list[dict[str, Any]]] = {}
        for relative_path in self.manifest.get("files_by_path", {}):
            facts = self.file_facts(relative_path)
            if facts is None or facts.parse_status != "ok":
                continue
            for function in facts.functions:
                for edge in function.call_edges:
                    for target in edge.resolved_targets:
                        index.setdefault(target, []).append(
                            {
                                "caller_file": function.file,
                                "caller_function_id": function.function_id,
                                "caller_qualified_name": function.qualified_name,
                                "line": edge.line,
                                "callee_expr": edge.callee_expr,
                                "returns_used": edge.returns_used,
                                "arg_exprs": list(edge.arg_exprs),
                            }
                        )
        self._caller_index = index
        return index


def _function_ref(function: FunctionSymbol) -> dict[str, Any]:
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
