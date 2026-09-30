# Glossary

| Term | Meaning |
| --- | --- |
| **Task** | One issue-fix run: a `Task` row plus a Droid fix session in a `task-<id>` workspace. |
| **Review task** | One PR review run: a `ReviewTask` row plus a Droid review session in a `review-<id>` workspace. |
| **Droid session** | A conversation with the Droid agent, created by `droid_sdk.Session` and identified by `droid_session_id`. Sessions persist on disk and can be resumed. |
| **Turn** | A single prompt-and-response cycle inside a session. A fix task is typically one turn, plus optional follow-up turns. |
| **Workspace** | A git clone of the target repository under `WORKSPACE_ROOT`, one per task or review. |
| **Fix session** | A Droid session that implements an issue and pushes a branch. Runs at `DROID_AUTONOMY`. |
| **Review session** | A read-only Droid session that inspects a PR diff and returns a verdict. Runs at `DROID_REVIEW_AUTONOMY`. |
| **Trigger label** | The GitHub label that starts a task, `Droid-complete` by default. |
| **Verdict** | The `VERDICT: APPROVE` or `VERDICT: REQUEST_CHANGES` line the review agent must end with. Parsed by `extract_verdict`. |
| **Branch line** | The `BRANCH: <name>` line a fix agent must end with. Parsed by `extract_branch_name`. |
| **Autonomy** | How much a Droid session may do without asking. Levels are `off`, `low`, `medium`, `high` (`droid_sdk.Autonomy`). |
| **Finalization** | The callback that runs after a turn ends: persisting the outcome, creating the PR, posting comments and reviews. |
| **Recovery** | Resuming sessions that a restart orphaned, or failing rows that never got a session. |
| **Factory credits** | Usage units reported by the Droid SDK per turn, stored as `factory_credits`. |
| **PR state** | The reconciled GitHub state of a task's PR: `open`, `closed`, or `merged`. |
