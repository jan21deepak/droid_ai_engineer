# Systems

These pages explain how Droid Forge works inside the single uvicorn process: the local agent client, the event-driven worker, the GitHub API surface, webhook dispatch, SQLite persistence, and observability. Every claim traces to a file under `app/`.

- [Droid client](droid-client.md): workspaces, Droid sessions, prompts, and the run registry that streams agent turns in the background.
- [Worker](worker.md): turn-completion handlers that turn agent output into pull requests, comments, reviews, and merges, plus restart recovery and the polling loop.
- [GitHub integration](github-integration.md): the REST client, webhook signature verification, trigger rules, and repo launch-config helpers.
- [Webhook and dispatch](webhook-and-dispatch.md): how a labeled issue enters the system and why dispatch returns 202 immediately.
- [Persistence](persistence.md): engine and session management, the tables, `TaskStatus`, and the additive migration runner.
- [Observability](observability.md): structured logging, the health endpoint, and the metrics computed from SQLite.

For the component map, see [Architecture](../overview/architecture.md). For the end-to-end workflow, see [Issue to PR](../features/issue-to-pr.md). For settings, see [Configuration](../reference/configuration.md).
