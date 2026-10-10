---
name: run-service
description: Build, run, and interactively verify the Droid Forge service locally, including health/metrics/dashboard checks
---

# Run and verify Droid Forge

Use this skill to bring the service up and prove it works interactively.

## Steps

1. **Environment**: activate the venv and install dependencies if missing:
   ```bash
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
   A missing `.env` is fine for smoke testing; the service boots `degraded`
   without tokens.

2. **Run**: start the service on port 8000 (pick another port if busy):
   ```bash
   .venv/bin/uvicorn app.app:app --host 127.0.0.1 --port 8000
   ```

3. **Verify health**: `GET /health` must return 200. With no tokens, expect
   `"status": "degraded"` with `database: ok` — that is healthy enough for
   local QA. `healthy` requires valid GitHub/Droid credentials.

4. **Drive the app**:
   - `GET /metrics` returns task totals, KPIs, and daily series as JSON.
   - `GET /dashboard` must render HTML (status 200).
   - `GET /api/droid/models` lists available models (needs a Droid key).

5. **Stop**: kill the uvicorn process when done.

## Success criteria

- `/health` returns 200 with `database: ok`.
- `/metrics` returns a JSON object with a `total` key.
- `/dashboard` returns 200 and contains the words "Droid Forge".
