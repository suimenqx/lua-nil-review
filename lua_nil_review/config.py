from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .common import sha256_hex


@dataclass
class SymbolTracingConfig:
    enabled: bool = True
    flatten_require_mode: str = "basename"
    max_depth: int = 5
    auto_silence_depth: int = 3
    min_required_trace_depth: int = 3
    max_branch_count: int = 16
    max_expanded_nodes: int = 64
    max_unique_slices: int = 12
    slice_mode: str = "logic_slice"
    max_slice_lines: int = 60
    agentic_retrace_enabled: bool = True
    agentic_retrace_depth_bonus: int = 4
    agentic_retrace_max_branch_count: int = 32
    agentic_retrace_max_expanded_nodes: int = 192
    agentic_frontier_jump_limit: int = 4
    module_resolution_overrides: dict[str, list[str]] = field(default_factory=dict)
    module_resolution_priority: list[str] = field(default_factory=list)
    default_visible_risk_levels: list[int] = field(default_factory=lambda: [1, 2])

    def to_normalized_dict(self) -> dict[str, Any]:
        max_depth = max(int(self.max_depth), int(self.min_required_trace_depth))
        return {
            "enabled": bool(self.enabled),
            "flatten_require_mode": self.flatten_require_mode,
            "max_depth": max_depth,
            "auto_silence_depth": int(self.auto_silence_depth),
            "min_required_trace_depth": int(self.min_required_trace_depth),
            "max_branch_count": int(self.max_branch_count),
            "max_expanded_nodes": int(self.max_expanded_nodes),
            "max_unique_slices": int(self.max_unique_slices),
            "slice_mode": self.slice_mode,
            "max_slice_lines": int(self.max_slice_lines),
            "agentic_retrace_enabled": bool(self.agentic_retrace_enabled),
            "agentic_retrace_depth_bonus": int(self.agentic_retrace_depth_bonus),
            "agentic_retrace_max_branch_count": int(self.agentic_retrace_max_branch_count),
            "agentic_retrace_max_expanded_nodes": int(self.agentic_retrace_max_expanded_nodes),
            "agentic_frontier_jump_limit": int(self.agentic_frontier_jump_limit),
            "module_resolution_overrides": {
                key: sorted(dict.fromkeys(paths))
                for key, paths in sorted(self.module_resolution_overrides.items())
            },
            "module_resolution_priority": list(dict.fromkeys(self.module_resolution_priority)),
            "default_visible_risk_levels": sorted({int(level) for level in self.default_visible_risk_levels}),
        }


@dataclass
class ReviewConfig:
    include: list[str] = field(default_factory=lambda: ["*.lua", "**/*.lua"])
    exclude: list[str] = field(default_factory=list)
    nil_guards: list[str] = field(default_factory=lambda: ["assert"])
    safe_wrappers: list[str] = field(default_factory=list)
    suppressions: list[Any] = field(default_factory=list)
    baseline: str | None = None
    symbol_tracing: SymbolTracingConfig = field(default_factory=SymbolTracingConfig)

    def to_normalized_dict(self) -> dict[str, Any]:
        suppressions = []
        for entry in self.suppressions:
            if isinstance(entry, str):
                suppressions.append({"finding_id": entry})
            else:
                suppressions.append(entry)
        normalized = {
            "include": sorted(set(self.include)),
            "exclude": sorted(set(self.exclude)),
            "nil_guards": sorted(set(self.nil_guards)),
            "safe_wrappers": sorted(set(self.safe_wrappers)),
            "suppressions": sorted(suppressions, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False)),
            "baseline": self.baseline,
            "symbol_tracing": self.symbol_tracing.to_normalized_dict(),
        }
        return normalized

    def fingerprint(self) -> str:
        return sha256_hex(json.dumps(self.to_normalized_dict(), ensure_ascii=False, sort_keys=True))

    def matches(self, relative_path: str) -> bool:
        path = PurePosixPath(relative_path)
        if self.include:
            include_match = any(path.match(pattern) or relative_path == pattern.lstrip("./") for pattern in self.include)
            if not include_match:
                return False
        if self.exclude and any(path.match(pattern) or relative_path == pattern.lstrip("./") for pattern in self.exclude):
            return False
        return True


def load_config(root: Path, config_path: str | None) -> tuple[ReviewConfig, Path | None]:
    path = Path(config_path) if config_path else root / ".lua-nil-review.json"
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        return ReviewConfig(), None
    raw = json.loads(path.read_text(encoding="utf-8"))
    config = ReviewConfig(
        include=list(raw.get("include", ["*.lua", "**/*.lua"])),
        exclude=list(raw.get("exclude", [])),
        nil_guards=list(raw.get("nil_guards", ["assert"])),
        safe_wrappers=list(raw.get("safe_wrappers", [])),
        suppressions=list(raw.get("suppressions", [])),
        baseline=raw.get("baseline"),
        symbol_tracing=SymbolTracingConfig(
            **{
                **SymbolTracingConfig().to_normalized_dict(),
                **dict(raw.get("symbol_tracing", {})),
            }
        ),
    )
    if "assert" not in config.nil_guards:
        config.nil_guards.append("assert")
    return config, path
