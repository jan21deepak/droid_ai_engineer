#!/usr/bin/env python
"""Export the FastAPI OpenAPI schema to docs/openapi.json.

The committed schema is the API contract; CI regenerates it and fails when the
committed copy is stale. Run after changing any request/response shape.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.app import app  # noqa: E402

OUT = ROOT / "docs" / "openapi.json"


def main() -> int:
    schema = app.openapi()
    OUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths = len(schema.get("paths", {}))
    print(f"Wrote {OUT.relative_to(ROOT)} ({paths} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
