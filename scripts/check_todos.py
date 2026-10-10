#!/usr/bin/env python
"""Fail on TODO/FIXME/HACK markers that are not linked to a tracking issue.

Debt markers must carry an issue reference like ``TODO(#123)`` (or
``TODO(owner)``) so stale debt is traceable. Unlinked markers are flagged.
Used by pre-commit and CI.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MARKER = re.compile(r"\b(TODO|FIXME|HACK)\b(?!\(#\d+\))(?!\([A-Za-z0-9_-]+\))")
EXCLUDED_DIRS = {".git", ".venv", ".pytest_cache", "node_modules", "data", "droid-wiki"}
ALLOWED_FILES = {Path("scripts/check_todos.py")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        default=["app", "tests", "scripts"],
        help="Files or directories to check (default: app tests scripts)",
    )
    args = parser.parse_args()

    failures: list[str] = []
    for raw in args.paths:
        path = Path(raw)
        candidates = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for file in candidates:
            if not file.is_file() or file in ALLOWED_FILES:
                continue
            if any(part in EXCLUDED_DIRS for part in file.parts):
                continue
            for lineno, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                if MARKER.search(line):
                    failures.append(f"{file}:{lineno}: {line.strip()}")

    if failures:
        print("Unlinked debt markers found (use TODO(#123) to link an issue):")
        for failure in failures:
            print(f"  {failure}")
        return 1

    print("No unlinked TODO/FIXME/HACK markers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
