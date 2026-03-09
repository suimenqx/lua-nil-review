from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timedelta, timezone
from hashlib import sha1, sha256
from pathlib import Path
from typing import Any, Iterable

STATE_VERSION = 1
ANALYZER_VERSION = "0.1.0"
RULE_ID = "lua.string-find-first-arg-nil"
RULE_VERSION = "1"
REPORT_TEMPLATE_VERSION = "1"
PARSER_VERSION = "luaparser-4.0.0"
SHARD_MAX_FINDINGS = 20
SHARD_MAX_BYTES = 120 * 1024
SHARD_TIMEOUT = timedelta(minutes=30)
SNIPPET_RADIUS = 3
SNIPPET_MAX_LINES = 30
BUILTIN_NON_NIL_CALLS = {
    "string.format",
    "table.concat",
    "tostring",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_stale(value: str | None, *, timeout: timedelta = SHARD_TIMEOUT) -> bool:
    stamp = parse_timestamp(value)
    if stamp is None:
        return True
    return datetime.now(timezone.utc) - stamp > timeout


def sha1_hex(value: str) -> str:
    return sha1(value.encode("utf-8")).hexdigest()


def sha256_hex(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def atomic_write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temp_path, path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def atomic_write_jsonl(path: Path, items: Iterable[Any]) -> None:
    lines = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in items]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def load_jsonl(path: Path) -> list[Any]:
    if not path.exists():
        return []
    items: list[Any] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                items.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return []
    return items


def remove_children(path: Path, suffixes: tuple[str, ...] | None = None) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            remove_children(child, suffixes=suffixes)
            try:
                child.rmdir()
            except OSError:
                pass
            continue
        if suffixes is None or child.suffix in suffixes:
            child.unlink(missing_ok=True)


def rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def owner_id(label: str) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{label}"
