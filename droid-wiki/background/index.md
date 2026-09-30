# Background

This section explains why Droid Forge is built the way it is. It covers the design choices behind the code and the migration that moved the project off Cursor Cloud Agents onto Factory's Droid runtime.

The [overview](../overview/index.md) and [architecture](../overview/architecture.md) pages describe what the service does. These pages are about the reasons and the history, so they are worth reading before changing the dispatch, finalization, or review paths.

## Pages

- [Design decisions](design-decisions.md) walks through seven choices that shape `app/droid_client.py` and `app/worker.py`, each with the trade-off it buys.
- [Migration from Cursor](migration-from-cursor.md) is the dated record of the Cursor Forge to Droid Forge refactor: the old architecture, the API mapping, what was deleted, the schema rename, and the four bugs a real end-to-end run surfaced.

## Related pages

- [Overview](../overview/index.md) for what the service is.
- [Architecture](../overview/architecture.md) for the component map.
- [Glossary](../overview/glossary.md) for shared vocabulary.
- [Configuration](../reference/configuration.md) for the settings referenced here.
