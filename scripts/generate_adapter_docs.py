from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lua_nil_review.adapter_docs import generated_files, legacy_files
from lua_nil_review.common import atomic_write_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate CODEAGENT.md from one shared source and remove legacy adapter docs.")
    parser.add_argument("--check", action="store_true", help="Exit non-zero if generated files are out of date.")
    args = parser.parse_args(argv)

    stale: list[Path] = []
    for path, content in generated_files(ROOT).items():
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing != content:
            stale.append(path)
            if not args.check:
                atomic_write_text(path, content)

    for path in legacy_files(ROOT):
        if path.exists():
            stale.append(path)
            if not args.check:
                path.unlink()

    if args.check:
        if stale:
            for path in stale:
                print(f"stale: {path.relative_to(ROOT).as_posix()}")
            return 1
        print("adapter docs are up to date")
        return 0

    if stale:
        for path in stale:
            print(f"wrote: {path.relative_to(ROOT).as_posix()}")
    else:
        print("adapter docs already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
