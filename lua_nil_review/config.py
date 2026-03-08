from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .common import sha256_hex


@dataclass
class ReviewConfig:
    include: list[str] = field(default_factory=lambda: ["*.lua", "**/*.lua"])
    exclude: list[str] = field(default_factory=list)
    nil_guards: list[str] = field(default_factory=lambda: ["assert"])
    safe_wrappers: list[str] = field(default_factory=list)
    suppressions: list[Any] = field(default_factory=list)
    baseline: str | None = None

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
    )
    if "assert" not in config.nil_guards:
        config.nil_guards.append("assert")
    return config, path
