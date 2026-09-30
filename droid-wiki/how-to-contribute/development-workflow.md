# Development workflow

The loop is branch, code, test, PR, merge. The whole service runs in one uvicorn process on your machine, so the feedback loop is fast, and end-to-end validation happens against a throwaway GitHub repo before anything touches a real repository.

## Branch

Branch from the default branch. Keep one change per branch with a descriptive name; the one merged PR in the history used `cursor/setup-dev-environment-9881`, and any equally clear scheme is fine. Short-lived branches keep review small.

## Set up

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in at least `GITHUB_WEBHOOK_SECRET`, `GITHUB_TOKEN`, and `FACTORY_API_KEY` in `.env`. For local runs you can instead leave the Factory key empty and set `DROID_ALLOW_CLI_AUTH=true` to reuse the droid CLI's own login. Every variable is described in the README environment table and in [Configuration](../reference/configuration.md).

## Run

```bash
mkdir -p data
uvicorn app.app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000/dashboard. Check http://localhost:8000/health; it reports database, GitHub, and Droid connectivity, and the app boots degraded if tokens are missing, so an unconfigured Droid is not a blocker for UI work.

Startup runs lightweight migrations, recovers interrupted Droid sessions, and starts the embedded worker loop (`worker_loop` in `app/worker.py`) alongside the API, all inside the single process. On shutdown the worker stops and in-flight runs are cancelled; sessions persist on disk and resume on the next start.

## Test

```bash
pytest -q
```

The suite is 85 tests, fully offline, and finishes in seconds. Run it before every commit. One file at a time: `pytest tests/test_worker.py -q`. Details are in [Testing](testing.md).

## Point the service at a throwaway GitHub repo

Do not validate against a repository you care about. The fix agent pushes branches and opens PRs to whatever repo is registered, so use a sandbox you own:

1. Create a small test repo (or fork a small project) under your GitHub account. The end-to-end validation that found four real bugs ran against a roughly 1,000-file fork, so the bigger the repo, the more realistic the test, but any disposable repo works.
2. Create a personal access token with write access to that repo and put it in `GITHUB_TOKEN`. The token gets embedded in clone remotes, so use a dedicated, scoped token.
3. Register the repo: dashboard, Repositories tab, paste the URL, click Add repository. Or `curl -X POST http://localhost:8000/api/repositories -H 'content-type: application/json' -d '{"url":"https://github.com/you/sandbox"}'`.
4. Seed issues. `scripts/create_superset_issues.sh` is the template: `REPO=you/sandbox LABEL=Droid-complete ./scripts/create_superset_issues.sh` creates the trigger label plus two sample issues with acceptance criteria. It needs `gh` authenticated with write access.
5. Trigger a run: label an issue `Droid-complete`, or select it on the dashboard Issues tab and click Assign to Droid (this works without a tunnel).
6. Watch: structured logs in the server terminal, the dashboard activity feed, the issue's completion comment, and the PR that forge opens.

Keep `REVIEW_AUTO_MERGE=false` so the sandbox PR is never merged automatically. When you are done, delete the branches and issues, or the whole repo.

## Commit, PR, merge

Run `pytest -q` one more time, then commit. The house style, visible throughout `git log`, is an imperative subject line and a body that explains the reasoning, with bullets when there is more than one point. If an agent wrote the change, add its `Co-authored-by` trailer.

Push the branch and open a pull request against the default branch. Keep the diff reviewable: the 2026-09-28 refactor was a single 33-file commit and was still reviewed through its test coverage and a written rationale in the message.

## After merge

If your change touched the launch, finalize, or review path, run the throwaway-repo flow once more after merge. The end-to-end path is where the suite's blind spots live; the completion-comment and permission-reject bugs both shipped green on unit tests.
