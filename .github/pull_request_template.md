## What does this PR change?

<!-- One or two sentences: the what and the why. Link related issues with "Fixes #123". -->

## How was it tested?

<!-- Commands run and their result, e.g. `pytest -q` (101 passed). Include the test for new behavior. -->

## Checklist

- [ ] `ruff check` and `ruff format --check` pass
- [ ] `mypy app/` passes (strict)
- [ ] `pytest -q` passes locally
- [ ] `docs/openapi.json` regenerated if API routes changed (`scripts/export_openapi.py`)
- [ ] No secrets in the diff
