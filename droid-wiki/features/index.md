# Features

Droid Forge's feature set is one pipeline: an issue enters through the webhook or the dashboard, a Droid fix session turns it into a pushed branch and a pull request, a second Droid session reviews that PR, and the dashboard tracks every run. Workspaces and restart recovery keep the pipeline running on one machine. Each page below covers one stage and names the code that implements it.

- [Issue to PR](issue-to-pr.md): the flagship workflow, from a labeled GitHub issue to a same-repo pull request and a completion comment.
- [PR review](pr-review.md): the read-only Droid review session, its verdict contract, review posting with the COMMENT fallback, and optional auto-merge.
- [Workspace management](workspace-management.md): per-task and per-review git clones under `WORKSPACE_ROOT`, authenticated remotes, setup commands, and reuse on restart.
- [Restart recovery](restart-recovery.md): how the service resumes or fails interrupted Droid sessions after a restart.
- [Dashboard](dashboard.md): the operator UI, its JSON endpoints, per-repo model and setup settings, and the assignment and follow-up controls.
