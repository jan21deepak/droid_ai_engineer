#!/usr/bin/env python
"""Fail if any Python source file exceeds the maximum line count.

Large files concentrate complexity and defeat focused agent review; this hook
keeps modules reviewable. Used by pre-commit and CI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MAX_LINES = 1000
EXCLUDED_DIRS = {".git", ".venv", ".pytest_cache", "node_modules", "data", "droid-wiki"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        default=["app", "tests", "scripts"],
        help="Files or directories to check (default: app tests scripts)",
    )
    parser.add_argument("--max-lines", type=int, default=MAX_LINES)
    args = parser.parse_args()

    failures: list[tuple[Path, int]] = []
    for raw in args.paths:
        path = Path(raw)
        candidates = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for file in candidates:
            if not file.is_file() or any(part in EXCLUDED_DIRS for part in file.parts):
                continue
            lines = len(file.read_text(encoding="utf-8").splitlines())
            if lines > args.max_lines:
                failures.append((file, lines))

    if failures:
        for file, lines in failures:
            print(f"{file}: {lines} lines exceeds the {args.max_lines}-line limit")
        return 1

    print(f"All files are within the {args.max_lines}-line limit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
